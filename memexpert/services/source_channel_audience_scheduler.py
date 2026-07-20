"""DB-backed scheduler for daily Telegram channel audience capture dispatch."""

from __future__ import annotations

import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from memexpert.core.config import Settings, get_settings
from memexpert.messaging.rabbitmq_outbox import RabbitPublisher
from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, TelegramSession
from memexpert.models.enums import SourcePlatform, TelegramSessionStatus
from memexpert.pipeline.events import build_source_channel_audience_capture_message_spec

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from memexpert.core.database import AsyncSessionFactory


@dataclass(frozen=True, slots=True)
class SourceChannelAudienceCaptureSchedulerResult:
    """Aggregate outcome for one due-channel audience enqueue sweep."""

    claimed: int
    enqueued: int
    source_channel_ids: tuple[uuid.UUID, ...]
    outbox_message_ids: tuple[uuid.UUID, ...]
    duration_seconds: float


async def run_scheduler_source_channel_audience_capture_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    publisher: RabbitPublisher | None = None,
    now: datetime | None = None,
    lock_owner: str | None = None,
) -> SourceChannelAudienceCaptureSchedulerResult:
    """Claim due Telegram channels and enqueue audience work transactionally."""

    service = SourceChannelAudienceCaptureScheduler(
        session_factory,
        settings=settings,
        publisher=publisher,
        lock_owner=lock_owner,
    )
    return await service.run(now=now)


def source_channel_audience_capture_scheduler_result_log_extra(
    job_id: str,
    result: SourceChannelAudienceCaptureSchedulerResult,
) -> dict[str, object]:
    """Return structured scheduler fields for one audience enqueue sweep."""

    return {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "status": "completed",
        "degraded_mode": False,
        "claimed": result.claimed,
        "enqueued": result.enqueued,
        "source_channel_ids": [str(channel_id) for channel_id in result.source_channel_ids],
        "outbox_message_ids": [str(message_id) for message_id in result.outbox_message_ids],
        "duration_seconds": result.duration_seconds,
    }


