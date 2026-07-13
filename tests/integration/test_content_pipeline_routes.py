"""Integration tests for the operator-facing content-pipeline HTTP routes."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image
from sqlalchemy import func, select

from memexpert.api.dependencies.pipeline import (
    PIPELINE_OPERATOR_TOKEN_HEADER_NAME,
    get_pipeline_ingest_accept_service,
    get_pipeline_ingest_read_service,
    get_pipeline_item_read_service,
    get_pipeline_replay_service,
    get_pipeline_sync_status_service,
)
from memexpert.core.classification import ClassificationResult
from memexpert.core.config import get_settings
from memexpert.core.ocr import OCRExtractionResult
from memexpert.core.qdrant import QdrantSimilarityMatch
from memexpert.core.voyage import VoyageEmbeddingResult
from memexpert.ingest.accept_service import PipelineIngestAcceptService
from memexpert.ingest.read_service import PipelineIngestReadService
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptResult, IngestAcceptSource, IngestRequestRead
from memexpert.ingest.target_collection_metadata import TARGET_COLLECTION_ID_METADATA_KEY
from memexpert.media.contracts import NormalizedMediaResult, UploadMediaDetails
from memexpert.models.base import utcnow
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeSource,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    IngestSourceKind,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    SourceAttachReason,
    SourcePlatform,
)
from memexpert.pipeline.constants import SYNC_REPLAY_BATCH_MAX
from memexpert.pipeline.items import PipelineItemReadService
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.pipeline.sync_status import PipelineSyncStatusService
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineItemDetail,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineStageJournalRead,
)
from memexpert.services import (
    PipelineItemNotFoundError,
    PipelineReplayNotAllowedError,
    PipelineUnsupportedMediaTypeError,
)

if TYPE_CHECKING:
    from datetime import datetime

    from aio_pika.abc import HeadersType
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

@dataclass(slots=True)
class StubPipelineService:
    """Simple async service double for route-contract and OpenAPI tests."""

    item_result: ContentPipelineItemRead | None = None
    list_result: tuple[ContentPipelineItemRead, ...] = ()
    replay_result: ContentPipelineReplayAccepted | None = None
    item_error: Exception | None = None
    list_error: Exception | None = None
    replay_error: Exception | None = None
    item_calls: list[uuid.UUID] = field(default_factory=list)
    list_calls: list[dict[str, object]] = field(default_factory=list)
    replay_calls: list[dict[str, object]] = field(default_factory=list)

    async def get_item(self, meme_file_id: uuid.UUID) -> ContentPipelineItemRead:
        self.item_calls.append(meme_file_id)
        if self.item_error is not None:
            raise self.item_error
        assert self.item_result is not None
        return self.item_result

    async def list_items(
        self,
        *,
        filter_by: ContentPipelineItemFilter,
        limit: int,
        stuck_after_seconds: int,
    ) -> tuple[ContentPipelineItemRead, ...]:
        self.list_calls.append(
            {
                "filter_by": filter_by,
                "limit": limit,
                "stuck_after_seconds": stuck_after_seconds,
            }
        )
        if self.list_error is not None:
            raise self.list_error
        return self.list_result

    async def replay_item(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage | None,
    ) -> ContentPipelineReplayAccepted:
        self.replay_calls.append({"meme_file_id": meme_file_id, "stage": stage})
        if self.replay_error is not None:
            raise self.replay_error
        assert self.replay_result is not None
        return self.replay_result


@dataclass(slots=True)
class StubIngestAcceptService:
    """Async service double for the raw upload accept route."""

    result: IngestAcceptResult | None = None
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def accept_bytes(
        self,
        *,
        source: IngestAcceptSource,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> IngestAcceptResult:
        self.calls.append(
            {
                "source": source,
                "filename": filename,
                "content_type": content_type,
                "media_bytes": media_bytes,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class StubIngestReadService:
    """Async service double for raw ingest-request read routes."""

    request: IngestRequestRead
    list_calls: list[dict[str, object]] = field(default_factory=list)
    detail_calls: list[uuid.UUID] = field(default_factory=list)

    async def list_requests(
        self,
        *,
        status: PipelineIngestRequestStatus | None,
        limit: int,
    ) -> tuple[IngestRequestRead, ...]:
        self.list_calls.append({"status": status, "limit": limit})
        return (self.request,)

    async def get_request(self, ingest_request_id: uuid.UUID) -> IngestRequestRead:
        self.detail_calls.append(ingest_request_id)
        return self.request


def _override_focused_pipeline_services(
    app: FastAPI,
    *,
    item_read_service: object | None = None,
    replay_service: object | None = None,
    sync_status_service: object | None = None,
) -> None:
    if item_read_service is not None:
        app.dependency_overrides[get_pipeline_item_read_service] = lambda: item_read_service
    if replay_service is not None:
        app.dependency_overrides[get_pipeline_replay_service] = lambda: replay_service
    if sync_status_service is not None:
        app.dependency_overrides[get_pipeline_sync_status_service] = lambda: sync_status_service


def _override_real_pipeline_services(
    app: FastAPI,
    session: AsyncSession,
    *,
    broker: RecordingBroker | None = None,
) -> None:
    _override_focused_pipeline_services(
        app,
        item_read_service=PipelineItemReadService(session),
        replay_service=PipelineReplayService(session, broker=broker),
        sync_status_service=PipelineSyncStatusService(session),
    )


@dataclass(slots=True)
class FakeStorageClient:
    """Small sync S3-compatible client used by the route-backed real service tests."""

    put_calls: list[dict[str, object]] = field(default_factory=list)
    delete_calls: list[dict[str, object]] = field(default_factory=list)

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object:
        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
                "ContentLength": ContentLength,
            }
        )
        return {"ETag": "fake"}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        return {"DeleteMarker": True}


@dataclass(slots=True)
class RecordingBroker:
    """Broker double used to observe route-driven replay dispatches."""

    events: list[ContentPipelineDispatchEvent] = field(default_factory=list)
    publish_calls: list[dict[str, object]] = field(default_factory=list)

    async def publish(
        self,
        message: object,
        /,
        queue: str = "",
        exchange: str | None = None,
        *,
        routing_key: str = "",
        mandatory: bool = True,
        persist: bool = False,
        content_type: str | None = None,
        headers: HeadersType | None = None,
        message_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> object:
        self.publish_calls.append(
            {
                "payload": message,
                "queue": queue,
                "exchange": exchange,
                "routing_key": routing_key,
                "mandatory": mandatory,
                "persist": persist,
                "content_type": content_type,
                "headers": headers,
                "message_id": message_id,
                "timestamp": timestamp,
            }
        )
        self.events.append(ContentPipelineDispatchEvent.model_validate(message))
        return None


def build_png_bytes(*, color: tuple[int, int, int] = (255, 128, 0)) -> bytes:
    """Generate a tiny multipart upload body in memory for route tests."""

    image = Image.new("RGB", (4, 4), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _distinct_upload_media_details(*, tag: str) -> UploadMediaDetails:
    """Build upload details with a unique perceptual hash for merge route fixtures."""

    perceptual_hash = (tag * 16)[:16]
    return UploadMediaDetails(
        media_type=ContentKind.IMAGE,
        mime_type="image/png",
        width=32,
        height=32,
        file_size_bytes=64,
        perceptual_hash=perceptual_hash,
    )


async def _seed_pipeline_item(
    session: AsyncSession,
    *,
    source_id: str,
    post_id: str,
    phash_tag: str,
    filename: str = "seed.png",
    media_bytes: bytes | None = None,
    source_kind: IngestSourceKind = IngestSourceKind.OPERATOR_UPLOAD,
) -> ContentPipelineItemRead:
    details = _distinct_upload_media_details(tag=phash_tag)
    resolved_bytes = media_bytes or f"seed-bytes:{source_id}:{post_id}:{phash_tag}".encode()
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    event_id = uuid.uuid7()
    now = utcnow()
    session.add(
        Meme(
            id=meme_id,
            media_type=details.media_type,
            primary_file_id=meme_file_id,
            language=ContentLanguage.NONE,
            is_public=source_kind is IngestSourceKind.PUBLIC_CRAWLER,
        )
    )
    await session.flush()
    session.add_all(
        [
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.PENDING,
                width=details.width,
                height=details.height,
                file_size_bytes=len(resolved_bytes),
                mime_type="image/png",
                s3_original_key=f"pipeline/originals/{meme_file_id}/original.{filename.rsplit('.', 1)[-1]}",
                perceptual_hash=details.perceptual_hash,
                sha256_hex=hashlib.sha256(resolved_bytes).hexdigest(),
                ingest_origin=IngestFileOrigin.NEW_MEME,
            ),
            MemeSource(
                file_id=meme_file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id=source_id,
                post_id=post_id,
                source_kind=source_kind,
                is_first_source=True,
                source_alive=True,
                attach_reason=SourceAttachReason.NEW_FILE,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=event_id,
                is_retryable=False,
                started_at=now,
                finished_at=now,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.PENDING,
                attempt_count=0,
                last_event_id=event_id,
                is_retryable=True,
            ),
        ]
    )
    await session.commit()
    return await PipelineItemReadService(session).get_item(meme_file_id)


def _normalized_media_result(meme_file_id: uuid.UUID, *, web_video: bool = True) -> NormalizedMediaResult:
    """Return a deterministic transcode result for the enriched detail route tests."""

    return NormalizedMediaResult(
        quality_score=0.77,
        blur_hash="L4AS~q00~q.8%MRjM{Rj00IU%MRj",
        web_video_object_key=f"pipeline/derived/{meme_file_id}/web.mp4" if web_video else None,
        web_video_bytes=b"detail-route-transcode-bytes" if web_video else None,
    )


def _ocr_result(*, fallback_used: bool, low_confidence: bool, confidence: float | None) -> OCRExtractionResult:
    """Return a deterministic OCR result with operator-visible fallback state."""

    return OCRExtractionResult(
        engine="paddleocr",
        fallback_engine="ocr-command" if fallback_used else None,
        fallback_used=fallback_used,
        low_confidence=low_confidence,
        confidence=confidence,
        language=ContentLanguage.EN,
        extracted_text="enriched detail",
        source_object_key="pipeline/derived/example/web.png",
    )


def _voyage_embedding(*, input_hash: str) -> VoyageEmbeddingResult:
    """Return a deterministic 1024-dim voyage vector for the enriched detail route tests."""

    return VoyageEmbeddingResult(
        model="voyage-multimodal-3.5",
        dimensions=1024,
        vector=tuple(0.001 * index for index in range(1024)),
        input_hash=input_hash,
    )


def build_item() -> ContentPipelineItemRead:
    """Construct a stable pipeline item payload for route-contract assertions."""

    now = utcnow()
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    return ContentPipelineItemRead(
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        current_stage=ContentPipelineStage.TRANSCODE,
        current_status=ContentPipelineStageStatus.PENDING,
        original_object_key=f"pipeline/originals/{meme_file_id}/original.png",
        web_video_object_key=None,
        last_event_id=uuid.uuid7(),
        normalized_reason=None,
        last_error_text=None,
        attempt_count=0,
        stages=(
            ContentPipelineStageJournalRead(
                id=uuid.uuid7(),
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=uuid.uuid7(),
                normalized_reason=None,
                last_error_text=None,
                is_retryable=False,
                retry_after=None,
                started_at=now,
                finished_at=now,
                created_at=now,
                updated_at=now,
            ),
            ContentPipelineStageJournalRead(
                id=uuid.uuid7(),
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.PENDING,
                attempt_count=0,
                last_event_id=uuid.uuid7(),
                normalized_reason=None,
                last_error_text=None,
                is_retryable=True,
                retry_after=None,
                started_at=None,
                finished_at=None,
                created_at=now,
                updated_at=now,
            ),
        ),
    )


def build_ingest_request(
    *,
    request_id: uuid.UUID | None = None,
    status: PipelineIngestRequestStatus = PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
) -> IngestRequestRead:
    """Construct a stable raw ingest-request payload for route assertions."""

    now = utcnow()
    resolved_request_id = request_id or uuid.uuid7()
    return IngestRequestRead(
        id=resolved_request_id,
        source_platform=SourcePlatform.TELEGRAM,
        source_id="channel-one",
        post_id="101",
        source_kind=IngestSourceKind.OPERATOR_UPLOAD,
        uploader_user_id=None,
        user_metadata={},
        source_metadata={"view_count": 7},
        declared_filename="sample.png",
        declared_content_type="image/png",
        temp_original_object_key=f"pipeline/temp-originals/{resolved_request_id}/original.png",
        sha256_hex="a" * 64,
        file_size_bytes=128,
        status=status,
        failure_code=None,
        failure_detail=None,
        attempt_count=0,
        locked_at=None,
        materialized_meme_id=None,
        materialized_meme_file_id=None,
        matched_meme_file_id=None,
        source_attach_reason=None,
        created_at=now,
        updated_at=now,
    )


async def test_pipeline_routes_require_operator_token_and_accept_real_multipart_uploads(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    ingest_request = build_ingest_request()
    uploader_user_id = uuid.uuid7()
    target_collection_id = uuid.uuid7()
    stub_service = StubIngestAcceptService(
        result=IngestAcceptResult(
            ingest_request=ingest_request,
            outcome=IngestAcceptOutcome.ACCEPTED_ASYNC,
        )
    )
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_pipeline_ingest_accept_service] = lambda: stub_service

    try:
        rejected_response = await client.post(
            "/api/v1/pipeline/uploads",
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
                "uploader_user_id": str(uploader_user_id),
                "target_collection_id": str(target_collection_id),
                "view_count": "7",
            },
            files={"file": ("sample.png", build_png_bytes(), "image/png")},
        )
        blank_header_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: "   "},
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
                "view_count": "7",
            },
            files={"file": ("sample.png", build_png_bytes(), "image/png")},
        )
        created_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
                "uploader_user_id": str(uploader_user_id),
                "target_collection_id": str(target_collection_id),
                "view_count": "7",
            },
            files={"file": ("sample.png", build_png_bytes(), "image/png")},
        )

        assert rejected_response.status_code == 401
        assert rejected_response.json() == {
            "code": "invalid_operator_token",
            "detail": "A valid operator token is required for the pipeline surface.",
        }
        assert blank_header_response.status_code == 401
        assert blank_header_response.json()["code"] == "invalid_operator_token"

        assert created_response.status_code == 202
        assert created_response.json()["id"] == str(ingest_request.id)
        assert created_response.json()["status"] == "media_inspect_pending"
        assert "meme_file_id" not in created_response.json()
        assert len(stub_service.calls) == 1
        upload_call = stub_service.calls[0]
        source = upload_call["source"]
        assert isinstance(source, IngestAcceptSource)
        assert source.source_platform.value == "telegram"
        assert source.source_id == "channel-one"
        assert source.post_id == "101"
        assert source.uploader_user_id == uploader_user_id
        assert source.source_kind is IngestSourceKind.OPERATOR_UPLOAD
        assert source.user_metadata[TARGET_COLLECTION_ID_METADATA_KEY] == str(target_collection_id)
        assert source.view_count == 7
        assert upload_call["filename"] == "sample.png"
        assert upload_call["content_type"] == "image/png"
        assert upload_call["media_bytes"] == build_png_bytes()
    finally:
        app.dependency_overrides.clear()


async def test_pipeline_upload_route_returns_ok_for_source_replay(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    ingest_request = build_ingest_request(status=PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE)
    stub_service = StubIngestAcceptService(
        result=IngestAcceptResult(
            ingest_request=ingest_request,
            outcome=IngestAcceptOutcome.SOURCE_REPLAY,
        )
    )
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_pipeline_ingest_accept_service] = lambda: stub_service

    try:
        response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
                "view_count": "7",
            },
            files={"file": ("sample.png", build_png_bytes(), "image/png")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == str(ingest_request.id)
    assert response.json()["status"] == "resolved_sha_duplicate"


async def test_pipeline_routes_expose_list_detail_replay_and_openapi_registration(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    item = build_item()
    ingest_request = build_ingest_request()
    replay_result = ContentPipelineReplayAccepted(
        meme_file_id=item.meme_file_id,
        replay_event_id=uuid.uuid7(),
        stage=ContentPipelineStage.TRANSCODE,
        attempt=2,
    )
    stub_service = StubPipelineService(
        item_result=item,
        list_result=(item,),
        replay_result=replay_result,
    )
    stub_ingest_read_service = StubIngestReadService(request=ingest_request)
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    _override_focused_pipeline_services(
        app,
        item_read_service=stub_service,
        replay_service=stub_service,
    )
    app.dependency_overrides[get_pipeline_ingest_read_service] = lambda: stub_ingest_read_service

    try:
        ingest_list_response = await client.get(
            "/api/v1/pipeline/ingest-requests",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            params={"status": "media_inspect_pending", "limit": 25},
        )
        ingest_detail_response = await client.get(
            f"/api/v1/pipeline/ingest-requests/{ingest_request.id}",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        list_response = await client.get(
            "/api/v1/pipeline/items",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            params={"filter": "failed", "limit": 25, "stuck_after_seconds": 120},
        )
        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item.meme_file_id}",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        replay_response = await client.post(
            f"/api/v1/pipeline/items/{item.meme_file_id}/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"stage": "transcode"},
        )
        openapi_response = await client.get("/openapi.json")

        paths = openapi_response.json()["paths"]
        ingest_list_parameters = paths["/api/v1/pipeline/ingest-requests"]["get"]["parameters"]
        ingest_detail_parameters = paths["/api/v1/pipeline/ingest-requests/{ingest_request_id}"]["get"]["parameters"]
        list_parameters = paths["/api/v1/pipeline/items"]["get"]["parameters"]
        detail_parameters = paths["/api/v1/pipeline/items/{meme_file_id}"]["get"]["parameters"]
        replay_parameters = paths["/api/v1/pipeline/items/{meme_file_id}/replay"]["post"]["parameters"]

        assert ingest_list_response.status_code == 200
        assert ingest_list_response.json()[0]["id"] == str(ingest_request.id)
        assert ingest_detail_response.status_code == 200
        assert ingest_detail_response.json()["status"] == "media_inspect_pending"
        assert stub_ingest_read_service.list_calls == [
            {"status": PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING, "limit": 25}
        ]
        assert stub_ingest_read_service.detail_calls == [ingest_request.id]

        assert list_response.status_code == 200
        assert list_response.json()[0]["meme_id"] == str(item.meme_id)
        assert stub_service.list_calls == [
            {
                "filter_by": ContentPipelineItemFilter.FAILED,
                "limit": 25,
                "stuck_after_seconds": 120,
            }
        ]

        assert detail_response.status_code == 200
        assert detail_response.json()["meme_id"] == str(item.meme_id)
        assert stub_service.item_calls == [item.meme_file_id]

        assert replay_response.status_code == 202
        assert replay_response.json() == replay_result.model_dump(mode="json")
        assert stub_service.replay_calls == [
            {
                "meme_file_id": item.meme_file_id,
                "stage": ContentPipelineStage.TRANSCODE,
            }
        ]

        assert "/api/v1/pipeline/items" in paths
        assert "/api/v1/pipeline/items/{meme_file_id}" in paths
        assert "/api/v1/pipeline/items/{meme_file_id}/replay" in paths
        assert "/api/v1/pipeline/ingest-requests" in paths
        assert "/api/v1/pipeline/ingest-requests/{ingest_request_id}" in paths
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in ingest_list_parameters
        )
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in ingest_detail_parameters
        )
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in list_parameters
        )
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in detail_parameters
        )
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in replay_parameters
        )
    finally:
        app.dependency_overrides.clear()


async def test_pipeline_upload_route_rejects_missing_file_and_blank_provenance_before_service_calls(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    stub_service = StubIngestAcceptService()
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_pipeline_ingest_accept_service] = lambda: stub_service

    try:
        missing_file_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
            },
        )
        blank_provenance_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "   ",
                "post_id": "101",
            },
            files={"file": ("sample.png", build_png_bytes(), "image/png")},
        )
        missing_owner_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
                "target_collection_id": str(uuid.uuid7()),
            },
            files={"file": ("sample.png", build_png_bytes(), "image/png")},
        )

        assert missing_file_response.status_code == 422
        assert blank_provenance_response.status_code == 400
        assert blank_provenance_response.json() == {
            "code": "pipeline_payload_invalid",
            "detail": "Value error, source provenance fields must not be blank.",
        }
        assert missing_owner_response.status_code == 400
        assert missing_owner_response.json() == {
            "code": "pipeline_payload_invalid",
            "detail": "uploader_user_id is required when target_collection_id is provided.",
        }
        assert stub_service.calls == []
    finally:
        app.dependency_overrides.clear()


async def test_pipeline_routes_map_service_errors_to_typed_http_payloads(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    item_id = uuid.uuid7()
    stub_service = StubPipelineService(
        item_error=PipelineItemNotFoundError(f"Pipeline item {item_id} does not exist."),
        replay_error=PipelineReplayNotAllowedError("No failed retryable stage exists for this pipeline item."),
    )
    stub_ingest_accept_service = StubIngestAcceptService(
        error=PipelineUnsupportedMediaTypeError("Uploaded media type is not supported."),
    )
    _override_focused_pipeline_services(
        app,
        item_read_service=stub_service,
        replay_service=stub_service,
    )
    app.dependency_overrides[get_pipeline_ingest_accept_service] = lambda: stub_ingest_accept_service

    try:
        upload_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
            },
            files={"file": ("sample.txt", b"not-an-image", "text/plain")},
        )
        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item_id}",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        replay_response = await client.post(
            f"/api/v1/pipeline/items/{item_id}/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"stage": "transcode"},
        )

        assert upload_response.status_code == 415
        assert upload_response.json() == {
            "code": "pipeline_unsupported_media_type",
            "detail": "Uploaded media type is not supported.",
        }
        assert detail_response.status_code == 404
        assert detail_response.json() == {
            "code": "pipeline_item_not_found",
            "detail": f"Pipeline item {item_id} does not exist.",
        }
        assert replay_response.status_code == 409
        assert replay_response.json() == {
            "code": "pipeline_replay_not_allowed",
            "detail": "No failed retryable stage exists for this pipeline item.",
        }
    finally:
        app.dependency_overrides.clear()


async def test_pipeline_upload_route_creates_raw_ingest_request_separate_from_items(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    accept_service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)
    read_service = PipelineIngestReadService(migrated_db_session)
    item_service = PipelineItemReadService(migrated_db_session)
    app.dependency_overrides[get_pipeline_ingest_accept_service] = lambda: accept_service
    app.dependency_overrides[get_pipeline_ingest_read_service] = lambda: read_service
    _override_focused_pipeline_services(app, item_read_service=item_service)

    try:
        upload_response = await client.post(
            "/api/v1/pipeline/uploads",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            data={
                "source_platform": "telegram",
                "source_id": "raw-route-channel",
                "post_id": "1701",
                "view_count": "7",
            },
            files={"file": ("raw-route.png", build_png_bytes(color=(1, 2, 3)), "image/png")},
        )
        ingest_list_response = await client.get(
            "/api/v1/pipeline/ingest-requests",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        items_response = await client.get(
            "/api/v1/pipeline/items",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            params={"filter": "all"},
        )
    finally:
        app.dependency_overrides.clear()

    assert upload_response.status_code == 202
    upload_body = upload_response.json()
    assert upload_body["status"] == "media_inspect_pending"
    assert upload_body["source_id"] == "raw-route-channel"
    assert upload_body["temp_original_object_key"].startswith("pipeline/temp-originals/")
    assert "meme_file_id" not in upload_body
    assert len(storage_client.put_calls) == 1
    assert storage_client.put_calls[0]["Key"] == upload_body["temp_original_object_key"]
    assert storage_client.delete_calls == []
    assert ingest_list_response.status_code == 200
    assert ingest_list_response.json()[0]["id"] == upload_body["id"]
    assert items_response.status_code == 200
    assert items_response.json() == []

    meme_count = await migrated_db_session.scalar(select(func.count()).select_from(Meme))
    meme_file_count = await migrated_db_session.scalar(select(func.count()).select_from(MemeFile))
    request_count = await migrated_db_session.scalar(select(func.count()).select_from(PipelineIngestRequest))
    outbox = await migrated_db_session.scalar(select(RabbitMQOutboxMessage))
    assert meme_count == 0
    assert meme_file_count == 0
    assert request_count == 1
    assert outbox is not None
    assert outbox.status is RabbitMQOutboxMessageStatus.PENDING
    assert outbox.aggregate_id == upload_body["id"]


async def test_pipeline_routes_list_failed_items_and_reject_replay_guards_with_real_service(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    broker = RecordingBroker()
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )
    _override_real_pipeline_services(app, migrated_db_session, broker=broker)

    try:
        item = await _seed_pipeline_item(
            migrated_db_session,
            source_id="memexpert-channel",
            post_id="9001",
            phash_tag="r",
            filename="route-sample.png",
        )

        guard_response = await client.post(
            f"/api/v1/pipeline/items/{item.meme_file_id}/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"stage": "transcode"},
        )
        unknown_response = await client.post(
            f"/api/v1/pipeline/items/{uuid.uuid7()}/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"stage": "transcode"},
        )

        assert guard_response.status_code == 409
        assert guard_response.json() == {
            "code": "pipeline_replay_not_allowed",
            "detail": "Stage transcode is not in a retryable failed state.",
        }
        assert unknown_response.status_code == 404
        assert unknown_response.json()["code"] == "pipeline_item_not_found"

        dispatch_event_id = item.last_event_id
        assert dispatch_event_id is not None
        await service.mark_stage_failed(
            meme_file_id=item.meme_file_id,
            stage=ContentPipelineStage.TRANSCODE,
            attempt=1,
            event_id=dispatch_event_id,
            normalized_reason="forced_transcode_failure",
            last_error_text="Stub transcode failed for route verification.",
            retryable=True,
        )

        list_response = await client.get(
            "/api/v1/pipeline/items",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            params={"filter": "failed"},
        )
        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item.meme_file_id}",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )

        assert list_response.status_code == 200
        assert list_response.json() == [
            {
                "meme_id": str(item.meme_id),
                "meme_file_id": str(item.meme_file_id),
                "sha256_hex": detail_response.json()["sha256_hex"],
                "ingest_origin": "new_meme",
                "matched_meme_file_id": None,
                "latest_source_id": detail_response.json()["latest_source_id"],
                "latest_source_attach_reason": "new_file",
                "latest_source_matched_meme_file_id": None,
                "current_stage": "transcode",
                "current_status": "failed",
                "original_object_key": item.original_object_key,
                "web_video_object_key": None,
                "last_event_id": str(dispatch_event_id),
                "normalized_reason": "forced_transcode_failure",
                "last_error_text": "Stub transcode failed for route verification.",
                "attempt_count": 1,
                "stages": detail_response.json()["stages"],
            }
        ]
        assert detail_response.status_code == 200
        assert detail_response.json()["current_status"] == "failed"
        assert detail_response.json()["normalized_reason"] == "forced_transcode_failure"
        assert detail_response.json()["last_error_text"] == "Stub transcode failed for route verification."
        assert detail_response.json()["attempt_count"] == 1
    finally:
        app.dependency_overrides.clear()


async def _seed_detail_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
    source_id: str,
    post_id: str,
    phash_tag: str,
    source_kind: IngestSourceKind = IngestSourceKind.OPERATOR_UPLOAD,
) -> uuid.UUID:
    """Create a pipeline item with a unique perceptual hash for detail-route tests."""

    _ = (storage_client, broker)
    item = await _seed_pipeline_item(
        session,
        source_id=source_id,
        post_id=post_id,
        phash_tag=phash_tag,
        filename=f"{phash_tag}.png",
        media_bytes=f"detail-route-bytes:{source_id}:{post_id}:{phash_tag}".encode(),
        source_kind=source_kind,
    )
    return item.meme_file_id


async def test_pipeline_detail_route_returns_empty_projections_before_ocr(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """Items that have not reached OCR must expose no stub OCR/classify text."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    broker = RecordingBroker()
    _override_real_pipeline_services(app, migrated_db_session, broker=broker)

    try:
        item = await _seed_pipeline_item(
            migrated_db_session,
            source_id="detail-early",
            post_id="9101",
            phash_tag="e",
            filename="early.png",
            media_bytes=b"detail-early-bytes",
        )

        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item.meme_file_id}/detail",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    detail = ContentPipelineItemDetail.model_validate(detail_response.json())
    assert detail.meme_file_id == item.meme_file_id
    assert detail.current_stage is ContentPipelineStage.TRANSCODE
    assert detail.current_status is ContentPipelineStageStatus.PENDING
    # OCR has not run — the projection must be absent.
    assert detail.ocr is None
    # Classification must report unknown, never defaulted to false.
    assert detail.classification.classified is False
    assert detail.classification.is_nsfw is None
    # No meme_ready event has been emitted.
    assert detail.ready_event is None
    # Merge lineage is empty.
    assert detail.merge.as_source == ()
    assert detail.merge.as_target == ()
    # Canonical context still reports the new meme as the canonical primary.
    assert detail.canonical is not None
    assert detail.canonical.canonical_meme_id == item.meme_id
    assert detail.canonical.is_canonical_primary is True
    assert detail.canonical.ocr_text is None


