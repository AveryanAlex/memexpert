"""Tests for idempotent moving-media preview-image repair."""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from botocore.exceptions import ClientError

from memexpert.core.config import Settings
from memexpert.workers.video_poster_backfill import (
    VideoPosterBackfiller,
    VideoPosterBackfillStatus,
    VideoPosterCandidate,
)

if TYPE_CHECKING:
    from memexpert.media.contracts import NormalizedMediaResult, UploadMediaDetails


class FakeStorageClient:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_calls: list[dict[str, object]] = []

    def head_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[Key][0])}

    def get_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        body, content_type = self.objects[Key]
        return {"Body": io.BytesIO(body), "ContentType": content_type}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object:
        assert ContentLength == len(Body)
        self.objects[Key] = (Body, ContentType)
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


@dataclass(slots=True)
class FakeMediaProcessor:
    preview_bytes: bytes = b"generated-preview-png"
    extract_calls: list[dict[str, object]] = field(default_factory=list)

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("poster backfill must not inspect uploads")

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult:
        _ = (meme_file_id, filename, content_type, media_bytes)
        raise AssertionError("poster backfill must not re-transcode videos")

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        self.extract_calls.append(
            {
                "filename": filename,
                "content_type": content_type,
                "media_bytes": media_bytes,
            }
        )
        return self.preview_bytes


@pytest.mark.asyncio
async def test_backfiller_generates_missing_preview_once_and_then_skips_it() -> None:
    meme_file_id = uuid.UUID("11111111-1111-7111-8111-111111111113")
    web_video_key = f"pipeline/derived/{meme_file_id}/web.mp4"
    preview_image_key = f"pipeline/derived/{meme_file_id}/preview.png"
    storage_client = FakeStorageClient()
    storage_client.objects[web_video_key] = (b"normalized-web-video", "video/mp4")
    media_processor = FakeMediaProcessor()
    backfiller = VideoPosterBackfiller(
        storage_client=storage_client,
        media_processor=media_processor,
        settings=Settings(),
    )
    candidate = VideoPosterCandidate(meme_file_id=meme_file_id, web_video_object_key=web_video_key)

    first = await backfiller.ensure_preview_image(candidate)
    second = await backfiller.ensure_preview_image(candidate)

    assert first is VideoPosterBackfillStatus.CREATED
    assert second is VideoPosterBackfillStatus.PRESENT
    assert storage_client.objects[preview_image_key] == (b"generated-preview-png", "image/png")
    assert len(storage_client.put_calls) == 1
    assert media_processor.extract_calls == [
        {
            "filename": "web.mp4",
            "content_type": "video/mp4",
            "media_bytes": b"normalized-web-video",
        }
    ]


@pytest.mark.asyncio
async def test_backfiller_force_regenerates_existing_preview() -> None:
    meme_file_id = uuid.UUID("11111111-1111-7111-8111-111111111114")
    web_video_key = f"pipeline/derived/{meme_file_id}/web.mp4"
    preview_image_key = f"pipeline/derived/{meme_file_id}/preview.png"
    storage_client = FakeStorageClient()
    storage_client.objects[web_video_key] = (b"normalized-web-video", "video/mp4")
    storage_client.objects[preview_image_key] = (b"stale-preview", "image/png")
    backfiller = VideoPosterBackfiller(
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(preview_bytes=b"fresh-preview"),
        settings=Settings(),
    )
    candidate = VideoPosterCandidate(meme_file_id=meme_file_id, web_video_object_key=web_video_key)

    result = await backfiller.ensure_preview_image(candidate, overwrite=True)

    assert result is VideoPosterBackfillStatus.CREATED
    assert storage_client.objects[preview_image_key] == (b"fresh-preview", "image/png")
