# ruff: noqa: TC003
"""Operator-facing ingest service for durable manual uploads and inspectable journal state."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, Self, cast

import imagehash
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from memexpert.core.broker import get_pipeline_broker, get_pipeline_broker_settings
from memexpert.core.config import Settings, get_settings
from memexpert.core.storage import build_original_object_key, get_pipeline_storage_settings, get_s3_client
from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeFile, MemeSource, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineItemRead,
    ContentPipelineStageJournalRead,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
)
from memexpert.services.errors import (
    PipelineIngestError,
    PipelineItemNotFoundError,
    PipelinePayloadTooLargeError,
    PipelinePayloadValidationError,
    PipelinePublishError,
    PipelineSourceConflictError,
    PipelineStorageError,
    PipelineUnsupportedMediaTypeError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_IMAGE_FORMAT_TO_MIME_TYPE: dict[str, str] = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_MIME_TYPE_TO_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/gif": frozenset({"gif"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/png": frozenset({"png"}),
    "image/webp": frozenset({"webp"}),
}
_STAGE_ORDER: dict[ContentPipelineStage, int] = {
    ContentPipelineStage.INGEST: 0,
    ContentPipelineStage.TRANSCODE: 1,
    ContentPipelineStage.SYNC_QDRANT: 2,
    ContentPipelineStage.SYNC_MEILI: 3,
}
_ACTIVE_STAGE_STATUSES = {
    ContentPipelineStageStatus.PENDING,
    ContentPipelineStageStatus.PROCESSING,
    ContentPipelineStageStatus.FAILED,
    ContentPipelineStageStatus.DUPLICATE,
}
_MAX_PERCEPTUAL_HASH_LENGTH = 64


class ObjectStorageClient(Protocol):
    """Minimal sync S3-compatible client surface used by the ingest service."""

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


DispatchEventPublisher = Callable[[ContentPipelineDispatchEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    """Normalized upload bytes plus the derived media metadata written durably."""

    filename: str
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    perceptual_hash: str
    object_key: str


class ContentPipelineService:
    """Persist manual uploads before publish and expose inspectable pipeline truth."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        publisher: DispatchEventPublisher | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage_settings = get_pipeline_storage_settings(self._settings)
        self._broker_settings = get_pipeline_broker_settings(self._settings)
        self._storage_client = storage_client or cast("ObjectStorageClient", get_s3_client())
        self._publisher = publisher or self._publish_dispatch_event

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        publisher: DispatchEventPublisher | None = None,
    ) -> Self:
        """Build the ingest service from shared runtime settings and lazy runtimes."""

        return cls(
            session,
            settings=settings,
            storage_client=storage_client,
            publisher=publisher,
        )

    async def create_upload(
        self,
        *,
        metadata: ContentPipelineUploadMetadata,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> ContentPipelineUploadRead:
        """Persist an operator upload durably, then publish downstream work exactly once."""

        meme_file_id = uuid.uuid7()
        prepared_upload = self._prepare_upload(
            meme_file_id=meme_file_id,
            filename=filename,
            content_type=content_type,
            media_bytes=media_bytes,
        )

        await self._put_original_object(prepared_upload=prepared_upload, media_bytes=media_bytes)

        try:
            item, dispatch_event = await self._persist_upload(
                meme_file_id=meme_file_id,
                metadata=metadata,
                prepared_upload=prepared_upload,
            )
        except Exception:
            await self._cleanup_uploaded_object(prepared_upload.object_key)
            raise

        if dispatch_event is None:
            return ContentPipelineUploadRead.model_validate(item.model_dump(mode="python"))

        try:
            await self._publisher(dispatch_event)
        except Exception as exc:
            await self._mark_dispatch_failure(
                meme_file_id=meme_file_id,
                dispatch_event=dispatch_event,
                error=exc,
            )
            raise PipelinePublishError("Upload was stored, but downstream dispatch failed.") from exc

        return ContentPipelineUploadRead.model_validate(item.model_dump(mode="python"))

    async def get_item(self, meme_file_id: uuid.UUID) -> ContentPipelineItemRead:
        """Return one pipeline item and its stage journal from durable PostgreSQL state."""

        result = await self._session.execute(
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .where(MemeFile.id == meme_file_id)
        )
        meme_file = result.scalar_one_or_none()
        if meme_file is None:
            raise PipelineItemNotFoundError(f"Pipeline item {meme_file_id} does not exist.")

        return self._build_item_read(meme_file)

    def _prepare_upload(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> PreparedUpload:
        normalized_filename = self._normalize_filename(filename)
        normalized_content_type = self._normalize_content_type(content_type)
        file_size_bytes = len(media_bytes)
        if file_size_bytes <= 0:
            raise PipelinePayloadValidationError("Uploaded file is empty.")
        if file_size_bytes > self._settings.pipeline_upload_max_bytes:
            raise PipelinePayloadTooLargeError(
                f"Uploaded file exceeds the {self._settings.pipeline_upload_max_bytes}-byte limit.",
            )

        try:
            with Image.open(BytesIO(media_bytes)) as image:
                detected_format = image.format
                image.load()
                width = image.width
                height = image.height
        except UnidentifiedImageError as exc:
            raise PipelineUnsupportedMediaTypeError("Uploaded file is not a readable image payload.") from exc
        except Image.DecompressionBombError as exc:
            raise PipelinePayloadValidationError("Uploaded image exceeds the configured pixel budget.") from exc
        except OSError as exc:
            raise PipelineUnsupportedMediaTypeError("Uploaded file is not a readable image payload.") from exc

        pixel_count = width * height
        if pixel_count <= 0:
            raise PipelinePayloadValidationError("Uploaded image dimensions are invalid.")
        if pixel_count > self._settings.pipeline_image_max_pixels:
            raise PipelinePayloadValidationError("Uploaded image exceeds the configured pixel budget.")

        if detected_format is None:
            raise PipelineUnsupportedMediaTypeError("Uploaded file format could not be detected.")

        detected_mime_type = _IMAGE_FORMAT_TO_MIME_TYPE.get(detected_format.upper())
        if detected_mime_type is None:
            raise PipelineUnsupportedMediaTypeError("Uploaded media type is not supported.")
        if detected_mime_type not in self._settings.pipeline_allowed_mime_types:
            raise PipelineUnsupportedMediaTypeError("Uploaded media type is not enabled for ingest.")
        if normalized_content_type != detected_mime_type:
            raise PipelineUnsupportedMediaTypeError(
                "Uploaded content type "
                f"{normalized_content_type!r} does not match detected media type {detected_mime_type!r}."
            )

        extension = self._normalize_extension(normalized_filename)
        allowed_extensions = _MIME_TYPE_TO_EXTENSIONS[detected_mime_type]
        if extension not in allowed_extensions:
            raise PipelineUnsupportedMediaTypeError(
                f"Filename extension .{extension} does not match uploaded media type {detected_mime_type!r}.",
            )

        with Image.open(BytesIO(media_bytes)) as hash_image:
            perceptual_hash = str(imagehash.phash(hash_image, hash_size=self._settings.pipeline_phash_size))
        if len(perceptual_hash) > _MAX_PERCEPTUAL_HASH_LENGTH:
            raise PipelineIngestError(
                "Configured perceptual-hash size exceeds the persisted meme_files.perceptual_hash contract.",
            )

        return PreparedUpload(
            filename=normalized_filename,
            mime_type=detected_mime_type,
            file_size_bytes=file_size_bytes,
            width=width,
            height=height,
            perceptual_hash=perceptual_hash,
            object_key=build_original_object_key(
                meme_file_id,
                normalized_filename,
                settings=self._settings,
            ),
        )

    async def _persist_upload(
        self,
        *,
        meme_file_id: uuid.UUID,
        metadata: ContentPipelineUploadMetadata,
        prepared_upload: PreparedUpload,
    ) -> tuple[ContentPipelineItemRead, ContentPipelineDispatchEvent | None]:
        now = utcnow()

        try:
            await self._ensure_source_identifier_is_available(metadata)
            duplicate_match = await self._find_duplicate_match(prepared_upload.perceptual_hash)

            if duplicate_match is None:
                meme_id = uuid.uuid7()
                publish_event_id = uuid.uuid7()
                dispatch_event = ContentPipelineDispatchEvent(
                    event_id=publish_event_id,
                    event_type=ContentPipelineEventType.MEME_CREATED,
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.TRANSCODE,
                    source_kind=ContentSourceKind.MANUAL_UPLOAD,
                    original_object_key=prepared_upload.object_key,
                    attempt=1,
                    created_at=now,
                )
                await self._create_new_upload_rows(
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    metadata=metadata,
                    prepared_upload=prepared_upload,
                    publish_event_id=publish_event_id,
                    created_at=now,
                )
            else:
                dispatch_event = None
                await self._create_duplicate_upload_rows(
                    duplicate_match=duplicate_match,
                    meme_file_id=meme_file_id,
                    metadata=metadata,
                    prepared_upload=prepared_upload,
                    created_at=now,
                )

            await self._session.commit()
        except (PipelinePayloadValidationError, PipelineSourceConflictError):
            await self._session.rollback()
            raise
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist the upload and journal state.") from exc

        item = await self.get_item(meme_file_id)
        return item, dispatch_event

    async def _create_new_upload_rows(
        self,
        *,
        meme_id: uuid.UUID,
        meme_file_id: uuid.UUID,
        metadata: ContentPipelineUploadMetadata,
        prepared_upload: PreparedUpload,
        publish_event_id: uuid.UUID,
        created_at: datetime,
    ) -> None:
        meme = Meme(
            id=meme_id,
            media_type=ContentKind.IMAGE,
            language=ContentLanguage.NONE,
            is_public=False,
        )
        self._session.add(meme)
        await self._session.flush()

        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.PENDING,
            width=prepared_upload.width,
            height=prepared_upload.height,
            file_size_bytes=prepared_upload.file_size_bytes,
            mime_type=prepared_upload.mime_type,
            s3_original_key=prepared_upload.object_key,
            perceptual_hash=prepared_upload.perceptual_hash,
            is_primary=True,
        )
        self._session.add(meme_file)
        await self._session.flush()

        meme.primary_file_id = meme_file_id
        self._session.add_all(
            [
                MemeSource(
                    file_id=meme_file_id,
                    platform=metadata.source_platform,
                    source_id=metadata.source_id,
                    post_id=metadata.post_id,
                    views=metadata.views,
                    reactions={},
                    is_first_source=True,
                    source_alive=True,
                ),
                PipelineStageJournal(
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=publish_event_id,
                    is_retryable=False,
                    started_at=created_at,
                    finished_at=created_at,
                ),
                PipelineStageJournal(
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.TRANSCODE,
                    status=ContentPipelineStageStatus.PENDING,
                    attempt_count=0,
                    last_event_id=publish_event_id,
                    is_retryable=True,
                ),
            ]
        )
        await self._session.flush()

    async def _create_duplicate_upload_rows(
        self,
        *,
        duplicate_match: MemeFile,
        meme_file_id: uuid.UUID,
        metadata: ContentPipelineUploadMetadata,
        prepared_upload: PreparedUpload,
        created_at: datetime,
    ) -> None:
        duplicate_event_id = uuid.uuid7()
        self._session.add_all(
            [
                MemeFile(
                    id=meme_file_id,
                    meme_id=duplicate_match.meme_id,
                    status=ContentProcessingStatus.FAILED,
                    width=prepared_upload.width,
                    height=prepared_upload.height,
                    file_size_bytes=prepared_upload.file_size_bytes,
                    mime_type=prepared_upload.mime_type,
                    s3_original_key=prepared_upload.object_key,
                    perceptual_hash=prepared_upload.perceptual_hash,
                    is_primary=False,
                ),
                MemeSource(
                    file_id=meme_file_id,
                    platform=metadata.source_platform,
                    source_id=metadata.source_id,
                    post_id=metadata.post_id,
                    views=metadata.views,
                    reactions={},
                    is_first_source=False,
                    source_alive=True,
                ),
                PipelineStageJournal(
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.DUPLICATE,
                    attempt_count=1,
                    last_event_id=duplicate_event_id,
                    normalized_reason="duplicate_perceptual_hash",
                    last_error_text=(
                        f"Exact duplicate matched existing meme_file_id {duplicate_match.id}."
                    ),
                    is_retryable=False,
                    started_at=created_at,
                    finished_at=created_at,
                ),
            ]
        )
        await self._session.flush()

    async def _find_duplicate_match(self, perceptual_hash: str) -> MemeFile | None:
        result = await self._session.execute(
            select(MemeFile)
            .where(MemeFile.perceptual_hash == perceptual_hash)
            .order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _ensure_source_identifier_is_available(self, metadata: ContentPipelineUploadMetadata) -> None:
        result = await self._session.execute(
            select(MemeSource)
            .where(
                MemeSource.platform == metadata.source_platform,
                MemeSource.source_id == metadata.source_id,
                MemeSource.post_id == metadata.post_id,
            )
            .limit(1)
        )
        existing_source = result.scalar_one_or_none()
        if existing_source is not None:
            raise PipelineSourceConflictError(
                "Source platform/source_id/post_id is already attached to an uploaded item.",
            )

    async def _put_original_object(
        self,
        *,
        prepared_upload: PreparedUpload,
        media_bytes: bytes,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._storage_client.put_object,
                Bucket=self._storage_settings.bucket,
                Key=prepared_upload.object_key,
                Body=media_bytes,
                ContentType=prepared_upload.mime_type,
                ContentLength=prepared_upload.file_size_bytes,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to store the uploaded original in S3-compatible storage.") from exc

    async def _cleanup_uploaded_object(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._storage_client.delete_object,
                Bucket=self._storage_settings.bucket,
                Key=object_key,
            )
        except Exception:
            return

    async def _publish_dispatch_event(self, event: ContentPipelineDispatchEvent) -> None:
        broker = get_pipeline_broker()
        payload = event.model_dump(mode="json")
        _ = await broker.publish(
            payload,
            exchange=self._broker_settings.exchange,
            routing_key=self._broker_settings.meme_created_routing_key,
            persist=True,
            content_type="application/json",
            message_id=str(event.event_id),
            timestamp=event.created_at,
            mandatory=True,
        )

    async def _mark_dispatch_failure(
        self,
        *,
        meme_file_id: uuid.UUID,
        dispatch_event: ContentPipelineDispatchEvent,
        error: Exception,
    ) -> None:
        result = await self._session.execute(
            select(PipelineStageJournal).where(
                PipelineStageJournal.meme_file_id == meme_file_id,
                PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
            )
        )
        transcode_entry = result.scalar_one_or_none()
        if transcode_entry is None:
            return

        failed_at = utcnow()
        transcode_entry.status = ContentPipelineStageStatus.FAILED
        transcode_entry.attempt_count = max(transcode_entry.attempt_count, dispatch_event.attempt)
        transcode_entry.last_event_id = dispatch_event.event_id
        transcode_entry.normalized_reason = "publish_failed"
        transcode_entry.last_error_text = str(error)[:4000]
        transcode_entry.is_retryable = True
        transcode_entry.retry_after = failed_at + timedelta(seconds=self._broker_settings.retry_backoff_seconds)
        transcode_entry.started_at = failed_at
        transcode_entry.finished_at = failed_at

        try:
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()

    def _build_item_read(self, meme_file: MemeFile) -> ContentPipelineItemRead:
        stage_entries = tuple(
            sorted(
                meme_file.pipeline_stage_journal_entries,
                key=lambda entry: _STAGE_ORDER[entry.stage],
            )
        )
        if not stage_entries:
            raise PipelineIngestError(f"Pipeline item {meme_file.id} is missing journal state.")

        current_entry = self._resolve_current_stage(stage_entries)
        return ContentPipelineItemRead(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            current_stage=current_entry.stage,
            current_status=current_entry.status,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
            last_event_id=current_entry.last_event_id,
            normalized_reason=current_entry.normalized_reason,
            last_error_text=current_entry.last_error_text,
            attempt_count=current_entry.attempt_count,
            stages=tuple(ContentPipelineStageJournalRead.model_validate(entry) for entry in stage_entries),
        )

    @staticmethod
    def _resolve_current_stage(stage_entries: tuple[PipelineStageJournal, ...]) -> PipelineStageJournal:
        for stage_entry in stage_entries:
            if stage_entry.status in _ACTIVE_STAGE_STATUSES:
                return stage_entry
        return stage_entries[-1]

    @staticmethod
    def _normalize_filename(filename: str | None) -> str:
        if filename is None:
            raise PipelinePayloadValidationError("Uploaded file must include a filename.")

        normalized_filename = PurePosixPath(filename).name.strip()
        if not normalized_filename:
            raise PipelinePayloadValidationError("Uploaded file must include a filename.")
        return normalized_filename

    @staticmethod
    def _normalize_content_type(content_type: str | None) -> str:
        if content_type is None:
            raise PipelineUnsupportedMediaTypeError("Uploaded file must include a media type.")

        normalized_content_type = content_type.strip().lower()
        if not normalized_content_type:
            raise PipelineUnsupportedMediaTypeError("Uploaded file must include a media type.")
        return normalized_content_type

    @staticmethod
    def _normalize_extension(filename: str) -> str:
        suffix = PurePosixPath(filename).suffix.lstrip(".").lower()
        if not suffix:
            raise PipelineUnsupportedMediaTypeError("Uploaded filename must include an extension.")
        return suffix


__all__ = ["ContentPipelineService", "ContentPipelineUploadRead", "PreparedUpload"]
