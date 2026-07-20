# ruff: noqa: TC001,TC003
"""Reusable meme read DTOs for web, Telegram bot, and API surfaces."""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from memexpert.models.enums import ContentKind, ContentLanguage


def _new_discovery_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


def new_discovery_request_id() -> str:
    """Return a compact public-safe discovery page/request identifier."""

    return _new_discovery_id("req")


def new_discovery_impression_id() -> str:
    """Return a compact public-safe visible-result impression identifier."""

    return _new_discovery_id("imp")


class MemeResultAttributionFiltersRead(BaseModel):
    """Safe normalized discovery filters echoed for later interaction events."""

    language: ContentLanguage | None = None
    media_type: ContentKind | None = None
    include_nsfw: bool = False
    tags: list[str] = Field(default_factory=list)
    scope: str | None = None
    collection_ids: list[str] = Field(default_factory=list)


class MemeResultAttributionRead(BaseModel):
    """Public-safe source metadata for one discoverable meme impression."""

    request_id: str | None = None
    impression_id: str = Field(default_factory=new_discovery_impression_id)
    surface: str | None = None
    source_algorithm: str | None = None
    rank: int | None = None
    query: str | None = None
    filters: MemeResultAttributionFiltersRead = Field(default_factory=MemeResultAttributionFiltersRead)
    collection_scope: str | None = None
    collection_ids: list[str] = Field(default_factory=list)
    source_meme_id: uuid.UUID | None = None
    algorithm_version: str | None = None
    score: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    reason: str | None = None


class MemeFileRead(BaseModel):
    """Public file metadata needed to render or send a meme."""

    id: uuid.UUID
    mime_type: str | None
    width: int | None
    height: int | None
    file_size_bytes: int | None
    s3_original_key: str
    s3_web_video_key: str | None
    blur_hash: str | None
    quality_score: float


class MemeCardRead(BaseModel):
    """Compact meme card shared by search results and quick-pick bot surfaces."""

    id: uuid.UUID
    media_type: ContentKind
    language: ContentLanguage
    is_nsfw: bool
    popularity_score: float
    like_count: int
    tags: list[str] = Field(default_factory=list)
    primary_file: MemeFileRead | None
    caption: str | None
    seo_page_slug: str | None = None
    created_at: datetime
    updated_at: datetime


class MemeDetailRead(MemeCardRead):
    """Detailed meme DTO for read screens and bot send confirmation flows."""

    ocr_text: str | None
    is_public: bool
    seo_title: str | None
    seo_description: str | None
    seo_alt_text: str | None = None
    seo_body_text: str | None = None
    seo_model_id: str | None = None
    seo_prompt_version: str | None = None
    seo_generated_at: datetime | None = None
    files: list[MemeFileRead] = Field(default_factory=list)


class MemeSlugRedirectRead(BaseModel):
    """Canonical slug metadata for id-based public links."""

    meme_id: uuid.UUID
    slug: str
    path: str
    should_redirect: bool


class MemeSearchScoreRead(BaseModel):
    """Debuggable score components from the initial hybrid ranker."""

    semantic: float
    text: float
    popularity: float
    total: float


class MemeSearchResultRead(BaseModel):
    """One ranked search result with reusable card data."""

    meme: MemeCardRead
    score: MemeSearchScoreRead
    attribution: MemeResultAttributionRead = Field(default_factory=MemeResultAttributionRead)


class MemeSearchPageRead(BaseModel):
    """Offset pagination envelope for hybrid meme search."""

    items: list[MemeSearchResultRead]
    limit: int
    offset: int
    total: int
    has_more: bool
    request_id: str = Field(default_factory=new_discovery_request_id)


class PublicMemeFileRenderRead(BaseModel):
    """Public media URLs and render metadata safe for website clients."""

    thumbnail_url: str | None = None
    preview_url: str | None = None
    display_url: str | None = None
    original_url: str | None = None
    download_url: str | None = None
    web_video_url: str | None = None
    width: int | None = None
    height: int | None = None
    blur_hash: str | None = None


class PublicMemeFileRead(BaseModel):
    """Safe public file metadata without internal storage object keys."""

    id: uuid.UUID
    mime_type: str | None
    width: int | None
    height: int | None
    file_size_bytes: int | None
    blur_hash: str | None
    quality_score: float
    render: PublicMemeFileRenderRead | None = None


