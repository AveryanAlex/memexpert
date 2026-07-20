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
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar, cast

from pydantic import SecretStr
from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    RPCError,
    ServerError,
    SessionRevokedError,
    TimedOutError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, InputPeerChannel
from telethon.tl.types import Message as TelethonMessage
from telethon.tl.types import MessageService as TelethonMessageService
from telethon.utils import get_input_channel, get_peer_id

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
    RawTelegramChannelAudience,
    RawTelegramMessage,
    TelegramLiveEvent,
    TelegramMessageEditedEvent,
    TelegramMessagesDeletedEvent,
    TelegramNewMessageEvent,
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
    from collections.abc import AsyncIterator, Awaitable, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Telethon's AUTH-level failures that indicate actual account/session
# revocation are permanent until an operator rotates the registry row.
_SESSION_BANNED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    UserDeactivatedError,
    SessionRevokedError,
)
_EXHAUSTED_REQUEST_PATTERN = re.compile(r"\bRequest was unsuccessful \d+ time\(s\)", re.IGNORECASE)


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

    if _EXHAUSTED_REQUEST_PATTERN.search(str(exc)):
        return PipelineTelegramProviderUnavailableError(
            "Telegram exhausted its internal request retries before receiving a response.",
        )
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


def _requires_client_reset(exc: BaseException) -> bool:
    """Return whether a failed operation may have poisoned Telethon transport state."""

    return isinstance(exc, (ConnectionError, TimeoutError, OSError, ServerError, TimedOutError)) or bool(
        _EXHAUSTED_REQUEST_PATTERN.search(str(exc)),
    )


