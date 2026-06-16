# ruff: noqa: TC001,TC003
"""Public SEO catalog DTOs for sitemap, feed, and landing-page builders."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import PublicMemeFileRead


class SeoCatalogSummaryRead(BaseModel):
    """Counts and freshness metadata for public safe SEO catalog planning."""

    public_safe_meme_count: int
    tag_count: int
    template_count: int
    updated_at: datetime | None = None


class SeoCatalogMemeTemplateRefRead(BaseModel):
    """Template metadata safe to embed in public meme catalog rows."""

    slug: str
    name: str
    title: str
    description: str | None = None


class SeoCatalogMemeRead(BaseModel):
    """Public safe meme row for sitemap image metadata and feed enclosures."""

    id: uuid.UUID
    seo_slug: str | None = None
    title: str
    description: str | None = None
    alt_text: str
    caption: str | None = None
    tags: list[str] = Field(default_factory=list)
    media_type: ContentKind
    language: ContentLanguage
    popularity_score: float
    like_count: int
    template: SeoCatalogMemeTemplateRefRead | None = None
    primary_file: PublicMemeFileRead | None = None
    files: list[PublicMemeFileRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SeoCatalogMemePageRead(BaseModel):
    """Offset pagination envelope for public safe meme catalog rows."""

    items: list[SeoCatalogMemeRead]
    limit: int
    offset: int
    total: int
    has_more: bool


class SeoCatalogTagRead(BaseModel):
    """Public tag landing metadata derived from safe public meme tags."""

    slug: str
    title: str
    description: str | None = None
    meme_count: int
    updated_at: datetime | None = None


class SeoCatalogTagPageRead(BaseModel):
    """Offset pagination envelope for tag landing records."""

    items: list[SeoCatalogTagRead]
    limit: int
    offset: int
    total: int
    has_more: bool


class SeoCatalogTemplateRead(BaseModel):
    """Public template landing metadata derived from safe public memes."""

    slug: str
    name: str
    title: str
    description: str | None = None
    meme_count: int
    updated_at: datetime | None = None


class SeoCatalogTemplatePageRead(BaseModel):
    """Offset pagination envelope for template landing records."""

    items: list[SeoCatalogTemplateRead]
    limit: int
    offset: int
    total: int
    has_more: bool


__all__ = [
    "SeoCatalogMemePageRead",
    "SeoCatalogMemeRead",
    "SeoCatalogMemeTemplateRefRead",
    "SeoCatalogSummaryRead",
    "SeoCatalogTagPageRead",
    "SeoCatalogTagRead",
    "SeoCatalogTemplatePageRead",
    "SeoCatalogTemplateRead",
]
