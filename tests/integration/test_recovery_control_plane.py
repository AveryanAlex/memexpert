# ruff: noqa: TC002
"""Integration coverage for durable recovery execution and reliability state."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.core.config import Settings
from memexpert.messaging.rabbitmq_outbox import RabbitOutboxRelay, recovery_stage_worker_attempt_ceiling
from memexpert.models.base import utcnow
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeFileSyncTargetSnapshot,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
)
from memexpert.models.enums import (
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    DependencyCircuitStatus,
    IngestSourceKind,
    MediaGenerationStatus,
    PipelineAttemptOutcome,
    PipelineCapacityStatus,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    RecoveryCapability,
    RecoveryDeadLetterStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.operations import (
    DependencyCircuitState,
    MediaGeneration,
    OperationalAuditLog,
    PipelineCapacityState,
    PipelineDeadLetter,
    PipelineStageAttempt,
    RecoveryJob,
    RecoveryJobItem,
    RecoveryQuerySnapshotMember,
)
from memexpert.models.user import User
from memexpert.pipeline.events import MEDIA_INSPECT_REQUESTED_EVENT_TYPE, PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.admin_recovery import (
    RecoveryActionRequest,
    RecoveryBatchPreviewRequest,
    RecoveryCandidateRead,
    RecoveryExplicitSelector,
    RecoveryJobRead,
    RecoveryMutationRequest,
    RecoveryQueryFilters,
    RecoveryQuerySelector,
    RecoveryRetryFailedPreviewRequest,
    RecoveryWorkRead,
    RecoveryWorkReference,
)
from memexpert.services.admin_recovery import AdminRecoveryConflictError, AdminRecoveryService
from memexpert.services.pipeline_reliability import (
    DependencyCircuitOpenError,
    PipelineCapacityPolicy,
    acquire_dependency_circuit,
    record_dependency_failure,
    record_dependency_success,
    record_pipeline_dead_letter,
    refresh_pipeline_capacity_states,
)
from memexpert.services.recovery_runtime import RecoveryRuntime

if TYPE_CHECKING:
    from collections.abc import Sequence


async def _seed_meme_file(
    session: AsyncSession,
    *,
    status: ContentProcessingStatus = ContentProcessingStatus.PROCESSING,
) -> MemeFile:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.IMAGE)
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=status,
        s3_original_key=f"tests/recovery/{file_id}.jpg",
        mime_type="image/jpeg",
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()
    return file


class _FirstLookupBarrierRecoveryService(AdminRecoveryService):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier
        self._first_lookup = True

    async def _idempotent_job(
        self,
        admin_user_id: uuid.UUID,
        request_id: uuid.UUID,
    ) -> RecoveryJob | None:
        existing = await super()._idempotent_job(admin_user_id, request_id)
        if self._first_lookup:
            self._first_lookup = False
            await self._barrier.wait()
        return existing


class _CandidateBarrierRecoveryService(AdminRecoveryService):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    async def get_candidate(
        self,
        kind: RecoveryWorkKind,
        work_id: str,
        *,
        ignore_recovery_item_id: uuid.UUID | None = None,
        verify_source_object: bool = True,
    ) -> RecoveryCandidateRead:
        candidate = await super().get_candidate(
            kind,
            work_id,
            ignore_recovery_item_id=ignore_recovery_item_id,
            verify_source_object=verify_source_object,
        )
        await self._barrier.wait()
        return candidate


class _PreviewValidationBarrierRecoveryService(AdminRecoveryService):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    async def _revalidate_preview_execution(
        self,
        job: RecoveryJob,
        items: Sequence[RecoveryJobItem],
        *,
        acknowledgements: set[str],
    ) -> None:
        await super()._revalidate_preview_execution(
            job,
            items,
            acknowledgements=acknowledgements,
        )
        await self._barrier.wait()


def _failed_outbox(label: str) -> RabbitMQOutboxMessage:
    return RabbitMQOutboxMessage(
        exchange="memexpert.pipeline",
        routing_key="pipeline.ocr",
        payload={"event_type": "meme_transcoded", "label": label},
        headers={},
        message_id=str(uuid.uuid7()),
        event_type="meme_transcoded",
        aggregate_type="meme_file",
        aggregate_id=str(uuid.uuid7()),
        status=RabbitMQOutboxMessageStatus.FAILED,
        attempt_count=5,
        last_error_text="broker unavailable",
    )


class _FailingPublishBroker:
    async def publish(self, *_args: object, **_kwargs: object) -> None:
        raise OSError("RabbitMQ remained unavailable.")


class _FailOncePublishBroker:
    def __init__(self) -> None:
        self.attempt_count = 0

    async def publish(self, *_args: object, **_kwargs: object) -> None:
        self.attempt_count += 1
        if self.attempt_count == 1:
            raise OSError("RabbitMQ was unavailable for the first attempt.")


async def test_concurrent_identical_retry_requests_share_one_recovery_job(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-idempotency-retry@example.com", is_admin=True)
    outbox = _failed_outbox("concurrent-retry")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(
        RecoveryWorkKind.OUTBOX,
        str(outbox.id),
    )
    request_id = uuid.uuid7()
    payload = RecoveryMutationRequest(
        request_id=request_id,
        version=work.version,
        reason="Retry after the broker recovered.",
        capability=RecoveryCapability.REBUILD_OUTBOX,
    )
    barrier = asyncio.Barrier(2)

    async def create() -> RecoveryJobRead:
        async with postgres_session_factory() as session:
            result = await _FirstLookupBarrierRecoveryService(session, barrier).retry_work(
                admin_user_id=admin.id,
                kind=RecoveryWorkKind.OUTBOX,
                work_id=str(outbox.id),
                payload=payload,
            )
            assert await session.scalar(select(RecoveryJob.id).where(RecoveryJob.id == result.id)) == result.id
            return result

    first, second = await asyncio.gather(create(), create())
    assert first.id == second.id

    async with postgres_session_factory() as session:
        jobs = (
            (
                await session.execute(
                    select(RecoveryJob).where(
                        RecoveryJob.requested_by_admin_user_id == admin.id,
                        RecoveryJob.request_id == request_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        items = (
            (await session.execute(select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == first.id)))
            .scalars()
            .all()
        )
        audits = (
            (
                await session.execute(
                    select(OperationalAuditLog).where(
                        OperationalAuditLog.admin_user_id == admin.id,
                        OperationalAuditLog.request_id == request_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 1
    assert len(items) == 1
    assert len(audits) == 1


async def test_concurrent_non_stage_actions_reserve_one_canonical_target(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-non-stage-race@example.com", is_admin=True)
    outbox = _failed_outbox("non-stage-action-race")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(
        RecoveryWorkKind.OUTBOX,
        str(outbox.id),
    )
    barrier = asyncio.Barrier(2)

    async def create() -> RecoveryJobRead | AdminRecoveryConflictError:
        async with postgres_session_factory() as session:
            try:
                return await _CandidateBarrierRecoveryService(session, barrier).perform_action(
                    admin_user_id=admin.id,
                    kind=RecoveryWorkKind.OUTBOX,
                    work_id=str(outbox.id),
                    payload=RecoveryActionRequest(
                        request_id=uuid.uuid7(),
                        version=work.version,
                        reason="Race two independently requested outbox repairs.",
                        action=RecoveryCapability.REBUILD_OUTBOX,
                    ),
                )
            except AdminRecoveryConflictError as exc:
                return exc

    outcomes = await asyncio.gather(create(), create())
    successes = [outcome for outcome in outcomes if isinstance(outcome, RecoveryJobRead)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, AdminRecoveryConflictError)]
    assert len(successes) == len(conflicts) == 1
    assert str(conflicts[0]) == "This recovery work already has an active Replay & Repair job."

    async with postgres_session_factory() as session:
        items = (
            (
                await session.execute(
                    select(RecoveryJobItem).where(
                        RecoveryJobItem.work_kind == RecoveryWorkKind.OUTBOX,
                        RecoveryJobItem.work_id == str(outbox.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(items) == 1
        assert items[0].reservation_active is True


async def test_concurrent_identical_batch_previews_share_one_recovery_job(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-idempotency-preview@example.com", is_admin=True)
    outbox = _failed_outbox("concurrent-preview")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(
        RecoveryWorkKind.OUTBOX,
        str(outbox.id),
    )
    request_id = uuid.uuid7()
    payload = RecoveryBatchPreviewRequest(
        request_id=request_id,
        action=RecoveryCapability.REBUILD_OUTBOX,
        reason="Preview the broker recovery batch.",
        selector=RecoveryExplicitSelector(
            items=[
                RecoveryWorkReference(
                    kind=RecoveryWorkKind.OUTBOX,
                    id=str(outbox.id),
                    version=work.version,
                )
            ]
        ),
    )
    barrier = asyncio.Barrier(2)

    async def create() -> RecoveryJobRead:
        async with postgres_session_factory() as session:
            result = await _FirstLookupBarrierRecoveryService(session, barrier).preview_batch(
                admin_user_id=admin.id,
                payload=payload,
            )
            assert await session.scalar(select(RecoveryJob.id).where(RecoveryJob.id == result.id)) == result.id
            return result

    first, second = await asyncio.gather(create(), create())
    assert first.id == second.id
    assert first.status is second.status is RecoveryJobStatus.PREVIEW

    async with postgres_session_factory() as session:
        service = AdminRecoveryService(session)
        scheduled = await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=first.id,
            version=first.version,
            reason="Approve the reviewed recovery batch.",
        )
        exact_replay = await service.preview_batch(admin_user_id=admin.id, payload=payload)
        assert exact_replay.id == scheduled.id
        assert exact_replay.status is RecoveryJobStatus.QUEUED
        assert exact_replay.reason == payload.reason

    async with postgres_session_factory() as session:
        jobs = (
            (
                await session.execute(
                    select(RecoveryJob).where(
                        RecoveryJob.requested_by_admin_user_id == admin.id,
                        RecoveryJob.request_id == request_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        items = (
            (await session.execute(select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == first.id)))
            .scalars()
            .all()
        )
    assert len(jobs) == 1
    assert len(items) == 1


async def test_concurrent_identical_preview_scheduling_is_idempotent(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-identical-schedule@example.com", is_admin=True)
    outbox = _failed_outbox("identical-schedule")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    work = await service.get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))
    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REBUILD_OUTBOX,
            reason="Preview one non-stage target before concurrent approval.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.OUTBOX,
                        id=str(outbox.id),
                        version=work.version,
                    )
                ]
            ),
        ),
    )

    async def schedule() -> RecoveryJobRead:
        async with postgres_session_factory() as session:
            return await AdminRecoveryService(session).schedule_batch(
                admin_user_id=admin.id,
                job_id=preview.id,
                version=preview.version,
                reason="Approve the same reviewed preview concurrently.",
            )

    first, second = await asyncio.gather(schedule(), schedule())
    assert first.id == second.id == preview.id
    assert first.status is second.status is RecoveryJobStatus.QUEUED
    async with postgres_session_factory() as session:
        item = await session.scalar(
            select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id)
        )
        audit_count = await session.scalar(
            select(func.count(OperationalAuditLog.id)).where(
                OperationalAuditLog.action == "schedule_recovery_batch",
                OperationalAuditLog.target_id == str(preview.id),
            )
        )
        assert item is not None
        assert item.is_root is True
        assert item.status is RecoveryJobItemStatus.QUEUED
        assert item.reservation_active is True
        assert audit_count == 1


async def test_concurrent_distinct_preview_scheduling_reserves_one_non_stage_target(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-distinct-schedule@example.com", is_admin=True)
    outbox = _failed_outbox("distinct-schedule")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    work = await service.get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))

    async def preview() -> RecoveryJobRead:
        return await service.preview_batch(
            admin_user_id=admin.id,
            payload=RecoveryBatchPreviewRequest(
                request_id=uuid.uuid7(),
                action=RecoveryCapability.REBUILD_OUTBOX,
                reason="Create an independent preview for the same outbox repair.",
                selector=RecoveryExplicitSelector(
                    items=[
                        RecoveryWorkReference(
                            kind=RecoveryWorkKind.OUTBOX,
                            id=str(outbox.id),
                            version=work.version,
                        )
                    ]
                ),
            ),
        )

    previews = (await preview(), await preview())
    barrier = asyncio.Barrier(2)

    async def schedule(preview_job: RecoveryJobRead) -> RecoveryJobRead | AdminRecoveryConflictError:
        async with postgres_session_factory() as session:
            try:
                return await _PreviewValidationBarrierRecoveryService(session, barrier).schedule_batch(
                    admin_user_id=admin.id,
                    job_id=preview_job.id,
                    version=preview_job.version,
                    reason="Race approval of two distinct previews.",
                )
            except AdminRecoveryConflictError as exc:
                return exc

    outcomes = await asyncio.gather(*(schedule(preview_job) for preview_job in previews))
    successes = [outcome for outcome in outcomes if isinstance(outcome, RecoveryJobRead)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, AdminRecoveryConflictError)]
    assert len(successes) == len(conflicts) == 1
    assert str(conflicts[0]) == "One or more recovery targets already have an active Replay & Repair job."

    async with postgres_session_factory() as session:
        jobs = (
            (await session.execute(select(RecoveryJob).where(RecoveryJob.id.in_([item.id for item in previews]))))
            .scalars()
            .all()
        )
        items = (
            (
                await session.execute(
                    select(RecoveryJobItem).where(
                        RecoveryJobItem.recovery_job_id.in_([item.id for item in previews])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {job.status for job in jobs} == {RecoveryJobStatus.PREVIEW, RecoveryJobStatus.QUEUED}
        assert sum(item.reservation_active for item in items) == 1


async def test_recovery_request_id_reuse_requires_matching_fingerprint(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="recovery-idempotency-mismatch@example.com", is_admin=True)
    first_outbox = _failed_outbox("fingerprint-first")
    second_outbox = _failed_outbox("fingerprint-second")
    migrated_db_session.add_all((admin, first_outbox, second_outbox))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    first_work = await service.get_work(RecoveryWorkKind.OUTBOX, str(first_outbox.id))
    second_work = await service.get_work(RecoveryWorkKind.OUTBOX, str(second_outbox.id))
    request_id = uuid.uuid7()
    original = RecoveryMutationRequest(
        request_id=request_id,
        version=first_work.version,
        reason="Retry the original broker operation.",
        capability=RecoveryCapability.REBUILD_OUTBOX,
    )
    created = await service.retry_work(
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(first_outbox.id),
        payload=original,
    )
    persisted = await migrated_db_session.get(RecoveryJob, created.id)
    assert persisted is not None
    persisted.status = RecoveryJobStatus.RUNNING
    await migrated_db_session.commit()

    exact_replay = await service.retry_work(
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(first_outbox.id),
        payload=original,
    )
    assert exact_replay.id == created.id
    assert exact_replay.status is RecoveryJobStatus.RUNNING

    mismatched_requests = (
        (
            str(first_outbox.id),
            RecoveryMutationRequest(
                request_id=request_id,
                version=first_work.version,
                reason="Use a different reason for the same operation.",
                capability=RecoveryCapability.REBUILD_OUTBOX,
            ),
        ),
        (
            str(second_outbox.id),
            RecoveryMutationRequest(
                request_id=request_id,
                version=second_work.version,
                reason=original.reason,
                capability=RecoveryCapability.REBUILD_OUTBOX,
            ),
        ),
        (
            str(first_outbox.id),
            RecoveryMutationRequest(
                request_id=request_id,
                version=first_work.version,
                reason=original.reason,
                capability=RecoveryCapability.RECOVER_DEAD_LETTER,
            ),
        ),
    )
    for work_id, payload in mismatched_requests:
        with pytest.raises(
            AdminRecoveryConflictError,
            match="request ID was already used for a different recovery request",
        ):
            await service.retry_work(
                admin_user_id=admin.id,
                kind=RecoveryWorkKind.OUTBOX,
                work_id=work_id,
                payload=payload,
            )


async def test_batch_preview_rejects_duplicate_work_references(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="recovery-duplicate-preview@example.com", is_admin=True)
    outbox = _failed_outbox("duplicate-preview")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    work = await service.get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))
    reference = RecoveryWorkReference(
        kind=RecoveryWorkKind.OUTBOX,
        id=str(outbox.id),
        version=work.version,
    )

    with pytest.raises(AdminRecoveryConflictError, match="same work item more than once"):
        await service.preview_batch(
            admin_user_id=admin.id,
            payload=RecoveryBatchPreviewRequest(
                request_id=uuid.uuid7(),
                action=RecoveryCapability.REBUILD_OUTBOX,
                reason="Reject a duplicate recovery selection.",
                selector=RecoveryExplicitSelector(items=[reference, reference]),
            ),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"snapshot_at": utcnow().replace(tzinfo=None).isoformat(), "key": [1, "time", "kind", "id"]},
        {"snapshot_at": utcnow().isoformat(), "key": [1, "too-short"]},
    ],
)
async def test_recovery_list_rejects_malformed_cursor_shape_and_naive_time(
    migrated_db_session: AsyncSession,
    payload: dict[str, object],
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(AdminRecoveryConflictError, match="cursor is invalid"):
        await AdminRecoveryService(migrated_db_session).list_work(cursor=cursor)


async def test_dead_letter_recovery_requires_the_same_canonical_event_generation(
    migrated_db_session: AsyncSession,
) -> None:
    file = await _seed_meme_file(migrated_db_session)
    current_event_id = uuid.uuid7()
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=5,
        last_event_id=current_event_id,
        normalized_reason="ocr_timeout",
        last_error_text="OCR timed out.",
        is_retryable=True,
        finished_at=utcnow(),
    )
    migrated_db_session.add(stage)
    await migrated_db_session.flush()
    dead_letter = PipelineDeadLetter(
        deduplication_key=uuid.uuid7().hex,
        broker_message_id=str(uuid.uuid7()),
        payload_hash="a" * 64,
        event_type="meme_transcoded",
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        normalized_reason="ocr_timeout",
        death_count=5,
        safe_payload={"event_id": str(uuid.uuid7())},
        safe_headers={},
        status=RecoveryDeadLetterStatus.UNRESOLVED,
    )
    migrated_db_session.add(dead_letter)
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)

    mismatched = await service.list_work()
    stage_work = next(item for item in mismatched.items if item.kind is RecoveryWorkKind.PIPELINE_STAGE)
    dead_letter_work = next(item for item in mismatched.items if item.kind is RecoveryWorkKind.DEAD_LETTER)
    assert stage_work.capabilities == [RecoveryCapability.RETRY_STAGE]
    assert dead_letter_work.capabilities == [RecoveryCapability.ARCHIVE_DEAD_LETTER]
    assert dead_letter_work.is_retryable is False

    await migrated_db_session.execute(
        update(PipelineDeadLetter)
        .where(PipelineDeadLetter.id == dead_letter.id)
        .values(safe_payload={"event_id": str(current_event_id)})
    )
    await migrated_db_session.commit()
    matched = await service.list_work()
    promoted = next(item for item in matched.items if item.kind is RecoveryWorkKind.PIPELINE_STAGE)
    assert promoted.bucket.value == "dead_lettered"
    assert promoted.capabilities == [RecoveryCapability.RECOVER_DEAD_LETTER]
    assert ":dead-letter:" in promoted.version
    assert not any(item.kind is RecoveryWorkKind.DEAD_LETTER for item in matched.items)


async def test_concurrent_schedule_and_cancel_are_serialized(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-schedule-cancel@example.com", is_admin=True)
    outbox = _failed_outbox("schedule-cancel")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    work = await service.get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))
    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REBUILD_OUTBOX,
            reason="Preview a serialized transition.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.OUTBOX,
                        id=str(outbox.id),
                        version=work.version,
                    )
                ]
            ),
        ),
    )

    async def schedule() -> RecoveryJobStatus | str:
        async with postgres_session_factory() as session:
            try:
                result = await AdminRecoveryService(session).schedule_batch(
                    admin_user_id=admin.id,
                    job_id=preview.id,
                    version=preview.version,
                    reason="Schedule the reviewed batch.",
                )
            except AdminRecoveryConflictError:
                return "conflict"
            return result.status

    async def cancel() -> RecoveryJobStatus | str:
        async with postgres_session_factory() as session:
            try:
                result = await AdminRecoveryService(session).cancel_batch(
                    admin_user_id=admin.id,
                    job_id=preview.id,
                    version=preview.version,
                    reason="Cancel the reviewed batch.",
                )
            except AdminRecoveryConflictError:
                return "conflict"
            return result.status

    outcomes = await asyncio.gather(schedule(), cancel())
    assert outcomes.count("conflict") == 1
    async with postgres_session_factory() as session:
        job = await session.get(RecoveryJob, preview.id)
        item = await session.scalar(select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id))
        assert job is not None
        assert item is not None
        if job.status is RecoveryJobStatus.CANCELLED:
            assert item.status is RecoveryJobItemStatus.CANCELLED
        else:
            assert job.status is RecoveryJobStatus.QUEUED
            assert item.status is RecoveryJobItemStatus.QUEUED


async def test_recovery_dispatch_requeues_failed_outbox_and_completes_job(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-runtime@example.com", is_admin=True)
    outbox = RabbitMQOutboxMessage(
        exchange="memexpert.pipeline",
        routing_key="pipeline.ocr",
        payload={"event_type": "meme_transcoded"},
        headers={},
        message_id=str(uuid.uuid7()),
        event_type="meme_transcoded",
        aggregate_type="meme_file",
        aggregate_id=str(uuid.uuid7()),
        status=RabbitMQOutboxMessageStatus.FAILED,
        attempt_count=5,
        last_error_text="broker unavailable",
    )
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()

    work = await AdminRecoveryService(migrated_db_session).get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.QUEUED,
        action=RecoveryCapability.REBUILD_OUTBOX,
        reason="Retry the broker publish after recovery.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        work_kind=RecoveryWorkKind.OUTBOX,
        work_id=str(outbox.id),
        action=RecoveryCapability.REBUILD_OUTBOX,
        expected_version=work.version,
        reservation_active=True,
    )
    migrated_db_session.add(item)
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    result = await runtime.dispatch_general_batch(batch_size=10)

    async with postgres_session_factory() as session:
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_outbox is not None
        assert persisted_outbox.status is RabbitMQOutboxMessageStatus.PENDING
        assert persisted_outbox.locked_at is None
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED
        assert persisted_item.dispatch_event_id == uuid.UUID(outbox.message_id)
        assert persisted_item.reservation_active is True
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.RUNNING
        assert persisted_job.completed_count == 0
        assert persisted_job.failed_count == 0
    assert result.claimed == 1
    assert result.dispatched == 1

    async with postgres_session_factory() as session:
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox.id, with_for_update=True)
        assert persisted_outbox is not None
        persisted_outbox.status = RabbitMQOutboxMessageStatus.PUBLISHED
        persisted_outbox.published_at = utcnow()
        await session.commit()

    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SUCCEEDED
        assert persisted_item.reservation_active is False
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED
        assert persisted_job.completed_count == 1
        assert persisted_job.failed_count == 0


async def test_dead_letter_ledger_deduplicates_redelivery_and_keeps_highest_death_count(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = {
        "event_id": str(uuid.uuid7()),
        "event_type": "meme_ocr_done",
        "meme_file_id": str(uuid.uuid7()),
        "stage": ContentPipelineStage.EMBED.value,
    }
    first_id = await record_pipeline_dead_letter(
        postgres_session_factory,
        payload=payload,
        headers={"authorization": "secret", "x-death": [{"count": 1}]},
        broker_message_id="broker-message-1",
        normalized_reason="embed_provider_blocked",
    )
    second_id = await record_pipeline_dead_letter(
        postgres_session_factory,
        payload=payload,
        headers={"authorization": "secret", "x-death": [{"count": 4}]},
        broker_message_id="broker-message-1",
        normalized_reason="embed_provider_blocked",
    )

    async with postgres_session_factory() as session:
        rows = (await session.execute(select(PipelineDeadLetter))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == first_id == second_id
        assert rows[0].death_count == 5
        assert rows[0].safe_headers["authorization"] == "[redacted]"

    async with postgres_session_factory() as session:
        await session.execute(
            update(PipelineDeadLetter)
            .where(PipelineDeadLetter.id == first_id)
            .values(
                status=RecoveryDeadLetterStatus.RESOLVED,
                resolved_at=utcnow(),
                resolution_note="Recovered once.",
            )
        )
        await session.commit()

    third_id = await record_pipeline_dead_letter(
        postgres_session_factory,
        payload=payload,
        headers={"x-death": [{"count": 1}]},
        broker_message_id="broker-message-1",
        normalized_reason="embed_provider_blocked",
    )
    async with postgres_session_factory() as session:
        reopened = await session.get(PipelineDeadLetter, third_id)
        assert reopened is not None
        assert reopened.id == first_id
        assert reopened.status is RecoveryDeadLetterStatus.UNRESOLVED
        assert reopened.resolved_at is None
        assert reopened.resolution_note is None


async def test_capacity_refresh_closes_and_reopens_with_hysteresis(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(2):
        file = await _seed_meme_file(migrated_db_session)
        migrated_db_session.add(
            PipelineStageJournal(
                meme_file_id=file.id,
                stage=ContentPipelineStage.OCR,
                status=ContentPipelineStageStatus.PENDING,
                attempt_count=0,
                is_retryable=True,
            )
        )
    await migrated_db_session.commit()
    policy = PipelineCapacityPolicy(
        close_pending_count=2,
        reopen_pending_count=1,
        close_oldest_age_seconds=3600,
        reopen_oldest_age_seconds=60,
    )

    closed_result = await refresh_pipeline_capacity_states(postgres_session_factory, policy=policy)
    assert ContentPipelineStage.OCR in closed_result.closed_stages

    async with postgres_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(PipelineStageJournal).where(PipelineStageJournal.stage == ContentPipelineStage.OCR)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = ContentPipelineStageStatus.SUCCEEDED
        await session.commit()

    reopened_result = await refresh_pipeline_capacity_states(postgres_session_factory, policy=policy)
    assert ContentPipelineStage.OCR not in reopened_result.closed_stages
    async with postgres_session_factory() as session:
        state = await session.scalar(
            select(PipelineCapacityState).where(PipelineCapacityState.stage == ContentPipelineStage.OCR)
        )
        assert state is not None
        assert state.status is PipelineCapacityStatus.OPEN
        assert state.pending_count == 0


async def test_dependency_circuit_opens_allows_one_probe_and_closes_on_success(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    error = RuntimeError("provider unavailable")
    for _ in range(2):
        await record_dependency_failure(
            postgres_session_factory,
            dependency="voyage",
            error=error,
            failure_threshold=2,
            cooldown_seconds=30,
        )

    with pytest.raises(DependencyCircuitOpenError):
        await acquire_dependency_circuit(
            postgres_session_factory,
            dependency="voyage",
            owner="worker-a",
        )

    async with postgres_session_factory() as session:
        row = await session.scalar(select(DependencyCircuitState).where(DependencyCircuitState.dependency == "voyage"))
        assert row is not None
        assert row.status is DependencyCircuitStatus.OPEN
        row.retry_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    await acquire_dependency_circuit(
        postgres_session_factory,
        dependency="voyage",
        owner="worker-a",
    )
    with pytest.raises(DependencyCircuitOpenError):
        await acquire_dependency_circuit(
            postgres_session_factory,
            dependency="voyage",
            owner="worker-b",
        )
    await record_dependency_success(postgres_session_factory, dependency="voyage")

    async with postgres_session_factory() as session:
        row = await session.scalar(select(DependencyCircuitState).where(DependencyCircuitState.dependency == "voyage"))
        assert row is not None
        assert row.status is DependencyCircuitStatus.CLOSED
        assert row.consecutive_failures == 0


async def test_stage_completion_preserves_each_attempt_outcome(
    migrated_db_session: AsyncSession,
) -> None:
    file = await _seed_meme_file(migrated_db_session)
    event_id = uuid.uuid7()
    migrated_db_session.add(
        PipelineStageJournal(
            meme_file_id=file.id,
            stage=ContentPipelineStage.TRANSCODE,
            status=ContentPipelineStageStatus.PENDING,
            attempt_count=0,
            last_event_id=event_id,
            is_retryable=True,
        )
    )
    await migrated_db_session.commit()
    service = PipelineStageCompletionService(
        migrated_db_session,
        settings=Settings(),
        worker_role="media",
        worker_instance_id="worker-test",
    )

    _ = await service.start_stage_processing(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        attempt=1,
        event_id=event_id,
    )
    await service.mark_stage_failed(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        attempt=1,
        event_id=event_id,
        normalized_reason="transcode_timeout",
        last_error_text="ffmpeg exceeded its deadline",
        retryable=True,
    )

    attempt = await migrated_db_session.scalar(
        select(PipelineStageAttempt).where(
            PipelineStageAttempt.meme_file_id == file.id,
            PipelineStageAttempt.event_id == event_id,
            PipelineStageAttempt.attempt_number == 1,
        )
    )
    assert attempt is not None
    assert attempt.outcome is PipelineAttemptOutcome.FAILED_RETRYABLE
    assert attempt.normalized_reason == "transcode_timeout"
    assert attempt.worker_role == "media"
    assert attempt.worker_instance_id == "worker-test"
    assert attempt.finished_at is not None


async def test_stuck_stage_reconciler_reconstructs_a_lost_dispatch(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = await _seed_meme_file(migrated_db_session)
    old_event_id = uuid.uuid7()
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.PROCESSING,
        attempt_count=1,
        last_event_id=old_event_id,
        is_retryable=True,
        started_at=utcnow() - timedelta(hours=1),
        updated_at=utcnow() - timedelta(hours=1),
    )
    migrated_db_session.add(stage)
    await migrated_db_session.commit()

    async def _skip_immediate_relay(self: object, message_ids: tuple[uuid.UUID, ...]) -> None:
        _ = (self, message_ids)

    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )
    runtime = RecoveryRuntime(
        session_factory=postgres_session_factory,
        settings=Settings(pipeline_stuck_reclaim_after_seconds=61),
    )
    assert await runtime.reclaim_stuck_work(batch_size=1) == 1

    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        assert persisted_stage is not None
        assert persisted_stage.status is ContentPipelineStageStatus.PENDING
        assert persisted_stage.last_event_id is not None
        assert persisted_stage.last_event_id != old_event_id
        assert persisted_stage.attempt_count == 2
        outbox = await session.scalar(
            select(RabbitMQOutboxMessage).where(RabbitMQOutboxMessage.message_id == str(persisted_stage.last_event_id))
        )
        assert outbox is not None
        assert outbox.status is RabbitMQOutboxMessageStatus.PENDING


def _ingest_request_for_reclaim(*, status: PipelineIngestRequestStatus) -> PipelineIngestRequest:
    identifier = uuid.uuid7().hex
    return PipelineIngestRequest(
        source_platform=SourcePlatform.TELEGRAM,
        source_id=f"channel-{identifier}",
        post_id=f"post-{identifier}",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
        user_metadata={},
        source_metadata={},
        temp_original_object_key=f"tests/recovery/{identifier}.jpg",
        sha256_hex="a" * 64,
        status=status,
        attempt_count=1,
        updated_at=utcnow() - timedelta(hours=1),
    )


def _media_inspect_outbox(
    request: PipelineIngestRequest,
    *,
    status: RabbitMQOutboxMessageStatus,
) -> RabbitMQOutboxMessage:
    return RabbitMQOutboxMessage(
        exchange="memexpert.pipeline",
        routing_key="pipeline.media_inspect",
        payload={"event_type": MEDIA_INSPECT_REQUESTED_EVENT_TYPE, "ingest_request_id": str(request.id)},
        headers={},
        message_id=str(uuid.uuid7()),
        event_type=MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
        aggregate_type=PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
        aggregate_id=str(request.id),
        status=status,
        published_at=utcnow() if status is RabbitMQOutboxMessageStatus.PUBLISHED else None,
    )


def _dead_letter_for(
    kind: RecoveryWorkKind,
    work_id: str,
    *,
    event_id: uuid.UUID | None = None,
    status: RecoveryDeadLetterStatus = RecoveryDeadLetterStatus.UNRESOLVED,
) -> PipelineDeadLetter:
    return PipelineDeadLetter(
        deduplication_key=uuid.uuid7().hex,
        broker_message_id=str(uuid.uuid7()),
        payload_hash="d" * 64,
        event_type="recovery_test_event",
        work_kind=kind,
        work_id=work_id,
        normalized_reason="recovery_test_failure",
        death_count=5,
        safe_payload={"event_id": str(event_id)} if event_id is not None else {},
        safe_headers={},
        status=status,
    )


async def _seed_recovery_job_item(
    session: AsyncSession,
    *,
    admin_user_id: uuid.UUID,
    kind: RecoveryWorkKind,
    work_id: str,
    action: RecoveryCapability,
    expected_version: str,
    status: RecoveryJobItemStatus = RecoveryJobItemStatus.QUEUED,
    dispatch_event_id: uuid.UUID | None = None,
) -> tuple[RecoveryJob, RecoveryJobItem]:
    job = RecoveryJob(
        requested_by_admin_user_id=admin_user_id,
        request_id=uuid.uuid7(),
        status=(RecoveryJobStatus.RUNNING if status is RecoveryJobItemStatus.DISPATCHED else RecoveryJobStatus.QUEUED),
        action=action,
        reason="Exercise durable recovery behavior.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    session.add(job)
    await session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        work_kind=kind,
        work_id=work_id,
        action=action,
        expected_version=expected_version,
        status=status,
        dispatch_event_id=dispatch_event_id,
        dispatched_at=utcnow() if status is RecoveryJobItemStatus.DISPATCHED else None,
    )
    session.add(item)
    await session.flush()
    return job, item


async def _skip_immediate_relay(self: object, message_ids: tuple[uuid.UUID, ...]) -> None:
    _ = (self, message_ids)


async def test_stuck_reclaimer_does_not_replay_old_pending_stage(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = await _seed_meme_file(migrated_db_session)
    event_id = uuid.uuid7()
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.PENDING,
        attempt_count=1,
        last_event_id=event_id,
        is_retryable=True,
        updated_at=utcnow() - timedelta(hours=1),
    )
    migrated_db_session.add(stage)
    await migrated_db_session.commit()
    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )

    runtime = RecoveryRuntime(
        session_factory=postgres_session_factory,
        settings=Settings(pipeline_stuck_reclaim_after_seconds=61),
    )
    assert await runtime.reclaim_stuck_work(batch_size=10) == 0

    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        outbox_rows = (await session.execute(select(RabbitMQOutboxMessage))).scalars().all()
        assert persisted_stage is not None
        assert persisted_stage.status is ContentPipelineStageStatus.PENDING
        assert persisted_stage.last_event_id == event_id
        assert persisted_stage.attempt_count == 1
        assert outbox_rows == []


@pytest.mark.parametrize(
    "outbox_status",
    [
        RabbitMQOutboxMessageStatus.PENDING,
        RabbitMQOutboxMessageStatus.PUBLISHING,
        RabbitMQOutboxMessageStatus.PUBLISHED,
    ],
)
async def test_stuck_reclaimer_does_not_replay_old_pending_ingest_backlog(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    outbox_status: RabbitMQOutboxMessageStatus,
) -> None:
    request = _ingest_request_for_reclaim(status=PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING)
    migrated_db_session.add(request)
    await migrated_db_session.flush()
    outbox = _media_inspect_outbox(request, status=outbox_status)
    migrated_db_session.add(outbox)
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(
        session_factory=postgres_session_factory,
        settings=Settings(pipeline_stuck_reclaim_after_seconds=61),
    )
    assert await runtime.reclaim_stuck_work(batch_size=10) == 0

    async with postgres_session_factory() as session:
        persisted_request = await session.get(PipelineIngestRequest, request.id)
        outbox_rows = (
            (
                await session.execute(
                    select(RabbitMQOutboxMessage).where(
                        RabbitMQOutboxMessage.aggregate_type == PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
                        RabbitMQOutboxMessage.aggregate_id == str(request.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_request is not None
        assert persisted_request.status is PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
        assert [row.id for row in outbox_rows] == [outbox.id]


async def test_stuck_ingest_reclaimer_reuses_existing_published_event(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _ingest_request_for_reclaim(status=PipelineIngestRequestStatus.MEDIA_INSPECTING)
    migrated_db_session.add(request)
    await migrated_db_session.flush()
    outbox = _media_inspect_outbox(request, status=RabbitMQOutboxMessageStatus.PUBLISHED)
    migrated_db_session.add(outbox)
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(
        session_factory=postgres_session_factory,
        settings=Settings(pipeline_stuck_reclaim_after_seconds=61),
    )
    assert await runtime.reclaim_stuck_work(batch_size=1) == 1

    async with postgres_session_factory() as session:
        persisted_request = await session.get(PipelineIngestRequest, request.id)
        outbox_rows = (
            (
                await session.execute(
                    select(RabbitMQOutboxMessage).where(
                        RabbitMQOutboxMessage.aggregate_type == PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
                        RabbitMQOutboxMessage.aggregate_id == str(request.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_request is not None
        assert persisted_request.status is PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
        assert [row.id for row in outbox_rows] == [outbox.id]


async def test_concurrent_recovery_dispatches_replay_one_canonical_stage(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-concurrent-dispatch@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        normalized_reason="ocr_timeout",
        last_error_text="OCR timed out.",
        is_retryable=True,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stage.id),
    )
    first_job, first_item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.RETRY_STAGE,
        expected_version=work.version,
    )
    second_job, second_item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.RETRY_STAGE,
        expected_version=work.version,
    )
    await migrated_db_session.commit()
    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())

    first_result, second_result = await asyncio.gather(
        runtime.dispatch_general_batch(batch_size=1),
        runtime.dispatch_general_batch(batch_size=1),
    )

    assert first_result.claimed == second_result.claimed == 1
    assert first_result.dispatched + second_result.dispatched == 1
    assert first_result.skipped_stale + second_result.skipped_stale == 1
    async with postgres_session_factory() as session:
        items = (
            (
                await session.execute(
                    select(RecoveryJobItem).where(RecoveryJobItem.id.in_((first_item.id, second_item.id)))
                )
            )
            .scalars()
            .all()
        )
        outbox_rows = (
            (
                await session.execute(
                    select(RabbitMQOutboxMessage).where(RabbitMQOutboxMessage.aggregate_id == str(file.id))
                )
            )
            .scalars()
            .all()
        )
        jobs = (
            (await session.execute(select(RecoveryJob).where(RecoveryJob.id.in_((first_job.id, second_job.id)))))
            .scalars()
            .all()
        )
        assert sorted(item.status.value for item in items) == [
            RecoveryJobItemStatus.DISPATCHED.value,
            RecoveryJobItemStatus.SKIPPED_STALE.value,
        ]
        assert len(outbox_rows) == 1
        assert sorted(job.status.value for job in jobs) == [RecoveryJobStatus.RUNNING.value] * 2


@pytest.mark.parametrize(
    ("terminal_status", "expected_item_status", "expected_dead_letter_status"),
    [
        (
            RabbitMQOutboxMessageStatus.PUBLISHED,
            RecoveryJobItemStatus.SUCCEEDED,
            RecoveryDeadLetterStatus.RESOLVED,
        ),
        (
            RabbitMQOutboxMessageStatus.FAILED,
            RecoveryJobItemStatus.FAILED,
            RecoveryDeadLetterStatus.UNRESOLVED,
        ),
    ],
    ids=("published", "terminal-failure"),
)
async def test_linked_outbox_dead_letter_waits_for_publish_outcome(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    terminal_status: RabbitMQOutboxMessageStatus,
    expected_item_status: RecoveryJobItemStatus,
    expected_dead_letter_status: RecoveryDeadLetterStatus,
) -> None:
    settings = Settings(pipeline_broker_retry_max_attempts=5)
    admin = User(email=f"recovery-linked-outbox-{terminal_status.value}@example.com", is_admin=True)
    outbox = _failed_outbox(f"linked-{terminal_status.value}")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.flush()
    dead_letter = _dead_letter_for(RecoveryWorkKind.OUTBOX, str(outbox.id))
    migrated_db_session.add(dead_letter)
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))
    assert work.capabilities == [RecoveryCapability.RECOVER_DEAD_LETTER]
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(outbox.id),
        action=RecoveryCapability.RECOVER_DEAD_LETTER,
        expected_version=work.version,
    )
    await migrated_db_session.commit()
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=settings)

    result = await runtime.dispatch_general_batch(batch_size=1)
    assert result.claimed == result.dispatched == 1
    async with postgres_session_factory() as session:
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox.id)
        persisted_dead_letter = await session.get(PipelineDeadLetter, dead_letter.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_outbox is not None
        assert persisted_outbox.status is RabbitMQOutboxMessageStatus.PENDING
        assert persisted_dead_letter is not None
        assert persisted_dead_letter.status is RecoveryDeadLetterStatus.RECOVERY_QUEUED
        assert persisted_dead_letter.recovery_item_id == item.id
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.RUNNING

    async with postgres_session_factory() as session:
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox.id, with_for_update=True)
        assert persisted_outbox is not None
        persisted_outbox.status = terminal_status
        if terminal_status is RabbitMQOutboxMessageStatus.PUBLISHED:
            persisted_outbox.published_at = utcnow()
        else:
            persisted_outbox.attempt_count = settings.pipeline_broker_retry_max_attempts + item.retry_limit
            persisted_outbox.last_error_text = "Broker remained unavailable."
        await session.commit()

    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_dead_letter = await session.get(PipelineDeadLetter, dead_letter.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_dead_letter is not None
        assert persisted_dead_letter.status is expected_dead_letter_status
        assert persisted_item is not None
        assert persisted_item.status is expected_item_status
        assert persisted_job is not None
        assert persisted_job.status is (
            RecoveryJobStatus.COMPLETED
            if expected_item_status is RecoveryJobItemStatus.SUCCEEDED
            else RecoveryJobStatus.COMPLETED_WITH_FAILURES
        )


@pytest.mark.parametrize("canonical_change", ["deleted", "superseded"])
async def test_reconciliation_terminalizes_missing_or_superseded_stage(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    canonical_change: str,
) -> None:
    admin = User(email=f"recovery-stale-{canonical_change}@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    dispatch_event_id = uuid.uuid7()
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.PENDING,
        attempt_count=2,
        last_event_id=dispatch_event_id,
        is_retryable=True,
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.RETRY_STAGE,
        expected_version="dispatched-version",
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatch_event_id=dispatch_event_id,
    )
    await migrated_db_session.commit()

    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id, with_for_update=True)
        assert persisted_stage is not None
        if canonical_change == "deleted":
            await session.delete(persisted_stage)
        else:
            persisted_stage.last_event_id = uuid.uuid7()
        await session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SKIPPED_STALE
        assert persisted_item.normalized_reason == "canonical_state_changed"
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES


async def test_replay_preview_materializes_exact_stage_cascade_dependencies(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="recovery-cascade@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    stages = {
        stage: PipelineStageJournal(
            meme_file_id=file.id,
            stage=stage,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for stage in (
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stages[ContentPipelineStage.OCR].id),
    )

    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            retry_limit=3,
            reason="Replay OCR and every exact dependent stage.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.PIPELINE_STAGE,
                        id=candidate.work.id,
                        version=candidate.work.version,
                    )
                ]
            ),
        ),
    )

    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.selected_root_count == 1
    assert preview.expanded_execution_count == 5
    items = {
        item.stage: item
        for item in (
            (
                await migrated_db_session.execute(
                    select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id)
                )
            )
            .scalars()
            .all()
        )
    }
    assert set(items) == {
        ContentPipelineStage.OCR,
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    }
    assert items[ContentPipelineStage.OCR].parent_item_id is None
    assert items[ContentPipelineStage.EMBED].parent_item_id == items[ContentPipelineStage.OCR].id
    assert items[ContentPipelineStage.CLASSIFY].parent_item_id == items[ContentPipelineStage.EMBED].id
    assert items[ContentPipelineStage.SYNC_QDRANT].parent_item_id == items[ContentPipelineStage.CLASSIFY].id
    assert items[ContentPipelineStage.SYNC_MEILI].parent_item_id == items[ContentPipelineStage.CLASSIFY].id
    assert items[ContentPipelineStage.OCR].status is RecoveryJobItemStatus.QUEUED
    assert all(
        item.status is RecoveryJobItemStatus.WAITING_DEPENDENCY
        for stage, item in items.items()
        if stage is not ContentPipelineStage.OCR
    )
    assert all(item.preserve_ready and item.suppress_fanout for item in items.values())


async def test_replay_candidate_blocks_unsuccessful_prerequisite(
    migrated_db_session: AsyncSession,
) -> None:
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    embed = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.EMBED,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=3,
        is_retryable=True,
    )
    classify = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.CLASSIFY,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        is_retryable=False,
    )
    migrated_db_session.add_all((embed, classify))
    await migrated_db_session.commit()

    candidate = await AdminRecoveryService(migrated_db_session).get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(classify.id),
    )
    replay = next(action for action in candidate.actions if action.capability is RecoveryCapability.REPLAY_STAGE)
    assert replay.available is False
    assert replay.blocked_prerequisites == ["classify requires a successful embed prerequisite."]


async def test_static_ocr_candidate_blocks_missing_transcode_prerequisite(
    migrated_db_session: AsyncSession,
) -> None:
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        is_retryable=False,
    )
    migrated_db_session.add(ocr)
    await migrated_db_session.commit()

    candidate = await AdminRecoveryService(migrated_db_session).get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(ocr.id),
    )

    replay = next(action for action in candidate.actions if action.capability is RecoveryCapability.REPLAY_STAGE)
    assert replay.available is False
    assert replay.blocked_prerequisites == ["ocr requires a successful transcode prerequisite."]


@pytest.mark.parametrize(
    ("scope", "expected_count"),
    [
        (RecoveryReplayScope.STAGE_ONLY, 1),
        (RecoveryReplayScope.STAGE_AND_DEPENDENTS, 6),
    ],
)
async def test_moving_transcode_replay_uses_failure_safe_regeneration_root_for_every_scope(
    migrated_db_session: AsyncSession,
    scope: RecoveryReplayScope,
    expected_count: int,
) -> None:
    admin = User(email=f"moving-transcode-{scope.value}@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    file.mime_type = "video/webm"
    file.s3_web_video_key = f"pipeline/derived/{file.id}/web.mp4"
    stages = {
        stage: PipelineStageJournal(
            meme_file_id=file.id,
            stage=stage,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for stage in (
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stages[ContentPipelineStage.TRANSCODE].id),
    )
    replay = next(action for action in candidate.actions if action.capability is RecoveryCapability.REPLAY_STAGE)
    scope_requirements = replay.scope_requirements[scope]
    if scope is RecoveryReplayScope.STAGE_AND_DEPENDENTS:
        assert scope_requirements.risks == [
            "External provider output or semantic merge results may differ from the previous successful run."
        ]
    else:
        assert scope_requirements.risks == []

    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=scope,
            retry_limit=3,
            reason="Replay moving-media transcode with failure-safe activation.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.PIPELINE_STAGE,
                        id=candidate.work.id,
                        version=candidate.work.version,
                    )
                ]
            ),
        ),
    )
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id)
            )
        )
        .scalars()
        .all()
    )
    root = next(item for item in items if item.is_root)
    assert len(items) == expected_count
    assert root.stage is ContentPipelineStage.TRANSCODE
    assert root.action is RecoveryCapability.REGENERATE_DERIVATIVES
    assert all(item.action is RecoveryCapability.REPLAY_STAGE for item in items if not item.is_root)


async def test_cascade_preview_blocks_active_dependent_stage(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="cascade-active-dependent@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    stages = {
        stage: PipelineStageJournal(
            meme_file_id=file.id,
            stage=stage,
            status=(
                ContentPipelineStageStatus.PROCESSING
                if stage is ContentPipelineStage.EMBED
                else ContentPipelineStageStatus.SUCCEEDED
            ),
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=None if stage is ContentPipelineStage.EMBED else utcnow(),
        )
        for stage in (
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stages[ContentPipelineStage.OCR].id),
    )

    with pytest.raises(AdminRecoveryConflictError, match="Dependent stage embed is processing"):
        await service.preview_batch(
            admin_user_id=admin.id,
            payload=RecoveryBatchPreviewRequest(
                request_id=uuid.uuid7(),
                action=RecoveryCapability.REPLAY_STAGE,
                scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
                reason="Do not overlap an active dependent stage.",
                selector=RecoveryExplicitSelector(
                    items=[
                        RecoveryWorkReference(
                            kind=RecoveryWorkKind.PIPELINE_STAGE,
                            id=candidate.work.id,
                            version=candidate.work.version,
                        )
                    ]
                ),
            ),
        )


async def test_cascade_terminal_dependent_requires_override_acknowledgement(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="cascade-terminal-dependent@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    stages = {
        stage: PipelineStageJournal(
            meme_file_id=file.id,
            stage=stage,
            status=(
                ContentPipelineStageStatus.FAILED
                if stage is ContentPipelineStage.CLASSIFY
                else ContentPipelineStageStatus.SUCCEEDED
            ),
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for stage in (
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    admin_id = admin.id
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stages[ContentPipelineStage.OCR].id),
    )
    reference = RecoveryWorkReference(
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        id=candidate.work.id,
        version=candidate.work.version,
    )
    replay = next(action for action in candidate.actions if action.capability is RecoveryCapability.REPLAY_STAGE)
    assert replay.required_acknowledgements == []
    assert replay.scope_requirements[
        RecoveryReplayScope.STAGE_ONLY
    ].required_acknowledgements == []
    assert replay.scope_requirements[
        RecoveryReplayScope.STAGE_AND_DEPENDENTS
    ].required_acknowledgements == ["terminal_override"]
    assert replay.model_dump(mode="json")["scope_requirements"]["stage_and_dependents"][
        "required_acknowledgements"
    ] == ["terminal_override"]

    stage_only_preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_ONLY,
            reason="Replay only OCR without overriding an unrelated terminal descendant.",
            selector=RecoveryExplicitSelector(items=[reference]),
        ),
    )
    assert stage_only_preview.total_count == 1

    with pytest.raises(AdminRecoveryConflictError, match="Required acknowledgement is missing: terminal_override"):
        await service.preview_batch(
            admin_user_id=admin.id,
            payload=RecoveryBatchPreviewRequest(
                request_id=uuid.uuid7(),
                action=RecoveryCapability.REPLAY_STAGE,
                scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
                reason="Require an override before replaying a terminal descendant.",
                selector=RecoveryExplicitSelector(items=[reference]),
            ),
        )
    await migrated_db_session.rollback()

    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, reference.id)
    with pytest.raises(AdminRecoveryConflictError, match="Required acknowledgement is missing: terminal_override"):
        await service.perform_action(
            admin_user_id=admin_id,
            kind=RecoveryWorkKind.PIPELINE_STAGE,
            work_id=reference.id,
            payload=RecoveryActionRequest(
                request_id=uuid.uuid7(),
                version=candidate.work.version,
                reason="Reject a cascade whose terminal descendant was not acknowledged.",
                action=RecoveryCapability.REPLAY_STAGE,
                scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            ),
        )

    preview = await service.preview_batch(
        admin_user_id=admin_id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            reason="Acknowledge the terminal descendant and replay the cascade.",
            acknowledgements=["terminal_override"],
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.PIPELINE_STAGE,
                        id=candidate.work.id,
                        version=candidate.work.version,
                    )
                ]
            ),
        ),
    )
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id)
            )
        )
        .scalars()
        .all()
    )
    assert items
    assert all(item.terminal_override_acknowledged for item in items)


async def test_terminal_stage_action_requires_and_persists_override_acknowledgement(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="terminal-stage-override@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    transcode = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        is_retryable=False,
        finished_at=utcnow(),
    )
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=4,
        normalized_reason="provider_rejected_input",
        is_retryable=False,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((admin, transcode, ocr))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    replay = next(action for action in candidate.actions if action.capability is RecoveryCapability.REPLAY_STAGE)
    assert replay.available is True
    assert replay.required_acknowledgements == ["terminal_override"]

    with pytest.raises(AdminRecoveryConflictError, match="Required acknowledgement is missing"):
        await service.perform_action(
            admin_user_id=admin.id,
            kind=RecoveryWorkKind.PIPELINE_STAGE,
            work_id=str(ocr.id),
            payload=RecoveryActionRequest(
                request_id=uuid.uuid7(),
                version=candidate.work.version,
                reason="Attempt terminal replay without the required acknowledgement.",
                action=RecoveryCapability.REPLAY_STAGE,
            ),
        )

    request_id = uuid.uuid7()
    reason = "Override the terminal provider decision after reviewing the source."
    job = await service.perform_action(
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(ocr.id),
        payload=RecoveryActionRequest(
            request_id=request_id,
            version=candidate.work.version,
            reason=reason,
            action=RecoveryCapability.REPLAY_STAGE,
            acknowledgements=["terminal_override"],
        ),
    )
    item = await migrated_db_session.scalar(select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == job.id))
    audit = await migrated_db_session.scalar(
        select(OperationalAuditLog).where(OperationalAuditLog.request_id == request_id)
    )
    assert job.status is RecoveryJobStatus.QUEUED
    assert item is not None and item.terminal_override_acknowledged is True
    assert audit is not None and audit.note == reason


async def test_query_materializer_resumes_and_excludes_overlapping_cascade_root(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="query-overlap@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    ordered_stages = (
        ContentPipelineStage.TRANSCODE,
        ContentPipelineStage.OCR,
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    )
    stages = {
        stage: PipelineStageJournal(
            id=uuid.UUID(f"00000000-0000-7000-8000-{index:012d}"),
            meme_file_id=file.id,
            stage=stage,
            status=(
                ContentPipelineStageStatus.FAILED
                if stage in {ContentPipelineStage.OCR, ContentPipelineStage.EMBED}
                else ContentPipelineStageStatus.SUCCEEDED
            ),
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=stage in {ContentPipelineStage.OCR, ContentPipelineStage.EMBED},
            finished_at=utcnow(),
        )
        for index, stage in enumerate(ordered_stages, start=1)
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            reason="Materialize overlapping cascade roots across restart-safe pages.",
            selector=RecoveryQuerySelector(filters=RecoveryQueryFilters()),
        ),
    )

    assert await service.materialize_next_preparing_job(page_size=1) is True
    assert await service.materialize_next_preparing_job(page_size=1) is True
    assert await service.materialize_next_preparing_job(page_size=1) is True
    assert await service.materialize_next_preparing_job(page_size=1) is True
    materialized = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preparing.id)
            )
        )
        .scalars()
        .all()
    )
    assert materialized.status is RecoveryJobStatus.PREVIEW
    assert materialized.selected_root_count == 1
    assert materialized.expanded_execution_count == 5
    assert materialized.excluded_count == 1
    assert materialized.exclusions_by_reason == {"overlapping_stage_selection": 1}
    assert len(items) == 5
    assert len({(item.meme_file_id, item.stage) for item in items}) == 5


async def test_empty_query_preview_cannot_be_scheduled(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="empty-query-preview@example.com", is_admin=True)
    migrated_db_session.add(admin)
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REBUILD_OUTBOX,
            reason="Prove an empty exact selector cannot create a stuck queued job.",
            selector=RecoveryQuerySelector(filters=RecoveryQueryFilters(kind=RecoveryWorkKind.OUTBOX)),
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=10) is True
    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    member_count = await migrated_db_session.scalar(
        select(func.count(RecoveryQuerySnapshotMember.id)).where(
            RecoveryQuerySnapshotMember.recovery_job_id == preparing.id
        )
    )
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.total_count == 0
    assert member_count == 0

    with pytest.raises(AdminRecoveryConflictError, match="no eligible work"):
        await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=preview.id,
            version=preview.version,
            reason="Do not schedule an empty recovery preview.",
        )


async def test_query_snapshot_excludes_transitioned_member_and_never_admits_transition_in(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="query-snapshot-transition@example.com", is_admin=True)
    transitioned_out = _failed_outbox("snapshot-transition-out")
    transitioned_in = _failed_outbox("snapshot-transition-in")
    transitioned_in.status = RabbitMQOutboxMessageStatus.PENDING
    migrated_db_session.add_all((admin, transitioned_out, transitioned_in))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REBUILD_OUTBOX,
            reason="Freeze exact outbox membership before canonical transitions.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(kind=RecoveryWorkKind.OUTBOX),
            ),
        ),
    )
    assert preparing.selection_snapshot_at is None
    assert await service.materialize_next_preparing_job(page_size=10) is True

    captured = (
        (
            await migrated_db_session.execute(
                select(RecoveryQuerySnapshotMember).where(
                    RecoveryQuerySnapshotMember.recovery_job_id == preparing.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(member.work_kind, member.work_id) for member in captured] == [
        (RecoveryWorkKind.OUTBOX, str(transitioned_out.id))
    ]

    transitioned_out.status = RabbitMQOutboxMessageStatus.PUBLISHED
    transitioned_in.status = RabbitMQOutboxMessageStatus.FAILED
    await migrated_db_session.commit()

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preparing.id)
            )
        )
        .scalars()
        .all()
    )
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.total_count == 0
    assert preview.preparation_scanned_count == 1
    assert preview.excluded_count == 1
    assert preview.exclusions_by_reason == {"canonical_state_changed": 1}
    assert items == []


async def test_successful_ocr_query_freezes_membership_and_materializes_exact_cascade(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="successful-ocr-query@example.com", is_admin=True)
    stable_file = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    transitioned_out_file = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    transitioned_in_file = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    source_changed_file = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    stable_stages = {
        stage: PipelineStageJournal(
            meme_file_id=stable_file.id,
            stage=stage,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for stage in (
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    }
    transitioned_out_transcode = PipelineStageJournal(
        meme_file_id=transitioned_out_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    transitioned_out = PipelineStageJournal(
        meme_file_id=transitioned_out_file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    transitioned_in_transcode = PipelineStageJournal(
        meme_file_id=transitioned_in_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    transitioned_in = PipelineStageJournal(
        meme_file_id=transitioned_in_file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=True,
        finished_at=utcnow(),
    )
    source_changed_transcode = PipelineStageJournal(
        meme_file_id=source_changed_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    source_changed = PipelineStageJournal(
        meme_file_id=source_changed_file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all(
        (
            admin,
            *stable_stages.values(),
            transitioned_out_transcode,
            transitioned_out,
            transitioned_in_transcode,
            transitioned_in,
            source_changed_transcode,
            source_changed,
        )
    )
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            retry_limit=5,
            reason="Replay every successful OCR root and its exact dependents.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(
                    successful_stage=True,
                    stage=ContentPipelineStage.OCR,
                )
            ),
        ),
    )

    assert preparing.status is RecoveryJobStatus.PREPARING
    assert preparing.selection_snapshot_at is None
    assert await service.materialize_next_preparing_job(page_size=10) is True
    captured = (
        (
            await migrated_db_session.execute(
                select(RecoveryQuerySnapshotMember).where(
                    RecoveryQuerySnapshotMember.recovery_job_id == preparing.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {member.work_id for member in captured} == {
        str(stable_stages[ContentPipelineStage.OCR].id),
        str(transitioned_out.id),
        str(source_changed.id),
    }
    assert all(
        member.root_key == f"successful_stage:pipeline_stage:{member.work_id}"
        and member.work_kind is RecoveryWorkKind.PIPELINE_STAGE
        and member.stage is ContentPipelineStage.OCR
        and member.captured_version
        and member.captured_context_fingerprint
        and not member.is_outdated_video
        for member in captured
    )

    transitioned_out.status = ContentPipelineStageStatus.FAILED
    transitioned_out.is_retryable = True
    transitioned_out.last_event_id = uuid.uuid7()
    transitioned_in.status = ContentPipelineStageStatus.SUCCEEDED
    transitioned_in.is_retryable = False
    transitioned_in.last_event_id = uuid.uuid7()
    source_changed_file.s3_original_key = f"tests/recovery/replaced/{source_changed_file.id}.jpg"
    await migrated_db_session.commit()

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preparing.id)
            )
        )
        .scalars()
        .all()
    )
    items_by_stage = {item.stage: item for item in items}
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.preparation_scanned_count == 3
    assert preview.selected_root_count == 1
    assert preview.expanded_execution_count == 5
    assert preview.total_count == 5
    assert preview.excluded_count == 2
    assert preview.exclusions_by_reason == {"canonical_state_changed": 2}
    assert set(items_by_stage) == {
        ContentPipelineStage.OCR,
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    }
    assert all(item.retry_limit == 5 for item in items)
    assert items_by_stage[ContentPipelineStage.OCR].is_root is True
    assert items_by_stage[ContentPipelineStage.EMBED].parent_item_id == items_by_stage[ContentPipelineStage.OCR].id
    assert (
        items_by_stage[ContentPipelineStage.CLASSIFY].parent_item_id
        == items_by_stage[ContentPipelineStage.EMBED].id
    )
    assert (
        items_by_stage[ContentPipelineStage.SYNC_QDRANT].parent_item_id
        == items_by_stage[ContentPipelineStage.CLASSIFY].id
    )
    assert (
        items_by_stage[ContentPipelineStage.SYNC_MEILI].parent_item_id
        == items_by_stage[ContentPipelineStage.CLASSIFY].id
    )
    assert all(item.meme_file_id == stable_file.id for item in items)


async def test_successful_stage_cascade_requires_and_persists_terminal_override(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="successful-stage-terminal@example.com", is_admin=True)
    file = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    stages = {
        stage: PipelineStageJournal(
            meme_file_id=file.id,
            stage=stage,
            status=(
                ContentPipelineStageStatus.FAILED
                if stage is ContentPipelineStage.EMBED
                else ContentPipelineStageStatus.SUCCEEDED
            ),
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for stage in (
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        )
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    selector = RecoveryQuerySelector(
        filters=RecoveryQueryFilters(
            successful_stage=True,
            stage=ContentPipelineStage.OCR,
        )
    )

    unacknowledged = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            reason="Prove terminal descendants cannot be overridden implicitly.",
            selector=selector,
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=10) is True
    assert await service.materialize_next_preparing_job(page_size=10) is True
    blocked_preview = await service.get_job(admin_user_id=admin.id, job_id=unacknowledged.id)
    assert blocked_preview.status is RecoveryJobStatus.PREVIEW
    assert blocked_preview.selected_root_count == 0
    assert blocked_preview.expanded_execution_count == 0
    assert blocked_preview.exclusions_by_reason == {"acknowledgement_required": 1}

    acknowledged = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            reason="Override the reviewed terminal descendant for an audited cascade.",
            selector=selector,
            acknowledgements=["terminal_override"],
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=10) is True
    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=acknowledged.id)
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == acknowledged.id)
            )
        )
        .scalars()
        .all()
    )
    persisted_job = await migrated_db_session.get(RecoveryJob, acknowledged.id)
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.selected_root_count == 1
    assert preview.expanded_execution_count == 5
    assert len(items) == 5
    assert all(item.terminal_override_acknowledged for item in items)
    assert persisted_job is not None
    assert persisted_job.selection["acknowledgements"] == ["terminal_override"]


async def test_successful_qdrant_query_captures_only_synced_target_roots(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="successful-qdrant-query@example.com", is_admin=True)
    synced_file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    failed_file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    meili_file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    classify_rows = [
        PipelineStageJournal(
            meme_file_id=file.id,
            stage=ContentPipelineStage.CLASSIFY,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for file in (synced_file, failed_file, meili_file)
    ]
    synced = MemeFileSyncTargetSnapshot(
        meme_file_id=synced_file.id,
        sync_target=SyncTargetKind.QDRANT,
        status=SyncTargetStatus.SYNCED,
        last_event_id=uuid.uuid7(),
        last_success_at=utcnow(),
        last_attempt_at=utcnow(),
        attempt_count=1,
    )
    failed = MemeFileSyncTargetSnapshot(
        meme_file_id=failed_file.id,
        sync_target=SyncTargetKind.QDRANT,
        status=SyncTargetStatus.FAILED,
        last_event_id=uuid.uuid7(),
        normalized_reason="sync_qdrant_timeout",
        last_attempt_at=utcnow(),
        attempt_count=1,
    )
    meili = MemeFileSyncTargetSnapshot(
        meme_file_id=meili_file.id,
        sync_target=SyncTargetKind.MEILISEARCH,
        status=SyncTargetStatus.SYNCED,
        last_event_id=uuid.uuid7(),
        last_success_at=utcnow(),
        last_attempt_at=utcnow(),
        attempt_count=1,
    )
    migrated_db_session.add_all((admin, *classify_rows, synced, failed, meili))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            reason="Replay every successfully synchronized Qdrant target.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(
                    successful_stage=True,
                    stage=ContentPipelineStage.SYNC_QDRANT,
                )
            ),
        ),
    )

    assert preparing.status is RecoveryJobStatus.PREPARING
    assert await service.materialize_next_preparing_job(page_size=10) is True
    captured = (
        (
            await migrated_db_session.execute(
                select(RecoveryQuerySnapshotMember).where(
                    RecoveryQuerySnapshotMember.recovery_job_id == preparing.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(captured) == 1
    assert captured[0].root_key == f"successful_stage:sync_target:{synced.id}"
    assert captured[0].work_kind is RecoveryWorkKind.SYNC_TARGET
    assert captured[0].work_id == str(synced.id)
    assert captured[0].meme_file_id == synced_file.id
    assert captured[0].stage is ContentPipelineStage.SYNC_QDRANT

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    items = (
        (
            await migrated_db_session.execute(
                select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preparing.id)
            )
        )
        .scalars()
        .all()
    )
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.preparation_scanned_count == 1
    assert preview.selected_root_count == 1
    assert preview.expanded_execution_count == 1
    assert preview.excluded_count == 0
    assert len(items) == 1
    assert items[0].work_kind is RecoveryWorkKind.SYNC_TARGET
    assert items[0].work_id == str(synced.id)
    assert items[0].stage is ContentPipelineStage.SYNC_QDRANT
    assert items[0].action is RecoveryCapability.REPLAY_STAGE


async def test_query_snapshot_capture_and_materialization_are_idempotent_across_restart(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="query-snapshot-restart@example.com", is_admin=True)
    outboxes = [_failed_outbox(f"snapshot-restart-{index}") for index in range(3)]
    migrated_db_session.add_all((admin, *outboxes))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    request_id = uuid.uuid7()
    payload = RecoveryBatchPreviewRequest(
        request_id=request_id,
        action=RecoveryCapability.REBUILD_OUTBOX,
        reason="Resume exact snapshot expansion without duplicating roots.",
        selector=RecoveryQuerySelector(
            filters=RecoveryQueryFilters(kind=RecoveryWorkKind.OUTBOX),
        ),
    )
    first = await service.preview_batch(admin_user_id=admin.id, payload=payload)
    repeated = await service.preview_batch(admin_user_id=admin.id, payload=payload)
    uncaptured_count = await migrated_db_session.scalar(
        select(func.count(RecoveryQuerySnapshotMember.id)).where(
            RecoveryQuerySnapshotMember.recovery_job_id == first.id
        )
    )
    assert repeated.id == first.id
    assert first.selection_snapshot_at is None
    assert uncaptured_count == 0

    assert await service.materialize_next_preparing_job(page_size=1) is True
    member_count = await migrated_db_session.scalar(
        select(func.count(RecoveryQuerySnapshotMember.id)).where(
            RecoveryQuerySnapshotMember.recovery_job_id == first.id
        )
    )
    assert member_count == 3

    assert await service.materialize_next_preparing_job(page_size=1) is True
    restarted_service = AdminRecoveryService(migrated_db_session)
    for _ in range(4):
        assert await restarted_service.materialize_next_preparing_job(page_size=1) is True
        current = await restarted_service.get_job(admin_user_id=admin.id, job_id=first.id)
        if current.status is RecoveryJobStatus.PREVIEW:
            break
    else:  # pragma: no cover - assertion helper.
        pytest.fail("snapshot materialization did not finish")

    item_count = await migrated_db_session.scalar(
        select(func.count(RecoveryJobItem.id)).where(RecoveryJobItem.recovery_job_id == first.id)
    )
    preview = await restarted_service.get_job(admin_user_id=admin.id, job_id=first.id)
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.preparation_scanned_count == 3
    assert preview.selected_root_count == 3
    assert item_count == 3


async def test_query_snapshot_capture_retries_cleanly_after_precommit_crash(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="query-snapshot-capture-crash@example.com", is_admin=True)
    outboxes = [_failed_outbox(f"snapshot-capture-crash-{index}") for index in range(2)]
    migrated_db_session.add_all((admin, *outboxes))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REBUILD_OUTBOX,
            reason="Retry immutable membership capture after an interrupted transaction.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(kind=RecoveryWorkKind.OUTBOX),
            ),
        ),
    )
    admin_id = preparing.requested_by_admin_user_id
    capture = service._capture_query_snapshot_members

    async def crash_after_flush(**kwargs: Any) -> tuple[object, int]:
        _ = await capture(**kwargs)
        raise RuntimeError("simulated capture crash before membership commit")

    monkeypatch.setattr(service, "_capture_query_snapshot_members", crash_after_flush)
    with pytest.raises(RuntimeError, match="simulated capture crash"):
        await service.materialize_next_preparing_job(page_size=10)
    await migrated_db_session.rollback()

    leased = await migrated_db_session.get(RecoveryJob, preparing.id)
    assert leased is not None
    assert leased.selection_snapshot_at is None
    leased.materialization_lease_owner = None
    leased.materialization_lease_at = None
    await migrated_db_session.commit()

    restarted_service = AdminRecoveryService(migrated_db_session)
    assert await restarted_service.materialize_next_preparing_job(page_size=10) is True
    captured_count = await migrated_db_session.scalar(
        select(func.count(RecoveryQuerySnapshotMember.id)).where(
            RecoveryQuerySnapshotMember.recovery_job_id == preparing.id
        )
    )
    captured_job = await restarted_service.get_job(admin_user_id=admin_id, job_id=preparing.id)
    assert captured_job.status is RecoveryJobStatus.PREPARING
    assert captured_job.selection_snapshot_at is not None
    assert captured_count == 2


async def test_query_snapshot_rejects_prerequisite_transition_in_after_capture(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="query-snapshot-prerequisite@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    transcode = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=True,
        finished_at=utcnow(),
    )
    embed = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.EMBED,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=True,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((admin, transcode, ocr, embed))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_ONLY,
            reason="Freeze the blocked Embed prerequisite state before it transitions.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(
                    kind=RecoveryWorkKind.PIPELINE_STAGE,
                    stage=ContentPipelineStage.EMBED,
                )
            ),
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=10) is True

    ocr.status = ContentPipelineStageStatus.SUCCEEDED
    ocr.last_event_id = uuid.uuid7()
    ocr.is_retryable = False
    await migrated_db_session.commit()

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.total_count == 0
    assert preview.exclusions_by_reason == {"canonical_state_changed": 1}


@pytest.mark.parametrize("descendant_change", ["version", "missing_topology"])
async def test_query_snapshot_rejects_descendant_context_change_after_capture(
    migrated_db_session: AsyncSession,
    descendant_change: str,
) -> None:
    admin = User(email=f"query-snapshot-descendant-{descendant_change}@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    transcode = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=True,
        finished_at=utcnow(),
    )
    embed = (
        PipelineStageJournal(
            meme_file_id=file.id,
            stage=ContentPipelineStage.EMBED,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        if descendant_change == "version"
        else None
    )
    migrated_db_session.add_all((admin, transcode, ocr, *([embed] if embed is not None else [])))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            reason="Freeze the exact cascade descendant topology and versions.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(
                    kind=RecoveryWorkKind.PIPELINE_STAGE,
                    stage=ContentPipelineStage.OCR,
                )
            ),
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=10) is True

    if embed is None:
        migrated_db_session.add(
            PipelineStageJournal(
                meme_file_id=file.id,
                stage=ContentPipelineStage.EMBED,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=uuid.uuid7(),
                is_retryable=False,
                finished_at=utcnow(),
            )
        )
    else:
        embed.last_event_id = uuid.uuid7()
        embed.attempt_count += 1
    await migrated_db_session.commit()

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.total_count == 0
    assert preview.exclusions_by_reason == {"canonical_state_changed": 1}


async def test_outdated_video_snapshot_freezes_transition_out_and_transition_in(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="outdated-snapshot-transition@example.com", is_admin=True)
    transitioned_out = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    transitioned_in = await _seed_meme_file(
        migrated_db_session,
        status=ContentProcessingStatus.READY,
    )
    for file in (transitioned_out, transitioned_in):
        file.mime_type = "video/webm"
        file.s3_original_key = f"tests/recovery/snapshot/{file.id}/original.webm"
        file.s3_web_video_key = f"pipeline/derived/{file.id}/web.mp4"
        file.source_has_audio = False
        file.web_video_has_audio = False
    transitioned_out.web_video_profile = "legacy-profile"
    transitioned_in.web_video_profile = "web-h264-aac-1080p30-v2"
    transitioned_in.web_video_verified_at = utcnow()
    stages = {
        file.id: PipelineStageJournal(
            meme_file_id=file.id,
            stage=ContentPipelineStage.TRANSCODE,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            finished_at=utcnow(),
        )
        for file in (transitioned_out, transitioned_in)
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            reason="Freeze outdated derivative membership before profile transitions.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(outdated_web_video=True),
            ),
            acknowledgements=["terminal_override"],
        ),
    )
    assert preparing.selection_snapshot_at is None
    assert await service.materialize_next_preparing_job(page_size=10) is True
    members = (
        (
            await migrated_db_session.execute(
                select(RecoveryQuerySnapshotMember).where(
                    RecoveryQuerySnapshotMember.recovery_job_id == preparing.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [member.meme_file_id for member in members] == [transitioned_out.id]

    transitioned_out.web_video_profile = "web-h264-aac-1080p30-v2"
    transitioned_out.web_video_verified_at = utcnow()
    transitioned_in.web_video_profile = "legacy-profile"
    await migrated_db_session.commit()

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    item_count = await migrated_db_session.scalar(
        select(func.count(RecoveryJobItem.id)).where(RecoveryJobItem.recovery_job_id == preparing.id)
    )
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.total_count == 0
    assert preview.preparation_scanned_count == 1
    assert preview.excluded_count == 1
    assert preview.exclusions_by_reason == {"canonical_state_changed": 1}
    assert item_count == 0


async def test_schedule_revalidates_missing_original_not_captured_by_stage_version(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="schedule-revalidation@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    transcode = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        is_retryable=False,
    )
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        is_retryable=False,
    )
    migrated_db_session.add_all((admin, transcode, ocr))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            reason="Preview a stage before its original disappears.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.PIPELINE_STAGE,
                        id=candidate.work.id,
                        version=candidate.work.version,
                    )
                ]
            ),
        ),
    )
    file.s3_original_key = ""
    await migrated_db_session.commit()

    with pytest.raises(AdminRecoveryConflictError, match="durable original is missing"):
        await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=preview.id,
            version=preview.version,
            reason="Revalidate canonical inputs before scheduling.",
        )


@pytest.mark.parametrize("descendant_change", ["version_changed", "missing_row_appeared"])
async def test_schedule_rejects_changed_cascade_descendant_execution_state(
    migrated_db_session: AsyncSession,
    descendant_change: str,
) -> None:
    admin = User(email=f"schedule-descendant-{descendant_change}@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    transcode = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
    )
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
    )
    embed = None
    if descendant_change == "version_changed":
        embed = PipelineStageJournal(
            meme_file_id=file.id,
            stage=ContentPipelineStage.EMBED,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
        )
    migrated_db_session.add_all([admin, transcode, ocr, *([embed] if embed is not None else [])])
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
            reason="Preview a cascade whose descendant generation will change.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.PIPELINE_STAGE,
                        id=str(ocr.id),
                        version=candidate.work.version,
                    )
                ]
            ),
        ),
    )

    if embed is None:
        migrated_db_session.add(
            PipelineStageJournal(
                meme_file_id=file.id,
                stage=ContentPipelineStage.EMBED,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=uuid.uuid7(),
                is_retryable=False,
            )
        )
    else:
        embed.last_event_id = uuid.uuid7()
        embed.attempt_count += 1
    await migrated_db_session.commit()

    with pytest.raises(AdminRecoveryConflictError, match="execution state changed"):
        await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=preview.id,
            version=preview.version,
            reason="Reject the stale reviewed cascade.",
        )


def test_outdated_video_query_rejects_unapplied_mixed_filters() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        RecoveryQueryFilters(outdated_web_video=True, query="narrow this selection")


async def test_outdated_video_materializer_excludes_unsafe_and_changed_rows(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="outdated-video-exclusions@example.com", is_admin=True)
    missing_original = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    unsupported = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    changed = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    missing_original.mime_type = "video/webm"
    missing_original.s3_original_key = ""
    unsupported.mime_type = "video/x-matroska"
    changed.mime_type = "video/webm"
    for file in (missing_original, unsupported, changed):
        file.s3_web_video_key = f"pipeline/derived/{file.id}/web.mp4"
        file.web_video_profile = "legacy-profile"
    stages = {
        file.id: PipelineStageJournal(
            meme_file_id=file.id,
            stage=ContentPipelineStage.TRANSCODE,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            is_retryable=False,
            finished_at=utcnow(),
        )
        for file in (missing_original, unsupported, changed)
    }
    migrated_db_session.add_all((admin, *stages.values()))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            reason="Exclude unsafe and post-snapshot derivative rows.",
            selector=RecoveryQuerySelector(filters=RecoveryQueryFilters(outdated_web_video=True)),
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=10) is True
    stages[changed.id].normalized_reason = "changed_after_snapshot"
    await migrated_db_session.commit()

    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.total_count == 0
    assert preview.excluded_count == 3
    assert preview.exclusions_by_reason == {
        "canonical_state_changed": 1,
        "missing_original": 1,
        "unsupported_media_type": 1,
    }


@pytest.mark.parametrize("mime_type", ["image/apng", "video/x-matroska"])
async def test_regeneration_candidate_rejects_unsupported_moving_media_mime(
    migrated_db_session: AsyncSession,
    mime_type: str,
) -> None:
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    file.mime_type = mime_type
    file.s3_web_video_key = f"pipeline/derived/{file.id}/web.mp4"
    file.web_video_profile = "legacy-profile"
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        is_retryable=False,
        finished_at=utcnow(),
    )
    migrated_db_session.add(stage)
    await migrated_db_session.commit()

    candidate = await AdminRecoveryService(migrated_db_session).get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stage.id),
    )
    regeneration = next(
        action
        for action in candidate.actions
        if action.capability is RecoveryCapability.REGENERATE_DERIVATIVES
    )
    assert regeneration.available is False
    assert regeneration.blocked_prerequisites == ["Derivative regeneration applies only to moving media."]


async def test_non_stage_candidate_stays_owned_until_active_job_finishes(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="non-stage-active-job@example.com", is_admin=True)
    outbox = _failed_outbox("active-non-stage")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    work = await service.get_work(RecoveryWorkKind.OUTBOX, str(outbox.id))
    first = await service.perform_action(
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(outbox.id),
        payload=RecoveryActionRequest(
            request_id=uuid.uuid7(),
            version=work.version,
            reason="Reserve one active outbox recovery job.",
            action=RecoveryCapability.REBUILD_OUTBOX,
        ),
    )
    assert first.status is RecoveryJobStatus.QUEUED

    candidate = await service.get_candidate(RecoveryWorkKind.OUTBOX, str(outbox.id))
    action = next(entry for entry in candidate.actions if entry.capability is RecoveryCapability.REBUILD_OUTBOX)
    assert candidate.active_job is not None and candidate.active_job.id == first.id
    assert action.available is False
    assert action.blocked_prerequisites == ["Another Replay & Repair job owns this work."]

    persisted_item = await migrated_db_session.scalar(
        select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == first.id)
    )
    assert persisted_item is not None
    assert persisted_item.reservation_active is True

    cancelled = await service.cancel_batch(
        admin_user_id=admin.id,
        job_id=first.id,
        version=first.version,
        reason="Cancel the queued repair and release its ownership.",
    )
    assert cancelled.status is RecoveryJobStatus.CANCELLED
    await migrated_db_session.refresh(persisted_item)
    assert persisted_item.status is RecoveryJobItemStatus.CANCELLED
    assert persisted_item.reservation_active is False


async def test_parent_failure_marks_waiting_descendant_skipped_dependency(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="skipped-dependency@example.com", is_admin=True)
    migrated_db_session.add(admin)
    await migrated_db_session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_AND_DEPENDENTS,
        retry_limit=3,
        reason="Skip a dependent step after its parent fails.",
        total_count=2,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    parent = RecoveryJobItem(
        recovery_job_id=job.id,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(uuid.uuid7()),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version="parent",
        status=RecoveryJobItemStatus.FAILED,
        finished_at=utcnow(),
    )
    migrated_db_session.add(parent)
    await migrated_db_session.flush()
    child = RecoveryJobItem(
        recovery_job_id=job.id,
        parent_item_id=parent.id,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(uuid.uuid7()),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version="child",
        is_root=False,
        status=RecoveryJobItemStatus.WAITING_DEPENDENCY,
    )
    migrated_db_session.add(child)
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime._skip_failed_dependencies(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted = await session.get(RecoveryJobItem, child.id)
        assert persisted is not None
        assert persisted.status is RecoveryJobItemStatus.SKIPPED_DEPENDENCY
        assert persisted.normalized_reason == "parent_step_failed"


async def test_uncapped_outdated_video_query_materializes_more_than_one_thousand_roots(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="recovery-query-materializer@example.com", is_admin=True)
    migrated_db_session.add(admin)
    root_count = 1001
    memes: list[Meme] = []
    files: list[MemeFile] = []
    stages: list[PipelineStageJournal] = []
    for _ in range(root_count):
        meme_id = uuid.uuid7()
        file_id = uuid.uuid7()
        memes.append(Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.VIDEO))
        files.append(
            MemeFile(
                id=file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.READY,
                s3_original_key=f"tests/recovery/query/{file_id}/original.webm",
                s3_web_video_key=f"pipeline/derived/{file_id}/web.mp4",
                mime_type="video/webm",
                web_video_profile="legacy-15fps",
                source_has_audio=True,
                web_video_has_audio=False,
            )
        )
        stages.append(
            PipelineStageJournal(
                meme_file_id=file_id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=uuid.uuid7(),
                is_retryable=False,
                finished_at=utcnow(),
            )
        )
    migrated_db_session.add_all(memes)
    await migrated_db_session.flush()
    migrated_db_session.add_all(files)
    await migrated_db_session.flush()
    migrated_db_session.add_all(stages)
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)

    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            scope=RecoveryReplayScope.STAGE_ONLY,
            retry_limit=3,
            reason="Materialize every outdated browser-video derivative.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(outdated_web_video=True),
            ),
        ),
    )
    assert preparing.status is RecoveryJobStatus.PREPARING

    for _ in range(20):
        if not await service.materialize_next_preparing_job(page_size=137):
            break
    materialized = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    item_count = await migrated_db_session.scalar(
        select(func.count(RecoveryJobItem.id)).where(RecoveryJobItem.recovery_job_id == preparing.id)
    )
    assert materialized.status is RecoveryJobStatus.PREVIEW
    assert materialized.selected_root_count == root_count
    assert materialized.expanded_execution_count == root_count
    assert materialized.excluded_count == 0
    assert item_count == root_count
    assert materialized.expires_at is not None
    assert materialized.materialization_completed_at is not None


async def test_cancellation_waits_for_dispatched_work_then_finalizes_accurate_counts(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-cancelling@example.com", is_admin=True)
    first_outbox = _failed_outbox("cancelling-dispatched")
    second_outbox = _failed_outbox("cancelling-waiting")
    migrated_db_session.add_all((admin, first_outbox, second_outbox))
    await migrated_db_session.commit()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        assigned_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REBUILD_OUTBOX,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Exercise safe cancellation.",
        total_count=2,
        expanded_execution_count=2,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    root_item = RecoveryJobItem(
        recovery_job_id=job.id,
        work_kind=RecoveryWorkKind.OUTBOX,
        work_id=str(first_outbox.id),
        action=RecoveryCapability.REBUILD_OUTBOX,
        expected_version="reserved",
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatched_at=utcnow(),
    )
    waiting_item = RecoveryJobItem(
        recovery_job_id=job.id,
        parent_item_id=root_item.id,
        is_root=False,
        work_kind=RecoveryWorkKind.OUTBOX,
        work_id=str(second_outbox.id),
        action=RecoveryCapability.REBUILD_OUTBOX,
        expected_version="waiting",
        status=RecoveryJobItemStatus.WAITING_DEPENDENCY,
    )
    migrated_db_session.add_all((root_item, waiting_item))
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    projected = await service.get_job(admin_user_id=admin.id, job_id=job.id)

    cancelling = await service.cancel_batch(
        admin_user_id=admin.id,
        job_id=job.id,
        version=projected.version,
        reason="Stop admitting undispatched descendants.",
    )
    assert cancelling.status is RecoveryJobStatus.CANCELLING
    assert cancelling.cancelled_count == 1
    first_outbox.status = RabbitMQOutboxMessageStatus.PUBLISHED
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted = await session.get(RecoveryJob, job.id)
        assert persisted is not None
        assert persisted.status is RecoveryJobStatus.CANCELLED
        assert persisted.completed_count == 2
        assert persisted.succeeded_count == 1
        assert persisted.cancelled_count == 1
        assert persisted.failed_count == 0


async def test_retry_failed_preview_links_source_item_and_excludes_terminal_override(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="recovery-retry-failed@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=4,
        last_event_id=uuid.uuid7(),
        normalized_reason="ocr_timeout",
        is_retryable=True,
    )
    prerequisite = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((admin, prerequisite, stage))
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stage.id),
    )
    source_job, source_item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.RETRY_STAGE,
        expected_version=work.version,
        status=RecoveryJobItemStatus.FAILED,
    )
    source_job.status = RecoveryJobStatus.COMPLETED_WITH_FAILURES
    source_job.scope = RecoveryReplayScope.STAGE_ONLY
    source_item.meme_file_id = file.id
    source_item.stage = ContentPipelineStage.OCR
    source_item.canonical_version = work.version
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    source_read = await service.get_job(admin_user_id=admin.id, job_id=source_job.id)

    retry = await service.preview_failed_items(
        admin_user_id=admin.id,
        job_id=source_job.id,
        payload=RecoveryRetryFailedPreviewRequest(
            request_id=uuid.uuid7(),
            version=source_read.version,
            reason="Retry only the failed OCR step.",
        ),
    )
    assert retry.source_recovery_job_id == source_job.id
    assert retry.items[0].source_item_id == source_item.id
    assert retry.items[0].status is RecoveryJobItemStatus.QUEUED

    stage.is_retryable = False
    stage.normalized_reason = "ocr_malformed_payload"
    await migrated_db_session.commit()
    source_read = await service.get_job(admin_user_id=admin.id, job_id=source_job.id)
    with pytest.raises(AdminRecoveryConflictError, match="no longer eligible"):
        await service.preview_failed_items(
            admin_user_id=admin.id,
            job_id=source_job.id,
            payload=RecoveryRetryFailedPreviewRequest(
                request_id=uuid.uuid7(),
                version=source_read.version,
                reason="Terminal replay still needs its explicit checkbox.",
            ),
        )


async def test_admin_replay_captures_previous_stage_state_and_absolute_budget_start(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-stage-snapshot@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    previous_event_id = uuid.uuid7()
    finished_at = utcnow() - timedelta(minutes=2)
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=4,
        last_event_id=previous_event_id,
        is_retryable=False,
        started_at=finished_at - timedelta(seconds=5),
        finished_at=finished_at,
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.commit()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Capture the previous successful journal generation.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version="snapshot",
        retry_limit=3,
        preserve_ready=True,
        suppress_fanout=True,
        reservation_active=True,
    )
    migrated_db_session.add(item)
    await migrated_db_session.commit()
    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )

    await PipelineReplayService(migrated_db_session, settings=Settings()).replay_admin_stage(
        file.id,
        stage=ContentPipelineStage.OCR,
        recovery_item=item,
    )
    assert item.attempt_budget_start == 5
    assert item.status is RecoveryJobItemStatus.DISPATCHED
    assert item.previous_stage_state == {
        "status": "succeeded",
        "attempt_count": 4,
        "last_event_id": str(previous_event_id),
        "normalized_reason": None,
        "last_error_text": None,
        "is_retryable": False,
        "retry_after": None,
        "started_at": (finished_at - timedelta(seconds=5)).isoformat(),
        "finished_at": finished_at.isoformat(),
    }


async def test_stage_replay_outbox_exhaustion_uses_its_own_counter_and_restores_pending_stage(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-stage-outbox-budget@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    previous_event_id = uuid.uuid7()
    previous_finished_at = utcnow() - timedelta(minutes=2)
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=4,
        last_event_id=previous_event_id,
        is_retryable=False,
        started_at=previous_finished_at - timedelta(seconds=5),
        finished_at=previous_finished_at,
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=1,
        reason="Fail exactly one stage-dispatch publication attempt.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version="snapshot",
        retry_limit=1,
        preserve_ready=True,
        suppress_fanout=True,
        reservation_active=True,
    )
    migrated_db_session.add(item)
    await migrated_db_session.commit()
    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )

    await PipelineReplayService(migrated_db_session, settings=Settings()).replay_admin_stage(
        file.id,
        stage=ContentPipelineStage.OCR,
        recovery_item=item,
    )
    assert item.attempt_budget_start == 5
    assert item.dispatch_event_id is not None
    outbox_id = await migrated_db_session.scalar(
        select(RabbitMQOutboxMessage.id).where(RabbitMQOutboxMessage.message_id == str(item.dispatch_event_id))
    )
    assert outbox_id is not None

    async with postgres_session_factory() as session:
        relay = RabbitOutboxRelay(
            session,
            broker=cast("Any", _FailingPublishBroker()),
            settings=Settings(),
        )
        first = await relay.publish_ids((outbox_id,))
        assert (first.claimed, first.published, first.failed) == (1, 0, 1)
        exhausted = await relay.publish_ids((outbox_id,))
        assert (exhausted.claimed, exhausted.published, exhausted.failed) == (0, 0, 0)

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox_id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_stage is not None
        assert persisted_stage.status is ContentPipelineStageStatus.SUCCEEDED
        assert persisted_stage.attempt_count == 4
        assert persisted_stage.last_event_id == previous_event_id
        assert persisted_stage.is_retryable is False
        assert persisted_stage.finished_at == previous_finished_at
        assert persisted_outbox is not None
        assert persisted_outbox.status is RabbitMQOutboxMessageStatus.FAILED
        assert persisted_outbox.attempt_count == 1
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.FAILED
        assert persisted_item.normalized_reason == "outbox_publish_failed"
        assert persisted_item.retryable_failures_consumed == 1
        assert persisted_item.reservation_active is False
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES
        assert persisted_job.failed_count == 1


async def test_stage_replay_outbox_exhaustion_does_not_overwrite_canonical_worker_progress(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-stage-outbox-race@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=4,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow() - timedelta(minutes=2),
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=1,
        reason="Prefer canonical worker progress over an ambiguous publish error.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version="snapshot",
        retry_limit=1,
        preserve_ready=True,
        suppress_fanout=True,
        reservation_active=True,
    )
    migrated_db_session.add(item)
    await migrated_db_session.commit()
    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )

    await PipelineReplayService(migrated_db_session, settings=Settings()).replay_admin_stage(
        file.id,
        stage=ContentPipelineStage.OCR,
        recovery_item=item,
    )
    assert item.dispatch_event_id is not None
    replay_event_id = item.dispatch_event_id
    outbox_id = await migrated_db_session.scalar(
        select(RabbitMQOutboxMessage.id).where(RabbitMQOutboxMessage.message_id == str(replay_event_id))
    )
    assert outbox_id is not None
    async with postgres_session_factory() as session:
        relay = RabbitOutboxRelay(
            session,
            broker=cast("Any", _FailingPublishBroker()),
            settings=Settings(),
        )
        failed = await relay.publish_ids((outbox_id,))
        assert (failed.claimed, failed.published, failed.failed) == (1, 0, 1)

    stage.status = ContentPipelineStageStatus.PROCESSING
    stage.started_at = utcnow()
    stage.finished_at = None
    await migrated_db_session.commit()
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 0
    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        assert persisted_stage is not None
        assert persisted_stage.status is ContentPipelineStageStatus.PROCESSING
        assert persisted_stage.last_event_id == replay_event_id
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED

    stage.status = ContentPipelineStageStatus.SUCCEEDED
    stage.is_retryable = False
    stage.finished_at = utcnow()
    await migrated_db_session.commit()
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_stage is not None
        assert persisted_stage.status is ContentPipelineStageStatus.SUCCEEDED
        assert persisted_stage.attempt_count == 5
        assert persisted_stage.last_event_id == replay_event_id
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SUCCEEDED
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED


async def test_stage_replay_publication_and_worker_failures_share_one_retry_budget(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-stage-shared-budget@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=4,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow() - timedelta(minutes=2),
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Count broker and worker retryable failures in one budget.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version="snapshot",
        retry_limit=3,
        preserve_ready=True,
        suppress_fanout=True,
        reservation_active=True,
    )
    migrated_db_session.add(item)
    await migrated_db_session.commit()
    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )

    await PipelineReplayService(migrated_db_session, settings=Settings()).replay_admin_stage(
        file.id,
        stage=ContentPipelineStage.OCR,
        recovery_item=item,
    )
    assert item.attempt_budget_start == 5
    assert item.dispatch_event_id is not None
    replay_event_id = item.dispatch_event_id
    outbox_id = await migrated_db_session.scalar(
        select(RabbitMQOutboxMessage.id).where(RabbitMQOutboxMessage.message_id == str(replay_event_id))
    )
    assert outbox_id is not None

    broker = _FailOncePublishBroker()
    async with postgres_session_factory() as session:
        relay = RabbitOutboxRelay(session, broker=cast("Any", broker), settings=Settings())
        first = await relay.publish_ids((outbox_id,))
        second = await relay.publish_ids((outbox_id,))
        assert (first.claimed, first.published, first.failed) == (1, 0, 1)
        assert (second.claimed, second.published, second.failed) == (1, 1, 0)

    async with postgres_session_factory() as completion_session:
        await PipelineStageCompletionService(completion_session, settings=Settings()).mark_stage_failed(
            meme_file_id=file.id,
            stage=ContentPipelineStage.OCR,
            attempt=5,
            event_id=replay_event_id,
            normalized_reason="ocr_timeout",
            last_error_text="OCR timed out after publication recovered.",
            retryable=True,
        )
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox_id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED
        assert persisted_item.previous_stage_state["stage_dispatch_publication_failures"] == 1
        assert persisted_item.retryable_failures_consumed == 2
        assert recovery_stage_worker_attempt_ceiling(persisted_item) == 6
        assert persisted_outbox is not None
        assert persisted_outbox.status is RabbitMQOutboxMessageStatus.PUBLISHED
        assert persisted_outbox.attempt_count == 2

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 0
    assert await runtime.reconcile(batch_size=10) == 0
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED
        assert persisted_item.retryable_failures_consumed == 2
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.RUNNING


async def test_regeneration_failure_waits_for_retry_budget_before_reconciliation(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-regeneration-budget@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    migrated_db_session.add(admin)
    await migrated_db_session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REGENERATE_DERIVATIVES,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Exercise regeneration retry reconciliation.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(uuid.uuid7()),
        action=RecoveryCapability.REGENERATE_DERIVATIVES,
        expected_version="generation",
        retry_limit=3,
        retryable_failures_consumed=1,
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatched_at=utcnow(),
    )
    migrated_db_session.add(item)
    await migrated_db_session.flush()
    generation_id = uuid.uuid7()
    generation = MediaGeneration(
        id=generation_id,
        meme_file_id=file.id,
        recovery_item_id=item.id,
        web_video_object_key=f"pipeline/derived/{file.id}/generations/{generation_id}/web.mp4",
        preview_image_object_key=f"pipeline/derived/{file.id}/generations/{generation_id}/preview.png",
        profile="web-h264-aac-1080p30-v2",
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.FAILED,
        safe_failure_reason="TransientUploadError",
        safe_failure_text="Object storage was temporarily unavailable.",
    )
    migrated_db_session.add(generation)
    await migrated_db_session.commit()
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())

    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id, with_for_update=True)
        assert persisted_item is not None
        assert await runtime._reconcile_item(session, persisted_item) is False
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED
        persisted_item.retryable_failures_consumed = 3
        assert await runtime._reconcile_item(session, persisted_item) is True
        assert persisted_item.status is RecoveryJobItemStatus.FAILED
        await runtime._reconcile_jobs(session)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_job is not None
        assert persisted_job.completed_count == 1
        assert persisted_job.failed_count == 1


async def test_successful_stage_reconciliation_releases_active_reservation(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-success-reservation@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    event_id = uuid.uuid7()
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=2,
        last_event_id=event_id,
        is_retryable=False,
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.RETRY_STAGE,
        expected_version="dispatched",
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatch_event_id=event_id,
    )
    item.meme_file_id = file.id
    item.stage = stage.stage
    item.reservation_active = True
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SUCCEEDED
        assert persisted_item.reservation_active is False
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED


async def test_superseded_media_generation_reconciles_as_success(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-superseded-generation@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session, status=ContentProcessingStatus.READY)
    migrated_db_session.add(admin)
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(uuid.uuid7()),
        action=RecoveryCapability.REGENERATE_DERIVATIVES,
        expected_version="generation",
        status=RecoveryJobItemStatus.DISPATCHED,
    )
    item.meme_file_id = file.id
    item.stage = ContentPipelineStage.TRANSCODE
    item.reservation_active = True
    generation_id = uuid.uuid7()
    migrated_db_session.add(
        MediaGeneration(
            id=generation_id,
            meme_file_id=file.id,
            recovery_item_id=item.id,
            web_video_object_key=f"pipeline/derived/{file.id}/generations/{generation_id}/web.mp4",
            preview_image_object_key=f"pipeline/derived/{file.id}/generations/{generation_id}/preview.png",
            profile="web-h264-aac-1080p30-v2",
            retry_limit=3,
            attempt_count=1,
            status=MediaGenerationStatus.SUPERSEDED,
        )
    )
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SUCCEEDED
        assert persisted_item.reservation_active is False
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED


async def test_terminal_sync_failure_reconciles_without_consuming_retry_budget(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-terminal-sync@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    event_id = uuid.uuid7()
    snapshot = MemeFileSyncTargetSnapshot(
        meme_file_id=file.id,
        sync_target=SyncTargetKind.QDRANT,
        status=SyncTargetStatus.FAILED,
        last_event_id=event_id,
        normalized_reason="sync_qdrant_malformed_payload",
        last_error_text="Provider payload was malformed.",
        attempt_count=1,
    )
    migrated_db_session.add_all((admin, snapshot))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.SYNC_TARGET,
        work_id=str(snapshot.id),
        action=RecoveryCapability.RESYNC_TARGET,
        expected_version="dispatched",
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatch_event_id=event_id,
    )
    item.retry_limit = 5
    item.reservation_active = True
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.FAILED
        assert persisted_item.normalized_reason == "sync_qdrant_malformed_payload"
        assert persisted_item.retryable_failures_consumed == 0
        assert persisted_item.reservation_active is False
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES


async def test_retryable_dispatch_failures_consume_only_the_item_budget(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-dispatch-budget@example.com", is_admin=True)
    outbox = _failed_outbox("dispatch-budget")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(outbox.id),
        action=RecoveryCapability.REBUILD_OUTBOX,
        expected_version="dispatch-budget",
    )
    item.retry_limit = 3
    item.reservation_active = True
    await migrated_db_session.commit()
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())

    for consumed in (1, 2):
        await runtime._record_dispatch_failure(item.id, OSError("broker unavailable"), retryable=True)
        async with postgres_session_factory() as session:
            persisted = await session.get(RecoveryJobItem, item.id)
            assert persisted is not None
            assert persisted.status is RecoveryJobItemStatus.QUEUED
            assert persisted.retryable_failures_consumed == consumed
            assert persisted.finished_at is None
            assert persisted.reservation_active is True

    await runtime._record_dispatch_failure(
        item.id,
        OSError("broker token=operator-secret at pipeline/derived/file/generations/new/web.mp4"),
        retryable=True,
    )
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.FAILED
        assert persisted_item.retryable_failures_consumed == 3
        assert persisted_item.reservation_active is False
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES


async def test_standalone_dead_letter_reconciliation_releases_superseded_stage(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-stale-dead-letter@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    dispatch_event_id = uuid.uuid7()
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.PENDING,
        attempt_count=2,
        last_event_id=uuid.uuid7(),
        is_retryable=True,
    )
    migrated_db_session.add_all((admin, stage))
    await migrated_db_session.flush()
    dead_letter = _dead_letter_for(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stage.id),
        event_id=dispatch_event_id,
        status=RecoveryDeadLetterStatus.RECOVERY_QUEUED,
    )
    migrated_db_session.add(dead_letter)
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.DEAD_LETTER,
        work_id=str(dead_letter.id),
        action=RecoveryCapability.RECOVER_DEAD_LETTER,
        expected_version="dispatched-version",
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatch_event_id=dispatch_event_id,
    )
    dead_letter.recovery_item_id = item.id
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_dead_letter = await session.get(PipelineDeadLetter, dead_letter.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_dead_letter is not None
        assert persisted_dead_letter.status is RecoveryDeadLetterStatus.UNRESOLVED
        assert persisted_dead_letter.recovery_item_id is None
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SKIPPED_STALE
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES


async def test_retryable_dispatch_failure_during_cancellation_never_requeues(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-cancelling-failure@example.com", is_admin=True)
    outbox = _failed_outbox("cancelling-dispatch-failure")
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(outbox.id),
        action=RecoveryCapability.REBUILD_OUTBOX,
        expected_version="dispatched",
        status=RecoveryJobItemStatus.DISPATCHED,
    )
    job.status = RecoveryJobStatus.CANCELLING
    item.retry_limit = 5
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    await runtime._record_dispatch_failure(
        item.id,
        OSError("broker token=operator-secret at pipeline/derived/file/generations/new/web.mp4"),
        retryable=True,
    )

    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.FAILED
        assert persisted_item.retryable_failures_consumed == 1
        assert persisted_item.safe_error_text is not None
        assert "operator-secret" not in persisted_item.safe_error_text
        assert "pipeline/derived" not in persisted_item.safe_error_text
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.CANCELLED
        assert persisted_job.queued_count == 0
        assert persisted_job.failed_count == 1


async def test_recovery_job_failed_count_excludes_stale_and_dependency_skips(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-distinct-failure-count@example.com", is_admin=True)
    migrated_db_session.add(admin)
    await migrated_db_session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REBUILD_OUTBOX,
        reason="Keep terminal counters semantically distinct.",
        total_count=3,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    for status in (
        RecoveryJobItemStatus.FAILED,
        RecoveryJobItemStatus.SKIPPED_STALE,
        RecoveryJobItemStatus.SKIPPED_DEPENDENCY,
    ):
        migrated_db_session.add(
            RecoveryJobItem(
                recovery_job_id=job.id,
                work_kind=RecoveryWorkKind.OUTBOX,
                work_id=str(uuid.uuid7()),
                action=RecoveryCapability.REBUILD_OUTBOX,
                expected_version="terminal",
                status=status,
                finished_at=utcnow(),
            )
        )
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    async with postgres_session_factory() as session:
        await runtime._reconcile_jobs(session)
        await session.commit()
    async with postgres_session_factory() as session:
        persisted = await session.get(RecoveryJob, job.id)
        assert persisted is not None
        assert persisted.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES
        assert persisted.failed_count == 1
        assert persisted.stale_count == 1
        assert persisted.skipped_count == 1


async def test_outbox_recovery_uses_item_budget_beyond_global_attempts_and_stops_exactly(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-outbox-exact-budget@example.com", is_admin=True)
    outbox = _failed_outbox("outbox-exact-budget")
    outbox.attempt_count = 20
    outbox.next_retry_at = utcnow() - timedelta(seconds=1)
    migrated_db_session.add_all((admin, outbox))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.OUTBOX,
        work_id=str(outbox.id),
        action=RecoveryCapability.REBUILD_OUTBOX,
        expected_version="dispatched",
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatch_event_id=uuid.UUID(outbox.message_id),
    )
    item.retry_limit = 3
    item.attempt_budget_start = 21
    budget_state: dict[str, object] = {"non_stage_budget_consumed_at_dispatch": 0}
    item.previous_stage_state = budget_state
    await migrated_db_session.commit()

    async with postgres_session_factory() as session:
        relay = RabbitOutboxRelay(session, broker=cast("Any", object()), settings=Settings())
        for expected_attempt in (21, 22, 23):
            claimed = await relay.claim_due(limit=10)
            assert len(claimed) == 1
            assert claimed[0].id == outbox.id
            assert claimed[0].attempt_count == expected_attempt
            claimed[0].status = RabbitMQOutboxMessageStatus.FAILED
            claimed[0].next_retry_at = utcnow() - timedelta(seconds=1)
            claimed[0].last_error_text = "RabbitMQ remained unavailable."
            await session.commit()
        assert await relay.claim_due(limit=10) == ()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    assert await runtime.reconcile(batch_size=10) == 1
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.FAILED
        assert persisted_item.retryable_failures_consumed == 3
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES


async def test_old_dispatched_backfill_with_null_event_is_not_reclaimed(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = User(email="recovery-long-backfill@example.com", is_admin=True)
    migrated_db_session.add(admin)
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.BACKFILL,
        work_id=str(uuid.uuid7()),
        action=RecoveryCapability.RESUME_BACKFILL,
        expected_version="running",
        status=RecoveryJobItemStatus.DISPATCHED,
    )
    item.dispatched_at = utcnow() - timedelta(hours=1)
    await migrated_db_session.commit()

    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    async with postgres_session_factory() as session:
        claimed = await runtime._claim_next_item(
            session,
            actions={RecoveryCapability.RESUME_BACKFILL},
            telegram=True,
            excluded_item_ids=set(),
        )
        assert claimed is None
    async with postgres_session_factory() as session:
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.DISPATCHED
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.RUNNING


async def test_missing_descendant_survives_capacity_wait_before_dispatch(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-missing-capacity@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    prerequisite = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    capacity = PipelineCapacityState(
        stage=ContentPipelineStage.OCR,
        status=PipelineCapacityStatus.CLOSED,
        pending_count=100,
        oldest_pending_age_seconds=60.0,
        throughput_per_minute_15m=1.0,
        reason="test_capacity_closed",
    )
    migrated_db_session.add_all((admin, prerequisite, capacity))
    await migrated_db_session.flush()
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=f"{file.id}:{ContentPipelineStage.OCR.value}",
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version=f"missing:{file.id}:{ContentPipelineStage.OCR.value}",
    )
    item.meme_file_id = file.id
    item.stage = ContentPipelineStage.OCR
    item.is_root = False
    item.reservation_active = True
    await migrated_db_session.commit()
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())

    first = await runtime.dispatch_general_batch(batch_size=1)
    assert first.claimed == first.waiting_capacity == 1
    async with postgres_session_factory() as session:
        waiting = await session.get(RecoveryJobItem, item.id)
        assert waiting is not None
        assert waiting.status is RecoveryJobItemStatus.WAITING_CAPACITY
        assert uuid.UUID(waiting.work_id)
        assert not waiting.expected_version.startswith("missing:")
        capacity_row = await session.get(PipelineCapacityState, capacity.id, with_for_update=True)
        assert capacity_row is not None
        capacity_row.status = PipelineCapacityStatus.OPEN
        await session.commit()

    monkeypatch.setattr(
        "memexpert.pipeline.replay.PipelineReplayService._relay_outbox_messages_after_commit",
        _skip_immediate_relay,
    )
    second = await runtime.dispatch_general_batch(batch_size=1)
    assert second.claimed == second.dispatched == 1
    assert second.skipped_stale == 0
    async with postgres_session_factory() as session:
        dispatched = await session.get(RecoveryJobItem, item.id)
        assert dispatched is not None
        assert dispatched.status is RecoveryJobItemStatus.DISPATCHED
        stage = await session.get(PipelineStageJournal, uuid.UUID(dispatched.work_id))
        assert stage is not None
        assert stage.status is ContentPipelineStageStatus.PENDING
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.RUNNING


async def test_dead_letter_archive_wins_before_recovery_lock(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-archive-race@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    event_id = uuid.uuid7()
    prerequisite = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=5,
        last_event_id=event_id,
        normalized_reason="ocr_timeout",
        last_error_text="OCR timed out.",
        is_retryable=True,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((admin, prerequisite, stage))
    await migrated_db_session.flush()
    dead_letter = _dead_letter_for(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stage.id),
        event_id=event_id,
    )
    migrated_db_session.add(dead_letter)
    await migrated_db_session.commit()
    work = await AdminRecoveryService(migrated_db_session).get_work(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(stage.id),
    )
    job, item = await _seed_recovery_job_item(
        migrated_db_session,
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        action=RecoveryCapability.RECOVER_DEAD_LETTER,
        expected_version=work.version,
    )
    await migrated_db_session.commit()

    before_dead_letter_lock = asyncio.Event()
    allow_dead_letter_lock = asyncio.Event()
    original_lock = RecoveryRuntime._lock_recovery_dead_letter

    async def _delay_dead_letter_lock(
        self: RecoveryRuntime,
        session: AsyncSession,
        recovery_item: RecoveryJobItem,
        recovery_work: RecoveryWorkRead,
    ) -> None:
        before_dead_letter_lock.set()
        await allow_dead_letter_lock.wait()
        await original_lock(self, session, recovery_item, recovery_work)

    monkeypatch.setattr(RecoveryRuntime, "_lock_recovery_dead_letter", _delay_dead_letter_lock)
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())
    dispatch_task = asyncio.create_task(runtime.dispatch_general_batch(batch_size=1))
    dead_letter_lock_wait = asyncio.create_task(before_dead_letter_lock.wait())
    done, _pending = await asyncio.wait(
        (dispatch_task, dead_letter_lock_wait),
        timeout=10,
        return_when=asyncio.FIRST_COMPLETED,
    )
    early_dispatch_result = dispatch_task.result() if dispatch_task.done() else None
    assert dead_letter_lock_wait in done, (
        f"Recovery dispatch ended before locking the dead letter: {early_dispatch_result}"
    )

    async with postgres_session_factory() as session:
        persisted_dead_letter = await session.get(PipelineDeadLetter, dead_letter.id, with_for_update=True)
        assert persisted_dead_letter is not None
        persisted_dead_letter.status = RecoveryDeadLetterStatus.ARCHIVED
        persisted_dead_letter.resolved_at = utcnow()
        persisted_dead_letter.resolution_note = "Archived concurrently by an operator."
        await session.commit()
    allow_dead_letter_lock.set()

    result = await dispatch_task
    assert result.claimed == result.skipped_stale == 1
    assert result.dispatched == 0
    assert await runtime.reconcile(batch_size=10) == 0
    async with postgres_session_factory() as session:
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        persisted_dead_letter = await session.get(PipelineDeadLetter, dead_letter.id)
        persisted_item = await session.get(RecoveryJobItem, item.id)
        persisted_job = await session.get(RecoveryJob, job.id)
        assert persisted_stage is not None
        assert persisted_stage.status is ContentPipelineStageStatus.FAILED
        assert persisted_stage.last_event_id == event_id
        assert persisted_dead_letter is not None
        assert persisted_dead_letter.status is RecoveryDeadLetterStatus.ARCHIVED
        assert persisted_item is not None
        assert persisted_item.status is RecoveryJobItemStatus.SKIPPED_STALE
        assert persisted_job is not None
        assert persisted_job.status is RecoveryJobStatus.COMPLETED_WITH_FAILURES
