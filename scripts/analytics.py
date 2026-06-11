"""Operational analytics commands."""

from __future__ import annotations

import argparse
import asyncio

from memexpert.core.database import build_async_engine
from memexpert.services.public_trends import refresh_public_trend_materialized_views


def main() -> None:
    """Run analytics maintenance commands."""

    parser = argparse.ArgumentParser(prog="memexpert-analytics")
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh_parser = subparsers.add_parser("refresh-trends", help="Refresh public trend materialized views")
    refresh_parser.add_argument(
        "--no-concurrently",
        action="store_true",
        help="Use plain REFRESH MATERIALIZED VIEW instead of trying CONCURRENTLY first",
    )
    args = parser.parse_args()

    if args.command == "refresh-trends":
        asyncio.run(_refresh_trends(concurrently=not args.no_concurrently))


async def _refresh_trends(*, concurrently: bool) -> None:
    engine = build_async_engine()
    try:
        await refresh_public_trend_materialized_views(engine, concurrently=concurrently)
    finally:
        await engine.dispose()


__all__ = ["main"]
