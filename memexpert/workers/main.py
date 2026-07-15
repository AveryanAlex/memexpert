"""Console entry point for the background workers process."""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

from memexpert.workers.pipeline_runtime import run_pipeline_runtime
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> None:
    """Run the RabbitMQ-backed content-pipeline worker runtime."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        type=WorkerRole,
        choices=tuple(WorkerRole),
        default=WorkerRole.ALL,
        help="consumer/dependency role to run (default: all)",
    )
    args = parser.parse_args(argv)
    asyncio.run(run_pipeline_runtime(role=args.role))


__all__ = ["main"]


if __name__ == "__main__":
    main()
