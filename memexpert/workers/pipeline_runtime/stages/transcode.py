# ruff: noqa: TC001,TC002,TC003
"""Transcode stage implementation for the pipeline worker runtime."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from pathlib import PurePosixPath

from memexpert.core.storage import (
    delete_object_if_present,
    download_object_bytes,
    get_pipeline_storage_settings,
    upload_object_bytes,
)
from memexpert.media.contracts import (
    SUPPORTED_MOVING_MEDIA_MIME_TYPES,
    MediaValidationError,
)
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services.media_generation import (
    MediaGenerationConflictError,
    MediaGenerationService,
    fail_and_cleanup_unactivated_generation,
    verify_uploaded_generation_object,
)
from memexpert.workers.pipeline_runtime.errors import ForcedTranscodeFailure
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext

_WEB_VIDEO_MIME_TYPE = "video/mp4"
_PREVIEW_IMAGE_MIME_TYPE = "image/png"

logger = logging.getLogger(__name__)


async def run_transcode_stage(
    context: PipelineStageHandlerContext,
    *,
    dispatch_event: ContentPipelineDispatchEvent,
    stage_context: PipelineStageWorkContext,
    attempt: int,
) -> None:
    """Normalize the original media asset and persist transcode completion state."""

    _maybe_force_transcode_failure(context, dispatch_event)
    if stage_context.mime_type is None:
        raise MediaValidationError("Pipeline item is missing the original media type required for transcode.")

    storage_settings = get_pipeline_storage_settings(context.settings)
    generation_id = None
    moving_media = stage_context.mime_type.lower() in SUPPORTED_MOVING_MEDIA_MIME_TYPES
    if moving_media and not _supports_generation_reservation(context.media_processor.normalize_for_web):
        raise MediaValidationError(
            "Moving-media normalization requires immutable generation reservation support."
        )
    if moving_media:
        async with context.session_factory() as session:
            generation = await MediaGenerationService(session, settings=context.settings).reserve(
                meme_file_id=dispatch_event.meme_file_id,
                expected_web_video_object_key=stage_context.web_video_object_key,
                recovery_item_id=stage_context.recovery_item_id,
                retry_limit=stage_context.retry_limit,
            )
            generation_id = generation.id

    try:
        original_bytes = await download_object_bytes(
            context.storage_client,
            bucket=storage_settings.bucket,
            key=stage_context.original_object_key,
        )
        if generation_id is not None:
            normalized = await context.media_processor.normalize_for_web(
                meme_file_id=dispatch_event.meme_file_id,
                filename=PurePosixPath(stage_context.original_object_key).name,
                content_type=stage_context.mime_type,
                media_bytes=original_bytes,
                generation_id=generation_id,
            )
        else:
            normalized = await context.media_processor.normalize_for_web(
                meme_file_id=dispatch_event.meme_file_id,
                filename=PurePosixPath(stage_context.original_object_key).name,
                content_type=stage_context.mime_type,
                media_bytes=original_bytes,
            )

        has_web_video_key = normalized.web_video_object_key is not None
        has_web_video_bytes = normalized.web_video_bytes is not None
        if has_web_video_key != has_web_video_bytes:
            raise MediaValidationError("Normalized media result has an incomplete web-video derivative.")

        has_preview_image_key = normalized.preview_image_object_key is not None
        has_preview_image_bytes = normalized.preview_image_bytes is not None
        if has_preview_image_key != has_preview_image_bytes:
            raise MediaValidationError("Normalized media result has an incomplete preview-image derivative.")
        if has_web_video_key != has_preview_image_key:
            raise MediaValidationError("Moving-media normalization must produce both playback and preview derivatives.")
        if moving_media and not has_web_video_key:
            raise MediaValidationError(
                "Moving-media normalization must produce immutable playback and preview derivatives."
            )
        if generation_id is not None:
            if normalized.generation_id != generation_id:
                raise MediaValidationError("Normalized media does not match the reserved generation.")
            async with context.session_factory() as session:
                await MediaGenerationService(session, settings=context.settings).record_verified(
                    generation_id,
                    normalized,
                )

        if normalized.preview_image_object_key is not None and normalized.preview_image_bytes is not None:
            await upload_object_bytes(
                context.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.preview_image_object_key,
                body=normalized.preview_image_bytes,
                content_type=_PREVIEW_IMAGE_MIME_TYPE,
            )

        if normalized.web_video_object_key is not None and normalized.web_video_bytes is not None:
            await upload_object_bytes(
                context.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.web_video_object_key,
                body=normalized.web_video_bytes,
                content_type=_WEB_VIDEO_MIME_TYPE,
            )

        if generation_id is not None:
            assert normalized.preview_image_object_key is not None
            assert normalized.preview_image_bytes is not None
            assert normalized.web_video_object_key is not None
            assert normalized.web_video_bytes is not None
            await verify_uploaded_generation_object(
                context.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.preview_image_object_key,
                expected_size=len(normalized.preview_image_bytes),
            )
            await verify_uploaded_generation_object(
                context.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.web_video_object_key,
                expected_size=len(normalized.web_video_bytes),
            )
            async with context.session_factory() as session:
                await MediaGenerationService(session, settings=context.settings).record_uploaded(generation_id)

        async with context.session_factory() as session:
            service = PipelineStageCompletionService(session, settings=context.settings, broker=context.broker)
            await service.complete_transcode_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                result=normalized,
            )
    except Exception as exc:
        if generation_id is not None:
            try:
                await fail_and_cleanup_unactivated_generation(
                    context.session_factory,
                    storage_client=context.storage_client,
                    bucket=storage_settings.bucket,
                    generation_id=generation_id,
                    error=exc,
                    stale=isinstance(exc, MediaGenerationConflictError),
                )
            except Exception:
                logger.exception(
                    "media_generation_failure_cleanup_failed",
                    extra={
                        "event": "media_generation_failure_cleanup_failed",
                        "generation_id": str(generation_id),
                        "meme_file_id": str(dispatch_event.meme_file_id),
                    },
                )
        else:
            for object_key in (
                getattr(locals().get("normalized"), "preview_image_object_key", None),
                getattr(locals().get("normalized"), "web_video_object_key", None),
            ):
                if object_key is not None:
                    await delete_object_if_present(
                        context.storage_client,
                        bucket=storage_settings.bucket,
                        key=object_key,
                    )
        raise


def _maybe_force_transcode_failure(
    context: PipelineStageHandlerContext,
    dispatch_event: ContentPipelineDispatchEvent,
) -> None:
    forced_target = context.settings.pipeline_worker_fail_transcode_for_meme_file_id
    if forced_target is None:
        return
    if forced_target == str(dispatch_event.meme_file_id):
        raise ForcedTranscodeFailure(
            "Forced transcode failure requested by pipeline_worker_fail_transcode_for_meme_file_id.",
        )


def _supports_generation_reservation(method: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except TypeError, ValueError:
        return False
    return any(
        parameter.name == "generation_id" or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )


__all__ = ["run_transcode_stage"]