async def test_pipeline_detail_route_returns_ocr_and_unknown_classification_after_ocr(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """A file past OCR but before classify must expose OCR truth but never fake is_nsfw."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    broker = RecordingBroker()
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )
    _override_real_pipeline_services(app, migrated_db_session, broker=broker)

    try:
        item = await _seed_pipeline_item(
            migrated_db_session,
            source_id="detail-ocr",
            post_id="9102",
            phash_tag="d",
            filename="ocr.png",
            media_bytes=b"detail-ocr-bytes",
        )
        await service.complete_transcode_stage(
            meme_file_id=item.meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            result=_normalized_media_result(item.meme_file_id),
        )
        await service.complete_ocr_stage(
            meme_file_id=item.meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            result=_ocr_result(fallback_used=True, low_confidence=True, confidence=0.42),
        )

        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item.meme_file_id}/detail",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    detail = ContentPipelineItemDetail.model_validate(detail_response.json())
    assert detail.current_stage is ContentPipelineStage.EMBED
    assert detail.current_status is ContentPipelineStageStatus.PENDING
    assert detail.ocr is not None
    assert detail.ocr.fallback_used is True
    assert detail.ocr.low_confidence is True
    assert detail.ocr.confidence == 0.42
    assert detail.ocr.extracted_text == "enriched detail"
    assert detail.ocr.language is ContentLanguage.EN
    # Classification still unknown until classify stage succeeds.
    assert detail.classification.classified is False
    assert detail.classification.is_nsfw is None
    assert detail.ready_event is None
    assert detail.canonical is not None
    # Canonical still has ocr_text unset because classify owns that write.
    assert detail.canonical.ocr_text is None


async def test_pipeline_detail_route_reports_merge_and_classify_and_ready(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """A fully processed merged item must expose merge + classify + meme_ready truth."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()

    # Seed the older canonical item and drive it through the heavy chain.
    older_meme_file_id = await _seed_detail_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="detail-merge-older",
        post_id="9200",
        phash_tag="o",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )
    older_service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )
    older_meme = (
        await PipelineItemReadService(migrated_db_session).get_item(older_meme_file_id)
    ).meme_id
    await older_service.complete_transcode_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=_normalized_media_result(older_meme_file_id),
    )
    await older_service.complete_ocr_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=_ocr_result(fallback_used=False, low_confidence=False, confidence=0.91),
    )
    _ = await older_service.complete_embed_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=_voyage_embedding(input_hash="1" * 64),
        similarity_matches=(),
    )
    await older_service.complete_classify_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=ClassificationResult(
            model="memexpert-nsfw-v1",
            is_nsfw=False,
            nsfw_score=0.05,
        ),
    )

    # Seed a newer item with a distinct hash and merge it into the older canonical.
    newer_meme_file_id = await _seed_detail_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="detail-merge-newer",
        post_id="9201",
        phash_tag="n",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )
    newer_service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )
    await newer_service.complete_transcode_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=_normalized_media_result(newer_meme_file_id),
    )
    await newer_service.complete_ocr_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=_ocr_result(fallback_used=False, low_confidence=False, confidence=0.93),
    )
    _ = await newer_service.complete_embed_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=_voyage_embedding(input_hash="2" * 64),
        similarity_matches=(
            QdrantSimilarityMatch(
                meme_file_id=older_meme_file_id,
                meme_id=older_meme,
                similarity_score=0.97,
            ),
        ),
    )
    # After the merge, the newer file lives under the older canonical meme.
    await newer_service.complete_classify_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=ClassificationResult(
            model="memexpert-nsfw-v1",
            is_nsfw=True,
            nsfw_score=0.82,
        ),
    )

    _override_real_pipeline_services(app, migrated_db_session, broker=broker)

    try:
        detail_response = await client.get(
            f"/api/v1/pipeline/items/{newer_meme_file_id}/detail",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        s01_response = await client.get(
            f"/api/v1/pipeline/items/{newer_meme_file_id}",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    assert s01_response.status_code == 200

    detail = ContentPipelineItemDetail.model_validate(detail_response.json())
    assert detail.classification.classified is True
    assert detail.classification.is_nsfw is True
    assert detail.ocr is not None
    assert detail.ocr.fallback_used is False
    assert detail.ready_event is not None
    # The merge projection must show the newer file as the source that moved under older_meme.
    assert len(detail.merge.as_source) == 1
    participation = detail.merge.as_source[0]
    assert participation.target_meme_id == older_meme
    assert participation.similarity_score == 0.97
    assert participation.merge_reason == "high_similarity_match"
    # Canonical context now points at the older meme.
    assert detail.canonical is not None
    assert detail.canonical.canonical_meme_id == older_meme

    # S01 contract stayed compatible: the enriched surface is a strict superset,
    # never replacing list/detail/replay payloads.
    s01_payload = s01_response.json()
    for field_name in (
        "meme_id",
        "meme_file_id",
        "current_stage",
        "current_status",
        "original_object_key",
        "web_video_object_key",
        "last_event_id",
        "normalized_reason",
        "last_error_text",
        "attempt_count",
        "stages",
    ):
        assert field_name in s01_payload
        assert s01_payload[field_name] == detail_response.json()[field_name]


async def test_pipeline_detail_route_reports_blocked_items_without_defaulted_classify(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """A blocked embed item must stay visible and expose the failure surface honestly."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    broker = RecordingBroker()
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )
    _override_real_pipeline_services(app, migrated_db_session, broker=broker)

    try:
        item = await _seed_pipeline_item(
            migrated_db_session,
            source_id="detail-blocked",
            post_id="9300",
            phash_tag="b",
            filename="blocked.png",
            media_bytes=b"detail-blocked-bytes",
        )
        await service.complete_transcode_stage(
            meme_file_id=item.meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            result=_normalized_media_result(item.meme_file_id),
        )
        await service.complete_ocr_stage(
            meme_file_id=item.meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            result=_ocr_result(fallback_used=False, low_confidence=False, confidence=0.88),
        )
        await service.mark_stage_failed(
            meme_file_id=item.meme_file_id,
            stage=ContentPipelineStage.EMBED,
            attempt=1,
            event_id=uuid.uuid7(),
            normalized_reason="embed_provider_blocked",
            last_error_text="voyage quota exceeded",
            retryable=True,
        )

        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item.meme_file_id}/detail",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    detail = ContentPipelineItemDetail.model_validate(detail_response.json())
    assert detail.current_stage is ContentPipelineStage.EMBED
    assert detail.current_status is ContentPipelineStageStatus.FAILED
    assert detail.normalized_reason == "embed_provider_blocked"
    assert detail.last_error_text == "voyage quota exceeded"
    # OCR is durable truth and should still appear.
    assert detail.ocr is not None
    assert detail.ocr.confidence == 0.88
    # Classification must still be unknown — no defaulted False.
    assert detail.classification.classified is False
    assert detail.classification.is_nsfw is None
    assert detail.ready_event is None


async def test_pipeline_detail_route_returns_404_for_unknown_item_and_registers_openapi(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """The detail route must reject unknown ids and appear in the OpenAPI schema."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    service = PipelineItemReadService(migrated_db_session)
    _override_focused_pipeline_services(app, item_read_service=service)
    unknown_id = uuid.uuid7()

    try:
        unknown_response = await client.get(
            f"/api/v1/pipeline/items/{unknown_id}/detail",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        openapi_response = await client.get("/openapi.json")
    finally:
        app.dependency_overrides.clear()

    assert unknown_response.status_code == 404
    assert unknown_response.json()["code"] == "pipeline_item_not_found"

    paths = openapi_response.json()["paths"]
    assert "/api/v1/pipeline/items/{meme_file_id}/detail" in paths
    # S01 routes are still present — the enriched surface is additive.
    assert "/api/v1/pipeline/items/{meme_file_id}" in paths
    assert "/api/v1/pipeline/items" in paths
    assert "/api/v1/pipeline/items/{meme_file_id}/replay" in paths


async def _drive_item_to_classify_succeeded(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
    source_id: str,
    post_id: str,
    phash_tag: str,
    input_hash_seed: str,
) -> uuid.UUID:
    """Create + transcode + OCR + embed + classify a pipeline item for sync route tests."""

    meme_file_id = await _seed_detail_item(
        session,
        storage_client=storage_client,
        broker=broker,
        source_id=source_id,
        post_id=post_id,
        phash_tag=phash_tag,
    )
    service = PipelineStageCompletionService(session, broker=broker)
    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=_normalized_media_result(meme_file_id),
    )
    await service.complete_ocr_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=_ocr_result(fallback_used=False, low_confidence=False, confidence=0.91),
    )
    _ = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=_voyage_embedding(input_hash=input_hash_seed * 64),
        similarity_matches=(),
    )
    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=ClassificationResult(
            model="memexpert-nsfw-v1",
            is_nsfw=False,
            nsfw_score=0.05,
        ),
    )
    return meme_file_id


