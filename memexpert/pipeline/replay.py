# ruff: noqa: TC001,TC003
"""Operator replay policy for pipeline items and per-target sync stages."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError

from memexpert.models.base import utcnow
from memexpert.models.content import PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentSourceKind,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.dispatch import PipelineDispatchingService
from memexpert.pipeline.helpers import is_replay_reserved, reserve_replay, snapshot_stage, sorted_stage_entries
from memexpert.pipeline.sync_status import ensure_sync_replay_allowed, upsert_sync_target_snapshot
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineReplayAccepted,
)
from memexpert.services.errors import PipelineIngestError, PipelinePublishError, PipelineReplayNotAllowedError


class PipelineReplayService(PipelineDispatchingService):
    """Reserve and publish operator-requested pipeline replays."""

    async def replay_item(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage | None = None,
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
        snapshot = snapshot_stage(target_entry)

        reserve_replay(target_entry, replay_event)
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

    async def replay_sync_target(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
    ) -> ContentPipelineReplayAccepted:
        meme_file = await self._get_meme_file(meme_file_id)
        ensure_sync_replay_allowed(meme_file)
        return await self._replay_single_sync_target(meme_file_id, target)

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
        snapshot = snapshot_stage(stage_entry)
        reserve_replay(stage_entry, replay_event)

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
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(
                f"Failed to persist sync replay reservation state for {target.value}.",
            ) from exc

        try:
            await self._publisher(replay_event)
        except Exception as exc:
            await self._restore_stage_snapshot(stage_entry.id, snapshot)
            raise PipelinePublishError(
                f"Sync replay was reserved, but downstream dispatch for {target.value} failed.",
            ) from exc

        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
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
