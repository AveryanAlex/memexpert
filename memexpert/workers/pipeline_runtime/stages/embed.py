# ruff: noqa: TC001,TC002,TC003
"""Embed stage implementation for the pipeline worker runtime."""

from __future__ import annotations

from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.workers.pipeline_runtime.errors import ForcedEmbedFailure
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext, load_preview_frame


async def run_embed_stage(
    context: PipelineStageHandlerContext,
    *,
    dispatch_event: ContentPipelineDispatchEvent,
    stage_context: PipelineStageWorkContext,
    attempt: int,
) -> None:
    """Embed a preview frame, search for duplicates, and persist merge-aware completion state."""

    _maybe_force_embed_failure(context, dispatch_event)
    preview_frame_bytes = await load_preview_frame(context, stage_context)
    embedding_result = await context.voyage_client.embed_image(
        image_bytes=preview_frame_bytes,
        mime_type="image/png",
    )
    similarity_matches = await context.qdrant_client.find_similar_memes(
        vector=embedding_result.vector,
        current_meme_file_id=dispatch_event.meme_file_id,
    )
    async with context.session_factory() as session:
        service = PipelineStageCompletionService(session, settings=context.settings, broker=context.broker)
        _ = await service.complete_embed_stage(
            meme_file_id=dispatch_event.meme_file_id,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            embedding_result=embedding_result,
            similarity_matches=similarity_matches,
        )


def _maybe_force_embed_failure(
    context: PipelineStageHandlerContext,
    dispatch_event: ContentPipelineDispatchEvent,
) -> None:
    forced_target = context.settings.pipeline_worker_fail_embed_for_meme_file_id
    if forced_target is None:
        return
    if forced_target == str(dispatch_event.meme_file_id):
        raise ForcedEmbedFailure(
            "Forced embed failure requested by pipeline_worker_fail_embed_for_meme_file_id.",
        )


__all__ = ["run_embed_stage"]
