# ruff: noqa: TC001,TC003
"""Public schemas for raw ingest-request acceptance and operator reads."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from memexpert.models.enums import PipelineIngestRequestStatus, SourceAttachReason, SourcePlatform
from memexpert.schemas.base import ORMSchema
from memexpert.schemas.pipeline_base import (
    MAX_OBJECT_KEY_LENGTH,
    MAX_PIPELINE_ERROR_LENGTH,
    MAX_PIPELINE_REASON_LENGTH,
    MAX_POST_ID_LENGTH,
    MAX_SOURCE_ID_LENGTH,
    MAX_TELEGRAM_CONTENT_TYPE_LENGTH,
    MAX_TELEGRAM_FILENAME_LENGTH,
)


class IngestAcceptOutcome(StrEnum):
    """Acceptance outcomes used by routes to choose the HTTP status code."""

    ACCEPTED_ASYNC = "accepted_async"
    RESOLVED_SHA_DUPLICATE = "resolved_sha_duplicate"
    SOURCE_REPLAY = "source_replay"


class IngestAcceptSource(BaseModel):
    """Source identity and lightweight metadata supplied with raw bytes."""

    model_config = ConfigDict(extra="forbid")

    source_platform: SourcePlatform
    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    post_id: str = Field(min_length=1, max_length=MAX_POST_ID_LENGTH)
    owner_user_id: uuid.UUID | None = None
    views: StrictInt = Field(default=0, ge=0)
    user_metadata: dict[str, object] = Field(default_factory=dict)
    source_metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("source_id", "post_id")
    @classmethod
    def _normalize_required_source_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("source provenance fields must not be blank.")
        return normalized_value


class IngestRequestRead(ORMSchema):
    """Operator/API projection of a raw ingest request row."""

    id: uuid.UUID
    source_platform: SourcePlatform
    source_id: str
    post_id: str
    owner_user_id: uuid.UUID | None = None
    user_metadata: dict[str, object] = Field(default_factory=dict)
    source_metadata: dict[str, object] = Field(default_factory=dict)
    declared_filename: str | None = Field(default=None, max_length=MAX_TELEGRAM_FILENAME_LENGTH)
    declared_content_type: str | None = Field(default=None, max_length=MAX_TELEGRAM_CONTENT_TYPE_LENGTH)
    temp_original_object_key: str | None = Field(default=None, max_length=MAX_OBJECT_KEY_LENGTH)
    sha256_hex: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    file_size_bytes: StrictInt | None = Field(default=None, ge=0)
    status: PipelineIngestRequestStatus
    failure_code: str | None = Field(default=None, max_length=MAX_PIPELINE_REASON_LENGTH)
    failure_detail: str | None = Field(default=None, max_length=MAX_PIPELINE_ERROR_LENGTH)
    attempt_count: StrictInt = Field(ge=0)
    locked_at: datetime | None = None
    materialized_meme_id: uuid.UUID | None = None
    materialized_meme_file_id: uuid.UUID | None = None
    matched_meme_file_id: uuid.UUID | None = None
    source_attach_reason: SourceAttachReason | None = None
    created_at: datetime
    updated_at: datetime


class IngestAcceptResult(BaseModel):
    """Service result carrying the read model plus route-level outcome."""

    model_config = ConfigDict(extra="forbid")

    ingest_request: IngestRequestRead
    outcome: IngestAcceptOutcome


__all__ = [
    "IngestAcceptOutcome",
    "IngestAcceptResult",
    "IngestAcceptSource",
    "IngestRequestRead",
]
