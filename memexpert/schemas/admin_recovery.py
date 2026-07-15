# ruff: noqa: TC001,TC003
"""Typed contracts for cookie-authenticated operational recovery."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from memexpert.models.enums import (
    ContentPipelineStage,
    RecoveryBucket,
    RecoveryCapability,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryWorkKind,
    SyncTargetKind,
)
from memexpert.schemas._text import normalize_required_text


class RecoverySummaryRead(BaseModel):
    retryable_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    stuck_count: int = Field(ge=0)
    dead_lettered_count: int = Field(ge=0)


class RecoveryWorkRead(BaseModel):
    kind: RecoveryWorkKind
    id: str
    bucket: RecoveryBucket
    title: str
    source_label: str | None = None
    source_channel_id: uuid.UUID | None = None
    post_id: str | None = None
    meme_file_id: uuid.UUID | None = None
    stage: ContentPipelineStage | None = None
    target: SyncTargetKind | None = None
    status: str
    reason: str | None = None
    safe_error: str | None = None
    error_code: str | None = None
    is_retryable: bool
    attempt_count: int = Field(ge=0)
    occurred_at: datetime
    next_attempt_at: datetime | None = None
    version: str
    capabilities: list[RecoveryCapability] = Field(default_factory=list)
    blocked_reason: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class RecoveryWorkPageRead(BaseModel):
    items: list[RecoveryWorkRead]
    next_cursor: str | None = None
    snapshot_at: datetime


class RecoveryMutationRequest(BaseModel):
    request_id: uuid.UUID
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)
    capability: RecoveryCapability

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryTargetMutationRequest(BaseModel):
    request_id: uuid.UUID
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryWorkReference(BaseModel):
    kind: RecoveryWorkKind
    id: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=255)


class RecoveryBatchPreviewRequest(BaseModel):
    request_id: uuid.UUID
    capability: RecoveryCapability
    reason: str = Field(min_length=3, max_length=500)
    items: list[RecoveryWorkReference] = Field(min_length=1, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryBatchScheduleRequest(BaseModel):
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryBatchCancelRequest(BaseModel):
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryJobItemRead(BaseModel):
    id: uuid.UUID
    work_kind: RecoveryWorkKind
    work_id: str
    action: RecoveryCapability
    status: RecoveryJobItemStatus
    normalized_reason: str | None = None
    safe_error: str | None = None
    dispatched_at: datetime | None = None
    finished_at: datetime | None = None


class RecoveryJobRead(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    status: RecoveryJobStatus
    action: RecoveryCapability
    reason: str
    total_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    expires_at: datetime | None = None
    scheduled_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    version: str
    items: list[RecoveryJobItemRead] = Field(default_factory=list)


class AdminSourceBackfillRead(BaseModel):
    id: uuid.UUID
    source_channel_id: uuid.UUID
    status: str
    requested_count: int = Field(ge=1)
    scanned_count: int = Field(ge=0)
    remaining_count: int = Field(ge=0)
    cursor_post_id: str | None = None
    attempt_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    last_error_code: str | None = None
    last_error_class: str | None = None
    safe_error: str | None = None
    is_retryable: bool
    next_attempt_at: datetime | None = None
    last_progress_at: datetime | None = None
    telegram_session_id: uuid.UUID | None = None
    telegram_session_name: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime
    version: str
    capabilities: list[RecoveryCapability] = Field(default_factory=list)


class AdminSourceBackfillPageRead(BaseModel):
    items: list[AdminSourceBackfillRead]


__all__ = [
    "AdminSourceBackfillPageRead",
    "AdminSourceBackfillRead",
    "RecoveryBatchCancelRequest",
    "RecoveryBatchPreviewRequest",
    "RecoveryBatchScheduleRequest",
    "RecoveryJobItemRead",
    "RecoveryJobRead",
    "RecoveryMutationRequest",
    "RecoverySummaryRead",
    "RecoveryTargetMutationRequest",
    "RecoveryWorkPageRead",
    "RecoveryWorkRead",
    "RecoveryWorkReference",
]
