"""Integration coverage for immutable moving-media generations."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, select
from sqlalchemy import inspect as sa_inspect

from memexpert.core.config import Settings
from memexpert.core.storage import (
    StorageConnectionError,
    build_preview_image_generation_object_key,
    build_web_video_generation_object_key,
)
from memexpert.media.contracts import (
    WEB_VIDEO_PROFILE_ID,
    AudioStreamObservation,
    MediaFrameRate,
    MediaProbeObservations,
    MediaValidationError,
    NormalizedMediaResult,
    PipelineMediaProcessorProtocol,
    UploadMediaDetails,
    VideoStreamObservation,
    WebVideoFrameRateMode,
)
from memexpert.models import metadata, utcnow
from memexpert.models.content import Meme, MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    MediaGenerationCleanupStatus,
    MediaGenerationStatus,
    RecoveryCapability,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
)
from memexpert.models.operations import MediaGeneration, RecoveryJob, RecoveryJobItem
from memexpert.models.user import User
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType
from memexpert.services.media_generation import (
    MediaGenerationConflictError,
    MediaGenerationError,
    MediaGenerationService,
    fail_and_cleanup_unactivated_generation,
    run_media_generation_gc_batch,
)
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext
from memexpert.workers.pipeline_runtime.stages.transcode import run_transcode_stage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class RecordingStorageClient:
    """S3-compatible deletion double used by cleanup and GC tests."""

    delete_calls: list[dict[str, str]] = field(default_factory=list)
    fail_key: str | None = None

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        if Key == self.fail_key:
            raise RuntimeError("simulated object deletion failure")
        return {"DeleteMarker": True}


@dataclass(slots=True)
class StorageBody:
    payload: bytes

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        return None


@dataclass(slots=True)
class GenerationStorageClient:
    """Record the immutable upload/verification order and inject failures."""

    objects: dict[str, bytes]
    failure_mode: str | None = None
    events: list[tuple[str, str]] = field(default_factory=list)
    delete_calls: list[dict[str, str]] = field(default_factory=list)

    def get_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        self.events.append(("get", Key))
        payload = self.objects[Key]
        return {"Body": StorageBody(payload), "ContentLength": len(payload)}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object:
        _ = (Bucket, ContentType)
        self.events.append(("put", Key))
        if self.failure_mode == "video_upload" and Key.endswith("/web.mp4"):
            raise RuntimeError("simulated video upload failure")
        assert ContentLength == len(Body)
        self.objects[Key] = Body
        return {"ETag": "generation-test"}

    def head_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        self.events.append(("head", Key))
        observed_size = len(self.objects[Key])
        if self.failure_mode == "video_verify" and Key.endswith("/web.mp4"):
            observed_size += 1
        return {"ContentLength": observed_size}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop(Key, None)
        return {"DeleteMarker": True}


@dataclass(slots=True)
class GenerationAwareMediaProcessor:
    failure_mode: str | None = None

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("inspect_upload is outside this transcode test")

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
        generation_id: uuid.UUID | None = None,
    ) -> NormalizedMediaResult:
        _ = (filename, content_type, media_bytes)
        if self.failure_mode == "processor":
            raise RuntimeError("simulated FFmpeg or probe failure")
        assert generation_id is not None
        return _normalized_result_for_ids(meme_file_id, generation_id)

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("extract_preview_frame is outside this transcode test")


@dataclass(slots=True)
class GenerationUnawareMediaProcessor:
    """Legacy-shaped double that must never process moving media."""

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("inspect_upload is outside this transcode test")

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult:
        _ = (meme_file_id, filename, content_type, media_bytes)
        raise AssertionError("generation-unaware moving-media processor must not be called")

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("extract_preview_frame is outside this transcode test")


class NoopBroker:
    async def publish(self, message: object, /, **kwargs: object) -> None:
        _ = (message, kwargs)


def _generation_keys(meme_file_id: uuid.UUID, generation_id: uuid.UUID) -> tuple[str, str]:
    return (
        build_web_video_generation_object_key(meme_file_id, generation_id),
        build_preview_image_generation_object_key(meme_file_id, generation_id),
    )


def _probe_observations(*, output: bool, has_audio: bool = True) -> MediaProbeObservations:
    frame_rate = MediaFrameRate(30_000, 1_001)
    video = VideoStreamObservation(
        index=0,
        codec_name="h264" if output else "vp9",
        profile="High" if output else "Profile 0",
        level=41 if output else None,
        pixel_format="yuv420p",
        width=1280,
        height=720,
        average_frame_rate=frame_rate,
        real_frame_rate=frame_rate,
        start_time_seconds=0.0,
        duration_seconds=2.002,
        bit_rate=5_500_000 if output else 2_000_000,
        frame_count=60,
    )
    audio_streams = (
        (
            AudioStreamObservation(
                index=1,
                codec_name="aac" if output else "opus",
                profile="LC" if output else None,
                sample_rate=48_000,
                channels=2,
                channel_layout="stereo",
                start_time_seconds=0.0,
                duration_seconds=2.002,
                bit_rate=128_000 if output else 96_000,
            ),
        )
        if has_audio
        else ()
    )
    return MediaProbeObservations(
        format_names=("mov", "mp4", "m4a", "3gp", "3g2", "mj2") if output else ("matroska", "webm"),
        format_long_name="QuickTime / MOV" if output else "Matroska / WebM",
        start_time_seconds=0.0,
        duration_seconds=2.002,
        bit_rate=5_700_000 if output else 2_100_000,
        byte_size=1_426_000 if output else 525_000,
        video_streams=(video,),
        audio_streams=audio_streams,
        subtitle_stream_count=0,
        data_stream_count=0,
        attachment_stream_count=0,
        unknown_stream_types=(),
        chapter_count=0,
    )


def _normalized_result(generation: MediaGeneration, *, has_audio: bool = True) -> NormalizedMediaResult:
    return _normalized_result_for_ids(
        generation.meme_file_id,
        generation.id,
        has_audio=has_audio,
    )


def _normalized_result_for_ids(
    meme_file_id: uuid.UUID | None,
    generation_id: uuid.UUID,
    *,
    has_audio: bool = True,
) -> NormalizedMediaResult:
    assert meme_file_id is not None
    return NormalizedMediaResult(
        quality_score=0.91,
        blur_hash="L4AS~q00~q.8%MRjM{Rj00IU%MRj",
        preview_image_object_key=build_preview_image_generation_object_key(meme_file_id, generation_id),
        preview_image_bytes=b"verified-preview",
        web_video_object_key=build_web_video_generation_object_key(meme_file_id, generation_id),
        web_video_bytes=b"verified-video",
        generation_id=generation_id,
        web_video_profile=WEB_VIDEO_PROFILE_ID,
        frame_rate_mode=WebVideoFrameRateMode.PRESERVE,
        source_has_audio=has_audio,
        web_video_has_audio=has_audio,
        source_observations=_probe_observations(output=False, has_audio=has_audio),
        output_observations=_probe_observations(output=True, has_audio=has_audio),
        web_video_verified_at=utcnow(),
    )


def _stage_handler_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: GenerationStorageClient,
    processor: PipelineMediaProcessorProtocol,
) -> PipelineStageHandlerContext:
    unused = cast("Any", object())
    return PipelineStageHandlerContext(
        settings=Settings(),
        session_factory=session_factory,
        storage_client=storage,
        media_processor=processor,
        ocr_processor=unused,
        voyage_client=unused,
        qdrant_client=unused,
        qdrant_sync_client=unused,
        meilisearch_sync_client=unused,
        classification_client=unused,
        broker=cast("Any", NoopBroker()),
    )


async def _seed_ready_moving_file(
    session: AsyncSession,
    *,
    file_status: ContentProcessingStatus = ContentProcessingStatus.READY,
    stage_status: ContentPipelineStageStatus = ContentPipelineStageStatus.SUCCEEDED,
    stage_attempt: int = 1,
    stage_event_id: uuid.UUID | None = None,
) -> tuple[MemeFile, PipelineStageJournal, MediaGeneration]:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    old_generation_id = uuid.uuid7()
    old_video_key, old_preview_key = _generation_keys(meme_file_id, old_generation_id)
    now = utcnow()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.VIDEO,
        primary_file_id=meme_file_id,
        is_public=True,
    )
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=file_status,
        width=1280,
        height=720,
        file_size_bytes=525_000,
        mime_type="video/webm",
        s3_original_key=f"pipeline/originals/{meme_file_id}/source.webm",
        s3_web_video_key=old_video_key,
        source_has_audio=True,
        web_video_has_audio=True,
        web_video_profile="legacy-web-video-profile",
        web_video_verified_at=now - timedelta(days=30),
    )
    stage = PipelineStageJournal(
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        status=stage_status,
        attempt_count=stage_attempt,
        last_event_id=stage_event_id or uuid.uuid7(),
        is_retryable=stage_status is not ContentPipelineStageStatus.SUCCEEDED,
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1) if stage_status is ContentPipelineStageStatus.SUCCEEDED else None,
    )
    old_generation = MediaGeneration(
        id=old_generation_id,
        meme_file_id=meme_file_id,
        expected_web_video_object_key=None,
        web_video_object_key=old_video_key,
        preview_image_object_key=old_preview_key,
        profile="legacy-web-video-profile",
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.ACTIVE,
        activated_at=now - timedelta(days=30),
    )
    session.add(meme)
    await session.flush()
    session.add_all((meme_file, stage))
    await session.flush()
    session.add(old_generation)
    await session.flush()
    meme_file.active_media_generation_id = old_generation.id
    await session.commit()
    return meme_file, stage, old_generation


async def _seed_regeneration_item(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    stage: PipelineStageJournal,
    event_id: uuid.UUID,
    preserve_ready: bool = True,
) -> RecoveryJobItem:
    admin = User(email=f"media-generation-{uuid.uuid7()}@example.com", is_admin=True)
    session.add(admin)
    await session.flush()
    job = RecoveryJob(
        requested_by_admin_user_id=admin.id,
        request_id=uuid.uuid7(),
        status=RecoveryJobStatus.RUNNING,
        action=RecoveryCapability.REGENERATE_DERIVATIVES,
        scope=RecoveryReplayScope.STAGE_ONLY,
        retry_limit=3,
        reason="Verify immutable derivative activation.",
        total_count=1,
        scheduled_at=utcnow(),
    )
    session.add(job)
    await session.flush()
    item = RecoveryJobItem(
        recovery_job_id=job.id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        work_kind=RecoveryWorkKind.PIPELINE_STAGE,
        work_id=str(meme_file.id),
        action=RecoveryCapability.REGENERATE_DERIVATIVES,
        expected_version="generation-test-v1",
        retry_limit=3,
        preserve_ready=preserve_ready,
        suppress_fanout=True,
        previous_stage_state={
            "status": ContentPipelineStageStatus.SUCCEEDED.value,
            "attempt_count": 1,
            "last_event_id": str(stage.last_event_id),
            "normalized_reason": None,
            "last_error_text": None,
            "is_retryable": False,
            "retry_after": None,
            "started_at": stage.started_at.isoformat() if stage.started_at is not None else None,
            "finished_at": stage.finished_at.isoformat() if stage.finished_at is not None else None,
        },
        reservation_active=True,
        status=RecoveryJobItemStatus.DISPATCHED,
        dispatch_event_id=event_id,
        dispatched_at=utcnow(),
    )
    session.add(item)
    await session.commit()
    return item


def test_media_generation_metadata_declares_durable_safety_contract() -> None:
    table = metadata.tables["media_generations"]
    meme_files = metadata.tables["meme_files"]

    assert {
        "meme_file_id",
        "recovery_item_id",
        "expected_web_video_object_key",
        "web_video_object_key",
        "preview_image_object_key",
        "profile",
        "retry_limit",
        "attempt_count",
        "status",
        "previous_file_status",
        "previous_stage_status",
        "previous_stage_observations",
        "source_observations",
        "output_observations",
        "source_width",
        "source_height",
        "source_frame_rate_numerator",
        "source_frame_rate_denominator",
        "source_duration_seconds",
        "source_has_audio",
        "output_width",
        "output_height",
        "output_frame_rate_numerator",
        "output_frame_rate_denominator",
        "output_duration_seconds",
        "output_video_bitrate",
        "output_byte_size",
        "output_video_codec",
        "output_audio_codec",
        "output_has_audio",
        "safe_failure_reason",
        "safe_failure_text",
        "verified_at",
        "uploaded_at",
        "activated_at",
        "superseded_at",
        "cleanup_status",
        "cleanup_attempt_count",
        "cleanup_error_text",
        "cleanup_at",
    }.issubset(table.c.keys())
    assert {index.name for index in table.indexes} == {
        "ix_media_generations_cleanup_status_created",
        "ix_media_generations_file_created",
        "ix_media_generations_recovery_item",
        "ix_media_generations_status_superseded",
    }
    unique_constraints = {
        constraint.name for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints == {
        "uq_media_generations_preview_image_object_key",
        "uq_media_generations_web_video_object_key",
    }
    check_sql = " ".join(
        str(constraint.sqltext) for constraint in table.constraints if isinstance(constraint, CheckConstraint)
    )
    assert "retry_limit IN (1, 3, 5)" in check_sql
    assert "attempt_count >= 0" in check_sql
    assert "cleanup_attempt_count >= 0" in check_sql
    active_generation_fk = next(iter(meme_files.c["active_media_generation_id"].foreign_keys))
    assert active_generation_fk.column.table.name == "media_generations"
    assert active_generation_fk.ondelete == "SET NULL"
    assert {
        "source_has_audio",
        "web_video_has_audio",
        "web_video_profile",
        "web_video_verified_at",
    }.issubset(meme_files.c.keys())
    assert sa_inspect(MediaGeneration).columns["status"] is not None


async def test_generation_activation_requires_uploaded_state_and_switches_both_artifacts_atomically(
    migrated_db_session: AsyncSession,
) -> None:
    event_id = uuid.uuid7()
    meme_file, stage, previous_generation = await _seed_ready_moving_file(
        migrated_db_session,
        stage_status=ContentPipelineStageStatus.PROCESSING,
        stage_attempt=2,
        stage_event_id=event_id,
    )
    recovery_item = await _seed_regeneration_item(
        migrated_db_session,
        meme_file=meme_file,
        stage=stage,
        event_id=event_id,
    )
    previous_video_key = meme_file.s3_web_video_key
    service = MediaGenerationService(migrated_db_session, settings=Settings())
    generation = await service.reserve(
        meme_file_id=meme_file.id,
        expected_web_video_object_key=previous_video_key,
        recovery_item_id=recovery_item.id,
        retry_limit=3,
    )
    result = _normalized_result(generation)
    await service.record_verified(generation.id, result)

    with pytest.raises(MediaGenerationConflictError, match="upload-verified"):
        await PipelineStageCompletionService(migrated_db_session).complete_transcode_stage(
            meme_file_id=meme_file.id,
            attempt=2,
            event_id=event_id,
            result=result,
        )

    await migrated_db_session.refresh(meme_file)
    assert meme_file.s3_web_video_key == previous_video_key
    assert meme_file.active_media_generation_id == previous_generation.id
    assert meme_file.status is ContentProcessingStatus.READY

    await service.record_uploaded(generation.id)
    await PipelineStageCompletionService(migrated_db_session).complete_transcode_stage(
        meme_file_id=meme_file.id,
        attempt=2,
        event_id=event_id,
        result=result,
    )

    await migrated_db_session.refresh(meme_file)
    await migrated_db_session.refresh(stage)
    await migrated_db_session.refresh(generation)
    await migrated_db_session.refresh(previous_generation)
    await migrated_db_session.refresh(recovery_item)
    assert meme_file.s3_web_video_key == generation.web_video_object_key
    assert meme_file.active_media_generation_id == generation.id
    assert meme_file.source_has_audio is True
    assert meme_file.web_video_has_audio is True
    assert meme_file.web_video_profile == WEB_VIDEO_PROFILE_ID
    assert meme_file.web_video_verified_at == result.web_video_verified_at
    assert meme_file.status is ContentProcessingStatus.READY
    assert generation.status is MediaGenerationStatus.ACTIVE
    assert generation.cleanup_status is MediaGenerationCleanupStatus.NOT_ELIGIBLE
    assert generation.activated_at is not None
    assert previous_generation.status is MediaGenerationStatus.SUPERSEDED
    assert previous_generation.cleanup_status is MediaGenerationCleanupStatus.PENDING
    assert previous_generation.superseded_at == generation.activated_at
    assert stage.status is ContentPipelineStageStatus.SUCCEEDED
    assert recovery_item.reservation_active is True
    ocr_entry = await migrated_db_session.scalar(
        select(PipelineStageJournal).where(
            PipelineStageJournal.meme_file_id == meme_file.id,
            PipelineStageJournal.stage == ContentPipelineStage.OCR,
        )
    )
    assert ocr_entry is None


async def test_moving_media_completion_rejects_unreserved_legacy_output(
    migrated_db_session: AsyncSession,
) -> None:
    event_id = uuid.uuid7()
    meme_file, stage, previous_generation = await _seed_ready_moving_file(
        migrated_db_session,
        stage_status=ContentPipelineStageStatus.PROCESSING,
        stage_attempt=2,
        stage_event_id=event_id,
    )
    previous_video_key = meme_file.s3_web_video_key
    unreserved = NormalizedMediaResult(
        quality_score=0.8,
        blur_hash=None,
        preview_image_object_key=f"pipeline/derived/{meme_file.id}/preview.png",
        preview_image_bytes=b"legacy-preview",
        web_video_object_key=f"pipeline/derived/{meme_file.id}/web.mp4",
        web_video_bytes=b"legacy-video",
    )

    with pytest.raises(MediaGenerationConflictError, match="reserved immutable generation"):
        await PipelineStageCompletionService(migrated_db_session).complete_transcode_stage(
            meme_file_id=meme_file.id,
            attempt=2,
            event_id=event_id,
            result=unreserved,
        )

    await migrated_db_session.refresh(meme_file)
    await migrated_db_session.refresh(stage)
    assert meme_file.s3_web_video_key == previous_video_key
    assert meme_file.active_media_generation_id == previous_generation.id
    assert stage.status is ContentPipelineStageStatus.PROCESSING


async def test_moving_transcode_rejects_generation_unaware_processor_before_reservation(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = uuid.uuid7()
    meme_file, _stage, previous_generation = await _seed_ready_moving_file(
        migrated_db_session,
        stage_status=ContentPipelineStageStatus.PROCESSING,
        stage_attempt=2,
        stage_event_id=event_id,
    )
    storage = GenerationStorageClient(objects={meme_file.s3_original_key: b"source-webm"})
    dispatch_event = ContentPipelineDispatchEvent(
        event_id=event_id,
        event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=meme_file.s3_original_key,
        attempt=2,
        created_at=utcnow(),
    )
    stage_context = PipelineStageWorkContext(
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        mime_type=meme_file.mime_type,
        original_object_key=meme_file.s3_original_key,
        web_video_object_key=meme_file.s3_web_video_key,
        retry_limit=3,
    )

    with pytest.raises(MediaValidationError, match="immutable generation reservation support"):
        await run_transcode_stage(
            _stage_handler_context(
                session_factory=postgres_session_factory,
                storage=storage,
                processor=cast("Any", GenerationUnawareMediaProcessor()),
            ),
            dispatch_event=dispatch_event,
            stage_context=stage_context,
            attempt=2,
        )

    async with postgres_session_factory() as session:
        generations = (
            (await session.execute(select(MediaGeneration).where(MediaGeneration.meme_file_id == meme_file.id)))
            .scalars()
            .all()
        )
    assert [generation.id for generation in generations] == [previous_generation.id]
    assert storage.events == []


async def test_derivative_maintenance_preserves_non_ready_file_status_on_failure(
    migrated_db_session: AsyncSession,
) -> None:
    event_id = uuid.uuid7()
    meme_file, stage, _previous_generation = await _seed_ready_moving_file(
        migrated_db_session,
        file_status=ContentProcessingStatus.FAILED,
    )
    recovery_item = await _seed_regeneration_item(
        migrated_db_session,
        meme_file=meme_file,
        stage=stage,
        event_id=event_id,
        preserve_ready=False,
    )
    completion = PipelineStageCompletionService(migrated_db_session)

    context = await completion.start_stage_processing(
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        attempt=2,
        event_id=event_id,
    )

    assert context is not None and context.preserve_ready is True
    await migrated_db_session.refresh(meme_file)
    assert meme_file.status is ContentProcessingStatus.FAILED

    await completion.mark_stage_failed(
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        attempt=2,
        event_id=event_id,
        normalized_reason="media_generation_failed",
        last_error_text="The replacement derivative could not be generated.",
        retryable=False,
    )

    await migrated_db_session.refresh(meme_file)
    await migrated_db_session.refresh(stage)
    await migrated_db_session.refresh(recovery_item)
    assert meme_file.status is ContentProcessingStatus.FAILED
    assert stage.status is ContentPipelineStageStatus.SUCCEEDED
    assert stage.attempt_count == 1
    assert recovery_item.status is RecoveryJobItemStatus.FAILED
    assert recovery_item.reservation_active is False


async def test_verified_generation_persists_sanitized_source_and_output_observations(
    migrated_db_session: AsyncSession,
) -> None:
    meme_file, _stage, _previous_generation = await _seed_ready_moving_file(migrated_db_session)
    generation = await MediaGenerationService(migrated_db_session, settings=Settings()).reserve(
        meme_file_id=meme_file.id,
        expected_web_video_object_key=meme_file.s3_web_video_key,
        recovery_item_id=None,
        retry_limit=5,
    )
    result = _normalized_result(generation)

    await MediaGenerationService(migrated_db_session, settings=Settings()).record_verified(generation.id, result)
    await migrated_db_session.refresh(generation)

    assert generation.status is MediaGenerationStatus.VERIFIED
    assert generation.retry_limit == 5
    assert generation.attempt_count == 1
    assert generation.source_width == 1280
    assert generation.source_height == 720
    assert (generation.source_frame_rate_numerator, generation.source_frame_rate_denominator) == (30_000, 1_001)
    assert generation.source_duration_seconds == pytest.approx(2.002)
    assert generation.source_has_audio is True
    assert generation.output_width == 1280
    assert generation.output_height == 720
    assert (generation.output_frame_rate_numerator, generation.output_frame_rate_denominator) == (30_000, 1_001)
    assert generation.output_duration_seconds == pytest.approx(2.002)
    assert generation.output_video_bitrate == 5_500_000
    assert generation.output_byte_size == 1_426_000
    assert generation.output_video_codec == "h264"
    assert generation.output_audio_codec == "aac"
    assert generation.output_has_audio is True
    source_video_streams = cast("list[dict[str, object]]", generation.source_observations["video_streams"])
    output_video_streams = cast("list[dict[str, object]]", generation.output_observations["video_streams"])
    assert source_video_streams[0]["codec_name"] == "vp9"
    assert output_video_streams[0]["profile"] == "High"
    assert generation.verified_at == result.web_video_verified_at


async def test_retry_fences_in_progress_generation_with_fresh_immutable_object_keys(
    migrated_db_session: AsyncSession,
) -> None:
    meme_file, stage, _previous_generation = await _seed_ready_moving_file(migrated_db_session)
    recovery_item = await _seed_regeneration_item(
        migrated_db_session,
        meme_file=meme_file,
        stage=stage,
        event_id=uuid.uuid7(),
    )
    service = MediaGenerationService(migrated_db_session, settings=Settings())
    first = await service.reserve(
        meme_file_id=meme_file.id,
        expected_web_video_object_key=meme_file.s3_web_video_key,
        recovery_item_id=recovery_item.id,
        retry_limit=3,
    )
    second = await service.reserve(
        meme_file_id=meme_file.id,
        expected_web_video_object_key=meme_file.s3_web_video_key,
        recovery_item_id=recovery_item.id,
        retry_limit=3,
    )

    await migrated_db_session.refresh(first)
    assert second.id != first.id
    assert second.web_video_object_key != first.web_video_object_key
    assert second.preview_image_object_key != first.preview_image_object_key
    assert second.attempt_count == 2
    assert second.status is MediaGenerationStatus.GENERATING
    assert first.status is MediaGenerationStatus.STALE
    assert first.cleanup_status is MediaGenerationCleanupStatus.PENDING
    assert first.safe_failure_reason == "superseded_by_retry"
    with pytest.raises(MediaGenerationConflictError, match="currently generating"):
        await service.record_verified(first.id, _normalized_result(first))


async def test_transcode_handler_uploads_and_verifies_both_artifacts_before_activation(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = uuid.uuid7()
    meme_file, _stage, previous_generation = await _seed_ready_moving_file(
        migrated_db_session,
        stage_status=ContentPipelineStageStatus.PROCESSING,
        stage_attempt=2,
        stage_event_id=event_id,
    )
    assert meme_file.s3_web_video_key is not None
    storage = GenerationStorageClient(objects={meme_file.s3_original_key: b"source-webm"})
    dispatch_event = ContentPipelineDispatchEvent(
        event_id=event_id,
        event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=meme_file.s3_original_key,
        attempt=2,
        created_at=utcnow(),
    )
    stage_context = PipelineStageWorkContext(
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        mime_type=meme_file.mime_type,
        original_object_key=meme_file.s3_original_key,
        web_video_object_key=meme_file.s3_web_video_key,
        retry_limit=3,
    )

    await run_transcode_stage(
        _stage_handler_context(
            session_factory=postgres_session_factory,
            storage=storage,
            processor=GenerationAwareMediaProcessor(),
        ),
        dispatch_event=dispatch_event,
        stage_context=stage_context,
        attempt=2,
    )

    async with postgres_session_factory() as session:
        persisted_file = await session.get(MemeFile, meme_file.id)
        generations = (
            (
                await session.execute(
                    select(MediaGeneration)
                    .where(MediaGeneration.meme_file_id == meme_file.id)
                    .order_by(MediaGeneration.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    assert persisted_file is not None
    assert len(generations) == 2
    activated = next(generation for generation in generations if generation.id != previous_generation.id)
    assert activated.status is MediaGenerationStatus.ACTIVE
    assert persisted_file.active_media_generation_id == activated.id
    assert persisted_file.s3_web_video_key == activated.web_video_object_key
    assert storage.events == [
        ("get", meme_file.s3_original_key),
        ("put", activated.preview_image_object_key),
        ("put", activated.web_video_object_key),
        ("head", activated.preview_image_object_key),
        ("head", activated.web_video_object_key),
    ]
    assert activated.preview_image_object_key in storage.objects
    assert activated.web_video_object_key in storage.objects


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [
        ("processor", RuntimeError),
        ("video_upload", StorageConnectionError),
        ("video_verify", MediaGenerationError),
    ],
)
async def test_transcode_failures_leave_old_generation_live_and_clean_only_reserved_objects(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    failure_mode: str,
    expected_error: type[Exception],
) -> None:
    event_id = uuid.uuid7()
    meme_file, stage, previous_generation = await _seed_ready_moving_file(
        migrated_db_session,
        stage_status=ContentPipelineStageStatus.PROCESSING,
        stage_attempt=2,
        stage_event_id=event_id,
    )
    previous_video_key = meme_file.s3_web_video_key
    storage = GenerationStorageClient(
        objects={meme_file.s3_original_key: b"source-webm"},
        failure_mode=failure_mode if failure_mode != "processor" else None,
    )
    dispatch_event = ContentPipelineDispatchEvent(
        event_id=event_id,
        event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=meme_file.s3_original_key,
        attempt=2,
        created_at=utcnow(),
    )
    stage_context = PipelineStageWorkContext(
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=ContentPipelineStage.TRANSCODE,
        mime_type=meme_file.mime_type,
        original_object_key=meme_file.s3_original_key,
        web_video_object_key=previous_video_key,
        retry_limit=3,
    )

    with pytest.raises(expected_error):
        await run_transcode_stage(
            _stage_handler_context(
                session_factory=postgres_session_factory,
                storage=storage,
                processor=GenerationAwareMediaProcessor(failure_mode=failure_mode),
            ),
            dispatch_event=dispatch_event,
            stage_context=stage_context,
            attempt=2,
        )

    async with postgres_session_factory() as session:
        persisted_file = await session.get(MemeFile, meme_file.id)
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        generations = (
            (await session.execute(select(MediaGeneration).where(MediaGeneration.meme_file_id == meme_file.id)))
            .scalars()
            .all()
        )
    assert persisted_file is not None
    assert persisted_file.s3_web_video_key == previous_video_key
    assert persisted_file.active_media_generation_id == previous_generation.id
    assert persisted_file.status is ContentProcessingStatus.READY
    assert persisted_stage is not None
    assert persisted_stage.status is ContentPipelineStageStatus.PROCESSING
    assert len(generations) == 2
    failed = next(generation for generation in generations if generation.id != previous_generation.id)
    assert failed.status is MediaGenerationStatus.FAILED
    assert failed.cleanup_status is MediaGenerationCleanupStatus.DELETED
    assert {call["Key"] for call in storage.delete_calls} == {
        failed.preview_image_object_key,
        failed.web_video_object_key,
    }
    assert previous_video_key not in {call["Key"] for call in storage.delete_calls}


async def test_concurrent_pointer_change_fences_activation_and_marks_generation_stale(
    migrated_db_session: AsyncSession,
) -> None:
    meme_file, stage, previous_generation = await _seed_ready_moving_file(migrated_db_session)
    previous_video_key = meme_file.s3_web_video_key
    generation_service = MediaGenerationService(migrated_db_session, settings=Settings())
    generation = await generation_service.reserve(
        meme_file_id=meme_file.id,
        expected_web_video_object_key=previous_video_key,
        recovery_item_id=None,
        retry_limit=3,
    )
    result = _normalized_result(generation)
    await generation_service.record_verified(generation.id, result)
    await generation_service.record_uploaded(generation.id)

    concurrent_generation_id = uuid.uuid7()
    concurrent_video_key, concurrent_preview_key = _generation_keys(meme_file.id, concurrent_generation_id)
    concurrent_generation = MediaGeneration(
        id=concurrent_generation_id,
        meme_file_id=meme_file.id,
        expected_web_video_object_key=previous_video_key,
        web_video_object_key=concurrent_video_key,
        preview_image_object_key=concurrent_preview_key,
        profile=WEB_VIDEO_PROFILE_ID,
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.ACTIVE,
        activated_at=utcnow(),
    )
    migrated_db_session.add(concurrent_generation)
    await migrated_db_session.flush()
    meme_file.s3_web_video_key = concurrent_video_key
    meme_file.active_media_generation_id = concurrent_generation.id
    await migrated_db_session.commit()

    with pytest.raises(MediaGenerationConflictError, match="active web-video pointer changed"):
        await PipelineStageCompletionService(migrated_db_session).complete_transcode_stage(
            meme_file_id=meme_file.id,
            attempt=stage.attempt_count,
            event_id=uuid.uuid7(),
            result=result,
        )

    await migrated_db_session.refresh(meme_file)
    await migrated_db_session.refresh(generation)
    await migrated_db_session.refresh(previous_generation)
    await migrated_db_session.refresh(concurrent_generation)
    assert meme_file.s3_web_video_key == concurrent_video_key
    assert meme_file.active_media_generation_id == concurrent_generation.id
    assert concurrent_generation.status is MediaGenerationStatus.ACTIVE
    assert previous_generation.status is MediaGenerationStatus.ACTIVE
    assert generation.status is MediaGenerationStatus.STALE
    assert generation.cleanup_status is MediaGenerationCleanupStatus.PENDING
    assert generation.safe_failure_reason == "active_pointer_changed"


async def test_two_sessions_serialize_generation_activation_and_fence_stale_waiter(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file, stage, previous_generation = await _seed_ready_moving_file(migrated_db_session)
    previous_video_key = meme_file.s3_web_video_key
    assert previous_video_key is not None

    # Avoid ordinary transcode fan-out so this regression isolates the pointer
    # swap transaction. Both uploaded rows intentionally carry the same expected
    # pointer, reproducing two deliveries that reached activation concurrently.
    migrated_db_session.add(
        PipelineStageJournal(
            meme_file_id=meme_file.id,
            stage=ContentPipelineStage.OCR,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
            started_at=utcnow() - timedelta(minutes=1),
            finished_at=utcnow(),
        )
    )
    candidates: list[MediaGeneration] = []
    results: list[NormalizedMediaResult] = []
    for _ in range(2):
        generation_id = uuid.uuid7()
        video_key, preview_key = _generation_keys(meme_file.id, generation_id)
        generation = MediaGeneration(
            id=generation_id,
            meme_file_id=meme_file.id,
            expected_web_video_object_key=previous_video_key,
            web_video_object_key=video_key,
            preview_image_object_key=preview_key,
            profile=WEB_VIDEO_PROFILE_ID,
            retry_limit=3,
            attempt_count=1,
            status=MediaGenerationStatus.UPLOADED,
            verified_at=utcnow(),
            uploaded_at=utcnow(),
        )
        candidates.append(generation)
        results.append(_normalized_result(generation))
    migrated_db_session.add_all(candidates)
    await migrated_db_session.commit()

    event_ids = (uuid.uuid7(), uuid.uuid7())
    async with postgres_session_factory() as first_session, postgres_session_factory() as second_session:
        # Prime both identity maps with the old active pointer. The waiter must
        # refresh this object after acquiring FOR UPDATE; merely issuing a locked
        # SELECT is insufficient when expire_on_commit=False.
        cached_files = await asyncio.gather(
            first_session.get(MemeFile, meme_file.id),
            second_session.get(MemeFile, meme_file.id),
        )
        assert all(cached is not None and cached.s3_web_video_key == previous_video_key for cached in cached_files)

        completion_results = await asyncio.gather(
            PipelineStageCompletionService(first_session).complete_transcode_stage(
                meme_file_id=meme_file.id,
                attempt=stage.attempt_count,
                event_id=event_ids[0],
                result=results[0],
            ),
            PipelineStageCompletionService(second_session).complete_transcode_stage(
                meme_file_id=meme_file.id,
                attempt=stage.attempt_count,
                event_id=event_ids[1],
                result=results[1],
            ),
            return_exceptions=True,
        )

    assert sum(outcome is None for outcome in completion_results) == 1
    conflicts = [outcome for outcome in completion_results if isinstance(outcome, MediaGenerationConflictError)]
    assert len(conflicts) == 1
    assert str(conflicts[0]) == "The active web-video pointer changed before activation."

    async with postgres_session_factory() as session:
        persisted_file = await session.get(MemeFile, meme_file.id)
        persisted_previous = await session.get(MediaGeneration, previous_generation.id)
        persisted_candidates = (
            (
                await session.execute(
                    select(MediaGeneration).where(MediaGeneration.id.in_(tuple(row.id for row in candidates)))
                )
            )
            .scalars()
            .all()
        )

    assert persisted_file is not None
    assert persisted_previous is not None
    active = [row for row in persisted_candidates if row.status is MediaGenerationStatus.ACTIVE]
    stale = [row for row in persisted_candidates if row.status is MediaGenerationStatus.STALE]
    assert len(active) == 1
    assert len(stale) == 1
    assert persisted_file.active_media_generation_id == active[0].id
    assert persisted_file.s3_web_video_key == active[0].web_video_object_key
    assert persisted_file.s3_web_video_key != stale[0].web_video_object_key
    assert stale[0].cleanup_status is MediaGenerationCleanupStatus.PENDING
    assert stale[0].safe_failure_reason == "active_pointer_changed"
    assert persisted_previous.status is MediaGenerationStatus.SUPERSEDED


async def test_failed_generation_cleanup_preserves_current_media_and_deletes_only_new_objects(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file, stage, previous_generation = await _seed_ready_moving_file(migrated_db_session)
    previous_video_key = meme_file.s3_web_video_key
    generation = await MediaGenerationService(migrated_db_session, settings=Settings()).reserve(
        meme_file_id=meme_file.id,
        expected_web_video_object_key=previous_video_key,
        recovery_item_id=None,
        retry_limit=3,
    )
    storage = RecordingStorageClient()

    cleaned = await fail_and_cleanup_unactivated_generation(
        postgres_session_factory,
        storage_client=storage,
        bucket="memexpert",
        generation_id=generation.id,
        error=RuntimeError("provider details must stay sanitized"),
    )

    async with postgres_session_factory() as session:
        persisted_file = await session.get(MemeFile, meme_file.id)
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        persisted_generation = await session.get(MediaGeneration, generation.id)
        persisted_previous = await session.get(MediaGeneration, previous_generation.id)
    assert cleaned is True
    assert persisted_file is not None
    assert persisted_file.s3_web_video_key == previous_video_key
    assert persisted_file.active_media_generation_id == previous_generation.id
    assert persisted_file.status is ContentProcessingStatus.READY
    assert persisted_stage is not None
    assert persisted_stage.status is ContentPipelineStageStatus.SUCCEEDED
    assert persisted_previous is not None
    assert persisted_previous.status is MediaGenerationStatus.ACTIVE
    assert persisted_generation is not None
    assert persisted_generation.status is MediaGenerationStatus.FAILED
    assert persisted_generation.cleanup_status is MediaGenerationCleanupStatus.DELETED
    assert persisted_generation.safe_failure_reason == "RuntimeError"
    assert persisted_generation.safe_failure_text == "provider details must stay sanitized"
    assert {call["Key"] for call in storage.delete_calls} == {
        generation.preview_image_object_key,
        generation.web_video_object_key,
    }
    assert previous_video_key not in {call["Key"] for call in storage.delete_calls}

    storage.delete_calls.clear()
    cleaned_active = await fail_and_cleanup_unactivated_generation(
        postgres_session_factory,
        storage_client=storage,
        bucket="memexpert",
        generation_id=previous_generation.id,
        error=RuntimeError("must not delete active generation"),
    )
    async with postgres_session_factory() as session:
        persisted_previous = await session.get(MediaGeneration, previous_generation.id)
    assert cleaned_active is False
    assert storage.delete_calls == []
    assert persisted_previous is not None
    assert persisted_previous.status is MediaGenerationStatus.ACTIVE
    assert persisted_previous.cleanup_status is MediaGenerationCleanupStatus.RETAINED_REFERENCED


async def test_generation_gc_deletes_only_old_unreferenced_recognized_objects(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file, _stage, referenced = await _seed_ready_moving_file(migrated_db_session)
    old = utcnow() - timedelta(days=9)
    young = utcnow() - timedelta(days=1)
    referenced.status = MediaGenerationStatus.SUPERSEDED
    referenced.superseded_at = old
    referenced.cleanup_status = MediaGenerationCleanupStatus.PENDING
    referenced.created_at = old

    deletable_id = uuid.uuid7()
    deletable_video, deletable_preview = _generation_keys(meme_file.id, deletable_id)
    deletable = MediaGeneration(
        id=deletable_id,
        meme_file_id=meme_file.id,
        web_video_object_key=deletable_video,
        preview_image_object_key=deletable_preview,
        profile=WEB_VIDEO_PROFILE_ID,
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.SUPERSEDED,
        superseded_at=old,
        cleanup_status=MediaGenerationCleanupStatus.PENDING,
        created_at=old + timedelta(minutes=1),
        updated_at=old + timedelta(minutes=1),
    )
    unknown_id = uuid.uuid7()
    unknown = MediaGeneration(
        id=unknown_id,
        meme_file_id=meme_file.id,
        web_video_object_key=f"pipeline/derived/{meme_file.id}/generations/{unknown_id}/unrecognized.mp4",
        preview_image_object_key=f"pipeline/derived/{meme_file.id}/generations/{unknown_id}/preview.png",
        profile=WEB_VIDEO_PROFILE_ID,
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.SUPERSEDED,
        superseded_at=old,
        cleanup_status=MediaGenerationCleanupStatus.PENDING,
        created_at=old + timedelta(minutes=2),
        updated_at=old + timedelta(minutes=2),
    )
    swapped_id = uuid.uuid7()
    swapped_video, swapped_preview = _generation_keys(meme_file.id, swapped_id)
    swapped = MediaGeneration(
        id=swapped_id,
        meme_file_id=meme_file.id,
        web_video_object_key=swapped_preview,
        preview_image_object_key=swapped_video,
        profile=WEB_VIDEO_PROFILE_ID,
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.SUPERSEDED,
        superseded_at=old,
        cleanup_status=MediaGenerationCleanupStatus.PENDING,
        created_at=old + timedelta(minutes=3),
        updated_at=old + timedelta(minutes=3),
    )
    mismatched_file_id = uuid.uuid7()
    mismatched_id = uuid.uuid7()
    mismatched = MediaGeneration(
        id=mismatched_id,
        meme_file_id=None,
        web_video_object_key=build_web_video_generation_object_key(meme_file.id, mismatched_id),
        preview_image_object_key=build_preview_image_generation_object_key(mismatched_file_id, mismatched_id),
        profile=WEB_VIDEO_PROFILE_ID,
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.SUPERSEDED,
        superseded_at=old,
        cleanup_status=MediaGenerationCleanupStatus.PENDING,
        created_at=old + timedelta(minutes=4),
        updated_at=old + timedelta(minutes=4),
    )
    young_id = uuid.uuid7()
    young_video, young_preview = _generation_keys(meme_file.id, young_id)
    young_generation = MediaGeneration(
        id=young_id,
        meme_file_id=meme_file.id,
        web_video_object_key=young_video,
        preview_image_object_key=young_preview,
        profile=WEB_VIDEO_PROFILE_ID,
        retry_limit=3,
        attempt_count=1,
        status=MediaGenerationStatus.SUPERSEDED,
        superseded_at=young,
        cleanup_status=MediaGenerationCleanupStatus.PENDING,
        created_at=young,
        updated_at=young,
    )
    migrated_db_session.add_all((deletable, unknown, swapped, mismatched, young_generation))
    await migrated_db_session.commit()
    storage = RecordingStorageClient()

    result = await run_media_generation_gc_batch(
        postgres_session_factory,
        # GC keeps the seven-day floor even if validation was bypassed by an
        # unsafe embedding or an object restored from older configuration.
        settings=Settings.model_construct(media_generation_retention_seconds=86400.0),
        storage_client=storage,
        batch_size=10,
    )

    async with postgres_session_factory() as session:
        persisted_referenced = await session.get(MediaGeneration, referenced.id)
        persisted_deletable = await session.get(MediaGeneration, deletable.id)
        persisted_unknown = await session.get(MediaGeneration, unknown.id)
        persisted_swapped = await session.get(MediaGeneration, swapped.id)
        persisted_mismatched = await session.get(MediaGeneration, mismatched.id)
        persisted_young = await session.get(MediaGeneration, young_generation.id)
    assert result.examined == 5
    assert result.deleted == 1
    assert result.retained_referenced == 1
    assert result.unrecognized == 3
    assert result.failed == 0
    assert {call["Key"] for call in storage.delete_calls} == {deletable_video, deletable_preview}
    assert persisted_referenced is not None
    assert persisted_referenced.cleanup_status is MediaGenerationCleanupStatus.RETAINED_REFERENCED
    assert persisted_deletable is not None
    assert persisted_deletable.cleanup_status is MediaGenerationCleanupStatus.DELETED
    assert persisted_unknown is not None
    assert persisted_unknown.cleanup_status is MediaGenerationCleanupStatus.FAILED
    assert persisted_swapped is not None
    assert persisted_swapped.cleanup_status is MediaGenerationCleanupStatus.FAILED
    assert persisted_mismatched is not None
    assert persisted_mismatched.cleanup_status is MediaGenerationCleanupStatus.FAILED
    assert persisted_young is not None
    assert persisted_young.cleanup_status is MediaGenerationCleanupStatus.PENDING
    assert young_video not in {call["Key"] for call in storage.delete_calls}


def test_media_generation_table_indexes_remain_explicitly_named() -> None:
    """Keep migration and ORM index names stable for operational inspection."""

    assert all(isinstance(index, Index) for index in metadata.tables["media_generations"].indexes)
