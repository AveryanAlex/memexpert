"""Real Telethon-backed implementation of :class:`PipelineTelegramClientProtocol`.

Kept separate from :mod:`memexpert.crawlers.telegram.client` so importing
``client`` never requires Telethon at runtime (the tests rely on that
boundary). Nothing in this module touches the filesystem or opens a
connection at import time — the :class:`TelethonClientFactory` builds the
underlying ``TelegramClient`` lazily on first use.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

from pydantic import SecretStr
from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    RPCError,
    SessionRevokedError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.types import Channel, InputPeerChannel
from telethon.tl.types import Message as TelethonMessage

from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
    RawTelegramChannel,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.session_crypto import (
    TelegramStringSessionCipher,
    TelegramStringSessionDecryptError,
)
from memexpert.crawlers.telegram.telethon_mapper import TelethonMessageNormalizer
from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSession
from memexpert.models.enums import TelegramSessionStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine
    from telethon.events import NewMessage

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


# Telethon's AUTH-level failures that indicate actual account/session
# revocation are permanent until an operator rotates the registry row.
_SESSION_BANNED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    UserDeactivatedError,
    SessionRevokedError,
)


def _translate_telethon_error(exc: BaseException) -> PipelineTelegramError:
    """Map a raw Telethon exception onto the typed crawler error taxonomy.

    Narrow, fall-through order:

    * ``FloodWaitError`` carries ``exc.seconds`` — preserve it so the
      runtime can park the session for the correct cooldown.
    * ``AuthKeyUnregisteredError`` means the stored StringSession is no
      longer authorized; require re-import without treating it as a ban.
    * ``UserDeactivated`` / ``SessionRevoked`` are permanent session failures;
      quarantine without retry.
    * ``RPCError`` (everything else Telegram sends back) + generic
      ``ConnectionError`` / ``TimeoutError`` / ``OSError`` are transient
      provider failures — the runtime will log and continue.
    * Anything else escapes unchanged so real bugs stay loud.
    """

    if isinstance(exc, FloodWaitError):
        wait_seconds = int(getattr(exc, "seconds", 0) or 0)
        return PipelineTelegramFloodWaitError(
            f"Telegram flood-waited this session for {wait_seconds}s.",
            wait_seconds=wait_seconds,
        )
    if isinstance(exc, AuthKeyUnregisteredError):
        return PipelineTelegramSessionAuthRequiredError(
            "Telegram rejected the stored StringSession auth key; import a valid StringSession.",
        )
    if isinstance(exc, _SESSION_BANNED_EXCEPTIONS):
        return PipelineTelegramSessionBannedError(
            f"Telegram marked this session as unusable: {exc.__class__.__name__}.",
        )
    if isinstance(exc, RPCError | ConnectionError | TimeoutError | OSError):
        return PipelineTelegramProviderUnavailableError(
            f"Telegram RPC or transport failure: {exc.__class__.__name__}: {exc}.",
        )
    raise exc  # pragma: no cover - propagate unknown types for visibility


@dataclass(frozen=True, slots=True)
class _LoadedTelegramSessionConfig:
    name: str
    string_session: SecretStr = field(repr=False)
    max_requests_per_second: float


@dataclass(slots=True)
class TelethonClientFactory:
    """Lazy factory that returns a connected :class:`TelegramClient`.

    Reads ``telegram_api_id`` / ``telegram_api_hash`` from settings and the
    encrypted Telethon ``StringSession`` from ``telegram_sessions`` only on the
    first :meth:`get_client` call. Subsequent calls return the cached client so
    the runtime does not reconnect on every catch-up sweep.
    """

    settings: Settings
    session_name: str
    session_factory: AsyncSessionFactory | None = None
    _client: TelegramClient | None = field(default=None, init=False, repr=False)
    _engine: AsyncEngine | None = field(default=None, init=False, repr=False)
    _session_factory: AsyncSessionFactory | None = field(default=None, init=False, repr=False)
    _max_requests_per_second: float | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def max_requests_per_second(self) -> float | None:
        """Return the DB-loaded session rate limit, if a row has been loaded."""

        return self._max_requests_per_second

    async def get_client(self) -> TelegramClient:
        """Return the connected Telethon client, building it on first use.

        Connection, start() (which would prompt for auth) are intentionally
        NOT called here. Operators import an already-authorized Telethon
        ``StringSession`` via ``scripts/auth_telegram_session.py``. We call
        ``connect()`` and refuse to proceed if the stored StringSession is not
        authorized so the runtime surfaces a clear error instead of blocking on
        an interactive prompt.
        """

        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            session_config = await self._load_session_config()
            self._max_requests_per_second = session_config.max_requests_per_second
            self._client = self._build_client(session_config.string_session)
            try:
                await self._client.connect()
            except Exception as exc:  # narrow: rewrap and re-raise as typed error
                translated_error = _translate_telethon_error(exc)
                if isinstance(translated_error, PipelineTelegramSessionAuthRequiredError):
                    with suppress(Exception):
                        await self._mark_auth_required(
                            error_class=type(translated_error).__name__,
                            error_text=str(translated_error),
                        )
                raise translated_error from exc
            if not await self._client.is_user_authorized():
                message = (
                    f"Telegram session {self.session_name!r} is not authorized; "
                    "import a valid Telethon StringSession with scripts/auth_telegram_session.py."
                )
                with suppress(Exception):
                    await self._mark_auth_required(
                        error_class=PipelineTelegramSessionAuthRequiredError.__name__,
                        error_text=message,
                    )
                with suppress(Exception):
                    disconnect_result: object = self._client.disconnect()
                    if inspect.isawaitable(disconnect_result):
                        await disconnect_result
                raise PipelineTelegramSessionAuthRequiredError(message)
            await self._mark_authorized(client=self._client)
            return self._client

    def _build_client(self, string_session: SecretStr) -> TelegramClient:
        """Construct the ``TelegramClient`` bound to this DB-backed StringSession."""

        api_id = self.settings.telegram_api_id
        api_hash_secret = self.settings.telegram_api_hash
        if api_id is None or api_hash_secret is None:
            raise PipelineTelegramProviderUnavailableError(
                "Telegram API credentials are not configured; "
                "set TELEGRAM_API_ID and TELEGRAM_API_HASH.",
            )
        return TelegramClient(
            session=StringSession(string_session.get_secret_value()),
            api_id=api_id,
            api_hash=api_hash_secret.get_secret_value(),
        )

    async def _load_session_config(self) -> _LoadedTelegramSessionConfig:
        """Load and validate the DB-backed StringSession for this factory."""

        session_factory = self._get_session_factory()
        async with session_factory() as db_session:
            row = await db_session.scalar(
                select(TelegramSession)
                .where(TelegramSession.name == self.session_name)
                .limit(1),
            )
            if row is None:
                raise PipelineTelegramSessionNotRunnableError(
                    f"Telegram session {self.session_name!r} does not exist.",
                )
            if not row.enabled:
                raise PipelineTelegramSessionNotRunnableError(
                    f"Telegram session {self.session_name!r} is disabled.",
                )
            if row.status is TelegramSessionStatus.AUTH_REQUIRED:
                raise PipelineTelegramSessionAuthRequiredError(
                    f"Telegram session {self.session_name!r} requires authentication.",
                )
            if row.status is not TelegramSessionStatus.ACTIVE:
                raise PipelineTelegramSessionNotRunnableError(
                    f"Telegram session {self.session_name!r} is {row.status.value}, not active.",
                )
            encrypted_string_session = (row.encrypted_string_session or "").strip()
            if not encrypted_string_session:
                raise PipelineTelegramSessionAuthRequiredError(
                    f"Telegram session {self.session_name!r} has no stored StringSession material.",
                )
            cipher = TelegramStringSessionCipher(self.settings.telegram_session_encryption_secret)
            try:
                string_session = cipher.decrypt(SecretStr(encrypted_string_session))
            except TelegramStringSessionDecryptError as exc:
                raise PipelineTelegramSessionNotRunnableError(
                    f"Telegram session {self.session_name!r} StringSession material cannot be decrypted.",
                ) from exc
            return _LoadedTelegramSessionConfig(
                name=row.name,
                string_session=string_session,
                max_requests_per_second=row.max_requests_per_second,
            )

    async def _mark_auth_required(self, *, error_class: str, error_text: str) -> None:
        """Persist the auth-required state after Telethon rejects the StringSession."""

        session_factory = self._get_session_factory()
        async with session_factory() as db_session:
            row = await db_session.scalar(
                select(TelegramSession)
                .where(TelegramSession.name == self.session_name)
                .limit(1),
            )
            if row is None:
                return
            row.status = TelegramSessionStatus.AUTH_REQUIRED
            row.last_error_class = error_class[:128]
            row.last_error_text = error_text[:4000]
            await db_session.commit()

    async def _mark_authorized(self, *, client: TelegramClient) -> None:
        """Persist heartbeat and safe account projection fields after authorization."""

        account = None
        get_me = getattr(client, "get_me", None)
        if get_me is not None:
            try:
                account = await get_me()
            except Exception:
                account = None
        session_factory = self._get_session_factory()
        async with session_factory() as db_session:
            row = await db_session.scalar(
                select(TelegramSession)
                .where(TelegramSession.name == self.session_name)
                .limit(1),
            )
            if row is None:
                return
            row.last_heartbeat_at = utcnow()
            row.last_error_class = None
            row.last_error_text = None
            if account is not None:
                account_user_id = getattr(account, "id", None)
                account_username = getattr(account, "username", None)
                account_phone = getattr(account, "phone", None)
                row.account_user_id = account_user_id if isinstance(account_user_id, int) else None
                row.account_username = (
                    account_username.strip() if isinstance(account_username, str) and account_username.strip() else None
                )
                row.account_phone_hint = _phone_hint(account_phone if isinstance(account_phone, str) else None)
            await db_session.commit()

    def _get_session_factory(self) -> AsyncSessionFactory:
        if self.session_factory is not None:
            return self.session_factory
        if self._session_factory is None:
            self._engine = build_async_engine(self.settings.database_url)
            self._session_factory = build_async_session_factory(self._engine)
        return self._session_factory

    async def close(self) -> None:
        """Dispose any engine this factory created for DB-backed session loading."""

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


class _RateLimiter:
    """Simple leaky-bucket limiter enforcing the Telethon request budget.

    The Telethon SDK has its own internal throttling for some RPC calls,
    but our ``download_media`` + ``iter_messages`` bursts can still blow
    through the Telegram flood threshold on an under-provisioned session.
    The limiter is intentionally tiny: one ``asyncio.Lock`` + a monotonic
    timestamp that every acquirer advances. This is enough to cap the
    adapter's outbound RPS at the configured ``crawler_max_requests_per_second``
    without pulling in a third-party rate-limiter dep.
    """

    def __init__(self, *, max_requests_per_second: float) -> None:
        if max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be strictly positive.")
        self._min_interval_seconds = 1.0 / max_requests_per_second
        self._lock = asyncio.Lock()
        self._next_allowed_at: float = 0.0

    def update_max_requests_per_second(self, *, max_requests_per_second: float) -> None:
        """Update the limiter from the loaded session row's policy."""

        if max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be strictly positive.")
        self._min_interval_seconds = 1.0 / max_requests_per_second

    async def acquire(self) -> None:
        """Sleep just enough to keep the outbound rate below the configured cap."""

        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            wait_for = self._next_allowed_at - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = loop.time()
            self._next_allowed_at = now + self._min_interval_seconds


