# ruff: noqa: TC001,TC003
"""Typed content-pipeline request, response, and broker-contract schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from memexpert.models.enums import (
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentSourceKind,
    SourcePlatform,
)

MAX_OBJECT_KEY_LENGTH = 1024
MAX_PIPELINE_REASON_LENGTH = 128
MAX_PIPELINE_ERROR_LENGTH = 4000
MAX_SOURCE_ID_LENGTH = 255
MAX_POST_ID_LENGTH = 255


class ContentPipelineEventType(StrEnum):
    """Machine-readable broker event names used by the content pipeline."""

    MEME_CREATED = "meme_created"
    MEME_TRANSCODED = "meme_transcoded"
    MEME_OCR_DONE = "meme_ocr_done"
    MEME_EMBEDDED = "meme_embedded"
    MEME_READY = "meme_ready"
    STAGE_REPLAY_REQUESTED = "stage_replay_requested"


_PIPELINE_EVENT_ALLOWED_STAGES: dict[ContentPipelineEventType, frozenset[ContentPipelineStage]] = {
    ContentPipelineEventType.MEME_CREATED: frozenset({ContentPipelineStage.TRANSCODE}),
    ContentPipelineEventType.MEME_TRANSCODED: frozenset({ContentPipelineStage.OCR}),
    ContentPipelineEventType.MEME_OCR_DONE: frozenset({ContentPipelineStage.EMBED}),
    ContentPipelineEventType.MEME_EMBEDDED: frozenset({ContentPipelineStage.CLASSIFY}),
    ContentPipelineEventType.MEME_READY: frozenset(
        {
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        }
    ),
    ContentPipelineEventType.STAGE_REPLAY_REQUESTED: frozenset(
        {
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        }
    ),
}


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
    REPLAY_NOT_ALLOWED = "pipeline_replay_not_allowed"


class ContentPipelineItemFilter(StrEnum):
    """List filters exposed by the operator-only inspect surface."""

    ALL = "all"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    STUCK = "stuck"


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

    @model_validator(mode="after")
    def _validate_event_stage_pairing(self) -> ContentPipelineDispatchEvent:
        allowed_stages = _PIPELINE_EVENT_ALLOWED_STAGES[self.event_type]
        if self.stage not in allowed_stages:
            sorted_stages = sorted(allowed_stages, key=lambda stage: stage.value)
            allowed_stage_values = ", ".join(stage.value for stage in sorted_stages)
            raise ValueError(
                f"event_type {self.event_type.value!r} only allows stage values: {allowed_stage_values}."
            )
        return self


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


class ContentPipelineOCRDetail(BaseModel):
    """Durable OCR audit projection exposed by the enriched inspect surface.

    This projection is absent from ``ContentPipelineItemDetail`` whenever the
    item has not yet produced an ``MemeFileOCRResult`` row. Operators must not
    treat a missing projection as empty text; it means OCR has not run.
    """

    model_config = ConfigDict(from_attributes=True)

    engine: str
    fallback_engine: str | None = None
    fallback_used: bool
    low_confidence: bool
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    language: ContentLanguage
    extracted_text: str | None = None
    source_object_key: str | None = None
    last_event_id: uuid.UUID | None = None


class ContentPipelineMergeParticipation(BaseModel):
    """One row in the merge-audit lineage for a pipeline item.

    Either the item was the *source* (its meme was merged into the canonical)
    or the item sits under the *target* (the canonical meme that absorbed another).
    The enriched inspect detail exposes both directions so operators can walk
    the lineage without poking at the audit table directly.
    """

    model_config = ConfigDict(from_attributes=True)

    log_id: uuid.UUID
    source_meme_id: uuid.UUID
    source_meme_file_id: uuid.UUID | None = None
    target_meme_id: uuid.UUID
    target_primary_file_id: uuid.UUID | None = None
    similarity_score: float | None = None
    merge_reason: str
    moved_file_ids: tuple[uuid.UUID, ...] = ()
    created_at: datetime


class ContentPipelineMergeDetail(BaseModel):
    """Aggregated merge-audit projection attached to the enriched inspect detail."""

    model_config = ConfigDict(extra="forbid")

    as_source: tuple[ContentPipelineMergeParticipation, ...] = ()
    as_target: tuple[ContentPipelineMergeParticipation, ...] = ()


class ContentPipelineClassificationDetail(BaseModel):
    """Classification projection with explicit unknown semantics.

    ``is_nsfw`` is ``None`` while the heavy chain has not reached classify
    completion. The enriched detail never defaults this to ``false`` because
    operators must see the difference between "not yet classified" and
    "classified as safe".
    """

    model_config = ConfigDict(extra="forbid")

    is_nsfw: bool | None = None
    classified: bool = False


class ContentPipelineCanonicalContext(BaseModel):
    """Canonical meme context for one pipeline item after (possibly) merging."""

    model_config = ConfigDict(extra="forbid")

    canonical_meme_id: uuid.UUID
    canonical_primary_file_id: uuid.UUID | None = None
    is_canonical_primary: bool
    quality_score: float
    ocr_text: str | None = None
    language: ContentLanguage


class ContentPipelineReadyEventSummary(BaseModel):
    """Emitted ``meme_ready`` event identifier captured by the classify stage row."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    classify_finished_at: datetime
    meme_file_ready: bool


