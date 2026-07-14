"""Backfill and verify deterministic preview images for stored web videos."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.core.config import get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.core.storage import get_s3_client
from memexpert.media.inspect import PipelineMediaProcessor
from memexpert.models.content import MemeFile
from memexpert.workers.video_poster_backfill import (
    VideoPosterBackfiller,
    VideoPosterBackfillStatus,
    VideoPosterCandidate,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence


@dataclass(frozen=True, slots=True)
class _CandidateResult:
    candidate: VideoPosterCandidate
    status: VideoPosterBackfillStatus | None = None
    exists: bool | None = None
    error: Exception | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memexpert-backfill-video-posters")
    parser.add_argument("--limit", type=int, default=None, help="Maximum web-video files to inspect")
    parser.add_argument("--concurrency", type=int, default=1, help="Maximum simultaneous storage/FFmpeg jobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="Exit non-zero if any preview image is missing")
    mode.add_argument("--force", action="store_true", help="Regenerate preview images that already exist")
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    if args.concurrency < 1:
        print("--concurrency must be at least 1", file=sys.stderr)
        return 2

    candidates = await _load_candidates(limit=args.limit)
    settings = get_settings()
    backfiller = VideoPosterBackfiller(
        storage_client=get_s3_client(),
        media_processor=PipelineMediaProcessor(settings=settings),
        settings=settings,
    )

    if args.verify_only:
        results = await _map_candidates(
            candidates,
            concurrency=args.concurrency,
            operation=lambda candidate: _verify_candidate(backfiller, candidate),
        )
        return _report_verification(results)

    results = await _map_candidates(
        candidates,
        concurrency=args.concurrency,
        operation=lambda candidate: _backfill_candidate(backfiller, candidate, force=args.force),
    )
    return _report_backfill(results)


async def _load_candidates(*, limit: int | None) -> tuple[VideoPosterCandidate, ...]:
    stmt = (
        select(MemeFile.id, MemeFile.s3_web_video_key)
        .where(MemeFile.s3_web_video_key.is_not(None))
        .order_by(MemeFile.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
    return tuple(
        VideoPosterCandidate(meme_file_id=meme_file_id, web_video_object_key=web_video_object_key)
        for meme_file_id, web_video_object_key in rows
        if web_video_object_key is not None
    )


async def _map_candidates(
    candidates: tuple[VideoPosterCandidate, ...],
    *,
    concurrency: int,
    operation: Callable[[VideoPosterCandidate], Awaitable[_CandidateResult]],
) -> tuple[_CandidateResult, ...]:
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(candidate: VideoPosterCandidate) -> _CandidateResult:
        async with semaphore:
            return await operation(candidate)

    return tuple(await asyncio.gather(*(run_one(candidate) for candidate in candidates)))


async def _verify_candidate(
    backfiller: VideoPosterBackfiller,
    candidate: VideoPosterCandidate,
) -> _CandidateResult:
    try:
        return _CandidateResult(candidate=candidate, exists=await backfiller.preview_image_exists(candidate))
    except Exception as exc:  # noqa: BLE001 - batch repair must report every failed item.
        return _CandidateResult(candidate=candidate, error=exc)


async def _backfill_candidate(
    backfiller: VideoPosterBackfiller,
    candidate: VideoPosterCandidate,
    *,
    force: bool,
) -> _CandidateResult:
    try:
        status = await backfiller.ensure_preview_image(candidate, overwrite=force)
        return _CandidateResult(candidate=candidate, status=status)
    except Exception as exc:  # noqa: BLE001 - batch repair must continue and report every failed item.
        return _CandidateResult(candidate=candidate, error=exc)


def _report_verification(results: tuple[_CandidateResult, ...]) -> int:
    missing = [result for result in results if result.exists is False]
    failed = [result for result in results if result.error is not None]
    for result in missing:
        print(f"missing meme_file_id={result.candidate.meme_file_id}", file=sys.stderr)
    for result in failed:
        print(
            f"failed meme_file_id={result.candidate.meme_file_id}: {result.error}",
            file=sys.stderr,
        )
    present = len(results) - len(missing) - len(failed)
    print(
        f"verification complete; inspected={len(results)} present={present} "
        f"missing={len(missing)} failed={len(failed)}"
    )
    return 1 if missing or failed else 0


def _report_backfill(results: tuple[_CandidateResult, ...]) -> int:
    created = [result for result in results if result.status is VideoPosterBackfillStatus.CREATED]
    present = [result for result in results if result.status is VideoPosterBackfillStatus.PRESENT]
    failed = [result for result in results if result.error is not None]
    for result in created:
        print(f"created meme_file_id={result.candidate.meme_file_id}")
    for result in failed:
        print(
            f"failed meme_file_id={result.candidate.meme_file_id}: {result.error}",
            file=sys.stderr,
        )
    print(
        f"backfill complete; inspected={len(results)} created={len(created)} "
        f"present={len(present)} failed={len(failed)}"
    )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
