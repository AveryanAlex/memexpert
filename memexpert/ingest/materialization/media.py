"""Media inspection preparation and validation for materialization."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from memexpert.core.storage import build_original_object_key
from memexpert.ingest.materialization.models import PreparedMaterialization
from memexpert.media.contracts import MediaProcessingError, MediaValidationError
from memexpert.models.enums import ContentKind
from memexpert.pipeline import constants as _consts
from memexpert.services.errors import PipelineIngestError, PipelinePayloadTooLargeError

if TYPE_CHECKING:
    from memexpert.core.config import Settings
    from memexpert.media.contracts import PipelineMediaProcessorProtocol, UploadMediaDetails
    from memexpert.models.content import PipelineIngestRequest


class MaterializationMediaPreparer:
    """Inspect raw upload bytes and prepare canonical materialization metadata."""

    def __init__(self, *, settings: Settings, media_processor: PipelineMediaProcessorProtocol) -> None:
        self._settings = settings
        self._media_processor = media_processor

    async def prepare(
        self,
        *,
        ingest_request: PipelineIngestRequest,
        media_bytes: bytes,
    ) -> PreparedMaterialization:
        """Inspect media and build the canonical original key for a new meme file."""

        filename = ingest_request.declared_filename or "upload.bin"
        content_type = ingest_request.declared_content_type or "application/octet-stream"
        sha256_hex = ingest_request.sha256_hex
        if sha256_hex is None:
            raise PipelineIngestError("Raw ingest request is missing its SHA256 digest.")

        try:
            inspected_media = await self._media_processor.inspect_upload(
                filename=filename,
                content_type=content_type,
                media_bytes=media_bytes,
            )
        except MediaProcessingError:
            raise
        except Exception as exc:
            raise MediaValidationError(str(exc)) from exc

        self._validate_inspected_media(inspected_media, actual_size=len(media_bytes))
        meme_file_id = uuid.uuid7()
        return PreparedMaterialization(
            filename=filename,
            media_type=inspected_media.media_type,
            mime_type=inspected_media.mime_type,
            file_size_bytes=inspected_media.file_size_bytes,
            width=inspected_media.width,
            height=inspected_media.height,
            perceptual_hash=inspected_media.perceptual_hash,
            sha256_hex=sha256_hex,
            object_key=build_original_object_key(
                meme_file_id,
                filename,
                settings=self._settings,
            ),
        )

    def _validate_inspected_media(self, inspected_media: UploadMediaDetails, *, actual_size: int) -> None:
        upload_limit = self._upload_limit_for_media_type(inspected_media.media_type)
        if actual_size > upload_limit:
            raise PipelinePayloadTooLargeError(f"Uploaded file exceeds the {upload_limit}-byte limit.")
        if len(inspected_media.perceptual_hash) > _consts.MAX_PERCEPTUAL_HASH_LENGTH:
            raise PipelineIngestError(
                "Configured perceptual-hash size exceeds the persisted meme_files.perceptual_hash contract."
            )

    def _upload_limit_for_media_type(self, media_type: ContentKind) -> int:
        if media_type is ContentKind.IMAGE:
            return self._settings.pipeline_image_upload_max_bytes
        if media_type is ContentKind.GIF:
            return self._settings.pipeline_gif_upload_max_bytes
        if media_type is ContentKind.VIDEO:
            return self._settings.pipeline_video_upload_max_bytes
        return self._settings.pipeline_image_upload_max_bytes


__all__ = ["MaterializationMediaPreparer"]
