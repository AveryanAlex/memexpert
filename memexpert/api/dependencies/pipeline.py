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
from memexpert.core.database import get_async_session_factory, get_db_session
from memexpert.core.meilisearch import (
    MeilisearchSyncClientProtocol,
    PipelineMeilisearchSyncClient,
)
from memexpert.core.qdrant import (
    PipelineQdrantClient,
    PipelineQdrantSyncClient,
    QdrantSimilarityClientProtocol,
    QdrantSyncClientProtocol,
)
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramClientProtocol,
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
)
from memexpert.crawlers.telegram.manager import TelegramSessionManager
from memexpert.crawlers.telegram.runtime import TelegramCrawlerRuntime
from memexpert.ingest.accept_service import PipelineIngestAcceptService
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.ingest.read_service import PipelineIngestReadService
from memexpert.pipeline.items import PipelineItemReadService
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.pipeline.smoke_proof import PipelineSmokeProofService
from memexpert.pipeline.sync_status import PipelineSyncStatusService
from memexpert.schemas.content_pipeline import ContentPipelineErrorCode, ContentPipelineErrorResponse
from memexpert.services import (
    CrawlerChannelNotFoundError,
    CrawlerChannelNotTrackedError,
    CrawlerInvalidSessionError,
    CrawlerSessionNotRunnableError,
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
from memexpert.services.crawler_operations import CrawlerOperationsService

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
    ContentPipelineErrorCode.CRAWLER_CHANNEL_NOT_FOUND: int(HTTPStatus.NOT_FOUND),
    ContentPipelineErrorCode.CRAWLER_CHANNEL_NOT_TRACKED: int(HTTPStatus.NOT_FOUND),
    ContentPipelineErrorCode.CRAWLER_INVALID_SESSION: int(HTTPStatus.CONFLICT),
    ContentPipelineErrorCode.CRAWLER_SESSION_NOT_RUNNABLE: int(HTTPStatus.CONFLICT),
    ContentPipelineErrorCode.TELEGRAM_FLOOD_WAIT: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ContentPipelineErrorCode.TELEGRAM_SESSION_BANNED: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ContentPipelineErrorCode.TELEGRAM_PROVIDER_UNAVAILABLE: int(HTTPStatus.BAD_GATEWAY),
    ContentPipelineErrorCode.TELEGRAM_MALFORMED_MESSAGE: int(HTTPStatus.UNPROCESSABLE_ENTITY),
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
    int(HTTPStatus.UNPROCESSABLE_ENTITY): {
        "description": (
            "The request payload is structurally invalid or the Telegram adapter "
            "returned a malformed message."
        ),
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.UNSUPPORTED_MEDIA_TYPE): {
        "description": "The uploaded media type is not supported by the ingest path.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.BAD_GATEWAY): {
        "description": "An upstream dependency (Telegram or a sync target) is unreachable.",
        "model": ContentPipelineErrorResponse,
    },
    int(HTTPStatus.SERVICE_UNAVAILABLE): {
        "description": "A required ingest dependency failed before or during downstream dispatch.",
        "model": ContentPipelineErrorResponse,
    },
}