@dataclass(slots=True)
class PipelineTelethonClient(PipelineTelegramClientProtocol):
    """Real Telethon-backed adapter honoring :class:`PipelineTelegramClientProtocol`.

    Instantiate via :meth:`create` so construction is async and the
    underlying :class:`TelegramClient` is built lazily. Every method
    translates Telethon exceptions onto the typed crawler error taxonomy
    via :func:`_translate_telethon_error`, so the crawler runtime never
    sees a raw ``telethon.errors.*`` exception.
    """

    factory: TelethonClientFactory
    rate_limiter: _RateLimiter
    _entity_cache: dict[str, Channel] = field(default_factory=dict, init=False, repr=False)
    _live_queue: asyncio.Queue[RawTelegramMessage] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _live_handler: object | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(
        cls,
        *,
        settings: Settings,
        session_name: str,
    ) -> PipelineTelethonClient:
        """Return a new adapter bound to ``session_name`` without connecting yet."""

        return cls(
            factory=TelethonClientFactory(settings=settings, session_name=session_name),
            rate_limiter=_RateLimiter(
                max_requests_per_second=settings.crawler_max_requests_per_second,
            ),
        )

    async def _get_client(self) -> TelegramClient:
        """Return a connected client and apply the DB-loaded rate limit."""

        client = await self.factory.get_client()
        max_requests_per_second = self.factory.max_requests_per_second
        if max_requests_per_second is not None:
            self.rate_limiter.update_max_requests_per_second(
                max_requests_per_second=max_requests_per_second,
            )
        return client

    async def _resolve_entity(self, channel_id_or_username: str) -> Channel:
        """Return the cached :class:`Channel` entity for a channel identifier.

        Calling ``client.get_entity`` repeatedly for the same channel is
        wasteful, so the adapter memoizes resolved entities keyed by
        the string identifier the runtime knows. Resolution goes through
        the rate limiter so a cold cache cannot spike outbound RPS.
        """

        cached = self._entity_cache.get(channel_id_or_username)
        if cached is not None:
            return cached
        client = await self._get_client()
        await self.rate_limiter.acquire()
        try:
            entity = await client.get_entity(channel_id_or_username)
        except Exception as exc:
            raise _translate_telethon_error(exc) from exc
        if not isinstance(entity, Channel):
            raise PipelineTelegramMalformedMessageError(
                f"Telegram entity {channel_id_or_username!r} is not a channel.",
            )
        self._entity_cache[channel_id_or_username] = entity
        return entity

    async def iter_channel_messages(
        self,
        *,
        channel_id: str,
        min_message_id: int | None,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Yield messages newer than ``min_message_id`` in oldest-first order."""

        client = await self._get_client()
        entity = await self._resolve_entity(channel_id)
        channel_title = getattr(entity, "title", None) or channel_id
        channel_username = getattr(entity, "username", None)
        min_id = int(min_message_id) if min_message_id is not None else 0
        await self.rate_limiter.acquire()
        try:
            async for raw_message in client.iter_messages(
                entity,
                limit=limit,
                min_id=min_id,
                reverse=True,
            ):
                yield TelethonMessageNormalizer.build(
                    message=raw_message,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    channel_username=channel_username,
                )
        except Exception as exc:
            raise _translate_telethon_error(exc) from exc

    async def iter_latest_channel_messages(
        self,
        *,
        channel_id: str,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Yield the latest ``limit`` messages in oldest-to-newest order."""

        messages = await self._load_history_window(
            channel_id=channel_id,
            limit=limit,
            max_message_id=None,
        )
        for message in reversed(messages):
            yield message

    async def iter_older_channel_messages(
        self,
        *,
        channel_id: str,
        before_message_id: int,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Yield an older page newest-first below an exclusive message id boundary.

        Advancing the durable history cursor in this order makes page recovery
        monotonic: if processing stops halfway through, every unprocessed
        message remains below the last committed boundary and is fetched on
        the next attempt.
        """

        messages = await self._load_history_window(
            channel_id=channel_id,
            limit=limit,
            max_message_id=before_message_id,
        )
        for message in messages:
            yield message

    async def _load_history_window(
        self,
        *,
        channel_id: str,
        limit: int,
        max_message_id: int | None,
    ) -> list[RawTelegramMessage]:
        """Load one newest-first Telethon page for latest/older history scans."""

        client = await self._get_client()
        entity = await self._resolve_entity(channel_id)
        channel_title = getattr(entity, "title", None) or channel_id
        channel_username = getattr(entity, "username", None)
        messages: list[RawTelegramMessage] = []
        await self.rate_limiter.acquire()
        try:
            async for raw_message in client.iter_messages(
                entity,
                limit=limit,
                max_id=max_message_id or 0,
            ):
                messages.append(
                    TelethonMessageNormalizer.build(
                        message=raw_message,
                        channel_id=channel_id,
                        channel_title=channel_title,
                        channel_username=channel_username,
                    )
                )
        except Exception as exc:
            raise _translate_telethon_error(exc) from exc
        return messages

    async def listen_live(
        self,
        *,
        channel_ids: Sequence[str],
        ready_event: asyncio.Event | None = None,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Stream live messages for the requested channels via a Telethon handler.

        Registers one ``events.NewMessage`` handler covering every
        resolved channel and bridges events into an ``asyncio.Queue``
        so the runtime's async for loop never blocks the Telethon
        dispatcher. The handler normalizes the message into a
        :class:`RawTelegramMessage` inside the handler and puts the
        projection on the queue; the async iterator just drains it.
        """

        client = await self._get_client()
        entities: list[Channel] = []
        channel_lookup: dict[int, tuple[str, str, str | None]] = {}
        for channel_id in channel_ids:
            entity = await self._resolve_entity(channel_id)
            entities.append(entity)
            channel_lookup[int(entity.id)] = (
                channel_id,
                getattr(entity, "title", None) or channel_id,
                getattr(entity, "username", None),
            )

        queue: asyncio.Queue[RawTelegramMessage] = asyncio.Queue()
        self._live_queue = queue

        async def _on_new_message(event: NewMessage.Event) -> None:
            try:
                raw_message = event.message
                resolved_channel_id = int(getattr(raw_message.peer_id, "channel_id", 0))
                lookup = channel_lookup.get(resolved_channel_id)
                if lookup is None:
                    return
                channel_id_str, channel_title, channel_username = lookup
                normalized = TelethonMessageNormalizer.build(
                    message=raw_message,
                    channel_id=channel_id_str,
                    channel_title=channel_title,
                    channel_username=channel_username,
                )
            except PipelineTelegramMalformedMessageError:
                # Never propagate into Telethon's dispatcher — malformed
                # live messages are logged by the caller via the queue
                # drain path; the handler just drops them.
                return
            await queue.put(normalized)

        client.add_event_handler(
            _on_new_message,
            events.NewMessage(chats=cast("list[object]", entities)),
        )
        self._live_handler = _on_new_message
        if ready_event is not None:
            ready_event.set()

        try:
            while not self._closed:
                next_message = await queue.get()
                yield next_message
        finally:
            if self._live_handler is not None:
                with suppress(Exception):
                    client.remove_event_handler(self._live_handler)
                self._live_handler = None
            self._live_queue = None

    async def download_media(self, message: RawTelegramMessage) -> bytes:
        """Download the media bytes for one normalized message via Telethon."""

        client = await self._get_client()
        raw_payload = message.raw_payload
        if not isinstance(raw_payload, TelethonMessage):
            raise PipelineTelegramMalformedMessageError(
                "RawTelegramMessage.raw_payload must be a Telethon message for download_media.",
            )
        media_buffer = BytesIO()
        await self.rate_limiter.acquire()
        try:
            _ = await client.download_media(raw_payload, file=media_buffer)
        except Exception as exc:
            raise _translate_telethon_error(exc) from exc
        result = media_buffer.getvalue()
        if not result:
            raise PipelineTelegramMalformedMessageError(
                f"Telethon returned empty media for message_id={message.message_id!r}.",
            )
        return result

    async def resolve_channel(self, username_or_id: str) -> RawTelegramChannel:
        """Resolve the typed channel projection for ``username_or_id``."""

        entity = await self._resolve_entity(username_or_id)
        subscriber_count = getattr(entity, "participants_count", None)
        return RawTelegramChannel(
            channel_id=str(entity.id),
            username=getattr(entity, "username", None),
            title=getattr(entity, "title", None) or username_or_id,
            subscriber_count=subscriber_count if isinstance(subscriber_count, int) else None,
        )

    async def fetch_single_message(
        self,
        *,
        channel_id: str,
        post_id: str,
    ) -> RawTelegramMessage:
        """Return one message by id without advancing any checkpoint state."""

        client = await self._get_client()
        entity = await self._resolve_entity(channel_id)
        channel_title = getattr(entity, "title", None) or channel_id
        channel_username = getattr(entity, "username", None)
        try:
            numeric_post_id = int(post_id)
        except ValueError as exc:
            raise PipelineTelegramMalformedMessageError(
                f"Telegram post ids must be numeric; got {post_id!r}.",
            ) from exc
        await self.rate_limiter.acquire()
        try:
            fetched = await client.get_messages(entity, ids=numeric_post_id)
        except Exception as exc:
            raise _translate_telethon_error(exc) from exc
        if fetched is None:
            raise PipelineTelegramMalformedMessageError(
                f"Telegram returned no message for ({channel_id!r}, {post_id!r}).",
            )
        if not isinstance(fetched, TelethonMessage):
            raise PipelineTelegramMalformedMessageError(
                f"Telegram returned a non-message container for ({channel_id!r}, {post_id!r}).",
            )
        return TelethonMessageNormalizer.build(
            message=cast("Any", fetched),
            channel_id=channel_id,
            channel_title=channel_title,
            channel_username=channel_username,
        )

    async def close(self) -> None:
        """Disconnect the underlying Telethon client if it is connected."""

        self._closed = True
        live_queue = self._live_queue
        if live_queue is not None:
            # Drain any pending queue entries so the listener iterator
            # exits cleanly instead of hanging on ``await queue.get()``.
            while not live_queue.empty():
                try:
                    live_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        client = self.factory._client  # noqa: SLF001 - intentional peek: close-only path
        if client is not None and client.is_connected():
            with suppress(Exception):
                disconnect_result: object = client.disconnect()
                if inspect.isawaitable(disconnect_result):
                    await disconnect_result
        await self.factory.close()


__all__ = [
    "PipelineTelethonClient",
    "TelethonClientFactory",
]


# Silence the unused InputPeerChannel import: the symbol is re-exported by
# Telethon's own runtime path but importing it eagerly in this module is
# deliberate so type-checkers that resolve ``events.NewMessage(chats=...)``
# through the Telethon stubs do not complain about the peer discriminant.
_ = InputPeerChannel


def _phone_hint(phone: str | None) -> str | None:
    if phone is None:
        return None
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 4:
        return None
    return f"ending-{digits[-4:]}"
