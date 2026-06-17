# ruff: noqa: TC001,TC003
"""Runtime, dispatch, and read-model schemas for the content pipeline.

Historically this module carried every content-pipeline schema in one file.
The foundational enums/error codes now live in :mod:`pipeline_base` and the
crawler/upload ingest contract now lives in :mod:`pipeline_ingest`; this
module keeps the runtime dispatch event, stage-journal read model, item
read/detail projections, proof-harness summaries, and smoke-proof schemas.
Every name from the sibling modules is re-exported below so the existing
``memexpert.schemas.content_pipeline`` import surface is byte-for-byte
preserved for every caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from memexpert.models.enums import (
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentSourceKind,
    IngestFileOrigin,
    SourceAttachReason,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramSessionStatus,
)
from memexpert.schemas.base import ORMSchema
from memexpert.schemas.pipeline_base import (
    _PIPELINE_EVENT_ALLOWED_STAGES,
    MAX_OBJECT_KEY_LENGTH,
    MAX_PIPELINE_ERROR_LENGTH,
    MAX_PIPELINE_REASON_LENGTH,
    MAX_POST_ID_LENGTH,
    MAX_SOURCE_ID_LENGTH,
    MAX_TELEGRAM_CHANNEL_TITLE_LENGTH,
    MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH,
    MAX_TELEGRAM_CONTENT_TYPE_LENGTH,
    MAX_TELEGRAM_FILENAME_LENGTH,
    MAX_TELEGRAM_SESSION_NAME_LENGTH,
    ContentPipelineErrorCode,
    ContentPipelineErrorResponse,
    ContentPipelineEventType,
    ContentPipelineItemFilter,
)
from memexpert.schemas.pipeline_ingest import (
    CRAWLER_MEDIA_TYPE_VALUES,
    ContentPipelineUploadMetadata,
    CrawlerForwardAttribution,
    CrawlerIngestOutcome,
    CrawlerIngestResult,
    CrawlerSourcePlatform,
    RawCrawlerPost,
)


class TelegramSessionStateRead(ORMSchema):
    """Read projection of ``TelegramSessionState`` rows for T03 operator routes.

    Kept in the schemas module so the operator-facing routes introduced in
    T03 can serialize the state without re-exporting SQLAlchemy models to
    the API layer. The projection is intentionally 1:1 with the ORM row so
    operators see the full session-health surface.

    ``owned_channel_count`` is additive — it defaults to ``0`` so callers
    that validate a raw ORM row (for tests and pre-T03 fixtures) still
    round-trip cleanly without providing the count. The T03
    ``CrawlerOperationsService.list_sessions`` method computes the value
    from a second query and passes it explicitly.
    """

    id: uuid.UUID
    session_name: str = Field(min_length=1, max_length=MAX_TELEGRAM_SESSION_NAME_LENGTH)
    status: TelegramSessionStatus
    last_error_class: str | None = Field(default=None, max_length=128)
    last_error_text: str | None = Field(default=None, max_length=MAX_PIPELINE_ERROR_LENGTH)
    flood_wait_until: datetime | None = None
    live_listener_started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    quarantined_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    owned_channel_count: StrictInt = Field(default=0, ge=0)


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


class ContentPipelineStageJournalRead(ORMSchema):
    """Public read model for a single stage-journal row."""

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
    sha256_hex: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ingest_origin: IngestFileOrigin | None = None
    matched_meme_file_id: uuid.UUID | None = None
    latest_source_id: uuid.UUID | None = None
    latest_source_attach_reason: SourceAttachReason | None = None
    latest_source_matched_meme_file_id: uuid.UUID | None = None
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


class ContentPipelineOCRDetail(ORMSchema):
    """Durable OCR audit projection exposed by the enriched inspect surface.

    This projection is absent from ``ContentPipelineItemDetail`` whenever the
    item has not yet produced an ``MemeFileOCRResult`` row. Operators must not
    treat a missing projection as empty text; it means OCR has not run.
    """

    engine: str
    fallback_engine: str | None = None
    fallback_used: bool
    low_confidence: bool
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    language: ContentLanguage
    extracted_text: str | None = None
    source_object_key: str | None = None
    last_event_id: uuid.UUID | None = None


class ContentPipelineMergeParticipation(ORMSchema):
    """One row in the merge-audit lineage for a pipeline item.

    Either the item was the *source* (its meme was merged into the canonical)
    or the item sits under the *target* (the canonical meme that absorbed another).
    The enriched inspect detail exposes both directions so operators can walk
    the lineage without poking at the audit table directly.
    """

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


class ContentPipelineSyncTargetPreview(BaseModel):
    """Bounded JSON preview of the payload a sync target last received.

    The preview is stored alongside the snapshot row so operators can see which
    fields were advertised to Qdrant or Meilisearch without re-reading the full
    payload. ``preview_fields`` is intentionally typed as ``dict[str, object]``
    because the preview shape differs per target and T02/T03 define the keys.
    """

    model_config = ConfigDict(extra="forbid")

    target: SyncTargetKind
    preview_fields: dict[str, object] = Field(default_factory=dict)
    preview_fetched_at: datetime


class PerTargetSyncStatus(BaseModel):
    """Per-target sync truth projection attached to the enriched inspect detail.

    ``status`` is independent of the pipeline stage journal — a target can be
    ``pending`` or ``failed`` even after the heavy-worker classify stage has
    succeeded. Operators rely on this separation when deciding whether a meme is
    ``partially_searchable`` (one target done, the other still catching up).
    """

    model_config = ConfigDict(extra="forbid")

    target: SyncTargetKind
    status: SyncTargetStatus
    last_event_id: uuid.UUID | None = None
    normalized_reason: str | None = Field(default=None, max_length=MAX_PIPELINE_REASON_LENGTH)
    last_error_text: str | None = Field(default=None, max_length=MAX_PIPELINE_ERROR_LENGTH)
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    attempt_count: StrictInt = Field(ge=0)
    last_preview: ContentPipelineSyncTargetPreview | None = None


class ContentPipelineSyncReplayTarget(BaseModel):
    """Request schema for the per-target replay routes introduced in T02/T03.

    T01 locks this schema so the replay routes and batch endpoint can rely on a
    single durable request shape. Exactly one target is replayed per call so
    Qdrant and Meilisearch failures can be diagnosed independently.
    """

    model_config = ConfigDict(extra="forbid")

    target: SyncTargetKind


class ContentPipelineSyncReplayBatchRequest(BaseModel):
    """Request schema for the per-target sync batch replay route introduced in T02.

    The service layer enforces the bounded batch size so operators cannot
    accidentally requeue an entire corpus in one call; the batch endpoint
    exists as a convenience for replaying a small cluster of failures in a
    single operator action.
    """

    model_config = ConfigDict(extra="forbid")

    meme_file_ids: list[uuid.UUID] = Field(min_length=1)


class ContentPipelineItemDetail(ContentPipelineItemRead):
    """Enriched detail projection extending the S01 item read.

    Every new field is optional: when the heavy chain has not produced the
    underlying audit state, the projection is ``None`` or an empty collection.
    This preserves the byte-for-byte S01 ``ContentPipelineItemRead`` contract
    while giving operators first-class visibility into OCR, merge,
    classification, ``meme_ready`` truth, and the per-target sync state.
    """

    ocr: ContentPipelineOCRDetail | None = None
    merge: ContentPipelineMergeDetail = Field(default_factory=ContentPipelineMergeDetail)
    classification: ContentPipelineClassificationDetail = Field(
        default_factory=ContentPipelineClassificationDetail,
    )
    canonical: ContentPipelineCanonicalContext | None = None
    ready_event: ContentPipelineReadyEventSummary | None = None
    sync_targets: dict[SyncTargetKind, PerTargetSyncStatus] = Field(default_factory=dict)


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
    """Compact per-item report row persisted by the S02 runtime proof harness.

    T03 adds the optional per-target sync status fields so the Markdown
    report can render both sync targets side-by-side in the per-item table
    without the report builder re-querying durable state. The fields default
    to ``None`` so pre-S03 fixtures keep deserializing cleanly.
    """

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
    sync_qdrant_status: SyncTargetStatus | None = None
    sync_meili_status: SyncTargetStatus | None = None
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
    """Aggregate per-stage counters for a bounded proof-harness run.

    T03 extends the S02 counters with per-target sync pass/fail splits plus
    the cross-target aggregates (``both_synced_count``,
    ``partially_searchable_count``) that the operator Markdown report needs.
    All new fields default to ``0`` so pre-S03 clients that deserialize the
    model without the new counters still validate.
    """

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
    sync_qdrant_synced: StrictInt = Field(default=0, ge=0)
    sync_qdrant_failed: StrictInt = Field(default=0, ge=0)
    sync_qdrant_pending: StrictInt = Field(default=0, ge=0)
    sync_qdrant_pass: StrictInt = Field(default=0, ge=0)
    sync_meili_pass: StrictInt = Field(default=0, ge=0)
    sync_meili_failed: StrictInt = Field(default=0, ge=0)
    both_synced_count: StrictInt = Field(default=0, ge=0)
    partially_searchable_count: StrictInt = Field(default=0, ge=0)
    blocked_by_qdrant_count: StrictInt = Field(default=0, ge=0)
    blocked_by_meili_count: StrictInt = Field(default=0, ge=0)


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


class SmokeProofTargetResult(BaseModel):
    """Per-target outcome of one search-sync smoke proof.

    ``searchable`` is ``True`` only when BOTH the id-lookup and the typed
    query-by-vector (Qdrant) or the text ``search`` call (Meilisearch) surface
    the target ``meme_file_id`` in their top hits. ``matched_by`` documents
    which of the two paths fired so operators can distinguish a document that
    exists in the index but is not retrievable via the text/vector query.
    """

    model_config = ConfigDict(extra="forbid")

    target: SyncTargetKind
    searchable: bool
    reason: str | None = None
    latency_ms: float | None = None
    matched_by: Literal["id_lookup", "query_match", "both"] | None = None


class SmokeProofResult(BaseModel):
    """Assembled dual-target smoke proof for one pipeline item.

    The per-target breakdown is authoritative — ``both_targets_searchable`` is
    derived from ``targets`` and must be ``True`` only when every per-target
    ``searchable`` flag is also ``True``. The route handler returns this
    model even on partial / negative proofs so operators see the per-target
    reason strings in the response body.
    """

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID
    query: str | None = None
    both_targets_searchable: bool
    targets: tuple[SmokeProofTargetResult, ...]
    evaluated_at: datetime


class ContentPipelineSearchSmokeRequest(BaseModel):
    """Request body for ``POST /api/v1/pipeline/search/smoke``.

    Exactly one of ``meme_file_id`` and ``query`` must be supplied. When only
    the ``query`` is supplied the route resolves a ``meme_file_id`` from the
    top Meilisearch hit before running the per-item proof, so the dual-target
    truth check always runs against a concrete pipeline item.
    """

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID | None = None
    query: str | None = None

    @model_validator(mode="after")
    def _validate_exactly_one_input(self) -> ContentPipelineSearchSmokeRequest:
        if self.meme_file_id is None and self.query is None:
            raise ValueError(
                "search smoke request must provide exactly one of "
                "meme_file_id or query.",
            )
        if self.meme_file_id is not None and self.query is not None:
            raise ValueError(
                "search smoke request must provide exactly one of "
                "meme_file_id or query — not both.",
            )
        if self.query is not None and not self.query.strip():
            raise ValueError("search smoke request query must not be blank.")
        return self


class ContentPipelineS03RunItemReport(BaseModel):
    """Compact per-item report row persisted by the S03 search-sync proof harness.

    Each item carries its terminal outcome, per-target sync status, and the
    per-item :class:`SmokeProofResult` captured after both sync targets reach
    a terminal state. ``failure_reason`` is the normalized reason the harness
    saw if either the polling or the smoke proof could not run for this item.
    """

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID
    outcome: str
    qdrant_status: SyncTargetStatus | None = None
    meili_status: SyncTargetStatus | None = None
    smoke_result: SmokeProofResult | None = None
    failure_reason: str | None = None


class ContentPipelineS03RunSummary(BaseModel):
    """Persisted S03 search-sync proof summary written to the artifact directory.

    The counters mirror :class:`ContentPipelineRunStageCounts` fields that
    matter for the sync chain (``both_synced``, ``partially_searchable``,
    ``blocked_by_*``) and add the smoke-proof specific ``smoke_pass_count``
    and ``stale_snapshot_ids`` set. ``stale_snapshot_ids`` lists items whose
    snapshot claimed both targets were synced but whose smoke proof said
    otherwise — this is the operator-facing symptom that sync truth and the
    search engine disagree.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime
    bounded_item_count: StrictInt = Field(ge=0)
    qdrant_synced_count: StrictInt = Field(default=0, ge=0)
    meilisearch_synced_count: StrictInt = Field(default=0, ge=0)
    both_synced_count: StrictInt = Field(default=0, ge=0)
    partial_count: StrictInt = Field(default=0, ge=0)
    blocked_by_qdrant_count: StrictInt = Field(default=0, ge=0)
    blocked_by_meili_count: StrictInt = Field(default=0, ge=0)
    smoke_pass_count: StrictInt = Field(default=0, ge=0)
    stale_snapshot_ids: tuple[uuid.UUID, ...] = ()
    item_reports: tuple[ContentPipelineS03RunItemReport, ...] = ()
    errors: tuple[str, ...] = ()