async def test_pipeline_qdrant_sync_status_route_returns_404_when_snapshot_missing(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """GET sync/qdrant must return 404 when the snapshot row has not been written yet."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-status-missing",
        post_id="9500",
        phash_tag="x",
        input_hash_seed="x",
    )
    _override_focused_pipeline_services(
        app,
        sync_status_service=PipelineSyncStatusService(migrated_db_session),
    )

    try:
        response = await client.get(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/qdrant",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["code"] == "pipeline_item_not_found"


async def test_pipeline_qdrant_sync_status_route_returns_synced_row(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """GET sync/qdrant must return the synced snapshot row once the worker writes it."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-status-synced",
        post_id="9501",
        phash_tag="y",
        input_hash_seed="y",
    )
    service = PipelineStageCompletionService(migrated_db_session, broker=broker)
    sync_event = uuid.uuid7()
    _ = await service.complete_sync_qdrant_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=sync_event,
        payload_preview={"point_id": str(meme_file_id), "tags": []},
    )
    _override_focused_pipeline_services(
        app,
        sync_status_service=PipelineSyncStatusService(migrated_db_session),
    )

    try:
        response = await client.get(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/qdrant",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "synced"
    assert body["target"] == "qdrant"
    assert body["attempt_count"] == 1
    assert body["last_preview"] is not None


async def test_pipeline_qdrant_sync_status_route_requires_operator_token(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """The GET sync/qdrant route must require the operator token header."""

    meme_file_id = uuid.uuid7()
    response = await client.get(f"/api/v1/pipeline/items/{meme_file_id}/sync/qdrant")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_operator_token"


async def test_pipeline_qdrant_sync_replay_route_happy_path(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """POST sync/qdrant/replay must reserve a new dispatch for an item past classify success."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-replay-happy",
        post_id="9502",
        phash_tag="z",
        input_hash_seed="z",
    )
    replay_service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=replay_service)

    try:
        response = await client.post(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/qdrant/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert body["stage"] == "sync_qdrant"
    assert body["meme_file_id"] == str(meme_file_id)


async def test_pipeline_qdrant_sync_replay_route_rejects_not_ready_items(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """POST sync/qdrant/replay must 409 when classify has not yet succeeded."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _seed_detail_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-replay-not-ready",
        post_id="9503",
        phash_tag="w",
    )
    replay_service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=replay_service)

    try:
        response = await client.post(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/qdrant/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["code"] == "pipeline_replay_not_allowed"


async def test_pipeline_qdrant_sync_replay_batch_route_happy_path(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """POST sync/qdrant/replay-batch must accept a bounded batch of ready items."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    first = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-batch-one",
        post_id="9504",
        phash_tag="a",
        input_hash_seed="1",
    )
    second = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-batch-two",
        post_id="9505",
        phash_tag="b",
        input_hash_seed="2",
    )
    replay_service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=replay_service)

    try:
        response = await client.post(
            "/api/v1/pipeline/sync/qdrant/replay-batch",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"meme_file_ids": [str(first), str(second)]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert len(body) == 2
    assert {item["meme_file_id"] for item in body} == {str(first), str(second)}
    for item in body:
        assert item["stage"] == "sync_qdrant"


async def test_pipeline_qdrant_sync_replay_batch_route_rejects_oversize_batches(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """The batch endpoint must reject requests larger than SYNC_REPLAY_BATCH_MAX."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-batch-oversize",
        post_id="9506",
        phash_tag="c",
        input_hash_seed="3",
    )
    service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=service)
    oversized_batch = [str(meme_file_id)] * (SYNC_REPLAY_BATCH_MAX + 1)

    try:
        response = await client.post(
            "/api/v1/pipeline/sync/qdrant/replay-batch",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"meme_file_ids": oversized_batch},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["code"] == "pipeline_replay_not_allowed"


async def test_pipeline_qdrant_sync_replay_batch_route_rejects_empty_body(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """FastAPI must surface Pydantic validation failures as 422 for empty batches."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    response = await client.post(
        "/api/v1/pipeline/sync/qdrant/replay-batch",
        headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        json={"meme_file_ids": []},
    )
    assert response.status_code == 422


async def test_pipeline_qdrant_sync_replay_route_registers_in_openapi(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """The new sync endpoints must appear in the generated OpenAPI schema."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    response = await client.get(
        "/openapi.json",
        headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
    )
    paths = response.json()["paths"]
    assert "/api/v1/pipeline/items/{meme_file_id}/sync/qdrant" in paths
    assert "/api/v1/pipeline/items/{meme_file_id}/sync/qdrant/replay" in paths
    assert "/api/v1/pipeline/sync/qdrant/replay-batch" in paths
    assert "/api/v1/pipeline/items/{meme_file_id}/sync/meili" in paths
    assert "/api/v1/pipeline/items/{meme_file_id}/sync/meili/replay" in paths
    assert "/api/v1/pipeline/sync/meili/replay-batch" in paths
    # S01 routes are still present — the new sync surface is additive.
    assert "/api/v1/pipeline/items/{meme_file_id}" in paths
    assert "/api/v1/pipeline/items/{meme_file_id}/replay" in paths


# --- T03: Meilisearch sync route tests --------------------------------------


async def test_pipeline_meili_sync_status_route_returns_404_when_snapshot_missing(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """GET sync/meili must return 404 when the snapshot row does not yet exist."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-status-missing",
        post_id="9600",
        phash_tag="1",
        input_hash_seed="1",
    )
    _override_focused_pipeline_services(
        app,
        sync_status_service=PipelineSyncStatusService(migrated_db_session),
    )

    try:
        response = await client.get(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/meili",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["code"] == "pipeline_item_not_found"


async def test_pipeline_meili_sync_status_route_returns_synced_row(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """GET sync/meili must return the synced snapshot row once the worker writes it."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-status-synced",
        post_id="9601",
        phash_tag="2",
        input_hash_seed="2",
    )
    service = PipelineStageCompletionService(migrated_db_session, broker=broker)
    sync_event = uuid.uuid7()
    _ = await service.complete_sync_meili_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=sync_event,
        payload_preview={"id": meme_file_id.hex, "tags": []},
    )
    _override_focused_pipeline_services(
        app,
        sync_status_service=PipelineSyncStatusService(migrated_db_session),
    )

    try:
        response = await client.get(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/meili",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "synced"
    assert body["target"] == "meilisearch"
    assert body["attempt_count"] == 1
    assert body["last_preview"] is not None


async def test_pipeline_meili_sync_status_route_requires_operator_token(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """The GET sync/meili route must require the operator token header."""

    meme_file_id = uuid.uuid7()
    response = await client.get(f"/api/v1/pipeline/items/{meme_file_id}/sync/meili")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_operator_token"


async def test_pipeline_meili_sync_replay_route_happy_path(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """POST sync/meili/replay must reserve a new dispatch for an item past classify success."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-replay-happy",
        post_id="9602",
        phash_tag="3",
        input_hash_seed="3",
    )
    service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=service)

    try:
        response = await client.post(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/meili/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert body["stage"] == "sync_meili"
    assert body["meme_file_id"] == str(meme_file_id)


async def test_pipeline_meili_sync_replay_route_rejects_not_ready_items(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """POST sync/meili/replay must 409 when classify has not yet succeeded."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _seed_detail_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-replay-not-ready",
        post_id="9603",
        phash_tag="4",
    )
    service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=service)

    try:
        response = await client.post(
            f"/api/v1/pipeline/items/{meme_file_id}/sync/meili/replay",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["code"] == "pipeline_replay_not_allowed"


async def test_pipeline_meili_sync_replay_batch_route_happy_path(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """POST sync/meili/replay-batch must accept a bounded batch of ready items."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    first = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-batch-one",
        post_id="9604",
        phash_tag="5",
        input_hash_seed="5",
    )
    second = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-batch-two",
        post_id="9605",
        phash_tag="6",
        input_hash_seed="6",
    )
    service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=service)

    try:
        response = await client.post(
            "/api/v1/pipeline/sync/meili/replay-batch",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"meme_file_ids": [str(first), str(second)]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert len(body) == 2
    assert {item["meme_file_id"] for item in body} == {str(first), str(second)}
    for item in body:
        assert item["stage"] == "sync_meili"


async def test_pipeline_meili_sync_replay_batch_route_rejects_oversize_batches(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """The Meilisearch batch endpoint must reject requests > SYNC_REPLAY_BATCH_MAX."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _drive_item_to_classify_succeeded(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="meili-batch-oversize",
        post_id="9606",
        phash_tag="7",
        input_hash_seed="7",
    )
    service = PipelineReplayService(migrated_db_session, broker=broker)
    _override_focused_pipeline_services(app, replay_service=service)
    oversized_batch = [str(meme_file_id)] * (SYNC_REPLAY_BATCH_MAX + 1)

    try:
        response = await client.post(
            "/api/v1/pipeline/sync/meili/replay-batch",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            json={"meme_file_ids": oversized_batch},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["code"] == "pipeline_replay_not_allowed"


async def test_pipeline_meili_sync_replay_batch_route_rejects_empty_body(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """FastAPI must surface Pydantic validation failures as 422 for empty batches."""

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    response = await client.post(
        "/api/v1/pipeline/sync/meili/replay-batch",
        headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        json={"meme_file_ids": []},
    )
    assert response.status_code == 422


async def test_pipeline_item_detail_preserves_pre_s03_byte_compatibility(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """S01/S02 byte-compat: an item that never reached classify has empty sync_targets.

    Every S01/S02 field must still deserialize identically from the
    response payload for a pre-classify item — this proves the T03
    ``sync_targets`` mapping addition is purely additive.
    """

    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id = await _seed_detail_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="detail-bytecompat",
        post_id="9607",
        phash_tag="8",
    )
    _override_focused_pipeline_services(
        app,
        item_read_service=PipelineItemReadService(migrated_db_session),
    )

    try:
        response = await client.get(
            f"/api/v1/pipeline/items/{meme_file_id}/detail",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    # sync_targets must be an empty mapping for pre-classify items.
    assert body["sync_targets"] == {}
    # Roundtrip through the pydantic model to prove the full S01/S02 field
    # set still deserializes cleanly after T03's additive changes.
    from memexpert.schemas.content_pipeline import ContentPipelineItemDetail

    detail = ContentPipelineItemDetail.model_validate(body)
    assert detail.sync_targets == {}
    assert detail.meme_file_id == meme_file_id
    # Critical S01 fields stay present.
    assert detail.current_stage is not None
    assert detail.current_status is not None
    assert detail.attempt_count >= 0
