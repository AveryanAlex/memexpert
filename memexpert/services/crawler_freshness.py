# ruff: noqa: TC003
"""Freshness aggregation helper for the T03 crawler operator surface.

The helper computes a :class:`CrawlerFreshnessSnapshot` from real
``meme_sources`` + ``pipeline_stage_journal`` rows. It is kept in its
own module (instead of adding to
``memexpert.services.content_pipeline_reporting``) because the reporting
file already carries the S02/S03 inspect-detail + run-summary helpers
and the freshness math is conceptually separate from the sync-truth
reporting path.

The aggregation path prefers ORM readability over SQL cleverness: we
pull at most ``_HARD_ROW_CAP`` MemeSource rows ordered by
``published_at DESC``, then group + trim + percentile-compute in pure
Python. This keeps the SQL trivially reviewable while still giving
operators a correct snapshot for the channels T04's freshness harness
will exercise. If operators later need an order-of-magnitude larger
sample we can promote the grouping into a window-function SQL path
without changing the caller contract.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.base import utcnow
from memexpert.models.content import (
    MemeSource,
    PipelineStageJournal,
    SourceChannel,
)
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    SourcePlatform,
)
from memexpert.schemas.crawler import (
    MAX_FRESHNESS_SAMPLE_ITEMS,
    CrawlerFreshnessChannelBreakdown,
    CrawlerFreshnessSampleItem,
    CrawlerFreshnessSnapshot,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

# Conservative hard cap on how many ``MemeSource`` rows the helper scans
# in a single call. The operator surface is an inspect endpoint, not a
# batch analytics query, so 2000 rows is more than enough to cover any
# realistic bounded freshness window while keeping the response time
# deterministic.
_HARD_ROW_CAP = 2000

# Stages whose ``finished_at`` timestamps determine the "both synced"
# moment for one meme file. Both stages must be present and succeeded
# for the file to count toward the freshness percentiles.
_SYNC_STAGES: tuple[ContentPipelineStage, ...] = (
    ContentPipelineStage.SYNC_QDRANT,
    ContentPipelineStage.SYNC_MEILI,
)


@dataclass(slots=True)
class _ChannelBucket:
    """In-memory scratch state while grouping sample items by channel.

    ``items`` accumulates every raw sample (including samples whose
    freshness is still ``None``) so the per-channel ``item_count`` counts
    honestly — the freshness caller's SLO check should see the full
    population, not just the items that have already finished syncing.
    """

    source_channel_id: uuid.UUID
    platform_id: str
    channel_title: str
    items: list[CrawlerFreshnessSampleItem]


async def build_crawler_freshness_snapshot(
    session: AsyncSession,
    *,
    since: datetime | None,
    limit_per_channel: int,
    slo_p50_seconds: float,
    slo_p95_seconds: float,
) -> CrawlerFreshnessSnapshot:
    """Return a freshness snapshot aggregated from durable pipeline truth.

    The per-channel limit is applied AFTER loading a hard-capped set of
    rows from the DB: we take the most recent ``_HARD_ROW_CAP`` sources,
    group them by ``(platform, source_id)``, then keep the first
    ``limit_per_channel`` samples per channel (sorted by
    ``published_at DESC``). The global ``sample_items`` list is the
    top-50 items by ``published_at``.
    """

    evaluated_at = utcnow()

    rows = await _load_candidate_rows(session, since=since)
    buckets = _bucket_rows_by_channel(rows, limit_per_channel=limit_per_channel)
    per_channel_breakdown = tuple(
        _build_channel_breakdown(bucket) for bucket in buckets.values()
    )

    global_items = _select_global_sample_items(buckets)
    freshness_values = [
        item.freshness_seconds for item in global_items if item.freshness_seconds is not None
    ]

    p50 = _percentile(sorted(freshness_values), 0.5) if freshness_values else None
    p95 = _percentile(sorted(freshness_values), 0.95) if freshness_values else None

    return CrawlerFreshnessSnapshot(
        snapshot_evaluated_at=evaluated_at,
        since=since,
        item_count=sum(len(bucket.items) for bucket in buckets.values()),
        p50_seconds=p50,
        p95_seconds=p95,
        slo_p50_seconds=slo_p50_seconds,
        slo_p95_seconds=slo_p95_seconds,
        slo_p50_pass=p50 is None or p50 < slo_p50_seconds,
        slo_p95_pass=p95 is None or p95 < slo_p95_seconds,
        per_channel=per_channel_breakdown,
        sample_items=global_items,
    )


async def _load_candidate_rows(
    session: AsyncSession,
    *,
    since: datetime | None,
) -> list[_CandidateRow]:
    """Return the joined ``(MemeSource, SourceChannel, sync finishes)`` rows.

    The function resolves the "both sync stages finished" timestamp by
    looking up the sync stage-journal rows per meme file; items whose
    sync chain has not yet finished are still returned so the
    per-channel ``item_count`` reflects the full honest population.
    """

    source_stmt = (
        select(MemeSource)
        .where(
            MemeSource.platform == SourcePlatform.TELEGRAM,
            MemeSource.published_at.is_not(None),
        )
        .order_by(MemeSource.published_at.desc())
        .limit(_HARD_ROW_CAP)
    )
    if since is not None:
        source_stmt = source_stmt.where(MemeSource.published_at >= since)

    source_rows = list((await session.execute(source_stmt)).scalars().all())
    if not source_rows:
        return []

    source_ids = {row.source_id for row in source_rows}
    channels_by_id = await _load_channels_by_platform_id(session, source_ids)
    sync_finishes = await _load_sync_finishes(
        session,
        meme_file_ids=[row.file_id for row in source_rows],
    )

    candidates: list[_CandidateRow] = []
    for source_row in source_rows:
        channel = channels_by_id.get(source_row.source_id)
        if channel is None:
            # Freshness reports must correspond to curated channels: a
            # MemeSource without a matching SourceChannel row is either
            # a legacy upload or a configuration drift and should not
            # contribute to the SLO numbers.
            continue
        # The SQL query filters ``published_at IS NOT NULL`` up front,
        # but the ORM column is declared ``Mapped[datetime | None]`` so
        # mypy needs an explicit narrowing to know the value is present.
        published_at = source_row.published_at
        if published_at is None:
            continue
        finishes = sync_finishes.get(source_row.file_id, {})
        both_synced_at = _compute_both_synced_at(finishes)
        candidates.append(
            _CandidateRow(
                meme_file_id=source_row.file_id,
                source_channel_id=channel.id,
                channel_platform_id=channel.platform_id,
                channel_title=channel.title,
                published_at=published_at,
                first_ingested_at=source_row.created_at,
                both_synced_at=both_synced_at,
            )
        )
    return candidates


async def _load_channels_by_platform_id(
    session: AsyncSession,
    platform_ids: set[str],
) -> dict[str, SourceChannel]:
    """Return the Telegram source channels matching any of ``platform_ids``."""

    if not platform_ids:
        return {}
    stmt = select(SourceChannel).where(
        SourceChannel.platform == SourcePlatform.TELEGRAM,
        SourceChannel.platform_id.in_(platform_ids),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {row.platform_id: row for row in rows}


async def _load_sync_finishes(
    session: AsyncSession,
    *,
    meme_file_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[ContentPipelineStage, datetime]]:
    """Return the finished_at timestamps for every succeeded sync stage row."""

    if not meme_file_ids:
        return {}
    stmt = select(PipelineStageJournal).where(
        PipelineStageJournal.meme_file_id.in_(meme_file_ids),
        PipelineStageJournal.stage.in_(_SYNC_STAGES),
        PipelineStageJournal.status == ContentPipelineStageStatus.SUCCEEDED,
    )
    result = (await session.execute(stmt)).scalars().all()
    by_file: dict[uuid.UUID, dict[ContentPipelineStage, datetime]] = defaultdict(dict)
    for entry in result:
        if entry.finished_at is None:
            continue
        by_file[entry.meme_file_id][entry.stage] = entry.finished_at
    return by_file


def _compute_both_synced_at(
    finishes: dict[ContentPipelineStage, datetime],
) -> datetime | None:
    """Return the max finish timestamp when BOTH sync stages have finished."""

    qdrant_at = finishes.get(ContentPipelineStage.SYNC_QDRANT)
    meili_at = finishes.get(ContentPipelineStage.SYNC_MEILI)
    if qdrant_at is None or meili_at is None:
        return None
    return max(qdrant_at, meili_at)


@dataclass(slots=True, frozen=True)
class _CandidateRow:
    """Flat in-memory representation of one freshness-candidate row."""

    meme_file_id: uuid.UUID
    source_channel_id: uuid.UUID
    channel_platform_id: str
    channel_title: str
    published_at: datetime
    first_ingested_at: datetime
    both_synced_at: datetime | None


def _bucket_rows_by_channel(
    rows: list[_CandidateRow],
    *,
    limit_per_channel: int,
) -> dict[uuid.UUID, _ChannelBucket]:
    """Group candidate rows by source channel, keeping at most ``limit_per_channel`` items per group."""

    buckets: dict[uuid.UUID, _ChannelBucket] = {}
    for row in rows:
        bucket = buckets.get(row.source_channel_id)
        if bucket is None:
            bucket = _ChannelBucket(
                source_channel_id=row.source_channel_id,
                platform_id=row.channel_platform_id,
                channel_title=row.channel_title,
                items=[],
            )
            buckets[row.source_channel_id] = bucket
        if len(bucket.items) >= limit_per_channel:
            continue
        bucket.items.append(_candidate_to_sample(row))
    return buckets


def _candidate_to_sample(row: _CandidateRow) -> CrawlerFreshnessSampleItem:
    """Turn one candidate row into the public sample projection."""

    freshness_seconds = (
        (row.both_synced_at - row.published_at).total_seconds()
        if row.both_synced_at is not None
        else None
    )
    return CrawlerFreshnessSampleItem(
        meme_file_id=row.meme_file_id,
        source_channel_id=row.source_channel_id,
        published_at=row.published_at,
        first_ingested_at=row.first_ingested_at,
        both_synced_at=row.both_synced_at,
        freshness_seconds=freshness_seconds,
    )


def _build_channel_breakdown(bucket: _ChannelBucket) -> CrawlerFreshnessChannelBreakdown:
    """Reduce one bucket into the per-channel freshness projection."""

    freshness_values = [
        item.freshness_seconds for item in bucket.items if item.freshness_seconds is not None
    ]
    sorted_freshness = sorted(freshness_values)
    p50 = _percentile(sorted_freshness, 0.5) if sorted_freshness else None
    p95 = _percentile(sorted_freshness, 0.95) if sorted_freshness else None

    items_with_both_synced = [item for item in bucket.items if item.both_synced_at is not None]
    most_recent_item = (
        max(items_with_both_synced, key=lambda item: item.published_at or _MIN_DATETIME)
        if items_with_both_synced
        else None
    )

    return CrawlerFreshnessChannelBreakdown(
        source_channel_id=bucket.source_channel_id,
        platform_id=bucket.platform_id,
        channel_title=bucket.channel_title,
        item_count=len(bucket.items),
        p50_seconds=p50,
        p95_seconds=p95,
        most_recent_item_at=most_recent_item.published_at if most_recent_item is not None else None,
        most_recent_freshness_seconds=(
            most_recent_item.freshness_seconds if most_recent_item is not None else None
        ),
    )


def _select_global_sample_items(
    buckets: dict[uuid.UUID, _ChannelBucket],
) -> tuple[CrawlerFreshnessSampleItem, ...]:
    """Return the bounded global sample sorted by ``published_at DESC``."""

    merged: list[CrawlerFreshnessSampleItem] = []
    for bucket in buckets.values():
        merged.extend(bucket.items)
    merged.sort(key=lambda item: item.published_at or _MIN_DATETIME, reverse=True)
    return tuple(merged[:MAX_FRESHNESS_SAMPLE_ITEMS])


# Python cannot compare ``None`` to a ``datetime``, so fall back to a
# UTC-aware sentinel that always sorts last when ``published_at`` is
# missing. The candidate query filters on ``published_at IS NOT NULL``
# today, but keeping this sentinel defends the Python-side fallback path.
_MIN_DATETIME = datetime.min.replace(tzinfo=UTC)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile matching numpy's ``linear`` method.

    Duplicated (rather than imported) from
    :mod:`memexpert.services.content_pipeline_reporting` so the crawler
    freshness surface does not reach into another module's private
    helper. The math is tiny and intentionally held in sync by the
    shared doc wording above.
    """

    if not sorted_values:
        raise ValueError("_percentile requires at least one value.")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    lower_index = int(math.floor(rank))
    upper_index = int(math.ceil(rank))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    return lower_value + (upper_value - lower_value) * (rank - lower_index)


__all__ = [
    "build_crawler_freshness_snapshot",
]
