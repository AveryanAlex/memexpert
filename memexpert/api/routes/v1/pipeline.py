# ruff: noqa: TC001,TC003
"""Operator-only content-pipeline upload and item-detail routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, UploadFile, status
from pydantic import ValidationError

from memexpert.api.dependencies.pipeline import (
    PIPELINE_ERROR_RESPONSES,
    PipelineServiceDep,
    require_pipeline_operator_token,
    to_pipeline_http_error,
)
from memexpert.models.enums import SourcePlatform
from memexpert.schemas.content_pipeline import (
    ContentPipelineItemRead,
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


__all__ = ["router"]
