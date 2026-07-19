# ruff: noqa: TC001,TC003
"""Stage journal state machine and completion service used by workers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile, MemeFileOCRResult, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    PipelineAttemptOutcome,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.operations import PipelineStageAttempt, RecoveryJobItem
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
    from datetime import datetime

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
        if stage not in _consts.SYNC_STAGES:
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
                    stage_entry.normalized_reason
                    if disposition is CancelledStageDisposition.DEAD_LETTER
                    else None
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
        if stage not in _consts.SYNC_STAGES:
            meme_file.status = ContentProcessingStatus.FAILED

        await self._record_attempt_finished(
            meme_file_id=meme_file.id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            outcome=(PipelineAttemptOutcome.FAILED_RETRYABLE if retryable else PipelineAttemptOutcome.FAILED_TERMINAL),
            normalized_reason=normalized_reason,
            safe_error_text=last_error_text,
            finished_at=failed_at,
        )

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

        downstream_dispatches = prepare_downstream_dispatches(
            self._session,
            meme_file=meme_file,
            stage=stage,
            created_at=finished_at,
        )
        if stage is ContentPipelineStage.CLASSIFY:
            meme_file.status = ContentProcessingStatus.READY
        elif stage not in _consts.SYNC_STAGES:
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


__all__ = [
    "CancelledStageDisposition",
    "CancelledStageResolution",
    "PipelineStageCompletionService",
]
