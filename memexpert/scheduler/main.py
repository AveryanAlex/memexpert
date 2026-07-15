"""Console entry point for the scheduler runtime."""

from __future__ import annotations

import asyncio

from memexpert.core.config import get_settings
from memexpert.runtime_health import RuntimeHealthReporter
from memexpert.scheduler.runtime import run_scheduler_runtime


def main() -> None:
    """Run the scheduler runtime."""

    settings = get_settings()
    health_reporter = RuntimeHealthReporter.from_settings(settings, service="memexpert-scheduler")
    asyncio.run(run_scheduler_runtime(settings=settings, health_reporter=health_reporter))


__all__ = ["main"]
