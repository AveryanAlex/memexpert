"""Integration tests for the operator-facing content-pipeline HTTP routes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image

from memexpert.api.dependencies.pipeline import (
    PIPELINE_OPERATOR_TOKEN_HEADER_NAME,
    get_content_pipeline_service,
)
from memexpert.core.config import get_settings
from memexpert.models.base import utcnow
from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus, SourcePlatform
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineStageJournalRead,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
)
from memexpert.services import (
    ContentPipelineService,
    PipelineItemNotFoundError,
    PipelineReplayNotAllowedError,
    PipelineUnsupportedMediaTypeError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class StubPipelineService:
    """Simple async service double for route-contract and OpenAPI tests."""

    upload_result: ContentPipelineUploadRead | None = None
    item_result: ContentPipelineItemRead | None = None
    list_result: tuple[ContentPipelineItemRead, ...] = ()
    replay_result: ContentPipelineReplayAccepted | None = None
    upload_error: Exception | None = None
    item_error: Exception | None = None
    list_error: Exception | None = None
    replay_error: Exception | None = None
    upload_calls: list[dict[str, object]] = field(default_factory=list)
    item_calls: list[uuid.UUID] = field(default_factory=list)
    list_calls: list[dict[str, object]] = field(default_factory=list)
    replay_calls: list[dict[str, object]] = field(default_factory=list)

    async def create_upload(
        self,
        *,
        metadata: ContentPipelineUploadMetadata,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> ContentPipelineUploadRead:
        self.upload_calls.append(
            {
                "metadata": metadata,
                "filename": filename,
                "content_type": content_type,
                "media_bytes": media_bytes,
            }
        )
        if self.upload_error is not None:
            raise self.upload_error
        assert self.upload_result is not None
        return self.upload_result

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
class RecordingPublisher:
    """Async publisher double used to observe route-driven replay and upload dispatches."""

    events: list[ContentPipelineDispatchEvent] = field(default_factory=list)

    async def __call__(self, event: ContentPipelineDispatchEvent) -> None:
        self.events.append(event)


def build_png_bytes() -> bytes:
    """Generate a tiny multipart upload body in memory for route tests."""

    image = Image.new("RGB", (4, 4), color=(255, 128, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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


async def test_pipeline_routes_require_operator_token_and_accept_real_multipart_uploads(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    item = build_item()
    stub_service = StubPipelineService(
        upload_result=ContentPipelineUploadRead.model_validate(item.model_dump(mode="python"))
    )
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_content_pipeline_service] = lambda: stub_service

    try:
        rejected_response = await client.post(
            "/api/v1/pipeline/uploads",
            data={
                "source_platform": "telegram",
                "source_id": "channel-one",
                "post_id": "101",
                "views": "7",
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
                "views": "7",
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
                "views": "7",
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

        assert created_response.status_code == 201
        assert created_response.json()["meme_file_id"] == str(item.meme_file_id)
        assert len(stub_service.upload_calls) == 1
        upload_call = stub_service.upload_calls[0]
        metadata = upload_call["metadata"]
        assert isinstance(metadata, ContentPipelineUploadMetadata)
        assert metadata.source_platform.value == "telegram"
        assert metadata.source_id == "channel-one"
        assert metadata.post_id == "101"
        assert metadata.views == 7
        assert upload_call["filename"] == "sample.png"
        assert upload_call["content_type"] == "image/png"
        assert upload_call["media_bytes"] == build_png_bytes()
    finally:
        app.dependency_overrides.clear()


async def test_pipeline_routes_expose_list_detail_replay_and_openapi_registration(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    item = build_item()
    replay_result = ContentPipelineReplayAccepted(
        meme_file_id=item.meme_file_id,
        replay_event_id=uuid.uuid7(),
        stage=ContentPipelineStage.TRANSCODE,
        attempt=2,
    )
    stub_service = StubPipelineService(
        upload_result=ContentPipelineUploadRead.model_validate(item.model_dump(mode="python")),
        item_result=item,
        list_result=(item,),
        replay_result=replay_result,
    )
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_content_pipeline_service] = lambda: stub_service

    try:
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
        list_parameters = paths["/api/v1/pipeline/items"]["get"]["parameters"]
        detail_parameters = paths["/api/v1/pipeline/items/{meme_file_id}"]["get"]["parameters"]
        replay_parameters = paths["/api/v1/pipeline/items/{meme_file_id}/replay"]["post"]["parameters"]

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
    stub_service = StubPipelineService(
        upload_result=ContentPipelineUploadRead.model_validate(build_item().model_dump(mode="python"))
    )
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_content_pipeline_service] = lambda: stub_service

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

        assert missing_file_response.status_code == 422
        assert blank_provenance_response.status_code == 400
        assert blank_provenance_response.json() == {
            "code": "pipeline_payload_invalid",
            "detail": "Value error, source provenance fields must not be blank.",
        }
        assert stub_service.upload_calls == []
    finally:
        app.dependency_overrides.clear()


async def test_pipeline_routes_map_service_errors_to_typed_http_payloads(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    item_id = uuid.uuid7()
    stub_service = StubPipelineService(
        upload_error=PipelineUnsupportedMediaTypeError("Uploaded media type is not supported."),
        item_error=PipelineItemNotFoundError(f"Pipeline item {item_id} does not exist."),
        replay_error=PipelineReplayNotAllowedError("No failed retryable stage exists for this pipeline item."),
    )
    app.dependency_overrides[get_content_pipeline_service] = lambda: stub_service

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


async def test_pipeline_routes_list_failed_items_and_reject_replay_guards_with_real_service(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    app.dependency_overrides[get_content_pipeline_service] = lambda: service

    try:
        item = await service.create_upload(
            metadata=ContentPipelineUploadMetadata(
                source_platform=SourcePlatform.TELEGRAM,
                source_id="memexpert-channel",
                post_id="9001",
                views=99,
            ),
            filename="route-sample.png",
            content_type="image/png",
            media_bytes=build_png_bytes(),
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

        dispatch_event = publisher.events[0]
        assert hasattr(dispatch_event, "event_id")
        assert hasattr(dispatch_event, "stage")
        await service.mark_stage_failed(
            meme_file_id=item.meme_file_id,
            stage=ContentPipelineStage.TRANSCODE,
            attempt=1,
            event_id=dispatch_event.event_id,
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
                "current_stage": "transcode",
                "current_status": "failed",
                "original_object_key": item.original_object_key,
                "web_video_object_key": None,
                "last_event_id": str(dispatch_event.event_id),
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
