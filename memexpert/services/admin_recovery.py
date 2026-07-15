# ruff: noqa: TC001,TC003
"""Durable browser-admin recovery query and mutation services."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.models.base import utcnow
from memexpert.models.content import (
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
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    RecoveryBucket,
    RecoveryCapability,
    RecoveryDeadLetterStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
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
)
from memexpert.schemas.admin_recovery import (
    AdminSourceBackfillPageRead,
    AdminSourceBackfillRead,
    RecoveryBatchPreviewRequest,
    RecoveryJobItemRead,
    RecoveryJobRead,
    RecoveryMutationRequest,
    RecoverySummaryRead,
    RecoveryWorkPageRead,
    RecoveryWorkRead,
)
from memexpert.services._integrity import integrity_constraint_name

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


_WORK_SCAN_LIMIT = 10_000
_STUCK_AFTER = timedelta(minutes=15)
_BACKFILL_STUCK_AFTER = timedelta(minutes=5)
_PREVIEW_TTL = timedelta(minutes=5)
_RECOVERY_JOB_REQUEST_CONSTRAINT = "uq_recovery_jobs_admin_request_id"
_BUCKET_PRIORITY = {
    RecoveryBucket.DEAD_LETTERED: 0,
    RecoveryBucket.STUCK: 1,
    RecoveryBucket.RETRYABLE: 2,
    RecoveryBucket.BLOCKED: 3,
}


class AdminRecoveryError(RuntimeError):
    """Base error for browser-admin recovery operations."""


class AdminRecoveryNotFoundError(AdminRecoveryError):
    """Raised when canonical recovery work does not exist."""


class AdminRecoveryConflictError(AdminRecoveryError):
    """Raised for stale versions, invalid actions, or completed batches."""


class AdminRecoveryService:
    """Query canonical failure state and create audited durable recovery jobs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_summary(self) -> RecoverySummaryRead:
        items = await self._collect_work(snapshot_at=utcnow())
        counts = Counter(item.bucket for item in items)
        return RecoverySummaryRead(
            retryable_count=counts[RecoveryBucket.RETRYABLE],
            blocked_count=counts[RecoveryBucket.BLOCKED],
            stuck_count=counts[RecoveryBucket.STUCK],
            dead_lettered_count=counts[RecoveryBucket.DEAD_LETTERED],
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
        return RecoveryWorkPageRead(items=page, next_cursor=next_cursor, snapshot_at=observed_through)

    async def get_work(self, kind: RecoveryWorkKind, work_id: str) -> RecoveryWorkRead:
        items = await self._collect_work(snapshot_at=utcnow())
        item = next((candidate for candidate in items if candidate.kind is kind and candidate.id == work_id), None)
        if item is None:
            raise AdminRecoveryNotFoundError(f"Recovery work {kind.value}/{work_id} does not exist.")
        return item

    async def retry_work(
        self,
        *,
        admin_user_id: uuid.UUID,
        kind: RecoveryWorkKind,
        work_id: str,
        payload: RecoveryMutationRequest,
    ) -> RecoveryJobRead:
        selection: dict[str, object] = {"kind": kind.value, "id": work_id}
        existing = await self._idempotent_job(admin_user_id, payload.request_id)
        if existing is not None:
            self._assert_idempotency_fingerprint(
                existing,
                action=payload.capability,
                selection=selection,
                reason=payload.reason,
            )
            return await self._project_job(existing)
        work = await self.get_work(kind, work_id)
        self._assert_mutation_allowed(work, payload.version, payload.capability)
        job = RecoveryJob(
            requested_by_admin_user_id=admin_user_id,
            request_id=payload.request_id,
            status=RecoveryJobStatus.QUEUED,
            action=payload.capability,
            reason=payload.reason,
            selection=selection,
            total_count=1,
            scheduled_at=utcnow(),
        )
        job, created = await self._insert_idempotent_job(job)
        if not created:
            return await self._project_job(job)
        item = RecoveryJobItem(
            recovery_job_id=job.id,
            work_kind=kind,
            work_id=work_id,
            action=payload.capability,
            expected_version=payload.version,
        )
        self._session.add(item)
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=payload.request_id,
                action=payload.capability.value,
                target_kind=kind.value,
                target_id=work_id,
                previous_values=work.model_dump(mode="json"),
                new_values={"recovery_job_id": str(job.id), "status": job.status.value},
                note=payload.reason,
            )
        )
        await self._session.commit()
        return await self._project_job(job)

    async def preview_batch(
        self,
        *,
        admin_user_id: uuid.UUID,
        payload: RecoveryBatchPreviewRequest,
    ) -> RecoveryJobRead:
        selection: dict[str, object] = {
            "items": [item.model_dump(mode="json") for item in payload.items]
        }
        existing = await self._idempotent_job(admin_user_id, payload.request_id)
        if existing is not None:
            self._assert_idempotency_fingerprint(
                existing,
                action=payload.capability,
                selection=selection,
                reason=payload.reason,
            )
            return await self._project_job(existing)
        reference_keys = [(reference.kind, reference.id) for reference in payload.items]
        if len(set(reference_keys)) != len(reference_keys):
            raise AdminRecoveryConflictError("Recovery batch contains the same work item more than once.")
        available = {
            (work.kind, work.id): work
            for work in await self._collect_work(snapshot_at=utcnow())
        }
        resolved: list[RecoveryWorkRead] = []
        for reference in payload.items:
            work = available.get((reference.kind, reference.id))
            if work is None:
                raise AdminRecoveryNotFoundError(
                    f"Recovery work {reference.kind.value}/{reference.id} does not exist."
                )
            self._assert_mutation_allowed(work, reference.version, payload.capability)
            resolved.append(work)
        now = utcnow()
        job = RecoveryJob(
            requested_by_admin_user_id=admin_user_id,
            request_id=payload.request_id,
            status=RecoveryJobStatus.PREVIEW,
            action=payload.capability,
            reason=payload.reason,
            selection=selection,
            total_count=len(resolved),
            expires_at=now + _PREVIEW_TTL,
        )
        job, created = await self._insert_idempotent_job(job)
        if not created:
            return await self._project_job(job)
        self._session.add_all(
            RecoveryJobItem(
                recovery_job_id=job.id,
                work_kind=work.kind,
                work_id=work.id,
                action=payload.capability,
                expected_version=work.version,
            )
            for work in resolved
        )
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
        job = await self._get_job(job_id, lock=True)
        self._assert_job_owner(job, admin_user_id)
        if job.status is not RecoveryJobStatus.PREVIEW:
            if job.status in {RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING}:
                return await self._project_job(job)
            raise AdminRecoveryConflictError(f"Recovery batch is {job.status.value}, not previewable.")
        if _version(job) != version:
            raise AdminRecoveryConflictError("Recovery preview changed; reload it before scheduling.")
        if job.expires_at is not None and job.expires_at <= utcnow():
            job.status = RecoveryJobStatus.EXPIRED
            await self._session.commit()
            raise AdminRecoveryConflictError("Recovery preview expired; create a fresh preview.")
        job.status = RecoveryJobStatus.QUEUED
        job.scheduled_at = utcnow()
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
        await self._session.commit()
        return await self._project_job(job)

    async def cancel_batch(
        self,
        *,
        admin_user_id: uuid.UUID,
        job_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> RecoveryJobRead:
        job = await self._get_job(job_id, lock=True)
        self._assert_job_owner(job, admin_user_id)
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
            if item.status in {RecoveryJobItemStatus.QUEUED, RecoveryJobItemStatus.WAITING_CAPACITY}:
                item.status = RecoveryJobItemStatus.CANCELLED
                item.finished_at = utcnow()
        job.status = RecoveryJobStatus.CANCELLED
        job.cancelled_at = utcnow()
        job.completed_count = sum(item.status is RecoveryJobItemStatus.CANCELLED for item in items)
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
        job = await self._get_job(job_id)
        self._assert_job_owner(job, admin_user_id)
        return await self._project_job(job)

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

    async def _collect_work(self, *, snapshot_at: datetime) -> list[RecoveryWorkRead]:
        stuck_before = snapshot_at - _STUCK_AFTER
        backfill_stuck_before = snapshot_at - _BACKFILL_STUCK_AFTER
        work: dict[tuple[RecoveryWorkKind, str], RecoveryWorkRead] = {}

        backfills = (
            (
                await self._session.execute(
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
                    .limit(_WORK_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
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

        posts = (
            (
                await self._session.execute(
                    select(SourceChannelPost)
                    .options(selectinload(SourceChannelPost.source_channel))
                    .where(
                        SourceChannelPost.status == SourceChannelPostStatus.FAILED,
                        SourceChannelPost.updated_at <= snapshot_at,
                    )
                    .order_by(SourceChannelPost.updated_at.desc())
                    .limit(_WORK_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
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

        ingest_rows = (
            (
                await self._session.execute(
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
                    .limit(_WORK_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
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

        stages = (
            (
                await self._session.execute(
                    select(PipelineStageJournal)
                    .where(
                        PipelineStageJournal.updated_at <= snapshot_at,
                        PipelineStageJournal.stage.not_in(
                            (ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI)
                        ),
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
                    .limit(_WORK_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        file_sources = await self._load_file_sources({row.meme_file_id for row in stages})
        for row in stages:
            stuck = row.status in {ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING}
            retryable = row.is_retryable and row.status is ContentPipelineStageStatus.FAILED
            source = file_sources.get(row.meme_file_id)
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
                version=_version(row, row.last_event_id),
                capabilities=[RecoveryCapability.RETRY_STAGE] if retryable else [],
                blocked_reason=None
                if retryable
                else "Stuck work must first be reclaimed by the automatic reconciler."
                if stuck
                else "The stage failure is non-retryable.",
                details={"event_id": str(row.last_event_id) if row.last_event_id is not None else None},
            )
            work[(item.kind, item.id)] = item

        sync_rows = (
            (
                await self._session.execute(
                    select(MemeFileSyncTargetSnapshot)
                    .where(
                        MemeFileSyncTargetSnapshot.updated_at <= snapshot_at,
                        or_(
                            MemeFileSyncTargetSnapshot.status == SyncTargetStatus.FAILED,
                            and_(
                                MemeFileSyncTargetSnapshot.status.in_(
                                    (SyncTargetStatus.PENDING, SyncTargetStatus.PROCESSING)
                                ),
                                MemeFileSyncTargetSnapshot.updated_at < stuck_before,
                            ),
                        ),
                    )
                    .order_by(MemeFileSyncTargetSnapshot.updated_at.desc())
                    .limit(_WORK_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
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

        outbox_rows = (
            (
                await self._session.execute(
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
                    .limit(_WORK_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
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

        dead_letters = (
            (
                await self._session.execute(
                    select(PipelineDeadLetter)
                    .where(
                        PipelineDeadLetter.status.in_(
                            (RecoveryDeadLetterStatus.UNRESOLVED, RecoveryDeadLetterStatus.RECOVERY_QUEUED)
                        ),
                        PipelineDeadLetter.updated_at <= snapshot_at,
                    )
                    .order_by(PipelineDeadLetter.updated_at.desc())
                    .limit(_WORK_SCAN_LIMIT)
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
            raise AdminRecoveryConflictError(
                "This request ID was already used for a different recovery request."
            )

    async def _get_job(self, job_id: uuid.UUID, *, lock: bool = False) -> RecoveryJob:
        job = await self._session.get(RecoveryJob, job_id, with_for_update=lock)
        if job is None:
            raise AdminRecoveryNotFoundError(f"Recovery job {job_id} does not exist.")
        return job

    def _assert_job_owner(self, job: RecoveryJob, admin_user_id: uuid.UUID) -> None:
        if job.requested_by_admin_user_id != admin_user_id:
            raise AdminRecoveryNotFoundError(f"Recovery job {job.id} does not exist.")

    def _assert_mutation_allowed(
        self,
        work: RecoveryWorkRead,
        expected_version: str,
        capability: RecoveryCapability,
    ) -> None:
        if work.version != expected_version:
            raise AdminRecoveryConflictError("Recovery work changed; reload it before retrying.")
        if capability not in work.capabilities:
            raise AdminRecoveryConflictError(
                work.blocked_reason or f"{capability.value} is not available for this work."
            )

    async def _project_job(self, job: RecoveryJob) -> RecoveryJobRead:
        items = (
            (
                await self._session.execute(
                    select(RecoveryJobItem)
                    .where(RecoveryJobItem.recovery_job_id == job.id)
                    .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return RecoveryJobRead(
            id=job.id,
            request_id=job.request_id,
            status=job.status,
            action=job.action,
            reason=job.reason,
            total_count=job.total_count,
            completed_count=job.completed_count,
            failed_count=job.failed_count,
            expires_at=job.expires_at,
            scheduled_at=job.scheduled_at,
            completed_at=job.completed_at,
            cancelled_at=job.cancelled_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
            version=_version(job),
            items=[
                RecoveryJobItemRead(
                    id=item.id,
                    work_kind=item.work_kind,
                    work_id=item.work_id,
                    action=item.action,
                    status=item.status,
                    normalized_reason=item.normalized_reason,
                    safe_error=_safe_error(item.safe_error_text),
                    dispatched_at=item.dispatched_at,
                    finished_at=item.finished_at,
                )
                for item in items
            ],
        )


def _source_label(channel: SourceChannel) -> str:
    return f"@{channel.username}" if channel.username else channel.title


def _safe_error(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized[:1000] or None


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
    except (TypeError, ValueError, AttributeError):
        return False
    return dead_letter_event_id == current_event_id


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
    "AdminRecoveryService",
]
