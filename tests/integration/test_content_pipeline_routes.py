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
from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus
from memexpert.schemas.content_pipeline import (
    ContentPipelineItemRead,
    ContentPipelineStageJournalRead,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
)
from memexpert.services import PipelineItemNotFoundError, PipelineUnsupportedMediaTypeError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient


@dataclass(slots=True)
class StubPipelineService:
    """Simple async service double for route-contract and OpenAPI tests."""

    upload_result: ContentPipelineUploadRead | None = None
    item_result: ContentPipelineItemRead | None = None
    upload_error: Exception | None = None
    item_error: Exception | None = None
    upload_calls: list[dict[str, object]] = field(default_factory=list)
    item_calls: list[uuid.UUID] = field(default_factory=list)

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


async def test_pipeline_routes_expose_item_detail_and_openapi_registration(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    item = build_item()
    stub_service = StubPipelineService(
        upload_result=ContentPipelineUploadRead.model_validate(item.model_dump(mode="python")),
        item_result=item,
    )
    operator_token = get_settings().pipeline_operator_token.get_secret_value()
    app.dependency_overrides[get_content_pipeline_service] = lambda: stub_service

    try:
        detail_response = await client.get(
            f"/api/v1/pipeline/items/{item.meme_file_id}",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
        )
        openapi_response = await client.get("/openapi.json")

        paths = openapi_response.json()["paths"]
        upload_parameters = paths["/api/v1/pipeline/uploads"]["post"]["parameters"]
        detail_parameters = paths["/api/v1/pipeline/items/{meme_file_id}"]["get"]["parameters"]

        assert detail_response.status_code == 200
        assert detail_response.json()["meme_id"] == str(item.meme_id)
        assert stub_service.item_calls == [item.meme_file_id]

        assert "/api/v1/pipeline/uploads" in paths
        assert "/api/v1/pipeline/items/{meme_file_id}" in paths
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in upload_parameters
        )
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME and parameter["in"] == "header"
            for parameter in detail_parameters
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
    finally:
        app.dependency_overrides.clear()
