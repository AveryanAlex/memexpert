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

from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    RPCError,
    SessionRevokedError,
    UserDeactivatedError,
)
from telethon.tl.types import Channel, InputPeerChannel
from telethon.tl.types import Message as TelethonMessage

from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionBannedError,
    RawTelegramChannel,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.telethon_mapper import TelethonMessageNormalizer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from telethon.events import NewMessage

    from memexpert.core.config import Settings


# Telethon's AUTH-level failures cover three distinct "this session is
# dead" variants. Group them into one tuple so the translation layer
# can hand them to a single except branch and map the lot onto
# :class:`PipelineTelegramSessionBannedError`.
_SESSION_BANNED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AuthKeyUnregisteredError,
    UserDeactivatedError,
    SessionRevokedError,
)


def _translate_telethon_error(exc: BaseException) -> PipelineTelegramError:
    """Map a raw Telethon exception onto the typed crawler error taxonomy.

    Narrow, fall-through order:

    * ``FloodWaitError`` carries ``exc.seconds`` — preserve it so the
      runtime can park the session for the correct cooldown.
    * ``AuthKey`` / ``UserDeactivated`` / ``SessionRevoked`` are all
      permanent session failures; quarantine without retry.
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
    if isinstance(exc, _SESSION_BANNED_EXCEPTIONS):
        return PipelineTelegramSessionBannedError(
            f"Telegram marked this session as unusable: {exc.__class__.__name__}.",
        )
    if isinstance(exc, RPCError | ConnectionError | TimeoutError | OSError):
        return PipelineTelegramProviderUnavailableError(
            f"Telegram RPC or transport failure: {exc.__class__.__name__}: {exc}.",
        )
    raise exc  # pragma: no cover - propagate unknown types for visibility


@dataclass(slots=True)
class TelethonClientFactory:
    """Lazy factory that returns a connected :class:`TelegramClient`.

    Reads ``telegram_api_id`` / ``telegram_api_hash`` / ``telegram_session_dir``
    from :class:`memexpert.core.config.Settings` and opens the session
    only on the first :meth:`get_client` call. Subsequent calls return
    the cached client so the runtime does not reconnect on every
    catch-up sweep.
    """

    settings: Settings
    session_name: str
    _client: TelegramClient | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def get_client(self) -> TelegramClient:
        """Return the connected Telethon client, building it on first use.

        Connection, start() (which would prompt for auth) are intentionally
        NOT called here — the caller is expected to have already
        authenticated the session via ``scripts/auth_telegram_session.py``.
        We call ``connect()`` and refuse to proceed if the session is not
        authorized so the runtime surfaces a clear error instead of
        blocking on an interactive prompt.
        """

        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            self._client = self._build_client()
            try:
                await self._client.connect()
            except Exception as exc:  # narrow: rewrap and re-raise as typed error
                raise _translate_telethon_error(exc) from exc
            if not await self._client.is_user_authorized():
                raise PipelineTelegramSessionBannedError(
                    f"Telegram session {self.session_name!r} is not authorized; "
                    "run scripts/auth_telegram_session.py first.",
                )
            return self._client

    def _build_client(self) -> TelegramClient:
        """Construct the ``TelegramClient`` bound to this session's ``.session`` file."""

        api_id = self.settings.telegram_api_id
        api_hash_secret = self.settings.telegram_api_hash
        if api_id is None or api_hash_secret is None:
            raise PipelineTelegramProviderUnavailableError(
                "Telegram API credentials are not configured; "
                "set TELEGRAM_API_ID and TELEGRAM_API_HASH.",
            )
        session_path = self._resolve_session_path()
        return TelegramClient(
            session=str(session_path),
            api_id=api_id,
            api_hash=api_hash_secret.get_secret_value(),
        )

    def _resolve_session_path(self) -> Path:
        """Return the ``<telegram_session_dir>/<session_name>.session`` path.

        The directory is created on demand so the first connection does
        not fail on a fresh checkout. Telethon appends its own
        ``.session`` suffix when it sees a path without one, but passing
        the explicit file keeps the runtime's log lines deterministic.
        """

        directory = self.settings.telegram_session_dir
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{self.session_name}.session"


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
        client = await self.factory.get_client()
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

        client = await self.factory.get_client()
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

    async def listen_live(
        self,
        *,
        channel_ids: Sequence[str],
    ) -> AsyncIterator[RawTelegramMessage]:
        """Stream live messages for the requested channels via a Telethon handler.

        Registers one ``events.NewMessage`` handler covering every
        resolved channel and bridges events into an ``asyncio.Queue``
        so the runtime's async for loop never blocks the Telethon
        dispatcher. The handler normalizes the message into a
        :class:`RawTelegramMessage` inside the handler and puts the
        projection on the queue; the async iterator just drains it.
        """

        client = await self.factory.get_client()
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

        client = await self.factory.get_client()
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

        client = await self.factory.get_client()
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


__all__ = [
    "PipelineTelethonClient",
    "TelethonClientFactory",
]


# Silence the unused InputPeerChannel import: the symbol is re-exported by
# Telethon's own runtime path but importing it eagerly in this module is
# deliberate so type-checkers that resolve ``events.NewMessage(chats=...)``
# through the Telethon stubs do not complain about the peer discriminant.
_ = InputPeerChannel
