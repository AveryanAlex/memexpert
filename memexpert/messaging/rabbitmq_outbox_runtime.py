"""Scheduler-owned runtime wrapper for publishing RabbitMQ outbox rows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from memexpert.core.broker import ensure_pipeline_broker_started
from memexpert.core.config import Settings, get_settings
from memexpert.messaging.rabbitmq_outbox import RabbitBrokerProtocol, RabbitOutboxRelay
from memexpert.models.base import utcnow

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

    return RabbitMQOutboxPublisherBatchResult(
        recovered=recovered,
        claimed=result.claimed,
        published=result.published,
        failed=result.failed,
        duration_seconds=time.perf_counter() - start_seconds,
    )


def rabbitmq_outbox_publisher_result_log_extra(
    job_id: str,
    result: RabbitMQOutboxPublisherBatchResult,
) -> dict[str, object]:
    """Return structured scheduler log fields for one RabbitMQ outbox publisher run."""

    return {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "recovered": result.recovered,
        "claimed": result.claimed,
        "published": result.published,
        "failed": result.failed,
        "duration_seconds": result.duration_seconds,
    }


__all__ = [
    "RabbitMQOutboxPublisherBatchResult",
    "rabbitmq_outbox_publisher_result_log_extra",
    "run_rabbitmq_outbox_publisher_batch",
]
