"""Reconcile duplicate non-null MemeFile SHA-256 groups one transaction at a time."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from memexpert.core.database import get_async_session_factory
from memexpert.services.sha_reconciliation import ShaDuplicateReconciliationService

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memexpert-reconcile-sha-duplicates")
    parser.add_argument("--limit", type=int, default=None, help="Maximum SHA groups to reconcile in this run")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Exit non-zero if any duplicate non-null SHA group remains",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2

    session_factory = get_async_session_factory()
    processed = 0
    while args.limit is None or processed < args.limit:
        async with session_factory() as session:
            service = ShaDuplicateReconciliationService(session)
            if args.verify_only:
                duplicate_sha = await service.find_next_duplicate_sha()
                if duplicate_sha is None:
                    print("no duplicate non-null SHA groups remain")
                    return 0
                print(f"duplicate SHA group remains: {duplicate_sha}", file=sys.stderr)
                return 1

            result = await service.reconcile_next()
            if result is None:
                print(f"reconciliation complete; processed_groups={processed}")
                return 0
            await session.commit()
            processed += 1
            print(
                "reconciled "
                f"sha256={result.sha256_hex} canonical_meme_id={result.canonical_meme_id} "
                f"canonical_file_id={result.canonical_meme_file_id} "
                f"obsolete_files={len(result.obsolete_meme_file_ids)}"
            )

    print(f"reconciliation limit reached; processed_groups={processed}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
