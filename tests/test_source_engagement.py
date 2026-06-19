"""Unit coverage for source engagement scheduling primitives."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memexpert.models.enums import SourceEngagementScheduleLabel
from memexpert.services.source_engagement import (
    next_source_engagement_schedule_slot,
    reaction_count_from_reactions,
    source_engagement_schedule_label_for,
)


def test_reaction_count_preserves_unknown_vs_known_zero() -> None:
    assert reaction_count_from_reactions(None) is None
    assert reaction_count_from_reactions({}) == 0
    assert reaction_count_from_reactions({"fire": 3, "heart": 2}) == 5


@pytest.mark.parametrize(
    ("now_offset", "expected_label", "expected_offset"),
    [
        (timedelta(minutes=30), SourceEngagementScheduleLabel.PLUS_1H, timedelta(hours=1)),
        (timedelta(hours=1), SourceEngagementScheduleLabel.PLUS_3H, timedelta(hours=3)),
        (timedelta(hours=3), SourceEngagementScheduleLabel.PLUS_12H, timedelta(hours=12)),
        (timedelta(hours=12), SourceEngagementScheduleLabel.PLUS_1D, timedelta(days=1)),
        (timedelta(days=1), SourceEngagementScheduleLabel.PLUS_3D, timedelta(days=3)),
        (timedelta(days=3), SourceEngagementScheduleLabel.PLUS_7D, timedelta(days=7)),
    ],
)
def test_next_source_engagement_schedule_slot_uses_published_at_for_fresh_posts(
    now_offset: timedelta,
    expected_label: SourceEngagementScheduleLabel,
    expected_offset: timedelta,
) -> None:
    published_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    slot = next_source_engagement_schedule_slot(
        published_at,
        now=published_at + now_offset,
    )

    assert slot is not None
    assert slot.label is expected_label
    assert slot.scheduled_for == published_at + expected_offset


def test_next_source_engagement_schedule_slot_returns_plus_one_month_after_early_slots() -> None:
    published_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    slot = next_source_engagement_schedule_slot(
        published_at,
        now=published_at + timedelta(days=8),
    )

    assert slot is not None
    assert slot.label is SourceEngagementScheduleLabel.PLUS_1MONTH
    assert slot.scheduled_for == datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def test_next_source_engagement_schedule_slot_returns_next_monthly_slot_for_old_posts() -> None:
    published_at = datetime(2025, 1, 15, 10, 30, tzinfo=UTC)
    now = datetime(2026, 6, 19, 10, 30, tzinfo=UTC)

    slot = next_source_engagement_schedule_slot(published_at, now=now)

    assert slot is not None
    assert slot.label is SourceEngagementScheduleLabel.MONTHLY
    assert slot.scheduled_for == datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
    assert slot.scheduled_for > now


def test_next_source_engagement_schedule_slot_handles_missing_published_at() -> None:
    assert next_source_engagement_schedule_slot(None, now=datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_source_engagement_schedule_label_for_persisted_monthly_slot() -> None:
    published_at = datetime(2025, 1, 15, 10, 30, tzinfo=UTC)
    scheduled_for = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)

    assert (
        source_engagement_schedule_label_for(published_at, scheduled_for)
        is SourceEngagementScheduleLabel.MONTHLY
    )
