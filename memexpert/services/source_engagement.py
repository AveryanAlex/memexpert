"""Reusable primitives for source engagement snapshots and scheduling."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from memexpert.models.base import utcnow
from memexpert.models.content import MemeSource, MemeSourceEngagementSnapshot
from memexpert.models.enums import (
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_EARLY_SCHEDULE_OFFSETS: tuple[tuple[SourceEngagementScheduleLabel, timedelta], ...] = (
    (SourceEngagementScheduleLabel.PLUS_1H, timedelta(hours=1)),
    (SourceEngagementScheduleLabel.PLUS_3H, timedelta(hours=3)),
    (SourceEngagementScheduleLabel.PLUS_12H, timedelta(hours=12)),
    (SourceEngagementScheduleLabel.PLUS_1D, timedelta(days=1)),
    (SourceEngagementScheduleLabel.PLUS_3D, timedelta(days=3)),
    (SourceEngagementScheduleLabel.PLUS_7D, timedelta(days=7)),
)


@dataclass(frozen=True, slots=True)
class SourceEngagementMetrics:
    """Normalized source engagement metrics returned by an upstream fetch."""

    view_count: int | None = None
    reactions: dict[str, int] | None = None
    reaction_count: int | None = None
    comment_count: int | None = None
    forward_count: int | None = None
    comments_state: SourceEngagementCommentsState = SourceEngagementCommentsState.UNKNOWN
    source_alive: bool = True
    raw_metrics: dict[str, object] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceEngagementScheduleSlot:
    """Next durable source engagement schedule slot for a published post."""

    label: SourceEngagementScheduleLabel
    scheduled_for: datetime


def reaction_count_from_reactions(reactions: dict[str, int] | None) -> int | None:
    """Return the total reaction count while preserving unknown-vs-known-zero."""

    if reactions is None:
        return None
    return sum(reactions.values())


def next_source_engagement_schedule_slot(
    published_at: datetime | None,
    *,
    now: datetime | None = None,
) -> SourceEngagementScheduleSlot | None:
    """Return the next source engagement schedule slot anchored to publish time."""

    if published_at is None:
        return None

    published_at_utc = _as_utc(published_at)
    now_utc = _as_utc(now or utcnow())

    for label, offset in _EARLY_SCHEDULE_OFFSETS:
        scheduled_for = published_at_utc + offset
        if scheduled_for > now_utc:
            return SourceEngagementScheduleSlot(label=label, scheduled_for=scheduled_for)

    first_monthly_slot = _add_months(published_at_utc, 1)
    if first_monthly_slot > now_utc:
        return SourceEngagementScheduleSlot(
            label=SourceEngagementScheduleLabel.PLUS_1MONTH,
            scheduled_for=first_monthly_slot,
        )

    month_count = max(1, _calendar_month_delta(published_at_utc, now_utc))
    monthly_slot = _add_months(published_at_utc, month_count)
    while monthly_slot <= now_utc:
        month_count += 1
        monthly_slot = _add_months(published_at_utc, month_count)

    return SourceEngagementScheduleSlot(
        label=SourceEngagementScheduleLabel.MONTHLY,
        scheduled_for=monthly_slot,
    )


def source_engagement_schedule_label_for(
    published_at: datetime | None,
    scheduled_for: datetime | None,
) -> SourceEngagementScheduleLabel | None:
    """Return the canonical label for a persisted engagement schedule slot."""

    if published_at is None or scheduled_for is None:
        return None

    published_at_utc = _as_utc(published_at)
    scheduled_for_utc = _as_utc(scheduled_for)

    for label, offset in _EARLY_SCHEDULE_OFFSETS:
        if published_at_utc + offset == scheduled_for_utc:
            return label

    first_monthly_slot = _add_months(published_at_utc, 1)
    if first_monthly_slot == scheduled_for_utc:
        return SourceEngagementScheduleLabel.PLUS_1MONTH

    month_count = max(1, _calendar_month_delta(published_at_utc, scheduled_for_utc))
    if month_count <= 1:
        return None
    if _add_months(published_at_utc, month_count) == scheduled_for_utc:
        return SourceEngagementScheduleLabel.MONTHLY
    return None


async def add_source_engagement_snapshot(
    session: AsyncSession,
    source: MemeSource,
    metrics: SourceEngagementMetrics,
    *,
    capture_reason: SourceEngagementCaptureReason,
    fetch_status: SourceEngagementFetchStatus,
    captured_at: datetime | None = None,
    scheduled_for: datetime | None = None,
    schedule_label: SourceEngagementScheduleLabel | None = None,
    update_source_schedule: bool = True,
) -> MemeSourceEngagementSnapshot:
    """Build and add a source engagement snapshot, then flush without committing."""

    resolved_captured_at = _as_utc(captured_at or utcnow())
    resolved_scheduled_for = None if scheduled_for is None else _as_utc(scheduled_for)
    snapshot = MemeSourceEngagementSnapshot(
        source=source,
    )
    _apply_snapshot_metrics(
        snapshot,
        metrics,
        capture_reason=capture_reason,
        fetch_status=fetch_status,
        captured_at=resolved_captured_at,
        scheduled_for=resolved_scheduled_for,
        schedule_label=schedule_label,
    )
    session.add(snapshot)

    if update_source_schedule:
        update_source_after_engagement_capture(
            source,
            metrics,
            fetch_status=fetch_status,
            captured_at=resolved_captured_at,
            scheduled_for=resolved_scheduled_for,
        )

    await session.flush()
    return snapshot


async def update_source_engagement_snapshot(
    session: AsyncSession,
    snapshot: MemeSourceEngagementSnapshot,
    source: MemeSource,
    metrics: SourceEngagementMetrics,
    *,
    capture_reason: SourceEngagementCaptureReason,
    fetch_status: SourceEngagementFetchStatus,
    captured_at: datetime | None = None,
    scheduled_for: datetime | None = None,
    schedule_label: SourceEngagementScheduleLabel | None = None,
    update_source_schedule: bool = True,
) -> MemeSourceEngagementSnapshot:
    """Update an existing schedule-slot snapshot, then flush without committing."""

    resolved_captured_at = _as_utc(captured_at or utcnow())
    resolved_scheduled_for = None if scheduled_for is None else _as_utc(scheduled_for)
    snapshot.source = source
    _apply_snapshot_metrics(
        snapshot,
        metrics,
        capture_reason=capture_reason,
        fetch_status=fetch_status,
        captured_at=resolved_captured_at,
        scheduled_for=resolved_scheduled_for,
        schedule_label=schedule_label,
    )
    if update_source_schedule:
        update_source_after_engagement_capture(
            source,
            metrics,
            fetch_status=fetch_status,
            captured_at=resolved_captured_at,
            scheduled_for=resolved_scheduled_for,
        )

    await session.flush()
    return snapshot


def update_source_after_engagement_capture(
    source: MemeSource,
    metrics: SourceEngagementMetrics,
    *,
    fetch_status: SourceEngagementFetchStatus,
    captured_at: datetime,
    scheduled_for: datetime | None,
) -> None:
    """Apply source-level engagement schedule/lease state after a capture attempt."""

    source.source_alive = metrics.source_alive
    source.last_engagement_check_at = captured_at
    if fetch_status is SourceEngagementFetchStatus.FAILED:
        source.next_engagement_check_at = scheduled_for or source.next_engagement_check_at
    else:
        next_slot = next_source_engagement_schedule_slot(source.published_at, now=captured_at)
        source.next_engagement_check_at = None if next_slot is None else next_slot.scheduled_for
    source.engagement_check_locked_at = None
    source.engagement_check_lock_owner = None
    source.last_engagement_error_code = (
        None if fetch_status is SourceEngagementFetchStatus.SUCCESS else metrics.error_code
    )


async def add_initial_source_engagement_snapshot(
    session: AsyncSession,
    source: MemeSource,
    metrics: SourceEngagementMetrics,
    *,
    captured_at: datetime | None = None,
) -> MemeSourceEngagementSnapshot:
    """Add the canonical initial engagement snapshot for a newly attached source."""

    return await add_source_engagement_snapshot(
        session,
        source,
        metrics,
        capture_reason=SourceEngagementCaptureReason.INGEST_INITIAL,
        fetch_status=SourceEngagementFetchStatus.SUCCESS,
        captured_at=captured_at,
        schedule_label=SourceEngagementScheduleLabel.INGEST_INITIAL,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _add_months(value: datetime, month_count: int) -> datetime:
    month_index = value.month - 1 + month_count
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _calendar_month_delta(start: datetime, end: datetime) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def _apply_snapshot_metrics(
    snapshot: MemeSourceEngagementSnapshot,
    metrics: SourceEngagementMetrics,
    *,
    capture_reason: SourceEngagementCaptureReason,
    fetch_status: SourceEngagementFetchStatus,
    captured_at: datetime,
    scheduled_for: datetime | None,
    schedule_label: SourceEngagementScheduleLabel | None,
) -> None:
    reaction_count = metrics.reaction_count
    if reaction_count is None:
        reaction_count = reaction_count_from_reactions(metrics.reactions)

    snapshot.captured_at = captured_at
    snapshot.scheduled_for = scheduled_for
    snapshot.capture_reason = capture_reason
    snapshot.schedule_label = schedule_label
    snapshot.view_count = _non_negative_or_none("view_count", metrics.view_count)
    snapshot.reactions = metrics.reactions
    snapshot.reaction_count = _non_negative_or_none("reaction_count", reaction_count)
    snapshot.comment_count = _non_negative_or_none("comment_count", metrics.comment_count)
    snapshot.forward_count = _non_negative_or_none("forward_count", metrics.forward_count)
    snapshot.comments_state = metrics.comments_state
    snapshot.fetch_status = fetch_status
    snapshot.source_alive = metrics.source_alive
    snapshot.error_code = metrics.error_code
    snapshot.raw_metrics = metrics.raw_metrics


def _non_negative_or_none(name: str, value: int | None) -> int | None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative when provided")
    return value


__all__ = [
    "SourceEngagementMetrics",
    "SourceEngagementScheduleSlot",
    "add_initial_source_engagement_snapshot",
    "add_source_engagement_snapshot",
    "next_source_engagement_schedule_slot",
    "reaction_count_from_reactions",
    "source_engagement_schedule_label_for",
    "update_source_after_engagement_capture",
    "update_source_engagement_snapshot",
]
