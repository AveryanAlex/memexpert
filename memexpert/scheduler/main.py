"""Console entry point for the scheduler runtime."""

from __future__ import annotations

import asyncio

from memexpert.scheduler.runtime import run_scheduler_runtime


def main() -> None:
    """Run the scheduler runtime."""

    asyncio.run(run_scheduler_runtime())


__all__ = ["main"]
