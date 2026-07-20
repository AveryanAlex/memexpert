"""Worker-side execution for Telegram channel audience capture requests."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import select

from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramFloodWaitError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
)
from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, SourceChannelAudienceSnapshot, TelegramSession
from memexpert.models.enums import (
    SourceChannelAudienceFetchStatus,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.pipeline.events import SourceChannelAudienceCaptureRequestedEvent
from memexpert.services.source_channel_audience import (
    SourceChannelAudienceObservation,
    record_source_channel_audience_observation,
    source_channel_audience_observation_from_count,
)

if TYPE_CHECKING:
    import uuid

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


type SourceChannelAudienceTelegramClientFactory = Callable[
    [SourceChannelAudienceCaptureRequestedEvent],
    PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
]


class SourceChannelAudienceTelegramSessionInvalidator(Protocol):
    """Close and forget one cached Telegram session client."""

    def __call__(self, *, session_id: uuid.UUID) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class SourceChannelAudienceCaptureResult:
    """Outcome for one channel-audience capture request."""

    source_channel_id: uuid.UUID
    fetch_status: SourceChannelAudienceFetchStatus | None
    snapshot_id: uuid.UUID | None
    duplicate: bool
    error_code: str | None = None


async def capture_source_channel_audience_request(
    session_factory: AsyncSessionFactory,
    event: SourceChannelAudienceCaptureRequestedEvent,
    *,
    telegram_client_factory: SourceChannelAudienceTelegramClientFactory,
    telegram_session_invalidator: SourceChannelAudienceTelegramSessionInvalidator | None = None,
    close_telegram_client_after_capture: bool = True,
) -> SourceChannelAudienceCaptureResult:
    """Fetch one full-channel audience observation and persist it."""

    service = SourceChannelAudienceCaptureService(
        session_factory,
        telegram_client_factory=telegram_client_factory,
        telegram_session_invalidator=telegram_session_invalidator,
        close_telegram_client_after_capture=close_telegram_client_after_capture,
    )
    return await service.capture(event)


def build_pipeline_source_channel_audience_telegram_client_factory(
    settings: Settings,
    *,
    session_manager: object | None = None,
) -> SourceChannelAudienceTelegramClientFactory:
    """Build the cached Telegram-session client factory used by workers."""

    if session_manager is None:
        from memexpert.core.database import get_async_session_factory
        from memexpert.crawlers.telegram.manager import TelegramSessionManager

        session_manager = TelegramSessionManager(settings=settings, session_factory=get_async_session_factory())
    typed_session_manager = cast("object", session_manager)

    async def _factory(event: SourceChannelAudienceCaptureRequestedEvent) -> PipelineTelegramClientProtocol:
        client_method = getattr(typed_session_manager, "source_channel_audience_client_for_event", None)
        if client_method is None:
            raise PipelineTelegramProviderUnavailableError(
                "Channel audience capture requires a TelegramSessionManager-backed client factory.",
            )
        client = client_method(event)
        if inspect.isawaitable(client):
            return cast("PipelineTelegramClientProtocol", await client)
        return cast("PipelineTelegramClientProtocol", client)

    return _factory


class SourceChannelAudienceCaptureService:
    """Capture one scheduled audience request without clearing valid cache on failure."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        telegram_client_factory: SourceChannelAudienceTelegramClientFactory,
        telegram_session_invalidator: SourceChannelAudienceTelegramSessionInvalidator | None = None,
        close_telegram_client_after_capture: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._telegram_client_factory = telegram_client_factory
        self._telegram_session_invalidator = telegram_session_invalidator
        self._close_telegram_client_after_capture = close_telegram_client_after_capture

    async def capture(
        self,
        event: SourceChannelAudienceCaptureRequestedEvent,
    ) -> SourceChannelAudienceCaptureResult:
        existing_result = await self._short_circuit_existing_terminal_snapshot(event)
        if existing_result is not None:
            return existing_result

        try:
            observation = await self._fetch_observation(event)
        except PipelineTelegramFloodWaitError as exc:
            return await self._handle_flood_wait(event, exc)
        except (PipelineTelegramSessionAuthRequiredError, PipelineTelegramSessionBannedError) as exc:
            return await self._handle_terminal_session_failure(event, exc)
        captured_at = utcnow()

        async with self._session_factory() as session, session.begin():
            channel = await session.scalar(
                select(SourceChannel)
                .where(SourceChannel.id == event.source_channel_id)
                .with_for_update(of=SourceChannel)
            )
            if channel is None:
                return SourceChannelAudienceCaptureResult(
                    source_channel_id=event.source_channel_id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="source_channel_not_found",
                )
            if not _event_is_current_for_channel(event, channel):
                return SourceChannelAudienceCaptureResult(
                    source_channel_id=channel.id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="stale_audience_capture_request",
                )

            existing_snapshot = await session.scalar(
                select(SourceChannelAudienceSnapshot)
                .where(
                    SourceChannelAudienceSnapshot.source_channel_id == channel.id,
                    SourceChannelAudienceSnapshot.capture_slot == event.capture_slot,
                    SourceChannelAudienceSnapshot.capture_reason == event.capture_reason,
                )
                .with_for_update(of=SourceChannelAudienceSnapshot)
            )
            if (
                existing_snapshot is not None
                and existing_snapshot.fetch_status is not SourceChannelAudienceFetchStatus.FAILED
            ):
                _clear_matching_lease(channel, event)
                return SourceChannelAudienceCaptureResult(
                    source_channel_id=channel.id,
                    fetch_status=existing_snapshot.fetch_status,
                    snapshot_id=existing_snapshot.id,
                    duplicate=True,
                    error_code=existing_snapshot.error_code,
                )

            snapshot = await record_source_channel_audience_observation(
                session,
                channel,
                observation,
                capture_reason=event.capture_reason,
                telegram_session_id=event.telegram_session_id,
                captured_at=captured_at,
                capture_slot=event.capture_slot,
                advance_daily_schedule=(
                    observation.fetch_status is not SourceChannelAudienceFetchStatus.FAILED
                ),
            )
            if observation.fetch_status is SourceChannelAudienceFetchStatus.FAILED:
                # Keep the original due slot so the hourly scheduler retries
                # this failed observation; successful/not-exposed captures
                # advance to the next deterministic daily slot.
                channel.next_audience_capture_at = event.scheduled_for
                channel.audience_capture_locked_at = None
                channel.audience_capture_lock_owner = None

        return SourceChannelAudienceCaptureResult(
            source_channel_id=event.source_channel_id,
            fetch_status=observation.fetch_status,
            snapshot_id=snapshot.id,
            duplicate=existing_snapshot is not None,
            error_code=observation.error_code,
        )

    async def _short_circuit_existing_terminal_snapshot(
        self,
        event: SourceChannelAudienceCaptureRequestedEvent,
    ) -> SourceChannelAudienceCaptureResult | None:
        async with self._session_factory() as session, session.begin():
            channel = await session.scalar(
                select(SourceChannel)
                .where(SourceChannel.id == event.source_channel_id)
                .with_for_update(of=SourceChannel)
            )
            if channel is None:
                return SourceChannelAudienceCaptureResult(
                    source_channel_id=event.source_channel_id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="source_channel_not_found",
                )
            if not _event_is_current_for_channel(event, channel):
                return SourceChannelAudienceCaptureResult(
                    source_channel_id=channel.id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="stale_audience_capture_request",
                )
            existing_snapshot = await session.scalar(
                select(SourceChannelAudienceSnapshot)
                .where(
                    SourceChannelAudienceSnapshot.source_channel_id == channel.id,
                    SourceChannelAudienceSnapshot.capture_slot == event.capture_slot,
                    SourceChannelAudienceSnapshot.capture_reason == event.capture_reason,
                )
                .with_for_update(of=SourceChannelAudienceSnapshot)
            )
            if existing_snapshot is None or existing_snapshot.fetch_status is SourceChannelAudienceFetchStatus.FAILED:
                return None
            _clear_matching_lease(channel, event)
            return SourceChannelAudienceCaptureResult(
                source_channel_id=channel.id,
                fetch_status=existing_snapshot.fetch_status,
                snapshot_id=existing_snapshot.id,
                duplicate=True,
                error_code=existing_snapshot.error_code,
            )

    async def _fetch_observation(
        self,
        event: SourceChannelAudienceCaptureRequestedEvent,
    ) -> SourceChannelAudienceObservation:
        client: PipelineTelegramClientProtocol | None = None
        try:
            client = await _maybe_await(self._telegram_client_factory(event))
            audience = await client.fetch_channel_audience(event.platform_id)
            return source_channel_audience_observation_from_count(audience.subscriber_count)
        except PipelineTelegramFloodWaitError:
            raise
        except (PipelineTelegramSessionAuthRequiredError, PipelineTelegramSessionBannedError):
            raise
        except Exception as exc:  # noqa: BLE001 - persist a safe normalized failure outcome.
            return SourceChannelAudienceObservation(
                fetch_status=SourceChannelAudienceFetchStatus.FAILED,
                error_code=type(exc).__name__[:128],
            )
        finally:
            if client is not None and self._close_telegram_client_after_capture:
                with suppress(Exception):
                    await client.close()

    async def _handle_flood_wait(
        self,
        event: SourceChannelAudienceCaptureRequestedEvent,
        exc: PipelineTelegramFloodWaitError,
    ) -> SourceChannelAudienceCaptureResult:
        captured_at = utcnow()
        flood_wait_until = captured_at + timedelta(seconds=max(exc.wait_seconds, 0))
        async with self._session_factory() as session, session.begin():
            telegram_session = await session.scalar(
                select(TelegramSession)
                .where(
                    TelegramSession.id == event.telegram_session_id,
                    TelegramSession.name == event.session_name,
                )
                .with_for_update(of=TelegramSession)
            )
            if telegram_session is not None:
                telegram_session.status = TelegramSessionStatus.FLOOD_WAIT
                telegram_session.flood_wait_until = flood_wait_until
                telegram_session.last_error_class = type(exc).__name__[:128]
                telegram_session.last_error_text = str(exc)[:4000]
            channel = await session.scalar(
                select(SourceChannel)
                .where(SourceChannel.id == event.source_channel_id)
                .with_for_update(of=SourceChannel)
            )
            if channel is not None and _event_is_current_for_channel(event, channel):
                _clear_matching_lease(channel, event)

        return SourceChannelAudienceCaptureResult(
            source_channel_id=event.source_channel_id,
            fetch_status=None,
            snapshot_id=None,
            duplicate=False,
            error_code=type(exc).__name__,
        )

    async def _handle_terminal_session_failure(
        self,
        event: SourceChannelAudienceCaptureRequestedEvent,
        exc: PipelineTelegramSessionAuthRequiredError | PipelineTelegramSessionBannedError,
    ) -> SourceChannelAudienceCaptureResult:
        observed_at = utcnow()
        async with self._session_factory() as session, session.begin():
            telegram_session = await session.scalar(
                select(TelegramSession)
                .where(
                    TelegramSession.id == event.telegram_session_id,
                    TelegramSession.name == event.session_name,
                )
                .with_for_update(of=TelegramSession)
            )
            if telegram_session is not None:
                if isinstance(exc, PipelineTelegramSessionBannedError):
                    telegram_session.status = TelegramSessionStatus.QUARANTINED
                    telegram_session.quarantined_at = observed_at
                else:
                    telegram_session.status = TelegramSessionStatus.AUTH_REQUIRED
                    telegram_session.live_listener_started_at = None
                telegram_session.last_error_class = type(exc).__name__[:128]
                telegram_session.last_error_text = str(exc)[:4000]

            channel = await session.scalar(
                select(SourceChannel)
                .where(SourceChannel.id == event.source_channel_id)
                .with_for_update(of=SourceChannel)
            )
            if channel is not None and _event_is_current_for_channel(event, channel):
                _clear_matching_lease(channel, event)

        if self._telegram_session_invalidator is not None:
            await self._telegram_session_invalidator(session_id=event.telegram_session_id)

        return SourceChannelAudienceCaptureResult(
            source_channel_id=event.source_channel_id,
            fetch_status=None,
            snapshot_id=None,
            duplicate=False,
            error_code=type(exc).__name__,
        )


