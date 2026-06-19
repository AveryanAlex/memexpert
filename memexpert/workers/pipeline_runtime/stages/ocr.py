# ruff: noqa: TC001,TC002,TC003
"""OCR stage implementation for the pipeline worker runtime."""

from __future__ import annotations

from pathlib import PurePosixPath

from memexpert.core.ocr import OCRProcessingError
from memexpert.core.storage import download_object_bytes, get_pipeline_storage_settings
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext


async def run_ocr_stage(
    context: PipelineStageHandlerContext,
    *,
    dispatch_event: ContentPipelineDispatchEvent,
    stage_context: PipelineStageWorkContext,
    attempt: int,
) -> None:
    """Extract OCR text from the normalized media when available, otherwise the original asset."""

    source_object_key = stage_context.web_video_object_key or stage_context.original_object_key
    source_mime_type = stage_context.mime_type
    if source_mime_type is None:
        raise OCRProcessingError("Pipeline item is missing the media type required for OCR.")

    storage_settings = get_pipeline_storage_settings(context.settings)
    source_bytes = await download_object_bytes(
        context.storage_client,
        bucket=storage_settings.bucket,
        key=source_object_key,
    )
    ocr_result = await context.ocr_processor.extract_text(
        filename=PurePosixPath(source_object_key).name,
        mime_type=source_mime_type,
        media_bytes=source_bytes,
        source_object_key=source_object_key,
    )
    async with context.session_factory() as session:
        service = PipelineStageCompletionService(session, settings=context.settings, broker=context.broker)
        await service.complete_ocr_stage(
            meme_file_id=dispatch_event.meme_file_id,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            result=ocr_result,
        )


__all__ = ["run_ocr_stage"]
