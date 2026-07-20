# ruff: noqa: TC001,TC003
"""Stage journal state machine and completion service used by workers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.media.contracts import SUPPORTED_MOVING_MEDIA_MIME_TYPES
from memexpert.messaging.rabbitmq_outbox import recovery_stage_publication_failures
from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile, MemeFileOCRResult, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    MediaGenerationCleanupStatus,
    MediaGenerationStatus,
    PipelineAttemptOutcome,
    RecoveryCapability,
    RecoveryJobItemStatus,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.operations import MediaGeneration, PipelineStageAttempt, RecoveryJobItem
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
from memexpert.services.media_generation import MediaGenerationConflictError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.classification import ClassificationResult
    from memexpert.core.config import Settings
    from memexpert.core.ocr import OCRExtractionResult
    from memexpert.core.qdrant import QdrantSimilarityMatch
    from memexpert.core.voyage import VoyageEmbeddingResult
    from memexpert.media.contracts import NormalizedMediaResult
    from memexpert.messaging.rabbitmq_outbox import RabbitBrokerProtocol
    from memexpert.services.content_merge import MergeOutcome


class CancelledStageDisposition(StrEnum):
    """Broker action that preserves canonical truth after delivery cancellation."""

    ACKNOWLEDGE = "acknowledge"
    DEAD_LETTER = "dead_letter"
    REQUEUE = "requeue"


@dataclass(frozen=True, slots=True)
class CancelledStageResolution:
    """Canonical disposition and failure reason for one cancelled delivery."""

    disposition: CancelledStageDisposition
    normalized_reason: str | None = None


class PipelineStageCompletionService(PipelineDispatchingService):
    """Persist stage transitions, stage outputs, fan-out, and sync snapshots."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        broker: RabbitBrokerProtocol | None = None,
        worker_role: str | None = None,
        worker_instance_id: str | None = None,
    ) -> None:
        super().__init__(session, settings=settings, broker=broker)
        self._worker_role = worker_role
        self._worker_instance_id = worker_instance_id

    async def start_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> PipelineStageWorkContext | None:
        stage_entry = await self._session.scalar(
            select(PipelineStageJournal)
            .where(
                PipelineStageJournal.meme_file_id == meme_file_id,
                PipelineStageJournal.stage == stage,
            )
            .with_for_update()
        )
        if stage_entry is None:
            raise PipelineIngestError(
                f"Pipeline item {meme_file_id} does not have durable journal state for stage {stage.value}."
            )
        if stage_entry.last_event_id == event_id and stage_entry.status in {
            ContentPipelineStageStatus.PROCESSING,
            ContentPipelineStageStatus.SUCCEEDED,
        }:
            await self._session.commit()
            return None

        meme_file = await self._get_meme_file(meme_file_id)
        recovery_item = await self._load_recovery_item(event_id)
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
        await self._record_attempt_started(
            meme_file_id=meme_file_id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            started_at=started_at,
        )
        preserve_file_status = _should_preserve_file_status(recovery_item, meme_file)
        if stage not in _consts.SYNC_STAGES and not preserve_file_status:
            meme_file.status = ContentProcessingStatus.PROCESSING

        await self._commit_stage_mutation("Failed to persist running stage state.")
        return PipelineStageWorkContext(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            mime_type=meme_file.mime_type,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
            recovery_item_id=recovery_item.id if recovery_item is not None else None,
            preserve_ready=preserve_file_status,
            retry_limit=recovery_item.retry_limit if recovery_item is not None else 3,
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
        # Atomic activation serializes on the file aggregate before locking the
        # transcode journal and generation rows.  Besides preventing concurrent
        # pointer swaps, the locked load refreshes any stale identity-map state.
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.TRANSCODE,
            for_update=True,
        )
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        moving_media = (meme_file.mime_type or "").lower() in SUPPORTED_MOVING_MEDIA_MIME_TYPES
        if moving_media and (
            result.generation_id is None
            or result.web_video_object_key is None
            or result.preview_image_object_key is None
        ):
            raise MediaGenerationConflictError(
                "Moving-media completion requires a reserved immutable generation and both artifacts."
            )
        if result.web_video_object_key is not None:
            if result.generation_id is None:
                # Static-image processors do not reserve moving-media generations.
                meme_file.s3_web_video_key = result.web_video_object_key
                meme_file.active_media_generation_id = None
                meme_file.source_has_audio = result.source_has_audio
                meme_file.web_video_has_audio = result.web_video_has_audio
                meme_file.web_video_profile = result.web_video_profile
                meme_file.web_video_verified_at = result.web_video_verified_at
            else:
                generation = await self._session.get(
                    MediaGeneration,
                    result.generation_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if generation is None or generation.meme_file_id != meme_file.id:
                    raise MediaGenerationConflictError("Moving-media generation no longer belongs to this file.")
                if generation.status is not MediaGenerationStatus.UPLOADED:
                    raise MediaGenerationConflictError(
                        "Moving-media generation was not upload-verified before activation."
                    )
                if meme_file.s3_web_video_key != generation.expected_web_video_object_key:
                    generation.status = MediaGenerationStatus.STALE
                    generation.cleanup_status = MediaGenerationCleanupStatus.PENDING
                    generation.safe_failure_reason = "active_pointer_changed"
                    generation.safe_failure_text = "The active web-video pointer changed before activation."
                    await self._session.commit()
                    raise MediaGenerationConflictError("The active web-video pointer changed before activation.")
                if (
                    generation.web_video_object_key != result.web_video_object_key
                    or generation.preview_image_object_key != result.preview_image_object_key
                ):
                    raise MediaGenerationConflictError("Completion output does not match the reserved generation keys.")
                previous_generation = (
                    await self._session.get(
                        MediaGeneration,
                        meme_file.active_media_generation_id,
                        with_for_update=True,
                        populate_existing=True,
                    )
                    if meme_file.active_media_generation_id is not None
                    else None
                )
                activated_at = utcnow()
                if previous_generation is not None and previous_generation.id != generation.id:
                    previous_generation.status = MediaGenerationStatus.SUPERSEDED
                    previous_generation.superseded_at = activated_at
                    previous_generation.cleanup_status = MediaGenerationCleanupStatus.PENDING
                meme_file.s3_web_video_key = generation.web_video_object_key
                meme_file.active_media_generation_id = generation.id
                meme_file.source_has_audio = result.source_has_audio
                meme_file.web_video_has_audio = result.web_video_has_audio
                meme_file.web_video_profile = result.web_video_profile
                meme_file.web_video_verified_at = result.web_video_verified_at or generation.verified_at or activated_at
                generation.status = MediaGenerationStatus.ACTIVE
                generation.activated_at = activated_at
                generation.cleanup_status = MediaGenerationCleanupStatus.NOT_ELIGIBLE
                generation.cleanup_error_text = None
        else:
            meme_file.s3_web_video_key = None
            meme_file.active_media_generation_id = None
            meme_file.source_has_audio = result.source_has_audio
            meme_file.web_video_has_audio = None
            meme_file.web_video_profile = None
            meme_file.web_video_verified_at = None
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
            stage_entry.status is ContentPipelineStageStatus.SUCCEEDED and stage_entry.last_event_id == event_id
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
        await self._apply_stage_failure(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            retryable=retryable,
            consume_retry_budget=True,
        )

    async def abandon_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> CancelledStageResolution:
        """Choose the safe broker disposition without overwriting later truth."""

        stage_entry = await self._session.scalar(
            select(PipelineStageJournal)
            .where(
                PipelineStageJournal.meme_file_id == meme_file_id,
                PipelineStageJournal.stage == stage,
            )
            .with_for_update()
        )
        if stage_entry is None:
            raise PipelineIngestError(
                f"Pipeline item {meme_file_id} does not have durable journal state for stage {stage.value}."
            )
        if stage_entry.last_event_id != event_id or attempt < stage_entry.attempt_count:
            await self._session.commit()
            return CancelledStageResolution(CancelledStageDisposition.ACKNOWLEDGE)

        if stage_entry.status is not ContentPipelineStageStatus.PROCESSING:
            if stage_entry.status is ContentPipelineStageStatus.FAILED and (
                not stage_entry.is_retryable or attempt >= self._broker_settings.retry_max_attempts
            ):
                disposition = CancelledStageDisposition.DEAD_LETTER
            elif stage_entry.status is ContentPipelineStageStatus.PENDING or (
                stage_entry.status is ContentPipelineStageStatus.FAILED and stage_entry.is_retryable
            ):
                disposition = CancelledStageDisposition.REQUEUE
            else:
                disposition = CancelledStageDisposition.ACKNOWLEDGE
            await self._session.commit()
            return CancelledStageResolution(
                disposition,
                normalized_reason=(
                    stage_entry.normalized_reason if disposition is CancelledStageDisposition.DEAD_LETTER else None
                ),
            )

        meme_file = await self._get_meme_file(meme_file_id)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        await self._apply_stage_failure(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            retryable=True,
            consume_retry_budget=False,
        )
        return CancelledStageResolution(CancelledStageDisposition.REQUEUE)

    async def _apply_stage_failure(
        self,
        *,
        meme_file: MemeFile,
        stage_entry: PipelineStageJournal,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
        retryable: bool,
        consume_retry_budget: bool,
    ) -> None:
        failed_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.FAILED
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = trim_reason(normalized_reason)
        stage_entry.last_error_text = trim_error_text(last_error_text)
        stage_entry.is_retryable = retryable
        stage_entry.retry_after = (
            failed_at + timedelta(seconds=self._broker_settings.retry_backoff_seconds) if retryable else None
        )
        stage_entry.started_at = stage_entry.started_at or failed_at
        stage_entry.finished_at = failed_at
        recovery_item = await self._load_recovery_item(event_id, lock=True)
        derivative_maintenance = bool(
            recovery_item is not None and recovery_item.action is RecoveryCapability.REGENERATE_DERIVATIVES
        )
        preserve_file_status = _should_preserve_file_status(recovery_item, meme_file)
        if stage not in _consts.SYNC_STAGES and not preserve_file_status and not derivative_maintenance:
            meme_file.status = ContentProcessingStatus.FAILED

        if recovery_item is not None and retryable and consume_retry_budget:
            budget_start = recovery_item.attempt_budget_start or attempt
            recovery_item.attempt_budget_start = budget_start
            publication_failures = recovery_stage_publication_failures(recovery_item)
            recovery_item.retryable_failures_consumed = max(
                recovery_item.retryable_failures_consumed,
                publication_failures + attempt - budget_start + 1,
            )
        await self._record_attempt_finished(
            meme_file_id=meme_file.id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            outcome=(
                PipelineAttemptOutcome.SKIPPED
                if not consume_retry_budget
                else PipelineAttemptOutcome.FAILED_RETRYABLE
                if retryable
                else PipelineAttemptOutcome.FAILED_TERMINAL
            ),
            normalized_reason=normalized_reason,
            safe_error_text=last_error_text,
            finished_at=failed_at,
        )

        if derivative_maintenance and recovery_item is not None:
            _restore_stage_state_after_derivative_failure(
                stage_entry,
                recovery_item.previous_stage_state,
                fallback_attempt=max(attempt - 1, 0),
            )
            budget_exhausted = recovery_item.retryable_failures_consumed >= recovery_item.retry_limit
            if not retryable or budget_exhausted:
                recovery_item.status = RecoveryJobItemStatus.FAILED
                recovery_item.normalized_reason = trim_reason(normalized_reason)
                recovery_item.safe_error_text = trim_error_text(last_error_text)
                recovery_item.finished_at = failed_at
                recovery_item.reservation_active = False

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

        await self._record_attempt_finished(
            meme_file_id=meme_file.id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            outcome=PipelineAttemptOutcome.SUCCEEDED,
            normalized_reason=None,
            safe_error_text=None,
            finished_at=finished_at,
        )

        recovery_item = await self._load_recovery_item(event_id, lock=True)
        downstream_dispatches = (
            ()
            if recovery_item is not None and recovery_item.suppress_fanout
            else prepare_downstream_dispatches(
                self._session,
                meme_file=meme_file,
                stage=stage,
                created_at=finished_at,
            )
        )
        preserve_file_status = _should_preserve_file_status(recovery_item, meme_file)
        if stage is ContentPipelineStage.CLASSIFY and not preserve_file_status:
            meme_file.status = ContentProcessingStatus.READY
        elif stage not in _consts.SYNC_STAGES and not preserve_file_status:
            meme_file.status = ContentProcessingStatus.PROCESSING
        outbox_message_ids = []
        dispatch_events = tuple(dispatch.event for dispatch in downstream_dispatches) + extra_dispatch_events
        for dispatch_event in dispatch_events:
            outbox_message_ids.append(await self._enqueue_dispatch_event(dispatch_event))

        await self._commit_stage_mutation("Failed to persist successful stage state.")
        await self._relay_outbox_messages_after_commit(tuple(outbox_message_ids))

    async def _record_attempt_started(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        started_at: datetime,
    ) -> None:
        attempt_row = await self._load_attempt(
            meme_file_id=meme_file_id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )
        if attempt_row is None:
            recovery_item_id = await self._session.scalar(
                select(RecoveryJobItem.id).where(RecoveryJobItem.dispatch_event_id == event_id)
            )
            attempt_row = PipelineStageAttempt(
                meme_file_id=meme_file_id,
                stage=stage,
                event_id=event_id,
                attempt_number=attempt,
                recovery_item_id=recovery_item_id,
                worker_role=self._worker_role,
                worker_instance_id=self._worker_instance_id,
                started_at=started_at,
            )
            self._session.add(attempt_row)
            return
        attempt_row.outcome = PipelineAttemptOutcome.PROCESSING
        attempt_row.normalized_reason = None
        attempt_row.safe_error_text = None
        attempt_row.finished_at = None
        attempt_row.worker_role = self._worker_role or attempt_row.worker_role
        attempt_row.worker_instance_id = self._worker_instance_id or attempt_row.worker_instance_id

    async def _record_attempt_finished(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        outcome: PipelineAttemptOutcome,
        normalized_reason: str | None,
        safe_error_text: str | None,
        finished_at: datetime,
    ) -> None:
        attempt_row = await self._load_attempt(
            meme_file_id=meme_file_id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )
        if attempt_row is None:
            recovery_item_id = await self._session.scalar(
                select(RecoveryJobItem.id).where(RecoveryJobItem.dispatch_event_id == event_id)
            )
            attempt_row = PipelineStageAttempt(
                meme_file_id=meme_file_id,
                stage=stage,
                event_id=event_id,
                attempt_number=attempt,
                recovery_item_id=recovery_item_id,
                worker_role=self._worker_role,
                worker_instance_id=self._worker_instance_id,
                started_at=finished_at,
            )
            self._session.add(attempt_row)
        attempt_row.outcome = outcome
        attempt_row.normalized_reason = trim_reason(normalized_reason) if normalized_reason else None
        attempt_row.safe_error_text = trim_error_text(safe_error_text) if safe_error_text else None
        attempt_row.finished_at = finished_at

    async def _load_attempt(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> PipelineStageAttempt | None:
        return await self._session.scalar(
            select(PipelineStageAttempt).where(
                PipelineStageAttempt.meme_file_id == meme_file_id,
                PipelineStageAttempt.stage == stage,
                PipelineStageAttempt.event_id == event_id,
                PipelineStageAttempt.attempt_number == attempt,
            )
        )

    async def _load_recovery_item(
        self,
        event_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> RecoveryJobItem | None:
        statement = select(RecoveryJobItem).where(RecoveryJobItem.dispatch_event_id == event_id)
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)


def _restore_stage_state_after_derivative_failure(
    stage_entry: PipelineStageJournal,
    snapshot: dict[str, object],
    *,
    fallback_attempt: int,
) -> None:
    """Restore catalog pipeline truth while the generation ledger records maintenance failure."""

    raw_status = snapshot.get("status")
    try:
        status = ContentPipelineStageStatus(str(raw_status))
    except ValueError:
        status = ContentPipelineStageStatus.SUCCEEDED
    stage_entry.status = status
    stage_entry.attempt_count = _snapshot_int(snapshot.get("attempt_count"), fallback=fallback_attempt)
    stage_entry.last_event_id = _snapshot_uuid(snapshot.get("last_event_id"))
    stage_entry.normalized_reason = _snapshot_optional_text(snapshot.get("normalized_reason"))
    stage_entry.last_error_text = _snapshot_optional_text(snapshot.get("last_error_text"))
    raw_retryable = snapshot.get("is_retryable")
    stage_entry.is_retryable = raw_retryable if isinstance(raw_retryable, bool) else False
    stage_entry.retry_after = _snapshot_datetime(snapshot.get("retry_after"))
    stage_entry.started_at = _snapshot_datetime(snapshot.get("started_at"))
    stage_entry.finished_at = _snapshot_datetime(snapshot.get("finished_at"))


def _should_preserve_file_status(
    recovery_item: RecoveryJobItem | None,
    meme_file: MemeFile,
) -> bool:
    if recovery_item is None:
        return False
    if recovery_item.action is RecoveryCapability.REGENERATE_DERIVATIVES:
        return True
    return recovery_item.preserve_ready and meme_file.status is ContentProcessingStatus.READY


def _snapshot_int(value: object, *, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return fallback
    try:
        return max(int(value), 0)
    except ValueError:
        return fallback


def _snapshot_uuid(value: object) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _snapshot_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _snapshot_optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "CancelledStageDisposition",
    "CancelledStageResolution",
    "PipelineStageCompletionService",
]
