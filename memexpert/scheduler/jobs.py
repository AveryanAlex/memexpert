"""Scheduler job definitions and runtime wrappers."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memexpert.core.database import build_async_session_factory
from memexpert.messaging.rabbitmq_outbox_runtime import (
    rabbitmq_outbox_publisher_result_log_extra,
    run_rabbitmq_outbox_publisher_batch,
)
from memexpert.services.public_trends import refresh_public_trend_materialized_views
from memexpert.services.scheduler_batch_jobs import (
    run_scheduler_search_index_sync_batch,
    run_scheduler_seo_backlog_batch,
    scheduler_batch_result_log_extra,
)
from memexpert.services.source_engagement_scheduler import (
    run_scheduler_source_engagement_capture_batch,
    source_engagement_capture_scheduler_result_log_extra,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from memexpert.core.config import Settings

logger = logging.getLogger(__name__)

JOB_ID_MATERIALIZED_VIEW_REFRESH = "materialized-view-refresh"
JOB_ID_SOURCE_ENGAGEMENT_CAPTURE = "source-engagement-capture"
JOB_ID_MOTD = "motd"
JOB_ID_SEARCH_INDEX_SYNC = "search-index-sync"
JOB_ID_SEO_BACKLOG_BATCHES = "seo-backlog-batches"
JOB_ID_RABBITMQ_OUTBOX_PUBLISHER = "rabbitmq-outbox-publisher"

SchedulerJobAction = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SchedulerJobDefinition:
    id: str
    trigger_seconds: float
    action: SchedulerJobAction
    enabled: bool
    description: str


def build_scheduler_job_definitions(settings: Settings, engine: AsyncEngine) -> tuple[SchedulerJobDefinition, ...]:
    return (
        SchedulerJobDefinition(
            id=JOB_ID_MATERIALIZED_VIEW_REFRESH,
            trigger_seconds=settings.scheduler_materialized_view_refresh_interval_seconds,
            action=lambda: refresh_public_trend_materialized_views(engine, concurrently=True),
            enabled=settings.scheduler_materialized_view_refresh_enabled,
            description="Refresh public trend materialized views.",
        ),
        SchedulerJobDefinition(
            id=JOB_ID_SOURCE_ENGAGEMENT_CAPTURE,
            trigger_seconds=settings.scheduler_source_engagement_capture_interval_seconds,
            action=_build_source_engagement_capture_job_action(settings, engine),
            enabled=settings.scheduler_source_engagement_capture_enabled,
            description="Enqueue due source engagement capture work.",
        ),
        SchedulerJobDefinition(
            id=JOB_ID_MOTD,
            trigger_seconds=settings.scheduler_motd_interval_seconds,
            action=_build_placeholder_job_action(JOB_ID_MOTD),
            enabled=settings.scheduler_motd_enabled,
            description="Refresh the message of the day.",
        ),
        SchedulerJobDefinition(
            id=JOB_ID_SEARCH_INDEX_SYNC,
            trigger_seconds=settings.scheduler_search_index_sync_interval_seconds,
            action=_build_search_index_sync_job_action(settings, engine),
            enabled=settings.scheduler_search_index_sync_enabled,
            description="Sync the search index.",
        ),
        SchedulerJobDefinition(
            id=JOB_ID_SEO_BACKLOG_BATCHES,
            trigger_seconds=settings.scheduler_seo_backlog_batches_interval_seconds,
            action=_build_seo_backlog_batches_job_action(settings, engine),
            enabled=settings.scheduler_seo_backlog_batches_enabled,
            description="Process SEO backlog batches.",
        ),
        SchedulerJobDefinition(
            id=JOB_ID_RABBITMQ_OUTBOX_PUBLISHER,
            trigger_seconds=settings.scheduler_rabbitmq_outbox_publisher_interval_seconds,
            action=_build_rabbitmq_outbox_publisher_job_action(settings, engine),
            enabled=settings.scheduler_rabbitmq_outbox_publisher_enabled,
            description="Publish durable RabbitMQ outbox messages.",
        ),
    )


def enabled_scheduler_jobs(settings: Settings, engine: AsyncEngine) -> tuple[SchedulerJobDefinition, ...]:
    return tuple(job for job in build_scheduler_job_definitions(settings, engine) if job.enabled)


async def run_logged_job(job_id: str, action: SchedulerJobAction) -> None:
    start_seconds = time.perf_counter()
    logger.info("scheduler_job_started", extra={"event": "scheduler_job_started", "job_id": job_id})

    try:
        await action()
    except Exception:
        duration_seconds = time.perf_counter() - start_seconds
        logger.exception(
            "scheduler_job_failed",
            extra={
                "event": "scheduler_job_failed",
                "job_id": job_id,
                "duration_seconds": duration_seconds,
            },
        )
        return

    duration_seconds = time.perf_counter() - start_seconds
    logger.info(
        "scheduler_job_succeeded",
        extra={
            "event": "scheduler_job_succeeded",
            "job_id": job_id,
            "duration_seconds": duration_seconds,
        },
    )


def _build_placeholder_job_action(job_id: str) -> SchedulerJobAction:
    async def _action() -> None:
        logger.info(
            "scheduler_job_placeholder_completed",
            extra={"event": "scheduler_job_placeholder_completed", "job_id": job_id},
        )

    return _action


def _build_source_engagement_capture_job_action(settings: Settings, engine: AsyncEngine) -> SchedulerJobAction:
    async def _action() -> None:
        session_factory = build_async_session_factory(engine)
        result = await run_scheduler_source_engagement_capture_batch(session_factory, settings=settings)
        logger.info(
            "scheduler_job_batch_result",
            extra=source_engagement_capture_scheduler_result_log_extra(
                JOB_ID_SOURCE_ENGAGEMENT_CAPTURE,
                result,
            ),
        )

    return _action


def _build_search_index_sync_job_action(settings: Settings, engine: AsyncEngine) -> SchedulerJobAction:
    async def _action() -> None:
        session_factory = build_async_session_factory(engine)
        result = await run_scheduler_search_index_sync_batch(session_factory, settings=settings)
        logger.info(
            "scheduler_job_batch_result",
            extra=scheduler_batch_result_log_extra(JOB_ID_SEARCH_INDEX_SYNC, result),
        )

    return _action


def _build_seo_backlog_batches_job_action(settings: Settings, engine: AsyncEngine) -> SchedulerJobAction:
    async def _action() -> None:
        session_factory = build_async_session_factory(engine)
        result = await run_scheduler_seo_backlog_batch(session_factory, settings=settings)
        logger.info(
            "scheduler_job_batch_result",
            extra=scheduler_batch_result_log_extra(JOB_ID_SEO_BACKLOG_BATCHES, result),
        )

    return _action


def _build_rabbitmq_outbox_publisher_job_action(settings: Settings, engine: AsyncEngine) -> SchedulerJobAction:
    async def _action() -> None:
        session_factory = build_async_session_factory(engine)
        result = await run_rabbitmq_outbox_publisher_batch(session_factory, settings=settings)
        logger.info(
            "scheduler_job_batch_result",
            extra=rabbitmq_outbox_publisher_result_log_extra(JOB_ID_RABBITMQ_OUTBOX_PUBLISHER, result),
        )

    return _action


__all__ = [
    "JOB_ID_MATERIALIZED_VIEW_REFRESH",
    "JOB_ID_MOTD",
    "JOB_ID_RABBITMQ_OUTBOX_PUBLISHER",
    "JOB_ID_SEARCH_INDEX_SYNC",
    "JOB_ID_SEO_BACKLOG_BATCHES",
    "JOB_ID_SOURCE_ENGAGEMENT_CAPTURE",
    "SchedulerJobDefinition",
    "build_scheduler_job_definitions",
    "enabled_scheduler_jobs",
    "run_logged_job",
]
