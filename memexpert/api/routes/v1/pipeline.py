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
from memexpert.models.enums import ContentPipelineStage, SourcePlatform
from memexpert.schemas.content_pipeline import (
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineReplayRequest,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
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


__all__ = ["router"]
