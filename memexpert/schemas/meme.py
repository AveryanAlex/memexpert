# ruff: noqa: TC001,TC003
"""Reusable meme read DTOs for web, Telegram bot, and API surfaces."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from memexpert.models.enums import ContentKind, ContentLanguage


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


class MemeSearchPageRead(BaseModel):
    """Offset pagination envelope for hybrid meme search."""

    items: list[MemeSearchResultRead]
    limit: int
    offset: int
    total: int
    has_more: bool


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


class PublicMemeSearchPageRead(BaseModel):
    """Offset pagination envelope for public meme catalog responses."""

    items: list[PublicMemeSearchResultRead]
    limit: int
    offset: int
    total: int
    has_more: bool


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


class PublicMemeTrendPageRead(BaseModel):
    """Offset pagination envelope for public trend rankings."""

    items: list[PublicMemeTrendRead]
    limit: int
    offset: int
    total: int
    has_more: bool


class PublicMemePopularityPointRead(BaseModel):
    """One real captured popularity snapshot for a public meme sparkline."""

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


class PublicTrendSummaryRead(BaseModel):
    """Aggregate trend summary for a public tag or template."""

    kind: str
    slug: str
    title: str
    description: str | None = None
    meme_count: int
    trend: PublicTrendMetricsRead


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
    "MemeSlugRedirectRead",
    "MemeSearchPageRead",
    "MemeSearchResultRead",
    "MemeSearchScoreRead",
    "PublicMemeCardRead",
    "PublicMemeDetailRead",
    "PublicMemeFileRead",
    "PublicMemeFileRenderRead",
    "PublicMemeLandingRead",
    "PublicMemePopularityPointRead",
    "PublicMemePopularitySummaryRead",
    "PublicMemeSearchPageRead",
    "PublicMemeSearchResultRead",
    "PublicMemeTrendPageRead",
    "PublicMemeTrendRead",
    "PublicTrendCountsRead",
    "PublicTrendMetricsRead",
    "PublicTrendSummaryRead",
]
