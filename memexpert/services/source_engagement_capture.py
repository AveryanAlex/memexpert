"""Worker-side execution for scheduled source engagement capture requests."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select

from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionBannedError,
    RawTelegramMessage,
)
from memexpert.models.base import utcnow
from memexpert.models.content import MemeSource, MemeSourceEngagementSnapshot
from memexpert.models.enums import (
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
)
from memexpert.pipeline.events import SourceEngagementCaptureRequestedEvent
from memexpert.services.source_engagement import (
    SourceEngagementMetrics,
    add_source_engagement_snapshot,
    update_source_engagement_snapshot,
)

if TYPE_CHECKING:
    import uuid

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


type SourceEngagementTelegramClientFactory = Callable[
    [SourceEngagementCaptureRequestedEvent],
    PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
]


@dataclass(frozen=True, slots=True)
class SourceEngagementCaptureResult:
    """Outcome for one source-engagement capture request."""

    meme_source_id: uuid.UUID
    fetch_status: SourceEngagementFetchStatus | None
    snapshot_id: uuid.UUID | None
    duplicate: bool
    error_code: str | None = None


async def capture_source_engagement_request(
    session_factory: AsyncSessionFactory,
    event: SourceEngagementCaptureRequestedEvent,
    *,
    telegram_client_factory: SourceEngagementTelegramClientFactory,
) -> SourceEngagementCaptureResult:
    """Fetch Telegram stats-only metadata and persist the scheduled source engagement snapshot."""

    service = SourceEngagementCaptureService(
        session_factory,
        telegram_client_factory=telegram_client_factory,
    )
    return await service.capture(event)


def build_pipeline_source_engagement_telegram_client_factory(
    settings: Settings,
) -> SourceEngagementTelegramClientFactory:
    """Build the real runtime Telegram client factory without importing Telethon eagerly."""

    def _factory(event: SourceEngagementCaptureRequestedEvent) -> PipelineTelegramClientProtocol:
        if event.session_name is None:
            raise PipelineTelegramProviderUnavailableError(
                "Source engagement capture requires a Telegram session_name on the request.",
            )

        from memexpert.crawlers.telegram.telethon_adapter import PipelineTelethonClient

        return PipelineTelethonClient.create(settings=settings, session_name=event.session_name)

    return _factory


class SourceEngagementCaptureService:
    """Capture one scheduled source engagement request and update durable source state."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        telegram_client_factory: SourceEngagementTelegramClientFactory,
    ) -> None:
        self._session_factory = session_factory
        self._telegram_client_factory = telegram_client_factory

    async def capture(self, event: SourceEngagementCaptureRequestedEvent) -> SourceEngagementCaptureResult:
        existing_result = await self._short_circuit_existing_terminal_snapshot(event)
        if existing_result is not None:
            return existing_result

        metrics, fetch_status = await self._fetch_metrics(event)
        captured_at = utcnow()

        async with self._session_factory() as session, session.begin():
            source = await session.scalar(
                select(MemeSource)
                .where(MemeSource.id == event.meme_source_id)
                .with_for_update(of=MemeSource)
            )
            if source is None:
                return SourceEngagementCaptureResult(
                    meme_source_id=event.meme_source_id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="meme_source_not_found",
                )
            if not _event_matches_source(event, source):
                _clear_matching_lease(source, event)
                source.last_engagement_error_code = "stale_capture_request"
                return SourceEngagementCaptureResult(
                    meme_source_id=source.id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="stale_capture_request",
                )

            if fetch_status is SourceEngagementFetchStatus.FAILED:
                metrics = replace(metrics, source_alive=source.source_alive)

            existing_snapshot = await session.scalar(
                select(MemeSourceEngagementSnapshot)
                .where(
                    MemeSourceEngagementSnapshot.meme_source_id == source.id,
                    MemeSourceEngagementSnapshot.scheduled_for == event.scheduled_for,
                    MemeSourceEngagementSnapshot.schedule_label == event.schedule_label,
                )
                .with_for_update(of=MemeSourceEngagementSnapshot)
            )
            if (
                existing_snapshot is not None
                and existing_snapshot.fetch_status is not SourceEngagementFetchStatus.FAILED
            ):
                _clear_matching_lease(source, event)
                return SourceEngagementCaptureResult(
                    meme_source_id=source.id,
                    fetch_status=existing_snapshot.fetch_status,
                    snapshot_id=existing_snapshot.id,
                    duplicate=True,
                    error_code=existing_snapshot.error_code,
                )

            if existing_snapshot is not None:
                snapshot = await update_source_engagement_snapshot(
                    session,
                    existing_snapshot,
                    source,
                    metrics,
                    capture_reason=SourceEngagementCaptureReason.SCHEDULED,
                    fetch_status=fetch_status,
                    captured_at=captured_at,
                    scheduled_for=event.scheduled_for,
                    schedule_label=event.schedule_label,
                )
            else:
                snapshot = await add_source_engagement_snapshot(
                    session,
                    source,
                    metrics,
                    capture_reason=SourceEngagementCaptureReason.SCHEDULED,
                    fetch_status=fetch_status,
                    captured_at=captured_at,
                    scheduled_for=event.scheduled_for,
                    schedule_label=event.schedule_label,
                )

        return SourceEngagementCaptureResult(
            meme_source_id=event.meme_source_id,
            fetch_status=fetch_status,
            snapshot_id=snapshot.id,
            duplicate=existing_snapshot is not None,
            error_code=metrics.error_code,
        )

    async def _short_circuit_existing_terminal_snapshot(
        self,
        event: SourceEngagementCaptureRequestedEvent,
    ) -> SourceEngagementCaptureResult | None:
        async with self._session_factory() as session, session.begin():
            source = await session.scalar(
                select(MemeSource)
                .where(MemeSource.id == event.meme_source_id)
                .with_for_update(of=MemeSource)
            )
            if source is None:
                return SourceEngagementCaptureResult(
                    meme_source_id=event.meme_source_id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="meme_source_not_found",
                )
            if not _event_matches_source(event, source):
                _clear_matching_lease(source, event)
                source.last_engagement_error_code = "stale_capture_request"
                return SourceEngagementCaptureResult(
                    meme_source_id=source.id,
                    fetch_status=None,
                    snapshot_id=None,
                    duplicate=False,
                    error_code="stale_capture_request",
                )

            existing_snapshot = await session.scalar(
                select(MemeSourceEngagementSnapshot)
                .where(
                    MemeSourceEngagementSnapshot.meme_source_id == source.id,
                    MemeSourceEngagementSnapshot.scheduled_for == event.scheduled_for,
                    MemeSourceEngagementSnapshot.schedule_label == event.schedule_label,
                )
                .with_for_update(of=MemeSourceEngagementSnapshot)
            )
            if existing_snapshot is None or existing_snapshot.fetch_status is SourceEngagementFetchStatus.FAILED:
                return None

            _clear_matching_lease(source, event)
            return SourceEngagementCaptureResult(
                meme_source_id=source.id,
                fetch_status=existing_snapshot.fetch_status,
                snapshot_id=existing_snapshot.id,
                duplicate=True,
                error_code=existing_snapshot.error_code,
            )

    async def _fetch_metrics(
        self,
        event: SourceEngagementCaptureRequestedEvent,
    ) -> tuple[SourceEngagementMetrics, SourceEngagementFetchStatus]:
        client: PipelineTelegramClientProtocol | None = None
        try:
            client = await _maybe_await(self._telegram_client_factory(event))
            message = await client.fetch_single_message(channel_id=event.source_id, post_id=event.post_id)
            return source_engagement_metrics_from_telegram_message(message), SourceEngagementFetchStatus.SUCCESS
        except Exception as exc:  # noqa: BLE001 - every Telegram/provider failure becomes a handled snapshot.
            return _metrics_for_capture_error(exc), _fetch_status_for_capture_error(exc)
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.close()


