"""Console entry point for the background workers process."""

from __future__ import annotations

import asyncio

from memexpert.workers.pipeline_runtime import run_pipeline_runtime


def main() -> None:
    """Run the RabbitMQ-backed content-pipeline worker runtime."""

    asyncio.run(run_pipeline_runtime())


__all__ = ["main"]


if __name__ == "__main__":
    main()