class PipelineHTTPError(Exception):
    """Internal API-layer pipeline exception rendered as a stable JSON payload.

    ``headers`` carries optional HTTP headers that must be attached to
    the JSON response (for example, ``Retry-After`` when the Telegram
    adapter surfaces a flood-wait cooldown). Keep the headers mapping
    typed as ``dict[str, str]`` so the JSON response renderer can pass
    it straight to :class:`JSONResponse` without format translation.
    """

    status_code: int
    payload: ContentPipelineErrorResponse
    headers: dict[str, str] | None

    def __init__(
        self,
        *,
        status_code: int,
        payload: ContentPipelineErrorResponse,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers = headers
        super().__init__(payload.detail)


async def pipeline_http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render operator-pipeline failures as the documented machine-readable schema."""

    pipeline_error = cast("PipelineHTTPError", exc)
    return JSONResponse(
        status_code=pipeline_error.status_code,
        content=pipeline_error.payload.model_dump(mode="json"),
        headers=pipeline_error.headers,
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


def get_pipeline_item_read_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineItemReadService:
    """Build the materialized item read service for one request."""

    return PipelineItemReadService.from_settings(session)


def get_pipeline_replay_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineReplayService:
    """Build the operator replay service for one request."""

    return PipelineReplayService.from_settings(session)


def get_pipeline_sync_status_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineSyncStatusService:
    """Build the per-target sync status read service for one request."""

    return PipelineSyncStatusService.from_settings(session)


def get_pipeline_smoke_proof_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineSmokeProofService:
    """Build the search smoke-proof service for one request."""

    return PipelineSmokeProofService.from_settings(session)


def get_pipeline_ingest_accept_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineIngestAcceptService:
    """Build the API-safe raw ingest accept service for one request."""

    return PipelineIngestAcceptService.from_settings(session)


def get_pipeline_ingest_read_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineIngestReadService:
    """Build the raw ingest-request read service for one request."""

    return PipelineIngestReadService(session=session)


def get_pipeline_crawler_ingest_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineCrawlerIngestService:
    """Build the API-safe crawler ingest wrapper for one request."""

    return PipelineCrawlerIngestService.from_settings(session)


def get_qdrant_sync_client() -> QdrantSyncClientProtocol:
    """Return the lazy Qdrant sync adapter used by the smoke-proof route.

    The adapter is cached across requests because the underlying
    ``AsyncQdrantClient`` maintains a pooled HTTP connection; tests override
    this dependency to inject fakes without importing the real SDK.
    """

    return PipelineQdrantSyncClient(settings=get_settings())


def get_qdrant_similarity_client() -> QdrantSimilarityClientProtocol:
    """Return the lazy Qdrant similarity adapter used by the smoke-proof route."""

    return PipelineQdrantClient(settings=get_settings())


def get_meilisearch_sync_client() -> MeilisearchSyncClientProtocol:
    """Return the lazy Meilisearch sync adapter used by the smoke-proof route."""

    return PipelineMeilisearchSyncClient(settings=get_settings())


def get_pipeline_telegram_client() -> PipelineTelegramClientProtocol:
    """Return the default Telegram adapter bound to the crawler runtime.

    The default is :class:`FakeTelegramClient` (no network calls, no
    Telethon import at call time) because the real
    :class:`PipelineTelethonClient` is inherently bound to a single
    ``session_name`` and the operator inspect surface cannot pick a
    session for the caller. Tests and the crawler worker override this
    provider via :meth:`FastAPI.dependency_overrides` to inject either a
    pinned fake or a per-session real client. Keeping the default a
    safe no-op means the route layer never imports Telethon at import
    time.
    """

    return FakeTelegramClient()


def get_crawler_runtime(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    ingest_service: Annotated[PipelineCrawlerIngestService, Depends(get_pipeline_crawler_ingest_service)],
    telegram_client: Annotated[
        PipelineTelegramClientProtocol,
        Depends(get_pipeline_telegram_client),
    ],
) -> TelegramCrawlerRuntime:
    """Construct the single-session crawler runtime for one request."""

    return TelegramCrawlerRuntime(
        ingest_service=ingest_service,
        telegram_client=telegram_client,
        session=session,
        settings=get_settings(),
    )


_telegram_session_manager: TelegramSessionManager | None = None


def get_telegram_session_manager() -> TelegramSessionManager:
    """Return the process-local multi-session Telegram manager."""

    global _telegram_session_manager
    if _telegram_session_manager is None:
        _telegram_session_manager = TelegramSessionManager(
            settings=get_settings(),
            session_factory=get_async_session_factory(),
        )
    return _telegram_session_manager


def get_crawler_operations_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    runtime: Annotated[TelegramCrawlerRuntime, Depends(get_crawler_runtime)],
    manager: Annotated[TelegramSessionManager, Depends(get_telegram_session_manager)],
) -> CrawlerOperationsService:
    """Return the operator crawler-operations service for one request."""

    return CrawlerOperationsService(session=session, runtime=runtime, manager=manager)


PipelineItemReadServiceDep = Annotated[PipelineItemReadService, Depends(get_pipeline_item_read_service)]
PipelineReplayServiceDep = Annotated[PipelineReplayService, Depends(get_pipeline_replay_service)]
PipelineSyncStatusServiceDep = Annotated[
    PipelineSyncStatusService,
    Depends(get_pipeline_sync_status_service),
]
PipelineSmokeProofServiceDep = Annotated[
    PipelineSmokeProofService,
    Depends(get_pipeline_smoke_proof_service),
]
PipelineIngestAcceptServiceDep = Annotated[
    PipelineIngestAcceptService,
    Depends(get_pipeline_ingest_accept_service),
]
PipelineIngestReadServiceDep = Annotated[
    PipelineIngestReadService,
    Depends(get_pipeline_ingest_read_service),
]
PipelineCrawlerIngestServiceDep = Annotated[
    PipelineCrawlerIngestService,
    Depends(get_pipeline_crawler_ingest_service),
]
OperatorTokenDep = Annotated[None, Depends(require_pipeline_operator_token)]
QdrantSyncClientDep = Annotated[QdrantSyncClientProtocol, Depends(get_qdrant_sync_client)]
QdrantSimilarityClientDep = Annotated[
    QdrantSimilarityClientProtocol,
    Depends(get_qdrant_similarity_client),
]
MeilisearchSyncClientDep = Annotated[
    MeilisearchSyncClientProtocol,
    Depends(get_meilisearch_sync_client),
]
PipelineTelegramClientDep = Annotated[
    PipelineTelegramClientProtocol,
    Depends(get_pipeline_telegram_client),
]
CrawlerRuntimeDep = Annotated[TelegramCrawlerRuntime, Depends(get_crawler_runtime)]
TelegramSessionManagerDep = Annotated[TelegramSessionManager, Depends(get_telegram_session_manager)]
CrawlerOperationsServiceDep = Annotated[
    CrawlerOperationsService,
    Depends(get_crawler_operations_service),
]


def to_pipeline_http_error(
    error: PipelineServiceError | PipelineTelegramError,
) -> PipelineHTTPError:
    """Convert a service-layer or crawler adapter error into an API-facing JSON payload.

    The function accepts BOTH the pipeline service exception hierarchy
    (:class:`PipelineServiceError`) and the crawler adapter hierarchy
    (:class:`PipelineTelegramError`) so the operator route layer never
    has to maintain a second dispatcher. The two hierarchies are
    intentionally distinct classes so the service layer can raise one
    without importing the other, but the route translation is centralized
    here because the operator pipeline and crawler surfaces share the
    same JSON error envelope.
    """

    if isinstance(error, PipelineTelegramError):
        return _telegram_error_to_http(error)
    error_code = _classify_pipeline_service_error(error)
    return PipelineHTTPError(
        status_code=PIPELINE_ERROR_STATUS_CODES[error_code],
        payload=ContentPipelineErrorResponse(code=error_code, detail=str(error)),
    )


def _classify_pipeline_service_error(
    error: PipelineServiceError,
) -> ContentPipelineErrorCode:
    """Return the machine-readable error code for a typed service error."""

    if isinstance(error, PipelineOperatorTokenError):
        return ContentPipelineErrorCode.INVALID_OPERATOR_TOKEN
    if isinstance(error, PipelineItemNotFoundError):
        return ContentPipelineErrorCode.ITEM_NOT_FOUND
    if isinstance(error, PipelinePayloadTooLargeError):
        return ContentPipelineErrorCode.PAYLOAD_TOO_LARGE
    if isinstance(error, PipelineUnsupportedMediaTypeError):
        return ContentPipelineErrorCode.UNSUPPORTED_MEDIA_TYPE
    if isinstance(error, PipelineSourceConflictError):
        return ContentPipelineErrorCode.SOURCE_CONFLICT
    if isinstance(error, PipelineReplayNotAllowedError):
        return ContentPipelineErrorCode.REPLAY_NOT_ALLOWED
    if isinstance(error, PipelineStorageError):
        return ContentPipelineErrorCode.STORAGE_FAILURE
    if isinstance(error, PipelinePublishError):
        return ContentPipelineErrorCode.PUBLISH_FAILURE
    # Crawler service errors are ``PipelineIngestError`` subclasses so
    # the dispatcher routes them to the dedicated operator codes BEFORE
    # falling through to the generic ingest-failure mapping.
    if isinstance(error, CrawlerChannelNotFoundError):
        return ContentPipelineErrorCode.CRAWLER_CHANNEL_NOT_FOUND
    if isinstance(error, CrawlerChannelNotTrackedError):
        return ContentPipelineErrorCode.CRAWLER_CHANNEL_NOT_TRACKED
    if isinstance(error, CrawlerInvalidSessionError):
        return ContentPipelineErrorCode.CRAWLER_INVALID_SESSION
    if isinstance(error, CrawlerSessionNotRunnableError):
        return ContentPipelineErrorCode.CRAWLER_SESSION_NOT_RUNNABLE
    if isinstance(error, PipelineIngestError):
        return ContentPipelineErrorCode.INGEST_FAILURE
    if isinstance(error, PipelinePayloadValidationError):
        return ContentPipelineErrorCode.PAYLOAD_INVALID
    return ContentPipelineErrorCode.INGEST_FAILURE


def _telegram_error_to_http(error: PipelineTelegramError) -> PipelineHTTPError:
    """Translate a crawler adapter error into the JSON error envelope.

    Flood-wait errors carry a concrete ``wait_seconds`` cooldown value
    Telegram told us to respect, so the function attaches a
    :rfc:`7231` ``Retry-After`` header (in seconds) to the response —
    operator tooling relies on that header to back off without
    stringifying the detail field.
    """

    headers: dict[str, str] | None = None
    if isinstance(error, PipelineTelegramFloodWaitError):
        error_code = ContentPipelineErrorCode.TELEGRAM_FLOOD_WAIT
        headers = {"Retry-After": str(max(0, int(error.wait_seconds)))}
    elif isinstance(error, PipelineTelegramSessionBannedError):
        error_code = ContentPipelineErrorCode.TELEGRAM_SESSION_BANNED
    elif isinstance(error, PipelineTelegramSessionNotRunnableError):
        error_code = ContentPipelineErrorCode.CRAWLER_SESSION_NOT_RUNNABLE
    elif isinstance(error, PipelineTelegramProviderUnavailableError):
        error_code = ContentPipelineErrorCode.TELEGRAM_PROVIDER_UNAVAILABLE
    elif isinstance(error, PipelineTelegramMalformedMessageError):
        error_code = ContentPipelineErrorCode.TELEGRAM_MALFORMED_MESSAGE
    else:
        error_code = ContentPipelineErrorCode.TELEGRAM_PROVIDER_UNAVAILABLE
    return PipelineHTTPError(
        status_code=PIPELINE_ERROR_STATUS_CODES[error_code],
        payload=ContentPipelineErrorResponse(code=error_code, detail=str(error)),
        headers=headers,
    )


__all__ = [
    "CrawlerOperationsServiceDep",
    "CrawlerRuntimeDep",
    "MeilisearchSyncClientDep",
    "OperatorTokenDep",
    "PIPELINE_ERROR_RESPONSES",
    "PIPELINE_ERROR_STATUS_CODES",
    "PIPELINE_OPERATOR_TOKEN_HEADER_NAME",
    "PipelineHTTPError",
    "PipelineCrawlerIngestServiceDep",
    "PipelineIngestAcceptServiceDep",
    "PipelineIngestReadServiceDep",
    "PipelineItemReadServiceDep",
    "PipelineReplayServiceDep",
    "PipelineSmokeProofServiceDep",
    "PipelineSyncStatusServiceDep",
    "PipelineTelegramClientDep",
    "QdrantSimilarityClientDep",
    "QdrantSyncClientDep",
    "get_crawler_operations_service",
    "get_crawler_runtime",
    "get_telegram_session_manager",
    "get_meilisearch_sync_client",
    "get_pipeline_item_read_service",
    "get_pipeline_ingest_accept_service",
    "get_pipeline_crawler_ingest_service",
    "get_pipeline_ingest_read_service",
    "get_pipeline_replay_service",
    "get_pipeline_smoke_proof_service",
    "get_pipeline_sync_status_service",
    "get_pipeline_telegram_client",
    "get_qdrant_similarity_client",
    "get_qdrant_sync_client",
    "pipeline_http_exception_handler",
    "require_pipeline_operator_token",
    "to_pipeline_http_error",
    "TelegramSessionManagerDep",
]
