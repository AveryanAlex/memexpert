# ruff: noqa: TC002
"""Integration coverage for durable recovery execution and reliability state."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.core.config import Settings
from memexpert.models.base import utcnow
from memexpert.models.content import (
    Meme,
    MemeFile,
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
    PipelineAttemptOutcome,
    PipelineCapacityStatus,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    RecoveryCapability,
    RecoveryDeadLetterStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryWorkKind,
    SourcePlatform,
)
from memexpert.models.operations import (
    DependencyCircuitState,
    OperationalAuditLog,
    PipelineCapacityState,
    PipelineDeadLetter,
    PipelineStageAttempt,
    RecoveryJob,
    RecoveryJobItem,
)
from memexpert.models.user import User
from memexpert.pipeline.events import MEDIA_INSPECT_REQUESTED_EVENT_TYPE, PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.admin_recovery import (
    RecoveryBatchPreviewRequest,
    RecoveryJobRead,
    RecoveryMutationRequest,
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
            assert await session.scalar(
                select(RecoveryJob.id).where(RecoveryJob.id == result.id)
            ) == result.id
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
            (
                await session.execute(
                    select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == first.id)
                )
            )
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
        capability=RecoveryCapability.REBUILD_OUTBOX,
        reason="Preview the broker recovery batch.",
        items=[
            RecoveryWorkReference(
                kind=RecoveryWorkKind.OUTBOX,
                id=str(outbox.id),
                version=work.version,
            )
        ],
    )
    barrier = asyncio.Barrier(2)

    async def create() -> RecoveryJobRead:
        async with postgres_session_factory() as session:
            result = await _FirstLookupBarrierRecoveryService(session, barrier).preview_batch(
                admin_user_id=admin.id,
                payload=payload,
            )
            assert await session.scalar(
                select(RecoveryJob.id).where(RecoveryJob.id == result.id)
            ) == result.id
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
            (
                await session.execute(
                    select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == first.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 1
    assert len(items) == 1


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
                capability=RecoveryCapability.REBUILD_OUTBOX,
                reason="Reject a duplicate recovery selection.",
                items=[reference, reference],
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
            capability=RecoveryCapability.REBUILD_OUTBOX,
            reason="Preview a serialized transition.",
            items=[
                RecoveryWorkReference(
                    kind=RecoveryWorkKind.OUTBOX,
                    id=str(outbox.id),
                    version=work.version,
                )
            ],
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
            (
                await session.execute(select(RecoveryJob).where(RecoveryJob.id.in_((first_job.id, second_job.id))))
            )
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
            persisted_outbox.attempt_count = settings.pipeline_broker_retry_max_attempts
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
        assert persisted_job.completed_count == 1
        assert persisted_job.failed_count == 1


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


async def test_dead_letter_archive_wins_before_recovery_lock(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(email="recovery-archive-race@example.com", is_admin=True)
    file = await _seed_meme_file(migrated_db_session)
    event_id = uuid.uuid7()
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
    migrated_db_session.add_all((admin, stage))
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
    await before_dead_letter_lock.wait()

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
