"""Scheduler runtime."""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import build_async_engine
from memexpert.scheduler.jobs import SchedulerJobDefinition, enabled_scheduler_jobs, run_logged_job
from memexpert.scheduler.locking import PostgresAdvisorySchedulerLock, SchedulerInstanceLockError
from memexpert.scheduler.logging import configure_scheduler_logging

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


class SchedulerLike(Protocol):
    """Small scheduler seam used by the runtime and tests."""

    def add_job(self, func: object, **kwargs: object) -> object: ...

    def start(self) -> None: ...

    def shutdown(self, *, wait: bool) -> None: ...


async def run_scheduler_runtime(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    scheduler: SchedulerLike | None = None,
    stop_waiter: Callable[[], Awaitable[None] | None] | None = None,
    lock: Any | None = None,
) -> None:
    """Run the scheduler until stopped."""

    resolved_settings = settings or get_settings()
    configure_scheduler_logging()

    owns_engine = engine is None
    resolved_engine = engine or build_async_engine()
    scheduler_instance = scheduler or AsyncIOScheduler()
    acquired_lock = lock
    connection = None
    connection_cm = None

    jobs = enabled_scheduler_jobs(resolved_settings, resolved_engine)

    started = False
    try:
        if resolved_settings.scheduler_advisory_lock_enabled:
            if acquired_lock is None:
                connection_cm = resolved_engine.connect()
                if hasattr(connection_cm, "__aenter__"):
                    connection = await connection_cm.__aenter__()
                else:
                    connection = connection_cm
                acquired_lock = PostgresAdvisorySchedulerLock(connection, resolved_settings.scheduler_advisory_lock_key)
            try:
                await acquired_lock.acquire()
            except SchedulerInstanceLockError:
                logger.error(
                    "scheduler_instance_lock_unavailable",
                    extra={
                        "event": "scheduler_instance_lock_unavailable",
                        "advisory_lock_key": resolved_settings.scheduler_advisory_lock_key,
                    },
                )
                raise
        else:
            logger.warning("scheduler_advisory_lock_disabled", extra={"event": "scheduler_advisory_lock_disabled"})

        for definition in jobs:
            async def _job_runner(job_definition: SchedulerJobDefinition = definition) -> None:
                await run_logged_job(job_definition.id, job_definition.action)

            scheduler_instance.add_job(
                _job_runner,
                trigger=IntervalTrigger(seconds=cast("Any", definition.trigger_seconds)),
                id=definition.id,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )

        logger.info(
            "scheduler_runtime_started",
            extra={
                "event": "scheduler_runtime_started",
                "jobs_registered": len(jobs),
                "advisory_lock_enabled": resolved_settings.scheduler_advisory_lock_enabled,
            },
        )

        scheduler_instance.start()
        started = True
        await _wait_for_stop(stop_waiter)
    finally:
        if started:
            scheduler_instance.shutdown(wait=True)

        if acquired_lock is not None:
            await acquired_lock.release()

        if connection_cm is not None:
            if hasattr(connection_cm, "__aexit__"):
                await connection_cm.__aexit__(None, None, None)
            elif connection is not None and hasattr(connection, "close"):
                await connection.close()
        elif connection is not None and hasattr(connection, "close"):
            await connection.close()

        if owns_engine:
            await resolved_engine.dispose()

        logger.info(
            "scheduler_runtime_stopped",
            extra={
                "event": "scheduler_runtime_stopped",
                "jobs_registered": len(jobs),
                "advisory_lock_enabled": resolved_settings.scheduler_advisory_lock_enabled,
            },
        )


async def _wait_for_stop(stop_waiter: Callable[[], Awaitable[None] | None] | None) -> None:
    if stop_waiter is not None:
        result = stop_waiter()
        if inspect.isawaitable(result):
            await result
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("scheduler_stop_requested", extra={"event": "scheduler_stop_requested"})
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _request_stop)

    try:
        await stop_event.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)


__all__ = ["run_scheduler_runtime"]