def _event_is_current_for_channel(
    event: SourceChannelAudienceCaptureRequestedEvent,
    channel: SourceChannel,
) -> bool:
    return (
        event.source_platform is SourcePlatform.TELEGRAM
        and channel.platform is SourcePlatform.TELEGRAM
        and channel.platform_id == event.platform_id
        and channel.telegram_session_id == event.telegram_session_id
        and channel.is_active
        and not channel.is_paused
        and channel.engagement_enabled
        and _same_instant(channel.next_audience_capture_at, event.scheduled_for)
    )


def _clear_matching_lease(
    channel: SourceChannel,
    event: SourceChannelAudienceCaptureRequestedEvent,
) -> None:
    if not _same_instant(channel.next_audience_capture_at, event.scheduled_for):
        return
    channel.audience_capture_locked_at = None
    channel.audience_capture_lock_owner = None


def _same_instant(left: datetime | None, right: datetime) -> bool:
    if left is None:
        return False
    return _as_utc(left) == _as_utc(right)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _maybe_await(
    value: PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
) -> PipelineTelegramClientProtocol:
    if inspect.isawaitable(value):
        return cast("PipelineTelegramClientProtocol", await value)
    return value


__all__ = [
    "SourceChannelAudienceCaptureResult",
    "SourceChannelAudienceCaptureService",
    "SourceChannelAudienceTelegramClientFactory",
    "SourceChannelAudienceTelegramSessionInvalidator",
    "build_pipeline_source_channel_audience_telegram_client_factory",
    "capture_source_channel_audience_request",
]
