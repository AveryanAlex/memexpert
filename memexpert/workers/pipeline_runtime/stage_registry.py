"""Explicit registry for post-materialization pipeline stage handlers."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from memexpert.models.enums import ContentPipelineStage
from memexpert.workers.pipeline_runtime.stages.classify import run_classify_stage
from memexpert.workers.pipeline_runtime.stages.embed import run_embed_stage
from memexpert.workers.pipeline_runtime.stages.ocr import run_ocr_stage
from memexpert.workers.pipeline_runtime.stages.sync_meili import run_sync_meili_stage
from memexpert.workers.pipeline_runtime.stages.sync_qdrant import run_sync_qdrant_stage
from memexpert.workers.pipeline_runtime.stages.transcode import run_transcode_stage

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from memexpert.pipeline.dispatch import PipelineStageWorkContext
    from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
    from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext


class PipelineStageHandler(Protocol):
    def __call__(
        self,
        context: PipelineStageHandlerContext,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> Awaitable[None]: ...


RUNNABLE_DOWNSTREAM_STAGES: tuple[ContentPipelineStage, ...] = (
    ContentPipelineStage.TRANSCODE,
    ContentPipelineStage.OCR,
    ContentPipelineStage.EMBED,
    ContentPipelineStage.CLASSIFY,
    ContentPipelineStage.SYNC_QDRANT,
    ContentPipelineStage.SYNC_MEILI,
)

PIPELINE_STAGE_HANDLERS = MappingProxyType(
    {
        ContentPipelineStage.TRANSCODE: run_transcode_stage,
        ContentPipelineStage.OCR: run_ocr_stage,
        ContentPipelineStage.EMBED: run_embed_stage,
        ContentPipelineStage.CLASSIFY: run_classify_stage,
        ContentPipelineStage.SYNC_QDRANT: run_sync_qdrant_stage,
        ContentPipelineStage.SYNC_MEILI: run_sync_meili_stage,
    }
)


def get_stage_handler(stage: ContentPipelineStage) -> PipelineStageHandler | None:
    """Return the registered runtime handler for a downstream stage, if any."""

    return PIPELINE_STAGE_HANDLERS.get(stage)


__all__ = [
    "PIPELINE_STAGE_HANDLERS",
    "RUNNABLE_DOWNSTREAM_STAGES",
    "PipelineStageHandler",
    "get_stage_handler",
]
