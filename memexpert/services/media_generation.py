# ruff: noqa: TC001,TC003
"""Durable reservation, activation support, and safe cleanup for media generations."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from memexpert.core.config import Settings, get_settings
from memexpert.core.storage import (
    build_preview_image_generation_object_key,
    build_web_video_generation_object_key,
    get_pipeline_storage_settings,
    get_s3_client,
    parse_media_generation_object_key,
)
from memexpert.media.contracts import WEB_VIDEO_PROFILE_ID, MediaFrameRate, NormalizedMediaResult
from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    MediaGenerationCleanupStatus,
    MediaGenerationStatus,
)
from memexpert.models.operations import MediaGeneration, RecoveryJobItem
from memexpert.services.safe_errors import sanitize_operational_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory


class MediaGenerationError(RuntimeError):
    """Base error for durable media-generation state transitions."""


class MediaGenerationConflictError(MediaGenerationError):
    """Raised when an expected active pointer or generation state is stale."""


@dataclass(frozen=True, slots=True)
class MediaGenerationGCResult:
    examined: int
    deleted: int
    retained_referenced: int
    unrecognized: int
    failed: int


class MediaGenerationService:
    """Persist generation state independently from expensive FFmpeg and S3 work."""

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()

    async def reserve(
        self,
        *,
        meme_file_id: uuid.UUID,
        expected_web_video_object_key: str | None,
        recovery_item_id: uuid.UUID | None,
        retry_limit: int,
    ) -> MediaGeneration:
        meme_file = await self._session.get(
            MemeFile,
            meme_file_id,
            with_for_update=True,
            populate_existing=True,
        )
        if meme_file is None:
            raise MediaGenerationConflictError(f"Meme file {meme_file_id} no longer exists.")
        if meme_file.s3_web_video_key != expected_web_video_object_key:
            raise MediaGenerationConflictError("The active web-video pointer changed before generation began.")
        stage_entry = await self._session.scalar(
            select(PipelineStageJournal)
            .where(
                PipelineStageJournal.meme_file_id == meme_file_id,
                PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if stage_entry is None:
            raise MediaGenerationConflictError("The transcode journal row no longer exists.")
        recovery_item = (
            await self._session.get(RecoveryJobItem, recovery_item_id, with_for_update=True)
            if recovery_item_id is not None
            else None
        )
        latest_generation = None
        next_attempt_count = 1
        if recovery_item is not None:
            if meme_file.active_media_generation_id is not None:
                active_generation = await self._session.get(
                    MediaGeneration,
                    meme_file.active_media_generation_id,
                    with_for_update=True,
                )
                if active_generation is not None and active_generation.recovery_item_id == recovery_item.id:
                    raise MediaGenerationConflictError("The recovery generation is already active.")
            latest_generation = await self._session.scalar(
                select(MediaGeneration)
                .where(
                    MediaGeneration.recovery_item_id == recovery_item.id,
                    MediaGeneration.status.in_(
                        (
                            MediaGenerationStatus.GENERATING,
                            MediaGenerationStatus.VERIFIED,
                            MediaGenerationStatus.UPLOADED,
                            MediaGenerationStatus.FAILED,
                            MediaGenerationStatus.STALE,
                        )
                    ),
                )
                .order_by(MediaGeneration.created_at.desc(), MediaGeneration.id.desc())
                .with_for_update()
                .limit(1)
            )
        else:
            latest_generation = await self._session.scalar(
                select(MediaGeneration)
                .where(
                    MediaGeneration.meme_file_id == meme_file.id,
                    MediaGeneration.recovery_item_id.is_(None),
                    MediaGeneration.status.in_(
                        (
                            MediaGenerationStatus.GENERATING,
                            MediaGenerationStatus.VERIFIED,
                            MediaGenerationStatus.UPLOADED,
                            MediaGenerationStatus.FAILED,
                            MediaGenerationStatus.STALE,
                        )
                    ),
                )
                .order_by(MediaGeneration.created_at.desc(), MediaGeneration.id.desc())
                .with_for_update()
                .limit(1)
            )
        if latest_generation is not None:
            if latest_generation.meme_file_id != meme_file.id:
                raise MediaGenerationConflictError("The prior generation no longer belongs to this file.")
            next_attempt_count = latest_generation.attempt_count + 1
            if latest_generation.status in {
                MediaGenerationStatus.GENERATING,
                MediaGenerationStatus.VERIFIED,
                MediaGenerationStatus.UPLOADED,
            }:
                latest_generation.status = MediaGenerationStatus.STALE
                latest_generation.cleanup_status = MediaGenerationCleanupStatus.PENDING
                latest_generation.safe_failure_reason = "superseded_by_retry"
                latest_generation.safe_failure_text = (
                    "A newer delivery reserved fresh immutable object keys for this generation attempt."
                )

        generation_id = uuid.uuid7()
        generation = MediaGeneration(
            id=generation_id,
            meme_file_id=meme_file.id,
            recovery_item_id=recovery_item.id if recovery_item is not None else None,
            expected_web_video_object_key=expected_web_video_object_key,
            web_video_object_key=build_web_video_generation_object_key(
                meme_file.id,
                generation_id,
                settings=self._settings,
            ),
            preview_image_object_key=build_preview_image_generation_object_key(
                meme_file.id,
                generation_id,
                settings=self._settings,
            ),
            profile=WEB_VIDEO_PROFILE_ID,
            retry_limit=retry_limit,
            attempt_count=next_attempt_count,
            previous_file_status=meme_file.status.value,
            previous_stage_status=stage_entry.status.value,
            previous_stage_observations=(
                dict(recovery_item.previous_stage_state)
                if recovery_item is not None
                else _stage_snapshot(stage_entry)
            ),
        )
        self._session.add(generation)
        await self._session.commit()
        return generation

    async def record_verified(self, generation_id: uuid.UUID, result: NormalizedMediaResult) -> None:
        generation = await self._session.get(MediaGeneration, generation_id, with_for_update=True)
        if generation is None:
            raise MediaGenerationConflictError(f"Media generation {generation_id} no longer exists.")
        if generation.status is not MediaGenerationStatus.GENERATING:
            raise MediaGenerationConflictError("Only the currently generating attempt may be verified.")
        if result.generation_id != generation.id:
            raise MediaGenerationConflictError("The verified output belongs to a different generation.")
        if (
            result.web_video_object_key != generation.web_video_object_key
            or result.preview_image_object_key != generation.preview_image_object_key
        ):
            raise MediaGenerationConflictError("Verified output keys do not match the reserved generation.")
        if result.web_video_profile != generation.profile or result.web_video_profile != WEB_VIDEO_PROFILE_ID:
            raise MediaGenerationConflictError("Verified output does not use the reserved immutable profile.")
        if result.source_observations is None or result.output_observations is None:
            raise MediaGenerationConflictError("Verified moving media is missing durable probe observations.")

        source_video = result.source_observations.primary_video
        output_video = result.output_observations.primary_video
        if source_video is None or output_video is None:
            raise MediaGenerationConflictError("Verified moving media is missing its primary video stream.")
        source_rate = source_video.average_frame_rate or source_video.real_frame_rate
        output_rate = output_video.average_frame_rate or output_video.real_frame_rate
        output_audio = result.output_observations.audio_streams[0] if result.output_observations.audio_streams else None

        generation.source_observations = asdict(result.source_observations)
        generation.output_observations = asdict(result.output_observations)
        generation.source_width = source_video.display_width
        generation.source_height = source_video.display_height
        _store_source_rate(generation, source_rate)
        generation.source_duration_seconds = (
            source_video.duration_seconds or result.source_observations.duration_seconds
        )
        generation.source_has_audio = result.source_has_audio
        generation.output_width = output_video.display_width
        generation.output_height = output_video.display_height
        _store_output_rate(generation, output_rate)
        generation.output_duration_seconds = (
            output_video.duration_seconds or result.output_observations.duration_seconds
        )
        generation.output_video_bitrate = output_video.bit_rate
        generation.output_byte_size = result.output_observations.byte_size
        generation.output_video_codec = output_video.codec_name
        generation.output_audio_codec = output_audio.codec_name if output_audio is not None else None
        generation.output_has_audio = result.web_video_has_audio
        generation.verified_at = result.web_video_verified_at or utcnow()
        generation.status = MediaGenerationStatus.VERIFIED
        await self._session.commit()

    async def record_uploaded(self, generation_id: uuid.UUID) -> None:
        generation = await self._session.get(MediaGeneration, generation_id, with_for_update=True)
        if generation is None:
            raise MediaGenerationConflictError(f"Media generation {generation_id} no longer exists.")
        if generation.status is not MediaGenerationStatus.VERIFIED:
            raise MediaGenerationConflictError("Only a locally verified generation may be marked uploaded.")
        generation.status = MediaGenerationStatus.UPLOADED
        generation.uploaded_at = utcnow()
        await self._session.commit()


async def verify_uploaded_generation_object(
    client: Any,
    *,
    bucket: str,
    key: str,
    expected_size: int,
) -> None:
    """Verify an immutable upload by length, using HEAD or a bounded body fallback."""

    if expected_size <= 0:
        raise MediaGenerationError("A generation object cannot be verified with an empty expected size.")
    try:
        head_object = getattr(client, "head_object", None)
        if callable(head_object):
            response = await asyncio.to_thread(head_object, Bucket=bucket, Key=key)
            observed_size = response.get("ContentLength") if isinstance(response, dict) else None
        else:
            response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
            body = response.get("Body") if isinstance(response, dict) else None
            if body is None or not hasattr(body, "read"):
                raise MediaGenerationError("Uploaded generation object did not return a readable body.")
            try:
                observed_size = len(await asyncio.to_thread(body.read))
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
    except MediaGenerationError:
        raise
    except Exception as exc:
        raise MediaGenerationError("Could not verify an uploaded generation object.") from exc
    if observed_size != expected_size:
        raise MediaGenerationError(
            f"An uploaded generation object has size {observed_size!r}; expected {expected_size}."
        )


async def fail_and_cleanup_unactivated_generation(
    session_factory: AsyncSessionFactory,
    *,
    storage_client: Any,
    bucket: str,
    generation_id: uuid.UUID,
    error: BaseException,
    stale: bool = False,
) -> bool:
    """Fence activation, then delete only a confirmed-unreferenced failed generation."""

    async with session_factory() as session:
        generation = await session.get(MediaGeneration, generation_id, with_for_update=True)
        if generation is None:
            return False
        referenced = await _generation_is_referenced(session, generation)
        if referenced:
            generation.cleanup_status = MediaGenerationCleanupStatus.RETAINED_REFERENCED
            generation.cleanup_at = utcnow()
            await session.commit()
            return False
        generation.status = MediaGenerationStatus.STALE if stale else MediaGenerationStatus.FAILED
        generation.safe_failure_reason = type(error).__name__[:128]
        generation.safe_failure_text = sanitize_operational_error(error) or type(error).__name__
        generation.cleanup_status = MediaGenerationCleanupStatus.PENDING
        generation.cleanup_attempt_count += 1
        keys = (generation.preview_image_object_key, generation.web_video_object_key)
        if not _recognized_generation_keys(generation, keys):
            generation.cleanup_status = MediaGenerationCleanupStatus.FAILED
            generation.cleanup_error_text = "Generation object keys are not recognized; automatic deletion was refused."
            await session.commit()
            return False
        await session.commit()

    try:
        for key in keys:
            await asyncio.to_thread(storage_client.delete_object, Bucket=bucket, Key=key)
    except Exception as exc:
        await _record_cleanup_failure(session_factory, generation_id, exc)
        return False

    async with session_factory() as session:
        generation = await session.get(MediaGeneration, generation_id, with_for_update=True)
        if generation is None:
            return False
        if await _generation_is_referenced(session, generation):
            generation.cleanup_status = MediaGenerationCleanupStatus.RETAINED_REFERENCED
            generation.cleanup_error_text = "A reference appeared while cleanup was in progress."
            generation.cleanup_at = utcnow()
            await session.commit()
            return False
        generation.cleanup_status = MediaGenerationCleanupStatus.DELETED
        generation.cleanup_error_text = None
        generation.cleanup_at = utcnow()
        await session.commit()
    return True


async def run_media_generation_gc_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    storage_client: Any | None = None,
    batch_size: int = 100,
) -> MediaGenerationGCResult:
    """Delete only recognized, old, unreferenced immutable generations."""

    resolved_settings = settings or get_settings()
    retention_seconds = max(
        float(getattr(resolved_settings, "media_generation_retention_seconds", 7 * 86400)),
        7 * 86400.0,
    )
    cutoff = utcnow() - timedelta(seconds=retention_seconds)
    client = storage_client or get_s3_client()
    storage = get_pipeline_storage_settings(resolved_settings)
    examined = deleted = retained = unrecognized = failed = 0
    examined_generation_ids: set[uuid.UUID] = set()

    for _ in range(max(1, min(batch_size, 1000))):
        async with session_factory() as session:
            generation = await session.scalar(
                select(MediaGeneration)
                .where(
                    MediaGeneration.status.in_(
                        (
                            MediaGenerationStatus.SUPERSEDED,
                            MediaGenerationStatus.FAILED,
                            MediaGenerationStatus.STALE,
                        )
                    ),
                    MediaGeneration.cleanup_status.not_in(
                        (
                            MediaGenerationCleanupStatus.DELETED,
                            MediaGenerationCleanupStatus.RETAINED_REFERENCED,
                        )
                    ),
                    MediaGeneration.id.not_in(examined_generation_ids),
                    func.coalesce(
                        MediaGeneration.superseded_at,
                        MediaGeneration.verified_at,
                        MediaGeneration.created_at,
                    )
                    < cutoff,
                )
                .order_by(MediaGeneration.created_at.asc(), MediaGeneration.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if generation is None:
                break
            examined_generation_ids.add(generation.id)
            examined += 1
            if await _generation_is_referenced(session, generation):
                generation.cleanup_status = MediaGenerationCleanupStatus.RETAINED_REFERENCED
                generation.cleanup_at = utcnow()
                await session.commit()
                retained += 1
                continue
            keys = (generation.preview_image_object_key, generation.web_video_object_key)
            if not _recognized_generation_keys(generation, keys):
                generation.cleanup_status = MediaGenerationCleanupStatus.FAILED
                generation.cleanup_error_text = "Unrecognized generation key; garbage collection refused deletion."
                generation.cleanup_at = utcnow()
                await session.commit()
                unrecognized += 1
                continue
            generation.cleanup_status = MediaGenerationCleanupStatus.PENDING
            generation.cleanup_attempt_count += 1
            generation_id = generation.id
            await session.commit()

        try:
            for key in keys:
                await asyncio.to_thread(client.delete_object, Bucket=storage.bucket, Key=key)
        except Exception as exc:
            await _record_cleanup_failure(session_factory, generation_id, exc)
            failed += 1
            continue
        async with session_factory() as session:
            generation = await session.get(MediaGeneration, generation_id, with_for_update=True)
            if generation is None:
                continue
            if await _generation_is_referenced(session, generation):
                generation.cleanup_status = MediaGenerationCleanupStatus.RETAINED_REFERENCED
                retained += 1
            else:
                generation.cleanup_status = MediaGenerationCleanupStatus.DELETED
                generation.cleanup_error_text = None
                deleted += 1
            generation.cleanup_at = utcnow()
            await session.commit()

    return MediaGenerationGCResult(
        examined=examined,
        deleted=deleted,
        retained_referenced=retained,
        unrecognized=unrecognized,
        failed=failed,
    )


async def _generation_is_referenced(session: AsyncSession, generation: MediaGeneration) -> bool:
    reference = await session.scalar(
        select(MemeFile.id)
        .where(
            or_(
                MemeFile.active_media_generation_id == generation.id,
                MemeFile.s3_web_video_key == generation.web_video_object_key,
            )
        )
        .limit(1)
    )
    return reference is not None


def _recognized_generation_keys(generation: MediaGeneration, keys: tuple[str, str]) -> bool:
    preview, video = (parse_media_generation_object_key(key) for key in keys)
    if preview is None or video is None:
        return False
    if preview.artifact_name != "preview.png" or video.artifact_name != "web.mp4":
        return False
    if preview.generation_id != generation.id or video.generation_id != generation.id:
        return False
    if preview.meme_file_id != video.meme_file_id:
        return False
    return generation.meme_file_id is None or (
        preview.meme_file_id == generation.meme_file_id
        and video.meme_file_id == generation.meme_file_id
    )


async def _record_cleanup_failure(
    session_factory: AsyncSessionFactory,
    generation_id: uuid.UUID,
    error: BaseException,
) -> None:
    async with session_factory() as session:
        generation = await session.get(MediaGeneration, generation_id, with_for_update=True)
        if generation is None:
            return
        generation.cleanup_status = MediaGenerationCleanupStatus.FAILED
        generation.cleanup_error_text = sanitize_operational_error(error) or type(error).__name__
        generation.cleanup_at = utcnow()
        await session.commit()


def _stage_snapshot(entry: PipelineStageJournal) -> dict[str, object]:
    return {
        "status": entry.status.value,
        "attempt_count": entry.attempt_count,
        "last_event_id": str(entry.last_event_id) if entry.last_event_id is not None else None,
        "normalized_reason": entry.normalized_reason,
        "last_error_text": entry.last_error_text,
        "is_retryable": entry.is_retryable,
        "retry_after": entry.retry_after.isoformat() if entry.retry_after is not None else None,
        "started_at": entry.started_at.isoformat() if entry.started_at is not None else None,
        "finished_at": entry.finished_at.isoformat() if entry.finished_at is not None else None,
    }


def _store_source_rate(generation: MediaGeneration, frame_rate: MediaFrameRate | None) -> None:
    generation.source_frame_rate_numerator = frame_rate.numerator if frame_rate is not None else None
    generation.source_frame_rate_denominator = frame_rate.denominator if frame_rate is not None else None


def _store_output_rate(generation: MediaGeneration, frame_rate: MediaFrameRate | None) -> None:
    generation.output_frame_rate_numerator = frame_rate.numerator if frame_rate is not None else None
    generation.output_frame_rate_denominator = frame_rate.denominator if frame_rate is not None else None


__all__ = [
    "MediaGenerationConflictError",
    "MediaGenerationError",
    "MediaGenerationGCResult",
    "MediaGenerationService",
    "fail_and_cleanup_unactivated_generation",
    "run_media_generation_gc_batch",
    "verify_uploaded_generation_object",
]