def _log_operation_failure(
    *,
    operation: str,
    session_name: str,
    exc: BaseException,
    translated_error: PipelineTelegramError,
    timeout_seconds: float,
    client_reset: bool,
    channel_id: str | None = None,
    post_id: str | None = None,
) -> None:
    """Emit a stable, queryable operation failure without leaking credentials."""

    logger.warning(
        "telegram_operation_failed",
        extra={
            "event": "telegram_operation_failed",
            "operation": operation,
            "session_name": session_name,
            "channel_id": channel_id,
            "post_id": post_id,
            "timeout_seconds": timeout_seconds,
            "error_class": type(exc).__name__,
            "translated_error_class": type(translated_error).__name__,
            "retryable": isinstance(translated_error, PipelineTelegramProviderUnavailableError),
            "client_reset": client_reset,
        },
    )


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
    the runtime does not reconnect on every catch-up sweep. Operators can set
    ``persist_session_state=False`` for read-only previews that must not update
    session heartbeat, account, or authentication diagnostics.
    """

    settings: Settings
    session_name: str
    session_factory: AsyncSessionFactory | None = None
    persist_session_state: bool = True
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
            client = self._build_client(session_config.string_session)
            self._client = client
            timeout_seconds = self.settings.crawler_telegram_connect_timeout_seconds
            try:
                async with asyncio.timeout(timeout_seconds):
                    await client.connect()
                    is_authorized = await client.is_user_authorized()
                    account = await client.get_me() if is_authorized else None
            except Exception as exc:  # narrow: rewrap and re-raise as typed error
                try:
                    translated_error = _translate_telethon_error(exc)
                except BaseException:
                    self._client = None
                    await self._disconnect_client(client)
                    raise
                if self.persist_session_state and isinstance(
                    translated_error,
                    PipelineTelegramSessionAuthRequiredError,
                ):
                    with suppress(Exception):
                        await self._mark_auth_required(
                            error_class=type(translated_error).__name__,
                            error_text=str(translated_error),
                        )
                _log_operation_failure(
                    operation="connect_auth",
                    session_name=self.session_name,
                    exc=exc,
                    translated_error=translated_error,
                    timeout_seconds=timeout_seconds,
                    client_reset=True,
                )
                self._client = None
                await self._disconnect_client(client)
                raise translated_error from exc
            if not is_authorized:
                message = (
                    f"Telegram session {self.session_name!r} is not authorized; "
                    "import a valid Telethon StringSession with scripts/auth_telegram_session.py."
                )
                if self.persist_session_state:
                    with suppress(Exception):
                        await self._mark_auth_required(
                            error_class=PipelineTelegramSessionAuthRequiredError.__name__,
                            error_text=message,
                        )
                self._client = None
                await self._disconnect_client(client)
                raise PipelineTelegramSessionAuthRequiredError(message)
            if self.persist_session_state:
                await self._mark_authorized(account=account)
            return client

    def _build_client(self, string_session: SecretStr) -> TelegramClient:
        """Construct the ``TelegramClient`` bound to this DB-backed StringSession."""

        api_id = self.settings.telegram_api_id
        api_hash_secret = self.settings.telegram_api_hash
        if api_id is None or api_hash_secret is None:
            raise PipelineTelegramProviderUnavailableError(
                "Telegram API credentials are not configured; set TELEGRAM_API_ID and TELEGRAM_API_HASH.",
            )
        return TelegramClient(
            session=StringSession(string_session.get_secret_value()),
            api_id=api_id,
            api_hash=api_hash_secret.get_secret_value(),
            request_retries=self.settings.crawler_telegram_request_retries,
            connection_retries=self.settings.crawler_telegram_connection_retries,
            raise_last_call_error=True,
            # Process-level account supervision owns reconnects.  Disabling
            # Telethon's indefinite background reconnect loop ensures a dead
            # socket completes ``client.disconnected`` and gets fully rebuilt.
            auto_reconnect=False,
        )

    async def _load_session_config(self) -> _LoadedTelegramSessionConfig:
        """Load and validate the DB-backed StringSession for this factory."""

        session_factory = self._get_session_factory()
        async with session_factory() as db_session:
            row = await db_session.scalar(
                select(TelegramSession).where(TelegramSession.name == self.session_name).limit(1),
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
                select(TelegramSession).where(TelegramSession.name == self.session_name).limit(1),
            )
            if row is None:
                return
            row.status = TelegramSessionStatus.AUTH_REQUIRED
            row.last_error_class = error_class[:128]
            row.last_error_text = error_text[:4000]
            await db_session.commit()

    async def _mark_authorized(self, *, account: object | None) -> None:
        """Persist heartbeat and safe account projection fields after authorization."""

        session_factory = self._get_session_factory()
        async with session_factory() as db_session:
            row = await db_session.scalar(
                select(TelegramSession).where(TelegramSession.name == self.session_name).limit(1),
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

    async def invalidate_client(self, *, client: TelegramClient | None = None) -> None:
        """Disconnect and evict the cached client after a poisoned transport failure."""

        async with self._lock:
            current_client = self._client
            if current_client is None or (client is not None and current_client is not client):
                return
            self._client = None
        await self._disconnect_client(current_client)

    async def close(self) -> None:
        """Disconnect the client and dispose any engine created by this factory."""

        await self.invalidate_client()

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    async def _disconnect_client(self, client: TelegramClient) -> None:
        """Best-effort bounded disconnect used by failure and shutdown paths."""

        with suppress(Exception):
            disconnect_result: object = client.disconnect()
            if inspect.isawaitable(disconnect_result):
                async with asyncio.timeout(min(self.settings.crawler_telegram_connect_timeout_seconds, 5.0)):
                    await disconnect_result


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
    _live_queue: asyncio.Queue[TelegramLiveEvent] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _live_handlers: list[object] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(
        cls,
        *,
        settings: Settings,
        session_name: str,
        persist_session_state: bool = True,
    ) -> PipelineTelethonClient:
        """Return an adapter bound to ``session_name`` without connecting yet.

        ``persist_session_state=False`` keeps connection/auth observations out
        of the database for read-only operator previews.
        """

        return cls(
            factory=TelethonClientFactory(
                settings=settings,
                session_name=session_name,
                persist_session_state=persist_session_state,
            ),
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

    async def _await_operation(
        self,
        awaitable: Awaitable[_T],
        *,
        operation: str,
        timeout_seconds: float,
        client: TelegramClient,
        channel_id: str | None = None,
        post_id: str | None = None,
    ) -> _T:
        """Await one Telethon RPC under a named deadline and normalize failures."""

        try:
            async with asyncio.timeout(timeout_seconds):
                return await awaitable
        except Exception as exc:
            await self._raise_operation_error(
                operation=operation,
                timeout_seconds=timeout_seconds,
                client=client,
                exc=exc,
                channel_id=channel_id,
                post_id=post_id,
            )

    async def _next_history_message(
        self,
        iterator: AsyncIterator[Any],
        *,
        client: TelegramClient,
        channel_id: str,
    ) -> Any:
        """Bound the wait for each Telethon history page fetched by an iterator."""

        timeout_seconds = self.factory.settings.crawler_telegram_history_page_timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                return await anext(iterator)
        except StopAsyncIteration:
            raise
        except Exception as exc:
            await self._raise_operation_error(
                operation="history_page",
                timeout_seconds=timeout_seconds,
                client=client,
                exc=exc,
                channel_id=channel_id,
            )

    async def _raise_operation_error(
        self,
        *,
        operation: str,
        timeout_seconds: float,
        client: TelegramClient,
        exc: BaseException,
        channel_id: str | None = None,
        post_id: str | None = None,
    ) -> NoReturn:
        """Translate, log, and evict a client whose transport may be unhealthy."""

        translated_error = _translate_telethon_error(exc)
        client_reset = _requires_client_reset(exc)
        _log_operation_failure(
            operation=operation,
            session_name=self.factory.session_name,
            exc=exc,
            translated_error=translated_error,
            timeout_seconds=timeout_seconds,
            client_reset=client_reset,
            channel_id=channel_id,
            post_id=post_id,
        )
        if client_reset:
            self._entity_cache.clear()
            await self.factory.invalidate_client(client=client)
        raise translated_error from exc

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
        entity = await self._await_operation(
            client.get_entity(channel_id_or_username),
            operation="resolve_channel",
            timeout_seconds=self.factory.settings.crawler_telegram_resolve_timeout_seconds,
            client=client,
            channel_id=channel_id_or_username,
        )
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
        iterator = client.iter_messages(
            entity,
            limit=limit,
            min_id=min_id,
            reverse=True,
        ).__aiter__()
        while True:
            try:
                raw_message = await self._next_history_message(
                    cast("AsyncIterator[Any]", iterator),
                    client=client,
                    channel_id=channel_id,
                )
            except StopAsyncIteration:
                break
            else:
                yield TelethonMessageNormalizer.build(
                    message=raw_message,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    channel_username=channel_username,
                )

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
        iterator = client.iter_messages(
            entity,
            limit=limit,
            max_id=max_message_id or 0,
        ).__aiter__()
        while True:
            try:
                raw_message = await self._next_history_message(
                    cast("AsyncIterator[Any]", iterator),
                    client=client,
                    channel_id=channel_id,
                )
            except StopAsyncIteration:
                break
            else:
                messages.append(
                    TelethonMessageNormalizer.build(
                        message=raw_message,
                        channel_id=channel_id,
                        channel_title=channel_title,
                        channel_username=channel_username,
                    )
                )
        return messages

    async def listen_live(
        self,
        *,
        channel_ids: Sequence[str],
        ready_event: asyncio.Event | None = None,
    ) -> AsyncIterator[TelegramLiveEvent]:
        """Stream new, edited, and deleted messages via Telethon handlers.

        Registers new-message, edit, and deletion handlers covering every
        resolved channel and bridges events into an ``asyncio.Queue`` so the
        runtime's async for loop never blocks the Telethon dispatcher. Message
        handlers normalize into :class:`RawTelegramMessage` before queuing;
        deletion handlers queue the affected channel and post IDs.
        """

        client = await self._get_client()
        entities: list[Channel] = []
        channel_lookup: dict[int, tuple[str, str, str | None]] = {}
        for channel_id in channel_ids:
            entity = await self._resolve_entity(channel_id)
            entities.append(entity)
            lookup = (
                channel_id,
                getattr(entity, "title", None) or channel_id,
                getattr(entity, "username", None),
            )
            channel_lookup[int(entity.id)] = lookup
            channel_lookup[int(get_peer_id(entity))] = lookup

        queue: asyncio.Queue[TelegramLiveEvent] = asyncio.Queue()
        self._live_queue = queue

        def _normalize_event_message(raw_message: object) -> RawTelegramMessage | None:
            try:
                peer_id = getattr(raw_message, "peer_id", None)
                resolved_channel_id = int(getattr(peer_id, "channel_id", 0))
                lookup = channel_lookup.get(resolved_channel_id)
                if lookup is None:
                    return None
                channel_id_str, channel_title, channel_username = lookup
                return TelethonMessageNormalizer.build(
                    message=cast("Any", raw_message),
                    channel_id=channel_id_str,
                    channel_title=channel_title,
                    channel_username=channel_username,
                )
            except PipelineTelegramMalformedMessageError:
                return None

        async def _on_new_message(event: Any) -> None:
            normalized = _normalize_event_message(event.message)
            if normalized is not None:
                await queue.put(TelegramNewMessageEvent(message=normalized))

        async def _on_message_edited(event: Any) -> None:
            normalized = _normalize_event_message(event.message)
            if normalized is not None:
                await queue.put(TelegramMessageEditedEvent(message=normalized))

        async def _on_messages_deleted(event: Any) -> None:
            raw_chat_id = getattr(event, "chat_id", None)
            if not isinstance(raw_chat_id, int):
                return
            lookup = channel_lookup.get(raw_chat_id)
            if lookup is None:
                return
            deleted_ids = getattr(event, "deleted_ids", None)
            if not isinstance(deleted_ids, (list, tuple)):
                return
            post_ids = tuple(str(post_id) for post_id in deleted_ids if isinstance(post_id, int))
            if post_ids:
                await queue.put(TelegramMessagesDeletedEvent(channel_id=lookup[0], post_ids=post_ids))

        live_handlers = (
            (_on_new_message, events.NewMessage(chats=cast("list[object]", entities))),
            (_on_message_edited, events.MessageEdited(chats=cast("list[object]", entities))),
            (_on_messages_deleted, events.MessageDeleted(chats=cast("list[object]", entities))),
        )
        for handler, event_builder in live_handlers:
            client.add_event_handler(handler, event_builder)
            self._live_handlers.append(handler)
        if ready_event is not None:
            ready_event.set()

        disconnected_awaitable = getattr(client, "disconnected", None)
        disconnected_guard = (
            asyncio.ensure_future(asyncio.shield(disconnected_awaitable))
            if inspect.isawaitable(disconnected_awaitable)
            else None
        )
        try:
            while not self._closed:
                next_message_task = asyncio.create_task(queue.get())
                try:
                    if disconnected_guard is None:
                        next_message = await next_message_task
                    else:
                        done, _ = await asyncio.wait(
                            (next_message_task, disconnected_guard),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if disconnected_guard in done:
                            if self._closed:
                                return
                            disconnect_error: BaseException | None = None
                            if not disconnected_guard.cancelled():
                                disconnect_error = disconnected_guard.exception()
                            await self._raise_operation_error(
                                operation="listen_live",
                                timeout_seconds=self.factory.settings.crawler_telegram_connect_timeout_seconds,
                                client=client,
                                exc=disconnect_error
                                or ConnectionError("Telethon disconnected while the live listener was active."),
                            )
                        next_message = next_message_task.result()
                finally:
                    if not next_message_task.done():
                        next_message_task.cancel()
                        _ = await asyncio.gather(next_message_task, return_exceptions=True)
                yield next_message
        finally:
            if disconnected_guard is not None and not disconnected_guard.done():
                disconnected_guard.cancel()
            for handler in self._live_handlers:
                with suppress(Exception):
                    client.remove_event_handler(cast("Any", handler))
            self._live_handlers.clear()
            self._live_queue = None

    async def fetch_messages(
        self,
        *,
        channel_id: str,
        post_ids: Sequence[str],
    ) -> dict[str, RawTelegramMessage | None]:
        """Fetch one RPC chunk, preserving only explicit missing results."""

        if len(post_ids) > 100:
            raise ValueError("Telegram metadata batches must contain at most 100 post ids.")
        if not post_ids:
            return {}
        numeric_post_ids: list[int] = []
        for post_id in post_ids:
            try:
                numeric_post_ids.append(int(post_id))
            except ValueError as exc:
                raise PipelineTelegramMalformedMessageError(
                    f"Telegram post ids must be numeric; got {post_id!r}.",
                ) from exc

        client = await self._get_client()
        entity = await self._resolve_entity(channel_id)
        channel_title = getattr(entity, "title", None) or channel_id
        channel_username = getattr(entity, "username", None)
        await self.rate_limiter.acquire()
        fetched = await self._await_operation(
            client.get_messages(entity, ids=numeric_post_ids),
            operation="fetch_messages",
            timeout_seconds=self.factory.settings.crawler_telegram_single_message_timeout_seconds,
            client=client,
            channel_id=channel_id,
        )
        fetched_messages = tuple(fetched) if isinstance(fetched, (list, tuple)) else (fetched,)
        for raw_message in fetched_messages:
            if raw_message is None:
                continue
            if not isinstance(raw_message, (TelethonMessage, TelethonMessageService)):
                raise PipelineTelegramMalformedMessageError(
                    "Telegram returned an unexpected item in a metadata batch: "
                    f"{type(raw_message).__name__}.",
                )
        if len(fetched_messages) != len(post_ids):
            # Telethon documents positional results for integer message IDs.
            # If Telegram omits an entry entirely, its position is ambiguous;
            # omit every key so the backfill treats the batch as transient
            # instead of guessing that any post was deleted.
            return {}

        results: dict[str, RawTelegramMessage | None] = {}
        for post_id, raw_message in zip(post_ids, fetched_messages, strict=True):
            if raw_message is None:
                results[post_id] = None
                continue
            normalized = TelethonMessageNormalizer.build(
                message=cast("Any", raw_message),
                channel_id=channel_id,
                channel_title=channel_title,
                channel_username=channel_username,
            )
            if normalized.message_id != post_id:
                raise PipelineTelegramMalformedMessageError(
                    "Telegram returned an unrequested message id in a metadata batch.",
                )
            results[post_id] = normalized
        return results

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
        _ = await self._await_operation(
            client.download_media(raw_payload, file=media_buffer),
            operation="download_media",
            timeout_seconds=self.factory.settings.crawler_telegram_media_download_timeout_seconds,
            client=client,
            channel_id=message.channel_id,
            post_id=message.message_id,
        )
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

    async def fetch_channel_audience(self, channel_id: str) -> RawTelegramChannelAudience:
        """Fetch the current participant count through ``channels.getFullChannel``."""

        client = await self._get_client()
        entity = await self._resolve_entity(channel_id)
        await self.rate_limiter.acquire()
        full_channel = await self._await_operation(
            client(GetFullChannelRequest(channel=get_input_channel(entity))),
            operation="fetch_channel_audience",
            timeout_seconds=self.factory.settings.crawler_telegram_resolve_timeout_seconds,
            client=client,
            channel_id=channel_id,
        )
        raw_count = getattr(getattr(full_channel, "full_chat", None), "participants_count", None)
        subscriber_count = raw_count if isinstance(raw_count, int) and raw_count >= 0 else None
        return RawTelegramChannelAudience(
            channel_id=str(entity.id),
            subscriber_count=subscriber_count,
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
        fetched = await self._await_operation(
            client.get_messages(entity, ids=numeric_post_id),
            operation="fetch_single_message",
            timeout_seconds=self.factory.settings.crawler_telegram_single_message_timeout_seconds,
            client=client,
            channel_id=channel_id,
            post_id=post_id,
        )
        if fetched is None:
            raise PipelineTelegramMalformedMessageError(
                f"Telegram returned no message for ({channel_id!r}, {post_id!r}).",
            )
        if not isinstance(fetched, (TelethonMessage, TelethonMessageService)):
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
