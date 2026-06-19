"""Scheduler-owned runtime wrapper for publishing RabbitMQ outbox rows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, func, or_, select

from memexpert.core.broker import ensure_pipeline_broker_started
from memexpert.core.config import Settings, get_settings
from memexpert.messaging.rabbitmq_outbox import RabbitBrokerProtocol, RabbitOutboxRelay
from memexpert.models.base import utcnow
from memexpert.models.content import RabbitMQOutboxMessage
from memexpert.models.enums import RabbitMQOutboxMessageStatus

if TYPE_CHECKING:
    from memexpert.core.database import AsyncSessionFactory


@dataclass(frozen=True, slots=True)
class RabbitMQOutboxPublisherBatchResult:
    """Aggregate outcome for one scheduler RabbitMQ outbox publisher sweep."""

    recovered: int
    claimed: int
    published: int
    failed: int
    duration_seconds: float
    outbox_due_count: int = 0
    outbox_pending_count: int = 0
    outbox_failed_count: int = 0
    outbox_publishing_count: int = 0
    outbox_oldest_due_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class _RabbitMQOutboxBacklogSnapshot:
    outbox_due_count: int
    outbox_pending_count: int
    outbox_failed_count: int
    outbox_publishing_count: int
    outbox_oldest_due_age_seconds: float | None


async def run_rabbitmq_outbox_publisher_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    broker: RabbitBrokerProtocol | None = None,
) -> RabbitMQOutboxPublisherBatchResult:
    """Recover stale leases, publish one bounded outbox batch, and return counts."""

    start_seconds = time.perf_counter()
    resolved_settings = settings or get_settings()
    resolved_broker = broker
    if resolved_broker is None:
        resolved_broker = await ensure_pipeline_broker_started(settings=resolved_settings)

    stale_before = utcnow() - timedelta(
        seconds=resolved_settings.scheduler_rabbitmq_outbox_publisher_stale_timeout_seconds,
    )
    async with session_factory() as session:
        relay = RabbitOutboxRelay(session, broker=resolved_broker, settings=resolved_settings)
        recovered = await relay.recover_stale_publishing(stale_before=stale_before)
        result = await relay.publish_batch(
            limit=resolved_settings.scheduler_rabbitmq_outbox_publisher_batch_size,
        )
    backlog = await _load_rabbitmq_outbox_backlog_snapshot(session_factory)

    return RabbitMQOutboxPublisherBatchResult(
        recovered=recovered,
        claimed=result.claimed,
        published=result.published,
        failed=result.failed,
        duration_seconds=time.perf_counter() - start_seconds,
        outbox_due_count=backlog.outbox_due_count,
        outbox_pending_count=backlog.outbox_pending_count,
        outbox_failed_count=backlog.outbox_failed_count,
        outbox_publishing_count=backlog.outbox_publishing_count,
        outbox_oldest_due_age_seconds=backlog.outbox_oldest_due_age_seconds,
    )


async def _load_rabbitmq_outbox_backlog_snapshot(
    session_factory: AsyncSessionFactory,
) -> _RabbitMQOutboxBacklogSnapshot:
    now = utcnow()
    due_predicate = and_(
        RabbitMQOutboxMessage.status.in_((RabbitMQOutboxMessageStatus.PENDING, RabbitMQOutboxMessageStatus.FAILED)),
        or_(RabbitMQOutboxMessage.next_retry_at.is_(None), RabbitMQOutboxMessage.next_retry_at <= now),
    )
    stmt = select(
        func.coalesce(func.sum(case((due_predicate, 1), else_=0)), 0),
        func.coalesce(
            func.sum(case((RabbitMQOutboxMessage.status == RabbitMQOutboxMessageStatus.PENDING, 1), else_=0)),
            0,
        ),
        func.coalesce(
            func.sum(case((RabbitMQOutboxMessage.status == RabbitMQOutboxMessageStatus.FAILED, 1), else_=0)),
            0,
        ),
        func.coalesce(
            func.sum(case((RabbitMQOutboxMessage.status == RabbitMQOutboxMessageStatus.PUBLISHING, 1), else_=0)),
            0,
        ),
        func.min(case((due_predicate, RabbitMQOutboxMessage.created_at), else_=None)),
    )
    async with session_factory() as session:
        due_count, pending_count, failed_count, publishing_count, oldest_due_created_at = (
            await session.execute(stmt)
        ).one()

    oldest_due_age_seconds = None
    if oldest_due_created_at is not None:
        oldest_due_age_seconds = max((now - oldest_due_created_at).total_seconds(), 0.0)
    return _RabbitMQOutboxBacklogSnapshot(
        outbox_due_count=int(due_count or 0),
        outbox_pending_count=int(pending_count or 0),
        outbox_failed_count=int(failed_count or 0),
        outbox_publishing_count=int(publishing_count or 0),
        outbox_oldest_due_age_seconds=oldest_due_age_seconds,
    )


def rabbitmq_outbox_publisher_result_log_extra(
    job_id: str,
    result: RabbitMQOutboxPublisherBatchResult,
) -> dict[str, object]:
    """Return structured scheduler log fields for one RabbitMQ outbox publisher run."""

    return {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "status": "completed",
        "degraded_mode": result.failed > 0 or result.outbox_due_count > 0,
        "recovered": result.recovered,
        "claimed": result.claimed,
        "published": result.published,
        "failed": result.failed,
        "duration_seconds": result.duration_seconds,
        "outbox_due_count": result.outbox_due_count,
        "outbox_pending_count": result.outbox_pending_count,
        "outbox_failed_count": result.outbox_failed_count,
        "outbox_publishing_count": result.outbox_publishing_count,
        "outbox_oldest_due_age_seconds": result.outbox_oldest_due_age_seconds,
    }


__all__ = [
    "RabbitMQOutboxPublisherBatchResult",
    "rabbitmq_outbox_publisher_result_log_extra",
    "run_rabbitmq_outbox_publisher_batch",
]
