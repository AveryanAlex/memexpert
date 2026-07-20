"""Run a bounded, read-only chronological recommendation evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import suppress
from typing import TYPE_CHECKING

from sqlalchemy import text

from memexpert.core.config import get_settings
from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.services.recommendations.offline_evaluator import (
    DEFAULT_K,
    DEFAULT_MAX_CASES,
    DEFAULT_MAX_CATALOG,
    DEFAULT_MAX_USERS,
    HARD_MAX_CASES,
    HARD_MAX_CATALOG,
    HARD_MAX_K,
    HARD_MAX_USERS,
    OfflineEvaluationBounds,
    OfflineEvaluationReport,
    evaluate_postgres_recommendations,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

EVALUATION_TIMEOUT_SECONDS = 600


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memexpert-recommendation-evaluator",
        description=(
            "Compare local recommendation representations over chronological held-out "
            "strong positives. The command is aggregate-only and forces a read-only transaction."
        ),
    )
    parser.add_argument(
        "--max-users",
        type=_bounded_int("max-users", HARD_MAX_USERS),
        default=DEFAULT_MAX_USERS,
        help=f"maximum eligible users to read (default {DEFAULT_MAX_USERS}, hard max {HARD_MAX_USERS})",
    )
    parser.add_argument(
        "--max-catalog",
        type=_bounded_int("max-catalog", HARD_MAX_CATALOG),
        default=DEFAULT_MAX_CATALOG,
        help=(
            f"maximum public embedded catalog items to score "
            f"(default {DEFAULT_MAX_CATALOG}, hard max {HARD_MAX_CATALOG})"
        ),
    )
    parser.add_argument(
        "--max-cases",
        type=_bounded_int("max-cases", HARD_MAX_CASES),
        default=DEFAULT_MAX_CASES,
        help=f"maximum chronological holdout cases (default {DEFAULT_MAX_CASES}, hard max {HARD_MAX_CASES})",
    )
    parser.add_argument(
        "--k",
        type=_bounded_int("k", HARD_MAX_K),
        default=DEFAULT_K,
        help=f"ranking cutoff K (default {DEFAULT_K}, hard max {HARD_MAX_K})",
    )
    parser.add_argument("--pretty", action="store_true", help="indent the aggregate JSON report")
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bounds = OfflineEvaluationBounds(
            max_users=args.max_users,
            max_catalog=args.max_catalog,
            max_cases=args.max_cases,
            k=args.k,
        )
    except ValueError as exc:
        print(f"invalid evaluation bounds: {exc}", file=sys.stderr)
        return 2

    engine: AsyncEngine | None = None
    report: OfflineEvaluationReport | None = None
    try:
        settings = get_settings()
        engine = build_async_engine(
            settings=settings,
            application_name="memexpert-recommendation-evaluator",
        )
        session_factory = build_async_session_factory(engine)
        async with session_factory() as session:
            try:
                # This is the first statement in the transaction. PostgreSQL
                # therefore enforces read-only semantics for every loader query.
                await session.execute(text("SET TRANSACTION READ ONLY"))
                async with asyncio.timeout(EVALUATION_TIMEOUT_SECONDS):
                    report = await evaluate_postgres_recommendations(
                        session,
                        settings=settings,
                        bounds=bounds,
                    )
            finally:
                with suppress(Exception):
                    await session.rollback()
    except Exception as exc:
        # Do not print URLs, SQL parameters, user identifiers, or provider
        # details from exception messages. The exception class is enough for
        # an operator to correlate with protected local logs.
        print(f"recommendation evaluation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            with suppress(Exception):
                await engine.dispose()

    if report is None:  # pragma: no cover - defensive narrowing
        return 1

    print(
        json.dumps(
            report.to_dict(),
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


def _bounded_int(name: str, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if parsed < 1 or parsed > maximum:
            raise argparse.ArgumentTypeError(f"{name} must be between 1 and {maximum}")
        return parsed

    return parse


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
