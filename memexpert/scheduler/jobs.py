"""Scheduler job definitions and runtime wrappers."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memexpert.services.public_trends import refresh_public_trend_materialized_views

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from memexpert.core.config import Settings

logger = logging.getLogger(__name__)

JOB_ID_MATERIALIZED_VIEW_REFRESH = "materialized-view-refresh"
JOB_ID_POPULARITY_SNAPSHOTS = "popularity-snapshots"
JOB_ID_MOTD = "motd"
JOB_ID_SEARCH_INDEX_SYNC = "search-index-sync"
JOB_ID_SEO_BACKLOG_BATCHES = "seo-backlog-batches"

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
            id=JOB_ID_POPULARITY_SNAPSHOTS,
            trigger_seconds=settings.scheduler_popularity_snapshots_interval_seconds,
            action=_build_placeholder_job_action(JOB_ID_POPULARITY_SNAPSHOTS),
            enabled=settings.scheduler_popularity_snapshots_enabled,
            description="Capture popularity snapshots.",
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
            action=_build_placeholder_job_action(JOB_ID_SEARCH_INDEX_SYNC),
            enabled=settings.scheduler_search_index_sync_enabled,
            description="Sync the search index.",
        ),
        SchedulerJobDefinition(
            id=JOB_ID_SEO_BACKLOG_BATCHES,
            trigger_seconds=settings.scheduler_seo_backlog_batches_interval_seconds,
            action=_build_placeholder_job_action(JOB_ID_SEO_BACKLOG_BATCHES),
            enabled=settings.scheduler_seo_backlog_batches_enabled,
            description="Process SEO backlog batches.",
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


__all__ = [
    "JOB_ID_MATERIALIZED_VIEW_REFRESH",
    "JOB_ID_MOTD",
    "JOB_ID_POPULARITY_SNAPSHOTS",
    "JOB_ID_SEARCH_INDEX_SYNC",
    "JOB_ID_SEO_BACKLOG_BATCHES",
    "SchedulerJobDefinition",
    "build_scheduler_job_definitions",
    "enabled_scheduler_jobs",
    "run_logged_job",
]
