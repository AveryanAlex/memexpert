# ruff: noqa: TC001,TC002
"""Reusable FastAPI operator-pipeline dependencies and error translation helpers."""

from __future__ import annotations

from http import HTTPStatus
from secrets import compare_digest
from typing import Annotated, Final, cast

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.core.config import get_settings
from memexpert.core.database import get_db_session
from memexpert.schemas.content_pipeline import ContentPipelineErrorCode, ContentPipelineErrorResponse
from memexpert.services import (
    ContentPipelineService,
    PipelineIngestError,
    PipelineItemNotFoundError,
    PipelineOperatorTokenError,
    PipelinePayloadTooLargeError,
    PipelinePayloadValidationError,
    PipelinePublishError,
    PipelineReplayNotAllowedError,
    PipelineServiceError,
    PipelineSourceConflictError,
    PipelineStorageError,
    PipelineUnsupportedMediaTypeError,
)

PIPELINE_OPERATOR_TOKEN_HEADER_NAME: Final = "X-Memexpert-Operator-Token"

PIPELINE_ERROR_STATUS_CODES: Final[dict[ContentPipelineErrorCode, int]] = {
    ContentPipelineErrorCode.INVALID_OPERATOR_TOKEN: int(HTTPStatus.UNAUTHORIZED),
    ContentPipelineErrorCode.ITEM_NOT_FOUND: int(HTTPStatus.NOT_FOUND),
    ContentPipelineErrorCode.PAYLOAD_INVALID: int(HTTPStatus.BAD_REQUEST),
    ContentPipelineErrorCode.PAYLOAD_TOO_LARGE: int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
    ContentPipelineErrorCode.UNSUPPORTED_MEDIA_TYPE: int(HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
    ContentPipelineErrorCode.SOURCE_CONFLICT: int(HTTPStatus.CONFLICT),
    ContentPipelineErrorCode.STORAGE_FAILURE: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ContentPipelineErrorCode.INGEST_FAILURE: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ContentPipelineErrorCode.PUBLISH_FAILURE: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ContentPipelineErrorCode.REPLAY_NOT_ALLOWED: int(HTTPStatus.CONFLICT),
}

PIPELINE_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    int(HTTPStatus.BAD_REQUEST): {
        "description": "The upload bytes or provenance metadata were malformed.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.UNAUTHORIZED): {
        "description": "The operator token was missing or invalid.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.NOT_FOUND): {
        "description": "The requested pipeline item does not exist.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.CONFLICT): {
        "description": "The upload or replay request conflicts with existing durable pipeline state.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE): {
        "description": "The uploaded file exceeds the configured byte limit.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.UNSUPPORTED_MEDIA_TYPE): {
        "description": "The uploaded media type is not supported by the ingest path.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.SERVICE_UNAVAILABLE): {
        "description": "A required ingest dependency failed before or during downstream dispatch.",
        "model": ContentPipelineErrorResponse,
    },
}


class PipelineHTTPError(Exception):
    """Internal API-layer pipeline exception rendered as a stable JSON payload."""

    status_code: int
    payload: ContentPipelineErrorResponse

    def __init__(self, *, status_code: int, payload: ContentPipelineErrorResponse) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload.detail)


async def pipeline_http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render operator-pipeline failures as the documented machine-readable schema."""

    pipeline_error = cast("PipelineHTTPError", exc)
    return JSONResponse(
        status_code=pipeline_error.status_code,
        content=pipeline_error.payload.model_dump(mode="json"),
    )


OperatorTokenHeaderDep = Annotated[str | None, Header(alias=PIPELINE_OPERATOR_TOKEN_HEADER_NAME)]


def require_pipeline_operator_token(operator_token: OperatorTokenHeaderDep = None) -> None:
    """Require the configured operator token without relying on guest/full account auth."""

    expected_token = get_settings().pipeline_operator_token.get_secret_value().strip()
    provided_token = operator_token.strip() if operator_token is not None else ""
    if not provided_token or not compare_digest(provided_token, expected_token):
        raise to_pipeline_http_error(
            PipelineOperatorTokenError("A valid operator token is required for the pipeline surface."),
        )


def get_content_pipeline_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ContentPipelineService:
    """Build the operator ingest service from the current request session."""

    return ContentPipelineService.from_settings(session)


PipelineServiceDep = Annotated[ContentPipelineService, Depends(get_content_pipeline_service)]
OperatorTokenDep = Annotated[None, Depends(require_pipeline_operator_token)]


def to_pipeline_http_error(error: PipelineServiceError) -> PipelineHTTPError:
    """Convert a service-layer pipeline error into an API-facing JSON error response."""

    if isinstance(error, PipelineOperatorTokenError):
        error_code = ContentPipelineErrorCode.INVALID_OPERATOR_TOKEN
    elif isinstance(error, PipelineItemNotFoundError):
        error_code = ContentPipelineErrorCode.ITEM_NOT_FOUND
    elif isinstance(error, PipelinePayloadTooLargeError):
        error_code = ContentPipelineErrorCode.PAYLOAD_TOO_LARGE
    elif isinstance(error, PipelineUnsupportedMediaTypeError):
        error_code = ContentPipelineErrorCode.UNSUPPORTED_MEDIA_TYPE
    elif isinstance(error, PipelineSourceConflictError):
        error_code = ContentPipelineErrorCode.SOURCE_CONFLICT
    elif isinstance(error, PipelineReplayNotAllowedError):
        error_code = ContentPipelineErrorCode.REPLAY_NOT_ALLOWED
    elif isinstance(error, PipelineStorageError):
        error_code = ContentPipelineErrorCode.STORAGE_FAILURE
    elif isinstance(error, PipelinePublishError):
        error_code = ContentPipelineErrorCode.PUBLISH_FAILURE
    elif isinstance(error, PipelineIngestError):
        error_code = ContentPipelineErrorCode.INGEST_FAILURE
    elif isinstance(error, PipelinePayloadValidationError):
        error_code = ContentPipelineErrorCode.PAYLOAD_INVALID
    else:
        error_code = ContentPipelineErrorCode.INGEST_FAILURE

    return PipelineHTTPError(
        status_code=PIPELINE_ERROR_STATUS_CODES[error_code],
        payload=ContentPipelineErrorResponse(code=error_code, detail=str(error)),
    )


__all__ = [
    "OperatorTokenDep",
    "PIPELINE_ERROR_RESPONSES",
    "PIPELINE_ERROR_STATUS_CODES",
    "PIPELINE_OPERATOR_TOKEN_HEADER_NAME",
    "PipelineHTTPError",
    "PipelineServiceDep",
    "get_content_pipeline_service",
    "pipeline_http_exception_handler",
    "require_pipeline_operator_token",
    "to_pipeline_http_error",
]
