"""Recovery control-plane guarantees for durable source-object presence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

import memexpert.services.admin_recovery as admin_recovery_module
from memexpert.core.config import Settings
from memexpert.core.storage import StorageObjectPresence
from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    RecoveryCapability,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
)
from memexpert.models.operations import RecoveryJob, RecoveryJobItem
from memexpert.models.user import User
from memexpert.schemas.admin_recovery import (
    RecoveryActionRequest,
    RecoveryBatchPreviewRequest,
    RecoveryExplicitSelector,
    RecoveryQueryFilters,
    RecoveryQuerySelector,
    RecoveryRetryFailedPreviewRequest,
    RecoveryWorkReference,
)
from memexpert.services.admin_recovery import (
    AdminRecoveryConflictError,
    AdminRecoveryService,
    AdminRecoveryStorageUnavailableError,
)
from memexpert.services.recovery_runtime import RecoveryRuntime

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class _PresenceChecker:
    presence: StorageObjectPresence
    calls: list[str] = field(default_factory=list)

    async def __call__(self, object_key: str) -> StorageObjectPresence:
        self.calls.append(object_key)
        return self.presence


async def _seed_ocr_replay(
    session: AsyncSession,
    *,
    email: str,
) -> tuple[User, MemeFile, PipelineStageJournal]:
    admin = User(email=email, is_admin=True)
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.IMAGE)
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"tests/recovery/presence/{file_id}/original.png",
        mime_type="image/png",
    )
    transcode = PipelineStageJournal(
        meme_file_id=file_id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    ocr = PipelineStageJournal(
        meme_file_id=file_id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=2,
        last_event_id=uuid.uuid7(),
        normalized_reason="ocr_timeout",
        is_retryable=True,
        finished_at=utcnow(),
    )
    session.add_all((admin, meme))
    await session.flush()
    session.add(meme_file)
    await session.flush()
    session.add_all((transcode, ocr))
    await session.commit()
    return admin, meme_file, ocr


@pytest.mark.parametrize(
    ("presence", "blocked_text"),
    [
        (StorageObjectPresence.MISSING, "missing from storage"),
        (StorageObjectPresence.UNAVAILABLE, "temporarily unavailable"),
    ],
)
async def test_candidate_and_direct_action_use_actual_original_presence(
    migrated_db_session: AsyncSession,
    presence: StorageObjectPresence,
    blocked_text: str,
) -> None:
    admin, meme_file, ocr = await _seed_ocr_replay(
        migrated_db_session,
        email=f"candidate-{presence.value}@example.com",
    )
    checker = _PresenceChecker(presence)
    service = AdminRecoveryService(
        migrated_db_session,
        object_presence_checker=checker,
    )

    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    replay = next(
        action for action in candidate.actions if action.capability is RecoveryCapability.REPLAY_STAGE
    )

    assert replay.available is False
    assert any(blocked_text in blocker for blocker in replay.blocked_prerequisites)
    assert checker.calls == [meme_file.s3_original_key]
    with pytest.raises(AdminRecoveryConflictError, match=blocked_text):
        await service.perform_action(
            admin_user_id=admin.id,
            kind=RecoveryWorkKind.PIPELINE_STAGE,
            work_id=str(ocr.id),
            payload=RecoveryActionRequest(
                request_id=uuid.uuid7(),
                version=candidate.work.version,
                reason="Do not schedule work whose original cannot be verified.",
                action=RecoveryCapability.REPLAY_STAGE,
                scope=RecoveryReplayScope.STAGE_ONLY,
            ),
        )


async def test_explicit_preview_rejects_definitively_missing_original(
    migrated_db_session: AsyncSession,
) -> None:
    admin, _meme_file, ocr = await _seed_ocr_replay(
        migrated_db_session,
        email="explicit-missing@example.com",
    )
    present_service = AdminRecoveryService(
        migrated_db_session,
        object_presence_checker=_PresenceChecker(StorageObjectPresence.PRESENT),
    )
    candidate = await present_service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    missing_service = AdminRecoveryService(
        migrated_db_session,
        object_presence_checker=_PresenceChecker(StorageObjectPresence.MISSING),
    )

    with pytest.raises(AdminRecoveryConflictError, match="missing from storage"):
        await missing_service.preview_batch(
            admin_user_id=admin.id,
            payload=RecoveryBatchPreviewRequest(
                request_id=uuid.uuid7(),
                action=RecoveryCapability.REPLAY_STAGE,
                scope=RecoveryReplayScope.STAGE_ONLY,
                reason="Reject an explicit selection with a missing original.",
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


async def test_schedule_revalidates_original_presence_before_reserving_items(
    migrated_db_session: AsyncSession,
) -> None:
    admin, _meme_file, ocr = await _seed_ocr_replay(
        migrated_db_session,
        email="schedule-missing@example.com",
    )
    checker = _PresenceChecker(StorageObjectPresence.PRESENT)
    service = AdminRecoveryService(migrated_db_session, object_presence_checker=checker)
    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REPLAY_STAGE,
            scope=RecoveryReplayScope.STAGE_ONLY,
            reason="Preview while the original is present.",
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
    checker.presence = StorageObjectPresence.MISSING

    with pytest.raises(AdminRecoveryConflictError, match="missing from storage"):
        await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=preview.id,
            version=preview.version,
            reason="Revalidate immediately before scheduling.",
        )

    persisted_job = await migrated_db_session.get(RecoveryJob, preview.id)
    item = await migrated_db_session.scalar(
        select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id)
    )
    assert persisted_job is not None and persisted_job.status is RecoveryJobStatus.PREVIEW
    assert item is not None and item.reservation_active is False


async def test_query_materialization_outage_keeps_page_unadvanced_then_missing_is_excluded(
    migrated_db_session: AsyncSession,
) -> None:
    admin = User(email="query-storage-outage@example.com", is_admin=True)
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.VIDEO)
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"tests/recovery/presence/{file_id}/original.webm",
        s3_web_video_key=f"pipeline/derived/{file_id}/web.mp4",
        mime_type="video/webm",
        web_video_profile="legacy-15fps",
        source_has_audio=True,
        web_video_has_audio=False,
    )
    transcode = PipelineStageJournal(
        meme_file_id=file_id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        is_retryable=False,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((admin, meme))
    await migrated_db_session.flush()
    migrated_db_session.add(meme_file)
    await migrated_db_session.flush()
    migrated_db_session.add(transcode)
    await migrated_db_session.commit()
    checker = _PresenceChecker(StorageObjectPresence.UNAVAILABLE)
    service = AdminRecoveryService(migrated_db_session, object_presence_checker=checker)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            scope=RecoveryReplayScope.STAGE_ONLY,
            reason="Materialize the exact outdated-video set safely.",
            selector=RecoveryQuerySelector(
                filters=RecoveryQueryFilters(outdated_web_video=True),
            ),
        ),
    )
    admin_id = admin.id
    preparing_id = preparing.id

    assert await service.materialize_next_preparing_job(page_size=10) is True
    assert checker.calls == []
    assert await service.materialize_next_preparing_job(page_size=10) is False
    stalled = await service.get_job(admin_user_id=admin_id, job_id=preparing_id)
    assert stalled.status is RecoveryJobStatus.PREPARING
    assert stalled.preparation_scanned_count == 0
    persisted_stalled = await migrated_db_session.get(RecoveryJob, preparing_id)
    assert persisted_stalled is not None
    assert persisted_stalled.materialization_cursor is None
    assert persisted_stalled.materialization_lease_owner is None

    checker.presence = StorageObjectPresence.MISSING
    assert await service.materialize_next_preparing_job(page_size=10) is True
    preview = await service.get_job(admin_user_id=admin_id, job_id=preparing_id)
    assert preview.status is RecoveryJobStatus.PREVIEW
    assert preview.preparation_scanned_count == 1
    assert preview.selected_root_count == 0
    assert preview.exclusions_by_reason == {"missing_original": 1}


async def test_runtime_admission_waits_on_outage_and_terminally_skips_missing_original(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, meme_file, ocr = await _seed_ocr_replay(
        migrated_db_session,
        email="runtime-presence@example.com",
    )
    candidate = await AdminRecoveryService(migrated_db_session).get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(ocr.id),
    )
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        assigned_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.QUEUED,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Fence source presence again at runtime admission.",
        total_count=1,
        selected_root_count=1,
        expanded_execution_count=1,
        queued_count=1,
        scheduled_at=utcnow(),
    )
    migrated_db_session.add(job)
    await migrated_db_session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.OCR,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(ocr.id),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version=candidate.work.version,
        retry_limit=3,
        reservation_active=True,
        status=RecoveryJobItemStatus.QUEUED,
    )
    migrated_db_session.add(item)
    await migrated_db_session.commit()
    checker = _PresenceChecker(StorageObjectPresence.UNAVAILABLE)
    monkeypatch.setattr(admin_recovery_module, "check_pipeline_object_presence", checker)
    runtime = RecoveryRuntime(session_factory=postgres_session_factory, settings=Settings())

    unavailable = await runtime.dispatch_general_batch(batch_size=1)
    assert unavailable.waiting_capacity == 1
    async with postgres_session_factory() as session:
        waiting = await session.get(RecoveryJobItem, item.id)
        assert waiting is not None
        assert waiting.status is RecoveryJobItemStatus.WAITING_CAPACITY
        assert waiting.retryable_failures_consumed == 0
        assert waiting.reservation_active is True

    checker.presence = StorageObjectPresence.MISSING
    missing = await runtime.dispatch_general_batch(batch_size=1)
    assert missing.skipped_stale == 1
    async with postgres_session_factory() as session:
        skipped = await session.get(RecoveryJobItem, item.id)
        assert skipped is not None
        assert skipped.status is RecoveryJobItemStatus.SKIPPED_STALE
        assert skipped.normalized_reason == "missing_original"
        assert skipped.retryable_failures_consumed == 0
        assert skipped.reservation_active is False


async def test_retry_failed_preview_aborts_instead_of_excluding_storage_outage(
    migrated_db_session: AsyncSession,
) -> None:
    admin, meme_file, ocr = await _seed_ocr_replay(
        migrated_db_session,
        email="retry-failed-storage-outage@example.com",
    )
    candidate = await AdminRecoveryService(migrated_db_session).get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(ocr.id),
    )
    source_job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        assigned_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.COMPLETED_WITH_FAILURES,
        action=RecoveryCapability.REPLAY_STAGE,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Source job with one failed replay item.",
        total_count=1,
        selected_root_count=1,
        expanded_execution_count=1,
        completed_count=1,
        failed_count=1,
        completed_at=utcnow(),
    )
    migrated_db_session.add(source_job)
    await migrated_db_session.flush()
    source_item = RecoveryJobItem(
        recovery_job_id=source_job.id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.OCR,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(ocr.id),
        action=RecoveryCapability.REPLAY_STAGE,
        expected_version=candidate.work.version,
        canonical_version=candidate.work.version,
        retry_limit=3,
        status=RecoveryJobItemStatus.FAILED,
        finished_at=utcnow(),
        reservation_active=False,
    )
    migrated_db_session.add(source_item)
    await migrated_db_session.commit()
    source_read = await AdminRecoveryService(migrated_db_session).get_job(
        admin_user_id=admin.id,
        job_id=source_job.id,
    )
    unavailable_service = AdminRecoveryService(
        migrated_db_session,
        object_presence_checker=_PresenceChecker(StorageObjectPresence.UNAVAILABLE),
    )

    with pytest.raises(AdminRecoveryStorageUnavailableError, match="temporarily unavailable"):
        await unavailable_service.preview_failed_items(
            admin_user_id=admin.id,
            job_id=source_job.id,
            payload=RecoveryRetryFailedPreviewRequest(
                request_id=uuid.uuid7(),
                version=source_read.version,
                reason="Do not shrink retry membership during a storage outage.",
            ),
        )

    await migrated_db_session.rollback()
    job_count = await migrated_db_session.scalar(select(func.count(RecoveryJob.id)))
    assert job_count == 1
