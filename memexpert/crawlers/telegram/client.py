# ruff: noqa: TC003
"""Typed Telegram crawler adapter contract, pure mapper, and in-memory fake client.

T01 locks the boundary so T02 can drop in the real Telethon implementation
without touching any service code. Nothing in this module performs I/O at
import time: the fake client is pure-Python in-memory state, and the real
adapter will follow the same protocol in T02. The
:class:`PipelineTelegramMessageMapper` is a pure function that converts a
typed raw message + its downloaded bytes into a
:class:`RawCrawlerPost` consumed by the crawler ingest service.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from memexpert.models.enums import SourceEngagementCommentsState, SourcePlatform
from memexpert.schemas.content_pipeline import (
    CrawlerForwardAttribution,
    RawCrawlerPost,
    TelegramPostMetadata,
)


class PipelineTelegramError(RuntimeError):
    """Base error raised by the Telegram crawler boundary.

    Kept structurally parallel to the Qdrant, Meilisearch, and Voyage error
    taxonomies so the runtime dispatcher can treat crawler failures with
    the same normalize-and-classify logic. Subclasses carry the specific
    failure mode so operators can distinguish a flood-wait cooldown from
    a malformed message from a provider outage.
    """


class PipelineTelegramFloodWaitError(PipelineTelegramError):
    """Raised when Telegram rate-limits a session and returns a cooldown window.

    ``wait_seconds`` is the server-side cooldown Telegram tells us to
    respect before retrying. T02 persists it on the
    :class:`memexpert.models.content.TelegramSession` row so the
    session distributor can quarantine sessions that are flooding.
    """

    def __init__(self, message: str, *, wait_seconds: int) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


class PipelineTelegramProviderUnavailableError(PipelineTelegramError):
    """Raised when Telegram is unreachable or refuses the request."""


class PipelineTelegramSessionBannedError(PipelineTelegramError):
    """Raised when Telegram responds that the session has been banned.

    Distinct from ``PipelineTelegramProviderUnavailableError`` because a
    banned session is permanent until an operator rotates it — the
    runtime must not retry automatically.
    """


class PipelineTelegramSessionNotRunnableError(PipelineTelegramError):
    """Raised when durable session state cannot run a Telethon client."""


class PipelineTelegramSessionAuthRequiredError(PipelineTelegramSessionNotRunnableError):
    """Raised when a stored StringSession exists but is not authorized."""


class PipelineTelegramMalformedMessageError(PipelineTelegramError):
    """Raised when Telethon returns a message the adapter cannot type-check.

    Mirrors ``QdrantSyncMalformedResponseError`` / ``VoyageMalformedResponseError``:
    malformed adapter responses are never retryable because the underlying
    message will keep failing the same way.
    """


@dataclass(frozen=True, slots=True)
class RawTelegramChannel:
    """Minimal typed projection of a Telegram channel resolved by the adapter.

    The adapter only needs the four fields the pipeline service consumes when
    upserting a ``SourceChannel`` row. ``subscriber_count`` is optional
    because the Telethon participant count is not always available.
    """

    channel_id: str
    username: str | None
    title: str
    subscriber_count: int | None


@dataclass(frozen=True, slots=True)
class RawTelegramChannelAudience:
    """Typed projection returned by Telegram ``channels.getFullChannel``.

    ``subscriber_count=None`` means Telegram did not expose the participant
    count. A known zero is preserved as a valid successful observation.
    """

    channel_id: str
    subscriber_count: int | None


@dataclass(frozen=True, slots=True, init=False)
class RawTelegramMessage:
    """Typed adapter-level projection of one Telegram channel message.

    The adapter materializes this struct from a raw Telethon message before
    crossing module boundaries. ``raw_payload`` is the opaque SDK-level
    object that should ONLY be handed back to the adapter's
    :meth:`PipelineTelegramClientProtocol.download_media` call — it must
    never leak into service code because its shape belongs to Telethon.
    """

    message_id: str
    channel_id: str
    channel_username: str | None
    channel_title: str
    published_at: datetime
    media_type: Literal["photo", "gif", "video", "unsupported"] | None
    view_count: int | None
    reactions: dict[str, int] | None
    forward: CrawlerForwardAttribution | None
    forward_count: int | None
    comment_count: int | None
    comments_state: SourceEngagementCommentsState
    telegram_post: TelegramPostMetadata
    raw_payload: object

    def __init__(
        self,
        *,
        message_id: str,
        channel_id: str,
        channel_username: str | None,
        channel_title: str,
        published_at: datetime,
        media_type: Literal["photo", "gif", "video", "unsupported"] | None,
        view_count: int | None = None,
        views: int | None = None,
        reactions: dict[str, int] | None = None,
        forward: CrawlerForwardAttribution | None = None,
        forward_count: int | None = None,
        comment_count: int | None = None,
        comments_state: SourceEngagementCommentsState = SourceEngagementCommentsState.UNKNOWN,
        telegram_post: TelegramPostMetadata | None = None,
        raw_payload: object = None,
    ) -> None:
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(self, "channel_username", channel_username)
        object.__setattr__(self, "channel_title", channel_title)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "view_count", view_count if view_count is not None else views)
        object.__setattr__(self, "reactions", None if reactions is None else dict(reactions))
        object.__setattr__(self, "forward", forward)
        object.__setattr__(self, "forward_count", forward_count)
        object.__setattr__(self, "comment_count", comment_count)
        object.__setattr__(self, "comments_state", comments_state)
        object.__setattr__(self, "telegram_post", telegram_post or TelegramPostMetadata())
        object.__setattr__(self, "raw_payload", raw_payload)

    @property
    def views(self) -> int | None:
        """Legacy metric accessor; new code should use ``view_count``."""

        return self.view_count


@dataclass(frozen=True, slots=True)
class TelegramNewMessageEvent:
    """Live Telegram event that should run the normal ingest path."""

    message: RawTelegramMessage


@dataclass(frozen=True, slots=True)
class TelegramMessageEditedEvent:
    """Live Telegram edit that refreshes post context without re-ingest."""

    message: RawTelegramMessage


@dataclass(frozen=True, slots=True)
class TelegramMessagesDeletedEvent:
    """Live deletion notice for existing posts in one tracked channel."""

    channel_id: str
    post_ids: tuple[str, ...]


type TelegramLiveEvent = TelegramNewMessageEvent | TelegramMessageEditedEvent | TelegramMessagesDeletedEvent


@runtime_checkable
class PipelineTelegramClientProtocol(Protocol):
    """Typed adapter boundary both the real Telethon client and the fake honor.

    Every method is ``async`` because T02's real adapter is inherently I/O
    bound. Both concrete implementations MUST raise one of the
    :class:`PipelineTelegramError` subclasses on failure so the runtime
    dispatcher can classify the error.
    """

    def iter_channel_messages(
        self,
        *,
        channel_id: str,
        min_message_id: int | None,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Yield messages newer than ``min_message_id`` for catch-up sweeps."""

    def iter_latest_channel_messages(
        self,
        *,
        channel_id: str,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Yield the latest bounded channel window in oldest-to-newest order."""

    def iter_older_channel_messages(
        self,
        *,
        channel_id: str,
        before_message_id: int,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Yield the newest bounded window below ``before_message_id`` newest-first."""

    def listen_live(
        self,
        *,
        channel_ids: Sequence[str],
        ready_event: asyncio.Event | None = None,
    ) -> AsyncIterator[TelegramLiveEvent]:
        """Stream live messages for the requested channels after catch-up completes."""

    async def fetch_messages(
        self,
        *,
        channel_id: str,
        post_ids: Sequence[str],
    ) -> dict[str, RawTelegramMessage | None]:
        """Fetch a bounded set of messages with explicit missing semantics.

        A requested key mapped to ``None`` means Telegram explicitly reported
        the post missing. An omitted key means the adapter could not determine
        that post's state and callers must treat it as transient.
        """

    async def download_media(self, message: RawTelegramMessage) -> bytes:
        """Materialize the media bytes attached to one raw Telegram message."""

    async def resolve_channel(self, username_or_id: str) -> RawTelegramChannel:
        """Resolve a human-friendly channel identifier into its typed projection."""

    async def fetch_channel_audience(self, channel_id: str) -> RawTelegramChannelAudience:
        """Fetch the channel's current audience through ``channels.getFullChannel``."""

    async def fetch_single_message(
        self,
        *,
        channel_id: str,
        post_id: str,
    ) -> RawTelegramMessage:
        """Return one specific message by ``(channel_id, post_id)`` for replay.

        The crawler runtime replays failed ingests by id without advancing
        the channel checkpoint. Implementations raise
        :class:`PipelineTelegramMalformedMessageError` when Telegram has
        no such message (or cannot parse it), and translate any transport
        failure into the typed error taxonomy like the other methods.
        """

    async def close(self) -> None:
        """Shut the adapter down and release any underlying client resources."""


class PipelineTelegramMessageMapper:
    """Pure helper that builds a :class:`RawCrawlerPost` from adapter output.

    Kept deliberately stateless so both the real adapter (T02) and the
    :class:`FakeTelegramClient` tests can use the same mapping path.
    Callers hand the mapper the already-downloaded ``media_bytes`` so the
    mapper never performs I/O itself. The mapper rejects messages with
    ``unsupported`` media here so the service contract only ever sees the
    three crawler-supported kinds; the caller's crawler runtime decides
    whether to short-circuit the post or record it as a skip before
    calling the service.
    """

    @staticmethod
    def build_raw_crawler_post(
        message: RawTelegramMessage,
        media_bytes: bytes,
    ) -> RawCrawlerPost:
        """Return a typed :class:`RawCrawlerPost` for one Telegram message.

        Raises :class:`PipelineTelegramMalformedMessageError` when the
        caller passes a message the mapper cannot safely convert.
        """

        raw_media_type = message.media_type
        if raw_media_type == "photo":
            narrowed_media_type: Literal["photo", "gif", "video"] = "photo"
        elif raw_media_type == "gif":
            narrowed_media_type = "gif"
        elif raw_media_type == "video":
            narrowed_media_type = "video"
        else:
            raise PipelineTelegramMalformedMessageError(
                "PipelineTelegramMessageMapper cannot build a RawCrawlerPost "
                f"for media_type={raw_media_type!r}.",
            )
        if not media_bytes:
            raise PipelineTelegramMalformedMessageError(
                "PipelineTelegramMessageMapper requires non-empty media_bytes.",
            )
        return RawCrawlerPost(
            platform=SourcePlatform.TELEGRAM,
            source_id=message.channel_id,
            post_id=message.message_id,
            published_at=message.published_at,
            channel_username=message.channel_username,
            channel_title=message.channel_title,
            media_type=narrowed_media_type,
            media_bytes=media_bytes,
            filename=None,
            content_type=None,
            view_count=message.view_count,
            reactions=None if message.reactions is None else dict(message.reactions),
            forward=message.forward,
            forward_count=message.forward_count,
            comment_count=message.comment_count,
            comments_state=message.comments_state,
            telegram_post=message.telegram_post,
        )


@dataclass
class FakeTelegramClient:
    """Deterministic in-memory implementation of the Telegram adapter protocol.

    Tests seed ``canned_messages`` + ``canned_channels`` + ``media_by_message``
    up front and optionally pin one of the typed errors to ``next_error`` so
    the next adapter call raises it. The fake makes zero network calls and
    does not import Telethon — it is safe to construct at import time in
    tests. ``downloaded_message_ids`` and ``closed`` let tests assert call
    ordering without hand-rolling their own spies.
    """

    canned_messages: dict[str, list[RawTelegramMessage]] = field(default_factory=dict)
    canned_channels: dict[str, RawTelegramChannel] = field(default_factory=dict)
    canned_channel_audiences: dict[str, RawTelegramChannelAudience] = field(default_factory=dict)
    media_by_message: dict[str, bytes] = field(default_factory=dict)
    live_messages: dict[str, list[RawTelegramMessage]] = field(default_factory=dict)
    live_events: list[TelegramLiveEvent] = field(default_factory=list)
    single_messages: dict[tuple[str, str], RawTelegramMessage] = field(default_factory=dict)
    next_error: PipelineTelegramError | None = None
    # Typed errors injected on download_media calls, keyed by message id.
    # Tests use this to simulate per-message provider failures without
    # tripping ``next_error`` pinning on other methods.
    download_errors: dict[str, PipelineTelegramError] = field(default_factory=dict)
    downloaded_message_ids: list[str] = field(default_factory=list)
    audience_fetch_channel_ids: list[str] = field(default_factory=list)
    closed: bool = False

    def pin_single_message(
        self,
        *,
        channel_id: str,
        post_id: str,
        message: RawTelegramMessage,
        media: bytes | None = None,
    ) -> None:
        """Pre-seed a single-message lookup the runtime's replay path uses.

        When ``media`` is provided, the entry is also registered with
        ``media_by_message`` so a subsequent ``download_media`` call on
        the returned message succeeds without a second helper call.
        """

        self.single_messages[(channel_id, post_id)] = message
        if media is not None:
            self.media_by_message[message.message_id] = media

    def _consume_pinned_error(self) -> None:
        if self.next_error is not None:
            pinned = self.next_error
            self.next_error = None
            raise pinned

    async def iter_channel_messages(
        self,
        *,
        channel_id: str,
        min_message_id: int | None,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        self._consume_pinned_error()
        messages = self.canned_messages.get(channel_id, [])
        yielded = 0
        for message in messages:
            if yielded >= limit:
                return
            if min_message_id is not None:
                try:
                    if int(message.message_id) <= min_message_id:
                        continue
                except ValueError:
                    # Fake clients are allowed to use opaque post ids — when
                    # both sides are strings, the caller controls ordering.
                    pass
            yielded += 1
            yield message

    async def iter_latest_channel_messages(
        self,
        *,
        channel_id: str,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        self._consume_pinned_error()
        messages = self.canned_messages.get(channel_id, [])
        for message in messages[-limit:]:
            yield message

    async def iter_older_channel_messages(
        self,
        *,
        channel_id: str,
        before_message_id: int,
        limit: int,
    ) -> AsyncIterator[RawTelegramMessage]:
        self._consume_pinned_error()
        eligible: list[RawTelegramMessage] = []
        for message in self.canned_messages.get(channel_id, []):
            try:
                if int(message.message_id) >= before_message_id:
                    continue
            except ValueError:
                continue
            eligible.append(message)
        for message in reversed(eligible[-limit:]):
            yield message

    async def listen_live(
        self,
        *,
        channel_ids: Sequence[str],
        ready_event: asyncio.Event | None = None,
    ) -> AsyncIterator[TelegramLiveEvent]:
        self._consume_pinned_error()
        if ready_event is not None:
            ready_event.set()
        if self.live_events:
            for event in self.live_events:
                yield event
            return
        for channel_id in channel_ids:
            for message in self.live_messages.get(channel_id, []):
                yield TelegramNewMessageEvent(message=message)

    async def fetch_messages(
        self,
        *,
        channel_id: str,
        post_ids: Sequence[str],
    ) -> dict[str, RawTelegramMessage | None]:
        self._consume_pinned_error()
        return {
            post_id: self.single_messages.get((channel_id, post_id))
            for post_id in post_ids
        }

    async def download_media(self, message: RawTelegramMessage) -> bytes:
        pinned_download_error = self.download_errors.pop(message.message_id, None)
        if pinned_download_error is not None:
            raise pinned_download_error
        self._consume_pinned_error()
        try:
            media_bytes = self.media_by_message[message.message_id]
        except KeyError as exc:
            raise PipelineTelegramMalformedMessageError(
                f"FakeTelegramClient has no media for message_id={message.message_id!r}.",
            ) from exc
        self.downloaded_message_ids.append(message.message_id)
        return media_bytes

    async def resolve_channel(self, username_or_id: str) -> RawTelegramChannel:
        self._consume_pinned_error()
        try:
            return self.canned_channels[username_or_id]
        except KeyError as exc:
            raise PipelineTelegramMalformedMessageError(
                f"FakeTelegramClient has no canned channel for {username_or_id!r}.",
            ) from exc

    async def fetch_channel_audience(self, channel_id: str) -> RawTelegramChannelAudience:
        self._consume_pinned_error()
        self.audience_fetch_channel_ids.append(channel_id)
        audience = self.canned_channel_audiences.get(channel_id)
        if audience is not None:
            return audience
        channel = self.canned_channels.get(channel_id)
        if channel is not None:
            return RawTelegramChannelAudience(
                channel_id=channel.channel_id,
                subscriber_count=channel.subscriber_count,
            )
        raise PipelineTelegramMalformedMessageError(
            f"FakeTelegramClient has no canned channel audience for {channel_id!r}.",
        )

    async def fetch_single_message(
        self,
        *,
        channel_id: str,
        post_id: str,
    ) -> RawTelegramMessage:
        self._consume_pinned_error()
        try:
            return self.single_messages[(channel_id, post_id)]
        except KeyError as exc:
            raise PipelineTelegramMalformedMessageError(
                f"FakeTelegramClient has no pinned single message for "
                f"({channel_id!r}, {post_id!r}).",
            ) from exc

    async def close(self) -> None:
        self.closed = True


__all__ = [
    "FakeTelegramClient",
    "PipelineTelegramClientProtocol",
    "PipelineTelegramError",
    "PipelineTelegramFloodWaitError",
    "PipelineTelegramMalformedMessageError",
    "PipelineTelegramMessageMapper",
    "PipelineTelegramProviderUnavailableError",
    "PipelineTelegramSessionAuthRequiredError",
    "PipelineTelegramSessionBannedError",
    "PipelineTelegramSessionNotRunnableError",
    "RawTelegramChannel",
    "RawTelegramChannelAudience",
    "RawTelegramMessage",
    "TelegramLiveEvent",
    "TelegramMessageEditedEvent",
    "TelegramMessagesDeletedEvent",
    "TelegramNewMessageEvent",
]
