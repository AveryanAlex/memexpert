# ruff: noqa: TC001,TC003
"""Stage journal state machine and completion service used by workers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING

from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile, MemeFileOCRResult, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.dispatch import (
    PipelineDispatchingService,
    PipelineStageWorkContext,
    prepare_downstream_dispatches,
)
from memexpert.pipeline.embedding_cache import persist_embedding_cache_row, validate_embedding_contract
from memexpert.pipeline.helpers import (
    build_sync_preview_model,
    ensure_stage_attempt_is_current,
    trim_error_text,
    trim_reason,
)
from memexpert.pipeline.sync_status import load_sync_target_status, upsert_sync_target_snapshot
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    PerTargetSyncStatus,
)
from memexpert.services.content_merge import ContentMergeService
from memexpert.services.errors import (
    PipelineIngestError,
    PipelineMergeTransactionError,
)

if TYPE_CHECKING:
    from memexpert.core.classification import ClassificationResult
    from memexpert.core.ocr import OCRExtractionResult
    from memexpert.core.qdrant import QdrantSimilarityMatch
    from memexpert.core.voyage import VoyageEmbeddingResult
    from memexpert.media.contracts import NormalizedMediaResult
    from memexpert.services.content_merge import MergeOutcome


class PipelineStageCompletionService(PipelineDispatchingService):
    """Persist stage transitions, stage outputs, fan-out, and sync snapshots."""

    async def start_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> PipelineStageWorkContext:
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
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
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, ContentPipelineStage.TRANSCODE)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
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
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, ContentPipelineStage.OCR)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

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
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.EMBED,
        )
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        validate_embedding_contract(self._settings, embedding_result)
        await persist_embedding_cache_row(
            self._session,
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
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.CLASSIFY,
        )
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        target_meme = await self._get_canonical_meme(meme_file.meme_id)
        target_meme.is_nsfw = target_meme.is_nsfw or classification_result.is_nsfw
        await self._apply_canonical_primary_truth(target_meme)

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.CLASSIFY,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_sync_qdrant_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: Mapping[str, object],
    ) -> PerTargetSyncStatus:
        return await self._complete_sync_target_stage(
            target=SyncTargetKind.QDRANT,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            payload_preview=payload_preview,
        )

    async def complete_sync_meili_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: Mapping[str, object],
    ) -> PerTargetSyncStatus:
        return await self._complete_sync_target_stage(
            target=SyncTargetKind.MEILISEARCH,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            payload_preview=payload_preview,
        )

    async def _complete_sync_target_stage(
        self,
        *,
        target: SyncTargetKind,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: Mapping[str, object],
    ) -> PerTargetSyncStatus:
        stage = _consts.SYNC_STAGE_BY_TARGET[target]
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        already_succeeded = (
            stage_entry.status is ContentPipelineStageStatus.SUCCEEDED
            and stage_entry.last_event_id == event_id
        )

        preview_model = build_sync_preview_model(payload_preview, target=target) if payload_preview else None
        await upsert_sync_target_snapshot(
            self._session,
            meme_file_id=meme_file_id,
            target=target,
            status=SyncTargetStatus.SYNCED,
            last_event_id=event_id,
            preview=preview_model,
            normalized_reason=None,
            last_error_text=None,
            bump_attempt=not already_succeeded,
            record_success=True,
        )

        sync_success_event = (
            self._build_sync_success_event(
                meme_file=meme_file,
                stage_entry=stage_entry,
                event_type=_consts.SYNC_SUCCESS_EVENT_TYPE_BY_TARGET[target],
                stage=stage,
                attempt=attempt,
            )
            if not already_succeeded
            else None
        )

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            extra_dispatch_events=(sync_success_event,) if sync_success_event is not None else (),
        )

        return await load_sync_target_status(self._session, meme_file_id, target)

    def _build_sync_success_event(
        self,
        *,
        meme_file: MemeFile,
        stage_entry: PipelineStageJournal,
        event_type: ContentPipelineEventType,
        stage: ContentPipelineStage,
        attempt: int,
    ) -> ContentPipelineDispatchEvent:
        return ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=event_type,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=max(stage_entry.attempt_count, attempt, 1),
            created_at=utcnow(),
        )

    async def fail_sync_qdrant_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> PerTargetSyncStatus:
        return await self._fail_sync_target_stage(
            target=SyncTargetKind.QDRANT,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
        )

    async def fail_sync_meili_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> PerTargetSyncStatus:
        return await self._fail_sync_target_stage(
            target=SyncTargetKind.MEILISEARCH,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
        )

    async def _fail_sync_target_stage(
        self,
        *,
        target: SyncTargetKind,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> PerTargetSyncStatus:
        is_retryable = normalized_reason != _consts.SYNC_MALFORMED_REASON_BY_TARGET[target]
        await upsert_sync_target_snapshot(
            self._session,
            meme_file_id=meme_file_id,
            target=target,
            status=SyncTargetStatus.FAILED,
            last_event_id=event_id,
            preview=None,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            bump_attempt=True,
            record_success=False,
        )
        await self.mark_stage_failed(
            meme_file_id=meme_file_id,
            stage=_consts.SYNC_STAGE_BY_TARGET[target],
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            retryable=is_retryable,
        )
        return await load_sync_target_status(self._session, meme_file_id, target)

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
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        failed_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.FAILED
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = trim_reason(normalized_reason)
        stage_entry.last_error_text = trim_error_text(last_error_text)
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
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
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
        extra_dispatch_events: tuple[ContentPipelineDispatchEvent, ...] = (),
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

        downstream_dispatches = prepare_downstream_dispatches(
            self._session,
            meme_file=meme_file,
            stage=stage,
            created_at=finished_at,
        )
        if stage is ContentPipelineStage.CLASSIFY:
            meme_file.status = ContentProcessingStatus.READY
        elif stage not in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}:
            meme_file.status = ContentProcessingStatus.PROCESSING

        outbox_message_ids = []
        dispatch_events = tuple(dispatch.event for dispatch in downstream_dispatches) + extra_dispatch_events
        for dispatch_event in dispatch_events:
            outbox_message_ids.append(await self._enqueue_dispatch_event(dispatch_event))

        await self._commit_stage_mutation("Failed to persist successful stage state.")
        await self._relay_outbox_messages_after_commit(tuple(outbox_message_ids))


__all__ = ["PipelineStageCompletionService"]
