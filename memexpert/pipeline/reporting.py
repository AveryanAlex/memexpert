# ruff: noqa: TC003
"""Inspect enrichment helpers for the operator pipeline surface.

The detail builder fills in the optional projections on
:class:`ContentPipelineItemDetail` only when the underlying audit state exists.
It never stubs empty OCR text or defaults ``is_nsfw`` to ``False`` before the
classify stage has really succeeded — that invariant is the whole point of the
enriched inspect surface.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    SyncTargetKind,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineCanonicalContext,
    ContentPipelineClassificationDetail,
    ContentPipelineItemDetail,
    ContentPipelineItemRead,
    ContentPipelineMergeDetail,
    ContentPipelineMergeParticipation,
    ContentPipelineOCRDetail,
    ContentPipelineReadyEventSummary,
    ContentPipelineSyncTargetPreview,
    PerTargetSyncStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def build_item_detail(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    base: ContentPipelineItemRead,
) -> ContentPipelineItemDetail:
    """Enrich a base item read with OCR, merge, classify, and ready-event truth.

    The caller is responsible for producing ``base`` through the normal item
    read path with the ``meme_file`` already loaded alongside its pipeline
    stage journal and OCR result relationship. This function only performs
    read queries; it never mutates durable state.
    """

    canonical_meme = await _load_canonical_meme(session, meme_file.meme_id)
    merge_detail = await _load_merge_detail(
        session,
        meme_file_id=base.meme_file_id,
        canonical_meme_id=canonical_meme.id,
    )
    classification, ready_event = _resolve_classify_projections(
        stage_entries=meme_file.pipeline_stage_journal_entries,
        canonical_meme=canonical_meme,
        is_canonical_primary_file=canonical_meme.primary_file_id == meme_file.id,
    )

    canonical_context = ContentPipelineCanonicalContext(
        canonical_meme_id=canonical_meme.id,
        canonical_primary_file_id=canonical_meme.primary_file_id,
        is_canonical_primary=canonical_meme.primary_file_id == meme_file.id,
        quality_score=meme_file.quality_score,
        ocr_text=canonical_meme.ocr_text,
        language=canonical_meme.language,
    )

    ocr_projection = (
        ContentPipelineOCRDetail.model_validate(meme_file.ocr_result)
        if meme_file.ocr_result is not None
        else None
    )

    sync_snapshots = await _load_sync_target_snapshots(session, meme_file.id)
    sync_targets = _project_sync_targets(sync_snapshots)

    return ContentPipelineItemDetail(
        **base.model_dump(mode="python"),
        ocr=ocr_projection,
        merge=merge_detail,
        classification=classification,
        canonical=canonical_context,
        ready_event=ready_event,
        sync_targets=sync_targets,
    )


async def _load_sync_target_snapshots(
    session: AsyncSession,
    meme_file_id: uuid.UUID,
) -> dict[SyncTargetKind, MemeFileSyncTargetSnapshot]:
    """Read-only loader for item-detail sync target projections."""

    result = await session.execute(
        select(MemeFileSyncTargetSnapshot).where(
            MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
        )
    )
    return {row.sync_target: row for row in result.scalars().all()}


def _project_sync_targets(
    snapshots: Mapping[SyncTargetKind, MemeFileSyncTargetSnapshot],
) -> dict[SyncTargetKind, PerTargetSyncStatus]:
    """Turn snapshot rows into the public per-target projection.

    Pure function, no DB access. T02 taught the Qdrant branch how to decode
    the stored ``last_payload_preview`` JSONB into a typed preview; T03 will
    mirror the same decoding for the Meilisearch branch. Empty snapshot
    mapping yields an empty projection, which is what the inspect surface
    must show for pre-S03 items that never went through sync.
    """

    projection: dict[SyncTargetKind, PerTargetSyncStatus] = {}
    for target, row in snapshots.items():
        projection[target] = PerTargetSyncStatus(
            target=row.sync_target,
            status=row.status,
            last_event_id=row.last_event_id,
            normalized_reason=row.normalized_reason,
            last_error_text=row.last_error_text,
            last_success_at=row.last_success_at,
            last_attempt_at=row.last_attempt_at,
            attempt_count=row.attempt_count,
            last_preview=decode_sync_preview(row.last_payload_preview, target=target),
        )
    return projection


def decode_sync_preview(
    raw_preview: object,
    *,
    target: SyncTargetKind,
) -> ContentPipelineSyncTargetPreview | None:
    """Decode a stored ``last_payload_preview`` JSONB blob into the typed projection.

    Returns ``None`` when the stored dict is empty or shaped in a way that
    would not round-trip through :class:`ContentPipelineSyncTargetPreview`.
    The inspect surface treats malformed or empty previews as "no preview
    known" rather than crashing the detail build. This is the single decode
    path the pipeline services, reporting layer, and inspect route all share.
    """

    if not isinstance(raw_preview, dict) or not raw_preview:
        return None
    try:
        return ContentPipelineSyncTargetPreview.model_validate(
            {
                "target": raw_preview.get("target", target.value),
                "preview_fields": raw_preview.get("preview_fields", {}),
                "preview_fetched_at": raw_preview.get("preview_fetched_at"),
            }
        )
    except Exception:  # noqa: BLE001 - malformed previews degrade to "no preview known".
        return None


async def _load_canonical_meme(session: AsyncSession, meme_id: uuid.UUID) -> Meme:
    result = await session.execute(select(Meme).where(Meme.id == meme_id))
    meme = result.scalar_one_or_none()
    if meme is None:
        raise LookupError(f"Canonical meme {meme_id} disappeared during detail build.")
    return meme


async def _load_merge_detail(
    session: AsyncSession,
    *,
    meme_file_id: uuid.UUID,
    canonical_meme_id: uuid.UUID,
) -> ContentPipelineMergeDetail:
    result = await session.execute(
        select(MemeMergeLog).where(
            or_(
                MemeMergeLog.source_meme_file_id == meme_file_id,
                MemeMergeLog.target_meme_id == canonical_meme_id,
            )
        )
    )
    rows = list(result.scalars().all())
    as_source: list[ContentPipelineMergeParticipation] = []
    as_target: list[ContentPipelineMergeParticipation] = []
    for row in rows:
        participation = _merge_log_to_participation(row)
        if row.source_meme_file_id == meme_file_id:
            as_source.append(participation)
        elif row.target_meme_id == canonical_meme_id:
            as_target.append(participation)
    return ContentPipelineMergeDetail(
        as_source=tuple(as_source),
        as_target=tuple(as_target),
    )


def _merge_log_to_participation(row: MemeMergeLog) -> ContentPipelineMergeParticipation:
    raw_moved_ids = row.details.get("moved_file_ids") if isinstance(row.details, dict) else None
    moved_file_ids: tuple[uuid.UUID, ...]
    if isinstance(raw_moved_ids, list):
        parsed_ids: list[uuid.UUID] = []
        for raw_id in raw_moved_ids:
            if isinstance(raw_id, str):
                try:
                    parsed_ids.append(uuid.UUID(raw_id))
                except ValueError:
                    # Malformed lineage metadata must not crash the inspect surface; the
                    # run-summary flagging logic will still surface a detail-build error.
                    continue
        moved_file_ids = tuple(parsed_ids)
    else:
        moved_file_ids = ()
    return ContentPipelineMergeParticipation(
        log_id=row.id,
        source_meme_id=row.source_meme_id,
        source_meme_file_id=row.source_meme_file_id,
        target_meme_id=row.target_meme_id,
        target_primary_file_id=row.target_primary_file_id,
        similarity_score=row.similarity_score,
        merge_reason=row.merge_reason,
        moved_file_ids=moved_file_ids,
        created_at=row.created_at,
    )


def _resolve_classify_projections(
    *,
    stage_entries: Iterable[PipelineStageJournal],
    canonical_meme: Meme,
    is_canonical_primary_file: bool,
) -> tuple[ContentPipelineClassificationDetail, ContentPipelineReadyEventSummary | None]:
    classify_entry: PipelineStageJournal | None = None
    for entry in stage_entries:
        if entry.stage is ContentPipelineStage.CLASSIFY:
            classify_entry = entry
            break

    if classify_entry is None or classify_entry.status is not ContentPipelineStageStatus.SUCCEEDED:
        return (
            ContentPipelineClassificationDetail(is_nsfw=None, classified=False),
            None,
        )

    classification = ContentPipelineClassificationDetail(
        is_nsfw=canonical_meme.is_nsfw,
        classified=True,
    )
    ready_event: ContentPipelineReadyEventSummary | None = None
    if classify_entry.last_event_id is not None and classify_entry.finished_at is not None:
        ready_event = ContentPipelineReadyEventSummary(
            event_id=classify_entry.last_event_id,
            classify_finished_at=classify_entry.finished_at,
            meme_file_ready=is_canonical_primary_file,
        )
    return classification, ready_event


__all__ = [
    "build_item_detail",
    "decode_sync_preview",
]
