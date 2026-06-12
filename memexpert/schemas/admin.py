# ruff: noqa: TC001,TC003
"""Schemas for the browser-admin API surface."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from memexpert.models.enums import (
    ChannelSuggestionStatus,
    ContentKind,
    ContentLanguage,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    SourcePlatform,
)
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
    """Minimal admin meme moderation row."""

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
    """Audited direct override for current meme moderation and template fields."""

    model_config = ConfigDict(extra="forbid")

    is_nsfw: StrictBool | None = None
    is_public: StrictBool | None = None
    template_id: uuid.UUID | None = None
    reason: ModerationReason | None = None
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def _require_flag_change(self) -> AdminMemeModerationUpdateRequest:
        if self.is_nsfw is None and self.is_public is None and "template_id" not in self.model_fields_set:
            raise ValueError("At least one moderation field must be supplied.")
        return self


class AdminModerationDecisionRead(ORMSchema):
    """Admin-visible immutable moderation audit record."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    meme_id: uuid.UUID
    report_id: uuid.UUID | None
    admin_user_id: uuid.UUID | None
    action: ModerationAction
    reason: ModerationReason | None
    note: str | None
    previous_is_public: bool
    previous_is_nsfw: bool
    new_is_public: bool
    new_is_nsfw: bool
    previous_template_id: uuid.UUID | None
    new_template_id: uuid.UUID | None
    created_at: datetime


class AdminModerationReportRead(ORMSchema):
    """Admin queue projection for open and historical reports."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    meme_id: uuid.UUID
    reporter_user_id: uuid.UUID | None
    status: ModerationReportStatus
    reason: ModerationReason
    note: str | None
    resolved_by_admin_user_id: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    meme: AdminMemeRead


class AdminModerationReportResolveRequest(BaseModel):
    """Resolve a report and create the corresponding decision audit record."""

    model_config = ConfigDict(extra="forbid")

    action: ModerationAction
    reason: ModerationReason | None = None
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class AdminMemeDetailRead(BaseModel):
    """Admin meme detail bundle for the browser management page."""

    model_config = ConfigDict(extra="forbid")

    meme: AdminMemeRead
    reports: list[AdminModerationReportRead]
    decisions: list[AdminModerationDecisionRead]


__all__ = [
    "AdminChannelSuggestionReviewRequest",
    "AdminMemeDetailRead",
    "AdminMemeModerationUpdateRequest",
    "AdminMemeRead",
    "AdminMemeTemplateRead",
    "AdminMemeTemplateUpdateRequest",
    "AdminModerationDecisionRead",
    "AdminModerationReportRead",
    "AdminModerationReportResolveRequest",
    "AdminSessionRead",
    "AdminSourceChannelCreateRequest",
    "AdminSourceChannelRead",
    "ChannelSuggestionRead",
    "ChannelSuggestionStatus",
]
