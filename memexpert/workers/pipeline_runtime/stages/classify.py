# ruff: noqa: TC001,TC002,TC003
"""Classify stage implementation for the pipeline worker runtime."""

from __future__ import annotations

from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.workers.pipeline_runtime.errors import ForcedClassifyFailure
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext, load_preview_frame


async def run_classify_stage(
    context: PipelineStageHandlerContext,
    *,
    dispatch_event: ContentPipelineDispatchEvent,
    stage_context: PipelineStageWorkContext,
    attempt: int,
) -> None:
    """Classify a preview frame and persist canonical NSFW truth."""

    _maybe_force_classify_failure(context, dispatch_event)
    preview_frame_bytes = await load_preview_frame(context, stage_context)
    classification_result = await context.classification_client.classify_image(
        image_bytes=preview_frame_bytes,
        mime_type="image/png",
    )
    async with context.session_factory() as session:
        service = PipelineStageCompletionService(session, settings=context.settings)
        await service.complete_classify_stage(
            meme_file_id=dispatch_event.meme_file_id,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            classification_result=classification_result,
        )


def _maybe_force_classify_failure(
    context: PipelineStageHandlerContext,
    dispatch_event: ContentPipelineDispatchEvent,
) -> None:
    forced_target = context.settings.pipeline_worker_fail_classify_for_meme_file_id
    if forced_target is None:
        return
    if forced_target == str(dispatch_event.meme_file_id):
        raise ForcedClassifyFailure(
            "Forced classify failure requested by pipeline_worker_fail_classify_for_meme_file_id.",
        )


__all__ = ["run_classify_stage"]