class SourceChannelAudienceCaptureScheduler:
    """Lease due channel rows and persist session-affined outbox messages."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        settings: Settings | None = None,
        publisher: RabbitPublisher | None = None,
        lock_owner: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._publisher = publisher or RabbitPublisher(settings=self._settings)
        self._lock_owner = lock_owner or _default_lock_owner()

    async def run(self, *, now: datetime | None = None) -> SourceChannelAudienceCaptureSchedulerResult:
        started = time.perf_counter()
        captured_now = _as_utc(now or utcnow())
        stale_before = captured_now - timedelta(
            seconds=self._settings.scheduler_source_channel_audience_capture_lease_timeout_seconds,
        )
        claimed_channel_ids: list[uuid.UUID] = []
        outbox_message_ids: list[uuid.UUID] = []

        async with self._session_factory() as session, session.begin():
            remaining = self._settings.scheduler_source_channel_audience_capture_batch_size
            due_sessions = await self._load_due_sessions(
                session,
                captured_now=captured_now,
                stale_before=stale_before,
            )
            for telegram_session in due_sessions:
                if remaining <= 0:
                    break
                _resume_expired_flood_wait_session(telegram_session, captured_now)
                channels = await self._claim_due_channels_for_session(
                    session,
                    telegram_session_id=telegram_session.id,
                    captured_now=captured_now,
                    stale_before=stale_before,
                    limit=min(
                        remaining,
                        self._settings.scheduler_source_channel_audience_capture_per_session_batch_size,
                    ),
                )
                if not channels:
                    continue
                remaining -= len(channels)
                session_name = _normalized_session_name(telegram_session.name)
                for channel in channels:
                    scheduled_for = _as_utc(channel.next_audience_capture_at or captured_now)
                    channel.next_audience_capture_at = scheduled_for
                    channel.audience_capture_locked_at = captured_now
                    channel.audience_capture_lock_owner = self._lock_owner
                    channel.audience_capture_attempt_count += 1
                    claimed_channel_ids.append(channel.id)
                    spec = build_source_channel_audience_capture_message_spec(
                        channel,
                        scheduled_for=scheduled_for,
                        capture_slot=scheduled_for.date(),
                        settings=self._settings,
                        telegram_session_id=telegram_session.id,
                        session_name=session_name,
                    )
                    outbox_message_ids.append(
                        await self._publisher.publish(spec, session=session, outbox=True)
                    )

        return SourceChannelAudienceCaptureSchedulerResult(
            claimed=len(claimed_channel_ids),
            enqueued=len(outbox_message_ids),
            source_channel_ids=tuple(claimed_channel_ids),
            outbox_message_ids=tuple(outbox_message_ids),
            duration_seconds=time.perf_counter() - started,
        )

    async def _load_due_sessions(
        self,
        session: AsyncSession,
        *,
        captured_now: datetime,
        stale_before: datetime,
    ) -> tuple[TelegramSession, ...]:
        oldest_due_by_session = (
            select(
                TelegramSession.id.label("telegram_session_id"),
                func.min(func.coalesce(SourceChannel.next_audience_capture_at, captured_now)).label("oldest_due_at"),
            )
            .join(SourceChannel, SourceChannel.telegram_session_id == TelegramSession.id)
            .where(*_eligible_audience_capture_filters(captured_now, stale_before))
            .group_by(TelegramSession.id)
            .subquery()
        )
        result = await session.execute(
            select(TelegramSession)
            .join(oldest_due_by_session, oldest_due_by_session.c.telegram_session_id == TelegramSession.id)
            .order_by(oldest_due_by_session.c.oldest_due_at.asc(), TelegramSession.id.asc())
        )
        return tuple(result.scalars().all())

    async def _claim_due_channels_for_session(
        self,
        session: AsyncSession,
        *,
        telegram_session_id: uuid.UUID,
        captured_now: datetime,
        stale_before: datetime,
        limit: int,
    ) -> tuple[SourceChannel, ...]:
        result = await session.execute(
            select(SourceChannel)
            .join(TelegramSession, TelegramSession.id == SourceChannel.telegram_session_id)
            .where(
                TelegramSession.id == telegram_session_id,
                *_eligible_audience_capture_filters(captured_now, stale_before),
            )
            .order_by(SourceChannel.next_audience_capture_at.asc().nullsfirst(), SourceChannel.id.asc())
            .limit(limit)
            .with_for_update(of=SourceChannel, skip_locked=True)
        )
        return tuple(result.scalars().all())


def _eligible_audience_capture_filters(
    captured_now: datetime,
    stale_before: datetime,
) -> tuple[ColumnElement[bool], ...]:
    return (
        SourceChannel.platform == SourcePlatform.TELEGRAM,
        SourceChannel.is_active.is_(True),
        SourceChannel.is_paused.is_(False),
        SourceChannel.engagement_enabled.is_(True),
        TelegramSession.enabled.is_(True),
        TelegramSession.engagement_enabled.is_(True),
        or_(
            TelegramSession.status == TelegramSessionStatus.ACTIVE,
            and_(
                TelegramSession.status == TelegramSessionStatus.FLOOD_WAIT,
                or_(
                    TelegramSession.flood_wait_until.is_(None),
                    TelegramSession.flood_wait_until <= captured_now,
                ),
            ),
        ),
        or_(
            SourceChannel.next_audience_capture_at.is_(None),
            SourceChannel.next_audience_capture_at <= captured_now,
        ),
        or_(
            SourceChannel.audience_capture_locked_at.is_(None),
            SourceChannel.audience_capture_locked_at <= stale_before,
        ),
    )


def _resume_expired_flood_wait_session(telegram_session: TelegramSession, captured_now: datetime) -> None:
    if telegram_session.status is not TelegramSessionStatus.FLOOD_WAIT:
        return
    if telegram_session.flood_wait_until is not None and telegram_session.flood_wait_until > captured_now:
        return
    telegram_session.status = TelegramSessionStatus.ACTIVE
    telegram_session.flood_wait_until = None
    telegram_session.last_error_class = None
    telegram_session.last_error_text = None


def _normalized_session_name(session_name: str) -> str:
    normalized = session_name.strip()
    if not normalized:
        raise ValueError("source channel audience capture requires a non-blank Telegram session name.")
    return normalized


def _default_lock_owner() -> str:
    return f"source-channel-audience-scheduler:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SourceChannelAudienceCaptureScheduler",
    "SourceChannelAudienceCaptureSchedulerResult",
    "run_scheduler_source_channel_audience_capture_batch",
    "source_channel_audience_capture_scheduler_result_log_extra",
]
