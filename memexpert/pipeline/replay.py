# ruff: noqa: TC001,TC003
"""Operator replay policy for pipeline items and per-target sync stages."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

from memexpert.models.base import utcnow
from memexpert.models.content import PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentSourceKind,
    RecoveryJobItemStatus,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.operations import RecoveryJobItem
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.dispatch import PipelineDispatchingService
from memexpert.pipeline.helpers import is_replay_reserved, reserve_replay, sorted_stage_entries
from memexpert.pipeline.sync_status import ensure_sync_replay_allowed, upsert_sync_target_snapshot
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineReplayAccepted,
)
from memexpert.services.errors import PipelineIngestError, PipelineReplayNotAllowedError


class PipelineReplayService(PipelineDispatchingService):
    """Reserve and publish operator-requested pipeline replays."""

    async def replay_item(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage | None = None,
        recovery_item: RecoveryJobItem | None = None,
    ) -> ContentPipelineReplayAccepted:
        meme_file = await self._get_meme_file(meme_file_id)
        if not meme_file.s3_original_key:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} is missing durable original storage identifiers.",
            )

        stage_entries = sorted_stage_entries(meme_file)
        target_entry = self._select_replay_entry(stage_entries, requested_stage=stage)

        if is_replay_reserved(target_entry):
            if target_entry.last_event_id is None:
                raise PipelineReplayNotAllowedError(
                    f"Pipeline item {meme_file_id} is already reserved for replay, but its event id is missing.",
                )
            self._attach_recovery_item(recovery_item, target_entry.last_event_id)
            if recovery_item is not None and recovery_item.attempt_budget_start is None:
                recovery_item.attempt_budget_start = max(target_entry.attempt_count, 1)
            if recovery_item is not None:
                await self._commit_recovery_attachment()
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
        reserve_replay(target_entry, replay_event)
        self._attach_recovery_item(recovery_item, replay_event.event_id, attempt=replay_attempt)
        outbox_message_id = await self._enqueue_dispatch_event(replay_event)
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist replay reservation state.") from exc

        await self._relay_outbox_messages_after_commit((outbox_message_id,))

        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
        )

    async def replay_admin_stage(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage,
        recovery_item: RecoveryJobItem,
    ) -> ContentPipelineReplayAccepted:
        """Reserve a successful or failed stage for cookie-admin orchestration.

        The operator-token methods intentionally retain their failure-only
        policy. Eligibility and terminal acknowledgement are checked again
        here so this broader path cannot be called safely without a durable
        recovery item.
        """

        if stage is ContentPipelineStage.INGEST:
            raise PipelineReplayNotAllowedError("The ingest stage cannot be replayed.")
        meme_file = await self._get_meme_file(meme_file_id)
        if not meme_file.s3_original_key:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} is missing durable original storage identifiers.",
            )
        stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is stage),
            None,
        )
        if stage_entry is None:
            stage_entry = PipelineStageJournal(
                id=uuid.uuid7(),
                meme_file_id=meme_file.id,
                stage=stage,
                status=ContentPipelineStageStatus.FAILED,
                attempt_count=0,
                normalized_reason="orchestrated_stage_not_materialized",
                is_retryable=True,
            )
            self._session.add(stage_entry)
            meme_file.pipeline_stage_journal_entries.append(stage_entry)
            if recovery_item.work_id.startswith(f"{meme_file.id}:"):
                recovery_item.work_id = str(stage_entry.id)

        if is_replay_reserved(stage_entry):
            if stage_entry.last_event_id is None:
                raise PipelineReplayNotAllowedError(
                    f"Pipeline item {meme_file_id} has an incomplete replay reservation.",
                )
            self._attach_recovery_item(
                recovery_item,
                stage_entry.last_event_id,
                attempt=max(stage_entry.attempt_count, 1),
            )
            await self._commit_recovery_attachment()
            return ContentPipelineReplayAccepted(
                meme_file_id=meme_file.id,
                replay_event_id=stage_entry.last_event_id,
                stage=stage,
                attempt=max(stage_entry.attempt_count, 1),
            )

        if stage_entry.status in {
            ContentPipelineStageStatus.PENDING,
            ContentPipelineStageStatus.PROCESSING,
            ContentPipelineStageStatus.DUPLICATE,
        }:
            raise PipelineReplayNotAllowedError(
                f"Stage {stage.value} is {stage_entry.status.value} and cannot be replayed yet.",
            )
        if (
            stage_entry.status is ContentPipelineStageStatus.FAILED
            and not stage_entry.is_retryable
            and not recovery_item.terminal_override_acknowledged
        ):
            raise PipelineReplayNotAllowedError(
                f"Terminal {stage.value} replay requires an audited acknowledgement.",
            )

        replay_attempt = max(stage_entry.attempt_count + 1, 1)
        replay_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=replay_attempt,
            created_at=utcnow(),
        )
        cast("Any", recovery_item).previous_stage_state = {
            "status": stage_entry.status.value,
            "attempt_count": stage_entry.attempt_count,
            "last_event_id": str(stage_entry.last_event_id) if stage_entry.last_event_id else None,
            "normalized_reason": stage_entry.normalized_reason,
            "last_error_text": stage_entry.last_error_text,
            "is_retryable": stage_entry.is_retryable,
            "retry_after": stage_entry.retry_after.isoformat() if stage_entry.retry_after else None,
            "started_at": stage_entry.started_at.isoformat() if stage_entry.started_at else None,
            "finished_at": stage_entry.finished_at.isoformat() if stage_entry.finished_at else None,
        }
        reserve_replay(stage_entry, replay_event)
        self._attach_recovery_item(recovery_item, replay_event.event_id, attempt=replay_attempt)
        recovery_item.canonical_version = f"{utcnow().isoformat()}:{replay_event.event_id}"

        if stage in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}:
            target = SyncTargetKind.QDRANT if stage is ContentPipelineStage.SYNC_QDRANT else SyncTargetKind.MEILISEARCH
            await upsert_sync_target_snapshot(
                self._session,
                meme_file_id=meme_file_id,
                target=target,
                status=SyncTargetStatus.PENDING,
                last_event_id=replay_event.event_id,
                preview=None,
                normalized_reason=_consts.PIPELINE_REASON_SYNC_REPLAY_REQUESTED,
                last_error_text=None,
                bump_attempt=False,
                record_success=False,
            )
        outbox_message_id = await self._enqueue_dispatch_event(replay_event)
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist admin replay reservation state.") from exc
        await self._relay_outbox_messages_after_commit((outbox_message_id,))
        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
        )

    async def replay_sync_target(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
        *,
        recovery_item: RecoveryJobItem | None = None,
    ) -> ContentPipelineReplayAccepted:
        meme_file = await self._get_meme_file(meme_file_id)
        ensure_sync_replay_allowed(meme_file)
        return await self._replay_single_sync_target(
            meme_file_id,
            target,
            recovery_item=recovery_item,
        )

    async def replay_sync_target_batch(
        self,
        meme_file_ids: Sequence[uuid.UUID],
        target: SyncTargetKind,
    ) -> tuple[ContentPipelineReplayAccepted, ...]:
        if len(meme_file_ids) > _consts.SYNC_REPLAY_BATCH_MAX:
            raise PipelineReplayNotAllowedError(
                "Sync replay batch size "
                f"{len(meme_file_ids)} exceeds the configured maximum of {_consts.SYNC_REPLAY_BATCH_MAX}.",
            )
        for meme_file_id in meme_file_ids:
            meme_file = await self._get_meme_file(meme_file_id)
            ensure_sync_replay_allowed(meme_file)

        accepted: list[ContentPipelineReplayAccepted] = []
        for meme_file_id in meme_file_ids:
            replay = await self._replay_single_sync_target(meme_file_id, target)
            accepted.append(replay)
        return tuple(accepted)

    async def _replay_single_sync_target(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
        *,
        recovery_item: RecoveryJobItem | None = None,
    ) -> ContentPipelineReplayAccepted:
        stage = _consts.SYNC_STAGE_BY_TARGET[target]
        meme_file = await self._get_meme_file(meme_file_id)
        if not meme_file.s3_original_key:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} is missing durable original storage identifiers.",
            )

        stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is stage),
            None,
        )
        if stage_entry is None:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} has no durable {stage.value} stage row yet.",
            )

        if is_replay_reserved(stage_entry):
            if stage_entry.last_event_id is None:
                raise PipelineReplayNotAllowedError(
                    f"Pipeline item {meme_file_id} is already reserved for replay, but its event id is missing.",
                )
            self._attach_recovery_item(recovery_item, stage_entry.last_event_id)
            if recovery_item is not None and recovery_item.attempt_budget_start is None:
                recovery_item.attempt_budget_start = max(stage_entry.attempt_count, 1)
            if recovery_item is not None:
                await self._commit_recovery_attachment()
            return ContentPipelineReplayAccepted(
                meme_file_id=meme_file.id,
                replay_event_id=stage_entry.last_event_id,
                stage=stage,
                attempt=max(stage_entry.attempt_count, 1),
            )

        replay_attempt = max(stage_entry.attempt_count + 1, 1)
        replay_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=replay_attempt,
            created_at=utcnow(),
        )
        reserve_replay(stage_entry, replay_event)
        self._attach_recovery_item(recovery_item, replay_event.event_id, attempt=replay_attempt)

        await upsert_sync_target_snapshot(
            self._session,
            meme_file_id=meme_file_id,
            target=target,
            status=SyncTargetStatus.PENDING,
            last_event_id=replay_event.event_id,
            preview=None,
            normalized_reason=_consts.PIPELINE_REASON_SYNC_REPLAY_REQUESTED,
            last_error_text=None,
            bump_attempt=False,
            record_success=False,
        )
        outbox_message_id = await self._enqueue_dispatch_event(replay_event)
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(
                f"Failed to persist sync replay reservation state for {target.value}.",
            ) from exc

        await self._relay_outbox_messages_after_commit((outbox_message_id,))

        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
        )

    @staticmethod
    def _attach_recovery_item(
        recovery_item: RecoveryJobItem | None,
        event_id: uuid.UUID,
        *,
        attempt: int | None = None,
    ) -> None:
        if recovery_item is None:
            return
        recovery_item.status = RecoveryJobItemStatus.DISPATCHED
        recovery_item.dispatch_event_id = event_id
        recovery_item.dispatched_at = utcnow()
        recovery_item.normalized_reason = None
        recovery_item.safe_error_text = None
        recovery_item.reservation_active = True
        if recovery_item.attempt_budget_start is None and attempt is not None:
            recovery_item.attempt_budget_start = attempt

    async def _commit_recovery_attachment(self) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to attach an existing replay to recovery work.") from exc

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
            if is_replay_reserved(requested_entry):
                return requested_entry
            if requested_entry.status is not ContentPipelineStageStatus.FAILED or not requested_entry.is_retryable:
                raise PipelineReplayNotAllowedError(
                    f"Stage {requested_stage.value} is not in a retryable failed state.",
                )
            return requested_entry

        for stage_entry in reversed(stage_entries):
            if is_replay_reserved(stage_entry):
                return stage_entry
            if stage_entry.status is ContentPipelineStageStatus.FAILED and stage_entry.is_retryable:
                return stage_entry

        raise PipelineReplayNotAllowedError("No failed retryable stage exists for this pipeline item.")


__all__ = ["PipelineReplayService"]
