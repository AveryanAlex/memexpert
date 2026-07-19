"""Backfill durable Telegram post text and relationship metadata."""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

from memexpert.core.config import get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.crawlers.telegram.metadata_backfill import TelegramPostMetadataBackfiller

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memexpert-backfill-telegram-post-metadata")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report metadata without updating source post rows",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Persist fetched metadata and deletion observations",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="ID_OR_USERNAME",
        help="Restrict to a Telegram channel id, username, or database UUID; repeat as needed",
    )
    parser.add_argument(
        "--batch-size",
        type=_bounded_batch_size,
        default=100,
        metavar="1..100",
        help="Telegram messages fetched and committed per batch (default: 100)",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backfiller = TelegramPostMetadataBackfiller(
        get_async_session_factory(),
        settings=get_settings(),
    )
    result = await backfiller.run(
        dry_run=args.dry_run,
        channel_filters=tuple(args.channel),
        batch_size=args.batch_size,
    )
    mode = "dry-run" if result.dry_run else "apply"
    print(
        "Telegram post metadata backfill complete; "
        f"mode={mode} channels={result.channels_inspected} batches={result.batches_processed} "
        f"candidates={result.candidates_inspected} captured={result.captured} missing={result.missing} "
        f"transient_failures={result.transient_failures} permanent_failures={result.permanent_failures} "
        f"stale={result.stale_candidates} unassigned_channels={result.unassigned_channels} "
        f"sessions_parked={result.sessions_parked} "
        f"sessions_quarantined={result.sessions_quarantined} sessions_skipped={result.sessions_skipped} "
        f"sessions_requiring_attention={result.sessions_requiring_attention}"
    )
    if result.operator_attention_required:
        return 2
    return 1 if result.retry_required else 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


def _bounded_batch_size(value: str) -> int:
    try:
        batch_size = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be an integer from 1 through 100") from exc
    if not 1 <= batch_size <= 100:
        raise argparse.ArgumentTypeError("batch size must be from 1 through 100")
    return batch_size


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
