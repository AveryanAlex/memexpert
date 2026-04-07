# ruff: noqa: TC001,TC003
"""Typed content-pipeline request, response, and broker-contract schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus, ContentSourceKind, SourcePlatform

MAX_OBJECT_KEY_LENGTH = 1024
MAX_PIPELINE_REASON_LENGTH = 128
MAX_PIPELINE_ERROR_LENGTH = 4000
MAX_SOURCE_ID_LENGTH = 255
MAX_POST_ID_LENGTH = 255


class ContentPipelineEventType(StrEnum):
    """Machine-readable broker event names used by the content pipeline."""

    MEME_CREATED = "meme_created"
    STAGE_REPLAY_REQUESTED = "stage_replay_requested"


class ContentPipelineErrorCode(StrEnum):
    """Machine-readable pipeline error codes returned by operator-facing routes."""

    INVALID_OPERATOR_TOKEN = "invalid_operator_token"
    ITEM_NOT_FOUND = "pipeline_item_not_found"
    PAYLOAD_INVALID = "pipeline_payload_invalid"
    PAYLOAD_TOO_LARGE = "pipeline_payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "pipeline_unsupported_media_type"
    SOURCE_CONFLICT = "pipeline_source_conflict"
    STORAGE_FAILURE = "pipeline_storage_failure"
    INGEST_FAILURE = "pipeline_ingest_failure"
    PUBLISH_FAILURE = "pipeline_publish_failure"


class ContentPipelineErrorResponse(BaseModel):
    """Machine-readable content-pipeline error payload used by HTTP routes."""

    code: ContentPipelineErrorCode
    detail: str


class ContentPipelineUploadMetadata(BaseModel):
    """Operator-supplied provenance metadata accepted alongside an uploaded file."""

    model_config = ConfigDict(extra="forbid")

    source_platform: SourcePlatform
    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    post_id: str = Field(min_length=1, max_length=MAX_POST_ID_LENGTH)
    views: StrictInt = Field(default=0, ge=0)

    @field_validator("source_id", "post_id")
    @classmethod
    def _normalize_required_source_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("source provenance fields must not be blank.")
        return normalized_value


class ContentPipelineDispatchEvent(BaseModel):
    """Thin, durable broker payload for content-pipeline stage dispatches."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: ContentPipelineEventType
    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    stage: ContentPipelineStage
    source_kind: ContentSourceKind
    original_object_key: str = Field(min_length=1, max_length=MAX_OBJECT_KEY_LENGTH)
    attempt: StrictInt = Field(ge=1)
    created_at: datetime

    @field_validator("original_object_key")
    @classmethod
    def _normalize_original_object_key(cls, value: str) -> str:
        normalized_value = value.strip().lstrip("/")
        if not normalized_value:
            raise ValueError("original_object_key must not be blank.")
        return normalized_value


class ContentPipelineStageJournalRead(BaseModel):
    """Public read model for a single stage-journal row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meme_file_id: uuid.UUID
    stage: ContentPipelineStage
    status: ContentPipelineStageStatus
    attempt_count: StrictInt = Field(ge=0)
    last_event_id: uuid.UUID | None = None
    normalized_reason: str | None = Field(default=None, max_length=MAX_PIPELINE_REASON_LENGTH)
    last_error_text: str | None = Field(default=None, max_length=MAX_PIPELINE_ERROR_LENGTH)
    is_retryable: bool
    retry_after: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ContentPipelineItemRead(BaseModel):
    """Read model for item-level inspect responses and list entries."""

    model_config = ConfigDict(extra="forbid")

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    current_stage: ContentPipelineStage
    current_status: ContentPipelineStageStatus
    original_object_key: str = Field(min_length=1, max_length=MAX_OBJECT_KEY_LENGTH)
    web_video_object_key: str | None = Field(default=None, max_length=MAX_OBJECT_KEY_LENGTH)
    last_event_id: uuid.UUID | None = None
    normalized_reason: str | None = Field(default=None, max_length=MAX_PIPELINE_REASON_LENGTH)
    last_error_text: str | None = Field(default=None, max_length=MAX_PIPELINE_ERROR_LENGTH)
    attempt_count: StrictInt = Field(ge=0)
    stages: tuple[ContentPipelineStageJournalRead, ...]


class ContentPipelineUploadRead(ContentPipelineItemRead):
    """Create-response payload returned after an operator upload is ingested."""


class ContentPipelineReplayRequest(BaseModel):
    """Replay request payload accepted by the operator-only pipeline surface."""

    model_config = ConfigDict(extra="forbid")

    stage: ContentPipelineStage | None = None


class ContentPipelineReplayAccepted(BaseModel):
    """Replay response payload confirming which stage was republished."""

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID
    replay_event_id: uuid.UUID
    stage: ContentPipelineStage
    attempt: StrictInt = Field(ge=1)


__all__ = [
    "ContentPipelineDispatchEvent",
    "ContentPipelineErrorCode",
    "ContentPipelineErrorResponse",
    "ContentPipelineEventType",
    "ContentPipelineItemRead",
    "ContentPipelineReplayAccepted",
    "ContentPipelineReplayRequest",
    "ContentPipelineStageJournalRead",
    "ContentPipelineUploadMetadata",
    "ContentPipelineUploadRead",
    "MAX_OBJECT_KEY_LENGTH",
    "MAX_PIPELINE_ERROR_LENGTH",
    "MAX_PIPELINE_REASON_LENGTH",
    "MAX_POST_ID_LENGTH",
    "MAX_SOURCE_ID_LENGTH",
]
