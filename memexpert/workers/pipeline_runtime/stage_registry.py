"""Explicit registry for post-materialization pipeline stage handlers."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from memexpert.models.enums import ContentPipelineStage

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from memexpert.pipeline.dispatch import PipelineStageWorkContext
    from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent


class StageImplementation(Protocol):
    def __call__(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> Awaitable[None]: ...


class StageFailureHook(Protocol):
    def __call__(self, dispatch_event: ContentPipelineDispatchEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class PipelineStageHandler:
    """Runtime method names needed to execute one downstream stage."""

    implementation_method_name: str
    failure_hook_method_name: str | None = None

    async def run(
        self,
        runtime: object,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        if self.failure_hook_method_name is not None:
            failure_hook = cast("StageFailureHook", getattr(runtime, self.failure_hook_method_name))
            failure_hook(dispatch_event)

        implementation = cast("StageImplementation", getattr(runtime, self.implementation_method_name))
        await implementation(
            dispatch_event=dispatch_event,
            stage_context=stage_context,
            attempt=attempt,
        )


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
        ContentPipelineStage.TRANSCODE: PipelineStageHandler(
            implementation_method_name="_run_transcode_stage",
            failure_hook_method_name="_maybe_force_transcode_failure",
        ),
        ContentPipelineStage.OCR: PipelineStageHandler(
            implementation_method_name="_run_ocr_stage",
        ),
        ContentPipelineStage.EMBED: PipelineStageHandler(
            implementation_method_name="_run_embed_stage",
            failure_hook_method_name="_maybe_force_embed_failure",
        ),
        ContentPipelineStage.CLASSIFY: PipelineStageHandler(
            implementation_method_name="_run_classify_stage",
            failure_hook_method_name="_maybe_force_classify_failure",
        ),
        ContentPipelineStage.SYNC_QDRANT: PipelineStageHandler(
            implementation_method_name="_run_sync_qdrant_stage",
            failure_hook_method_name="_maybe_force_sync_qdrant_failure",
        ),
        ContentPipelineStage.SYNC_MEILI: PipelineStageHandler(
            implementation_method_name="_run_sync_meili_stage",
            failure_hook_method_name="_maybe_force_sync_meili_failure",
        ),
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
