# ruff: noqa: TC001,TC003
"""Schemas for the browser-admin API surface."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from memexpert.models.enums import ChannelSuggestionStatus, ContentKind, ContentLanguage, SourcePlatform
from memexpert.schemas.base import ORMSchema
from memexpert.schemas.user import ChannelSuggestionRead, UserRead

MAX_SOURCE_ID_LENGTH = 255
MAX_SOURCE_TITLE_LENGTH = 255
MAX_SOURCE_USERNAME_LENGTH = 255
MAX_ADMIN_NOTE_LENGTH = 2048
MAX_TEMPLATE_SLUG_LENGTH = 255
MAX_TEMPLATE_NAME_LENGTH = 255


class AdminSessionRead(BaseModel):
    """Current admin session projection returned to the SvelteKit guard."""

    user: UserRead


class AdminChannelSuggestionReviewRequest(BaseModel):
    """Approve/reject note payload for channel suggestions."""

    model_config = ConfigDict(extra="forbid")

    admin_note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("admin_note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AdminSourceChannelRead(ORMSchema):
    """Admin projection for curated source-channel rows."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    platform: SourcePlatform
    platform_id: str
    username: str | None
    title: str
    subscriber_count: int | None
    is_active: bool
    is_paused: bool
    catchup_enabled: bool
    catchup_message_limit: int
    session_id: str | None
    last_read_post_id: str | None
    last_fetched_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminSourceChannelCreateRequest(BaseModel):
    """Create one curated source channel for crawler pickup."""

    model_config = ConfigDict(extra="forbid")

    platform: SourcePlatform
    platform_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    username: str | None = Field(default=None, max_length=MAX_SOURCE_USERNAME_LENGTH)
    title: str = Field(min_length=1, max_length=MAX_SOURCE_TITLE_LENGTH)
    subscriber_count: int | None = Field(default=None, ge=0)
    session_id: str | None = Field(default=None, max_length=255)
    catchup_enabled: StrictBool = True
    catchup_message_limit: StrictInt = Field(default=500, ge=1, le=10000)

    @field_validator("platform_id", "title")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank.")
        return normalized

    @field_validator("username", "session_id")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AdminMemeTemplateRead(ORMSchema):
    """Admin-editable meme-template taxonomy row."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    is_curated: bool
    base_image_url: str | None
    text_regions: list[dict[str, object]] | None
    created_at: datetime
    updated_at: datetime


class AdminMemeTemplateUpdateRequest(BaseModel):
    """Partial update for meme template metadata."""

    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(default=None, min_length=1, max_length=MAX_TEMPLATE_SLUG_LENGTH)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_TEMPLATE_NAME_LENGTH)
    description: str | None = None
    is_curated: StrictBool | None = None
    base_image_url: str | None = None
    text_regions: list[dict[str, object]] | None = None

    @field_validator("slug", "name")
    @classmethod
    def _normalize_required_update_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank.")
        return normalized

    @field_validator("description", "base_image_url")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AdminMemeRead(ORMSchema):
    """Minimal admin meme moderation row.

    Current schema has no moderation queue/audit table, so moderation in this
    first slice is a direct override of durable ``memes.is_public`` and
    ``memes.is_nsfw`` fields.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    media_type: ContentKind
    language: ContentLanguage
    is_nsfw: bool
    is_public: bool
    popularity_score: float
    like_count: int
    tags: list[str]
    template_id: uuid.UUID | None
    author_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AdminMemeModerationUpdateRequest(BaseModel):
    """Direct moderation override for current meme fields."""

    model_config = ConfigDict(extra="forbid")

    is_nsfw: StrictBool | None = None
    is_public: StrictBool | None = None


__all__ = [
    "AdminChannelSuggestionReviewRequest",
    "AdminMemeModerationUpdateRequest",
    "AdminMemeRead",
    "AdminMemeTemplateRead",
    "AdminMemeTemplateUpdateRequest",
    "AdminSessionRead",
    "AdminSourceChannelCreateRequest",
    "AdminSourceChannelRead",
    "ChannelSuggestionRead",
    "ChannelSuggestionStatus",
]
