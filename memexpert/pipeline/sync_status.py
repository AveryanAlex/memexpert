# ruff: noqa: TC001,TC003
"""Per-target search-sync snapshot status helpers and read service."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile, MemeFileSyncTargetSnapshot
from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus, SyncTargetKind, SyncTargetStatus
from memexpert.pipeline.helpers import trim_error_text, trim_reason
from memexpert.pipeline.reporting import decode_sync_preview
from memexpert.pipeline.state import PipelineDatabaseService
from memexpert.schemas.content_pipeline import ContentPipelineSyncTargetPreview, PerTargetSyncStatus
from memexpert.services.errors import PipelineIngestError, PipelineItemNotFoundError, PipelineReplayNotAllowedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PipelineSyncStatusService(PipelineDatabaseService):
    """Read the current durable sync truth for one pipeline item/target."""

    async def get_sync_target_status(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
    ) -> PerTargetSyncStatus:
        _ = await self._get_meme_file(meme_file_id)
        snapshots = await load_sync_target_snapshots(self._session, meme_file_id)
        row = snapshots.get(target)
        if row is None:
            raise PipelineItemNotFoundError(
                f"Pipeline item {meme_file_id} has no {target.value} sync snapshot row yet.",
            )
        return project_sync_target_status(row, target=target)


async def upsert_sync_target_snapshot(
    session: AsyncSession,
    *,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
    status: SyncTargetStatus,
    last_event_id: uuid.UUID | None,
    preview: ContentPipelineSyncTargetPreview | None,
    normalized_reason: str | None,
    last_error_text: str | None,
    bump_attempt: bool,
    record_success: bool,
) -> MemeFileSyncTargetSnapshot:
    """Upsert the snapshot row for one ``(meme_file_id, target)`` pair."""

    now = utcnow()
    snapshots = await load_sync_target_snapshots(session, meme_file_id)
    existing = snapshots.get(target)

    trimmed_reason = trim_reason(normalized_reason) if normalized_reason is not None else None
    trimmed_error = trim_error_text(last_error_text) if last_error_text is not None else None
    preview_json = preview.model_dump(mode="json") if preview is not None else None

    if existing is None:
        row = MemeFileSyncTargetSnapshot(
            meme_file_id=meme_file_id,
            sync_target=target,
            status=status,
            last_event_id=last_event_id,
            normalized_reason=trimmed_reason,
            last_error_text=trimmed_error,
            last_payload_preview=preview_json or {},
            last_success_at=now if record_success else None,
            last_attempt_at=now,
            attempt_count=1 if bump_attempt else 0,
        )
        session.add(row)
        await session.flush()
        return row

    existing.status = status
    existing.last_event_id = last_event_id
    existing.normalized_reason = trimmed_reason
    existing.last_error_text = trimmed_error
    existing.last_attempt_at = now
    if bump_attempt:
        existing.attempt_count = existing.attempt_count + 1
    if record_success:
        existing.last_success_at = now
        if preview_json is not None:
            existing.last_payload_preview = preview_json
    await session.flush()
    return existing


async def load_sync_target_status(
    session: AsyncSession,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
) -> PerTargetSyncStatus:
    """Return the persisted status for one target after a mutation."""

    snapshots = await load_sync_target_snapshots(session, meme_file_id)
    row = snapshots.get(target)
    if row is None:
        raise PipelineIngestError(
            f"Sync target snapshot for {target.value} disappeared after upsert on pipeline item {meme_file_id}.",
        )
    return project_sync_target_status(row, target=target)


async def load_sync_target_snapshots(
    session: AsyncSession,
    meme_file_id: uuid.UUID,
) -> dict[SyncTargetKind, MemeFileSyncTargetSnapshot]:
    result = await session.execute(
        select(MemeFileSyncTargetSnapshot).where(
            MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
        )
    )
    return {row.sync_target: row for row in result.scalars().all()}


def project_sync_target_status(
    row: MemeFileSyncTargetSnapshot,
    *,
    target: SyncTargetKind,
) -> PerTargetSyncStatus:
    return PerTargetSyncStatus(
        target=row.sync_target,
        status=row.status,
        last_event_id=row.last_event_id,
        normalized_reason=row.normalized_reason,
        last_error_text=row.last_error_text,
        last_success_at=row.last_success_at,
        last_attempt_at=row.last_attempt_at,
        attempt_count=row.attempt_count,
        last_preview=decode_sync_preview(row.last_payload_preview, target=target),
    )


def ensure_sync_replay_allowed(meme_file: MemeFile) -> None:
    """Raise when a per-target sync replay is requested before classify succeeds."""

    classify_entry = next(
        (
            entry
            for entry in meme_file.pipeline_stage_journal_entries
            if entry.stage is ContentPipelineStage.CLASSIFY
        ),
        None,
    )
    if classify_entry is None or classify_entry.status is not ContentPipelineStageStatus.SUCCEEDED:
        raise PipelineReplayNotAllowedError(
            f"Pipeline item {meme_file.id} cannot replay sync targets before classify succeeds.",
        )


def project_sync_targets(
    snapshots: Mapping[SyncTargetKind, MemeFileSyncTargetSnapshot],
) -> dict[SyncTargetKind, PerTargetSyncStatus]:
    return {target: project_sync_target_status(row, target=target) for target, row in snapshots.items()}


__all__ = [
    "PipelineSyncStatusService",
    "ensure_sync_replay_allowed",
    "load_sync_target_snapshots",
    "load_sync_target_status",
    "project_sync_target_status",
    "project_sync_targets",
    "upsert_sync_target_snapshot",
]
