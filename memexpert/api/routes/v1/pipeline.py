# ruff: noqa: TC001,TC003
"""Operator-only content-pipeline upload, inspect, and replay routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Form, Path, Query, UploadFile, status
from pydantic import ValidationError

from memexpert.api.dependencies.pipeline import (
    PIPELINE_ERROR_RESPONSES,
    PipelineServiceDep,
    require_pipeline_operator_token,
    to_pipeline_http_error,
)
from memexpert.models.enums import ContentPipelineStage, SourcePlatform, SyncTargetKind
from memexpert.schemas.content_pipeline import (
    ContentPipelineItemDetail,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineReplayRequest,
    ContentPipelineSyncReplayBatchRequest,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
    PerTargetSyncStatus,
)
from memexpert.services import PipelinePayloadValidationError, PipelineServiceError

router = APIRouter(
    prefix="/pipeline",
    tags=["pipeline"],
    dependencies=[Depends(require_pipeline_operator_token)],
)


@router.post(
    "/uploads",
    response_model=ContentPipelineUploadRead,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an original asset into the manual operator ingest path",
)
async def create_pipeline_upload(
    pipeline_service: PipelineServiceDep,
    source_platform: Annotated[SourcePlatform, Form()],
    source_id: Annotated[str, Form(min_length=1)],
    post_id: Annotated[str, Form(min_length=1)],
    file: Annotated[UploadFile, File()],
    views: Annotated[int, Form(ge=0)] = 0,
) -> ContentPipelineUploadRead:
    """Persist one uploaded original before any downstream publish is attempted."""

    try:
        metadata = ContentPipelineUploadMetadata(
            source_platform=source_platform,
            source_id=source_id,
            post_id=post_id,
            views=views,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        detail = str(first_error.get("msg", "Uploaded provenance metadata is invalid."))
        raise to_pipeline_http_error(PipelinePayloadValidationError(detail)) from exc

    try:
        media_bytes = await file.read()
        return await pipeline_service.create_upload(
            metadata=metadata,
            filename=file.filename,
            content_type=file.content_type,
            media_bytes=media_bytes,
        )
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc
    finally:
        await file.close()


@router.get(
    "/items",
    response_model=list[ContentPipelineItemRead],
    responses=PIPELINE_ERROR_RESPONSES,
    summary="List failed, stuck, duplicate, or all pipeline items",
)
async def list_pipeline_items(
    pipeline_service: PipelineServiceDep,
    filter_by: Annotated[ContentPipelineItemFilter, Query(alias="filter")] = ContentPipelineItemFilter.FAILED,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    stuck_after_seconds: Annotated[int, Query(ge=1, le=86_400)] = 60,
) -> list[ContentPipelineItemRead]:
    """Return operator-facing pipeline items filtered by the current durable stage state."""

    try:
        items = await pipeline_service.list_items(
            filter_by=filter_by,
            limit=limit,
            stuck_after_seconds=stuck_after_seconds,
        )
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc

    return list(items)


@router.get(
    "/items/{meme_file_id}",
    response_model=ContentPipelineItemRead,
    responses=PIPELINE_ERROR_RESPONSES,
    summary="Read one pipeline item and its current stage-journal state",
)
async def read_pipeline_item(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
) -> ContentPipelineItemRead:
    """Return durable inspect state for one uploaded or duplicate-short-circuited file."""

    try:
        return await pipeline_service.get_item(meme_file_id)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.get(
    "/items/{meme_file_id}/detail",
    response_model=ContentPipelineItemDetail,
    responses=PIPELINE_ERROR_RESPONSES,
    summary="Read enriched S02 inspect detail (OCR, merge, classify, meme_ready)",
)
async def read_pipeline_item_detail(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
) -> ContentPipelineItemDetail:
    """Return the operator-facing enriched projection for one pipeline item.

    This surface is additive to ``GET /items/{meme_file_id}`` — it inherits
    every S01 field and adds optional projections for OCR truth, merge
    lineage, classification state, canonical-primary context, and the emitted
    ``meme_ready`` event id. Missing projections mean the underlying audit
    state has not yet been produced; operators must not read them as
    defaulted-false values.
    """

    try:
        return await pipeline_service.get_item_detail(meme_file_id)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.post(
    "/items/{meme_file_id}/replay",
    response_model=ContentPipelineReplayAccepted,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay the last retryable failed stage for one pipeline item",
)
async def replay_pipeline_item(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
    payload: Annotated[ContentPipelineReplayRequest | None, Body()] = None,
) -> ContentPipelineReplayAccepted:
    """Republish one failed stage without re-uploading the original durable ingest state."""

    resolved_payload = payload or ContentPipelineReplayRequest()
    requested_stage = resolved_payload.stage
    if requested_stage is ContentPipelineStage.INGEST:
        raise to_pipeline_http_error(
            PipelinePayloadValidationError("Replay is only supported for downstream stages after ingest."),
        )

    try:
        return await pipeline_service.replay_item(meme_file_id, stage=requested_stage)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


async def _read_sync_target_status(
    *,
    pipeline_service: PipelineServiceDep,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
) -> PerTargetSyncStatus:
    """Shared GET helper used by both per-target sync status routes.

    Extracted once three call sites existed (Qdrant + Meilisearch GET +
    smoke proof in T04) so the typed error translation stays in one place.
    """

    try:
        return await pipeline_service.get_sync_target_status(meme_file_id, target)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


async def _replay_sync_target(
    *,
    pipeline_service: PipelineServiceDep,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
) -> ContentPipelineReplayAccepted:
    """Shared POST helper used by both per-target single-item replay routes."""

    try:
        return await pipeline_service.replay_sync_target(meme_file_id, target)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


async def _replay_sync_target_batch(
    *,
    pipeline_service: PipelineServiceDep,
    payload: ContentPipelineSyncReplayBatchRequest,
    target: SyncTargetKind,
) -> list[ContentPipelineReplayAccepted]:
    """Shared POST helper used by both per-target batch replay routes.

    The service layer owns the ``SYNC_REPLAY_BATCH_MAX`` cap so the route
    layer just forwards the payload without double-bookkeeping it.
    """

    try:
        accepted = await pipeline_service.replay_sync_target_batch(
            payload.meme_file_ids,
            target,
        )
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc
    return list(accepted)


@router.get(
    "/items/{meme_file_id}/sync/qdrant",
    response_model=PerTargetSyncStatus,
    responses=PIPELINE_ERROR_RESPONSES,
    summary="Read the per-target Qdrant sync status for one pipeline item",
)
async def read_pipeline_item_qdrant_sync_status(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
) -> PerTargetSyncStatus:
    """Return the durable Qdrant sync snapshot row for one pipeline item.

    Returns ``404`` when the item exists but has no Qdrant sync snapshot row
    yet; operators must see the difference between "item missing" and "item
    has never been synced to Qdrant".
    """

    return await _read_sync_target_status(
        pipeline_service=pipeline_service,
        meme_file_id=meme_file_id,
        target=SyncTargetKind.QDRANT,
    )


@router.post(
    "/items/{meme_file_id}/sync/qdrant/replay",
    response_model=ContentPipelineReplayAccepted,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay the Qdrant sync stage for one pipeline item",
)
async def replay_pipeline_item_qdrant_sync(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
) -> ContentPipelineReplayAccepted:
    """Reserve and republish the Qdrant sync stage for one pipeline item.

    The classify stage must have already succeeded for the item — otherwise
    the per-target sync truth has no ready canonical state to advertise and
    the service surface rejects the request with ``409``.
    """

    return await _replay_sync_target(
        pipeline_service=pipeline_service,
        meme_file_id=meme_file_id,
        target=SyncTargetKind.QDRANT,
    )


@router.post(
    "/sync/qdrant/replay-batch",
    response_model=list[ContentPipelineReplayAccepted],
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay the Qdrant sync stage for a bounded batch of pipeline items",
)
async def replay_pipeline_items_qdrant_sync_batch(
    pipeline_service: PipelineServiceDep,
    payload: Annotated[ContentPipelineSyncReplayBatchRequest, Body()],
) -> list[ContentPipelineReplayAccepted]:
    """Reserve and republish the Qdrant sync stage for every item in a bounded batch.

    The service layer enforces the bounded batch size; the route just forwards
    the caller payload without imposing its own cap so the enforcement stays
    in one place.
    """

    return await _replay_sync_target_batch(
        pipeline_service=pipeline_service,
        payload=payload,
        target=SyncTargetKind.QDRANT,
    )


@router.get(
    "/items/{meme_file_id}/sync/meili",
    response_model=PerTargetSyncStatus,
    responses=PIPELINE_ERROR_RESPONSES,
    summary="Read the per-target Meilisearch sync status for one pipeline item",
)
async def read_pipeline_item_meili_sync_status(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
) -> PerTargetSyncStatus:
    """Return the durable Meilisearch sync snapshot row for one pipeline item.

    Returns ``404`` when the item exists but has no Meilisearch sync snapshot
    row yet; operators must see the difference between "item missing" and
    "item has never been synced to Meilisearch". Mirrors the Qdrant surface
    exactly so operator drill-down is symmetric between the two targets.
    """

    return await _read_sync_target_status(
        pipeline_service=pipeline_service,
        meme_file_id=meme_file_id,
        target=SyncTargetKind.MEILISEARCH,
    )


@router.post(
    "/items/{meme_file_id}/sync/meili/replay",
    response_model=ContentPipelineReplayAccepted,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay the Meilisearch sync stage for one pipeline item",
)
async def replay_pipeline_item_meili_sync(
    meme_file_id: Annotated[uuid.UUID, Path()],
    pipeline_service: PipelineServiceDep,
) -> ContentPipelineReplayAccepted:
    """Reserve and republish the Meilisearch sync stage for one pipeline item.

    Independent from the Qdrant replay route — replaying Meilisearch never
    touches the Qdrant snapshot row or vice versa, so operators can fix the
    two targets in either order without cross-contamination.
    """

    return await _replay_sync_target(
        pipeline_service=pipeline_service,
        meme_file_id=meme_file_id,
        target=SyncTargetKind.MEILISEARCH,
    )


@router.post(
    "/sync/meili/replay-batch",
    response_model=list[ContentPipelineReplayAccepted],
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay the Meilisearch sync stage for a bounded batch of pipeline items",
)
async def replay_pipeline_items_meili_sync_batch(
    pipeline_service: PipelineServiceDep,
    payload: Annotated[ContentPipelineSyncReplayBatchRequest, Body()],
) -> list[ContentPipelineReplayAccepted]:
    """Reserve and republish the Meilisearch sync stage for every item in a bounded batch.

    Same cap and translation semantics as the Qdrant batch route — the
    service layer owns ``SYNC_REPLAY_BATCH_MAX``.
    """

    return await _replay_sync_target_batch(
        pipeline_service=pipeline_service,
        payload=payload,
        target=SyncTargetKind.MEILISEARCH,
    )


__all__ = ["router"]
