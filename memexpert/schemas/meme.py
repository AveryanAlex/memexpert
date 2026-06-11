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
    created_at: datetime
    updated_at: datetime


class MemeDetailRead(MemeCardRead):
    """Detailed meme DTO for read screens and bot send confirmation flows."""

    ocr_text: str | None
    is_public: bool
    author_user_id: uuid.UUID | None
    seo_page_slug: str | None
    seo_title: str | None
    seo_description: str | None
    files: list[MemeFileRead] = Field(default_factory=list)


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


class PublicMemeFileRead(BaseModel):
    """Safe public file metadata without internal storage object keys."""

    id: uuid.UUID
    mime_type: str | None
    width: int | None
    height: int | None
    file_size_bytes: int | None
    blur_hash: str | None
    quality_score: float


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
    created_at: datetime
    updated_at: datetime


class PublicMemeDetailRead(PublicMemeCardRead):
    """Safe public meme detail DTO without owner or storage internals."""

    ocr_text: str | None
    seo_page_slug: str | None
    seo_title: str | None
    seo_description: str | None
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


__all__ = [
    "MemeCardRead",
    "MemeDetailRead",
    "MemeFileRead",
    "MemeSearchPageRead",
    "MemeSearchResultRead",
    "MemeSearchScoreRead",
    "PublicMemeCardRead",
    "PublicMemeDetailRead",
    "PublicMemeFileRead",
    "PublicMemeSearchPageRead",
    "PublicMemeSearchResultRead",
]
