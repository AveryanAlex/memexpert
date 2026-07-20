# ruff: noqa: TC001,TC003
"""Typed contracts for cookie-authenticated Replay & Repair operations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from memexpert.models.enums import (
    ContentPipelineStage,
    RecoveryBucket,
    RecoveryCapability,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
    SyncTargetKind,
)
from memexpert.schemas._text import normalize_required_text

RecoveryRetryLimit = Literal[1, 3, 5]
RecoveryAcknowledgement = Literal["terminal_override", "stale_dependents", "media_replacement"]
RecoverySelectorType = Literal["explicit", "query"]


def _default_retry_limits() -> list[RecoveryRetryLimit]:
    return [1, 3, 5]


class _RecoverySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoverySummaryRead(_RecoverySchema):
    retryable_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    stuck_count: int = Field(ge=0)
    dead_lettered_count: int = Field(ge=0)
    outdated_web_video_count: int = Field(ge=0)


class RecoveryWorkRead(_RecoverySchema):
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
    actions: list[RecoveryActionRead] = Field(default_factory=list)
    blocked_reason: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class RecoveryWorkPageRead(_RecoverySchema):
    items: list[RecoveryWorkRead]
    next_cursor: str | None = None
    snapshot_at: datetime


class RecoveryActionScopeRequirementsRead(_RecoverySchema):
    """Effective operator-facing requirements for one replay scope."""

    warnings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    required_acknowledgements: list[RecoveryAcknowledgement] = Field(default_factory=list)


class RecoveryActionRead(_RecoverySchema):
    capability: RecoveryCapability
    available: bool
    scopes: list[RecoveryReplayScope] = Field(default_factory=list)
    default_scope: RecoveryReplayScope | None = None
    retry_limits: list[RecoveryRetryLimit] = Field(default_factory=_default_retry_limits)
    default_retry_limit: RecoveryRetryLimit = 3
    downstream_stages: list[ContentPipelineStage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    required_acknowledgements: list[RecoveryAcknowledgement] = Field(default_factory=list)
    scope_requirements: dict[RecoveryReplayScope, RecoveryActionScopeRequirementsRead] = Field(
        default_factory=dict
    )
    blocked_prerequisites: list[str] = Field(default_factory=list)


class RecoveryMediaProfileRead(_RecoverySchema):
    profile: str | None = None
    verified_at: datetime | None = None
    source_has_audio: bool | None = None
    web_video_has_audio: bool | None = None
    outdated: bool = False


class RecoveryActiveJobRead(_RecoverySchema):
    id: uuid.UUID
    status: RecoveryJobStatus
    requested_by_admin_user_id: uuid.UUID
    assigned_admin_user_id: uuid.UUID | None = None
    action: RecoveryCapability
    scope: RecoveryReplayScope
    created_at: datetime


class RecoveryCandidateRead(_RecoverySchema):
    work: RecoveryWorkRead
    actions: list[RecoveryActionRead]
    warnings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    media_profile: RecoveryMediaProfileRead | None = None
    active_job: RecoveryActiveJobRead | None = None


class RecoveryMutationRequest(_RecoverySchema):
    """Compatibility request accepted by the historical ``/retry`` route."""

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


class RecoveryActionRequest(_RecoverySchema):
    request_id: uuid.UUID
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)
    action: RecoveryCapability = Field(
        validation_alias=AliasChoices("action", "capability"),
        serialization_alias="action",
    )
    scope: RecoveryReplayScope = RecoveryReplayScope.STAGE_ONLY
    retry_limit: RecoveryRetryLimit = 3
    acknowledgements: list[RecoveryAcknowledgement] = Field(default_factory=list)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)

    @field_validator("acknowledgements")
    @classmethod
    def acknowledgements_are_unique(
        cls,
        values: list[RecoveryAcknowledgement],
    ) -> list[RecoveryAcknowledgement]:
        if len(values) != len(set(values)):
            raise ValueError("acknowledgements must not contain duplicates")
        return values


class RecoveryTargetMutationRequest(_RecoverySchema):
    request_id: uuid.UUID
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryWorkReference(_RecoverySchema):
    kind: RecoveryWorkKind
    id: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=255)


class RecoveryExplicitSelector(_RecoverySchema):
    type: Literal["explicit"] = "explicit"
    items: list[RecoveryWorkReference] = Field(min_length=1, max_length=1000)


class RecoveryQueryFilters(_RecoverySchema):
    bucket: RecoveryBucket | None = None
    kind: RecoveryWorkKind | None = None
    source_channel_id: uuid.UUID | None = None
    stage: ContentPipelineStage | None = None
    reason: str | None = Field(default=None, max_length=128)
    query: str | None = Field(default=None, max_length=255)
    outdated_web_video: bool = False
    successful_stage: bool = False

    @field_validator("reason", "query", mode="before")
    @classmethod
    def normalize_optional_filter(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = " ".join(value.split())
        return normalized or None

    @model_validator(mode="after")
    def specialized_selectors_are_not_broadened_by_ignored_filters(self) -> RecoveryQueryFilters:
        if self.outdated_web_video and self.successful_stage:
            raise ValueError("outdated_web_video and successful_stage are mutually exclusive")
        if self.outdated_web_video and any(
            value is not None
            for value in (
                self.bucket,
                self.kind,
                self.source_channel_id,
                self.stage,
                self.reason,
                self.query,
            )
        ):
            raise ValueError("outdated_web_video cannot be combined with other recovery filters")
        if self.successful_stage:
            if self.stage is None or self.stage is ContentPipelineStage.INGEST:
                raise ValueError("successful_stage requires one non-Ingest stage")
            if any(
                value is not None
                for value in (
                    self.bucket,
                    self.kind,
                    self.source_channel_id,
                    self.reason,
                    self.query,
                )
            ):
                raise ValueError("successful_stage cannot be combined with other recovery filters")
        return self


class RecoveryQuerySelector(_RecoverySchema):
    type: Literal["query"] = "query"
    filters: RecoveryQueryFilters = Field(default_factory=RecoveryQueryFilters)
    snapshot_at: datetime | None = None

    @field_validator("snapshot_at")
    @classmethod
    def snapshot_is_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("snapshot_at must include a timezone")
        return value


RecoveryBatchSelector = Annotated[
    RecoveryExplicitSelector | RecoveryQuerySelector,
    Field(discriminator="type"),
]


class RecoveryBatchPreviewRequest(_RecoverySchema):
    request_id: uuid.UUID
    action: RecoveryCapability | None = Field(
        default=None,
        validation_alias=AliasChoices("action", "capability"),
        serialization_alias="action",
    )
    scope: RecoveryReplayScope = RecoveryReplayScope.STAGE_ONLY
    retry_limit: RecoveryRetryLimit = 3
    reason: str = Field(min_length=3, max_length=500)
    selector: RecoveryBatchSelector | None = None
    # Compatibility with the pre-selector contract.
    items: list[RecoveryWorkReference] | None = Field(default=None, min_length=1, max_length=1000)
    acknowledgements: list[RecoveryAcknowledgement] = Field(default_factory=list)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)

    @model_validator(mode="after")
    def validate_selection(self) -> RecoveryBatchPreviewRequest:
        if self.action is None:
            raise ValueError("action is required")
        if (self.selector is None) == (self.items is None):
            raise ValueError("provide exactly one of selector or legacy items")
        if len(self.acknowledgements) != len(set(self.acknowledgements)):
            raise ValueError("acknowledgements must not contain duplicates")
        return self

    def resolved_selector(self) -> RecoveryBatchSelector:
        if self.selector is not None:
            return self.selector
        if self.items is None:  # pragma: no cover - guarded by model validation.
            raise ValueError("batch selector is missing")
        return RecoveryExplicitSelector(items=self.items)


class RecoveryBatchScheduleRequest(_RecoverySchema):
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryBatchCancelRequest(RecoveryBatchScheduleRequest):
    pass


class RecoveryBatchHandoffRequest(RecoveryBatchScheduleRequest):
    assigned_admin_user_id: uuid.UUID


class RecoveryRetryFailedPreviewRequest(_RecoverySchema):
    request_id: uuid.UUID
    version: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=500)
    retry_limit: RecoveryRetryLimit | None = None

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class RecoveryJobItemRead(_RecoverySchema):
    id: uuid.UUID
    parent_item_id: uuid.UUID | None = None
    source_item_id: uuid.UUID | None = None
    work_kind: RecoveryWorkKind
    work_id: str
    meme_file_id: uuid.UUID | None = None
    stage: ContentPipelineStage | None = None
    is_root: bool = True
    action: RecoveryCapability
    status: RecoveryJobItemStatus
    expected_version: str | None = None
    canonical_version: str | None = None
    retry_limit: int = Field(default=3, ge=1, le=5)
    attempt_budget_start: int = Field(default=0, ge=0)
    retryable_failures_consumed: int = Field(default=0, ge=0)
    normalized_reason: str | None = None
    safe_error: str | None = None
    dispatched_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RecoveryJobRead(_RecoverySchema):
    id: uuid.UUID
    request_id: uuid.UUID
    requested_by_admin_user_id: uuid.UUID
    assigned_admin_user_id: uuid.UUID | None = None
    source_recovery_job_id: uuid.UUID | None = None
    status: RecoveryJobStatus
    action: RecoveryCapability
    scope: RecoveryReplayScope
    retry_limit: int = Field(ge=1, le=5)
    reason: str
    total_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    selected_root_count: int = Field(default=0, ge=0)
    expanded_execution_count: int = Field(default=0, ge=0)
    preparation_scanned_count: int = Field(default=0, ge=0)
    excluded_count: int = Field(default=0, ge=0)
    exclusions_by_reason: dict[str, int] = Field(default_factory=dict)
    queued_count: int = Field(default=0, ge=0)
    waiting_count: int = Field(default=0, ge=0)
    dispatched_count: int = Field(default=0, ge=0)
    succeeded_count: int = Field(default=0, ge=0)
    stale_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    cancelled_count: int = Field(default=0, ge=0)
    selection_snapshot_at: datetime | None = None
    materialization_completed_at: datetime | None = None
    expires_at: datetime | None = None
    scheduled_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    version: str
    # Retained for compatibility. Large job consumers should use the item page.
    items: list[RecoveryJobItemRead] = Field(default_factory=list)


class RecoveryJobPageRead(_RecoverySchema):
    items: list[RecoveryJobRead]
    next_cursor: str | None = None


class RecoveryJobItemPageRead(_RecoverySchema):
    items: list[RecoveryJobItemRead]
    next_cursor: str | None = None


class AdminSourceBackfillRead(_RecoverySchema):
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


class AdminSourceBackfillPageRead(_RecoverySchema):
    items: list[AdminSourceBackfillRead]


__all__ = [
    "AdminSourceBackfillPageRead",
    "AdminSourceBackfillRead",
    "RecoveryActionRead",
    "RecoveryActionRequest",
    "RecoveryActiveJobRead",
    "RecoveryBatchCancelRequest",
    "RecoveryBatchHandoffRequest",
    "RecoveryBatchPreviewRequest",
    "RecoveryBatchScheduleRequest",
    "RecoveryCandidateRead",
    "RecoveryExplicitSelector",
    "RecoveryJobItemPageRead",
    "RecoveryJobItemRead",
    "RecoveryJobPageRead",
    "RecoveryJobRead",
    "RecoveryMediaProfileRead",
    "RecoveryMutationRequest",
    "RecoveryQueryFilters",
    "RecoveryQuerySelector",
    "RecoveryRetryFailedPreviewRequest",
    "RecoverySummaryRead",
    "RecoveryTargetMutationRequest",
    "RecoveryWorkPageRead",
    "RecoveryWorkRead",
    "RecoveryWorkReference",
]
