# ruff: noqa: TC003
"""Operator-facing ingest service for durable manual uploads and inspectable journal state."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, Self, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from memexpert.core.broker import ensure_pipeline_broker_started, get_pipeline_broker_settings
from memexpert.core.config import Settings, get_settings
from memexpert.core.media import NormalizedMediaResult, PipelineMediaProcessor, PipelineMediaProcessorProtocol
from memexpert.core.ocr import OCRExtractionResult, OCRProcessorProtocol, PipelineOCRProcessor
from memexpert.core.storage import build_original_object_key, get_pipeline_storage_settings, get_s3_client
from memexpert.core.voyage import VoyageMalformedResponseError
from memexpert.models.base import utcnow
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeSource,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    EmbeddingInputType,
)
from memexpert.schemas.content_pipeline import (
    MAX_PIPELINE_ERROR_LENGTH,
    MAX_PIPELINE_REASON_LENGTH,
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineItemDetail,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineStageJournalRead,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
)
from memexpert.services.content_merge import ContentMergeService, MergeOutcome
from memexpert.services.content_pipeline_reporting import build_item_detail
from memexpert.services.errors import (
    PipelineIngestError,
    PipelineItemNotFoundError,
    PipelineMergeTransactionError,
    PipelinePayloadTooLargeError,
    PipelinePayloadValidationError,
    PipelinePublishError,
    PipelineReplayNotAllowedError,
    PipelineSourceConflictError,
    PipelineStorageError,
    PipelineUnsupportedMediaTypeError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.classification import ClassificationResult
    from memexpert.core.qdrant import QdrantSimilarityMatch
    from memexpert.core.voyage import VoyageEmbeddingResult


_STAGE_ORDER: dict[ContentPipelineStage, int] = {
    ContentPipelineStage.INGEST: 0,
    ContentPipelineStage.TRANSCODE: 1,
    ContentPipelineStage.OCR: 2,
    ContentPipelineStage.EMBED: 3,
    ContentPipelineStage.CLASSIFY: 4,
    ContentPipelineStage.SYNC_QDRANT: 5,
    ContentPipelineStage.SYNC_MEILI: 6,
}
_ACTIVE_STAGE_STATUSES = {
    ContentPipelineStageStatus.PENDING,
    ContentPipelineStageStatus.PROCESSING,
    ContentPipelineStageStatus.FAILED,
    ContentPipelineStageStatus.DUPLICATE,
}
_MAX_PERCEPTUAL_HASH_LENGTH = 64
_DEFAULT_PIPELINE_ITEMS_LIMIT = 50
_MAX_PIPELINE_ITEMS_LIMIT = 200
_DEFAULT_STUCK_AFTER_SECONDS = 60
_PIPELINE_REASON_PUBLISH_FAILED = "publish_failed"
_PIPELINE_REASON_REPLAY_REQUESTED = "replay_requested"


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
    media_type: ContentKind
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    perceptual_hash: str
    object_key: str


@dataclass(frozen=True, slots=True)
class PipelineStageWorkContext:
    """Compact worker context returned after a stage is marked processing."""

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    stage: ContentPipelineStage
    mime_type: str | None
    original_object_key: str
    web_video_object_key: str | None


@dataclass(frozen=True, slots=True)
class StageJournalSnapshot:
    """Durable stage-journal state captured before replay reserves a new attempt."""

    attempt_count: int
    finished_at: datetime | None
    is_retryable: bool
    last_error_text: str | None
    last_event_id: uuid.UUID | None
    normalized_reason: str | None
    retry_after: datetime | None
    started_at: datetime | None
    status: ContentPipelineStageStatus


@dataclass(frozen=True, slots=True)
class DownstreamStageDispatch:
    """A durable next-stage dispatch created after one stage succeeds."""

    event: ContentPipelineDispatchEvent
    stage_entry: PipelineStageJournal


_DOWNSTREAM_STAGE_EVENT_TYPES: dict[ContentPipelineStage, ContentPipelineEventType] = {
    ContentPipelineStage.TRANSCODE: ContentPipelineEventType.MEME_TRANSCODED,
    ContentPipelineStage.OCR: ContentPipelineEventType.MEME_OCR_DONE,
    ContentPipelineStage.EMBED: ContentPipelineEventType.MEME_EMBEDDED,
    ContentPipelineStage.CLASSIFY: ContentPipelineEventType.MEME_READY,
}
_NEXT_STAGE_BY_STAGE: dict[ContentPipelineStage, ContentPipelineStage] = {
    ContentPipelineStage.TRANSCODE: ContentPipelineStage.OCR,
    ContentPipelineStage.OCR: ContentPipelineStage.EMBED,
    ContentPipelineStage.EMBED: ContentPipelineStage.CLASSIFY,
    ContentPipelineStage.CLASSIFY: ContentPipelineStage.SYNC_QDRANT,
}


class ContentPipelineService:
    """Persist manual uploads before publish and expose inspectable pipeline truth."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        publisher: DispatchEventPublisher | None = None,
        media_processor: PipelineMediaProcessorProtocol | None = None,
        ocr_processor: OCRProcessorProtocol | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage_settings = get_pipeline_storage_settings(self._settings)
        self._broker_settings = get_pipeline_broker_settings(self._settings)
        self._storage_client = storage_client or cast("ObjectStorageClient", get_s3_client())
        self._publisher = publisher or self._publish_dispatch_event
        self._media_processor = media_processor or PipelineMediaProcessor(settings=self._settings)
        self._ocr_processor = ocr_processor or PipelineOCRProcessor(
            settings=self._settings,
            media_processor=self._media_processor,
        )

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        publisher: DispatchEventPublisher | None = None,
        media_processor: PipelineMediaProcessorProtocol | None = None,
        ocr_processor: OCRProcessorProtocol | None = None,
    ) -> Self:
        """Build the ingest service from shared runtime settings and lazy runtimes."""

        return cls(
            session,
            settings=settings,
            storage_client=storage_client,
            publisher=publisher,
            media_processor=media_processor,
            ocr_processor=ocr_processor,
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
        prepared_upload = await self._prepare_upload(
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

        meme_file = await self._get_meme_file(meme_file_id)
        return self._build_item_read(meme_file)

    async def get_item_detail(self, meme_file_id: uuid.UUID) -> ContentPipelineItemDetail:
        """Return the enriched S02 operator projection with OCR/merge/classify truth.

        The S01 ``get_item`` contract stays untouched; this method builds a
        strict superset that surfaces durable OCR provenance, merge lineage,
        classification, canonical-primary context, and the emitted ``meme_ready``
        event id when the heavy chain has reached classify completion. Optional
        projections are absent (``None`` or empty) whenever the underlying audit
        state has not been produced yet — operators must never see a defaulted
        ``is_nsfw=False`` for an item that has not really been classified.
        """

        meme_file = await self._get_meme_file(meme_file_id)
        base = self._build_item_read(meme_file)
        return await build_item_detail(self._session, meme_file=meme_file, base=base)

    async def list_items(
        self,
        *,
        filter_by: ContentPipelineItemFilter = ContentPipelineItemFilter.FAILED,
        limit: int = _DEFAULT_PIPELINE_ITEMS_LIMIT,
        stuck_after_seconds: int = _DEFAULT_STUCK_AFTER_SECONDS,
    ) -> tuple[ContentPipelineItemRead, ...]:
        """Return operator-visible pipeline items filtered by the current durable state."""

        resolved_limit = max(1, min(limit, _MAX_PIPELINE_ITEMS_LIMIT))
        resolved_stuck_after_seconds = max(stuck_after_seconds, 1)
        stale_before = utcnow() - timedelta(seconds=resolved_stuck_after_seconds)

        result = await self._session.execute(
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .order_by(MemeFile.created_at.desc())
        )

        items: list[ContentPipelineItemRead] = []
        for meme_file in result.scalars().all():
            stage_entries = self._sorted_stage_entries(meme_file)
            if not stage_entries:
                continue

            current_entry = self._resolve_current_stage(stage_entries)
            if not self._matches_list_filter(
                current_entry,
                filter_by=filter_by,
                stale_before=stale_before,
            ):
                continue

            items.append(self._build_item_read(meme_file, stage_entries=stage_entries, current_entry=current_entry))
            if len(items) >= resolved_limit:
                break

        return tuple(items)

    async def replay_item(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage | None = None,
    ) -> ContentPipelineReplayAccepted:
        """Reserve and republish the last retryable failed stage without rewriting ingest state."""

        meme_file = await self._get_meme_file(meme_file_id)
        if not meme_file.s3_original_key:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} is missing durable original storage identifiers.",
            )

        stage_entries = self._sorted_stage_entries(meme_file)
        target_entry = self._select_replay_entry(stage_entries, requested_stage=stage)

        if self._is_replay_reserved(target_entry):
            if target_entry.last_event_id is None:
                raise PipelineReplayNotAllowedError(
                    f"Pipeline item {meme_file_id} is already reserved for replay, but its event id is missing.",
                )
            return ContentPipelineReplayAccepted(
                meme_file_id=meme_file.id,
                replay_event_id=target_entry.last_event_id,
                stage=target_entry.stage,
                attempt=max(target_entry.attempt_count, 1),
            )

        replay_attempt = max(target_entry.attempt_count + 1, 1)
        replay_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=target_entry.stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=replay_attempt,
            created_at=utcnow(),
        )
        snapshot = self._snapshot_stage(target_entry)

        self._reserve_replay(target_entry, replay_event)
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist replay reservation state.") from exc

        try:
            await self._publisher(replay_event)
        except Exception as exc:
            await self._restore_stage_snapshot(target_entry.id, snapshot)
            raise PipelinePublishError("Replay was reserved, but downstream dispatch failed.") from exc

        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
        )

    async def start_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> PipelineStageWorkContext:
        """Mark one stage as running and return the durable media context workers need."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        started_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.PROCESSING
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = None
        stage_entry.last_error_text = None
        stage_entry.is_retryable = True
        stage_entry.retry_after = None
        stage_entry.started_at = started_at
        stage_entry.finished_at = None
        meme_file.status = ContentProcessingStatus.PROCESSING

        await self._commit_stage_mutation("Failed to persist running stage state.")
        return PipelineStageWorkContext(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            mime_type=meme_file.mime_type,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
        )

    async def mark_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> None:
        """Persist a worker transition from queued to actively running."""

        _ = await self.start_stage_processing(
            meme_file_id=meme_file_id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_transcode_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        result: NormalizedMediaResult,
    ) -> None:
        """Persist normalized derivative metadata, then advance the durable stage chain."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, ContentPipelineStage.TRANSCODE)
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        meme_file.s3_web_video_key = result.web_video_object_key
        meme_file.mime_type = result.mime_type
        meme_file.width = result.width
        meme_file.height = result.height
        meme_file.file_size_bytes = result.file_size_bytes
        meme_file.quality_score = result.quality_score
        meme_file.blur_hash = result.blur_hash
        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.TRANSCODE,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_ocr_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        result: OCRExtractionResult,
    ) -> None:
        """Persist durable OCR provenance, then advance the durable stage chain."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, ContentPipelineStage.OCR)
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        if meme_file.ocr_result is None:
            meme_file.ocr_result = MemeFileOCRResult(
                meme_file_id=meme_file.id,
                engine=result.engine,
                fallback_engine=result.fallback_engine,
                fallback_used=result.fallback_used,
                low_confidence=result.low_confidence,
                confidence=result.confidence,
                language=result.language,
                extracted_text=result.extracted_text,
                source_object_key=result.source_object_key,
                last_event_id=event_id,
            )
            self._session.add(meme_file.ocr_result)
        else:
            meme_file.ocr_result.engine = result.engine
            meme_file.ocr_result.fallback_engine = result.fallback_engine
            meme_file.ocr_result.fallback_used = result.fallback_used
            meme_file.ocr_result.low_confidence = result.low_confidence
            meme_file.ocr_result.confidence = result.confidence
            meme_file.ocr_result.language = result.language
            meme_file.ocr_result.extracted_text = result.extracted_text
            meme_file.ocr_result.source_object_key = result.source_object_key
            meme_file.ocr_result.last_event_id = event_id

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.OCR,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_embed_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        embedding_result: VoyageEmbeddingResult,
        similarity_matches: tuple[QdrantSimilarityMatch, ...],
    ) -> MergeOutcome:
        """Persist the durable embedding, run auto-merge, and advance the stage chain."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.EMBED,
        )
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        self._validate_embedding_contract(embedding_result)
        await self._persist_embedding_cache_row(
            meme_file=meme_file,
            embedding_result=embedding_result,
        )

        merge_service = ContentMergeService(
            self._session,
            similarity_threshold=self._settings.pipeline_merge_similarity_threshold,
        )
        try:
            merge_outcome = await merge_service.maybe_merge_after_embed(
                meme_file=meme_file,
                similarity_matches=similarity_matches,
            )
        except PipelineIngestError:
            await self._session.rollback()
            raise
        except Exception as exc:
            # A mid-merge exception (Qdrant outage during similarity lookup, row-lock
            # conflict, or any transient runtime failure) rolls back the single merge
            # transaction and stays replayable — distinct from contract-violation
            # failures above, which surface the base ``PipelineIngestError``.
            await self._session.rollback()
            raise PipelineMergeTransactionError(
                "Failed to apply the post-embed auto-merge transaction.",
            ) from exc

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.EMBED,
            attempt=attempt,
            event_id=event_id,
        )
        return merge_outcome

    async def complete_classify_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        classification_result: ClassificationResult,
    ) -> None:
        """Persist classification truth on the canonical meme and emit meme_ready."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.CLASSIFY,
        )
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        target_meme = await self._get_canonical_meme(meme_file.meme_id)
        target_meme.is_nsfw = classification_result.is_nsfw
        await self._apply_canonical_primary_truth(target_meme)

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.CLASSIFY,
            attempt=attempt,
            event_id=event_id,
        )

    async def mark_stage_failed(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
        retryable: bool,
    ) -> None:
        """Persist a failed worker attempt with an explicit retryability decision."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        failed_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.FAILED
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = self._trim_reason(normalized_reason)
        stage_entry.last_error_text = self._trim_error_text(last_error_text)
        stage_entry.is_retryable = retryable
        stage_entry.retry_after = (
            failed_at + timedelta(seconds=self._broker_settings.retry_backoff_seconds)
            if retryable
            else None
        )
        stage_entry.started_at = stage_entry.started_at or failed_at
        stage_entry.finished_at = failed_at
        meme_file.status = ContentProcessingStatus.FAILED

        await self._commit_stage_mutation("Failed to persist failed stage state.")

    async def mark_stage_succeeded(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> None:
        """Persist a successful worker attempt and enqueue the next durable stage when needed."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        self._ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )

    async def _finalize_stage_success(
        self,
        *,
        meme_file: MemeFile,
        stage_entry: PipelineStageJournal,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> None:
        finished_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.SUCCEEDED
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = None
        stage_entry.last_error_text = None
        stage_entry.is_retryable = False
        stage_entry.retry_after = None
        stage_entry.started_at = stage_entry.started_at or finished_at
        stage_entry.finished_at = finished_at

        downstream_dispatches = self._prepare_downstream_dispatches(
            meme_file=meme_file,
            stage=stage,
            created_at=finished_at,
        )
        # Classification is the terminal heavy-worker stage: once it succeeds the file
        # is truly downstream-ready even though we still publish MEME_READY so S03's
        # sync_qdrant/sync_meili consumers can pick up the canonical state.
        if stage is ContentPipelineStage.CLASSIFY:
            meme_file.status = ContentProcessingStatus.READY
        elif downstream_dispatches:
            meme_file.status = ContentProcessingStatus.PROCESSING
        else:
            meme_file.status = ContentProcessingStatus.PROCESSING

        await self._commit_stage_mutation("Failed to persist successful stage state.")

        for dispatch in downstream_dispatches:
            try:
                await self._publisher(dispatch.event)
            except Exception as exc:
                await self._mark_publish_failure_for_downstream_stage(dispatch=dispatch, error=exc)
                raise PipelinePublishError("Stage success was stored, but downstream dispatch failed.") from exc

    async def _prepare_upload(
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

        try:
            inspected_media = await self._media_processor.inspect_upload(
                filename=normalized_filename,
                content_type=normalized_content_type,
                media_bytes=media_bytes,
            )
        except Exception as exc:
            raise self._translate_media_processing_error(exc) from exc

        upload_limit = self._upload_limit_for_media_type(inspected_media.media_type)
        if file_size_bytes > upload_limit:
            raise PipelinePayloadTooLargeError(f"Uploaded file exceeds the {upload_limit}-byte limit.")
        if len(inspected_media.perceptual_hash) > _MAX_PERCEPTUAL_HASH_LENGTH:
            raise PipelineIngestError(
                "Configured perceptual-hash size exceeds the persisted meme_files.perceptual_hash contract.",
            )

        return PreparedUpload(
            filename=normalized_filename,
            media_type=inspected_media.media_type,
            mime_type=inspected_media.mime_type,
            file_size_bytes=inspected_media.file_size_bytes,
            width=inspected_media.width,
            height=inspected_media.height,
            perceptual_hash=inspected_media.perceptual_hash,
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
            media_type=prepared_upload.media_type,
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
        broker = await ensure_pipeline_broker_started(settings=self._settings)
        payload = event.model_dump(mode="json")
        _ = await broker.publish(
            payload,
            exchange=self._broker_settings.exchange,
            routing_key=self._broker_settings.routing_key_for_stage(event.stage),
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
        try:
            await self.mark_stage_failed(
                meme_file_id=meme_file_id,
                stage=dispatch_event.stage,
                attempt=dispatch_event.attempt,
                event_id=dispatch_event.event_id,
                normalized_reason=_PIPELINE_REASON_PUBLISH_FAILED,
                last_error_text=str(error),
                retryable=True,
            )
        except PipelineIngestError:
            return
        except PipelineItemNotFoundError:
            return

    async def _get_meme_file(self, meme_file_id: uuid.UUID) -> MemeFile:
        result = await self._session.execute(
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.ocr_result),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .where(MemeFile.id == meme_file_id)
        )
        meme_file = result.scalar_one_or_none()
        if meme_file is None:
            raise PipelineItemNotFoundError(f"Pipeline item {meme_file_id} does not exist.")
        return meme_file

    async def _get_canonical_meme(self, meme_id: uuid.UUID) -> Meme:
        result = await self._session.execute(select(Meme).where(Meme.id == meme_id))
        meme = result.scalar_one_or_none()
        if meme is None:
            raise PipelineIngestError(
                f"Canonical meme {meme_id} is missing when finalizing classification.",
            )
        return meme

    async def _apply_canonical_primary_truth(self, meme: Meme) -> None:
        primary_file_id = meme.primary_file_id
        if primary_file_id is None:
            meme.ocr_text = None
            meme.language = ContentLanguage.NONE
            return

        result = await self._session.execute(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == primary_file_id)
        )
        ocr_row = result.scalar_one_or_none()
        if ocr_row is None:
            meme.ocr_text = None
            meme.language = ContentLanguage.NONE
            return

        meme.ocr_text = ocr_row.extracted_text
        meme.language = ocr_row.language

    def _validate_embedding_contract(self, embedding_result: VoyageEmbeddingResult) -> None:
        expected_dimensions = self._settings.pipeline_voyage_output_dimensions
        if embedding_result.dimensions != expected_dimensions:
            raise PipelineIngestError(
                "Rejected embedding vector with unexpected dimensionality "
                f"(got {embedding_result.dimensions}, expected {expected_dimensions}).",
            )
        if len(embedding_result.vector) != expected_dimensions:
            raise PipelineIngestError(
                "Rejected embedding vector whose length does not match the declared dimensions.",
            )
        try:
            _ = embedding_result.embedding_bytes
        except VoyageMalformedResponseError as exc:
            raise PipelineIngestError(str(exc)) from exc

    async def _persist_embedding_cache_row(
        self,
        *,
        meme_file: MemeFile,
        embedding_result: VoyageEmbeddingResult,
    ) -> None:
        existing_result = await self._session.execute(
            select(EmbeddingCache).where(
                EmbeddingCache.input_hash == embedding_result.input_hash,
                EmbeddingCache.model_version == embedding_result.model,
                EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
            )
        )
        existing_cache_row = existing_result.scalar_one_or_none()
        if existing_cache_row is not None:
            existing_cache_row.embedding = embedding_result.embedding_bytes
            existing_cache_row.source_file_id = meme_file.id
            await self._session.flush()
            return

        self._session.add(
            EmbeddingCache(
                input_hash=embedding_result.input_hash,
                input_type=EmbeddingInputType.IMAGE,
                embedding=embedding_result.embedding_bytes,
                model_version=embedding_result.model,
                source_file_id=meme_file.id,
            )
        )
        await self._session.flush()

    async def _get_meme_file_and_stage_entry(
        self,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
    ) -> tuple[MemeFile, PipelineStageJournal]:
        meme_file = await self._get_meme_file(meme_file_id)
        stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is stage),
            None,
        )
        if stage_entry is None:
            raise PipelineIngestError(
                f"Pipeline item {meme_file_id} does not have durable journal state for stage {stage.value}."
            )
        return meme_file, stage_entry

    async def _commit_stage_mutation(self, failure_message: str) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(failure_message) from exc

    def _prepare_downstream_dispatches(
        self,
        *,
        meme_file: MemeFile,
        stage: ContentPipelineStage,
        created_at: datetime,
    ) -> tuple[DownstreamStageDispatch, ...]:
        next_stage = _NEXT_STAGE_BY_STAGE.get(stage)
        if next_stage is None:
            return ()

        existing_stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is next_stage),
            None,
        )
        if existing_stage_entry is not None:
            return ()

        dispatch_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=_DOWNSTREAM_STAGE_EVENT_TYPES[stage],
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=next_stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=1,
            created_at=created_at,
        )
        stage_entry = PipelineStageJournal(
            meme_file_id=meme_file.id,
            stage=next_stage,
            status=ContentPipelineStageStatus.PENDING,
            attempt_count=0,
            last_event_id=dispatch_event.event_id,
            is_retryable=True,
        )
        self._session.add(stage_entry)
        meme_file.pipeline_stage_journal_entries.append(stage_entry)
        return (DownstreamStageDispatch(event=dispatch_event, stage_entry=stage_entry),)

    async def _mark_publish_failure_for_downstream_stage(
        self,
        *,
        dispatch: DownstreamStageDispatch,
        error: Exception,
    ) -> None:
        try:
            await self.mark_stage_failed(
                meme_file_id=dispatch.stage_entry.meme_file_id,
                stage=dispatch.stage_entry.stage,
                attempt=dispatch.event.attempt,
                event_id=dispatch.event.event_id,
                normalized_reason=_PIPELINE_REASON_PUBLISH_FAILED,
                last_error_text=str(error),
                retryable=True,
            )
        except (PipelineIngestError, PipelineItemNotFoundError):
            return

    @staticmethod
    def _ensure_stage_attempt_is_current(stage_entry: PipelineStageJournal, *, attempt: int) -> None:
        if attempt < stage_entry.attempt_count:
            raise PipelineIngestError(
                "Received a stale stage transition for "
                f"{stage_entry.stage.value}: attempt {attempt} is behind durable attempt {stage_entry.attempt_count}."
            )

    async def _restore_stage_snapshot(self, stage_entry_id: uuid.UUID, snapshot: StageJournalSnapshot) -> None:
        try:
            result = await self._session.execute(
                select(PipelineStageJournal).where(PipelineStageJournal.id == stage_entry_id)
            )
            stage_entry = result.scalar_one_or_none()
            if stage_entry is None:
                raise PipelineIngestError(
                    f"Replay reservation for stage journal {stage_entry_id} disappeared before restore.",
                )

            stage_entry.status = snapshot.status
            stage_entry.attempt_count = snapshot.attempt_count
            stage_entry.last_event_id = snapshot.last_event_id
            stage_entry.normalized_reason = snapshot.normalized_reason
            stage_entry.last_error_text = snapshot.last_error_text
            stage_entry.is_retryable = snapshot.is_retryable
            stage_entry.retry_after = snapshot.retry_after
            stage_entry.started_at = snapshot.started_at
            stage_entry.finished_at = snapshot.finished_at
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise

    def _build_item_read(
        self,
        meme_file: MemeFile,
        *,
        stage_entries: tuple[PipelineStageJournal, ...] | None = None,
        current_entry: PipelineStageJournal | None = None,
    ) -> ContentPipelineItemRead:
        resolved_stage_entries = stage_entries or self._sorted_stage_entries(meme_file)
        if not resolved_stage_entries:
            raise PipelineIngestError(f"Pipeline item {meme_file.id} is missing journal state.")

        resolved_current_entry = current_entry or self._resolve_current_stage(resolved_stage_entries)
        return ContentPipelineItemRead(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            current_stage=resolved_current_entry.stage,
            current_status=resolved_current_entry.status,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
            last_event_id=resolved_current_entry.last_event_id,
            normalized_reason=resolved_current_entry.normalized_reason,
            last_error_text=resolved_current_entry.last_error_text,
            attempt_count=resolved_current_entry.attempt_count,
            stages=tuple(ContentPipelineStageJournalRead.model_validate(entry) for entry in resolved_stage_entries),
        )

    @staticmethod
    def _sorted_stage_entries(meme_file: MemeFile) -> tuple[PipelineStageJournal, ...]:
        return tuple(
            sorted(
                meme_file.pipeline_stage_journal_entries,
                key=lambda entry: _STAGE_ORDER[entry.stage],
            )
        )

    @staticmethod
    def _resolve_current_stage(stage_entries: tuple[PipelineStageJournal, ...]) -> PipelineStageJournal:
        for stage_entry in stage_entries:
            if stage_entry.status in _ACTIVE_STAGE_STATUSES:
                return stage_entry
        return stage_entries[-1]

    @staticmethod
    def _is_replay_reserved(stage_entry: PipelineStageJournal) -> bool:
        return (
            stage_entry.status in {ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING}
            and stage_entry.normalized_reason == _PIPELINE_REASON_REPLAY_REQUESTED
            and stage_entry.last_event_id is not None
        )

    def _select_replay_entry(
        self,
        stage_entries: tuple[PipelineStageJournal, ...],
        *,
        requested_stage: ContentPipelineStage | None,
    ) -> PipelineStageJournal:
        if requested_stage is not None:
            requested_entry = next(
                (entry for entry in stage_entries if entry.stage is requested_stage),
                None,
            )
            if requested_entry is None:
                raise PipelineReplayNotAllowedError(
                    f"Stage {requested_stage.value} has no durable journal row for this pipeline item.",
                )
            if self._is_replay_reserved(requested_entry):
                return requested_entry
            if requested_entry.status is not ContentPipelineStageStatus.FAILED or not requested_entry.is_retryable:
                raise PipelineReplayNotAllowedError(
                    f"Stage {requested_stage.value} is not in a retryable failed state.",
                )
            return requested_entry

        for stage_entry in reversed(stage_entries):
            if self._is_replay_reserved(stage_entry):
                return stage_entry
            if stage_entry.status is ContentPipelineStageStatus.FAILED and stage_entry.is_retryable:
                return stage_entry

        raise PipelineReplayNotAllowedError("No failed retryable stage exists for this pipeline item.")

    @staticmethod
    def _snapshot_stage(stage_entry: PipelineStageJournal) -> StageJournalSnapshot:
        return StageJournalSnapshot(
            status=stage_entry.status,
            attempt_count=stage_entry.attempt_count,
            last_event_id=stage_entry.last_event_id,
            normalized_reason=stage_entry.normalized_reason,
            last_error_text=stage_entry.last_error_text,
            is_retryable=stage_entry.is_retryable,
            retry_after=stage_entry.retry_after,
            started_at=stage_entry.started_at,
            finished_at=stage_entry.finished_at,
        )

    @staticmethod
    def _reserve_replay(stage_entry: PipelineStageJournal, replay_event: ContentPipelineDispatchEvent) -> None:
        stage_entry.status = ContentPipelineStageStatus.PENDING
        stage_entry.attempt_count = replay_event.attempt
        stage_entry.last_event_id = replay_event.event_id
        stage_entry.normalized_reason = _PIPELINE_REASON_REPLAY_REQUESTED
        stage_entry.last_error_text = None
        stage_entry.is_retryable = True
        stage_entry.retry_after = None
        stage_entry.started_at = None
        stage_entry.finished_at = None

    @staticmethod
    def _matches_list_filter(
        current_entry: PipelineStageJournal,
        *,
        filter_by: ContentPipelineItemFilter,
        stale_before: datetime,
    ) -> bool:
        if filter_by is ContentPipelineItemFilter.ALL:
            return True
        if filter_by is ContentPipelineItemFilter.DUPLICATE:
            return current_entry.status is ContentPipelineStageStatus.DUPLICATE
        if filter_by is ContentPipelineItemFilter.FAILED:
            return current_entry.status is ContentPipelineStageStatus.FAILED
        if filter_by is ContentPipelineItemFilter.STUCK:
            if current_entry.status not in {
                ContentPipelineStageStatus.PENDING,
                ContentPipelineStageStatus.PROCESSING,
            }:
                return False
            if current_entry.retry_after is not None:
                return current_entry.retry_after <= utcnow()
            return current_entry.updated_at <= stale_before
        return False

    @staticmethod
    def _trim_reason(normalized_reason: str) -> str:
        return normalized_reason.strip()[:MAX_PIPELINE_REASON_LENGTH]

    @staticmethod
    def _trim_error_text(last_error_text: str) -> str:
        return last_error_text.strip()[:MAX_PIPELINE_ERROR_LENGTH]

    def _upload_limit_for_media_type(self, media_type: ContentKind) -> int:
        if media_type is ContentKind.IMAGE:
            return self._settings.pipeline_image_upload_max_bytes
        if media_type is ContentKind.GIF:
            return self._settings.pipeline_gif_upload_max_bytes
        if media_type is ContentKind.VIDEO:
            return self._settings.pipeline_video_upload_max_bytes
        return self._settings.pipeline_image_upload_max_bytes

    @staticmethod
    def _translate_media_processing_error(exc: Exception) -> PipelinePayloadValidationError:
        from memexpert.core.media import MediaProcessingError, MediaTimeoutError, MediaValidationError

        if isinstance(exc, MediaTimeoutError):
            return PipelinePayloadValidationError(str(exc))
        if isinstance(exc, MediaValidationError):
            return PipelineUnsupportedMediaTypeError(str(exc))
        if isinstance(exc, MediaProcessingError):
            return PipelinePayloadValidationError(str(exc))
        return PipelinePayloadValidationError(str(exc))

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


__all__ = [
    "ContentPipelineService",
    "ContentPipelineUploadRead",
    "PreparedUpload",
    "StageJournalSnapshot",
]
