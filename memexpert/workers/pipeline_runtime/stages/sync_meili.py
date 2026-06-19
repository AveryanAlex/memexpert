# ruff: noqa: TC001,TC002,TC003
"""Meilisearch sync stage implementation for the pipeline worker runtime."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from memexpert.core.meilisearch import PipelineMeilisearchDocument
from memexpert.models.enums import ContentPipelineStage
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import PipelineIngestError, PipelinePublishError
from memexpert.services.search_index_sync import build_meilisearch_document, load_search_index_state
from memexpert.workers.pipeline_runtime.errors import (
    ForcedSyncMeiliFailure,
    normalize_failure_reason,
    render_error_text,
)
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncMeiliInputs:
    """Compact bundle of canonical state the sync_meili stage needs per attempt."""

    document: PipelineMeilisearchDocument


async def run_sync_meili_stage(
    context: PipelineStageHandlerContext,
    *,
    dispatch_event: ContentPipelineDispatchEvent,
    stage_context: PipelineStageWorkContext,
    attempt: int,
) -> None:
    """Load canonical state, upsert to Meilisearch, and record per-target sync truth."""

    _ = stage_context
    _maybe_force_sync_meili_failure(context, dispatch_event)
    try:
        sync_inputs = await _load_sync_meili_inputs(context, dispatch_event.meme_file_id)
    except Exception:
        await _record_sync_meili_failure(
            context,
            dispatch_event=dispatch_event,
            attempt=attempt,
            exc=PipelineIngestError(
                f"Canonical state for {dispatch_event.meme_file_id} is missing or unreadable for sync_meili.",
            ),
        )
        raise

    try:
        await context.meilisearch_sync_client.upsert_document(sync_inputs.document)
    except Exception as exc:
        await _record_sync_meili_failure(
            context,
            dispatch_event=dispatch_event,
            attempt=attempt,
            exc=exc,
        )
        raise

    preview_payload: dict[str, object] = {}
    try:
        fetched_preview = await context.meilisearch_sync_client.fetch_document(
            dispatch_event.meme_file_id,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, any failure degrades to empty preview.
        logger.warning(
            "meilisearch sync preview fetch failed for %s: %s",
            dispatch_event.meme_file_id,
            exc,
        )
        fetched_preview = None

    if fetched_preview is not None:
        preview_payload = dict(fetched_preview.preview_fields)

    try:
        async with context.session_factory() as session:
            service = PipelineStageCompletionService(session, settings=context.settings)
            _ = await service.complete_sync_meili_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                payload_preview=preview_payload,
            )
    except PipelinePublishError:
        raise
    except Exception as exc:
        await _record_sync_meili_failure(
            context,
            dispatch_event=dispatch_event,
            attempt=attempt,
            exc=exc,
        )
        raise


async def _load_sync_meili_inputs(
    context: PipelineStageHandlerContext,
    meme_file_id: uuid.UUID,
) -> SyncMeiliInputs:
    """Load canonical meme + primary-file OCR text for sync_meili."""

    async with context.session_factory() as session:
        loaded_state = await load_search_index_state(session, meme_file_id)
    return SyncMeiliInputs(document=build_meilisearch_document(loaded_state.canonical))


async def _record_sync_meili_failure(
    context: PipelineStageHandlerContext,
    *,
    dispatch_event: ContentPipelineDispatchEvent,
    attempt: int,
    exc: Exception,
) -> None:
    """Best-effort per-target snapshot failure recording before dispatcher handling."""

    normalized_reason = normalize_failure_reason(ContentPipelineStage.SYNC_MEILI, exc)
    last_error_text = render_error_text(exc)
    try:
        async with context.session_factory() as session:
            service = PipelineStageCompletionService(session, settings=context.settings)
            _ = await service.fail_sync_meili_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                normalized_reason=normalized_reason,
                last_error_text=last_error_text,
            )
    except Exception:  # noqa: BLE001 - snapshot upsert is best-effort before re-raise.
        return


def _maybe_force_sync_meili_failure(
    context: PipelineStageHandlerContext,
    dispatch_event: ContentPipelineDispatchEvent,
) -> None:
    forced_target = context.settings.pipeline_worker_fail_sync_meili_for_meme_file_id
    if forced_target is None:
        return
    if forced_target == str(dispatch_event.meme_file_id):
        raise ForcedSyncMeiliFailure(
            "Forced sync_meili failure requested by pipeline_worker_fail_sync_meili_for_meme_file_id.",
        )


__all__ = ["SyncMeiliInputs", "run_sync_meili_stage"]