__all__ = [
    "CRAWLER_MEDIA_TYPE_VALUES",
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
    "ContentPipelineS03RunItemReport",
    "ContentPipelineS03RunSummary",
    "ContentPipelineSearchSmokeRequest",
    "ContentPipelineStageJournalRead",
    "ContentPipelineStageTimings",
    "ContentPipelineSyncReplayBatchRequest",
    "ContentPipelineSyncReplayTarget",
    "ContentPipelineSyncTargetPreview",
    "ContentPipelineUploadMetadata",
    "ContentPipelineUploadRead",
    "CrawlerForwardAttribution",
    "CrawlerIngestOutcome",
    "CrawlerIngestResult",
    "CrawlerSourcePlatform",
    "MAX_OBJECT_KEY_LENGTH",
    "MAX_PIPELINE_ERROR_LENGTH",
    "MAX_PIPELINE_REASON_LENGTH",
    "MAX_POST_ID_LENGTH",
    "MAX_SOURCE_ID_LENGTH",
    "MAX_TELEGRAM_CHANNEL_TITLE_LENGTH",
    "MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH",
    "MAX_TELEGRAM_CONTENT_TYPE_LENGTH",
    "MAX_TELEGRAM_FILENAME_LENGTH",
    "MAX_TELEGRAM_SESSION_NAME_LENGTH",
    "PerTargetSyncStatus",
    "RawCrawlerPost",
    "SmokeProofResult",
    "SmokeProofTargetResult",
    "TelegramSessionStateRead",
    "_PIPELINE_EVENT_ALLOWED_STAGES",
]