class PublicMemeViewerAccess(StrEnum):
    """Safe viewer-relative visibility markers for expanded search scopes."""

    PUBLIC = "public"
    PRIVATE = "private"
    SHARED = "shared"


class PublicMemeViewerAccessRead(BaseModel):
    """Viewer-relative result visibility without owner or collection metadata."""

    visibility: PublicMemeViewerAccess


class PublicMemeCardRead(BaseModel):
    """Safe public meme card for catalog search and browse responses."""

    id: uuid.UUID
    media_type: ContentKind
    language: ContentLanguage
    is_nsfw: bool
    popularity_score: float
    like_count: int
    tags: list[str] = Field(default_factory=list)
    primary_file: PublicMemeFileRead | None
    caption: str | None
    seo_page_slug: str | None = None
    viewer_has_favorited: bool = False
    viewer_has_saved: bool = Field(
        default=False,
        description="Whether the meme belongs to any non-Favorites collection accessible to the viewer.",
    )
    viewer_has_pinned: bool = False
    viewer_access: PublicMemeViewerAccessRead | None = None
    created_at: datetime
    updated_at: datetime


class PublicMemeDetailRead(PublicMemeCardRead):
    """Safe public meme detail DTO without owner or storage internals."""

    ocr_text: str | None
    seo_title: str | None
    seo_description: str | None
    seo_alt_text: str | None = None
    seo_body_text: str | None = None
    seo_model_id: str | None = None
    seo_prompt_version: str | None = None
    seo_generated_at: datetime | None = None
    files: list[PublicMemeFileRead] = Field(default_factory=list)


class PublicMemeSearchResultRead(BaseModel):
    """One public search result without internal ranking/debug components."""

    meme: PublicMemeCardRead
    attribution: MemeResultAttributionRead = Field(default_factory=MemeResultAttributionRead)


class PublicMemeOfTheDayRead(BaseModel):
    """Daily public-safe MOTD cache response for homepage rendering."""

    meme: PublicMemeCardRead | None = None
    selected_for: date
    refreshed_at: datetime
    algorithm_version: str
    score: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    reason: str
    candidate_count: int
    attribution: MemeResultAttributionRead | None = None


class MemeFavoriteMutationRead(BaseModel):
    """Authoritative Favorite state returned after an idempotent mutation."""

    favorited: bool
    changed: bool
    like_count: int = Field(ge=0)


class PublicMemeSearchPageRead(BaseModel):
    """Offset pagination envelope for public meme catalog responses."""

    items: list[PublicMemeSearchResultRead]
    limit: int
    offset: int
    total: int
    has_more: bool
    request_id: str = Field(default_factory=new_discovery_request_id)


class PublicTrendCountsRead(BaseModel):
    """Aggregate public trend event counts for one ranking window."""

    views: int = 0
    sends: int = 0
    likes: int = 0
    saves: int = 0
    downloads: int = 0


class PublicTrendMetricsRead(BaseModel):
    """Materialized public trend metrics without raw user or query payloads."""

    recent: PublicTrendCountsRead = Field(default_factory=PublicTrendCountsRead)
    previous: PublicTrendCountsRead = Field(default_factory=PublicTrendCountsRead)
    latest_snapshot_at: datetime | None = None
    latest_source_views: int = 0
    latest_source_reactions: int = 0
    latest_source_reposts: int = 0
    latest_platform_views: int = 0
    latest_platform_sends: int = 0
    latest_platform_saves: int = 0
    latest_platform_likes: int = 0
    latest_popularity_score: float = 0.0
    engagement_24h: float = 0.0
    trending_score: float = 0.0
    refreshed_at: datetime | None = None


class PublicMemeTrendRead(BaseModel):
    """One public meme ranking row plus aggregate trend metrics."""

    meme: PublicMemeCardRead
    trend: PublicTrendMetricsRead
    attribution: MemeResultAttributionRead = Field(default_factory=MemeResultAttributionRead)


class PublicMemeTrendPageRead(BaseModel):
    """Offset pagination envelope for public trend rankings."""

    items: list[PublicMemeTrendRead]
    limit: int
    offset: int
    total: int
    has_more: bool
    request_id: str = Field(default_factory=new_discovery_request_id)


