# ruff: noqa: TC001,TC003
"""Durable browser-admin recovery query and mutation services."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import socket
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from memexpert.core.storage import (
    StorageObjectPresence,
    check_pipeline_object_presence,
)
from memexpert.media.contracts import SUPPORTED_MOVING_MEDIA_MIME_TYPES, WEB_VIDEO_PROFILE_ID
from memexpert.models.base import utcnow
from memexpert.models.content import (
    MemeFile,
    MemeFileSyncTargetSnapshot,
    MemeSource,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    SourceChannel,
    SourceChannelBackfillJob,
    SourceChannelPost,
)
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    RecoveryBucket,
    RecoveryCapability,
    RecoveryDeadLetterStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
    SourceChannelBackfillJobStatus,
    SourceChannelPostStatus,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.operations import (
    OperationalAuditLog,
    PipelineDeadLetter,
    RecoveryJob,
    RecoveryJobItem,
    RecoveryQuerySnapshotMember,
)
from memexpert.models.user import User
from memexpert.schemas.admin_recovery import (
    AdminSourceBackfillPageRead,
    AdminSourceBackfillRead,
    RecoveryActionRead,
    RecoveryActionRequest,
    RecoveryActionScopeRequirementsRead,
    RecoveryActiveJobRead,
    RecoveryBatchPreviewRequest,
    RecoveryCandidateRead,
    RecoveryJobItemPageRead,
    RecoveryJobItemRead,
    RecoveryJobPageRead,
    RecoveryJobRead,
    RecoveryMediaProfileRead,
    RecoveryMutationRequest,
    RecoveryQueryFilters,
    RecoveryQuerySelector,
    RecoveryRetryFailedPreviewRequest,
    RecoverySummaryRead,
    RecoveryWorkPageRead,
    RecoveryWorkRead,
)
from memexpert.services._integrity import integrity_constraint_name
from memexpert.services.recovery_versions import media_recovery_version
from memexpert.services.safe_errors import sanitize_operational_error

_WORK_SCAN_LIMIT = 10_000
_STUCK_AFTER = timedelta(minutes=15)
_BACKFILL_STUCK_AFTER = timedelta(minutes=5)
_PREVIEW_TTL = timedelta(minutes=5)
_MATERIALIZATION_LEASE = timedelta(minutes=5)
_MATERIALIZATION_PAGE_SIZE = 250
_SNAPSHOT_CAPTURE_BATCH_SIZE = 500
_RECOVERY_JOB_REQUEST_CONSTRAINT = "uq_recovery_jobs_admin_request_id"
_ACTIVE_ITEM_RESERVATION_CONSTRAINTS = frozenset(
    {
        "uq_recovery_job_items_active_stage_reservation",
        "uq_recovery_job_items_active_work_reservation",
    }
)
_TERMINAL_JOB_STATUSES = {
    RecoveryJobStatus.COMPLETED,
    RecoveryJobStatus.COMPLETED_WITH_FAILURES,
    RecoveryJobStatus.CANCELLED,
    RecoveryJobStatus.EXPIRED,
}
_TERMINAL_ITEM_STATUSES = {
    RecoveryJobItemStatus.SUCCEEDED,
    RecoveryJobItemStatus.FAILED,
    RecoveryJobItemStatus.SKIPPED_STALE,
    RecoveryJobItemStatus.SKIPPED_DEPENDENCY,
    RecoveryJobItemStatus.CANCELLED,
}
_DOWNSTREAM_STAGES = {
    ContentPipelineStage.TRANSCODE: (
        ContentPipelineStage.OCR,
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    ),
    ContentPipelineStage.OCR: (
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    ),
    ContentPipelineStage.EMBED: (
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    ),
    ContentPipelineStage.CLASSIFY: (
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    ),
    ContentPipelineStage.SYNC_QDRANT: (),
    ContentPipelineStage.SYNC_MEILI: (),
}
_PIPELINE_STAGE_ORDER = {
    stage: index
    for index, stage in enumerate(
        (
            ContentPipelineStage.INGEST,
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    )
}
_PROVIDER_SEMANTIC_MERGE_RISK = (
    "External provider output or semantic merge results may differ from the previous successful run."
)
_BUCKET_PRIORITY = {
    RecoveryBucket.DEAD_LETTERED: 0,
    RecoveryBucket.STUCK: 1,
    RecoveryBucket.RETRYABLE: 2,
    RecoveryBucket.BLOCKED: 3,
}

type ObjectPresenceChecker = Callable[[str], Awaitable[StorageObjectPresence]]


@dataclass(frozen=True, slots=True)
class _SourceObjectObservation:
    """One source-object identity and its preflight presence result."""

    key: str | None
    presence: StorageObjectPresence


class AdminRecoveryError(RuntimeError):
    """Base error for browser-admin recovery operations."""


class AdminRecoveryNotFoundError(AdminRecoveryError):
    """Raised when canonical recovery work does not exist."""


class AdminRecoveryConflictError(AdminRecoveryError):
    """Raised for stale versions, invalid actions, or completed batches."""


class AdminRecoveryStorageUnavailableError(AdminRecoveryConflictError):
    """Raised when source-object eligibility cannot be verified safely."""


class AdminRecoveryOriginalMissingError(AdminRecoveryConflictError):
    """Raised when a required source object is definitively absent."""


class AdminRecoveryService:
    """Query canonical failure state and create audited durable recovery jobs."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        object_presence_checker: ObjectPresenceChecker | None = None,
    ) -> None:
        self._session = session
        self._object_presence_checker = object_presence_checker

    async def _probe_source_object(self, key: str | None) -> _SourceObjectObservation:
        normalized_key = (key or "").strip()
        if not normalized_key:
            return _SourceObjectObservation(key=None, presence=StorageObjectPresence.MISSING)
        checker = self._object_presence_checker or check_pipeline_object_presence
        try:
            presence = await checker(normalized_key)
        except Exception:
            presence = StorageObjectPresence.UNAVAILABLE
        return _SourceObjectObservation(key=normalized_key, presence=presence)

    async def _probe_source_objects(
        self,
        keys: Sequence[str | None],
    ) -> dict[str | None, _SourceObjectObservation]:
        """Probe unique keys with bounded concurrency and bounded task fan-out."""

        unique_keys = tuple(dict.fromkeys(keys))
        observations: dict[str | None, _SourceObjectObservation] = {}
        semaphore = asyncio.Semaphore(16)

        async def probe(key: str | None) -> _SourceObjectObservation:
            async with semaphore:
                return await self._probe_source_object(key)

        for offset in range(0, len(unique_keys), 256):
            chunk = unique_keys[offset : offset + 256]
            results = await asyncio.gather(*(probe(key) for key in chunk))
            observations.update(zip(chunk, results, strict=True))
        return observations

    @staticmethod
    def _assert_source_object_usable(observation: _SourceObjectObservation) -> None:
        if observation.presence is StorageObjectPresence.MISSING:
            raise AdminRecoveryOriginalMissingError(
                "The required durable original is missing from storage."
            )
        if observation.presence is StorageObjectPresence.UNAVAILABLE:
            raise AdminRecoveryStorageUnavailableError(
                "Original storage is temporarily unavailable; retry after storage recovers."
            )

    async def _candidate_source_object_key(
        self,
        candidate: RecoveryCandidateRead,
    ) -> str | None:
        return await self._work_source_object_key(candidate.work)

    async def _work_source_object_key(self, work: RecoveryWorkRead) -> str | None:
        if work.stage is not None and work.meme_file_id is not None:
            return await self._session.scalar(
                select(MemeFile.s3_original_key).where(MemeFile.id == work.meme_file_id)
            )
        if work.kind is RecoveryWorkKind.INGEST_REQUEST:
            request_id = _parse_uuid(work.id)
            if request_id is None:
                return None
            return await self._session.scalar(
                select(PipelineIngestRequest.temp_original_object_key).where(
                    PipelineIngestRequest.id == request_id
                )
            )
        return None

    @staticmethod
    def _work_requires_source_object(work: RecoveryWorkRead) -> bool:
        return work.stage is not None or work.kind is RecoveryWorkKind.INGEST_REQUEST

    async def _work_source_observation(
        self,
        work: RecoveryWorkRead,
    ) -> _SourceObjectObservation | None:
        if not self._work_requires_source_object(work):
            return None
        return await self._probe_source_object(await self._work_source_object_key(work))

    @staticmethod
    def _source_object_blockers(
        observation: _SourceObjectObservation | None,
    ) -> list[str]:
        if observation is None or observation.presence is StorageObjectPresence.PRESENT:
            return []
        if observation.presence is StorageObjectPresence.MISSING:
            return ["The durable original is missing from storage."]
        return ["Original storage is temporarily unavailable; retry after storage recovers."]

    async def _verify_candidate_source_object(
        self,
        candidate: RecoveryCandidateRead,
        *,
        action: RecoveryCapability,
    ) -> None:
        if candidate.work.stage is None and not (
            candidate.work.kind is RecoveryWorkKind.INGEST_REQUEST
            and action is RecoveryCapability.REINSPECT_INGEST
        ):
            return
        observation = await self._probe_source_object(
            await self._candidate_source_object_key(candidate)
        )
        self._assert_source_object_usable(observation)

    async def verify_recovery_item_source_object(self, item: RecoveryJobItem) -> None:
        """Recheck the source object immediately before runtime admission."""

        if item.action in {
            RecoveryCapability.ARCHIVE_DEAD_LETTER,
            RecoveryCapability.REBUILD_OUTBOX,
            RecoveryCapability.RESUME_BACKFILL,
            RecoveryCapability.REPLAY_SOURCE_POST,
        }:
            return
        key: str | None = None
        if item.meme_file_id is not None:
            key = await self._session.scalar(
                select(MemeFile.s3_original_key).where(MemeFile.id == item.meme_file_id)
            )
        elif item.work_kind is RecoveryWorkKind.INGEST_REQUEST:
            request_id = _parse_uuid(item.work_id)
            if request_id is not None:
                key = await self._session.scalar(
                    select(PipelineIngestRequest.temp_original_object_key).where(
                        PipelineIngestRequest.id == request_id
                    )
                )
        else:
            return
        self._assert_source_object_usable(await self._probe_source_object(key))

    @staticmethod
    def _item_requires_source_object(
        *,
        action: RecoveryCapability,
        meme_file_id: uuid.UUID | None,
        work_kind: RecoveryWorkKind,
    ) -> bool:
        if action in {
            RecoveryCapability.ARCHIVE_DEAD_LETTER,
            RecoveryCapability.REBUILD_OUTBOX,
            RecoveryCapability.RESUME_BACKFILL,
            RecoveryCapability.REPLAY_SOURCE_POST,
        }:
            return False
        return meme_file_id is not None or work_kind is RecoveryWorkKind.INGEST_REQUEST

    async def _load_job_source_object_keys(
        self,
        job_id: uuid.UUID,
    ) -> dict[tuple[str, str], str | None]:
        item_rows = (
            await self._session.execute(
                select(
                    RecoveryJobItem.action,
                    RecoveryJobItem.meme_file_id,
                    RecoveryJobItem.work_kind,
                    RecoveryJobItem.work_id,
                ).where(RecoveryJobItem.recovery_job_id == job_id)
            )
        ).all()
        file_ids = {
            meme_file_id
            for action, meme_file_id, work_kind, _work_id in item_rows
            if meme_file_id is not None
            and self._item_requires_source_object(
                action=action,
                meme_file_id=meme_file_id,
                work_kind=work_kind,
            )
        }
        ingest_ids = {
            request_id
            for action, meme_file_id, work_kind, work_id in item_rows
            if self._item_requires_source_object(
                action=action,
                meme_file_id=meme_file_id,
                work_kind=work_kind,
            )
            and meme_file_id is None
            and work_kind is RecoveryWorkKind.INGEST_REQUEST
            and (request_id := _parse_uuid(work_id)) is not None
        }
        file_keys = {
            str(file_id): object_key
            for file_id, object_key in (
                await self._session.execute(
                    select(MemeFile.id, MemeFile.s3_original_key).where(MemeFile.id.in_(file_ids))
                )
            ).all()
        }
        ingest_keys = {
            str(request_id): object_key
            for request_id, object_key in (
                await self._session.execute(
                    select(
                        PipelineIngestRequest.id,
                        PipelineIngestRequest.temp_original_object_key,
                    ).where(PipelineIngestRequest.id.in_(ingest_ids))
                )
            ).all()
        }
        result: dict[tuple[str, str], str | None] = {}
        for file_id in file_ids:
            result[("file", str(file_id))] = file_keys.get(str(file_id))
        for request_id in ingest_ids:
            result[("ingest", str(request_id))] = ingest_keys.get(str(request_id))
        return result

    async def _preflight_job_source_objects(
        self,
        job_id: uuid.UUID,
    ) -> dict[tuple[str, str], _SourceObjectObservation]:
        keys_by_reference = await self._load_job_source_object_keys(job_id)
        # End the unlocked read transaction before any potentially slow network
        # calls. The locked scheduling transaction rechecks every object key.
        await self._session.commit()
        observations_by_key = await self._probe_source_objects(tuple(keys_by_reference.values()))
        observations = {
            reference: observations_by_key[key]
            for reference, key in keys_by_reference.items()
        }
        for observation in observations.values():
            self._assert_source_object_usable(observation)
        return observations

    async def _assert_job_source_objects_unchanged(
        self,
        job_id: uuid.UUID,
        observations: dict[tuple[str, str], _SourceObjectObservation],
    ) -> None:
        current = await self._load_job_source_object_keys(job_id)
        expected = {reference: observation.key for reference, observation in observations.items()}
        if current != expected:
            raise AdminRecoveryConflictError(
                "Recovery preview source media changed; create a fresh preview before scheduling."
            )

    async def get_summary(self) -> RecoverySummaryRead:
        items = await self._collect_work(snapshot_at=utcnow())
        counts = Counter(item.bucket for item in items)
        outdated_web_video_count = await self._session.scalar(
            select(func.count(MemeFile.id)).where(
                MemeFile.s3_web_video_key.is_not(None),
                or_(
                    MemeFile.web_video_profile.is_(None),
                    MemeFile.web_video_profile != WEB_VIDEO_PROFILE_ID,
                    MemeFile.web_video_verified_at.is_(None),
                    MemeFile.source_has_audio.is_(None),
                    MemeFile.web_video_has_audio.is_(None),
                    MemeFile.source_has_audio.is_distinct_from(MemeFile.web_video_has_audio),
                ),
            )
        )
        return RecoverySummaryRead(
            retryable_count=counts[RecoveryBucket.RETRYABLE],
            blocked_count=counts[RecoveryBucket.BLOCKED],
            stuck_count=counts[RecoveryBucket.STUCK],
            dead_lettered_count=counts[RecoveryBucket.DEAD_LETTERED],
            outdated_web_video_count=outdated_web_video_count or 0,
        )

    async def list_work(
        self,
        *,
        bucket: RecoveryBucket | None = None,
        kind: RecoveryWorkKind | None = None,
        source_channel_id: uuid.UUID | None = None,
        stage: ContentPipelineStage | None = None,
        reason: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RecoveryWorkPageRead:
        snapshot_at, cursor_key = _decode_cursor(cursor)
        observed_through = min(snapshot_at or utcnow(), utcnow())
        items = await self._collect_work(snapshot_at=observed_through)
        normalized_query = (query or "").strip().lower()
        normalized_reason = (reason or "").strip().lower()
        filtered = [
            item
            for item in items
            if (bucket is None or item.bucket is bucket)
            and (kind is None or item.kind is kind)
            and (source_channel_id is None or item.source_channel_id == source_channel_id)
            and (stage is None or item.stage is stage)
            and (not normalized_reason or normalized_reason in (item.error_code or item.reason or "").lower())
            and (
                not normalized_query
                or normalized_query
                in " ".join(
                    value
                    for value in (
                        item.title,
                        item.source_label or "",
                        item.post_id or "",
                        str(item.meme_file_id or ""),
                        item.id,
                    )
                    if value
                ).lower()
            )
        ]
        filtered.sort(key=_work_sort_key)
        if cursor_key is not None:
            filtered = [item for item in filtered if _work_sort_key(item) > cursor_key]
        bounded_limit = max(1, min(limit, 100))
        page = filtered[:bounded_limit]
        next_cursor = None
        if len(filtered) > bounded_limit and page:
            next_cursor = _encode_cursor(observed_through, _work_sort_key(page[-1]))
        source_keys_by_index = {
            index: await self._work_source_object_key(item)
            for index, item in enumerate(page)
            if self._work_requires_source_object(item)
        }
        observations_by_key = await self._probe_source_objects(
            tuple(source_keys_by_index.values())
        )
        page = [
            item.model_copy(
                update={
                    "actions": await self._actions_for_projected_work(
                        item,
                        source_observation=(
                            observations_by_key[source_keys_by_index[index]]
                            if index in source_keys_by_index
                            else None
                        ),
                    )
                }
            )
            for index, item in enumerate(page)
        ]
        return RecoveryWorkPageRead(items=page, next_cursor=next_cursor, snapshot_at=observed_through)

    async def get_work(self, kind: RecoveryWorkKind, work_id: str) -> RecoveryWorkRead:
        items = await self._collect_work(snapshot_at=utcnow(), target=(kind, work_id))
        item = next((candidate for candidate in items if candidate.kind is kind and candidate.id == work_id), None)
        if item is None:
            raise AdminRecoveryNotFoundError(f"Recovery work {kind.value}/{work_id} does not exist.")
        return item

    async def get_candidate(
        self,
        kind: RecoveryWorkKind,
        work_id: str,
        *,
        ignore_recovery_item_id: uuid.UUID | None = None,
        verify_source_object: bool = True,
    ) -> RecoveryCandidateRead:
        """Project every backend-owned action, including deliberately blocked actions."""

        work, meme_file = await self._load_candidate_work(kind, work_id)
        active_job = await self._load_active_job(work, ignore_recovery_item_id=ignore_recovery_item_id)
        prerequisite_blocks = await self._stage_prerequisite_blocks(work)
        terminal_descendant_stages = await self._terminal_descendant_stages(work)
        source_observation = (
            await self._work_source_observation(work)
            if verify_source_object
            else None
        )
        actions = self._candidate_actions(
            work,
            meme_file=meme_file,
            active_job=active_job,
            prerequisite_blocks=prerequisite_blocks,
            terminal_descendant_stages=terminal_descendant_stages,
            source_observation=source_observation,
        )
        warnings: list[str] = []
        risks: list[str] = []
        if work.stage is not None and _DOWNSTREAM_STAGES.get(work.stage):
            warnings.append("Stage-only replay leaves existing downstream data untouched and potentially stale.")
        if work.stage in {
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
        }:
            risks.append(_PROVIDER_SEMANTIC_MERGE_RISK)
        media_profile = None
        if meme_file is not None and meme_file.s3_web_video_key is not None:
            media_profile = RecoveryMediaProfileRead(
                profile=meme_file.web_video_profile,
                verified_at=meme_file.web_video_verified_at,
                source_has_audio=meme_file.source_has_audio,
                web_video_has_audio=meme_file.web_video_has_audio,
                outdated=_web_video_is_outdated(meme_file),
            )
        return RecoveryCandidateRead(
            work=work,
            actions=actions,
            warnings=warnings,
            risks=risks,
            media_profile=media_profile,
            active_job=active_job,
        )

    async def _actions_for_projected_work(
        self,
        work: RecoveryWorkRead,
        *,
        source_observation: _SourceObjectObservation | None = None,
    ) -> list[RecoveryActionRead]:
        """Hydrate one already-paginated row with the current backend action contract."""

        meme_file = (
            await self._session.get(MemeFile, work.meme_file_id)
            if work.meme_file_id is not None
            else None
        )
        active_job = await self._load_active_job(work)
        prerequisite_blocks = await self._stage_prerequisite_blocks(work)
        terminal_descendant_stages = await self._terminal_descendant_stages(work)
        if source_observation is None and self._work_requires_source_object(work):
            source_observation = await self._work_source_observation(work)
        return self._candidate_actions(
            work,
            meme_file=meme_file,
            active_job=active_job,
            prerequisite_blocks=prerequisite_blocks,
            terminal_descendant_stages=terminal_descendant_stages,
            source_observation=source_observation,
        )

    async def _terminal_descendant_stages(self, work: RecoveryWorkRead) -> set[ContentPipelineStage]:
        if work.meme_file_id is None or work.stage is None:
            return set()
        downstream = _DOWNSTREAM_STAGES.get(work.stage, ())
        if not downstream:
            return set()
        rows = (
            (
                await self._session.execute(
                    select(
                        PipelineStageJournal.stage,
                        PipelineStageJournal.status,
                        PipelineStageJournal.is_retryable,
                    ).where(
                        PipelineStageJournal.meme_file_id == work.meme_file_id,
                        PipelineStageJournal.stage.in_(downstream),
                    )
                )
            )
            .tuples()
            .all()
        )
        return {
            stage
            for stage, status, is_retryable in rows
            if status is ContentPipelineStageStatus.FAILED and not is_retryable
        }

    async def _stage_prerequisite_blocks(self, work: RecoveryWorkRead) -> list[str]:
        if work.meme_file_id is None or work.stage is None:
            return []
        prerequisite_by_stage = {
            ContentPipelineStage.OCR: ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.EMBED: ContentPipelineStage.OCR,
            ContentPipelineStage.CLASSIFY: ContentPipelineStage.EMBED,
            ContentPipelineStage.SYNC_QDRANT: ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_MEILI: ContentPipelineStage.CLASSIFY,
        }
        prerequisite = prerequisite_by_stage.get(work.stage)
        if prerequisite is None:
            return []
        status = await self._session.scalar(
            select(PipelineStageJournal.status).where(
                PipelineStageJournal.meme_file_id == work.meme_file_id,
                PipelineStageJournal.stage == prerequisite,
            )
        )
        if status is ContentPipelineStageStatus.SUCCEEDED:
            return []
        return [f"{work.stage.value} requires a successful {prerequisite.value} prerequisite."]

    async def perform_action(
        self,
        *,
        admin_user_id: uuid.UUID,
        kind: RecoveryWorkKind,
        work_id: str,
        payload: RecoveryActionRequest,
    ) -> RecoveryJobRead:
        """Version-fence, audit, and schedule one cookie-admin action."""

        selection: dict[str, object] = {
            "type": "explicit",
            "items": [{"kind": kind.value, "id": work_id, "version": payload.version}],
            "scope": payload.scope.value,
            "retry_limit": payload.retry_limit,
            "acknowledgements": sorted(payload.acknowledgements),
        }
        existing = await self._idempotent_job(admin_user_id, payload.request_id)
        if existing is not None:
            self._assert_idempotency_fingerprint(
                existing,
                action=payload.action,
                selection=selection,
                reason=payload.reason,
            )
            return await self._project_job(existing)

        candidate = await self.get_candidate(kind, work_id)
        if candidate.work.version != payload.version:
            raise AdminRecoveryConflictError("Recovery work changed; reload it before replaying.")
        action = next(
            (entry for entry in candidate.actions if entry.capability is payload.action),
            None,
        )
        if action is None:
            raise AdminRecoveryConflictError(f"{payload.action.value} is not defined for this work.")
        if not action.available:
            raise AdminRecoveryConflictError(
                "; ".join(action.blocked_prerequisites) or f"{payload.action.value} is not currently available."
            )
        if payload.scope not in action.scopes:
            raise AdminRecoveryConflictError(f"{payload.scope.value} is not available for {payload.action.value}.")
        scope_requirements = _action_requirements_for_scope(action, payload.scope)
        missing_acknowledgements = set(scope_requirements.required_acknowledgements) - set(
            payload.acknowledgements
        )
        if missing_acknowledgements:
            names = ", ".join(sorted(missing_acknowledgements))
            raise AdminRecoveryConflictError(f"Required acknowledgement is missing: {names}.")
        await self._verify_candidate_source_object(candidate, action=payload.action)

        now = utcnow()
        job = RecoveryJob(
            requested_by_admin_user_id=admin_user_id,
            assigned_admin_user_id=admin_user_id,
            request_id=payload.request_id,
            status=RecoveryJobStatus.QUEUED,
            action=payload.action,
            scope=payload.scope,
            retry_limit=payload.retry_limit,
            reason=payload.reason,
            selection=selection,
            selection_snapshot_at=now,
            scheduled_at=now,
        )
        job, created = await self._insert_idempotent_job(job)
        if not created:
            return await self._project_job(job)
        items = await self._build_execution_items(
            job=job,
            candidate=candidate,
            scope=payload.scope,
            retry_limit=payload.retry_limit,
            terminal_override_acknowledged="terminal_override" in payload.acknowledgements,
            reserve=True,
        )
        self._session.add_all(items)
        self._set_materialized_counts(job, items, selected_roots=1)
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=payload.request_id,
                action=payload.action.value,
                target_kind=kind.value,
                target_id=work_id,
                previous_values=candidate.work.model_dump(mode="json"),
                new_values={
                    "recovery_job_id": str(job.id),
                    "status": job.status.value,
                    "scope": payload.scope.value,
                    "retry_limit": payload.retry_limit,
                    "acknowledgements": sorted(payload.acknowledgements),
                },
                note=payload.reason,
            )
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if integrity_constraint_name(exc) in _ACTIVE_ITEM_RESERVATION_CONSTRAINTS:
                raise AdminRecoveryConflictError(
                    "This recovery work already has an active Replay & Repair job."
                ) from exc
            raise
        return await self._project_job(job)

    async def retry_work(
        self,
        *,
        admin_user_id: uuid.UUID,
        kind: RecoveryWorkKind,
        work_id: str,
        payload: RecoveryMutationRequest,
    ) -> RecoveryJobRead:
        return await self.perform_action(
            admin_user_id=admin_user_id,
            kind=kind,
            work_id=work_id,
            payload=RecoveryActionRequest(
                request_id=payload.request_id,
                version=payload.version,
                reason=payload.reason,
                action=payload.capability,
                scope=RecoveryReplayScope.STAGE_ONLY,
                retry_limit=3,
            ),
        )

    async def preview_batch(
        self,
        *,
        admin_user_id: uuid.UUID,
        payload: RecoveryBatchPreviewRequest,
    ) -> RecoveryJobRead:
        action = payload.action
        if action is None:  # pragma: no cover - enforced by the request model.
            raise AdminRecoveryConflictError("Recovery action is required.")
        selector = payload.resolved_selector()
        selection: dict[str, object] = {
            "selector": selector.model_dump(mode="json"),
            "scope": payload.scope.value,
            "retry_limit": payload.retry_limit,
            "acknowledgements": sorted(payload.acknowledgements),
        }
        existing = await self._idempotent_job(admin_user_id, payload.request_id)
        if existing is not None:
            self._assert_idempotency_fingerprint(
                existing,
                action=action,
                selection=selection,
                reason=payload.reason,
            )
            return await self._project_job(existing)

        now = utcnow()
        if isinstance(selector, RecoveryQuerySelector):
            job = RecoveryJob(
                requested_by_admin_user_id=admin_user_id,
                assigned_admin_user_id=admin_user_id,
                request_id=payload.request_id,
                status=RecoveryJobStatus.PREPARING,
                action=action,
                scope=payload.scope,
                retry_limit=payload.retry_limit,
                reason=payload.reason,
                selection=selection,
            )
            job, _created = await self._insert_idempotent_job(job)
            await self._session.commit()
            return await self._project_job(job)

        reference_keys = [(reference.kind, reference.id) for reference in selector.items]
        if len(set(reference_keys)) != len(reference_keys):
            raise AdminRecoveryConflictError("Recovery batch contains the same work item more than once.")
        job = RecoveryJob(
            requested_by_admin_user_id=admin_user_id,
            assigned_admin_user_id=admin_user_id,
            request_id=payload.request_id,
            status=RecoveryJobStatus.PREVIEW,
            action=action,
            scope=payload.scope,
            retry_limit=payload.retry_limit,
            reason=payload.reason,
            selection=selection,
            selection_snapshot_at=now,
            materialization_completed_at=now,
            expires_at=now + _PREVIEW_TTL,
        )
        job, created = await self._insert_idempotent_job(job)
        if not created:
            return await self._project_job(job)

        execution_items: list[RecoveryJobItem] = []
        execution_keys: set[tuple[str, str, str]] = set()
        for reference in selector.items:
            candidate = await self.get_candidate(reference.kind, reference.id)
            if candidate.work.version != reference.version:
                raise AdminRecoveryConflictError(
                    f"Recovery work {reference.kind.value}/{reference.id} changed; reload the selection."
                )
            candidate_action = next(
                (entry for entry in candidate.actions if entry.capability is action),
                None,
            )
            if candidate_action is None or not candidate_action.available:
                blocked = candidate_action.blocked_prerequisites if candidate_action is not None else []
                raise AdminRecoveryConflictError(
                    "; ".join(blocked) or f"{action.value} is unavailable for {reference.kind.value}/{reference.id}."
                )
            if payload.scope not in candidate_action.scopes:
                raise AdminRecoveryConflictError(
                    f"{payload.scope.value} is unavailable for {reference.kind.value}/{reference.id}."
                )
            scope_requirements = _action_requirements_for_scope(candidate_action, payload.scope)
            missing_acknowledgements = set(scope_requirements.required_acknowledgements) - set(
                payload.acknowledgements
            )
            if missing_acknowledgements:
                names = ", ".join(sorted(missing_acknowledgements))
                raise AdminRecoveryConflictError(f"Required acknowledgement is missing: {names}.")
            await self._verify_candidate_source_object(candidate, action=action)
            built = await self._build_execution_items(
                job=job,
                candidate=candidate,
                scope=payload.scope,
                retry_limit=payload.retry_limit,
                terminal_override_acknowledged="terminal_override" in payload.acknowledgements,
                reserve=False,
            )
            for item in built:
                key = _execution_item_key(item)
                if key in execution_keys:
                    raise AdminRecoveryConflictError(
                        "Selected roots expand to the same stage more than once; narrow the selection."
                    )
                execution_keys.add(key)
            execution_items.extend(built)

        self._session.add_all(execution_items)
        self._set_materialized_counts(job, execution_items, selected_roots=len(selector.items))
        await self._session.commit()
        return await self._project_job(job)

    async def schedule_batch(
        self,
        *,
        admin_user_id: uuid.UUID,
        job_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> RecoveryJobRead:
        unlocked_job = await self._get_job(job_id)
        self._assert_job_assignee(unlocked_job, admin_user_id)
        if unlocked_job.status is not RecoveryJobStatus.PREVIEW:
            if unlocked_job.status in {RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING}:
                return await self._project_job(unlocked_job)
            if unlocked_job.status is RecoveryJobStatus.PREPARING:
                raise AdminRecoveryConflictError("Recovery preview is still being prepared.")
            raise AdminRecoveryConflictError(
                f"Recovery batch is {unlocked_job.status.value}, not previewable."
            )
        if _version(unlocked_job) != version:
            raise AdminRecoveryConflictError("Recovery preview changed; reload it before scheduling.")
        source_observations = await self._preflight_job_source_objects(job_id)

        job = await self._get_job(job_id, lock=True)
        self._assert_job_assignee(job, admin_user_id)
        if job.status is not RecoveryJobStatus.PREVIEW:
            if job.status in {RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING}:
                return await self._project_job(job)
            if job.status is RecoveryJobStatus.PREPARING:
                raise AdminRecoveryConflictError("Recovery preview is still being prepared.")
            raise AdminRecoveryConflictError(f"Recovery batch is {job.status.value}, not previewable.")
        if _version(job) != version:
            raise AdminRecoveryConflictError("Recovery preview changed; reload it before scheduling.")
        if job.expires_at is not None and job.expires_at <= utcnow():
            job.status = RecoveryJobStatus.EXPIRED
            await self._session.commit()
            raise AdminRecoveryConflictError("Recovery preview expired; create a fresh preview.")

        items = (
            (
                await self._session.execute(
                    select(RecoveryJobItem)
                    .where(RecoveryJobItem.recovery_job_id == job.id)
                    .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not items:
            raise AdminRecoveryConflictError("Recovery preview contains no eligible work.")
        await self._assert_job_source_objects_unchanged(job.id, source_observations)
        acknowledgements = _job_acknowledgements(job)
        if _is_outdated_derivative_preview(job, items):
            await self._revalidate_outdated_derivative_preview(items, acknowledgements=acknowledgements)
        else:
            await self._revalidate_preview_execution(job, items, acknowledgements=acknowledgements)
        for item in items:
            item.reservation_active = True
        job.status = RecoveryJobStatus.QUEUED
        job.scheduled_at = utcnow()
        self._refresh_job_counts_from_items(job, items)
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=job.request_id,
                action="schedule_recovery_batch",
                target_kind="recovery_job",
                target_id=str(job.id),
                previous_values={"status": RecoveryJobStatus.PREVIEW.value},
                new_values={"status": RecoveryJobStatus.QUEUED.value},
                note=reason,
            )
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if integrity_constraint_name(exc) in _ACTIVE_ITEM_RESERVATION_CONSTRAINTS:
                raise AdminRecoveryConflictError(
                    "One or more recovery targets already have an active Replay & Repair job."
                ) from exc
            raise
        return await self._project_job(job)

    async def _revalidate_preview_execution(
        self,
        job: RecoveryJob,
        items: Sequence[RecoveryJobItem],
        *,
        acknowledgements: set[str],
    ) -> None:
        stored_by_key = {_execution_item_key(item): item for item in items}
        if len(stored_by_key) != len(items):
            raise AdminRecoveryConflictError(
                "Recovery preview contains duplicate execution steps; create a fresh preview."
            )
        rebuilt_by_key: dict[tuple[str, str, str], RecoveryJobItem] = {}
        for item in items:
            if item.is_root:
                candidate = await self.get_candidate(
                    item.work_kind,
                    item.work_id,
                    verify_source_object=False,
                )
                if candidate.work.version != item.expected_version:
                    raise AdminRecoveryConflictError(
                        "Recovery preview contains changed work; create a fresh preview before scheduling."
                    )
                candidate_action = next(
                    (entry for entry in candidate.actions if entry.capability is job.action),
                    None,
                )
                if candidate_action is None or not candidate_action.available:
                    blocked = candidate_action.blocked_prerequisites if candidate_action is not None else []
                    raise AdminRecoveryConflictError(
                        "; ".join(blocked)
                        or "Recovery preview contains work that is no longer eligible; create a fresh preview."
                    )
                if (job.scope or RecoveryReplayScope.STAGE_ONLY) not in candidate_action.scopes:
                    raise AdminRecoveryConflictError(
                        "Recovery preview scope is no longer available; create a fresh preview."
                    )
                scope_requirements = _action_requirements_for_scope(
                    candidate_action,
                    job.scope or RecoveryReplayScope.STAGE_ONLY,
                )
                if set(scope_requirements.required_acknowledgements) - acknowledgements:
                    raise AdminRecoveryConflictError(
                        "Recovery preview requires a new acknowledgement; create a fresh preview."
                    )
                # Rebuild without persisting to revalidate active, duplicate, and
                # terminal descendants whose state may have changed since preview.
                rebuilt = await self._build_execution_items(
                    job=job,
                    candidate=candidate,
                    scope=job.scope or RecoveryReplayScope.STAGE_ONLY,
                    retry_limit=job.retry_limit,
                    terminal_override_acknowledged="terminal_override" in acknowledgements,
                    reserve=False,
                )
                for rebuilt_item in rebuilt:
                    key = _execution_item_key(rebuilt_item)
                    if key in rebuilt_by_key:
                        raise AdminRecoveryConflictError(
                            "Recovery preview roots now overlap; create a fresh preview before scheduling."
                        )
                    rebuilt_by_key[key] = rebuilt_item

        if set(rebuilt_by_key) != set(stored_by_key):
            raise AdminRecoveryConflictError(
                "Recovery preview execution steps changed; create a fresh preview before scheduling."
            )
        stored_key_by_id = {item.id: key for key, item in stored_by_key.items()}
        rebuilt_key_by_id = {item.id: key for key, item in rebuilt_by_key.items()}
        for key, stored_item in stored_by_key.items():
            rebuilt_item = rebuilt_by_key[key]
            stored_parent_key = (
                stored_key_by_id.get(stored_item.parent_item_id) if stored_item.parent_item_id is not None else None
            )
            rebuilt_parent_key = (
                rebuilt_key_by_id.get(rebuilt_item.parent_item_id) if rebuilt_item.parent_item_id is not None else None
            )
            if (
                rebuilt_item.work_kind is not stored_item.work_kind
                or rebuilt_item.work_id != stored_item.work_id
                or rebuilt_item.expected_version != stored_item.expected_version
                or rebuilt_item.action is not stored_item.action
                or rebuilt_item.is_root is not stored_item.is_root
                or rebuilt_parent_key != stored_parent_key
            ):
                raise AdminRecoveryConflictError(
                    "Recovery preview execution state changed; create a fresh preview before scheduling."
                )

    async def _revalidate_outdated_derivative_preview(
        self,
        items: Sequence[RecoveryJobItem],
        *,
        acknowledgements: set[str],
    ) -> None:
        file_ids = {item.meme_file_id for item in items if item.meme_file_id is not None}
        files = {
            row.id: row
            for row in (
                (await self._session.execute(select(MemeFile).where(MemeFile.id.in_(file_ids)))).scalars().all()
            )
        }
        stage_rows = {
            row.meme_file_id: row
            for row in (
                (
                    await self._session.execute(
                        select(PipelineStageJournal).where(
                            PipelineStageJournal.meme_file_id.in_(file_ids),
                            PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        active_file_ids = set(
            (
                await self._session.execute(
                    select(RecoveryJobItem.meme_file_id)
                    .join(RecoveryJob, RecoveryJob.id == RecoveryJobItem.recovery_job_id)
                    .where(
                        RecoveryJobItem.meme_file_id.in_(file_ids),
                        RecoveryJobItem.stage == ContentPipelineStage.TRANSCODE,
                        RecoveryJobItem.status.in_(
                            (
                                RecoveryJobItemStatus.QUEUED,
                                RecoveryJobItemStatus.WAITING_DEPENDENCY,
                                RecoveryJobItemStatus.WAITING_CAPACITY,
                                RecoveryJobItemStatus.DISPATCHED,
                            )
                        ),
                        RecoveryJob.status.in_(
                            (
                                RecoveryJobStatus.QUEUED,
                                RecoveryJobStatus.RUNNING,
                                RecoveryJobStatus.CANCELLING,
                            )
                        ),
                    )
                )
            ).scalars()
        )
        terminal_override = "terminal_override" in acknowledgements
        for item in items:
            meme_file = files.get(item.meme_file_id)
            stage_row = stage_rows.get(item.meme_file_id)
            if meme_file is None or stage_row is None:
                raise AdminRecoveryConflictError(
                    "Recovery preview media state changed; create a fresh preview before scheduling."
                )
            if (
                not item.is_root
                or item.parent_item_id is not None
                or item.work_kind is not RecoveryWorkKind.PIPELINE_STAGE
                or item.work_id != str(stage_row.id)
                or item.action is not RecoveryCapability.REGENERATE_DERIVATIVES
                or item.expected_version != media_recovery_version(stage_row, meme_file)
                or item.meme_file_id in active_file_ids
                or not (meme_file.s3_original_key or "").strip()
                or not _is_moving_media(meme_file)
                or not _web_video_is_outdated(meme_file)
                or stage_row.status
                in {
                    ContentPipelineStageStatus.PENDING,
                    ContentPipelineStageStatus.PROCESSING,
                    ContentPipelineStageStatus.DUPLICATE,
                }
                or (
                    stage_row.status is ContentPipelineStageStatus.FAILED
                    and not stage_row.is_retryable
                    and not terminal_override
                )
            ):
                raise AdminRecoveryConflictError(
                    "Recovery preview media state changed; create a fresh preview before scheduling."
                )

    async def cancel_batch(
        self,
        *,
        admin_user_id: uuid.UUID,
        job_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> RecoveryJobRead:
        job = await self._get_job(job_id, lock=True)
        self._assert_job_assignee(job, admin_user_id)
        if job.status is RecoveryJobStatus.CANCELLED:
            return await self._project_job(job)
        if job.status in {
            RecoveryJobStatus.COMPLETED,
            RecoveryJobStatus.COMPLETED_WITH_FAILURES,
            RecoveryJobStatus.EXPIRED,
        }:
            raise AdminRecoveryConflictError(f"Recovery batch is already {job.status.value}.")
        if _version(job) != version:
            raise AdminRecoveryConflictError("Recovery batch changed; reload it before cancelling.")
        items = (
            (
                await self._session.execute(
                    select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == job.id).with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for item in items:
            if item.status in {
                RecoveryJobItemStatus.QUEUED,
                RecoveryJobItemStatus.WAITING_DEPENDENCY,
                RecoveryJobItemStatus.WAITING_CAPACITY,
            }:
                item.status = RecoveryJobItemStatus.CANCELLED
                item.finished_at = utcnow()
                item.reservation_active = False
        has_dispatched = any(item.status is RecoveryJobItemStatus.DISPATCHED for item in items)
        job.status = RecoveryJobStatus.CANCELLING if has_dispatched else RecoveryJobStatus.CANCELLED
        job.cancelled_at = utcnow()
        if not has_dispatched:
            job.completed_at = utcnow()
        self._refresh_job_counts_from_items(job, items)
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=job.request_id,
                action="cancel_recovery_batch",
                target_kind="recovery_job",
                target_id=str(job.id),
                previous_values={},
                new_values={"status": job.status.value},
                note=reason,
            )
        )
        await self._session.commit()
        return await self._project_job(job)

    async def get_job(self, *, admin_user_id: uuid.UUID, job_id: uuid.UUID) -> RecoveryJobRead:
        del admin_user_id  # All admins may inspect operational jobs.
        job = await self._get_job(job_id)
        return await self._project_job(job)

    async def list_jobs(
        self,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> RecoveryJobPageRead:
        cursor_key = _decode_job_cursor(cursor)
        stmt = select(RecoveryJob)
        if status is not None:
            try:
                parsed_status = RecoveryJobStatus(status)
            except ValueError as exc:
                raise AdminRecoveryConflictError("Recovery job status filter is invalid.") from exc
            stmt = stmt.where(RecoveryJob.status == parsed_status)
        if cursor_key is not None:
            created_at, job_id = cursor_key
            stmt = stmt.where(
                or_(
                    RecoveryJob.created_at < created_at,
                    and_(RecoveryJob.created_at == created_at, RecoveryJob.id < job_id),
                )
            )
        bounded_limit = max(1, min(limit, 100))
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(RecoveryJob.created_at.desc(), RecoveryJob.id.desc()).limit(bounded_limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = rows[:bounded_limit]
        next_cursor = None
        if len(rows) > bounded_limit and page:
            next_cursor = _encode_job_cursor(page[-1].created_at, page[-1].id)
        return RecoveryJobPageRead(
            items=[await self._project_job(job, include_items=False) for job in page],
            next_cursor=next_cursor,
        )

    async def list_job_items(
        self,
        *,
        job_id: uuid.UUID,
        status: str | None,
        cursor: str | None,
        limit: int,
        failed_first: bool,
    ) -> RecoveryJobItemPageRead:
        _ = await self._get_job(job_id)
        cursor_key = _decode_item_cursor(cursor)
        priority = case(
            (RecoveryJobItem.status == RecoveryJobItemStatus.FAILED, 0),
            (RecoveryJobItem.status == RecoveryJobItemStatus.SKIPPED_STALE, 1),
            (RecoveryJobItem.status == RecoveryJobItemStatus.SKIPPED_DEPENDENCY, 2),
            else_=3,
        )
        stmt = select(RecoveryJobItem, priority.label("priority")).where(RecoveryJobItem.recovery_job_id == job_id)
        if status is not None:
            try:
                parsed_status = RecoveryJobItemStatus(status)
            except ValueError as exc:
                raise AdminRecoveryConflictError("Recovery item status filter is invalid.") from exc
            stmt = stmt.where(RecoveryJobItem.status == parsed_status)
        if cursor_key is not None:
            cursor_priority, created_at, item_id = cursor_key
            if failed_first:
                stmt = stmt.where(
                    or_(
                        priority > cursor_priority,
                        and_(priority == cursor_priority, RecoveryJobItem.created_at > created_at),
                        and_(
                            priority == cursor_priority,
                            RecoveryJobItem.created_at == created_at,
                            RecoveryJobItem.id > item_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        RecoveryJobItem.created_at > created_at,
                        and_(RecoveryJobItem.created_at == created_at, RecoveryJobItem.id > item_id),
                    )
                )
        bounded_limit = max(1, min(limit, 100))
        ordering = (
            (priority.asc(), RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
            if failed_first
            else (RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
        )
        rows = (await self._session.execute(stmt.order_by(*ordering).limit(bounded_limit + 1))).all()
        page = rows[:bounded_limit]
        next_cursor = None
        if len(rows) > bounded_limit and page:
            last_item, last_priority = page[-1]
            next_cursor = _encode_item_cursor(int(last_priority), last_item.created_at, last_item.id)
        return RecoveryJobItemPageRead(
            items=[self._project_job_item(item) for item, _priority in page],
            next_cursor=next_cursor,
        )

    async def handoff_job(
        self,
        *,
        admin_user_id: uuid.UUID,
        job_id: uuid.UUID,
        assigned_admin_user_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> RecoveryJobRead:
        job = await self._get_job(job_id, lock=True)
        if _version(job) != version:
            raise AdminRecoveryConflictError("Recovery job changed; reload it before handoff.")
        assignee = await self._session.get(User, assigned_admin_user_id)
        if assignee is None or not assignee.is_admin:
            raise AdminRecoveryConflictError("Recovery jobs may only be assigned to an administrator.")
        previous_assignee = job.assigned_admin_user_id
        job.assigned_admin_user_id = assigned_admin_user_id
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=job.request_id,
                action="handoff_recovery_job",
                target_kind="recovery_job",
                target_id=str(job.id),
                previous_values={"assigned_admin_user_id": str(previous_assignee) if previous_assignee else None},
                new_values={"assigned_admin_user_id": str(assigned_admin_user_id)},
                note=reason,
            )
        )
        await self._session.commit()
        return await self._project_job(job)

    async def preview_failed_items(
        self,
        *,
        admin_user_id: uuid.UUID,
        job_id: uuid.UUID,
        payload: RecoveryRetryFailedPreviewRequest,
    ) -> RecoveryJobRead:
        source_job = await self._get_job(job_id)
        if _version(source_job) != payload.version:
            raise AdminRecoveryConflictError("Recovery job changed; reload it before retrying failures.")
        failed_items = (
            (
                await self._session.execute(
                    select(RecoveryJobItem)
                    .where(
                        RecoveryJobItem.recovery_job_id == job_id,
                        RecoveryJobItem.status == RecoveryJobItemStatus.FAILED,
                    )
                    .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not failed_items:
            raise AdminRecoveryConflictError("This recovery job has no failed items to retry.")
        selection: dict[str, object] = {
            "selector": {
                "type": "explicit",
                "items": [
                    {"kind": item.work_kind.value, "id": item.work_id, "version": item.canonical_version or ""}
                    for item in failed_items
                ],
            },
            "source_recovery_job_id": str(job_id),
            "scope": (source_job.scope or RecoveryReplayScope.STAGE_ONLY).value,
            "retry_limit": payload.retry_limit or source_job.retry_limit,
            "acknowledgements": [],
        }
        existing = await self._idempotent_job(admin_user_id, payload.request_id)
        if existing is not None:
            self._assert_idempotency_fingerprint(
                existing,
                action=source_job.action,
                selection=selection,
                reason=payload.reason,
            )
            return await self._project_job(existing)
        now = utcnow()
        retry_job = RecoveryJob(
            requested_by_admin_user_id=admin_user_id,
            assigned_admin_user_id=admin_user_id,
            source_recovery_job_id=source_job.id,
            request_id=payload.request_id,
            status=RecoveryJobStatus.PREVIEW,
            action=source_job.action,
            scope=source_job.scope or RecoveryReplayScope.STAGE_ONLY,
            retry_limit=payload.retry_limit or source_job.retry_limit,
            reason=payload.reason,
            selection=selection,
            selection_snapshot_at=now,
            materialization_completed_at=now,
            expires_at=now + _PREVIEW_TTL,
        )
        retry_job, created = await self._insert_idempotent_job(retry_job)
        if not created:
            return await self._project_job(retry_job)
        retry_items: list[RecoveryJobItem] = []
        exclusions: Counter[str] = Counter()
        execution_keys: set[tuple[str, str, str]] = set()
        for source_item in failed_items:
            try:
                candidate = await self.get_candidate(source_item.work_kind, source_item.work_id)
                await self._verify_candidate_source_object(candidate, action=source_item.action)
                action = next(
                    (entry for entry in candidate.actions if entry.capability is source_item.action),
                    None,
                )
                if action is None:
                    exclusions["no_longer_eligible"] += 1
                    continue
                scope_requirements = _action_requirements_for_scope(
                    action,
                    source_job.scope or RecoveryReplayScope.STAGE_ONLY,
                )
                if scope_requirements.required_acknowledgements:
                    exclusions["acknowledgement_required"] += 1
                    continue
                if not action.available:
                    exclusions["no_longer_eligible"] += 1
                    continue
                built = await self._build_execution_items(
                    job=retry_job,
                    candidate=candidate,
                    scope=source_job.scope or RecoveryReplayScope.STAGE_ONLY,
                    retry_limit=payload.retry_limit or source_job.retry_limit,
                    terminal_override_acknowledged=False,
                    reserve=False,
                    source_item_id=source_item.id,
                )
                built_keys = {_execution_item_key(item) for item in built}
                if built_keys & execution_keys:
                    exclusions["overlapping_stage_selection"] += 1
                    continue
                execution_keys.update(built_keys)
                retry_items.extend(built)
            except AdminRecoveryStorageUnavailableError:
                raise
            except AdminRecoveryOriginalMissingError:
                exclusions["missing_original"] += 1
            except AdminRecoveryNotFoundError, AdminRecoveryConflictError:
                exclusions["canonical_state_changed"] += 1
        if not retry_items:
            raise AdminRecoveryConflictError("Failed recovery items are no longer eligible for replay.")
        self._session.add_all(retry_items)
        retry_job.exclusions_by_reason = dict(exclusions)
        retry_job.excluded_count = sum(exclusions.values())
        self._set_materialized_counts(
            retry_job,
            retry_items,
            selected_roots=sum(item.is_root for item in retry_items),
        )
        await self._session.commit()
        return await self._project_job(retry_job)

    async def _capture_query_snapshot_members(
        self,
        *,
        job: RecoveryJob,
        selector: RecoveryQuerySelector,
    ) -> tuple[datetime, int]:
        """Capture exact query roots under one repeatable-read database snapshot."""

        bind = self._session.bind
        if bind is None:  # pragma: no cover - application sessions are always bound.
            raise AdminRecoveryConflictError("Recovery snapshot storage is unavailable.")

        # The client timestamp describes the page the operator was viewing, but
        # current-row tables cannot reconstruct arbitrary historical state. The
        # durable selection snapshot is therefore the server-owned MVCC snapshot
        # taken atomically with this capture.
        async with AsyncSession(bind=bind, expire_on_commit=False) as snapshot_session:
            _ = await snapshot_session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"},
            )
            captured_at = await snapshot_session.scalar(select(func.transaction_timestamp()))
            if captured_at is None:  # pragma: no cover - PostgreSQL always returns a timestamp.
                raise AdminRecoveryConflictError("Recovery snapshot timestamp is unavailable.")

            captured_count = 0
            batch: list[RecoveryQuerySnapshotMember] = []
            if selector.filters.outdated_web_video:
                active_file_ids = set(
                    (
                        await snapshot_session.execute(
                            select(RecoveryJobItem.meme_file_id).where(
                                RecoveryJobItem.stage == ContentPipelineStage.TRANSCODE,
                                RecoveryJobItem.reservation_active.is_(True),
                            )
                        )
                    ).scalars()
                )
                rows = await snapshot_session.stream(
                    select(MemeFile, PipelineStageJournal)
                    .outerjoin(
                        PipelineStageJournal,
                        and_(
                            PipelineStageJournal.meme_file_id == MemeFile.id,
                            PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
                        ),
                    )
                    .where(MemeFile.s3_web_video_key.is_not(None))
                    .order_by(MemeFile.id.asc())
                    .execution_options(yield_per=_SNAPSHOT_CAPTURE_BATCH_SIZE)
                )
                async for meme_file, stage in rows:
                    if not _web_video_is_outdated(meme_file):
                        continue
                    batch.append(
                        RecoveryQuerySnapshotMember(
                            id=uuid.uuid7(),
                            recovery_job_id=job.id,
                            root_key=f"outdated_video:{meme_file.id}",
                            work_kind=RecoveryWorkKind.PIPELINE_STAGE,
                            work_id=str(stage.id) if stage is not None else None,
                            meme_file_id=meme_file.id,
                            stage=ContentPipelineStage.TRANSCODE,
                            captured_version=(
                                media_recovery_version(stage, meme_file)
                                if stage is not None
                                else _outdated_file_snapshot_version(meme_file)
                            ),
                            captured_context_fingerprint=_stable_snapshot_fingerprint(
                                {
                                    "active_reservation": meme_file.id in active_file_ids,
                                    "stage_present": stage is not None,
                                }
                            ),
                            is_outdated_video=True,
                        )
                    )
                    if len(batch) >= _SNAPSHOT_CAPTURE_BATCH_SIZE:
                        self._session.add_all(batch)
                        await self._session.flush()
                        captured_count += len(batch)
                        batch.clear()
            elif selector.filters.successful_stage:
                selected_stage = selector.filters.stage
                if selected_stage is None or selected_stage is ContentPipelineStage.INGEST:
                    raise AdminRecoveryConflictError(
                        "Successful-stage recovery requires one non-Ingest stage."
                    )
                snapshot_service = AdminRecoveryService(snapshot_session)
                if selected_stage in {
                    ContentPipelineStage.SYNC_QDRANT,
                    ContentPipelineStage.SYNC_MEILI,
                }:
                    selected_target = (
                        SyncTargetKind.QDRANT
                        if selected_stage is ContentPipelineStage.SYNC_QDRANT
                        else SyncTargetKind.MEILISEARCH
                    )
                    rows = await snapshot_session.stream_scalars(
                        select(MemeFileSyncTargetSnapshot)
                        .where(
                            MemeFileSyncTargetSnapshot.sync_target == selected_target,
                            MemeFileSyncTargetSnapshot.status == SyncTargetStatus.SYNCED,
                        )
                        .order_by(MemeFileSyncTargetSnapshot.id.asc())
                        .execution_options(yield_per=_SNAPSHOT_CAPTURE_BATCH_SIZE)
                    )
                    async for row in rows:
                        full_candidate = await snapshot_service.get_candidate(
                            RecoveryWorkKind.SYNC_TARGET,
                            str(row.id),
                            verify_source_object=False,
                        )
                        context_fingerprint, _planned_items = (
                            await snapshot_service._query_member_context_fingerprint(
                                job=job,
                                candidate=full_candidate,
                            )
                        )
                        batch.append(
                            RecoveryQuerySnapshotMember(
                                id=uuid.uuid7(),
                                recovery_job_id=job.id,
                                root_key=f"successful_stage:sync_target:{row.id}",
                                work_kind=RecoveryWorkKind.SYNC_TARGET,
                                work_id=str(row.id),
                                meme_file_id=row.meme_file_id,
                                stage=selected_stage,
                                captured_version=full_candidate.work.version,
                                captured_context_fingerprint=context_fingerprint,
                                is_outdated_video=False,
                            )
                        )
                        if len(batch) >= _SNAPSHOT_CAPTURE_BATCH_SIZE:
                            self._session.add_all(batch)
                            await self._session.flush()
                            captured_count += len(batch)
                            batch.clear()
                else:
                    rows = await snapshot_session.stream_scalars(
                        select(PipelineStageJournal)
                        .where(
                            PipelineStageJournal.stage == selected_stage,
                            PipelineStageJournal.status == ContentPipelineStageStatus.SUCCEEDED,
                        )
                        .order_by(PipelineStageJournal.id.asc())
                        .execution_options(yield_per=_SNAPSHOT_CAPTURE_BATCH_SIZE)
                    )
                    async for row in rows:
                        full_candidate = await snapshot_service.get_candidate(
                            RecoveryWorkKind.PIPELINE_STAGE,
                            str(row.id),
                            verify_source_object=False,
                        )
                        context_fingerprint, _planned_items = (
                            await snapshot_service._query_member_context_fingerprint(
                                job=job,
                                candidate=full_candidate,
                            )
                        )
                        batch.append(
                            RecoveryQuerySnapshotMember(
                                id=uuid.uuid7(),
                                recovery_job_id=job.id,
                                root_key=f"successful_stage:pipeline_stage:{row.id}",
                                work_kind=RecoveryWorkKind.PIPELINE_STAGE,
                                work_id=str(row.id),
                                meme_file_id=row.meme_file_id,
                                stage=selected_stage,
                                captured_version=full_candidate.work.version,
                                captured_context_fingerprint=context_fingerprint,
                                is_outdated_video=False,
                            )
                        )
                        if len(batch) >= _SNAPSHOT_CAPTURE_BATCH_SIZE:
                            self._session.add_all(batch)
                            await self._session.flush()
                            captured_count += len(batch)
                            batch.clear()
            else:
                snapshot_service = AdminRecoveryService(snapshot_session)
                work = await snapshot_service._collect_work(
                    snapshot_at=captured_at,
                    scan_limit=None,
                )
                for candidate in sorted(work, key=_query_snapshot_work_sort_key):
                    if not _matches_filters(candidate, selector.filters):
                        continue
                    full_candidate = await snapshot_service.get_candidate(
                        candidate.kind,
                        candidate.id,
                        verify_source_object=False,
                    )
                    context_fingerprint, _planned_items = (
                        await snapshot_service._query_member_context_fingerprint(
                            job=job,
                            candidate=full_candidate,
                        )
                    )
                    batch.append(
                        RecoveryQuerySnapshotMember(
                            id=uuid.uuid7(),
                            recovery_job_id=job.id,
                            root_key=f"work:{candidate.kind.value}:{candidate.id}",
                            work_kind=candidate.kind,
                            work_id=candidate.id,
                            meme_file_id=full_candidate.work.meme_file_id,
                            stage=full_candidate.work.stage,
                            captured_version=full_candidate.work.version,
                            captured_context_fingerprint=context_fingerprint,
                            is_outdated_video=False,
                        )
                    )
                    if len(batch) >= _SNAPSHOT_CAPTURE_BATCH_SIZE:
                        self._session.add_all(batch)
                        await self._session.flush()
                        captured_count += len(batch)
                        batch.clear()

            if batch:
                self._session.add_all(batch)
                await self._session.flush()
                captured_count += len(batch)

            await snapshot_session.rollback()
        return captured_at, captured_count

    async def _query_member_context_fingerprint(
        self,
        *,
        job: RecoveryJob,
        candidate: RecoveryCandidateRead,
    ) -> tuple[str, list[RecoveryJobItem] | None]:
        """Fingerprint eligibility, reservations, and the exact replay topology."""

        scope = job.scope or RecoveryReplayScope.STAGE_ONLY
        acknowledgements = _job_acknowledgements(job)
        action = next(
            (entry for entry in candidate.actions if entry.capability is job.action),
            None,
        )
        action_payload: dict[str, object] | None = None
        requirements: RecoveryActionScopeRequirementsRead | None = None
        if action is not None:
            requirements = _action_requirements_for_scope(action, scope)
            action_payload = {
                "available": action.available,
                "scopes": sorted(value.value for value in action.scopes),
                "blocked_prerequisites": sorted(action.blocked_prerequisites),
                "required_acknowledgements": sorted(requirements.required_acknowledgements),
            }

        stage_context: list[dict[str, object]] = []
        active_stage_reservations: list[dict[str, str]] = []
        work = candidate.work
        source_object_key = (
            await self._work_source_object_key(work)
            if self._work_requires_source_object(work)
            else None
        )
        if work.meme_file_id is not None and work.stage is not None:
            stages = [work.stage]
            if scope is RecoveryReplayScope.STAGE_AND_DEPENDENTS:
                stages.extend(_DOWNSTREAM_STAGES.get(work.stage, ()))
            rows = {
                row.stage: row
                for row in (
                    (
                        await self._session.execute(
                            select(PipelineStageJournal).where(
                                PipelineStageJournal.meme_file_id == work.meme_file_id,
                                PipelineStageJournal.stage.in_(stages),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            for stage in stages:
                row = rows.get(stage)
                stage_context.append(
                    {
                        "stage": stage.value,
                        "row_id": str(row.id) if row is not None else None,
                        "version": _version(row, row.last_event_id) if row is not None else "missing",
                        "status": row.status.value if row is not None else None,
                        "is_retryable": row.is_retryable if row is not None else None,
                    }
                )
            active_stage_reservations = [
                {
                    "stage": row.stage.value,
                    "work_kind": row.work_kind.value,
                    "work_id": row.work_id,
                }
                for row in (
                    (
                        await self._session.execute(
                            select(RecoveryJobItem).where(
                                RecoveryJobItem.meme_file_id == work.meme_file_id,
                                RecoveryJobItem.stage.in_(stages),
                                RecoveryJobItem.reservation_active.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if row.stage is not None
            ]
            active_stage_reservations.sort(
                key=lambda value: (value["stage"], value["work_kind"], value["work_id"])
            )

        planned_items: list[RecoveryJobItem] | None = None
        execution_payload: list[dict[str, object]] | None = None
        execution_error: str | None = None
        if (
            action is not None
            and action.available
            and scope in action.scopes
            and requirements is not None
            and not (set(requirements.required_acknowledgements) - acknowledgements)
        ):
            try:
                planned_items = await self._build_execution_items(
                    job=job,
                    candidate=candidate,
                    scope=scope,
                    retry_limit=job.retry_limit,
                    terminal_override_acknowledged="terminal_override" in acknowledgements,
                    reserve=False,
                )
            except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
                execution_error = exc.__class__.__name__
            else:
                key_by_id = {item.id: _execution_item_key(item) for item in planned_items}
                execution_payload = [
                    {
                        "key": _execution_item_key(item),
                        "parent": key_by_id.get(item.parent_item_id),
                        "work_kind": item.work_kind.value,
                        "work_id": item.work_id,
                        "action": item.action.value,
                        "expected_version": item.expected_version,
                        "is_root": item.is_root,
                    }
                    for item in planned_items
                ]

        payload: dict[str, object] = {
            "schema": "query-member-context-v1",
            "action": action_payload,
            "scope": scope.value,
            "source_object_key": source_object_key,
            "stage_context": stage_context,
            "active_stage_reservations": active_stage_reservations,
            "execution": execution_payload,
            "execution_error": execution_error,
        }
        return _stable_snapshot_fingerprint(payload), planned_items

    async def _load_snapshot_member_source_keys(
        self,
        members: Sequence[RecoveryQuerySnapshotMember],
    ) -> dict[uuid.UUID, str | None]:
        file_ids = {member.meme_file_id for member in members if member.meme_file_id is not None}
        ingest_member_ids = {
            member.id: request_id
            for member in members
            if member.meme_file_id is None
            and member.work_kind is RecoveryWorkKind.INGEST_REQUEST
            and member.work_id is not None
            and (request_id := _parse_uuid(member.work_id)) is not None
        }
        file_keys = {
            file_id: object_key
            for file_id, object_key in (
                await self._session.execute(
                    select(MemeFile.id, MemeFile.s3_original_key).where(MemeFile.id.in_(file_ids))
                )
            ).all()
        }
        ingest_keys = {
            request_id: object_key
            for request_id, object_key in (
                await self._session.execute(
                    select(
                        PipelineIngestRequest.id,
                        PipelineIngestRequest.temp_original_object_key,
                    ).where(PipelineIngestRequest.id.in_(set(ingest_member_ids.values())))
                )
            ).all()
        }
        keys: dict[uuid.UUID, str | None] = {
            member.id: file_keys.get(member.meme_file_id)
            for member in members
            if member.meme_file_id is not None
        }
        keys.update(
            {
                member_id: ingest_keys.get(request_id)
                for member_id, request_id in ingest_member_ids.items()
            }
        )
        return keys

    async def _preflight_snapshot_page_source_objects(
        self,
        *,
        job: RecoveryJob,
        page_size: int,
    ) -> dict[uuid.UUID, _SourceObjectObservation]:
        page = await self._query_snapshot_member_page(job=job, page_size=page_size)
        keys_by_member = await self._load_snapshot_member_source_keys(page)
        # The durable lease, rather than a database row lock, owns this page
        # while HEAD requests are in flight.
        await self._session.commit()
        observations_by_key = await self._probe_source_objects(tuple(keys_by_member.values()))
        observations = {
            member_id: observations_by_key[key]
            for member_id, key in keys_by_member.items()
        }
        if any(
            observation.presence is StorageObjectPresence.UNAVAILABLE
            for observation in observations.values()
        ):
            raise AdminRecoveryStorageUnavailableError(
                "Original storage is temporarily unavailable; materialization will retry later."
            )
        return observations

    async def _release_materialization_lease(
        self,
        *,
        job_id: uuid.UUID,
        lease_owner: str,
        lease_generation: int,
    ) -> None:
        job = await self._get_job(job_id, lock=True)
        if (
            job.status is RecoveryJobStatus.PREPARING
            and job.materialization_lease_owner == lease_owner
            and job.materialization_lease_generation == lease_generation
        ):
            job.materialization_lease_owner = None
            job.materialization_lease_at = None
            await self._session.commit()
            return
        await self._session.commit()

    async def materialize_next_preparing_job(self, *, page_size: int = _MATERIALIZATION_PAGE_SIZE) -> bool:
        """Materialize one restart-safe keyset page for an uncapped query preview."""

        now = utcnow()
        lease_stale_before = now - _MATERIALIZATION_LEASE
        job = await self._session.scalar(
            select(RecoveryJob)
            .where(
                RecoveryJob.status == RecoveryJobStatus.PREPARING,
                or_(
                    RecoveryJob.materialization_lease_at.is_(None),
                    RecoveryJob.materialization_lease_at < lease_stale_before,
                ),
            )
            .order_by(RecoveryJob.created_at.asc(), RecoveryJob.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return False
        lease_owner = f"{socket.gethostname()}:{uuid.uuid4()}"
        job.materialization_lease_owner = lease_owner
        job.materialization_lease_at = now
        job.materialization_lease_generation += 1
        lease_generation = job.materialization_lease_generation
        await self._session.commit()

        # Reload under the durable lease. The cursor and inserted page advance in
        # the same transaction, so a crashed materializer safely repeats a page.
        job = await self._get_job(job.id, lock=True)
        if (
            job.status is not RecoveryJobStatus.PREPARING
            or job.materialization_lease_owner != lease_owner
            or job.materialization_lease_generation != lease_generation
        ):
            await self._session.commit()
            return True
        raw_selector = job.selection.get("selector")
        if not isinstance(raw_selector, dict) or raw_selector.get("type") != "query":
            await self._fail_preparation(job, "invalid_query_selector")
            return True
        try:
            selector = RecoveryQuerySelector.model_validate(raw_selector)
        except ValueError:
            await self._fail_preparation(job, "invalid_query_selector")
            return True

        if (
            selector.filters.successful_stage
            and job.action is not RecoveryCapability.REPLAY_STAGE
        ):
            await self._fail_preparation(job, "selector_action_mismatch")
            return True

        if job.selection_snapshot_at is None:
            captured_at, _captured_count = await self._capture_query_snapshot_members(
                job=job,
                selector=selector,
            )
            job.selection_snapshot_at = captured_at
            job.materialization_lease_owner = None
            job.materialization_lease_at = None
            await self._session.commit()
            return True

        bounded_page_size = max(1, min(page_size, 1000))
        job_id = job.id
        # Release the job row lock before probing storage. The durable lease
        # prevents another materializer from owning the page, and its generation
        # is fenced again before any cursor or count changes are persisted.
        await self._session.commit()
        unlocked_job = await self._get_job(job_id)
        if (
            unlocked_job.status is not RecoveryJobStatus.PREPARING
            or unlocked_job.materialization_lease_owner != lease_owner
            or unlocked_job.materialization_lease_generation != lease_generation
        ):
            await self._session.commit()
            return True
        try:
            source_observations = await self._preflight_snapshot_page_source_objects(
                job=unlocked_job,
                page_size=bounded_page_size,
            )
        except AdminRecoveryStorageUnavailableError:
            await self._release_materialization_lease(
                job_id=job_id,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
            return False

        job = await self._get_job(job_id, lock=True)
        if (
            job.status is not RecoveryJobStatus.PREPARING
            or job.materialization_lease_owner != lease_owner
            or job.materialization_lease_generation != lease_generation
        ):
            await self._session.commit()
            return True
        raw_selector = job.selection.get("selector")
        try:
            selector = RecoveryQuerySelector.model_validate(raw_selector)
        except ValueError:
            await self._fail_preparation(job, "invalid_query_selector")
            return True
        if selector.filters.outdated_web_video:
            complete = await self._materialize_outdated_video_page(
                job,
                filters=selector.filters,
                page_size=bounded_page_size,
                source_observations=source_observations,
            )
        elif selector.filters.successful_stage:
            complete = await self._materialize_successful_stage_page(
                job,
                filters=selector.filters,
                page_size=bounded_page_size,
                source_observations=source_observations,
            )
        else:
            complete = await self._materialize_attention_page(
                job,
                filters=selector.filters,
                page_size=bounded_page_size,
                source_observations=source_observations,
            )
        job.materialization_lease_owner = None
        job.materialization_lease_at = None
        if complete and job.status is RecoveryJobStatus.PREPARING:
            await self._finish_materialization(job)
        await self._session.commit()
        return True

    async def _materialize_outdated_video_page(
        self,
        job: RecoveryJob,
        *,
        filters: RecoveryQueryFilters,
        page_size: int,
        source_observations: dict[uuid.UUID, _SourceObjectObservation],
    ) -> bool:
        if job.action is not RecoveryCapability.REGENERATE_DERIVATIVES:
            await self._fail_preparation(job, "selector_action_mismatch")
            return True
        if (job.scope or RecoveryReplayScope.STAGE_ONLY) is not RecoveryReplayScope.STAGE_ONLY:
            await self._fail_preparation(job, "selector_scope_mismatch")
            return True
        if not filters.outdated_web_video:
            await self._fail_preparation(job, "selector_filter_mismatch")
            return True
        try:
            page = await self._query_snapshot_member_page(job=job, page_size=page_size)
        except ValueError:
            await self._fail_preparation(job, "invalid_materialization_cursor")
            return True
        if any(not member.is_outdated_video for member in page):
            await self._fail_preparation(job, "snapshot_membership_mismatch")
            return True
        job.preparation_scanned_count += len(page)
        if page:
            job.materialization_cursor = str(page[-1].id)

        file_ids = {member.meme_file_id for member in page if member.meme_file_id is not None}
        files = {
            file.id: file
            for file in (
                (await self._session.execute(select(MemeFile).where(MemeFile.id.in_(file_ids))))
                .scalars()
                .all()
            )
        }
        transcode_row_list = (
            (
                await self._session.execute(
                    select(PipelineStageJournal).where(
                        PipelineStageJournal.meme_file_id.in_(file_ids),
                        PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
                    )
                )
            )
            .scalars()
            .all()
        )
        transcode_rows = {
            row.id: row
            for row in transcode_row_list
        }
        transcode_rows_by_file = {row.meme_file_id: row for row in transcode_row_list}
        exclusions = Counter(job.exclusions_by_reason)
        new_items: list[RecoveryJobItem] = []
        acknowledgements = _job_acknowledgements(job)
        active_file_ids = set(
            (
                await self._session.execute(
                    select(RecoveryJobItem.meme_file_id).where(
                        RecoveryJobItem.meme_file_id.in_(file_ids),
                        RecoveryJobItem.stage == ContentPipelineStage.TRANSCODE,
                        RecoveryJobItem.reservation_active.is_(True),
                    )
                )
            ).scalars()
        )
        for member in page:
            if member.meme_file_id is None:
                exclusions["canonical_state_changed"] += 1
                continue
            file = files.get(member.meme_file_id)
            if file is None or member.captured_version is None:
                exclusions["canonical_state_changed"] += 1
                continue
            source_observation = source_observations.get(member.id)
            current_source_key = (file.s3_original_key or "").strip() or None
            if source_observation is None or source_observation.key != current_source_key:
                exclusions["canonical_state_changed"] += 1
                continue
            if source_observation.presence is StorageObjectPresence.MISSING:
                exclusions["missing_original"] += 1
                continue
            if member.work_id is None:
                current_stage = transcode_rows_by_file.get(file.id)
                if member.captured_version != _outdated_file_snapshot_version(file):
                    exclusions["canonical_state_changed"] += 1
                    continue
                row = None
            else:
                try:
                    stage_id = uuid.UUID(member.work_id)
                except ValueError:
                    exclusions["canonical_state_changed"] += 1
                    continue
                row = transcode_rows.get(stage_id)
                if (
                    row is None
                    or row.meme_file_id != file.id
                    or member.captured_version != media_recovery_version(row, file)
                ):
                    exclusions["canonical_state_changed"] += 1
                    continue
                current_stage = row
            current_context = _stable_snapshot_fingerprint(
                {
                    "active_reservation": file.id in active_file_ids,
                    "stage_present": current_stage is not None,
                }
            )
            if member.captured_context_fingerprint != current_context:
                exclusions["canonical_state_changed"] += 1
                continue
            if not _web_video_is_outdated(file):
                exclusions["canonical_state_changed"] += 1
                continue
            if not _has_moving_media_mime(file):
                exclusions["unsupported_media_type"] += 1
                continue
            if row is None:
                exclusions["missing_transcode_stage"] += 1
                continue
            if file.id in active_file_ids:
                exclusions["active_recovery_job"] += 1
                continue
            if row.status in {
                ContentPipelineStageStatus.PENDING,
                ContentPipelineStageStatus.PROCESSING,
                ContentPipelineStageStatus.DUPLICATE,
            }:
                exclusions["ineligible"] += 1
                continue
            terminal = row.status is ContentPipelineStageStatus.FAILED and not row.is_retryable
            if terminal and "terminal_override" not in acknowledgements:
                exclusions["acknowledgement_required"] += 1
                continue
            new_items.append(
                RecoveryJobItem(
                    recovery_job_id=job.id,
                    meme_file_id=file.id,
                    stage=ContentPipelineStage.TRANSCODE,
                    is_root=True,
                    work_kind=RecoveryWorkKind.PIPELINE_STAGE,
                    work_id=str(row.id),
                    action=RecoveryCapability.REGENERATE_DERIVATIVES,
                    expected_version=member.captured_version,
                    retry_limit=job.retry_limit,
                    preserve_ready=file.status is ContentProcessingStatus.READY,
                    suppress_fanout=True,
                    terminal_override_acknowledged=terminal,
                    reservation_active=False,
                    status=RecoveryJobItemStatus.QUEUED,
                )
            )

        self._session.add_all(new_items)
        job.exclusions_by_reason = dict(exclusions)
        job.excluded_count = sum(exclusions.values())
        job.selected_root_count += sum(item.is_root for item in new_items)
        job.expanded_execution_count += len(new_items)
        job.total_count = job.expanded_execution_count
        self._refresh_job_counts_from_items(job, new_items, incremental=True)
        return len(page) < page_size

    async def _materialize_attention_page(
        self,
        job: RecoveryJob,
        *,
        filters: RecoveryQueryFilters,
        page_size: int,
        source_observations: dict[uuid.UUID, _SourceObjectObservation],
    ) -> bool:
        if filters.outdated_web_video or filters.successful_stage:
            await self._fail_preparation(job, "selector_filter_mismatch")
            return True
        return await self._materialize_candidate_snapshot_page(
            job,
            page_size=page_size,
            source_observations=source_observations,
            successful_stage=None,
        )

    async def _materialize_successful_stage_page(
        self,
        job: RecoveryJob,
        *,
        filters: RecoveryQueryFilters,
        page_size: int,
        source_observations: dict[uuid.UUID, _SourceObjectObservation],
    ) -> bool:
        selected_stage = filters.stage
        if job.action is not RecoveryCapability.REPLAY_STAGE:
            await self._fail_preparation(job, "selector_action_mismatch")
            return True
        if (
            not filters.successful_stage
            or selected_stage is None
            or selected_stage is ContentPipelineStage.INGEST
        ):
            await self._fail_preparation(job, "selector_filter_mismatch")
            return True
        return await self._materialize_candidate_snapshot_page(
            job,
            page_size=page_size,
            source_observations=source_observations,
            successful_stage=selected_stage,
        )

    async def _materialize_candidate_snapshot_page(
        self,
        job: RecoveryJob,
        *,
        page_size: int,
        source_observations: dict[uuid.UUID, _SourceObjectObservation],
        successful_stage: ContentPipelineStage | None,
    ) -> bool:
        try:
            page = await self._query_snapshot_member_page(job=job, page_size=page_size)
        except ValueError:
            await self._fail_preparation(job, "invalid_materialization_cursor")
            return True
        if any(member.is_outdated_video for member in page):
            await self._fail_preparation(job, "snapshot_membership_mismatch")
            return True
        if successful_stage is not None:
            expected_kind = (
                RecoveryWorkKind.SYNC_TARGET
                if successful_stage
                in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}
                else RecoveryWorkKind.PIPELINE_STAGE
            )
            if any(
                member.stage is not successful_stage or member.work_kind is not expected_kind
                for member in page
            ):
                await self._fail_preparation(job, "snapshot_membership_mismatch")
                return True
        current_source_keys = await self._load_snapshot_member_source_keys(page)
        job.preparation_scanned_count += len(page)
        if page:
            job.materialization_cursor = str(page[-1].id)
        exclusions = Counter(job.exclusions_by_reason)
        new_items: list[RecoveryJobItem] = []
        acknowledgements = _job_acknowledgements(job)
        existing_items = (
            (await self._session.execute(select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == job.id)))
            .scalars()
            .all()
        )
        execution_keys = {_execution_item_key(item) for item in existing_items}
        for member in page:
            try:
                source_observation = source_observations.get(member.id)
                if source_observation is not None:
                    current_source_key = (current_source_keys.get(member.id) or "").strip() or None
                    if source_observation.key != current_source_key:
                        exclusions["canonical_state_changed"] += 1
                        continue
                    if source_observation.presence is StorageObjectPresence.MISSING:
                        exclusions["missing_original"] += 1
                        continue
                if member.work_id is None or member.captured_version is None:
                    exclusions["canonical_state_changed"] += 1
                    continue
                candidate = await self.get_candidate(
                    member.work_kind,
                    member.work_id,
                    verify_source_object=False,
                )
                if successful_stage is not None and (
                    candidate.work.stage is not successful_stage
                    or candidate.work.status
                    != (
                        SyncTargetStatus.SYNCED.value
                        if successful_stage
                        in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}
                        else ContentPipelineStageStatus.SUCCEEDED.value
                    )
                ):
                    exclusions["canonical_state_changed"] += 1
                    continue
                if candidate.work.version != member.captured_version:
                    exclusions["canonical_state_changed"] += 1
                    continue
                context_fingerprint, planned_items = await self._query_member_context_fingerprint(
                    job=job,
                    candidate=candidate,
                )
                if member.captured_context_fingerprint != context_fingerprint:
                    exclusions["canonical_state_changed"] += 1
                    continue
                root_execution_key = (
                    ("stage", str(member.meme_file_id), member.stage.value)
                    if member.meme_file_id is not None and member.stage is not None
                    else ("work", member.work_kind.value, member.work_id)
                )
                if root_execution_key in execution_keys:
                    exclusions["overlapping_stage_selection"] += 1
                    continue
                action = next(
                    (entry for entry in candidate.actions if entry.capability is job.action and entry.available),
                    None,
                )
                if action is None or (job.scope or RecoveryReplayScope.STAGE_ONLY) not in action.scopes:
                    exclusions["ineligible"] += 1
                    continue
                scope_requirements = _action_requirements_for_scope(
                    action,
                    job.scope or RecoveryReplayScope.STAGE_ONLY,
                )
                if set(scope_requirements.required_acknowledgements) - acknowledgements:
                    exclusions["acknowledgement_required"] += 1
                    continue
                built = planned_items
                if built is None:
                    built = await self._build_execution_items(
                        job=job,
                        candidate=candidate,
                        scope=job.scope or RecoveryReplayScope.STAGE_ONLY,
                        retry_limit=job.retry_limit,
                        terminal_override_acknowledged="terminal_override" in acknowledgements,
                        reserve=False,
                    )
                built_keys = {_execution_item_key(item) for item in built}
                if built_keys & execution_keys:
                    exclusions["overlapping_stage_selection"] += 1
                    continue
                execution_keys.update(built_keys)
                new_items.extend(built)
            except AdminRecoveryNotFoundError, AdminRecoveryConflictError:
                exclusions["canonical_state_changed"] += 1
        self._session.add_all(new_items)
        job.exclusions_by_reason = dict(exclusions)
        job.excluded_count = sum(exclusions.values())
        job.selected_root_count += sum(item.is_root for item in new_items)
        job.expanded_execution_count += len(new_items)
        job.total_count = job.expanded_execution_count
        self._refresh_job_counts_from_items(job, new_items, incremental=True)
        return len(page) < page_size

    async def _query_snapshot_member_page(
        self,
        *,
        job: RecoveryJob,
        page_size: int,
    ) -> list[RecoveryQuerySnapshotMember]:
        stmt = select(RecoveryQuerySnapshotMember).where(
            RecoveryQuerySnapshotMember.recovery_job_id == job.id,
        )
        if job.materialization_cursor:
            try:
                cursor_id = uuid.UUID(job.materialization_cursor)
            except ValueError as exc:
                raise ValueError("invalid materialization cursor") from exc
            stmt = stmt.where(RecoveryQuerySnapshotMember.id > cursor_id)
        return list(
            (
                await self._session.execute(
                    stmt.order_by(RecoveryQuerySnapshotMember.id.asc()).limit(page_size)
                )
            )
            .scalars()
            .all()
        )

    async def _finish_materialization(self, job: RecoveryJob) -> None:
        now = utcnow()
        job.status = RecoveryJobStatus.PREVIEW
        job.materialization_completed_at = now
        job.expires_at = now + _PREVIEW_TTL
        job.materialization_cursor = None

    async def _fail_preparation(self, job: RecoveryJob, reason: str) -> None:
        job.status = RecoveryJobStatus.COMPLETED_WITH_FAILURES
        job.failed_count = max(job.failed_count, 1)
        job.excluded_count += 1
        exclusions = Counter(job.exclusions_by_reason)
        exclusions[reason] += 1
        job.exclusions_by_reason = dict(exclusions)
        job.materialization_completed_at = utcnow()
        job.completed_at = utcnow()
        job.materialization_lease_owner = None
        job.materialization_lease_at = None
        await self._session.commit()

    async def list_backfills(self, source_channel_id: uuid.UUID) -> AdminSourceBackfillPageRead:
        rows = (
            (
                await self._session.execute(
                    select(SourceChannelBackfillJob)
                    .options(
                        selectinload(SourceChannelBackfillJob.source_channel).selectinload(
                            SourceChannel.telegram_session
                        )
                    )
                    .where(SourceChannelBackfillJob.source_channel_id == source_channel_id)
                    .order_by(SourceChannelBackfillJob.created_at.desc(), SourceChannelBackfillJob.id.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return AdminSourceBackfillPageRead(items=[_project_backfill(row) for row in rows])

    async def resume_backfill(
        self,
        *,
        admin_user_id: uuid.UUID,
        source_channel_id: uuid.UUID,
        job_id: uuid.UUID,
        request_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> RecoveryJobRead:
        row = await self._session.get(SourceChannelBackfillJob, job_id)
        if row is None or row.source_channel_id != source_channel_id:
            raise AdminRecoveryNotFoundError(f"Backfill job {job_id} does not exist for this source.")
        return await self.retry_work(
            admin_user_id=admin_user_id,
            kind=RecoveryWorkKind.BACKFILL,
            work_id=str(job_id),
            payload=RecoveryMutationRequest(
                request_id=request_id,
                version=version,
                reason=reason,
                capability=RecoveryCapability.RESUME_BACKFILL,
            ),
        )

    async def replay_source_post(
        self,
        *,
        admin_user_id: uuid.UUID,
        source_channel_id: uuid.UUID,
        post_id: str,
        request_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> RecoveryJobRead:
        row = await self._session.scalar(
            select(SourceChannelPost).where(
                SourceChannelPost.source_channel_id == source_channel_id,
                SourceChannelPost.post_id == post_id,
            )
        )
        if row is None:
            raise AdminRecoveryNotFoundError(f"Source post {post_id} does not exist for this source.")
        return await self.retry_work(
            admin_user_id=admin_user_id,
            kind=RecoveryWorkKind.SOURCE_POST,
            work_id=str(row.id),
            payload=RecoveryMutationRequest(
                request_id=request_id,
                version=version,
                reason=reason,
                capability=RecoveryCapability.REPLAY_SOURCE_POST,
            ),
        )

    async def _collect_work(
        self,
        *,
        snapshot_at: datetime,
        scan_limit: int | None = _WORK_SCAN_LIMIT,
        target: tuple[RecoveryWorkKind, str] | None = None,
    ) -> list[RecoveryWorkRead]:
        stuck_before = snapshot_at - _STUCK_AFTER
        backfill_stuck_before = snapshot_at - _BACKFILL_STUCK_AFTER
        work: dict[tuple[RecoveryWorkKind, str], RecoveryWorkRead] = {}
        target_id = _parse_uuid(target[1]) if target is not None else None
        if target is not None and target_id is None:
            return []

        async def load_rows(kind: RecoveryWorkKind, id_column, stmt):
            if target is not None:
                if target[0] is not kind:
                    return []
                stmt = stmt.where(id_column == target_id)
            return (await self._session.execute(stmt)).scalars().all()

        backfills = await load_rows(
            RecoveryWorkKind.BACKFILL,
            SourceChannelBackfillJob.id,
            select(SourceChannelBackfillJob)
            .options(selectinload(SourceChannelBackfillJob.source_channel))
            .where(
                SourceChannelBackfillJob.updated_at <= snapshot_at,
                or_(
                    SourceChannelBackfillJob.status.in_(
                        (
                            SourceChannelBackfillJobStatus.FAILED,
                            SourceChannelBackfillJobStatus.WAITING_RETRY,
                            SourceChannelBackfillJobStatus.WAITING_CAPACITY,
                        )
                    ),
                    and_(
                        SourceChannelBackfillJob.status == SourceChannelBackfillJobStatus.RUNNING,
                        SourceChannelBackfillJob.last_progress_at < backfill_stuck_before,
                    ),
                ),
            )
            .order_by(SourceChannelBackfillJob.updated_at.desc())
            .limit(scan_limit),
        )
        for row in backfills:
            stuck = row.status is SourceChannelBackfillJobStatus.RUNNING
            waiting = row.status in {
                SourceChannelBackfillJobStatus.WAITING_RETRY,
                SourceChannelBackfillJobStatus.WAITING_CAPACITY,
            }
            capabilities = (
                [RecoveryCapability.RESUME_BACKFILL]
                if row.status is SourceChannelBackfillJobStatus.FAILED and row.is_retryable
                else []
            )
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.BACKFILL,
                id=str(row.id),
                bucket=RecoveryBucket.STUCK
                if stuck
                else RecoveryBucket.RETRYABLE
                if row.is_retryable
                else RecoveryBucket.BLOCKED,
                title=f"Backfill for {row.source_channel.title}",
                source_label=_source_label(row.source_channel),
                source_channel_id=row.source_channel_id,
                post_id=row.failed_post_id,
                status=row.status.value,
                reason=row.last_error_code,
                safe_error=_safe_error(row.last_error_text),
                error_code=row.last_error_code,
                is_retryable=row.is_retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.updated_at,
                next_attempt_at=row.next_attempt_at,
                version=_version(row),
                capabilities=capabilities,
                blocked_reason=("Already waiting for automatic retry or capacity." if waiting else None),
                details={
                    "requested_count": row.requested_message_count,
                    "scanned_count": row.scanned_message_count,
                    "quarantined_count": row.quarantined_message_count,
                    "cursor_post_id": row.cursor_post_id,
                },
            )
            work[(item.kind, item.id)] = item

        posts = await load_rows(
            RecoveryWorkKind.SOURCE_POST,
            SourceChannelPost.id,
            select(SourceChannelPost)
            .options(selectinload(SourceChannelPost.source_channel))
            .where(
                SourceChannelPost.status == SourceChannelPostStatus.FAILED,
                SourceChannelPost.updated_at <= snapshot_at,
            )
            .order_by(SourceChannelPost.updated_at.desc())
            .limit(scan_limit),
        )
        for row in posts:
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.SOURCE_POST,
                id=str(row.id),
                bucket=RecoveryBucket.RETRYABLE if row.is_retryable else RecoveryBucket.BLOCKED,
                title=f"Telegram post {row.post_id}",
                source_label=_source_label(row.source_channel),
                source_channel_id=row.source_channel_id,
                post_id=row.post_id,
                status=row.status.value,
                reason=row.last_error_code,
                safe_error=_safe_error(row.last_error_text),
                error_code=row.last_error_code,
                is_retryable=row.is_retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.updated_at,
                next_attempt_at=row.next_attempt_at,
                version=_version(row),
                capabilities=[RecoveryCapability.REPLAY_SOURCE_POST] if row.is_retryable else [],
                blocked_reason=None
                if row.is_retryable
                else "The crawler classified this post failure as non-retryable.",
            )
            work[(item.kind, item.id)] = item

        ingest_rows = await load_rows(
            RecoveryWorkKind.INGEST_REQUEST,
            PipelineIngestRequest.id,
            select(PipelineIngestRequest)
            .where(
                PipelineIngestRequest.updated_at <= snapshot_at,
                or_(
                    PipelineIngestRequest.status.in_(
                        (
                            PipelineIngestRequestStatus.FAILED_INVALID_MEDIA,
                            PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH,
                            PipelineIngestRequestStatus.PUBLISH_FAILED,
                        )
                    ),
                    and_(
                        PipelineIngestRequest.status.in_(
                            (
                                PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
                                PipelineIngestRequestStatus.MEDIA_INSPECTING,
                            )
                        ),
                        PipelineIngestRequest.updated_at < stuck_before,
                    ),
                ),
            )
            .order_by(PipelineIngestRequest.updated_at.desc())
            .limit(scan_limit),
        )
        for row in ingest_rows:
            stuck = row.status in {
                PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
                PipelineIngestRequestStatus.MEDIA_INSPECTING,
            }
            retryable = (
                row.temp_original_object_key is not None and row.status is PipelineIngestRequestStatus.PUBLISH_FAILED
            )
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.INGEST_REQUEST,
                id=str(row.id),
                bucket=RecoveryBucket.STUCK
                if stuck
                else RecoveryBucket.RETRYABLE
                if retryable
                else RecoveryBucket.BLOCKED,
                title=f"Ingest request {row.post_id}",
                post_id=row.post_id,
                meme_file_id=row.materialized_meme_file_id,
                status=row.status.value,
                reason=row.failure_code,
                safe_error=_safe_error(row.failure_detail),
                error_code=row.failure_code,
                is_retryable=retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.updated_at,
                version=_version(row),
                capabilities=[RecoveryCapability.REINSPECT_INGEST] if retryable else [],
                blocked_reason=None if retryable else "No retained retryable temporary object is available.",
            )
            work[(item.kind, item.id)] = item

        stages = await load_rows(
            RecoveryWorkKind.PIPELINE_STAGE,
            PipelineStageJournal.id,
            select(PipelineStageJournal)
            .where(
                PipelineStageJournal.updated_at <= snapshot_at,
                PipelineStageJournal.stage.not_in((ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI)),
                or_(
                    PipelineStageJournal.status == ContentPipelineStageStatus.FAILED,
                    and_(
                        PipelineStageJournal.status.in_(
                            (ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING)
                        ),
                        PipelineStageJournal.updated_at < stuck_before,
                    ),
                ),
            )
            .order_by(PipelineStageJournal.updated_at.desc())
            .limit(scan_limit),
        )
        file_sources = await self._load_file_sources({row.meme_file_id for row in stages})
        transcode_file_ids = {
            row.meme_file_id for row in stages if row.stage is ContentPipelineStage.TRANSCODE
        }
        stage_files = (
            {
                file.id: file
                for file in (
                    (
                        await self._session.execute(
                            select(MemeFile).where(MemeFile.id.in_(transcode_file_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            if transcode_file_ids
            else {}
        )
        for row in stages:
            stuck = row.status in {ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING}
            retryable = row.is_retryable and row.status is ContentPipelineStageStatus.FAILED
            source = file_sources.get(row.meme_file_id)
            meme_file = stage_files.get(row.meme_file_id)
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.PIPELINE_STAGE,
                id=str(row.id),
                bucket=RecoveryBucket.STUCK
                if stuck
                else RecoveryBucket.RETRYABLE
                if retryable
                else RecoveryBucket.BLOCKED,
                title=f"{row.stage.value.replace('_', ' ').title()} for {row.meme_file_id}",
                source_label=source[1] if source else None,
                source_channel_id=source[0] if source else None,
                meme_file_id=row.meme_file_id,
                stage=row.stage,
                status=row.status.value,
                reason=row.normalized_reason,
                safe_error=_safe_error(row.last_error_text),
                error_code=row.normalized_reason,
                is_retryable=retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.updated_at,
                next_attempt_at=row.retry_after,
                version=(
                    media_recovery_version(row, meme_file)
                    if (
                        row.stage is ContentPipelineStage.TRANSCODE
                        and meme_file is not None
                        and _is_moving_media(meme_file)
                    )
                    else _version(row, row.last_event_id)
                ),
                capabilities=[RecoveryCapability.RETRY_STAGE] if retryable else [],
                blocked_reason=None
                if retryable
                else "Stuck work must first be reclaimed by the automatic reconciler."
                if stuck
                else "The stage failure is non-retryable.",
                details={"event_id": str(row.last_event_id) if row.last_event_id is not None else None},
            )
            work[(item.kind, item.id)] = item

        sync_rows = await load_rows(
            RecoveryWorkKind.SYNC_TARGET,
            MemeFileSyncTargetSnapshot.id,
            select(MemeFileSyncTargetSnapshot)
            .where(
                MemeFileSyncTargetSnapshot.updated_at <= snapshot_at,
                or_(
                    MemeFileSyncTargetSnapshot.status == SyncTargetStatus.FAILED,
                    and_(
                        MemeFileSyncTargetSnapshot.status.in_((SyncTargetStatus.PENDING, SyncTargetStatus.PROCESSING)),
                        MemeFileSyncTargetSnapshot.updated_at < stuck_before,
                    ),
                ),
            )
            .order_by(MemeFileSyncTargetSnapshot.updated_at.desc())
            .limit(scan_limit),
        )
        sync_sources = await self._load_file_sources({row.meme_file_id for row in sync_rows})
        malformed_reasons = {"sync_qdrant_malformed_payload", "sync_meili_malformed_payload"}
        for row in sync_rows:
            stuck = row.status in {SyncTargetStatus.PENDING, SyncTargetStatus.PROCESSING}
            retryable = row.status is SyncTargetStatus.FAILED and row.normalized_reason not in malformed_reasons
            source = sync_sources.get(row.meme_file_id)
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.SYNC_TARGET,
                id=str(row.id),
                bucket=RecoveryBucket.STUCK
                if stuck
                else RecoveryBucket.RETRYABLE
                if retryable
                else RecoveryBucket.BLOCKED,
                title=f"{row.sync_target.value.title()} sync for {row.meme_file_id}",
                source_label=source[1] if source else None,
                source_channel_id=source[0] if source else None,
                meme_file_id=row.meme_file_id,
                stage=(
                    ContentPipelineStage.SYNC_QDRANT
                    if row.sync_target is SyncTargetKind.QDRANT
                    else ContentPipelineStage.SYNC_MEILI
                ),
                target=row.sync_target,
                status=row.status.value,
                reason=row.normalized_reason,
                safe_error=_safe_error(row.last_error_text),
                error_code=row.normalized_reason,
                is_retryable=retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.last_attempt_at or row.updated_at,
                version=_version(row, row.last_event_id),
                capabilities=[RecoveryCapability.RESYNC_TARGET] if retryable else [],
                blocked_reason=None
                if retryable
                else "Stuck sync work is awaiting automatic reclaim."
                if stuck
                else "The sync payload is malformed and cannot be replayed safely.",
                details={"event_id": str(row.last_event_id) if row.last_event_id is not None else None},
            )
            work[(item.kind, item.id)] = item

        outbox_rows = await load_rows(
            RecoveryWorkKind.OUTBOX,
            RabbitMQOutboxMessage.id,
            select(RabbitMQOutboxMessage)
            .where(
                RabbitMQOutboxMessage.updated_at <= snapshot_at,
                or_(
                    RabbitMQOutboxMessage.status == RabbitMQOutboxMessageStatus.FAILED,
                    and_(
                        RabbitMQOutboxMessage.status == RabbitMQOutboxMessageStatus.PUBLISHING,
                        RabbitMQOutboxMessage.updated_at < stuck_before,
                    ),
                ),
            )
            .order_by(RabbitMQOutboxMessage.updated_at.desc())
            .limit(scan_limit),
        )
        for row in outbox_rows:
            stuck = row.status is RabbitMQOutboxMessageStatus.PUBLISHING
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.OUTBOX,
                id=str(row.id),
                bucket=RecoveryBucket.STUCK if stuck else RecoveryBucket.RETRYABLE,
                title=f"Outbox event {row.event_type}",
                status=row.status.value,
                reason="outbox_publish_failed",
                safe_error=_safe_error(row.last_error_text),
                error_code="outbox_publish_failed",
                is_retryable=True,
                attempt_count=row.attempt_count,
                occurred_at=row.updated_at,
                next_attempt_at=row.next_retry_at,
                version=_version(row),
                capabilities=[RecoveryCapability.REBUILD_OUTBOX] if not stuck else [],
                blocked_reason="Publishing lease is stale and awaits automatic reclaim." if stuck else None,
            )
            work[(item.kind, item.id)] = item

        dead_letter_stmt = select(PipelineDeadLetter).where(
            PipelineDeadLetter.status.in_(
                (RecoveryDeadLetterStatus.UNRESOLVED, RecoveryDeadLetterStatus.RECOVERY_QUEUED)
            ),
            PipelineDeadLetter.updated_at <= snapshot_at,
        )
        if target is not None:
            if target[0] is RecoveryWorkKind.DEAD_LETTER:
                dead_letter_stmt = dead_letter_stmt.where(PipelineDeadLetter.id == target_id)
            else:
                dead_letter_stmt = dead_letter_stmt.where(
                    PipelineDeadLetter.work_kind == target[0],
                    PipelineDeadLetter.work_id == target[1],
                )
        dead_letters = (
            (
                await self._session.execute(
                    dead_letter_stmt.order_by(PipelineDeadLetter.updated_at.desc()).limit(scan_limit)
                )
            )
            .scalars()
            .all()
        )
        for row in dead_letters:
            if row.work_kind is not None and row.work_id:
                linked_key = (row.work_kind, row.work_id)
                existing = work.get(linked_key)
            else:
                linked_key = None
                existing = None
            if existing is not None and linked_key is not None and _dead_letter_matches_work(row, existing):
                work[linked_key] = existing.model_copy(
                    update={
                        "bucket": RecoveryBucket.DEAD_LETTERED,
                        "reason": row.normalized_reason,
                        "error_code": row.normalized_reason,
                        "version": f"{existing.version}:dead-letter:{_version(row)}",
                        "capabilities": [RecoveryCapability.RECOVER_DEAD_LETTER],
                        "blocked_reason": None,
                        "details": {**existing.details, "dead_letter_id": str(row.id), "death_count": row.death_count},
                    }
                )
                continue
            item = RecoveryWorkRead(
                kind=RecoveryWorkKind.DEAD_LETTER,
                id=str(row.id),
                bucket=RecoveryBucket.DEAD_LETTERED,
                title=f"Dead-lettered {row.event_type or 'unparseable event'}",
                status=row.status.value,
                reason=row.normalized_reason,
                error_code=row.normalized_reason,
                is_retryable=False,
                attempt_count=row.death_count,
                occurred_at=row.updated_at,
                version=_version(row),
                capabilities=[RecoveryCapability.ARCHIVE_DEAD_LETTER],
                blocked_reason=(
                    "The linked canonical work is no longer in the same recoverable generation; "
                    "archive this dead letter."
                    if row.work_kind is not None and row.work_id is not None
                    else "This dead letter could not be linked safely to canonical work."
                ),
                details={"event_type": row.event_type, "work_kind": row.work_kind, "work_id": row.work_id},
            )
            work[(item.kind, item.id)] = item

        return list(work.values())

    async def _load_candidate_work(
        self,
        kind: RecoveryWorkKind,
        work_id: str,
    ) -> tuple[RecoveryWorkRead, MemeFile | None]:
        if kind is RecoveryWorkKind.PIPELINE_STAGE:
            row_id = _parse_uuid(work_id)
            row = await self._session.get(PipelineStageJournal, row_id) if row_id is not None else None
            if row is None:
                raise AdminRecoveryNotFoundError(f"Recovery work {kind.value}/{work_id} does not exist.")
            meme_file = await self._session.get(MemeFile, row.meme_file_id)
            if meme_file is None:
                raise AdminRecoveryNotFoundError(f"Pipeline file {row.meme_file_id} does not exist.")
            retryable = row.status is ContentPipelineStageStatus.FAILED and row.is_retryable
            active = row.status in {ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING}
            bucket = (
                RecoveryBucket.STUCK if active else RecoveryBucket.RETRYABLE if retryable else RecoveryBucket.BLOCKED
            )
            capabilities = [RecoveryCapability.RETRY_STAGE] if retryable else []
            projected = RecoveryWorkRead(
                kind=kind,
                id=str(row.id),
                bucket=bucket,
                title=f"{row.stage.value.replace('_', ' ').title()} for {row.meme_file_id}",
                meme_file_id=row.meme_file_id,
                stage=row.stage,
                status=row.status.value,
                reason=row.normalized_reason,
                safe_error=_safe_error(row.last_error_text),
                error_code=row.normalized_reason,
                is_retryable=retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.updated_at,
                next_attempt_at=row.retry_after,
                version=(
                    media_recovery_version(row, meme_file)
                    if row.stage is ContentPipelineStage.TRANSCODE and _is_moving_media(meme_file)
                    else _version(row, row.last_event_id)
                ),
                capabilities=capabilities,
                blocked_reason=(
                    "Pending or processing work must finish or be reclaimed before replay." if active else None
                ),
                details={
                    "event_id": str(row.last_event_id) if row.last_event_id else None,
                    "mime_type": meme_file.mime_type,
                    "file_status": meme_file.status.value,
                },
            )
            return await self._with_linked_dead_letter(projected), meme_file
        if kind is RecoveryWorkKind.SYNC_TARGET:
            row_id = _parse_uuid(work_id)
            row = await self._session.get(MemeFileSyncTargetSnapshot, row_id) if row_id is not None else None
            if row is None:
                raise AdminRecoveryNotFoundError(f"Recovery work {kind.value}/{work_id} does not exist.")
            meme_file = await self._session.get(MemeFile, row.meme_file_id)
            if meme_file is None:
                raise AdminRecoveryNotFoundError(f"Pipeline file {row.meme_file_id} does not exist.")
            stage = (
                ContentPipelineStage.SYNC_QDRANT
                if row.sync_target is SyncTargetKind.QDRANT
                else ContentPipelineStage.SYNC_MEILI
            )
            active = row.status in {SyncTargetStatus.PENDING, SyncTargetStatus.PROCESSING}
            retryable = row.status is SyncTargetStatus.FAILED and row.normalized_reason not in {
                "sync_qdrant_malformed_payload",
                "sync_meili_malformed_payload",
            }
            projected = RecoveryWorkRead(
                kind=kind,
                id=str(row.id),
                bucket=(
                    RecoveryBucket.STUCK
                    if active
                    else RecoveryBucket.RETRYABLE
                    if retryable
                    else RecoveryBucket.BLOCKED
                ),
                title=f"{row.sync_target.value.title()} sync for {row.meme_file_id}",
                meme_file_id=row.meme_file_id,
                stage=stage,
                target=row.sync_target,
                status=row.status.value,
                reason=row.normalized_reason,
                safe_error=_safe_error(row.last_error_text),
                error_code=row.normalized_reason,
                is_retryable=retryable,
                attempt_count=row.attempt_count,
                occurred_at=row.last_attempt_at or row.updated_at,
                version=_version(row, row.last_event_id),
                capabilities=[RecoveryCapability.RESYNC_TARGET] if retryable else [],
                blocked_reason=(
                    "Pending or processing work must finish or be reclaimed before replay." if active else None
                ),
                details={"event_id": str(row.last_event_id) if row.last_event_id else None},
            )
            return await self._with_linked_dead_letter(projected), meme_file

        work = await self.get_work(kind, work_id)
        meme_file = await self._session.get(MemeFile, work.meme_file_id) if work.meme_file_id else None
        return work, meme_file

    async def _with_linked_dead_letter(self, work: RecoveryWorkRead) -> RecoveryWorkRead:
        dead_letter = await self._session.scalar(
            select(PipelineDeadLetter)
            .where(
                PipelineDeadLetter.work_kind == work.kind,
                PipelineDeadLetter.work_id == work.id,
                PipelineDeadLetter.status.in_(
                    (RecoveryDeadLetterStatus.UNRESOLVED, RecoveryDeadLetterStatus.RECOVERY_QUEUED)
                ),
            )
            .order_by(PipelineDeadLetter.created_at.asc())
            .limit(1)
        )
        if dead_letter is None or not _dead_letter_matches_work(dead_letter, work):
            return work
        return work.model_copy(
            update={
                "bucket": RecoveryBucket.DEAD_LETTERED,
                "reason": dead_letter.normalized_reason,
                "error_code": dead_letter.normalized_reason,
                "version": f"{work.version}:dead-letter:{_version(dead_letter)}",
                "capabilities": [RecoveryCapability.RECOVER_DEAD_LETTER],
                "blocked_reason": None,
                "details": {
                    **work.details,
                    "dead_letter_id": str(dead_letter.id),
                    "death_count": dead_letter.death_count,
                },
            }
        )

    async def _load_active_job(
        self,
        work: RecoveryWorkRead,
        *,
        ignore_recovery_item_id: uuid.UUID | None = None,
    ) -> RecoveryActiveJobRead | None:
        predicate = and_(
            RecoveryJobItem.work_kind == work.kind,
            RecoveryJobItem.work_id == work.id,
        )
        if work.meme_file_id is not None and work.stage is not None:
            predicate = or_(
                predicate,
                and_(
                    RecoveryJobItem.meme_file_id == work.meme_file_id,
                    RecoveryJobItem.stage == work.stage,
                ),
            )
        stmt = (
            select(RecoveryJob, RecoveryJobItem)
            .join(RecoveryJobItem, RecoveryJobItem.recovery_job_id == RecoveryJob.id)
            .where(
                predicate,
                RecoveryJobItem.status.in_(
                    (
                        RecoveryJobItemStatus.QUEUED,
                        RecoveryJobItemStatus.WAITING_DEPENDENCY,
                        RecoveryJobItemStatus.WAITING_CAPACITY,
                        RecoveryJobItemStatus.DISPATCHED,
                    )
                ),
                RecoveryJob.status.in_(
                    (
                        RecoveryJobStatus.QUEUED,
                        RecoveryJobStatus.RUNNING,
                        RecoveryJobStatus.CANCELLING,
                    )
                ),
            )
        )
        if ignore_recovery_item_id is not None:
            stmt = stmt.where(RecoveryJobItem.id != ignore_recovery_item_id)
        row = (await self._session.execute(stmt.order_by(RecoveryJob.created_at.asc()).limit(1))).first()
        if row is None:
            return None
        job, _item = row
        return RecoveryActiveJobRead(
            id=job.id,
            status=job.status,
            requested_by_admin_user_id=job.requested_by_admin_user_id,
            assigned_admin_user_id=job.assigned_admin_user_id,
            action=job.action,
            scope=job.scope or RecoveryReplayScope.STAGE_ONLY,
            created_at=job.created_at,
        )

    def _candidate_actions(
        self,
        work: RecoveryWorkRead,
        *,
        meme_file: MemeFile | None,
        active_job: RecoveryActiveJobRead | None,
        prerequisite_blocks: list[str],
        terminal_descendant_stages: set[ContentPipelineStage],
        source_observation: _SourceObjectObservation | None,
    ) -> list[RecoveryActionRead]:
        if work.stage is None:
            capability_by_kind = {
                RecoveryWorkKind.BACKFILL: RecoveryCapability.RESUME_BACKFILL,
                RecoveryWorkKind.SOURCE_POST: RecoveryCapability.REPLAY_SOURCE_POST,
                RecoveryWorkKind.INGEST_REQUEST: RecoveryCapability.REINSPECT_INGEST,
                RecoveryWorkKind.OUTBOX: RecoveryCapability.REBUILD_OUTBOX,
                RecoveryWorkKind.DEAD_LETTER: RecoveryCapability.ARCHIVE_DEAD_LETTER,
            }
            capabilities = list(dict.fromkeys([*work.capabilities, capability_by_kind.get(work.kind)]))
            source_blockers = (
                self._source_object_blockers(source_observation)
                if work.kind is RecoveryWorkKind.INGEST_REQUEST
                else []
            )
            return [
                RecoveryActionRead(
                    capability=capability,
                    available=(
                        capability in work.capabilities
                        and active_job is None
                        and not source_blockers
                    ),
                    scopes=[RecoveryReplayScope.STAGE_ONLY],
                    default_scope=RecoveryReplayScope.STAGE_ONLY,
                    scope_requirements={
                        RecoveryReplayScope.STAGE_ONLY: RecoveryActionScopeRequirementsRead()
                    },
                    blocked_prerequisites=(
                        ["Another Replay & Repair job owns this work."]
                        if active_job is not None
                        else source_blockers
                        if source_blockers
                        else [work.blocked_reason]
                        if capability not in work.capabilities and work.blocked_reason
                        else []
                    ),
                )
                for capability in capabilities
                if capability is not None
            ]

        stage = work.stage
        active_state = work.status in {
            ContentPipelineStageStatus.PENDING.value,
            ContentPipelineStageStatus.PROCESSING.value,
            SyncTargetStatus.PENDING.value,
            SyncTargetStatus.PROCESSING.value,
        }
        failed = work.status in {ContentPipelineStageStatus.FAILED.value, SyncTargetStatus.FAILED.value}
        terminal = failed and not work.is_retryable
        unsupported = stage is ContentPipelineStage.INGEST or work.status == ContentPipelineStageStatus.DUPLICATE.value
        common_blocked: list[str] = list(prerequisite_blocks)
        if unsupported:
            common_blocked.append("Ingest and duplicate rows cannot be replayed.")
        if active_state:
            common_blocked.append("Pending or processing work must finish or be reclaimed first.")
        common_blocked.extend(self._source_object_blockers(source_observation))
        if source_observation is None and (meme_file is None or not meme_file.s3_original_key):
            common_blocked.append("The durable original is missing from storage.")
        if active_job is not None:
            common_blocked.append("Another Replay & Repair job owns this stage.")

        downstream = list(_DOWNSTREAM_STAGES.get(stage, ()))
        replay_scopes = [RecoveryReplayScope.STAGE_ONLY, RecoveryReplayScope.STAGE_AND_DEPENDENTS]
        root_acknowledgements = ["terminal_override"] if terminal else []
        replay_warnings = ["Stage-only replay leaves existing downstream data stale."] if downstream else []
        provider_stages = {
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
        }
        replay_risks = [_PROVIDER_SEMANTIC_MERGE_RISK] if stage in provider_stages else []
        cascade_risks = (
            [_PROVIDER_SEMANTIC_MERGE_RISK]
            if provider_stages.intersection((stage, *downstream))
            else []
        )
        cascade_acknowledgements = (
            ["terminal_override"] if terminal or terminal_descendant_stages else []
        )
        replay_scope_requirements = {
            RecoveryReplayScope.STAGE_ONLY: RecoveryActionScopeRequirementsRead(
                warnings=replay_warnings,
                risks=replay_risks,
                required_acknowledgements=root_acknowledgements,
            ),
            RecoveryReplayScope.STAGE_AND_DEPENDENTS: RecoveryActionScopeRequirementsRead(
                risks=cascade_risks,
                required_acknowledgements=cascade_acknowledgements,
            ),
        }

        actions: list[RecoveryActionRead] = []
        retry_capability = (
            RecoveryCapability.RESYNC_TARGET
            if work.kind is RecoveryWorkKind.SYNC_TARGET
            else RecoveryCapability.RETRY_STAGE
        )
        actions.append(
            RecoveryActionRead(
                capability=retry_capability,
                available=work.is_retryable and failed and not common_blocked,
                scopes=[RecoveryReplayScope.STAGE_ONLY],
                default_scope=RecoveryReplayScope.STAGE_ONLY,
                downstream_stages=[],
                scope_requirements={
                    RecoveryReplayScope.STAGE_ONLY: RecoveryActionScopeRequirementsRead()
                },
                blocked_prerequisites=(
                    common_blocked
                    if common_blocked
                    else []
                    if work.is_retryable and failed
                    else ["This compatibility retry is available only for retryable failures."]
                ),
            )
        )
        actions.append(
            RecoveryActionRead(
                capability=RecoveryCapability.REPLAY_STAGE,
                available=not common_blocked
                and (
                    failed
                    or work.status
                    in {
                        ContentPipelineStageStatus.SUCCEEDED.value,
                        SyncTargetStatus.SYNCED.value,
                    }
                ),
                scopes=replay_scopes,
                default_scope=RecoveryReplayScope.STAGE_ONLY,
                downstream_stages=downstream,
                warnings=replay_warnings,
                risks=replay_risks,
                required_acknowledgements=root_acknowledgements,
                scope_requirements=replay_scope_requirements,
                blocked_prerequisites=common_blocked,
            )
        )
        if stage is ContentPipelineStage.TRANSCODE:
            media_blocked = list(common_blocked)
            if not _is_moving_media(meme_file):
                media_blocked.append("Derivative regeneration applies only to moving media.")
            actions.append(
                RecoveryActionRead(
                    capability=RecoveryCapability.REGENERATE_DERIVATIVES,
                    available=not media_blocked,
                    scopes=[RecoveryReplayScope.STAGE_ONLY],
                    default_scope=RecoveryReplayScope.STAGE_ONLY,
                    downstream_stages=[],
                    warnings=[
                        "Only the web video and poster are replaced; OCR, embeddings, "
                        "classification, and search stay untouched."
                    ],
                    risks=["The active media pointer changes only after both immutable artifacts verify."],
                    required_acknowledgements=root_acknowledgements,
                    scope_requirements={
                        RecoveryReplayScope.STAGE_ONLY: RecoveryActionScopeRequirementsRead(
                            warnings=[
                                "Only the web video and poster are replaced; OCR, embeddings, "
                                "classification, and search stay untouched."
                            ],
                            risks=[
                                "The active media pointer changes only after both immutable artifacts verify."
                            ],
                            required_acknowledgements=root_acknowledgements,
                        )
                    },
                    blocked_prerequisites=media_blocked,
                )
            )
        declared_capabilities = {action.capability for action in actions}
        for capability in work.capabilities:
            if capability in declared_capabilities:
                continue
            actions.append(
                RecoveryActionRead(
                    capability=capability,
                    available=not common_blocked,
                    scopes=[RecoveryReplayScope.STAGE_ONLY],
                    default_scope=RecoveryReplayScope.STAGE_ONLY,
                    required_acknowledgements=(
                        root_acknowledgements if capability is RecoveryCapability.RECOVER_DEAD_LETTER else []
                    ),
                    scope_requirements={
                        RecoveryReplayScope.STAGE_ONLY: RecoveryActionScopeRequirementsRead(
                            required_acknowledgements=(
                                root_acknowledgements
                                if capability is RecoveryCapability.RECOVER_DEAD_LETTER
                                else []
                            )
                        )
                    },
                    blocked_prerequisites=common_blocked,
                )
            )
        return actions

    async def _build_execution_items(
        self,
        *,
        job: RecoveryJob,
        candidate: RecoveryCandidateRead,
        scope: RecoveryReplayScope,
        retry_limit: int,
        terminal_override_acknowledged: bool,
        reserve: bool,
        source_item_id: uuid.UUID | None = None,
    ) -> list[RecoveryJobItem]:
        work = candidate.work
        if work.stage is None or work.meme_file_id is None:
            return [
                RecoveryJobItem(
                    id=uuid.uuid7(),
                    recovery_job_id=job.id,
                    source_item_id=source_item_id,
                    is_root=True,
                    work_kind=work.kind,
                    work_id=work.id,
                    action=job.action,
                    expected_version=work.version,
                    retry_limit=retry_limit,
                    terminal_override_acknowledged=terminal_override_acknowledged,
                    reservation_active=reserve,
                    status=RecoveryJobItemStatus.QUEUED,
                )
            ]

        meme_file = await self._session.get(MemeFile, work.meme_file_id)
        if meme_file is None:
            raise AdminRecoveryNotFoundError(f"Pipeline file {work.meme_file_id} does not exist.")
        root_stage = work.stage
        stages = [root_stage]
        if scope is RecoveryReplayScope.STAGE_AND_DEPENDENTS:
            stages.extend(_DOWNSTREAM_STAGES.get(root_stage, ()))
        stage_rows = {
            row.stage: row
            for row in (
                (
                    await self._session.execute(
                        select(PipelineStageJournal).where(PipelineStageJournal.meme_file_id == work.meme_file_id)
                    )
                )
                .scalars()
                .all()
            )
        }
        active_reserved_stages = set(
            (
                await self._session.execute(
                    select(RecoveryJobItem.stage)
                    .join(RecoveryJob, RecoveryJob.id == RecoveryJobItem.recovery_job_id)
                    .where(
                        RecoveryJobItem.meme_file_id == work.meme_file_id,
                        RecoveryJobItem.stage.in_(stages[1:]),
                        RecoveryJobItem.status.in_(
                            (
                                RecoveryJobItemStatus.QUEUED,
                                RecoveryJobItemStatus.WAITING_DEPENDENCY,
                                RecoveryJobItemStatus.WAITING_CAPACITY,
                                RecoveryJobItemStatus.DISPATCHED,
                            )
                        ),
                        RecoveryJob.status.in_(
                            (
                                RecoveryJobStatus.QUEUED,
                                RecoveryJobStatus.RUNNING,
                                RecoveryJobStatus.CANCELLING,
                            )
                        ),
                    )
                )
            ).scalars()
        )
        for stage in stages[1:]:
            row = stage_rows.get(stage)
            if stage in active_reserved_stages:
                raise AdminRecoveryConflictError(
                    f"Dependent stage {stage.value} is already owned by another Replay & Repair job."
                )
            if row is None:
                continue
            if row.status in {
                ContentPipelineStageStatus.PENDING,
                ContentPipelineStageStatus.PROCESSING,
            }:
                raise AdminRecoveryConflictError(
                    f"Dependent stage {stage.value} is {row.status.value} and must finish or be reclaimed first."
                )
            if row.status is ContentPipelineStageStatus.DUPLICATE:
                raise AdminRecoveryConflictError(
                    f"Dependent stage {stage.value} is a duplicate row and cannot be replayed."
                )
            if (
                row.status is ContentPipelineStageStatus.FAILED
                and not row.is_retryable
                and not terminal_override_acknowledged
            ):
                raise AdminRecoveryConflictError(
                    f"Terminal dependent stage {stage.value} requires terminal_override acknowledgement."
                )
        ids = {stage: uuid.uuid7() for stage in stages}
        classify_id = ids.get(ContentPipelineStage.CLASSIFY)
        items: list[RecoveryJobItem] = []
        previous_id: uuid.UUID | None = None
        for index, stage in enumerate(stages):
            row = stage_rows.get(stage)
            is_root = index == 0
            if is_root:
                kind = work.kind
                item_work_id = work.id
                expected_version = work.version
                action = (
                    RecoveryCapability.REGENERATE_DERIVATIVES
                    if job.action
                    in {
                        RecoveryCapability.RETRY_STAGE,
                        RecoveryCapability.REPLAY_STAGE,
                    }
                    and stage is ContentPipelineStage.TRANSCODE
                    and _is_moving_media(meme_file)
                    else job.action
                )
            else:
                kind = RecoveryWorkKind.PIPELINE_STAGE
                item_work_id = str(row.id) if row is not None else f"{work.meme_file_id}:{stage.value}"
                expected_version = (
                    _version(row, row.last_event_id)
                    if row is not None
                    else f"missing:{work.meme_file_id}:{stage.value}"
                )
                action = RecoveryCapability.REPLAY_STAGE
            if stage in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI} and classify_id:
                parent_id = classify_id
            else:
                parent_id = previous_id
            item = RecoveryJobItem(
                id=ids[stage],
                recovery_job_id=job.id,
                parent_item_id=parent_id,
                source_item_id=source_item_id if is_root else None,
                meme_file_id=work.meme_file_id,
                stage=stage,
                is_root=is_root,
                work_kind=kind,
                work_id=item_work_id,
                action=action,
                expected_version=expected_version,
                retry_limit=retry_limit,
                preserve_ready=meme_file.status is ContentProcessingStatus.READY,
                suppress_fanout=True,
                terminal_override_acknowledged=terminal_override_acknowledged,
                reservation_active=reserve,
                status=(RecoveryJobItemStatus.QUEUED if is_root else RecoveryJobItemStatus.WAITING_DEPENDENCY),
            )
            items.append(item)
            if stage not in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}:
                previous_id = item.id
        return items

    @staticmethod
    def _set_materialized_counts(
        job: RecoveryJob,
        items: Sequence[RecoveryJobItem],
        *,
        selected_roots: int,
    ) -> None:
        job.selected_root_count = selected_roots
        job.expanded_execution_count = len(items)
        job.total_count = len(items)
        AdminRecoveryService._refresh_job_counts_from_items(job, items)

    @staticmethod
    def _refresh_job_counts_from_items(
        job: RecoveryJob,
        items: Sequence[RecoveryJobItem],
        *,
        incremental: bool = False,
    ) -> None:
        counts = Counter(item.status for item in items)
        values = {
            "queued_count": counts[RecoveryJobItemStatus.QUEUED],
            "waiting_count": counts[RecoveryJobItemStatus.WAITING_DEPENDENCY]
            + counts[RecoveryJobItemStatus.WAITING_CAPACITY],
            "dispatched_count": counts[RecoveryJobItemStatus.DISPATCHED],
            "succeeded_count": counts[RecoveryJobItemStatus.SUCCEEDED],
            "stale_count": counts[RecoveryJobItemStatus.SKIPPED_STALE],
            "skipped_count": counts[RecoveryJobItemStatus.SKIPPED_DEPENDENCY],
            "cancelled_count": counts[RecoveryJobItemStatus.CANCELLED],
        }
        for field, value in values.items():
            setattr(job, field, getattr(job, field) + value if incremental else value)
        terminal_count = sum(counts[status] for status in _TERMINAL_ITEM_STATUSES)
        failed_count = counts[RecoveryJobItemStatus.FAILED]
        if incremental:
            job.completed_count += terminal_count
            job.failed_count += failed_count
        else:
            job.completed_count = terminal_count
            job.failed_count = failed_count

    async def _load_file_sources(
        self,
        file_ids: set[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[uuid.UUID | None, str]]:
        if not file_ids:
            return {}
        rows = (
            await self._session.execute(
                select(
                    MemeSource.file_id,
                    SourceChannel.id,
                    SourceChannel.username,
                    SourceChannel.title,
                    MemeSource.source_id,
                )
                .join(
                    SourceChannel,
                    and_(
                        SourceChannel.platform == MemeSource.platform, SourceChannel.platform_id == MemeSource.source_id
                    ),
                    isouter=True,
                )
                .where(MemeSource.file_id.in_(file_ids))
                .order_by(MemeSource.created_at.asc())
            )
        ).all()
        result: dict[uuid.UUID, tuple[uuid.UUID | None, str]] = {}
        for file_id, channel_id, username, title, source_id in rows:
            result.setdefault(file_id, (channel_id, f"@{username}" if username else title or source_id))
        return result

    async def _idempotent_job(self, admin_user_id: uuid.UUID, request_id: uuid.UUID) -> RecoveryJob | None:
        return await self._session.scalar(
            select(RecoveryJob).where(
                RecoveryJob.requested_by_admin_user_id == admin_user_id,
                RecoveryJob.request_id == request_id,
            )
        )

    async def _insert_idempotent_job(self, job: RecoveryJob) -> tuple[RecoveryJob, bool]:
        try:
            async with self._session.begin_nested():
                self._session.add(job)
                await self._session.flush()
        except IntegrityError as exc:
            if integrity_constraint_name(exc) != _RECOVERY_JOB_REQUEST_CONSTRAINT:
                raise
            existing = await self._idempotent_job(
                job.requested_by_admin_user_id,
                job.request_id,
            )
            if existing is None:
                raise AdminRecoveryConflictError(
                    "Recovery request was created concurrently but could not be loaded; retry it."
                ) from exc
            self._assert_idempotency_fingerprint(
                existing,
                action=job.action,
                selection=job.selection,
                reason=job.reason,
            )
            return existing, False
        return job, True

    @staticmethod
    def _assert_idempotency_fingerprint(
        job: RecoveryJob,
        *,
        action: RecoveryCapability,
        selection: dict[str, object],
        reason: str,
    ) -> None:
        if job.action != action or job.selection != selection or job.reason != reason:
            raise AdminRecoveryConflictError("This request ID was already used for a different recovery request.")

    async def _get_job(self, job_id: uuid.UUID, *, lock: bool = False) -> RecoveryJob:
        job = await self._session.get(
            RecoveryJob,
            job_id,
            with_for_update=lock,
            populate_existing=lock,
        )
        if job is None:
            raise AdminRecoveryNotFoundError(f"Recovery job {job_id} does not exist.")
        return job

    def _assert_job_assignee(self, job: RecoveryJob, admin_user_id: uuid.UUID) -> None:
        permitted_admin_id = job.assigned_admin_user_id or job.requested_by_admin_user_id
        if permitted_admin_id != admin_user_id:
            raise AdminRecoveryConflictError(
                "This job is assigned to another administrator; perform an audited handoff first."
            )

    async def _project_job(self, job: RecoveryJob, *, include_items: bool = True) -> RecoveryJobRead:
        items: list[RecoveryJobItem] = []
        if include_items:
            items = list(
                (
                    await self._session.execute(
                        select(RecoveryJobItem)
                        .where(RecoveryJobItem.recovery_job_id == job.id)
                        .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        return RecoveryJobRead(
            id=job.id,
            request_id=job.request_id,
            requested_by_admin_user_id=job.requested_by_admin_user_id,
            assigned_admin_user_id=job.assigned_admin_user_id,
            source_recovery_job_id=job.source_recovery_job_id,
            status=job.status,
            action=job.action,
            scope=job.scope or RecoveryReplayScope.STAGE_ONLY,
            retry_limit=job.retry_limit,
            reason=job.reason,
            total_count=job.total_count,
            completed_count=job.completed_count,
            failed_count=job.failed_count,
            selected_root_count=job.selected_root_count,
            expanded_execution_count=job.expanded_execution_count,
            preparation_scanned_count=job.preparation_scanned_count,
            excluded_count=job.excluded_count,
            exclusions_by_reason=job.exclusions_by_reason,
            queued_count=job.queued_count,
            waiting_count=job.waiting_count,
            dispatched_count=job.dispatched_count,
            succeeded_count=job.succeeded_count,
            stale_count=job.stale_count,
            skipped_count=job.skipped_count,
            cancelled_count=job.cancelled_count,
            selection_snapshot_at=job.selection_snapshot_at,
            materialization_completed_at=job.materialization_completed_at,
            expires_at=job.expires_at,
            scheduled_at=job.scheduled_at,
            completed_at=job.completed_at,
            cancelled_at=job.cancelled_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
            version=_version(job),
            items=[self._project_job_item(item) for item in items],
        )

    @staticmethod
    def _project_job_item(item: RecoveryJobItem) -> RecoveryJobItemRead:
        return RecoveryJobItemRead(
            id=item.id,
            parent_item_id=item.parent_item_id,
            source_item_id=item.source_item_id,
            work_kind=item.work_kind,
            work_id=item.work_id,
            meme_file_id=item.meme_file_id,
            stage=item.stage,
            is_root=item.is_root,
            action=item.action,
            status=item.status,
            expected_version=item.expected_version,
            canonical_version=item.canonical_version,
            retry_limit=item.retry_limit,
            attempt_budget_start=item.attempt_budget_start or 0,
            retryable_failures_consumed=item.retryable_failures_consumed,
            normalized_reason=item.normalized_reason,
            safe_error=_safe_error(item.safe_error_text),
            dispatched_at=item.dispatched_at,
            finished_at=item.finished_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


def _source_label(channel: SourceChannel) -> str:
    return f"@{channel.username}" if channel.username else channel.title


def _safe_error(value: str | None) -> str | None:
    return sanitize_operational_error(value, max_length=1000)


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _is_moving_media(file: MemeFile | None) -> bool:
    return file is not None and _has_moving_media_mime(file)


def _has_moving_media_mime(file: MemeFile) -> bool:
    mime_type = (file.mime_type or "").casefold()
    return mime_type in SUPPORTED_MOVING_MEDIA_MIME_TYPES


def _web_video_is_outdated(file: MemeFile) -> bool:
    if file.s3_web_video_key is None:
        return False
    return bool(
        file.web_video_profile != WEB_VIDEO_PROFILE_ID
        or file.web_video_verified_at is None
        or file.source_has_audio is None
        or file.web_video_has_audio is None
        or file.source_has_audio != file.web_video_has_audio
    )


def _job_acknowledgements(job: RecoveryJob) -> set[str]:
    raw = job.selection.get("acknowledgements")
    if not isinstance(raw, list):
        return set()
    return {str(value) for value in raw}


def _action_requirements_for_scope(
    action: RecoveryActionRead,
    scope: RecoveryReplayScope,
) -> RecoveryActionScopeRequirementsRead:
    scoped = action.scope_requirements.get(scope)
    if scoped is not None:
        return scoped
    # Compatibility for previews created while the flat action contract was active.
    return RecoveryActionScopeRequirementsRead(
        warnings=action.warnings,
        risks=action.risks,
        required_acknowledgements=action.required_acknowledgements,
    )


def _execution_item_key(item: RecoveryJobItem) -> tuple[str, str, str]:
    if item.meme_file_id is not None and item.stage is not None:
        return ("stage", str(item.meme_file_id), item.stage.value)
    return ("work", item.work_kind.value, item.work_id)


def _is_outdated_derivative_preview(
    job: RecoveryJob,
    items: Sequence[RecoveryJobItem],
) -> bool:
    raw_selector = job.selection.get("selector")
    raw_filters = raw_selector.get("filters") if isinstance(raw_selector, dict) else None
    return bool(
        job.action is RecoveryCapability.REGENERATE_DERIVATIVES
        and (job.scope or RecoveryReplayScope.STAGE_ONLY) is RecoveryReplayScope.STAGE_ONLY
        and isinstance(raw_filters, dict)
        and raw_filters.get("outdated_web_video") is True
        and items
        and all(
            item.is_root
            and item.stage is ContentPipelineStage.TRANSCODE
            and item.meme_file_id is not None
            and item.action is RecoveryCapability.REGENERATE_DERIVATIVES
            for item in items
        )
    )


def _matches_filters(item: RecoveryWorkRead, filters: RecoveryQueryFilters) -> bool:
    normalized_query = (filters.query or "").casefold()
    normalized_reason = (filters.reason or "").casefold()
    return bool(
        (filters.bucket is None or item.bucket is filters.bucket)
        and (filters.kind is None or item.kind is filters.kind)
        and (filters.source_channel_id is None or item.source_channel_id == filters.source_channel_id)
        and (filters.stage is None or item.stage is filters.stage)
        and (not normalized_reason or normalized_reason in (item.error_code or item.reason or "").casefold())
        and (
            not normalized_query
            or normalized_query
            in " ".join(
                value
                for value in (
                    item.title,
                    item.source_label or "",
                    item.post_id or "",
                    str(item.meme_file_id or ""),
                    item.id,
                )
                if value
            ).casefold()
        )
    )


def _query_snapshot_work_sort_key(item: RecoveryWorkRead) -> tuple[str, str, int, str]:
    """Put upstream roots first so a cascade owns overlapping descendants."""

    return (
        item.kind.value,
        str(item.meme_file_id or ""),
        _PIPELINE_STAGE_ORDER.get(item.stage, len(_PIPELINE_STAGE_ORDER)),
        item.id,
    )


def _stable_snapshot_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _encode_job_cursor(created_at: datetime, job_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(job_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_job_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        return created_at, uuid.UUID(str(payload["id"]))
    except (binascii.Error, KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AdminRecoveryConflictError("Recovery job cursor is invalid or expired.") from exc


def _encode_item_cursor(priority: int, created_at: datetime, item_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"priority": priority, "created_at": created_at.isoformat(), "id": str(item_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_item_cursor(cursor: str | None) -> tuple[int, datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        return int(payload["priority"]), created_at, uuid.UUID(str(payload["id"]))
    except (binascii.Error, KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AdminRecoveryConflictError("Recovery item cursor is invalid or expired.") from exc


def _dead_letter_matches_work(dead_letter: PipelineDeadLetter, work: RecoveryWorkRead) -> bool:
    if dead_letter.work_kind != work.kind or dead_letter.work_id != work.id:
        return False
    if work.kind not in {RecoveryWorkKind.PIPELINE_STAGE, RecoveryWorkKind.SYNC_TARGET}:
        return True
    raw_event_id = dead_letter.safe_payload.get("event_id")
    canonical_event_id = work.details.get("event_id")
    try:
        dead_letter_event_id = uuid.UUID(str(raw_event_id))
        current_event_id = uuid.UUID(str(canonical_event_id))
    except TypeError, ValueError, AttributeError:
        return False
    return dead_letter_event_id == current_event_id


def _outdated_file_snapshot_version(meme_file: MemeFile) -> str:
    """Fence an outdated root that had no Transcode journal at capture time."""

    return f"outdated-file-v1:{meme_file.updated_at.isoformat()}"


def _version(row: object, event_id: uuid.UUID | None = None) -> str:
    updated_at = getattr(row, "updated_at", None)
    stamp = updated_at.isoformat() if updated_at is not None else ""
    return f"{stamp}:{event_id or ''}"


def _project_backfill(row: SourceChannelBackfillJob) -> AdminSourceBackfillRead:
    session = row.source_channel.telegram_session
    capabilities = (
        [RecoveryCapability.RESUME_BACKFILL]
        if row.status is SourceChannelBackfillJobStatus.FAILED and row.is_retryable
        else []
    )
    return AdminSourceBackfillRead(
        id=row.id,
        source_channel_id=row.source_channel_id,
        status=row.status.value,
        requested_count=row.requested_message_count,
        scanned_count=row.scanned_message_count,
        remaining_count=max(0, row.requested_message_count - row.scanned_message_count),
        cursor_post_id=row.cursor_post_id,
        attempt_count=row.attempt_count,
        quarantined_count=row.quarantined_message_count,
        last_error_code=row.last_error_code,
        last_error_class=row.last_error_class,
        safe_error=_safe_error(row.last_error_text),
        is_retryable=row.is_retryable,
        next_attempt_at=row.next_attempt_at,
        last_progress_at=row.last_progress_at,
        telegram_session_id=row.source_channel.telegram_session_id,
        telegram_session_name=session.name if session is not None else None,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.completed_at,
        updated_at=row.updated_at,
        version=_version(row),
        capabilities=capabilities,
    )


def _work_sort_key(item: RecoveryWorkRead) -> tuple[int, str, str, str]:
    return (_BUCKET_PRIORITY[item.bucket], item.occurred_at.isoformat(), item.kind.value, item.id)


def _encode_cursor(snapshot_at: datetime, key: tuple[int, str, str, str]) -> str:
    payload = json.dumps({"snapshot_at": snapshot_at.isoformat(), "key": key}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, tuple[int, str, str, str] | None]:
    if not cursor:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        from datetime import datetime

        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        snapshot = datetime.fromisoformat(payload["snapshot_at"])
        raw_key = payload["key"]
        if snapshot.tzinfo is None or snapshot.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        if not isinstance(raw_key, list) or len(raw_key) != 4:
            raise ValueError("cursor key must contain four fields")
        return snapshot, (int(raw_key[0]), str(raw_key[1]), str(raw_key[2]), str(raw_key[3]))
    except (
        binascii.Error,
        IndexError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise AdminRecoveryConflictError("Recovery cursor is invalid or expired.") from exc


__all__ = [
    "AdminRecoveryConflictError",
    "AdminRecoveryError",
    "AdminRecoveryNotFoundError",
    "AdminRecoveryOriginalMissingError",
    "AdminRecoveryService",
    "AdminRecoveryStorageUnavailableError",
]
