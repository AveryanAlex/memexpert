"""Response schemas for the read-only browser-admin analytics workspace."""
# ruff: noqa: TC003

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _AdminAnalyticsSchema(BaseModel):
    """Keep analytics responses deliberately aggregate-only and stable."""

    model_config = ConfigDict(extra="forbid")


class AdminAnalyticsRangeRead(_AdminAnalyticsSchema):
    """Resolved inclusive UTC calendar-date range and matching comparison period."""

    start_date: date
    end_date: date
    comparison_start_date: date
    comparison_end_date: date
    timezone: Literal["UTC"] = "UTC"
    bucket: Literal["day"] = "day"


class AdminAnalyticsMetricRead(_AdminAnalyticsSchema):
    """Current-period value with a same-length prior-period comparison."""

    value: int | float
    previous_value: int | float
    change: int | float
    change_percent: float | None = None


class AdminAnalyticsBreakdownRead(_AdminAnalyticsSchema):
    """One named aggregate suitable for a bar, donut, or compact table."""

    key: str
    count: int


class AdminAnalyticsSurfaceRead(_AdminAnalyticsSchema):
    """Interaction/page-view count for one coarse product surface."""

    surface: str
    count: int


class AdminAnalyticsOverviewActivityPointRead(_AdminAnalyticsSchema):
    date: date
    page_views: int
    active_users: int
    interactions: int
    searches: int
    downloads: int
    new_memes: int


class AdminAnalyticsDiscoveryFunnelRead(_AdminAnalyticsSchema):
    searches: int
    searches_with_results: int
    searches_without_results: int
    detail_clicks: int
    downloads: int


class AdminAnalyticsSourceActivityRead(_AdminAnalyticsSchema):
    sources: int
    new_sources: int
    source_views: int
    source_reactions: int
    source_reposts: int


class AdminAnalyticsOverviewRead(_AdminAnalyticsSchema):
    range: AdminAnalyticsRangeRead
    metrics: dict[str, AdminAnalyticsMetricRead]
    activity: list[AdminAnalyticsOverviewActivityPointRead]
    discovery_funnel: AdminAnalyticsDiscoveryFunnelRead
    surface_mix: list[AdminAnalyticsSurfaceRead]
    source_activity: AdminAnalyticsSourceActivityRead


class AdminAnalyticsEngagementActivityPointRead(_AdminAnalyticsSchema):
    date: date
    interactions: int
    searches: int
    zero_result_searches: int
    detail_clicks: int
    downloads: int
    sends: int
    saves: int
    shares: int


class AdminAnalyticsSearchQueryRead(_AdminAnalyticsSchema):
    """Aggregate for one raw query; never includes a user or request identifier."""

    query: str
    query_key: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    searches: int
    zero_result_searches: int
    zero_result_rate: float | None = None
    average_latency_ms: float | None = None
    detail_clicks: int
    downloads: int


class AdminAnalyticsEngagementRead(_AdminAnalyticsSchema):
    range: AdminAnalyticsRangeRead
    metrics: dict[str, AdminAnalyticsMetricRead]
    activity: list[AdminAnalyticsEngagementActivityPointRead]
    interactions_by_type: list[AdminAnalyticsBreakdownRead]
    surface_mix: list[AdminAnalyticsSurfaceRead]
    top_search_queries: list[AdminAnalyticsSearchQueryRead]


class AdminAnalyticsAudienceActivityPointRead(_AdminAnalyticsSchema):
    date: date
    new_guests: int
    new_full_accounts: int
    active_users: int
    guest_to_full_conversions: int


class AdminAnalyticsRetentionPeriodRead(_AdminAnalyticsSchema):
    eligible_users: int
    retained_users: int
    rate: float | None = None


class AdminAnalyticsRetentionCohortRead(_AdminAnalyticsSchema):
    cohort_date: date
    cohort_size: int
    d1: AdminAnalyticsRetentionPeriodRead | None = None
    d7: AdminAnalyticsRetentionPeriodRead | None = None
    d30: AdminAnalyticsRetentionPeriodRead | None = None


class AdminAnalyticsAudienceRead(_AdminAnalyticsSchema):
    range: AdminAnalyticsRangeRead
    metrics: dict[str, AdminAnalyticsMetricRead]
    activity: list[AdminAnalyticsAudienceActivityPointRead]
    surface_mix: list[AdminAnalyticsSurfaceRead]
    retention_cohorts: list[AdminAnalyticsRetentionCohortRead]


class AdminAnalyticsCatalogGrowthPointRead(_AdminAnalyticsSchema):
    date: date
    new_memes: int


class AdminAnalyticsSourceEngagementPointRead(_AdminAnalyticsSchema):
    date: date
    source_views: int
    source_reactions: int
    source_reposts: int


class AdminAnalyticsContentRead(_AdminAnalyticsSchema):
    range: AdminAnalyticsRangeRead
    metrics: dict[str, AdminAnalyticsMetricRead]
    catalog_growth: list[AdminAnalyticsCatalogGrowthPointRead]
    media_types: list[AdminAnalyticsBreakdownRead]
    languages: list[AdminAnalyticsBreakdownRead]
    visibility: list[AdminAnalyticsBreakdownRead]
    processing: list[AdminAnalyticsBreakdownRead]
    source_health: list[AdminAnalyticsBreakdownRead]
    source_engagement: list[AdminAnalyticsSourceEngagementPointRead]


class AdminAnalyticsSearchQueryPageRead(_AdminAnalyticsSchema):
    range: AdminAnalyticsRangeRead
    items: list[AdminAnalyticsSearchQueryRead]
    total: int
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class AdminAnalyticsQueryMemeOutcomeRead(_AdminAnalyticsSchema):
    """Aggregate interactions attributable to one query and one meme."""

    meme_id: uuid.UUID
    interactions: int
    detail_clicks: int
    downloads: int
    saves: int
    shares: int


class AdminAnalyticsSearchQueryDetailRead(_AdminAnalyticsSchema):
    range: AdminAnalyticsRangeRead
    query: str
    query_key: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    searches: int
    zero_result_searches: int
    zero_result_rate: float | None = None
    average_latency_ms: float | None = None
    meme_outcomes: list[AdminAnalyticsQueryMemeOutcomeRead]


__all__ = [
    "AdminAnalyticsAudienceRead",
    "AdminAnalyticsBreakdownRead",
    "AdminAnalyticsCatalogGrowthPointRead",
    "AdminAnalyticsContentRead",
    "AdminAnalyticsDiscoveryFunnelRead",
    "AdminAnalyticsEngagementActivityPointRead",
    "AdminAnalyticsEngagementRead",
    "AdminAnalyticsMetricRead",
    "AdminAnalyticsOverviewActivityPointRead",
    "AdminAnalyticsOverviewRead",
    "AdminAnalyticsQueryMemeOutcomeRead",
    "AdminAnalyticsRangeRead",
    "AdminAnalyticsRetentionCohortRead",
    "AdminAnalyticsRetentionPeriodRead",
    "AdminAnalyticsSearchQueryDetailRead",
    "AdminAnalyticsSearchQueryPageRead",
    "AdminAnalyticsSearchQueryRead",
    "AdminAnalyticsSourceActivityRead",
    "AdminAnalyticsSourceEngagementPointRead",
    "AdminAnalyticsSurfaceRead",
]
