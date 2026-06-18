"""Worker-only raw ingest-request materializer.

This module is intentionally not re-exported from ``memexpert.ingest`` so API
startup keeps importing only the API-safe accept/read services. Worker runtime
code injects the heavy media processor that performs Pillow/ImageHash/FFmpeg
inspection.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.config import Settings, get_settings
from memexpert.core.perceptual_hashes import (
    DEFAULT_PERCEPTUAL_HASH_ALGORITHM,
    hamming_distance_hex,
    perceptual_hash_bit_size,
)
from memexpert.core.storage import (
    PipelineStorageSettings,
    build_original_object_key,
    delete_object_if_present,
    download_object_bytes,
    get_pipeline_storage_settings,
    get_s3_client,
    upload_object_bytes,
)
from memexpert.ingest.source_metadata import (
    source_forward_ids,
    source_is_forwarded,
    source_published_at,
    source_reactions,
)
from memexpert.media.contracts import (
    MediaProcessingError,
    MediaValidationError,
    PipelineMediaProcessorProtocol,
    UploadMediaDetails,
)
from memexpert.models.base import utcnow
from memexpert.models.content import (
    BlockedPerceptualHash,
    Meme,
    MemeFile,
    MemeSource,
    ModerationDecision,
    PipelineIngestRequest,
    PipelineOutboxEvent,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    ModerationAction,
    PipelineIngestRequestStatus,
    SourceAttachReason,
)
from memexpert.pipeline.outbox import build_meme_created_transcode_outbox_event
from memexpert.services import content_pipeline_constants as _consts
from memexpert.services.content_pipeline_helpers import trim_error_text
from memexpert.services.errors import PipelineIngestError, PipelinePayloadTooLargeError, PipelineStorageError

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


FAILED_INVALID_MEDIA_CODE = "invalid_media"
FAILED_MEDIA_TOO_LARGE_CODE = "payload_too_large"
FAILED_BLOCKED_PHASH_CODE = "blocked_perceptual_hash"
_ELIGIBLE_STATUSES = frozenset({PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING})


class ObjectStorageClient(Protocol):
    """Minimal S3-compatible surface used by the materializer."""

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

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


@dataclass(frozen=True, slots=True)
class PreparedMaterialization:
    """Inspected upload metadata plus the canonical original object key."""

    filename: str
    media_type: ContentKind
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    perceptual_hash: str
    sha256_hex: str
    object_key: str


@dataclass(frozen=True, slots=True)
class BlockedPerceptualHashMatch:
    """Active blocked pHash row plus its computed distance to incoming media."""

    blocked_hash: BlockedPerceptualHash
    hamming_distance: int


@dataclass(frozen=True, slots=True)
class PipelineIngestMaterializationResult:
    """Outcome summary returned by one materialization attempt."""

    ingest_request_id: uuid.UUID
    status: PipelineIngestRequestStatus
    materialized_meme_id: uuid.UUID | None = None
    materialized_meme_file_id: uuid.UUID | None = None
    matched_meme_file_id: uuid.UUID | None = None
    outbox_event_id: uuid.UUID | None = None


class PipelineIngestMaterializer:
    """Materialize raw ingest requests into durable content-pipeline rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        media_processor: PipelineMediaProcessorProtocol,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage_settings: PipelineStorageSettings = get_pipeline_storage_settings(self._settings)
        self._storage_client = storage_client or cast("ObjectStorageClient", get_s3_client())
        self._media_processor = media_processor

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        media_processor: PipelineMediaProcessorProtocol,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
    ) -> Self:
        """Build the worker-only materializer from shared settings."""

        return cls(
            session,
            media_processor=media_processor,
            settings=settings,
            storage_client=storage_client,
        )

    async def materialize(self, ingest_request_id: uuid.UUID) -> PipelineIngestMaterializationResult:
        """Inspect raw temp bytes and persist post-materialization state atomically."""

        ingest_request = await self._load_locked_request(ingest_request_id)
        if ingest_request.status not in _ELIGIBLE_STATUSES:
            return self._result(ingest_request)

        now = utcnow()
        ingest_request.status = PipelineIngestRequestStatus.MEDIA_INSPECTING
        ingest_request.locked_at = now
        ingest_request.attempt_count += 1
        ingest_request.failure_code = None
        ingest_request.failure_detail = None

        temp_object_key = ingest_request.temp_original_object_key
        if temp_object_key is None:
            await self._mark_invalid_media(
                ingest_request,
                code=FAILED_INVALID_MEDIA_CODE,
                detail="Raw ingest request is missing its temporary original object key.",
            )
            return self._result(ingest_request)

        original_bytes = await self._download_temp_object(temp_object_key)
        try:
            prepared = await self._prepare_materialization(
                ingest_request=ingest_request,
                media_bytes=original_bytes,
            )
        except MediaProcessingError as exc:
            await self._mark_invalid_media(
                ingest_request,
                code=FAILED_INVALID_MEDIA_CODE,
                detail=str(exc),
            )
            return self._result(ingest_request)
        except PipelinePayloadTooLargeError as exc:
            await self._mark_invalid_media(
                ingest_request,
                code=FAILED_MEDIA_TOO_LARGE_CODE,
                detail=str(exc),
            )
            return self._result(ingest_request)

        blocked_match = await self._find_blocked_perceptual_hash_match(prepared.perceptual_hash)
        await self._put_canonical_original(prepared=prepared, media_bytes=original_bytes)
        try:
            if blocked_match is not None:
                await self._materialize_blocked_request(
                    ingest_request=ingest_request,
                    prepared=prepared,
                    blocked_match=blocked_match,
                    created_at=now,
                )
                outbox_event: PipelineOutboxEvent | None = None
            else:
                outbox_event = await self._materialize_normal_request(
                    ingest_request=ingest_request,
                    prepared=prepared,
                    created_at=now,
                )
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            await self._cleanup_canonical_original(prepared.object_key)
            raise PipelineIngestError("Failed to persist materialized ingest request state.") from exc
        except Exception:
            await self._session.rollback()
            await self._cleanup_canonical_original(prepared.object_key)
            raise

        await self._cleanup_temp_object(temp_object_key)
        return self._result(ingest_request, outbox_event=outbox_event)

    async def _load_locked_request(self, ingest_request_id: uuid.UUID) -> PipelineIngestRequest:
        result = await self._session.execute(
            select(PipelineIngestRequest)
            .where(PipelineIngestRequest.id == ingest_request_id)
            .with_for_update()
        )
        ingest_request = result.scalar_one_or_none()
        if ingest_request is None:
            raise PipelineIngestError(f"Pipeline ingest request {ingest_request_id} does not exist.")
        return ingest_request

    async def _download_temp_object(self, key: str) -> bytes:
        try:
            return await download_object_bytes(
                self._storage_client,
                bucket=self._storage_settings.bucket,
                key=key,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to download the raw original from temporary storage.") from exc

    async def _prepare_materialization(
        self,
        *,
        ingest_request: PipelineIngestRequest,
        media_bytes: bytes,
    ) -> PreparedMaterialization:
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

    async def _put_canonical_original(
        self,
        *,
        prepared: PreparedMaterialization,
        media_bytes: bytes,
    ) -> None:
        try:
            await upload_object_bytes(
                self._storage_client,
                bucket=self._storage_settings.bucket,
                key=prepared.object_key,
                body=media_bytes,
                content_type=prepared.mime_type,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to promote the uploaded original into canonical storage.") from exc

    async def _mark_invalid_media(
        self,
        ingest_request: PipelineIngestRequest,
        *,
        code: str,
        detail: str,
    ) -> None:
        ingest_request.status = PipelineIngestRequestStatus.FAILED_INVALID_MEDIA
        ingest_request.failure_code = code
        ingest_request.failure_detail = trim_error_text(detail or "Media inspection failed.")
        ingest_request.locked_at = None
        ingest_request.materialized_meme_id = None
        ingest_request.materialized_meme_file_id = None
        ingest_request.matched_meme_file_id = None
        ingest_request.source_attach_reason = None
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist invalid media ingest-request state.") from exc

    async def _materialize_blocked_request(
        self,
        *,
        ingest_request: PipelineIngestRequest,
        prepared: PreparedMaterialization,
        blocked_match: BlockedPerceptualHashMatch,
        created_at: datetime,
    ) -> None:
        meme_id = uuid.uuid7()
        meme_file_id = self._meme_file_id_from_key(prepared.object_key)
        await self._create_blocked_rows(
            meme_id=meme_id,
            meme_file_id=meme_file_id,
            ingest_request=ingest_request,
            prepared=prepared,
            blocked_match=blocked_match,
            created_at=created_at,
        )
        ingest_request.status = PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH
        ingest_request.failure_code = FAILED_BLOCKED_PHASH_CODE
        ingest_request.failure_detail = self._blocked_hash_error_text(blocked_match)
        ingest_request.locked_at = None
        ingest_request.materialized_meme_id = meme_id
        ingest_request.materialized_meme_file_id = meme_file_id
        ingest_request.matched_meme_file_id = None
        ingest_request.source_attach_reason = SourceAttachReason.BLOCKED_PERCEPTUAL_HASH_NEW_FILE
        await self._session.flush()

    async def _materialize_normal_request(
        self,
        *,
        ingest_request: PipelineIngestRequest,
        prepared: PreparedMaterialization,
        created_at: datetime,
    ) -> PipelineOutboxEvent:
        phash_match = await self._find_exact_phash_match(prepared.perceptual_hash)
        meme_file_id = self._meme_file_id_from_key(prepared.object_key)
        event_id = uuid.uuid7()
        if phash_match is None:
            meme_id = uuid.uuid7()
            await self._create_new_rows(
                meme_id=meme_id,
                meme_file_id=meme_file_id,
                ingest_request=ingest_request,
                prepared=prepared,
                publish_event_id=event_id,
                created_at=created_at,
            )
            source_attach_reason = SourceAttachReason.NEW_FILE
            matched_meme_file_id: uuid.UUID | None = None
        else:
            meme_id = phash_match.meme_id
            await self._create_phash_match_rows(
                phash_match=phash_match,
                meme_file_id=meme_file_id,
                ingest_request=ingest_request,
                prepared=prepared,
                publish_event_id=event_id,
                created_at=created_at,
            )
            source_attach_reason = SourceAttachReason.PHASH_EXACT_NEW_FILE
            matched_meme_file_id = phash_match.id

        meme_file = await self._get_meme_file(meme_file_id)
        outbox_event = build_meme_created_transcode_outbox_event(
            meme_file,
            event_id=event_id,
            created_at=created_at,
            settings=self._settings,
        )
        self._session.add(outbox_event)
        ingest_request.status = PipelineIngestRequestStatus.MATERIALIZED
        ingest_request.failure_code = None
        ingest_request.failure_detail = None
        ingest_request.locked_at = None
        ingest_request.materialized_meme_id = meme_id
        ingest_request.materialized_meme_file_id = meme_file_id
        ingest_request.matched_meme_file_id = matched_meme_file_id
        ingest_request.source_attach_reason = source_attach_reason
        await self._session.flush()
        return outbox_event

    async def _create_blocked_rows(
        self,
        *,
        meme_id: uuid.UUID,
        meme_file_id: uuid.UUID,
        ingest_request: PipelineIngestRequest,
        prepared: PreparedMaterialization,
        blocked_match: BlockedPerceptualHashMatch,
        created_at: datetime,
    ) -> None:
        blocked_hash = blocked_match.blocked_hash
        event_id = uuid.uuid7()
        forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
        meme = Meme(
            id=meme_id,
            media_type=prepared.media_type,
            primary_file_id=meme_file_id,
            language=ContentLanguage.NONE,
            is_public=False,
            author_user_id=ingest_request.owner_user_id,
        )
        self._session.add(meme)
        await self._session.flush()

        self._session.add(
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.FAILED,
                width=prepared.width,
                height=prepared.height,
                file_size_bytes=prepared.file_size_bytes,
                mime_type=prepared.mime_type,
                s3_original_key=prepared.object_key,
                perceptual_hash=prepared.perceptual_hash,
                sha256_hex=prepared.sha256_hex,
                ingest_origin=IngestFileOrigin.BLOCKED_PERCEPTUAL_HASH,
                blocked_perceptual_hash_id=blocked_hash.id,
            )
        )
        self._session.add_all(
            [
                MemeSource(
                    file_id=meme_file_id,
                    platform=ingest_request.source_platform,
                    source_id=ingest_request.source_id,
                    post_id=ingest_request.post_id,
                    views=self._source_views(ingest_request),
                    reactions=source_reactions(ingest_request.source_metadata),
                    is_first_source=not source_is_forwarded(ingest_request.source_metadata),
                    source_alive=True,
                    published_at=source_published_at(ingest_request.source_metadata),
                    forwarded_from_source_id=forwarded_from_source_id,
                    forwarded_from_post_id=forwarded_from_post_id,
                    attach_reason=SourceAttachReason.BLOCKED_PERCEPTUAL_HASH_NEW_FILE,
                ),
                PipelineStageJournal(
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.FAILED,
                    attempt_count=1,
                    last_event_id=event_id,
                    normalized_reason=_consts.PIPELINE_REASON_BLOCKED_PERCEPTUAL_HASH,
                    last_error_text=self._blocked_hash_error_text(blocked_match),
                    is_retryable=False,
                    started_at=created_at,
                    finished_at=created_at,
                ),
                ModerationDecision(
                    meme=meme,
                    admin_user_id=None,
                    action=ModerationAction.HIDE,
                    reason=blocked_hash.reason,
                    note=self._blocked_hash_error_text(blocked_match),
                    previous_is_public=False,
                    previous_is_nsfw=False,
                    new_is_public=False,
                    new_is_nsfw=False,
                    previous_template_id=None,
                    new_template_id=None,
                ),
            ]
        )
        await self._session.flush()

    async def _create_new_rows(
        self,
        *,
        meme_id: uuid.UUID,
        meme_file_id: uuid.UUID,
        ingest_request: PipelineIngestRequest,
        prepared: PreparedMaterialization,
        publish_event_id: uuid.UUID,
        created_at: datetime,
    ) -> None:
        forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
        meme = Meme(
            id=meme_id,
            media_type=prepared.media_type,
            primary_file_id=meme_file_id,
            language=ContentLanguage.NONE,
            is_public=False,
            author_user_id=ingest_request.owner_user_id,
        )
        self._session.add(meme)
        await self._session.flush()

        self._session.add(
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.PENDING,
                width=prepared.width,
                height=prepared.height,
                file_size_bytes=prepared.file_size_bytes,
                mime_type=prepared.mime_type,
                s3_original_key=prepared.object_key,
                perceptual_hash=prepared.perceptual_hash,
                sha256_hex=prepared.sha256_hex,
                ingest_origin=IngestFileOrigin.NEW_MEME,
            )
        )
        self._session.add_all(
            [
                MemeSource(
                    file_id=meme_file_id,
                    platform=ingest_request.source_platform,
                    source_id=ingest_request.source_id,
                    post_id=ingest_request.post_id,
                    views=self._source_views(ingest_request),
                    reactions=source_reactions(ingest_request.source_metadata),
                    is_first_source=not source_is_forwarded(ingest_request.source_metadata),
                    source_alive=True,
                    published_at=source_published_at(ingest_request.source_metadata),
                    forwarded_from_source_id=forwarded_from_source_id,
                    forwarded_from_post_id=forwarded_from_post_id,
                    attach_reason=SourceAttachReason.NEW_FILE,
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

    async def _create_phash_match_rows(
        self,
        *,
        phash_match: MemeFile,
        meme_file_id: uuid.UUID,
        ingest_request: PipelineIngestRequest,
        prepared: PreparedMaterialization,
        publish_event_id: uuid.UUID,
        created_at: datetime,
    ) -> None:
        forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
        self._session.add(
            MemeFile(
                id=meme_file_id,
                meme_id=phash_match.meme_id,
                status=ContentProcessingStatus.PENDING,
                width=prepared.width,
                height=prepared.height,
                file_size_bytes=prepared.file_size_bytes,
                mime_type=prepared.mime_type,
                s3_original_key=prepared.object_key,
                perceptual_hash=prepared.perceptual_hash,
                sha256_hex=prepared.sha256_hex,
                ingest_origin=IngestFileOrigin.PHASH_EXACT_EXISTING_MEME,
                matched_meme_file_id=phash_match.id,
            )
        )
        self._session.add_all(
            [
                MemeSource(
                    file_id=meme_file_id,
                    platform=ingest_request.source_platform,
                    source_id=ingest_request.source_id,
                    post_id=ingest_request.post_id,
                    views=self._source_views(ingest_request),
                    reactions=source_reactions(ingest_request.source_metadata),
                    is_first_source=False,
                    source_alive=True,
                    published_at=source_published_at(ingest_request.source_metadata),
                    forwarded_from_source_id=forwarded_from_source_id,
                    forwarded_from_post_id=forwarded_from_post_id,
                    attach_reason=SourceAttachReason.PHASH_EXACT_NEW_FILE,
                    matched_meme_file_id=phash_match.id,
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

    async def _find_blocked_perceptual_hash_match(
        self,
        perceptual_hash: str,
    ) -> BlockedPerceptualHashMatch | None:
        try:
            hash_size = perceptual_hash_bit_size(perceptual_hash)
        except ValueError:
            return None
        rows = (
            await self._session.execute(
                select(BlockedPerceptualHash)
                .where(
                    BlockedPerceptualHash.is_active.is_(True),
                    BlockedPerceptualHash.hash_algorithm == DEFAULT_PERCEPTUAL_HASH_ALGORITHM,
                    BlockedPerceptualHash.hash_size == hash_size,
                )
                .order_by(BlockedPerceptualHash.created_at.asc(), BlockedPerceptualHash.id.asc())
            )
        ).scalars().all()

        best_match: BlockedPerceptualHashMatch | None = None
        for blocked_hash in rows:
            distance = hamming_distance_hex(perceptual_hash, blocked_hash.perceptual_hash)
            if distance is None or distance > blocked_hash.max_hamming_distance:
                continue
            candidate = BlockedPerceptualHashMatch(blocked_hash=blocked_hash, hamming_distance=distance)
            if best_match is None or candidate.hamming_distance < best_match.hamming_distance:
                best_match = candidate
        return best_match

    async def _find_exact_phash_match(self, perceptual_hash: str) -> MemeFile | None:
        result = await self._session.execute(
            select(MemeFile)
            .where(
                MemeFile.perceptual_hash == perceptual_hash,
                MemeFile.blocked_perceptual_hash_id.is_(None),
            )
            .order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_meme_file(self, meme_file_id: uuid.UUID) -> MemeFile:
        result = await self._session.execute(select(MemeFile).where(MemeFile.id == meme_file_id))
        meme_file = result.scalar_one_or_none()
        if meme_file is None:
            raise PipelineIngestError(f"Materialized meme file {meme_file_id} disappeared before outbox creation.")
        return meme_file

    async def _cleanup_temp_object(self, key: str) -> None:
        await delete_object_if_present(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=key,
        )

    async def _cleanup_canonical_original(self, key: str) -> None:
        await delete_object_if_present(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=key,
        )

    def _upload_limit_for_media_type(self, media_type: ContentKind) -> int:
        if media_type is ContentKind.IMAGE:
            return self._settings.pipeline_image_upload_max_bytes
        if media_type is ContentKind.GIF:
            return self._settings.pipeline_gif_upload_max_bytes
        if media_type is ContentKind.VIDEO:
            return self._settings.pipeline_video_upload_max_bytes
        return self._settings.pipeline_image_upload_max_bytes

    @staticmethod
    def _blocked_hash_error_text(blocked_match: BlockedPerceptualHashMatch) -> str:
        blocked_hash = blocked_match.blocked_hash
        note_suffix = f" Note: {blocked_hash.note}" if blocked_hash.note else ""
        return trim_error_text(
            "Upload matched blocked perceptual hash "
            f"{blocked_hash.id} ({blocked_hash.hash_algorithm}, distance "
            f"{blocked_match.hamming_distance}/{blocked_hash.max_hamming_distance}, reason "
            f"{blocked_hash.reason.value}).{note_suffix}"
        )

    @staticmethod
    def _source_views(ingest_request: PipelineIngestRequest) -> int:
        raw_views = ingest_request.source_metadata.get("views")
        if isinstance(raw_views, int) and raw_views >= 0:
            return raw_views
        return 0

    @staticmethod
    def _meme_file_id_from_key(object_key: str) -> uuid.UUID:
        try:
            return uuid.UUID(object_key.split("/")[-2])
        except (IndexError, ValueError) as exc:
            raise PipelineIngestError(f"Canonical object key {object_key!r} does not contain a meme_file_id.") from exc

    @staticmethod
    def _result(
        ingest_request: PipelineIngestRequest,
        *,
        outbox_event: PipelineOutboxEvent | None = None,
    ) -> PipelineIngestMaterializationResult:
        return PipelineIngestMaterializationResult(
            ingest_request_id=ingest_request.id,
            status=ingest_request.status,
            materialized_meme_id=ingest_request.materialized_meme_id,
            materialized_meme_file_id=ingest_request.materialized_meme_file_id,
            matched_meme_file_id=ingest_request.matched_meme_file_id,
            outbox_event_id=outbox_event.id if outbox_event is not None else None,
        )


__all__ = [
    "FAILED_BLOCKED_PHASH_CODE",
    "FAILED_INVALID_MEDIA_CODE",
    "FAILED_MEDIA_TOO_LARGE_CODE",
    "ObjectStorageClient",
    "PipelineIngestMaterializationResult",
    "PipelineIngestMaterializer",
]