class PublicMemePopularityPointRead(BaseModel):
    """One derived public engagement point for a meme sparkline."""

    captured_at: datetime
    source_views: int
    source_reactions: int
    source_reposts: int
    platform_views: int
    platform_sends: int
    platform_saves: int
    platform_likes: int
    popularity_score: float


class PublicMemePopularitySummaryRead(BaseModel):
    """Public per-meme popularity summary backed by MV metrics and real snapshots."""

    meme_id: uuid.UUID
    trend: PublicTrendMetricsRead | None = None
    sparkline: list[PublicMemePopularityPointRead] = Field(default_factory=list)


class PublicMemeSourceSort(StrEnum):
    """Stable public source-post orderings supported by the detail page."""

    VIEWS_DESC = "views_desc"
    REACTIONS_DESC = "reactions_desc"
    REPOSTS_DESC = "reposts_desc"
    INTERACTION_RATE_DESC = "interaction_rate_desc"
    NEWEST = "newest"
    OLDEST = "oldest"


class PublicMemeAnalyticsWindow(StrEnum):
    """Selectable public analytics history windows."""

    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    ALL = "all"


class PublicMemeAnalyticsGranularity(StrEnum):
    """Time bucket used by one public analytics point."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    ADAPTIVE = "adaptive"


class PublicMemeMetricCoverageRead(BaseModel):
    """How many source posts exposed one nullable Telegram counter."""

    measured_posts: int = Field(default=0, ge=0)
    total_posts: int = Field(default=0, ge=0)
    ratio: float = Field(default=0.0, ge=0.0, le=1.0)


class PublicMemeSourceCoverageRead(BaseModel):
    """Per-counter measurement coverage for public source posts."""

    views: PublicMemeMetricCoverageRead = Field(default_factory=PublicMemeMetricCoverageRead)
    reactions: PublicMemeMetricCoverageRead = Field(default_factory=PublicMemeMetricCoverageRead)
    comments: PublicMemeMetricCoverageRead = Field(default_factory=PublicMemeMetricCoverageRead)
    reposts: PublicMemeMetricCoverageRead = Field(default_factory=PublicMemeMetricCoverageRead)


class PublicMemeSourceTotalsRead(BaseModel):
    """Known absolute Telegram counters; ``None`` means no post exposed the metric."""

    views: int | None = Field(default=None, ge=0)
    reactions: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)


class PublicMemeSourceRateRead(BaseModel):
    """A transparent ratio-of-sums with its eligible-post coverage."""

    value: float | None = Field(default=None, ge=0.0)
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    eligible_posts: int = Field(default=0, ge=0)
    total_posts: int = Field(default=0, ge=0)


class PublicMemeSourceRatesRead(BaseModel):
    """Views-based Telegram engagement rates over fully measurable post subsets."""

    reactions: PublicMemeSourceRateRead = Field(default_factory=PublicMemeSourceRateRead)
    comments: PublicMemeSourceRateRead = Field(default_factory=PublicMemeSourceRateRead)
    reposts: PublicMemeSourceRateRead = Field(default_factory=PublicMemeSourceRateRead)
    interactions: PublicMemeSourceRateRead = Field(default_factory=PublicMemeSourceRateRead)


class PublicMemeSourceAudienceRead(BaseModel):
    """Subscriber observations usable for one source post without claiming reach."""

    audience_at_publish: int | None = Field(default=None, ge=0)
    current_audience: int | None = Field(default=None, ge=0)
    views_per_1000_subscribers: float | None = Field(default=None, ge=0.0)
    interactions_per_1000_subscribers: float | None = Field(default=None, ge=0.0)


class PublicMemeSourceAudienceSummaryRead(BaseModel):
    """Coverage and normalized source performance over known audience snapshots."""

    current_known_channels: int = Field(default=0, ge=0)
    total_channels: int = Field(default=0, ge=0)
    publish_time_eligible_posts: int = Field(default=0, ge=0)
    total_posts: int = Field(default=0, ge=0)
    views_per_1000_subscribers: PublicMemeSourceRateRead = Field(
        default_factory=PublicMemeSourceRateRead,
    )
    interactions_per_1000_subscribers: PublicMemeSourceRateRead = Field(
        default_factory=PublicMemeSourceRateRead,
    )


class PublicMemeSourcePostRead(BaseModel):
    """One public Telegram post without crawler/session or raw-source internals."""

    channel_title: str
    channel_username: str | None = None
    channel_url: str | None = None
    post_url: str | None = None
    published_at: datetime | None = None
    available: bool
    captured_at: datetime | None = None
    views: int | None = Field(default=None, ge=0)
    reactions: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    rates: PublicMemeSourceRatesRead = Field(default_factory=PublicMemeSourceRatesRead)
    audience: PublicMemeSourceAudienceRead = Field(default_factory=PublicMemeSourceAudienceRead)


class PublicMemeSourceSummaryRead(BaseModel):
    """Aggregate public Telegram provenance and measurement coverage."""

    total_posts: int = Field(default=0, ge=0)
    available_posts: int = Field(default=0, ge=0)
    distinct_channels: int = Field(default=0, ge=0)
    earliest_published_at: datetime | None = None
    latest_published_at: datetime | None = None
    latest_captured_at: datetime | None = None
    totals: PublicMemeSourceTotalsRead = Field(default_factory=PublicMemeSourceTotalsRead)
    coverage: PublicMemeSourceCoverageRead = Field(default_factory=PublicMemeSourceCoverageRead)
    rates: PublicMemeSourceRatesRead = Field(default_factory=PublicMemeSourceRatesRead)
    audience: PublicMemeSourceAudienceSummaryRead = Field(default_factory=PublicMemeSourceAudienceSummaryRead)


class PublicMemeSourcePageRead(BaseModel):
    """Stable offset page of public Telegram source posts."""

    meme_id: uuid.UUID
    snapshot_at: datetime
    sort: PublicMemeSourceSort
    items: list[PublicMemeSourcePostRead] = Field(default_factory=list)
    summary: PublicMemeSourceSummaryRead = Field(default_factory=PublicMemeSourceSummaryRead)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)
    has_more: bool


class PublicMemeActivityCountsRead(BaseModel):
    """Exact activity counts; downloads are reported but excluded from Recorded activity."""

    source_views: int = Field(default=0, ge=0)
    source_reactions: int = Field(default=0, ge=0)
    source_reposts: int = Field(default=0, ge=0)
    memeexpert_views: int = Field(default=0, ge=0)
    memeexpert_sends: int = Field(default=0, ge=0)
    memeexpert_saves: int = Field(default=0, ge=0)
    memeexpert_favorites: int = Field(default=0, ge=0)
    downloads: int = Field(default=0, ge=0)
    recorded_activity: int = Field(default=0, ge=0)


class PublicMemeActivityPointRead(PublicMemeActivityCountsRead):
    """One exact public activity bucket."""

    bucket_start: datetime
    bucket_end: datetime
    granularity: PublicMemeAnalyticsGranularity


class PublicMemeAnalyticsMomentumRead(BaseModel):
    """Latest seven complete/partial UTC days compared with the preceding seven."""

    recent_recorded_activity: int = Field(default=0, ge=0)
    previous_recorded_activity: int = Field(default=0, ge=0)
    change: int = 0
    change_rate: float | None = None


class PublicMemeAnalyticsPeakRead(BaseModel):
    """Highest Recorded activity bucket in the selected period."""

    bucket_start: datetime
    bucket_end: datetime
    granularity: PublicMemeAnalyticsGranularity
    recorded_activity: int = Field(ge=0)


class PublicMemeAnalyticsSummaryRead(BaseModel):
    """Selected-period totals and compact headline metrics."""

    totals: PublicMemeActivityCountsRead = Field(default_factory=PublicMemeActivityCountsRead)
    average_recorded_activity_per_day: float = Field(default=0.0, ge=0.0)
    current_favorites: int = Field(default=0, ge=0)
    momentum: PublicMemeAnalyticsMomentumRead = Field(default_factory=PublicMemeAnalyticsMomentumRead)
    peak: PublicMemeAnalyticsPeakRead | None = None


class PublicMemeObservedSourcePointRead(PublicMemeSourceTotalsRead):
    """Absolute known Telegram counters at an observation boundary."""

    observed_at: datetime
    coverage: PublicMemeSourceCoverageRead = Field(default_factory=PublicMemeSourceCoverageRead)


class PublicMemeObservedSourceSeriesRead(BaseModel):
    """Opening baseline plus real absolute Telegram observations."""

    opening_baseline: PublicMemeObservedSourcePointRead
    points: list[PublicMemeObservedSourcePointRead] = Field(default_factory=list)


class PublicMemeWebExposureFunnelRead(BaseModel):
    """Web exposure funnel using only distinct matched impression tokens."""

    recorded_card_impressions: int = Field(default=0, ge=0)
    attributed_impressions: int = Field(default=0, ge=0)
    matched_detail_clicks: int = Field(default=0, ge=0)
    matched_high_intent_actions: int = Field(default=0, ge=0)
    detail_click_rate: float | None = Field(default=None, ge=0.0)
    high_intent_rate: float | None = Field(default=None, ge=0.0)


class PublicMemeInlineExposureFunnelRead(BaseModel):
    """Telegram inline funnel using only distinct matched impression tokens."""

    inline_results_served: int = Field(default=0, ge=0)
    attributed_results_served: int = Field(default=0, ge=0)
    matched_chosen: int = Field(default=0, ge=0)
    matched_sent: int = Field(default=0, ge=0)
    chosen_rate: float | None = Field(default=None, ge=0.0)
    sent_rate: float | None = Field(default=None, ge=0.0)


class PublicMemeExposureFunnelsRead(BaseModel):
    """Separate web and Telegram-inline exposure funnels."""

    web: PublicMemeWebExposureFunnelRead = Field(default_factory=PublicMemeWebExposureFunnelRead)
    telegram_inline: PublicMemeInlineExposureFunnelRead = Field(
        default_factory=PublicMemeInlineExposureFunnelRead,
    )


class PublicMemeChannelAudienceChangeRead(BaseModel):
    """Known subscriber-count change without presenting summed subscribers as reach."""

    total_channels: int = Field(default=0, ge=0)
    current_known_channels: int = Field(default=0, ge=0)
    comparable_channels: int = Field(default=0, ge=0)
    net_known_subscriber_change: int | None = None


class PublicMemeAnalyticsRead(BaseModel):
    """Privacy-safe professional analytics for one visible public meme."""

    meme_id: uuid.UUID
    window: PublicMemeAnalyticsWindow
    start_at: datetime
    end_at: datetime
    granularity: PublicMemeAnalyticsGranularity
    history_start_at: datetime | None = None
    history_end_at: datetime | None = None
    refreshed_at: datetime
    insufficient_history: bool = False
    summary: PublicMemeAnalyticsSummaryRead = Field(default_factory=PublicMemeAnalyticsSummaryRead)
    activity_points: list[PublicMemeActivityPointRead] = Field(default_factory=list)
    observed_source: PublicMemeObservedSourceSeriesRead
    source_performance: PublicMemeSourceSummaryRead = Field(default_factory=PublicMemeSourceSummaryRead)
    audience_change: PublicMemeChannelAudienceChangeRead = Field(
        default_factory=PublicMemeChannelAudienceChangeRead,
    )
    exposure_funnels: PublicMemeExposureFunnelsRead = Field(default_factory=PublicMemeExposureFunnelsRead)


class PublicTrendAggregatePointRead(BaseModel):
    """One real aggregate trend point for public tag/template history."""

    observed_at: datetime | None = None
    value: float
    metric: str
    label: str
    meme_count: int = 0
    snapshot_count: int = 0
    source_views: int = 0
    source_reactions: int = 0
    source_reposts: int = 0
    platform_views: int = 0
    platform_sends: int = 0
    platform_saves: int = 0
    platform_likes: int = 0


class PublicTrendComparisonPointRead(PublicTrendAggregatePointRead):
    """One real trend comparison point for a meme or aggregate series."""


class PublicTrendComparisonSeriesRead(BaseModel):
    """One requested comparison item plus real analytics points when available."""

    kind: str
    value: str
    title: str
    description: str | None = None
    meme: PublicMemeCardRead | None = None
    trend: PublicTrendMetricsRead | None = None
    points: list[PublicTrendComparisonPointRead] = Field(default_factory=list)
    insufficient_history: bool = False
    no_data_reason: str | None = None
    current_only_reason: str | None = None


class PublicTrendComparisonRead(BaseModel):
    """Shareable public trend comparison response for URL item params."""

    items: list[PublicTrendComparisonSeriesRead]
    requested_items: list[str] = Field(default_factory=list)
    max_items: int


class PublicTrendSummaryRead(BaseModel):
    """Aggregate trend summary for a public tag or template."""

    kind: str
    slug: str
    title: str
    description: str | None = None
    meme_count: int
    trend: PublicTrendMetricsRead
    points: list[PublicTrendAggregatePointRead] = Field(default_factory=list)
    insufficient_history: bool = False
    no_data_reason: str | None = None
    current_only_reason: str | None = None


class PublicTrendTimelineMemeRead(BaseModel):
    """Top meme for one real snapshot timeline period."""

    meme: PublicMemeCardRead
    popularity_score: float
    snapshot_count: int
    first_captured_at: datetime
    last_captured_at: datetime
    source_views: int = 0
    source_reactions: int = 0
    source_reposts: int = 0
    platform_views: int = 0
    platform_sends: int = 0
    platform_saves: int = 0
    platform_likes: int = 0


class PublicTrendTimelinePeriodRead(BaseModel):
    """One month/year period with top public memes from real snapshots."""

    period: str
    period_start: datetime
    top_memes: list[PublicTrendTimelineMemeRead] = Field(default_factory=list)
    meme_count: int
    snapshot_count: int


class PublicTrendTimelinePageRead(BaseModel):
    """Offset pagination envelope for public meme timeline periods."""

    granularity: str
    periods: list[PublicTrendTimelinePeriodRead]
    limit: int
    offset: int
    total: int
    has_more: bool


class PublicMemeLandingRead(BaseModel):
    """Minimal tag/template landing response for organic pages."""

    kind: str
    slug: str
    title: str
    description: str | None
    page: PublicMemeSearchPageRead
    trend_summary: PublicTrendSummaryRead | None = None


__all__ = [
    "MemeCardRead",
    "MemeDetailRead",
    "MemeFileRead",
    "MemeResultAttributionFiltersRead",
    "MemeResultAttributionRead",
    "MemeSlugRedirectRead",
    "MemeSearchPageRead",
    "MemeSearchResultRead",
    "MemeSearchScoreRead",
    "PublicMemeCardRead",
    "PublicMemeActivityCountsRead",
    "PublicMemeActivityPointRead",
    "PublicMemeAnalyticsGranularity",
    "PublicMemeAnalyticsMomentumRead",
    "PublicMemeAnalyticsPeakRead",
    "PublicMemeAnalyticsRead",
    "PublicMemeAnalyticsSummaryRead",
    "PublicMemeAnalyticsWindow",
    "PublicMemeChannelAudienceChangeRead",
    "PublicMemeDetailRead",
    "PublicMemeExposureFunnelsRead",
    "PublicMemeFileRead",
    "PublicMemeFileRenderRead",
    "PublicMemeInlineExposureFunnelRead",
    "PublicMemeLandingRead",
    "PublicMemeMetricCoverageRead",
    "PublicMemeOfTheDayRead",
    "PublicMemeObservedSourcePointRead",
    "PublicMemeObservedSourceSeriesRead",
    "PublicMemePopularityPointRead",
    "PublicMemePopularitySummaryRead",
    "PublicMemeSourceAudienceRead",
    "PublicMemeSourceAudienceSummaryRead",
    "PublicMemeSourceCoverageRead",
    "PublicMemeSourcePageRead",
    "PublicMemeSourcePostRead",
    "PublicMemeSourceRateRead",
    "PublicMemeSourceRatesRead",
    "PublicMemeSourceSort",
    "PublicMemeSourceSummaryRead",
    "PublicMemeSourceTotalsRead",
    "PublicMemeWebExposureFunnelRead",
    "PublicTrendAggregatePointRead",
    "PublicTrendComparisonPointRead",
    "PublicTrendComparisonRead",
    "PublicTrendComparisonSeriesRead",
    "PublicMemeSearchPageRead",
    "PublicMemeSearchResultRead",
    "PublicMemeTrendPageRead",
    "PublicMemeTrendRead",
    "PublicTrendCountsRead",
    "PublicTrendMetricsRead",
    "PublicTrendSummaryRead",
    "PublicTrendTimelineMemeRead",
    "PublicTrendTimelinePageRead",
    "PublicTrendTimelinePeriodRead",
    "new_discovery_impression_id",
    "new_discovery_request_id",
]
