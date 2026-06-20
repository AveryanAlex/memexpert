"""DB-backed scheduler for source engagement capture dispatch."""

from __future__ import annotations

import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from memexpert.core.config import Settings, get_settings
from memexpert.messaging.rabbitmq_outbox import RabbitPublisher
from memexpert.models.base import utcnow
from memexpert.models.content import MemeSource, SourceChannel, TelegramSession
from memexpert.models.enums import SourcePlatform, TelegramSessionStatus
from memexpert.pipeline.events import build_source_engagement_capture_message_spec
from memexpert.services.source_engagement import source_engagement_schedule_label_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from memexpert.core.database import AsyncSessionFactory


@dataclass(frozen=True, slots=True)
class SourceEngagementCaptureSchedulerResult:
    """Aggregate outcome for one due-source engagement capture enqueue sweep."""

    claimed: int
    enqueued: int
    meme_source_ids: tuple[uuid.UUID, ...]
    outbox_message_ids: tuple[uuid.UUID, ...]
    duration_seconds: float


async def run_scheduler_source_engagement_capture_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    publisher: RabbitPublisher | None = None,
    now: datetime | None = None,
    lock_owner: str | None = None,
) -> SourceEngagementCaptureSchedulerResult:
    """Claim due Telegram sources and enqueue engagement capture work through the generic outbox."""

    service = SourceEngagementCaptureScheduler(
        session_factory,
        settings=settings,
        publisher=publisher,
        lock_owner=lock_owner,
    )
    return await service.run(now=now)


def source_engagement_capture_scheduler_result_log_extra(
    job_id: str,
    result: SourceEngagementCaptureSchedulerResult,
) -> dict[str, object]:
    """Return structured scheduler log fields for one engagement-capture enqueue run."""

    return {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "claimed": result.claimed,
        "enqueued": result.enqueued,
        "meme_source_ids": [str(source_id) for source_id in result.meme_source_ids],
        "outbox_message_ids": [str(message_id) for message_id in result.outbox_message_ids],
        "duration_seconds": result.duration_seconds,
    }


class SourceEngagementCaptureScheduler:
    """Claim due source rows and persist RabbitMQ outbox messages in the same transaction."""

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

    async def run(self, *, now: datetime | None = None) -> SourceEngagementCaptureSchedulerResult:
        start_seconds = time.perf_counter()
        captured_now = now or utcnow()
        stale_before = captured_now - timedelta(
            seconds=self._settings.scheduler_source_engagement_capture_lease_timeout_seconds,
        )

        claimed_source_ids: list[uuid.UUID] = []
        outbox_message_ids: list[uuid.UUID] = []

        async with self._session_factory() as session, session.begin():
            remaining_global_batch_size = self._settings.scheduler_source_engagement_capture_batch_size
            due_sessions = await self._load_due_sessions(
                session,
                captured_now=captured_now,
                stale_before=stale_before,
            )

            for telegram_session in due_sessions:
                if remaining_global_batch_size <= 0:
                    break

                _resume_expired_flood_wait_session(telegram_session, captured_now)
                per_session_limit = min(
                    remaining_global_batch_size,
                    self._settings.scheduler_source_engagement_capture_per_session_batch_size,
                )
                sources = await self._claim_due_sources_for_session(
                    session,
                    telegram_session_id=telegram_session.id,
                    captured_now=captured_now,
                    stale_before=stale_before,
                    limit=per_session_limit,
                )
                if not sources:
                    continue

                remaining_global_batch_size -= len(sources)
                session_name = _normalized_session_name(telegram_session.name)
                for source in sources:
                    claimed_source_ids.append(source.id)
                    source.engagement_check_locked_at = captured_now
                    source.engagement_check_lock_owner = self._lock_owner
                    source.engagement_check_attempt_count += 1

                    schedule_label = source_engagement_schedule_label_for(
                        source.published_at,
                        source.next_engagement_check_at,
                    )
                    if schedule_label is None or source.next_engagement_check_at is None:
                        source.engagement_check_locked_at = None
                        source.engagement_check_lock_owner = None
                        source.last_engagement_error_code = "schedule_label_unresolved"
                        continue

                    spec = build_source_engagement_capture_message_spec(
                        source,
                        scheduled_for=source.next_engagement_check_at,
                        schedule_label=schedule_label,
                        settings=self._settings,
                        telegram_session_id=telegram_session.id,
                        session_name=session_name,
                    )
                    outbox_message_id = await self._publisher.publish(spec, session=session, outbox=True)
                    outbox_message_ids.append(outbox_message_id)

        return SourceEngagementCaptureSchedulerResult(
            claimed=len(claimed_source_ids),
            enqueued=len(outbox_message_ids),
            meme_source_ids=tuple(claimed_source_ids),
            outbox_message_ids=tuple(outbox_message_ids),
            duration_seconds=time.perf_counter() - start_seconds,
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
                func.min(MemeSource.next_engagement_check_at).label("oldest_due_at"),
            )
            .join(SourceChannel, SourceChannel.telegram_session_id == TelegramSession.id)
            .join(
                MemeSource,
                and_(
                    MemeSource.platform == SourceChannel.platform,
                    MemeSource.source_id == SourceChannel.platform_id,
                ),
            )
            .where(*_eligible_source_engagement_filters(captured_now, stale_before))
            .group_by(TelegramSession.id)
            .subquery()
        )
        result = await session.execute(
            select(TelegramSession)
            .join(oldest_due_by_session, oldest_due_by_session.c.telegram_session_id == TelegramSession.id)
            .order_by(oldest_due_by_session.c.oldest_due_at.asc(), TelegramSession.id.asc())
        )
        return tuple(result.scalars().all())

    async def _claim_due_sources_for_session(
        self,
        session: AsyncSession,
        *,
        telegram_session_id: uuid.UUID,
        captured_now: datetime,
        stale_before: datetime,
        limit: int,
    ) -> tuple[MemeSource, ...]:
        result = await session.execute(
            select(MemeSource)
            .join(
                SourceChannel,
                and_(
                    MemeSource.platform == SourceChannel.platform,
                    MemeSource.source_id == SourceChannel.platform_id,
                ),
            )
            .join(TelegramSession, TelegramSession.id == SourceChannel.telegram_session_id)
            .where(
                TelegramSession.id == telegram_session_id,
                *_eligible_source_engagement_filters(captured_now, stale_before),
            )
            .order_by(MemeSource.next_engagement_check_at.asc(), MemeSource.id.asc())
            .limit(limit)
            .with_for_update(of=MemeSource, skip_locked=True)
        )
        return tuple(result.scalars().all())


def _default_lock_owner() -> str:
    return f"source-engagement-scheduler:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _eligible_source_engagement_filters(
    captured_now: datetime,
    stale_before: datetime,
) -> tuple[ColumnElement[bool], ...]:
    return (
        MemeSource.platform == SourcePlatform.TELEGRAM,
        MemeSource.source_alive.is_(True),
        SourceChannel.platform == SourcePlatform.TELEGRAM,
        SourceChannel.is_active.is_(True),
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
        MemeSource.next_engagement_check_at.is_not(None),
        MemeSource.next_engagement_check_at <= captured_now,
        or_(
            MemeSource.engagement_check_locked_at.is_(None),
            MemeSource.engagement_check_locked_at <= stale_before,
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
        raise ValueError("source engagement capture requires a non-blank Telegram session name.")
    return normalized


__all__ = [
    "SourceEngagementCaptureScheduler",
    "SourceEngagementCaptureSchedulerResult",
    "run_scheduler_source_engagement_capture_batch",
    "source_engagement_capture_scheduler_result_log_extra",
]