class ContentPipelineItemDetail(ContentPipelineItemRead):
    """Enriched detail projection extending the S01 item read.

    Every new field is optional: when the heavy chain has not produced the
    underlying audit state, the projection is ``None`` or an empty collection.
    This preserves the byte-for-byte S01 ``ContentPipelineItemRead`` contract
    while giving operators first-class visibility into OCR, merge,
    classification, and ``meme_ready`` truth.
    """

    ocr: ContentPipelineOCRDetail | None = None
    merge: ContentPipelineMergeDetail = Field(default_factory=ContentPipelineMergeDetail)
    classification: ContentPipelineClassificationDetail = Field(
        default_factory=ContentPipelineClassificationDetail,
    )
    canonical: ContentPipelineCanonicalContext | None = None
    ready_event: ContentPipelineReadyEventSummary | None = None


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


class ContentPipelineRunItemReport(BaseModel):
    """Compact per-item report row persisted by the S02 runtime proof harness."""

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID
    meme_id: uuid.UUID
    terminal_stage: ContentPipelineStage
    terminal_status: ContentPipelineStageStatus
    outcome: str
    meme_ready_event_id: uuid.UUID | None = None
    failure_reason: str | None = None
    failure_text: str | None = None
    ocr_fallback_used: bool = False
    ocr_low_confidence: bool = False
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    merged_into_meme_id: uuid.UUID | None = None
    is_nsfw: bool | None = None
    drill_down_url: str


class ContentPipelineStageTimings(BaseModel):
    """Stage-latency percentiles derived from the journal's started_at/finished_at."""

    model_config = ConfigDict(extra="forbid")

    stage: ContentPipelineStage
    sample_count: StrictInt = Field(ge=0)
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    max_seconds: float | None = None


class ContentPipelineRunStageCounts(BaseModel):
    """Aggregate per-stage counters for a bounded proof-harness run."""

    model_config = ConfigDict(extra="forbid")

    transcode_pass: StrictInt = Field(default=0, ge=0)
    transcode_failed: StrictInt = Field(default=0, ge=0)
    ocr_pass: StrictInt = Field(default=0, ge=0)
    ocr_failed: StrictInt = Field(default=0, ge=0)
    ocr_fallback_used: StrictInt = Field(default=0, ge=0)
    ocr_low_confidence: StrictInt = Field(default=0, ge=0)
    embed_pass: StrictInt = Field(default=0, ge=0)
    embed_blocked: StrictInt = Field(default=0, ge=0)
    merge_count: StrictInt = Field(default=0, ge=0)
    classify_pass: StrictInt = Field(default=0, ge=0)
    classify_blocked: StrictInt = Field(default=0, ge=0)
    ready_count: StrictInt = Field(default=0, ge=0)
    blocked_count: StrictInt = Field(default=0, ge=0)


class ContentPipelineRunSummary(BaseModel):
    """Persisted proof-harness summary written to the S02 artifact directory.

    Combines pass-rate, fallback-rate, per-stage timings, merge counts, blocked
    items, and emitted ``meme_ready`` ids into one machine-readable payload that
    operators read before deciding whether the heavy chain behaved truthfully
    for the current corpus run.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime
    dataset_root: str
    api_base_url: str
    bounded_item_count: StrictInt = Field(ge=0)
    stage_counts: ContentPipelineRunStageCounts
    stage_timings: tuple[ContentPipelineStageTimings, ...]
    ready_event_ids: tuple[uuid.UUID, ...]
    blocked_item_ids: tuple[uuid.UUID, ...]
    flagged_item_ids: tuple[uuid.UUID, ...]
    item_reports: tuple[ContentPipelineRunItemReport, ...]
    errors: tuple[str, ...] = ()


__all__ = [
    "ContentPipelineCanonicalContext",
    "ContentPipelineClassificationDetail",
    "ContentPipelineDispatchEvent",
    "ContentPipelineErrorCode",
    "ContentPipelineErrorResponse",
    "ContentPipelineEventType",
    "ContentPipelineItemDetail",
    "ContentPipelineItemFilter",
    "ContentPipelineItemRead",
    "ContentPipelineMergeDetail",
    "ContentPipelineMergeParticipation",
    "ContentPipelineOCRDetail",
    "ContentPipelineReadyEventSummary",
    "ContentPipelineReplayAccepted",
    "ContentPipelineReplayRequest",
    "ContentPipelineRunItemReport",
    "ContentPipelineRunStageCounts",
    "ContentPipelineRunSummary",
    "ContentPipelineStageJournalRead",
    "ContentPipelineStageTimings",
    "ContentPipelineUploadMetadata",
    "ContentPipelineUploadRead",
    "MAX_OBJECT_KEY_LENGTH",
    "MAX_PIPELINE_ERROR_LENGTH",
    "MAX_PIPELINE_REASON_LENGTH",
    "MAX_POST_ID_LENGTH",
    "MAX_SOURCE_ID_LENGTH",
]