def source_engagement_metrics_from_telegram_message(message: RawTelegramMessage) -> SourceEngagementMetrics:
    """Convert a stats-only Telegram message projection into canonical engagement metrics."""

    reactions = None if message.reactions is None else dict(message.reactions)
    return SourceEngagementMetrics(
        view_count=message.view_count,
        reactions=reactions,
        comment_count=message.comment_count,
        forward_count=message.forward_count,
        comments_state=message.comments_state,
        source_alive=True,
        raw_metrics={
            "view_count": message.view_count,
            "reactions": reactions,
            "comment_count": message.comment_count,
            "forward_count": message.forward_count,
            "comments_state": message.comments_state.value,
            "message_id": message.message_id,
            "channel_id": message.channel_id,
            "published_at": message.published_at.isoformat(),
        },
    )


def _event_matches_source(event: SourceEngagementCaptureRequestedEvent, source: MemeSource) -> bool:
    return (
        event.source_platform is SourcePlatform.TELEGRAM
        and source.platform is SourcePlatform.TELEGRAM
        and source.source_id == event.source_id
        and source.post_id == event.post_id
    )


def _clear_matching_lease(source: MemeSource, event: SourceEngagementCaptureRequestedEvent) -> None:
    if not _same_instant(source.next_engagement_check_at, event.scheduled_for):
        return
    source.engagement_check_locked_at = None
    source.engagement_check_lock_owner = None


def _metrics_for_capture_error(exc: Exception) -> SourceEngagementMetrics:
    error_code = _error_code(exc)
    source_alive = not isinstance(
        exc,
        PipelineTelegramMalformedMessageError | PipelineTelegramSessionBannedError,
    )
    return SourceEngagementMetrics(
        comments_state=SourceEngagementCommentsState.UNKNOWN,
        source_alive=source_alive,
        raw_metrics={"error_class": error_code, "error_text": str(exc)},
        error_code=error_code,
    )


def _fetch_status_for_capture_error(exc: Exception) -> SourceEngagementFetchStatus:
    if isinstance(exc, PipelineTelegramMalformedMessageError):
        return SourceEngagementFetchStatus.NOT_FOUND
    if isinstance(exc, PipelineTelegramSessionBannedError):
        return SourceEngagementFetchStatus.NOT_ACCESSIBLE
    if isinstance(
        exc,
        PipelineTelegramFloodWaitError | PipelineTelegramProviderUnavailableError | PipelineTelegramError,
    ):
        return SourceEngagementFetchStatus.FAILED
    return SourceEngagementFetchStatus.FAILED


def _error_code(exc: Exception) -> str:
    return type(exc).__name__[:128]


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
    "SourceEngagementCaptureResult",
    "SourceEngagementCaptureService",
    "SourceEngagementTelegramClientFactory",
    "build_pipeline_source_engagement_telegram_client_factory",
    "capture_source_engagement_request",
    "source_engagement_metrics_from_telegram_message",
]
