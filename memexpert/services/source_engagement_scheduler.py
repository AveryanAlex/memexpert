"""DB-backed scheduler for source engagement capture dispatch."""

from __future__ import annotations

import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select

from memexpert.core.config import Settings, get_settings
from memexpert.messaging.rabbitmq_outbox import RabbitPublisher
from memexpert.models.base import utcnow
from memexpert.models.content import MemeSource, SourceChannel, TelegramSession
from memexpert.models.enums import SourcePlatform, TelegramSessionStatus
from memexpert.pipeline.events import build_source_engagement_capture_message_spec
from memexpert.services.source_engagement import source_engagement_schedule_label_for

if TYPE_CHECKING:
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
            rows = (
                await session.execute(
                    select(MemeSource, TelegramSession.name)
                    .join(
                        SourceChannel,
                        and_(
                            SourceChannel.platform == SourcePlatform.TELEGRAM,
                            SourceChannel.platform_id == MemeSource.source_id,
                        ),
                    )
                    .join(TelegramSession, TelegramSession.id == SourceChannel.telegram_session_id)
                    .where(
                        MemeSource.platform == SourcePlatform.TELEGRAM,
                        MemeSource.source_alive.is_(True),
                        SourceChannel.is_active.is_(True),
                        SourceChannel.engagement_enabled.is_(True),
                        TelegramSession.enabled.is_(True),
                        TelegramSession.status == TelegramSessionStatus.ACTIVE,
                        TelegramSession.engagement_enabled.is_(True),
                        MemeSource.next_engagement_check_at.is_not(None),
                        MemeSource.next_engagement_check_at <= captured_now,
                        or_(
                            MemeSource.engagement_check_locked_at.is_(None),
                            MemeSource.engagement_check_locked_at <= stale_before,
                        ),
                    )
                    .order_by(MemeSource.next_engagement_check_at.asc(), MemeSource.id.asc())
                    .limit(self._settings.scheduler_source_engagement_capture_batch_size)
                    .with_for_update(of=MemeSource, skip_locked=True)
                )
            ).all()

            for source, session_name in rows:
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
                    session_name=_normalized_session_name(session_name),
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


def _default_lock_owner() -> str:
    return f"source-engagement-scheduler:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _normalized_session_name(session_name: str | None) -> str | None:
    if session_name is None:
        return None
    normalized = session_name.strip()
    return normalized or None


__all__ = [
    "SourceEngagementCaptureScheduler",
    "SourceEngagementCaptureSchedulerResult",
    "run_scheduler_source_engagement_capture_batch",
    "source_engagement_capture_scheduler_result_log_extra",
]
