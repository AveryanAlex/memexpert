"""Idempotent moving-media preview-image repair helpers for worker runtimes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

from botocore.exceptions import ClientError

from memexpert.core.config import Settings, get_settings
from memexpert.core.storage import (
    StorageConnectionError,
    derive_preview_image_object_key,
    download_object_bytes,
    get_pipeline_storage_settings,
    parse_media_generation_object_key,
    upload_object_bytes,
)
from memexpert.media.contracts import MediaValidationError, PipelineMediaProcessorProtocol

if TYPE_CHECKING:
    import uuid

_PREVIEW_IMAGE_MIME_TYPE = "image/png"
_WEB_VIDEO_MIME_TYPE = "video/mp4"
_MISSING_OBJECT_ERROR_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class VideoPosterStorageClient(Protocol):
    """S3-compatible operations required by the poster backfill."""

    def head_object(self, *, Bucket: str, Key: str) -> object: ...

    def get_object(self, *, Bucket: str, Key: str) -> object: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class VideoPosterCandidate:
    """One file whose durable web video must have a companion preview image."""

    meme_file_id: uuid.UUID
    web_video_object_key: str


class VideoPosterBackfillStatus(StrEnum):
    """Outcome of ensuring one moving-media preview artifact."""

    CREATED = "created"
    PRESENT = "present"


class VideoPosterBackfiller:
    """Generate deterministic PNG preview frames for existing web videos."""

    def __init__(
        self,
        *,
        storage_client: VideoPosterStorageClient,
        media_processor: PipelineMediaProcessorProtocol,
        settings: Settings | None = None,
    ) -> None:
        self._storage_client = storage_client
        self._media_processor = media_processor
        self._settings = settings or get_settings()
        self._storage_settings = get_pipeline_storage_settings(self._settings)

    def preview_image_object_key(self, candidate: VideoPosterCandidate) -> str:
        """Return the deterministic companion key for one legacy web video."""

        self._reject_immutable_generation(candidate)

        return derive_preview_image_object_key(
            candidate.web_video_object_key,
            meme_file_id=candidate.meme_file_id,
            settings=self._settings,
        )

    async def preview_image_exists(self, candidate: VideoPosterCandidate) -> bool:
        """Check whether one candidate already has its durable preview frame."""

        object_key = self.preview_image_object_key(candidate)
        try:
            await asyncio.to_thread(
                self._storage_client.head_object,
                Bucket=self._storage_settings.bucket,
                Key=object_key,
            )
        except ClientError as exc:
            error = exc.response.get("Error", {})
            response_metadata = exc.response.get("ResponseMetadata", {})
            error_code = str(error.get("Code", ""))
            http_status = response_metadata.get("HTTPStatusCode")
            if error_code in _MISSING_OBJECT_ERROR_CODES or http_status == 404:
                return False
            raise StorageConnectionError(f"Failed to inspect S3 object {object_key}: {exc}") from exc
        except Exception as exc:
            raise StorageConnectionError(f"Failed to inspect S3 object {object_key}: {exc}") from exc
        return True

    async def ensure_preview_image(
        self,
        candidate: VideoPosterCandidate,
        *,
        overwrite: bool = False,
    ) -> VideoPosterBackfillStatus:
        """Create a missing preview image, or report that the artifact exists."""

        self._reject_immutable_generation(candidate)
        if not overwrite and await self.preview_image_exists(candidate):
            return VideoPosterBackfillStatus.PRESENT

        web_video_bytes = await download_object_bytes(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=candidate.web_video_object_key,
        )
        preview_image_bytes = await self._media_processor.extract_preview_frame(
            filename=PurePosixPath(candidate.web_video_object_key).name,
            content_type=_WEB_VIDEO_MIME_TYPE,
            media_bytes=web_video_bytes,
        )
        if not preview_image_bytes:
            raise MediaValidationError(
                f"Preview extraction produced no bytes for moving-media file {candidate.meme_file_id}."
            )

        await upload_object_bytes(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=self.preview_image_object_key(candidate),
            body=preview_image_bytes,
            content_type=_PREVIEW_IMAGE_MIME_TYPE,
        )
        return VideoPosterBackfillStatus.CREATED

    def _reject_immutable_generation(self, candidate: VideoPosterCandidate) -> None:
        generation_key = parse_media_generation_object_key(
            candidate.web_video_object_key,
            settings=self._settings,
        )
        if generation_key is not None:
            raise MediaValidationError(
                "Immutable generation posters cannot be repaired in place; "
                "use Replay & Repair derivative regeneration to activate a fresh pair."
            )


__all__ = [
    "VideoPosterBackfiller",
    "VideoPosterBackfillStatus",
    "VideoPosterCandidate",
    "VideoPosterStorageClient",
]
