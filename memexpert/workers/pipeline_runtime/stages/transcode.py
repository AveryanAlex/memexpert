# ruff: noqa: TC001,TC002,TC003
"""Transcode stage implementation for the pipeline worker runtime."""

from __future__ import annotations

from pathlib import PurePosixPath

from memexpert.core.storage import (
    delete_object_if_present,
    download_object_bytes,
    get_pipeline_storage_settings,
    upload_object_bytes,
)
from memexpert.media.contracts import MediaValidationError
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.workers.pipeline_runtime.errors import ForcedTranscodeFailure
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext

_WEB_VIDEO_MIME_TYPE = "video/mp4"
_PREVIEW_IMAGE_MIME_TYPE = "image/png"


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
    original_bytes = await download_object_bytes(
        context.storage_client,
        bucket=storage_settings.bucket,
        key=stage_context.original_object_key,
    )
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

    uploaded_object_keys: list[str] = []
    try:
        if normalized.preview_image_object_key is not None and normalized.preview_image_bytes is not None:
            await upload_object_bytes(
                context.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.preview_image_object_key,
                body=normalized.preview_image_bytes,
                content_type=_PREVIEW_IMAGE_MIME_TYPE,
            )
            uploaded_object_keys.append(normalized.preview_image_object_key)

        if normalized.web_video_object_key is not None and normalized.web_video_bytes is not None:
            await upload_object_bytes(
                context.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.web_video_object_key,
                body=normalized.web_video_bytes,
                content_type=_WEB_VIDEO_MIME_TYPE,
            )
            uploaded_object_keys.append(normalized.web_video_object_key)

        async with context.session_factory() as session:
            service = PipelineStageCompletionService(session, settings=context.settings, broker=context.broker)
            await service.complete_transcode_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                result=normalized,
            )
    except Exception:
        for object_key in uploaded_object_keys:
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


__all__ = ["run_transcode_stage"]
