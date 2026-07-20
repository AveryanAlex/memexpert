# ruff: noqa: TC002
"""Media-state version fencing for Replay & Repair derivative work."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from memexpert.core.config import Settings
from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    MediaGenerationStatus,
    RecoveryCapability,
    RecoveryJobItemStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
)
from memexpert.models.operations import MediaGeneration, RecoveryJobItem
from memexpert.models.user import User
from memexpert.schemas.admin_recovery import (
    RecoveryActionRequest,
    RecoveryBatchPreviewRequest,
    RecoveryExplicitSelector,
    RecoveryQueryFilters,
    RecoveryQuerySelector,
    RecoveryWorkReference,
)
from memexpert.services.admin_recovery import AdminRecoveryConflictError, AdminRecoveryService
from memexpert.services.recovery_runtime import RecoveryRuntime

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_MEDIA_STATE_FIELDS = (
    "s3_original_key",
    "s3_web_video_key",
    "active_media_generation_id",
    "web_video_profile",
    "web_video_verified_at",
    "source_has_audio",
    "web_video_has_audio",
    "mime_type",
    "status",
    "updated_at",
)


async def _seed_outdated_video(
    session: AsyncSession,
) -> tuple[User, MemeFile, PipelineStageJournal, MediaGeneration, MediaGeneration]:
    admin = User(email=f"media-version-{uuid.uuid7()}@example.com", is_admin=True)
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.VIDEO)
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"tests/recovery/media-version/{file_id}/original-a.webm",
        s3_web_video_key=f"pipeline/derived/{file_id}/generations/active-a/web.mp4",
        mime_type="video/webm",
        web_video_profile="legacy-profile-a",
        web_video_verified_at=utcnow() - timedelta(days=2),
        source_has_audio=True,
        web_video_has_audio=True,
    )
    session.add_all((admin, meme))
    await session.flush()
    session.add(file)
    await session.flush()
    active_generation = MediaGeneration(
        meme_file_id=file.id,
        web_video_object_key=file.s3_web_video_key,
        preview_image_object_key=f"pipeline/derived/{file.id}/generations/active-a/preview.png",
        profile="legacy-profile-a",
        retry_limit=3,
        status=MediaGenerationStatus.ACTIVE,
    )
    replacement_generation = MediaGeneration(
        meme_file_id=file.id,
        web_video_object_key=f"pipeline/derived/{file.id}/generations/active-b/web.mp4",
        preview_image_object_key=f"pipeline/derived/{file.id}/generations/active-b/preview.png",
        profile="legacy-profile-b",
        retry_limit=3,
        status=MediaGenerationStatus.VERIFIED,
    )
    session.add_all((active_generation, replacement_generation))
    await session.flush()
    file.active_media_generation_id = active_generation.id
    stage = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=2,
        last_event_id=uuid.uuid7(),
        normalized_reason="transcode_failed",
        is_retryable=True,
        finished_at=utcnow(),
    )
    session.add(stage)
    await session.commit()
    return admin, file, stage, active_generation, replacement_generation


async def _change_media_state(
    session: AsyncSession,
    file: MemeFile,
    active_generation: MediaGeneration,
    replacement_generation: MediaGeneration,
    field: str,
) -> None:
    if field == "s3_original_key":
        file.s3_original_key = f"tests/recovery/media-version/{file.id}/original-b.webm"
    elif field == "s3_web_video_key":
        file.s3_web_video_key = replacement_generation.web_video_object_key
    elif field == "active_media_generation_id":
        active_generation.status = MediaGenerationStatus.SUPERSEDED
        replacement_generation.status = MediaGenerationStatus.ACTIVE
        file.active_media_generation_id = replacement_generation.id
    elif field == "web_video_profile":
        file.web_video_profile = "legacy-profile-b"
    elif field == "web_video_verified_at":
        assert file.web_video_verified_at is not None
        file.web_video_verified_at += timedelta(hours=1)
    elif field == "source_has_audio":
        file.source_has_audio = False
    elif field == "web_video_has_audio":
        file.web_video_has_audio = False
    elif field == "mime_type":
        file.mime_type = "video/mp4"
    elif field == "status":
        file.status = ContentProcessingStatus.FAILED
    elif field == "updated_at":
        file.updated_at += timedelta(seconds=1)
    else:  # pragma: no cover - the parametrization is the closed field set.
        raise AssertionError(f"Unsupported media-state field: {field}")
    await session.commit()


@pytest.mark.parametrize("field", _MEDIA_STATE_FIELDS)
async def test_explicit_preview_fences_every_reviewed_media_state_field(
    migrated_db_session: AsyncSession,
    field: str,
) -> None:
    admin, file, stage, active_generation, replacement_generation = await _seed_outdated_video(
        migrated_db_session
    )
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(stage.id))
    assert candidate.work.version.startswith("media-v1:")
    assert len(candidate.work.version) == 73
    assert file.s3_original_key not in candidate.work.version
    assert file.s3_web_video_key is not None
    assert file.s3_web_video_key not in candidate.work.version
    assert (
        await service.get_work(RecoveryWorkKind.PIPELINE_STAGE, str(stage.id))
    ).version == candidate.work.version
    preview = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            scope=RecoveryReplayScope.STAGE_ONLY,
            reason="Review one derivative regeneration before scheduling it.",
            selector=RecoveryExplicitSelector(
                items=[
                    RecoveryWorkReference(
                        kind=RecoveryWorkKind.PIPELINE_STAGE,
                        id=str(stage.id),
                        version=candidate.work.version,
                    )
                ]
            ),
        ),
    )

    await _change_media_state(
        migrated_db_session,
        file,
        active_generation,
        replacement_generation,
        field,
    )
    changed = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(stage.id))
    assert changed.work.version != candidate.work.version

    with pytest.raises(AdminRecoveryConflictError, match="changed work"):
        await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=preview.id,
            version=preview.version,
            reason="Schedule only the reviewed media state.",
        )


@pytest.mark.parametrize("field", _MEDIA_STATE_FIELDS)
async def test_outdated_query_preview_fences_every_materialized_media_state_field(
    migrated_db_session: AsyncSession,
    field: str,
) -> None:
    admin, file, _stage, active_generation, replacement_generation = await _seed_outdated_video(
        migrated_db_session
    )
    service = AdminRecoveryService(migrated_db_session)
    preparing = await service.preview_batch(
        admin_user_id=admin.id,
        payload=RecoveryBatchPreviewRequest(
            request_id=uuid.uuid7(),
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            scope=RecoveryReplayScope.STAGE_ONLY,
            reason="Materialize the exact outdated derivative set.",
            selector=RecoveryQuerySelector(filters=RecoveryQueryFilters(outdated_web_video=True)),
        ),
    )
    assert await service.materialize_next_preparing_job(page_size=100) is True
    assert await service.materialize_next_preparing_job(page_size=100) is True
    preview = await service.get_job(admin_user_id=admin.id, job_id=preparing.id)
    assert preview.total_count == 1
    item = await migrated_db_session.scalar(
        select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == preview.id)
    )
    assert item is not None
    assert item.expected_version.startswith("media-v1:")

    await _change_media_state(
        migrated_db_session,
        file,
        active_generation,
        replacement_generation,
        field,
    )

    with pytest.raises(AdminRecoveryConflictError, match="media state changed"):
        await service.schedule_batch(
            admin_user_id=admin.id,
            job_id=preview.id,
            version=preview.version,
            reason="Reject state that changed after exact materialization.",
        )


@pytest.mark.parametrize("field", _MEDIA_STATE_FIELDS)
async def test_runtime_admission_fences_every_scheduled_media_state_field(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    field: str,
) -> None:
    admin, file, stage, active_generation, replacement_generation = await _seed_outdated_video(
        migrated_db_session
    )
    service = AdminRecoveryService(migrated_db_session)
    candidate = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(stage.id))
    job = await service.perform_action(
        admin_user_id=admin.id,
        kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(stage.id),
        payload=RecoveryActionRequest(
            request_id=uuid.uuid7(),
            version=candidate.work.version,
            reason="Queue derivative regeneration with a reviewed media-state fence.",
            action=RecoveryCapability.REGENERATE_DERIVATIVES,
            scope=RecoveryReplayScope.STAGE_ONLY,
        ),
    )

    await _change_media_state(
        migrated_db_session,
        file,
        active_generation,
        replacement_generation,
        field,
    )
    result = await RecoveryRuntime(
        session_factory=postgres_session_factory,
        settings=Settings(),
    ).dispatch_general_batch(batch_size=1)
    assert result.claimed == 1
    assert result.skipped_stale == 1
    assert result.dispatched == 0
    async with postgres_session_factory() as session:
        item = await session.scalar(
            select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == job.id)
        )
        assert item is not None
        assert item.status is RecoveryJobItemStatus.SKIPPED_STALE
        assert item.normalized_reason == "canonical_state_changed"


async def test_non_media_and_non_transcode_stage_versions_keep_journal_semantics(
    migrated_db_session: AsyncSession,
) -> None:
    _admin, file, transcode, _active_generation, _replacement_generation = await _seed_outdated_video(
        migrated_db_session
    )
    ocr = PipelineStageJournal(
        meme_file_id=file.id,
        stage=ContentPipelineStage.OCR,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        normalized_reason="ocr_failed",
        is_retryable=True,
        finished_at=utcnow(),
    )
    image_meme_id = uuid.uuid7()
    image_file_id = uuid.uuid7()
    image_meme = Meme(id=image_meme_id, primary_file_id=image_file_id, media_type=ContentKind.IMAGE)
    image_file = MemeFile(
        id=image_file_id,
        meme_id=image_meme_id,
        status=ContentProcessingStatus.FAILED,
        s3_original_key=f"tests/recovery/media-version/{image_file_id}/original-a.jpg",
        mime_type="image/jpeg",
    )
    image_transcode = PipelineStageJournal(
        meme_file_id=image_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        normalized_reason="image_transcode_failed",
        is_retryable=True,
        finished_at=utcnow(),
    )
    migrated_db_session.add_all((ocr, image_meme))
    await migrated_db_session.flush()
    migrated_db_session.add(image_file)
    await migrated_db_session.flush()
    migrated_db_session.add(image_transcode)
    await migrated_db_session.commit()
    service = AdminRecoveryService(migrated_db_session)
    ocr_before = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    image_before = await service.get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(image_transcode.id),
    )
    assert not ocr_before.work.version.startswith("media-v1:")
    assert not image_before.work.version.startswith("media-v1:")

    file.web_video_profile = "another-legacy-profile"
    image_file.s3_original_key = f"tests/recovery/media-version/{image_file_id}/original-b.jpg"
    await migrated_db_session.commit()

    ocr_after = await service.get_candidate(RecoveryWorkKind.PIPELINE_STAGE, str(ocr.id))
    image_after = await service.get_candidate(
        RecoveryWorkKind.PIPELINE_STAGE,
        str(image_transcode.id),
    )
    assert ocr_after.work.version == ocr_before.work.version
    assert image_after.work.version == image_before.work.version
    assert transcode.stage is ContentPipelineStage.TRANSCODE
