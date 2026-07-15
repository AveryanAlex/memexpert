"""Focused unit coverage for admin analytics payload compatibility helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from memexpert.services.admin_analytics import (
    AdminAnalyticsDateRangeError,
    _analyse_search_queries,
    _count_interactions,
    _counter_delta,
    _counter_high_watermark,
    _EventRecord,
    _retention_cohort_members_by_date,
    build_admin_analytics_query_key,
    resolve_admin_analytics_date_range,
)


def test_date_range_uses_inclusive_utc_dates_and_a_same_length_prior_period() -> None:
    date_range = resolve_admin_analytics_date_range(
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 15),
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert date_range.day_count == 15
    assert date_range.comparison_start_date == date(2026, 6, 16)
    assert date_range.comparison_end_date == date(2026, 6, 30)


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        (date(2026, 7, 2), date(2026, 7, 1)),
        (date(2025, 7, 14), date(2026, 7, 15)),
        (date(2026, 7, 16), date(2026, 7, 16)),
    ],
)
def test_date_range_rejects_invalid_too_large_and_future_windows(start_date: date, end_date: date) -> None:
    with pytest.raises(AdminAnalyticsDateRangeError):
        resolve_admin_analytics_date_range(
            start_date=start_date,
            end_date=end_date,
            now=datetime(2026, 7, 15, tzinfo=UTC),
        )


def test_search_query_analysis_accepts_strict_and_legacy_payload_attribution() -> None:
    meme_id = uuid.uuid7()
    occurred_at = datetime(2026, 7, 15, 12, tzinfo=UTC)
    records = [
        _EventRecord(
            event_type="search_query",
            payload={
                "query": "  Grumpy   Cat ",
                "request_id": "strict-request",
                "properties": {"result_total": 2, "latency_ms": 18.5},
            },
            user_id=None,
            occurred_at=occurred_at,
        ),
        _EventRecord(
            event_type="search_query",
            payload={"query": "grumpy cat", "request_id": "legacy-request", "result_count": 0},
            user_id=None,
            occurred_at=occurred_at,
        ),
        _EventRecord(
            event_type="meme_detail_click",
            payload={"request_id": "strict-request", "refs": {"meme_id": str(meme_id)}},
            user_id=None,
            occurred_at=occurred_at,
        ),
        _EventRecord(
            event_type="meme_download",
            payload={"request_id": "legacy-request", "meme_id": str(meme_id)},
            user_id=None,
            occurred_at=occurred_at,
        ),
        _EventRecord(
            event_type="meme_download",
            payload={"query": "grumpy cat", "meme_id": str(meme_id)},
            user_id=None,
            occurred_at=occurred_at,
        ),
    ]

    stats = _analyse_search_queries(records)["grumpy cat"]

    assert stats.query == "Grumpy Cat"
    assert stats.searches == 2
    assert stats.zero_result_searches == 1
    assert stats.average_latency_ms == 18.5
    assert stats.detail_clicks == 1
    assert stats.downloads == 1
    assert stats.outcomes[meme_id].interactions == 2
    assert stats.outcomes[meme_id].detail_clicks == 1
    assert stats.outcomes[meme_id].downloads == 1


def test_retention_cohorts_keep_guest_identity_after_merge_and_dedupe_user_fallbacks() -> None:
    cohort_date = date(2026, 6, 1)
    occurred_at = datetime(2026, 6, 1, 12, tzinfo=UTC)
    merged_guest_id = uuid.uuid7()
    canonical_user_id = uuid.uuid7()
    unmerged_guest_id = uuid.uuid7()
    direct_full_user_id = uuid.uuid7()
    events = [
        _EventRecord(
            event_type="auth_event",
            payload={
                "properties": {"action": "guest_created"},
                "refs": {"source_user_id": str(merged_guest_id)},
            },
            user_id=canonical_user_id,
            occurred_at=occurred_at,
        ),
        _EventRecord(
            event_type="auth_event",
            payload={
                "properties": {"action": "guest_created"},
                "refs": {"source_user_id": str(merged_guest_id)},
            },
            user_id=canonical_user_id,
            occurred_at=occurred_at + timedelta(seconds=1),
        ),
        _EventRecord(
            event_type="auth_event",
            payload={
                "properties": {"action": "guest_created"},
                "refs": {"source_user_id": str(unmerged_guest_id)},
            },
            user_id=unmerged_guest_id,
            occurred_at=occurred_at,
        ),
    ]
    created_users = [
        (merged_guest_id, occurred_at),
        (canonical_user_id, occurred_at),
        (unmerged_guest_id, occurred_at),
        (direct_full_user_id, occurred_at),
    ]

    cohorts = _retention_cohort_members_by_date(
        events,
        created_users,
        today=cohort_date + timedelta(days=40),
    )

    assert cohorts == {
        cohort_date: {
            merged_guest_id: canonical_user_id,
            canonical_user_id: canonical_user_id,
            unmerged_guest_id: unmerged_guest_id,
            direct_full_user_id: direct_full_user_id,
        }
    }


def test_opaque_query_key_is_stable_for_a_normalized_group_and_non_reversible() -> None:
    secret = "test-analytics-key-secret"
    first = build_admin_analytics_query_key("  Grumpy   Cat ", secret=secret)
    second = build_admin_analytics_query_key("grumpy cat", secret=secret)

    assert first == second
    assert len(first) == 64
    assert "grumpy" not in first
    assert first != build_admin_analytics_query_key("grumpy cat", secret="another-secret")


def test_interaction_totals_exclude_exposure_and_lifecycle_events() -> None:
    occurred_at = datetime(2026, 7, 15, 12, tzinfo=UTC)
    events = [
        _EventRecord(event_type=event_type, payload={}, user_id=None, occurred_at=occurred_at)
        for event_type in (
            "page_view",
            "search_query",
            "impression",
            "meme_impression",
            "inline_query",
            "inline_served",
            "auth_event",
            "meme_detail_click",
            "meme_download",
        )
    ]

    assert _count_interactions(events) == 2


def test_cumulative_counter_recovery_does_not_double_count_previous_high_watermark() -> None:
    baseline: int | None = 100
    observed_deltas: list[int] = []
    for current in (90, 100, 110):
        observed_deltas.append(_counter_delta(current, baseline))
        baseline = _counter_high_watermark(current, baseline)

    assert observed_deltas == [0, 0, 10]
    assert baseline == 110
