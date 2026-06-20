# ruff: noqa: TC001,TC003
"""Schemas for the browser-admin API surface."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, field_validator, model_validator

from memexpert.core.perceptual_hashes import (
    DEFAULT_PERCEPTUAL_HASH_ALGORITHM,
    MAX_PERCEPTUAL_HASH_HEX_LENGTH,
    normalize_hash_algorithm,
    normalize_perceptual_hash,
    perceptual_hash_bit_size,
)
from memexpert.models.enums import (
    ChannelSuggestionStatus,
    ContentKind,
    ContentLanguage,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.schemas._text import normalize_optional_text, normalize_required_text
from memexpert.schemas.base import ORMSchema
from memexpert.schemas.user import ChannelSuggestionRead, UserRead

MAX_SOURCE_ID_LENGTH = 255
MAX_SOURCE_TITLE_LENGTH = 255
MAX_SOURCE_USERNAME_LENGTH = 255
MAX_TELEGRAM_SESSION_NAME_LENGTH = 64
MAX_ADMIN_NOTE_LENGTH = 2048
MAX_TEMPLATE_SLUG_LENGTH = 255
MAX_TEMPLATE_NAME_LENGTH = 255
MAX_DESTRUCTIVE_CONFIRMATION_LENGTH = 128
MAX_HASH_ALGORITHM_LENGTH = 32
MAX_TELEGRAM_ACCOUNT_USERNAME_LENGTH = 255
MAX_TELEGRAM_ACCOUNT_PHONE_HINT_LENGTH = 64
MAX_ADMIN_TELEGRAM_ERROR_CLASS_LENGTH = 128
MAX_ADMIN_TELEGRAM_ERROR_TEXT_LENGTH = 4000


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
        return normalize_optional_text(value)


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
    live_enabled: bool
    engagement_enabled: bool
    catchup_message_limit: int
    telegram_session_id: uuid.UUID | None
    telegram_session_name: str | None
    is_orphaned: bool
    is_indexable: bool
    last_read_post_id: str | None
    last_fetched_at: datetime | None
    operational_status: Literal["active", "inactive", "paused"]
    freshness_status: Literal["checkpoint_only", "fresh", "never_fetched", "stale"]
    seconds_since_last_fetch: int | None
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
    telegram_session_id: uuid.UUID | None = None
    telegram_session_name: str | None = Field(default=None, max_length=MAX_TELEGRAM_SESSION_NAME_LENGTH)
    orphaned: StrictBool = False
    catchup_enabled: StrictBool = True
    live_enabled: StrictBool = True
    engagement_enabled: StrictBool = True
    catchup_message_limit: StrictInt = Field(default=500, ge=1, le=10000)

    @field_validator("platform_id", "title")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("username", "telegram_session_name")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def _check_assignment_shape(self) -> AdminSourceChannelCreateRequest:
        if self.orphaned and (self.telegram_session_id is not None or self.telegram_session_name is not None):
            raise ValueError("orphaned source channels cannot also specify a Telegram session target.")
        if self.telegram_session_id is not None and self.telegram_session_name is not None:
            raise ValueError("Specify telegram_session_id or telegram_session_name, not both.")
        return self


class AdminSourceChannelUpdateRequest(BaseModel):
    """Patch source-channel crawling/indexing controls."""

    model_config = ConfigDict(extra="forbid")

    catchup_enabled: StrictBool | None = None
    live_enabled: StrictBool | None = None
    engagement_enabled: StrictBool | None = None
    catchup_message_limit: StrictInt | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def _require_patch_field(self) -> AdminSourceChannelUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one source-channel field must be supplied.")
        return self


class AdminSourceChannelAssignRequest(BaseModel):
    """Move one source channel to a concrete DB-backed Telegram session."""

    model_config = ConfigDict(extra="forbid")

    telegram_session_id: uuid.UUID
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminSourceChannelOrphanRequest(BaseModel):
    """Explicitly orphan a source channel and disable all crawler controls."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminTelegramSessionRead(ORMSchema):
    """Secret-free admin projection for DB-backed Telegram sessions."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str
    owned_channel_count: int = Field(ge=0)
    status: TelegramSessionStatus
    enabled: bool
    flood_wait_until: datetime | None
    live_listener_started_at: datetime | None
    last_heartbeat_at: datetime | None
    last_error_class: str | None
    last_error_text: str | None
    quarantined_at: datetime | None
    live_enabled: bool
    catchup_enabled: bool
    engagement_enabled: bool
    max_requests_per_second: float = Field(gt=0)
    account_user_id: int | None
    account_username: str | None
    account_phone_hint: str | None
    has_string_session: bool
    created_at: datetime
    updated_at: datetime


class AdminTelegramSessionCreateRequest(BaseModel):
    """Create/import a DB-backed Telegram session row without returning secret material."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=MAX_TELEGRAM_SESSION_NAME_LENGTH)
    display_name: str | None = Field(default=None, max_length=MAX_SOURCE_TITLE_LENGTH)
    string_session: SecretStr | None = None
    validate_session: StrictBool = Field(default=False, alias="validate")
    enabled: StrictBool = True
    live_enabled: StrictBool = True
    catchup_enabled: StrictBool = True
    engagement_enabled: StrictBool = True
    max_requests_per_second: float = Field(default=1.0, gt=0)
    account_user_id: int | None = Field(default=None, ge=1)
    account_username: str | None = Field(default=None, max_length=MAX_TELEGRAM_ACCOUNT_USERNAME_LENGTH)
    account_phone_hint: str | None = Field(default=None, max_length=MAX_TELEGRAM_ACCOUNT_PHONE_HINT_LENGTH)
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @field_validator("account_username", "account_phone_hint", "note")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @field_validator("string_session")
    @classmethod
    def _normalize_string_session(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw_value = value.get_secret_value().strip()
        if not raw_value:
            raise ValueError("string_session must not be blank when provided.")
        return SecretStr(raw_value)


class AdminTelegramSessionUpdateRequest(BaseModel):
    """Patch session status and crawler policy toggles."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=MAX_SOURCE_TITLE_LENGTH)
    enabled: StrictBool | None = None
    status: TelegramSessionStatus | None = None
    live_enabled: StrictBool | None = None
    catchup_enabled: StrictBool | None = None
    engagement_enabled: StrictBool | None = None
    max_requests_per_second: float | None = Field(default=None, gt=0)
    flood_wait_until: datetime | None = None
    last_error_class: str | None = Field(default=None, max_length=MAX_ADMIN_TELEGRAM_ERROR_CLASS_LENGTH)
    last_error_text: str | None = Field(default=None, max_length=MAX_ADMIN_TELEGRAM_ERROR_TEXT_LENGTH)
    clear_error: StrictBool = False
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @field_validator("last_error_class", "last_error_text", "note")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def _require_patch_field(self) -> AdminTelegramSessionUpdateRequest:
        if not (self.model_fields_set - {"note"}):
            raise ValueError("At least one Telegram session field must be supplied.")
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be null.")
        return self


class AdminTelegramSessionValidateRequest(BaseModel):
    """Validate a stored Telegram StringSession and optionally check a source channel."""

    model_config = ConfigDict(extra="forbid")

    source_channel_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminTelegramSessionValidateRead(BaseModel):
    """Result of a successful stored-session validation."""

    model_config = ConfigDict(extra="forbid")

    telegram_session: AdminTelegramSessionRead
    channel_checked: bool
    channel_reference: str | None = None


class AdminTelegramSessionDeleteRequest(BaseModel):
    """Explicit confirmation payload for deleting a Telegram session."""

    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(min_length=1, max_length=MAX_DESTRUCTIVE_CONFIRMATION_LENGTH)
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("confirmation")
    @classmethod
    def _normalize_confirmation(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminTelegramSessionActionRead(BaseModel):
    """Result of a Telegram session destructive/admin action."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["delete"]
    telegram_session_id: uuid.UUID
    orphaned_source_channel_count: int = Field(ge=0)
    message: str


class AdminTelegramChannelGroupRead(BaseModel):
    """Channels grouped by assigned Telegram session plus the orphaned group."""

    model_config = ConfigDict(extra="forbid")

    telegram_session: AdminTelegramSessionRead | None
    is_orphaned: bool
    channels: list[AdminSourceChannelRead]


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


class AdminMemeTemplateCreateRequest(BaseModel):
    """Create a meme template taxonomy row."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=MAX_TEMPLATE_SLUG_LENGTH)
    name: str = Field(min_length=1, max_length=MAX_TEMPLATE_NAME_LENGTH)
    description: str | None = None
    is_curated: StrictBool = False
    base_image_url: str | None = None
    text_regions: list[dict[str, object]] | None = None

    @field_validator("slug", "name")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("description", "base_image_url")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


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
        return normalize_required_text(value)

    @field_validator("description", "base_image_url")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminMemeTemplateMergeRequest(BaseModel):
    """Merge one duplicate template into a target template."""

    model_config = ConfigDict(extra="forbid")

    target_template_id: uuid.UUID
    confirmation: str = Field(min_length=1, max_length=MAX_DESTRUCTIVE_CONFIRMATION_LENGTH)
    note: str = Field(min_length=1, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("confirmation", "note")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        return normalize_required_text(value)


class AdminMemeTemplateDeleteRequest(BaseModel):
    """Explicit confirmation payload for safe template deletion."""

    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(min_length=1, max_length=MAX_DESTRUCTIVE_CONFIRMATION_LENGTH)
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("confirmation")
    @classmethod
    def _normalize_confirmation(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminMemeTemplateActionRead(BaseModel):
    """Result of a safe meme-template admin action."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["delete", "merge"]
    source_template_id: uuid.UUID
    target_template_id: uuid.UUID | None
    affected_meme_count: int
    message: str


class AdminBlockedPerceptualHashRead(ORMSchema):
    """Admin projection for a blocked perceptual-hash pattern."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    perceptual_hash: str
    hash_algorithm: str
    hash_size: int
    max_hamming_distance: int
    reason: ModerationReason
    note: str | None
    is_active: bool
    created_by_admin_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AdminBlockedPerceptualHashCreateRequest(BaseModel):
    """Create a durable blocked perceptual-hash pattern."""

    model_config = ConfigDict(extra="forbid")

    perceptual_hash: str = Field(min_length=1, max_length=MAX_PERCEPTUAL_HASH_HEX_LENGTH)
    hash_algorithm: str = Field(
        default=DEFAULT_PERCEPTUAL_HASH_ALGORITHM,
        min_length=1,
        max_length=MAX_HASH_ALGORITHM_LENGTH,
    )
    hash_size: StrictInt | None = Field(default=None, ge=1)
    max_hamming_distance: StrictInt = Field(default=0, ge=0)
    reason: ModerationReason = ModerationReason.OTHER
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)
    is_active: StrictBool = True

    @field_validator("perceptual_hash")
    @classmethod
    def _normalize_perceptual_hash(cls, value: str) -> str:
        return normalize_perceptual_hash(value)

    @field_validator("hash_algorithm")
    @classmethod
    def _normalize_hash_algorithm(cls, value: str) -> str:
        normalized = normalize_hash_algorithm(value)
        if normalized != DEFAULT_PERCEPTUAL_HASH_ALGORITHM:
            raise ValueError("Only the phash perceptual hash algorithm is currently enforced by ingest.")
        return normalized

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def _derive_and_check_hash_size(self) -> AdminBlockedPerceptualHashCreateRequest:
        derived_size = perceptual_hash_bit_size(self.perceptual_hash)
        if self.hash_size is None:
            self.hash_size = derived_size
        if self.hash_size != derived_size:
            raise ValueError("hash_size must match perceptual_hash bit length.")
        if self.max_hamming_distance > self.hash_size:
            raise ValueError("max_hamming_distance cannot exceed hash_size.")
        return self


class AdminBlockedPerceptualHashUpdateRequest(BaseModel):
    """Partial update for a blocked perceptual-hash pattern."""

    model_config = ConfigDict(extra="forbid")

    perceptual_hash: str | None = Field(default=None, min_length=1, max_length=MAX_PERCEPTUAL_HASH_HEX_LENGTH)
    hash_algorithm: str | None = Field(default=None, min_length=1, max_length=MAX_HASH_ALGORITHM_LENGTH)
    hash_size: StrictInt | None = Field(default=None, ge=1)
    max_hamming_distance: StrictInt | None = Field(default=None, ge=0)
    reason: ModerationReason | None = None
    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)
    is_active: StrictBool | None = None

    @field_validator("perceptual_hash")
    @classmethod
    def _normalize_perceptual_hash(cls, value: str | None) -> str | None:
        return None if value is None else normalize_perceptual_hash(value)

    @field_validator("hash_algorithm")
    @classmethod
    def _normalize_hash_algorithm(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_hash_algorithm(value)
        if normalized != DEFAULT_PERCEPTUAL_HASH_ALGORITHM:
            raise ValueError("Only the phash perceptual hash algorithm is currently enforced by ingest.")
        return normalized

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def _check_inline_hash_size(self) -> AdminBlockedPerceptualHashUpdateRequest:
        if self.perceptual_hash is not None:
            derived_size = perceptual_hash_bit_size(self.perceptual_hash)
            if self.hash_size is None:
                self.hash_size = derived_size
            if self.hash_size != derived_size:
                raise ValueError("hash_size must match perceptual_hash bit length.")
        return self


class AdminBlockedPerceptualHashDeactivateRequest(BaseModel):
    """Optional audit note for blocked pHash deactivation."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


class AdminBlockedPerceptualHashActionRead(BaseModel):
    """Result of a safe blocked pHash admin action."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["deactivate", "delete"]
    blocked_perceptual_hash_id: uuid.UUID
    matched_meme_file_count: int
    message: str


class AdminBlockedPerceptualHashAuditRead(ORMSchema):
    """Immutable blocked pHash lifecycle audit row."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    blocked_perceptual_hash_id: uuid.UUID
    admin_user_id: uuid.UUID | None
    action: str
    previous_values: dict[str, object]
    new_values: dict[str, object]
    note: str | None
    created_at: datetime


class AdminMemeRead(ORMSchema):
    """Minimal admin meme moderation row."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    media_type: ContentKind
    language: ContentLanguage
    is_nsfw: bool
    is_public: bool
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
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def _require_flag_change(self) -> AdminMemeModerationUpdateRequest:
        if self.is_nsfw is None and self.is_public is None and "template_id" not in self.model_fields_set:
            raise ValueError("At least one moderation field must be supplied.")
        return self


class AdminMemeDeleteRequest(BaseModel):
    """Explicit confirmation payload for irreversible meme deletion."""

    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(min_length=1, max_length=MAX_DESTRUCTIVE_CONFIRMATION_LENGTH)
    note: str = Field(min_length=1, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("confirmation", "note")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        return normalize_required_text(value)


class AdminMemeMergeRequest(BaseModel):
    """Explicit target and confirmation payload for irreversible meme merging."""

    model_config = ConfigDict(extra="forbid")

    target_meme_id: uuid.UUID
    confirmation: str = Field(min_length=1, max_length=MAX_DESTRUCTIVE_CONFIRMATION_LENGTH)
    note: str = Field(min_length=1, max_length=MAX_ADMIN_NOTE_LENGTH)

    @field_validator("confirmation", "note")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        return normalize_required_text(value)


class AdminMemeDestructiveActionRead(BaseModel):
    """Result of an audited admin destructive meme action."""

    model_config = ConfigDict(extra="forbid")

    action: str
    source_meme_id: uuid.UUID
    target_meme_id: uuid.UUID | None
    audit_log_id: uuid.UUID
    affected_snapshot: dict[str, object]
    message: str


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
        return normalize_optional_text(value)


class AdminMemeDetailRead(BaseModel):
    """Admin meme detail bundle for the browser management page."""

    model_config = ConfigDict(extra="forbid")

    meme: AdminMemeRead
    reports: list[AdminModerationReportRead]
    decisions: list[AdminModerationDecisionRead]


__all__ = [
    "AdminBlockedPerceptualHashActionRead",
    "AdminBlockedPerceptualHashAuditRead",
    "AdminBlockedPerceptualHashCreateRequest",
    "AdminBlockedPerceptualHashDeactivateRequest",
    "AdminBlockedPerceptualHashRead",
    "AdminBlockedPerceptualHashUpdateRequest",
    "AdminChannelSuggestionReviewRequest",
    "AdminMemeTemplateActionRead",
    "AdminMemeTemplateCreateRequest",
    "AdminMemeTemplateDeleteRequest",
    "AdminMemeTemplateMergeRequest",
    "AdminMemeDeleteRequest",
    "AdminMemeDetailRead",
    "AdminMemeDestructiveActionRead",
    "AdminMemeMergeRequest",
    "AdminMemeModerationUpdateRequest",
    "AdminMemeRead",
    "AdminMemeTemplateRead",
    "AdminMemeTemplateUpdateRequest",
    "AdminModerationDecisionRead",
    "AdminModerationReportRead",
    "AdminModerationReportResolveRequest",
    "AdminSessionRead",
    "AdminSourceChannelAssignRequest",
    "AdminSourceChannelCreateRequest",
    "AdminSourceChannelOrphanRequest",
    "AdminSourceChannelRead",
    "AdminSourceChannelUpdateRequest",
    "AdminTelegramChannelGroupRead",
    "AdminTelegramSessionActionRead",
    "AdminTelegramSessionCreateRequest",
    "AdminTelegramSessionDeleteRequest",
    "AdminTelegramSessionRead",
    "AdminTelegramSessionUpdateRequest",
    "AdminTelegramSessionValidateRead",
    "AdminTelegramSessionValidateRequest",
    "ChannelSuggestionRead",
    "ChannelSuggestionStatus",
]
