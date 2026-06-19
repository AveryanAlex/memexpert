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
    reaction_count = metrics.reaction_count
    if reaction_count is None:
        reaction_count = reaction_count_from_reactions(metrics.reactions)

    snapshot = MemeSourceEngagementSnapshot(
        source=source,
        captured_at=resolved_captured_at,
        scheduled_for=resolved_scheduled_for,
        capture_reason=capture_reason,
        schedule_label=schedule_label,
        view_count=_non_negative_or_none("view_count", metrics.view_count),
        reactions=metrics.reactions,
        reaction_count=_non_negative_or_none("reaction_count", reaction_count),
        comment_count=_non_negative_or_none("comment_count", metrics.comment_count),
        forward_count=_non_negative_or_none("forward_count", metrics.forward_count),
        comments_state=metrics.comments_state,
        fetch_status=fetch_status,
        source_alive=metrics.source_alive,
        error_code=metrics.error_code,
        raw_metrics=metrics.raw_metrics,
    )
    session.add(snapshot)

    if update_source_schedule:
        source.last_engagement_check_at = resolved_captured_at
        next_slot = next_source_engagement_schedule_slot(source.published_at, now=resolved_captured_at)
        source.next_engagement_check_at = None if next_slot is None else next_slot.scheduled_for
        source.engagement_check_locked_at = None
        source.engagement_check_lock_owner = None
        source.last_engagement_error_code = (
            None if fetch_status is SourceEngagementFetchStatus.SUCCESS else metrics.error_code
        )

    await session.flush()
    return snapshot


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


def _non_negative_or_none(name: str, value: int | None) -> int | None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative when provided")
    return value


__all__ = [
    "SourceEngagementMetrics",
    "SourceEngagementScheduleSlot",
    "add_source_engagement_snapshot",
    "next_source_engagement_schedule_slot",
    "reaction_count_from_reactions",
]
