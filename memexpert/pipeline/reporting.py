# ruff: noqa: TC003
"""Inspect enrichment and run-summary helpers for the operator surface.

The aggregation math used by the proof harness stays import-safe and pure so
unit tests can exercise it without a live RabbitMQ, Voyage, or Qdrant stack.

The detail builder fills in the optional projections on
:class:`ContentPipelineItemDetail` only when the underlying audit state exists.
It never stubs empty OCR text or defaults ``is_nsfw`` to ``False`` before the
classify stage has really succeeded — that invariant is the whole point of the
enriched inspect surface.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
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
    ContentPipelineRunItemReport,
    ContentPipelineRunStageCounts,
    ContentPipelineRunSummary,
    ContentPipelineStageJournalRead,
    ContentPipelineStageTimings,
    ContentPipelineSyncTargetPreview,
    PerTargetSyncStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Stages we compute timing percentiles for. Ingest is excluded because it is
# always instantaneous, and sync stages are covered by S03.
_TIMING_STAGES: tuple[ContentPipelineStage, ...] = (
    ContentPipelineStage.TRANSCODE,
    ContentPipelineStage.OCR,
    ContentPipelineStage.EMBED,
    ContentPipelineStage.CLASSIFY,
)

# Outcome tags used by the item reports. Kept as module constants so the proof
# harness and its tests never drift from the set the run summary advertises.
OUTCOME_READY = "ready"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAILED = "failed"
OUTCOME_IN_FLIGHT = "in_flight"
OUTCOME_DUPLICATE = "duplicate"
# New in S03: emitted when at least one but not all sync targets have caught up.
# T03 tightens ``_classify_outcome`` to actually return this value; T01 only
# exposes the constant so downstream tests and the runbook can refer to it.
OUTCOME_PARTIALLY_SEARCHABLE = "partially_searchable"


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


def summarize_run(
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    dataset_root: str,
    api_base_url: str,
    items: Sequence[ContentPipelineItemDetail],
    errors: Sequence[str] = (),
) -> ContentPipelineRunSummary:
    """Derive the compact run summary from a bounded list of item detail reads.

    This function is intentionally pure and synchronous so tests and the proof
    harness can share the same aggregation path. The inputs are already the
    result of calling the enriched detail route; no further DB access happens
    here.
    """

    item_reports = tuple(_build_item_report(item, api_base_url=api_base_url) for item in items)
    stage_counts = _build_stage_counts(items=items, item_reports=item_reports)
    stage_timings = tuple(_build_stage_timings(items, stage) for stage in _TIMING_STAGES)

    ready_event_ids = tuple(
        report.meme_ready_event_id
        for report in item_reports
        if report.meme_ready_event_id is not None
    )
    blocked_item_ids = tuple(
        report.meme_file_id for report in item_reports if report.outcome == OUTCOME_BLOCKED
    )
    flagged_item_ids = tuple(
        report.meme_file_id
        for report in item_reports
        if report.ocr_low_confidence or report.ocr_fallback_used
    )

    return ContentPipelineRunSummary(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        dataset_root=dataset_root,
        api_base_url=api_base_url,
        bounded_item_count=len(items),
        stage_counts=stage_counts,
        stage_timings=stage_timings,
        ready_event_ids=ready_event_ids,
        blocked_item_ids=blocked_item_ids,
        flagged_item_ids=flagged_item_ids,
        item_reports=item_reports,
        errors=tuple(errors),
    )


def _build_item_report(
    item: ContentPipelineItemDetail,
    *,
    api_base_url: str,
) -> ContentPipelineRunItemReport:
    outcome = _classify_outcome(item)
    merged_into: uuid.UUID | None = None
    if item.merge.as_source:
        # The service only records as_source for a file that was merged away.
        merged_into = item.merge.as_source[0].target_meme_id

    meme_ready_event_id = item.ready_event.event_id if item.ready_event is not None else None
    ocr_fallback_used = item.ocr.fallback_used if item.ocr is not None else False
    ocr_low_confidence = item.ocr.low_confidence if item.ocr is not None else False
    ocr_confidence = item.ocr.confidence if item.ocr is not None else None
    is_nsfw = item.classification.is_nsfw if item.classification.classified else None

    qdrant_status = item.sync_targets.get(SyncTargetKind.QDRANT)
    meili_status = item.sync_targets.get(SyncTargetKind.MEILISEARCH)

    drill_down_url = f"{api_base_url.rstrip('/')}/api/v1/pipeline/items/{item.meme_file_id}/detail"

    return ContentPipelineRunItemReport(
        meme_file_id=item.meme_file_id,
        meme_id=item.meme_id,
        terminal_stage=item.current_stage,
        terminal_status=item.current_status,
        outcome=outcome,
        meme_ready_event_id=meme_ready_event_id,
        failure_reason=item.normalized_reason,
        failure_text=item.last_error_text,
        ocr_fallback_used=ocr_fallback_used,
        ocr_low_confidence=ocr_low_confidence,
        ocr_confidence=ocr_confidence,
        merged_into_meme_id=merged_into,
        is_nsfw=is_nsfw,
        sync_qdrant_status=qdrant_status.status if qdrant_status is not None else None,
        sync_meili_status=meili_status.status if meili_status is not None else None,
        drill_down_url=drill_down_url,
    )


def _classify_outcome(item: ContentPipelineItemDetail) -> str:
    """Classify one item's terminal outcome from the combined stage + sync truth.

    T03 tightens the S02 classifier so it honestly distinguishes a fully
    searchable item (both targets synced) from the partially-searchable
    state (exactly one target synced). The ordering of checks is important:

    1. Duplicate shortcut — ingest-side de-dup bypasses the heavy chain
       entirely; no sync targets apply.
    2. Terminal non-retryable pre-sync failure — ``OUTCOME_FAILED``.
    3. Per-target sync status — if classify succeeded, interpret the pair
       of sync_targets as ``READY`` / ``PARTIALLY_SEARCHABLE`` / ``BLOCKED``
       / ``IN_FLIGHT``.
    4. Pre-classify failure — ``OUTCOME_BLOCKED`` or ``OUTCOME_IN_FLIGHT``
       depending on whether a retryable failure is present.
    """

    if item.current_status is ContentPipelineStageStatus.DUPLICATE:
        return OUTCOME_DUPLICATE

    classify_entry = next(
        (entry for entry in item.stages if entry.stage is ContentPipelineStage.CLASSIFY),
        None,
    )
    classify_succeeded = (
        classify_entry is not None
        and classify_entry.status is ContentPipelineStageStatus.SUCCEEDED
    )

    # Any non-retryable pre-sync failure is terminal and takes precedence
    # over whatever the sync snapshot rows report.
    if item.current_status is ContentPipelineStageStatus.FAILED:
        failed_entry = next(
            (entry for entry in item.stages if entry.stage == item.current_stage),
            None,
        )
        if (
            failed_entry is not None
            and not failed_entry.is_retryable
            and failed_entry.stage
            not in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}
        ):
            return OUTCOME_FAILED

    if classify_succeeded:
        qdrant = item.sync_targets.get(SyncTargetKind.QDRANT)
        meili = item.sync_targets.get(SyncTargetKind.MEILISEARCH)
        qdrant_synced = qdrant is not None and qdrant.status.value == "synced"
        meili_synced = meili is not None and meili.status.value == "synced"
        if qdrant_synced and meili_synced:
            return OUTCOME_READY
        if qdrant_synced or meili_synced:
            return OUTCOME_PARTIALLY_SEARCHABLE

        qdrant_blocked = _is_sync_target_blocked(qdrant, item.stages, ContentPipelineStage.SYNC_QDRANT)
        meili_blocked = _is_sync_target_blocked(meili, item.stages, ContentPipelineStage.SYNC_MEILI)
        if qdrant_blocked or meili_blocked:
            return OUTCOME_BLOCKED
        return OUTCOME_IN_FLIGHT

    if item.current_status is ContentPipelineStageStatus.FAILED:
        return OUTCOME_BLOCKED
    return OUTCOME_IN_FLIGHT


def _is_sync_target_blocked(
    status: PerTargetSyncStatus | None,
    stages: Iterable[ContentPipelineStageJournalRead],
    sync_stage: ContentPipelineStage,
) -> bool:
    """Return ``True`` when a sync target is genuinely blocked.

    Blocked = snapshot row reports ``failed`` AND the matching stage-journal
    row is marked non-retryable. This pairs the durable sync truth (the
    snapshot row) with the replay gate (the stage row's ``is_retryable``
    flag) so operators see the same decision the runtime would make.
    """

    if status is None or status.status.value != "failed":
        return False
    stage_entry = next(
        (entry for entry in stages if entry.stage is sync_stage),
        None,
    )
    if stage_entry is None:
        return False
    return not stage_entry.is_retryable


def _build_stage_counts(
    *,
    items: Sequence[ContentPipelineItemDetail],
    item_reports: Sequence[ContentPipelineRunItemReport],
) -> ContentPipelineRunStageCounts:
    counts = ContentPipelineRunStageCounts()

    items_by_id = {item.meme_file_id: item for item in items}
    for item in items:
        for entry in item.stages:
            _tally_stage_entry(counts, entry)
        if item.ocr is not None:
            if item.ocr.fallback_used:
                counts.ocr_fallback_used += 1
            if item.ocr.low_confidence:
                counts.ocr_low_confidence += 1
        if item.merge.as_source:
            counts.merge_count += 1
        _tally_sync_targets(counts, item)

    for report in item_reports:
        if report.outcome == OUTCOME_READY:
            counts.ready_count += 1
            counts.both_synced_count += 1
        if report.outcome == OUTCOME_PARTIALLY_SEARCHABLE:
            counts.partially_searchable_count += 1
        if report.outcome == OUTCOME_BLOCKED:
            counts.blocked_count += 1
            blocked_item = items_by_id.get(report.meme_file_id)
            if blocked_item is not None:
                if _is_sync_target_blocked(
                    blocked_item.sync_targets.get(SyncTargetKind.QDRANT),
                    blocked_item.stages,
                    ContentPipelineStage.SYNC_QDRANT,
                ):
                    counts.blocked_by_qdrant_count += 1
                if _is_sync_target_blocked(
                    blocked_item.sync_targets.get(SyncTargetKind.MEILISEARCH),
                    blocked_item.stages,
                    ContentPipelineStage.SYNC_MEILI,
                ):
                    counts.blocked_by_meili_count += 1

    return counts


def _tally_sync_targets(
    counts: ContentPipelineRunStageCounts,
    item: ContentPipelineItemDetail,
) -> None:
    """Bump per-target sync counters from one item's snapshot rows.

    Pure aggregation: ``_build_stage_counts`` owns the aggregate counters
    (``both_synced_count``, ``partially_searchable_count``,
    ``blocked_by_*_count``) because those cross-target decisions have to
    honor the full ``_classify_outcome`` logic on a per-report basis. Here
    we only touch the single-target pass/fail/pending counters.
    """

    qdrant_status = item.sync_targets.get(SyncTargetKind.QDRANT)
    if qdrant_status is not None:
        if qdrant_status.status.value == "synced":
            counts.sync_qdrant_synced += 1
            counts.sync_qdrant_pass += 1
        elif qdrant_status.status.value == "failed":
            counts.sync_qdrant_failed += 1
        else:
            counts.sync_qdrant_pending += 1

    meili_status = item.sync_targets.get(SyncTargetKind.MEILISEARCH)
    if meili_status is not None:
        if meili_status.status.value == "synced":
            counts.sync_meili_pass += 1
        elif meili_status.status.value == "failed":
            counts.sync_meili_failed += 1


def _tally_stage_entry(
    counts: ContentPipelineRunStageCounts,
    entry: ContentPipelineStageJournalRead,
) -> None:
    if entry.stage is ContentPipelineStage.TRANSCODE:
        if entry.status is ContentPipelineStageStatus.SUCCEEDED:
            counts.transcode_pass += 1
        elif entry.status is ContentPipelineStageStatus.FAILED:
            counts.transcode_failed += 1
    elif entry.stage is ContentPipelineStage.OCR:
        if entry.status is ContentPipelineStageStatus.SUCCEEDED:
            counts.ocr_pass += 1
        elif entry.status is ContentPipelineStageStatus.FAILED:
            counts.ocr_failed += 1
    elif entry.stage is ContentPipelineStage.EMBED:
        if entry.status is ContentPipelineStageStatus.SUCCEEDED:
            counts.embed_pass += 1
        elif entry.status is ContentPipelineStageStatus.FAILED:
            counts.embed_blocked += 1
    elif entry.stage is ContentPipelineStage.CLASSIFY:
        if entry.status is ContentPipelineStageStatus.SUCCEEDED:
            counts.classify_pass += 1
        elif entry.status is ContentPipelineStageStatus.FAILED:
            counts.classify_blocked += 1


def _build_stage_timings(
    items: Sequence[ContentPipelineItemDetail],
    stage: ContentPipelineStage,
) -> ContentPipelineStageTimings:
    durations: list[float] = []
    for item in items:
        for entry in item.stages:
            if entry.stage is stage and entry.started_at is not None and entry.finished_at is not None:
                delta = (entry.finished_at - entry.started_at).total_seconds()
                if delta >= 0:
                    durations.append(delta)
    if not durations:
        return ContentPipelineStageTimings(
            stage=stage,
            sample_count=0,
            p50_seconds=None,
            p95_seconds=None,
            max_seconds=None,
        )

    sorted_durations = sorted(durations)
    return ContentPipelineStageTimings(
        stage=stage,
        sample_count=len(sorted_durations),
        p50_seconds=_percentile(sorted_durations, 0.5),
        p95_seconds=_percentile(sorted_durations, 0.95),
        max_seconds=sorted_durations[-1],
    )


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    # Linear interpolation between the two closest ranks, matching the "linear"
    # method in numpy.percentile so operators comparing against external tools
    # see the same number.
    if not sorted_values:
        raise ValueError("_percentile requires at least one value.")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    lower_index = int(math.floor(rank))
    upper_index = int(math.ceil(rank))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    return lower_value + (upper_value - lower_value) * (rank - lower_index)


def render_markdown_report(summary: ContentPipelineRunSummary) -> str:
    """Render a compact human-readable Markdown companion to the JSON summary."""

    lines: list[str] = []
    lines.append(f"# Content pipeline S02 heavy-worker run {summary.run_id}")
    lines.append("")
    lines.append(f"- Dataset root: `{summary.dataset_root}`")
    lines.append(f"- API base: `{summary.api_base_url}`")
    lines.append(f"- Started: {summary.started_at.isoformat()}")
    lines.append(f"- Finished: {summary.finished_at.isoformat()}")
    lines.append(f"- Bounded item count: {summary.bounded_item_count}")
    lines.append("")
    lines.append("## Stage counts")
    lines.append("")
    stage_counts_data = summary.stage_counts.model_dump()
    for key, value in stage_counts_data.items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")
    lines.append("## Stage timings (seconds)")
    lines.append("")
    lines.append("| Stage | Samples | p50 | p95 | max |")
    lines.append("|-------|---------|-----|-----|-----|")
    for timing in summary.stage_timings:
        stage_name = timing.stage.value
        samples = timing.sample_count
        p50 = _format_seconds(timing.p50_seconds)
        p95 = _format_seconds(timing.p95_seconds)
        max_val = _format_seconds(timing.max_seconds)
        lines.append(f"| {stage_name} | {samples} | {p50} | {p95} | {max_val} |")
    lines.append("")
    lines.append("## Search sync per target")
    lines.append("")
    lines.append("| Target | Synced | Failed |")
    lines.append("|--------|--------|--------|")
    lines.append(
        f"| qdrant | {summary.stage_counts.sync_qdrant_pass} | "
        f"{summary.stage_counts.sync_qdrant_failed} |"
    )
    lines.append(
        f"| meilisearch | {summary.stage_counts.sync_meili_pass} | "
        f"{summary.stage_counts.sync_meili_failed} |"
    )
    lines.append("")
    lines.append(
        f"- both_synced: {summary.stage_counts.both_synced_count}"
    )
    lines.append(
        f"- partially_searchable: {summary.stage_counts.partially_searchable_count}"
    )
    lines.append(
        f"- blocked_by_qdrant: {summary.stage_counts.blocked_by_qdrant_count}"
    )
    lines.append(
        f"- blocked_by_meili: {summary.stage_counts.blocked_by_meili_count}"
    )
    lines.append("")
    lines.append("## Emitted meme_ready events")
    lines.append("")
    if summary.ready_event_ids:
        for event_id in summary.ready_event_ids:
            lines.append(f"- `{event_id}`")
    else:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Blocked items")
    lines.append("")
    if summary.blocked_item_ids:
        for blocked_id in summary.blocked_item_ids:
            lines.append(f"- `{blocked_id}`")
    else:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Flagged items (fallback or low-confidence OCR)")
    lines.append("")
    if summary.flagged_item_ids:
        for flagged_id in summary.flagged_item_ids:
            lines.append(f"- `{flagged_id}`")
    else:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Per-item detail")
    lines.append("")
    lines.append(
        "| meme_file_id | outcome | stage/status | sync (qdrant / meili) | ready event | drill-down |"
    )
    lines.append(
        "|--------------|---------|--------------|-----------------------|-------------|------------|"
    )
    for report in summary.item_reports:
        mid = report.meme_file_id
        outcome = report.outcome
        stage_label = report.terminal_stage.value
        status_label = report.terminal_status.value
        ready = str(report.meme_ready_event_id) if report.meme_ready_event_id else "—"
        url = report.drill_down_url
        qdrant_label = report.sync_qdrant_status.value if report.sync_qdrant_status is not None else "—"
        meili_label = report.sync_meili_status.value if report.sync_meili_status is not None else "—"
        lines.append(
            f"| `{mid}` | {outcome} | {stage_label}/{status_label} | "
            f"{qdrant_label} / {meili_label} | {ready} | [detail]({url}) |"
        )
    if summary.errors:
        lines.append("")
        lines.append("## Errors")
        lines.append("")
        for error_line in summary.errors:
            lines.append(f"- {error_line}")
    lines.append("")
    return "\n".join(lines)


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


__all__ = [
    "OUTCOME_BLOCKED",
    "OUTCOME_DUPLICATE",
    "OUTCOME_FAILED",
    "OUTCOME_IN_FLIGHT",
    "OUTCOME_PARTIALLY_SEARCHABLE",
    "OUTCOME_READY",
    "build_item_detail",
    "decode_sync_preview",
    "render_markdown_report",
    "summarize_run",
]
