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
    author_user_id: uuid.UUID | None
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
    viewer_has_saved: bool = False
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
    "PublicMemeDetailRead",
    "PublicMemeFileRead",
    "PublicMemeFileRenderRead",
    "PublicMemeLandingRead",
    "PublicMemeOfTheDayRead",
    "PublicMemePopularityPointRead",
    "PublicMemePopularitySummaryRead",
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
