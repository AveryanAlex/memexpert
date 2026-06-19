# ruff: noqa: TC001
"""Media-inspect materialization stage implementation."""

from __future__ import annotations

from memexpert.ingest.materializer import PipelineIngestMaterializer
from memexpert.pipeline.events import MediaInspectRequestedEvent
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext


async def run_media_inspect_stage(
    context: PipelineStageHandlerContext,
    *,
    inspect_event: MediaInspectRequestedEvent,
) -> None:
    """Materialize one raw-ingest request into durable content-pipeline state."""

    async with context.session_factory() as session:
        materializer = PipelineIngestMaterializer(
            session,
            settings=context.settings,
            storage_client=context.storage_client,
            media_processor=context.media_processor,
        )
        _ = await materializer.materialize(inspect_event.ingest_request_id)


__all__ = ["run_media_inspect_stage"]
