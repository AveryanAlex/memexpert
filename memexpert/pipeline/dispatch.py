# ruff: noqa: TC001,TC003
"""Dispatch publishing and downstream fan-out helpers for pipeline stages."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Self

from memexpert.core.broker import ensure_pipeline_broker_started, get_pipeline_broker_settings
from memexpert.core.config import Settings
from memexpert.models.content import PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentSourceKind,
)
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.state import PipelineDatabaseService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.broker import PipelineBrokerSettings
    from memexpert.models.content import MemeFile


DispatchEventPublisher = Callable[[ContentPipelineDispatchEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PipelineStageWorkContext:
    """Compact worker context returned after a stage is marked processing."""

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    stage: ContentPipelineStage
    mime_type: str | None
    original_object_key: str
    web_video_object_key: str | None


@dataclass(frozen=True, slots=True)
class DownstreamStageDispatch:
    """A durable next-stage dispatch created after one stage succeeds."""

    event: ContentPipelineDispatchEvent
    stage_entry: PipelineStageJournal


class PipelineDispatchingService(PipelineDatabaseService):
    """Base for focused services that publish pipeline dispatch events."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        publisher: DispatchEventPublisher | None = None,
    ) -> None:
        super().__init__(session, settings=settings)
        self._broker_settings = get_pipeline_broker_settings(self._settings)
        self._publisher = publisher or self._publish_dispatch_event

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        publisher: DispatchEventPublisher | None = None,
    ) -> Self:
        """Build the dispatching service from shared runtime settings."""

        return cls(
            session,
            settings=settings,
            publisher=publisher,
        )

    async def _publish_dispatch_event(self, event: ContentPipelineDispatchEvent) -> None:
        broker = await ensure_pipeline_broker_started(settings=self._settings)
        payload = event.model_dump(mode="json")
        _ = await broker.publish(
            payload,
            exchange=self._broker_settings.exchange,
            routing_key=resolve_routing_key_for_event(event, broker_settings=self._broker_settings),
            persist=True,
            content_type="application/json",
            message_id=str(event.event_id),
            timestamp=event.created_at,
            mandatory=True,
        )


def resolve_routing_key_for_event(
    event: ContentPipelineDispatchEvent,
    *,
    broker_settings: PipelineBrokerSettings,
) -> str:
    """Select the routing key for a work dispatch or sync-status event."""

    status_event_types = {
        ContentPipelineEventType.MEME_QDRANT_SYNCED,
        ContentPipelineEventType.MEME_MEILI_SYNCED,
    }
    if event.event_type in status_event_types:
        return broker_settings.routing_key_for_event(event.event_type)
    return broker_settings.routing_key_for_stage(event.stage)


def prepare_downstream_dispatches(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    stage: ContentPipelineStage,
    created_at: datetime,
) -> tuple[DownstreamStageDispatch, ...]:
    if stage is ContentPipelineStage.CLASSIFY:
        return _prepare_classify_fan_out_dispatches(
            session,
            meme_file=meme_file,
            created_at=created_at,
        )

    next_stage = _consts.NEXT_STAGE_BY_STAGE.get(stage)
    if next_stage is None:
        return ()

    existing_stage_entry = next(
        (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is next_stage),
        None,
    )
    if existing_stage_entry is not None:
        return ()

    dispatch = _build_downstream_dispatch(
        session,
        meme_file=meme_file,
        predecessor_stage=stage,
        next_stage=next_stage,
        created_at=created_at,
    )
    return (dispatch,)


def _prepare_classify_fan_out_dispatches(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    created_at: datetime,
) -> tuple[DownstreamStageDispatch, ...]:
    """Build both sync-target dispatches classify success must hand off to."""

    dispatches: list[DownstreamStageDispatch] = []
    for next_stage in _consts.CLASSIFY_FAN_OUT_STAGES:
        existing_stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is next_stage),
            None,
        )
        if existing_stage_entry is not None:
            return ()
        dispatches.append(
            _build_downstream_dispatch(
                session,
                meme_file=meme_file,
                predecessor_stage=ContentPipelineStage.CLASSIFY,
                next_stage=next_stage,
                created_at=created_at,
            )
        )
    return tuple(dispatches)


def _build_downstream_dispatch(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    predecessor_stage: ContentPipelineStage,
    next_stage: ContentPipelineStage,
    created_at: datetime,
) -> DownstreamStageDispatch:
    dispatch_event = ContentPipelineDispatchEvent(
        event_id=uuid.uuid7(),
        event_type=_consts.DOWNSTREAM_STAGE_EVENT_TYPES[predecessor_stage],
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        stage=next_stage,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=meme_file.s3_original_key,
        attempt=1,
        created_at=created_at,
    )
    stage_entry = PipelineStageJournal(
        meme_file_id=meme_file.id,
        stage=next_stage,
        status=ContentPipelineStageStatus.PENDING,
        attempt_count=0,
        last_event_id=dispatch_event.event_id,
        is_retryable=True,
    )
    session.add(stage_entry)
    meme_file.pipeline_stage_journal_entries.append(stage_entry)
    return DownstreamStageDispatch(event=dispatch_event, stage_entry=stage_entry)


__all__ = [
    "DispatchEventPublisher",
    "DownstreamStageDispatch",
    "PipelineDispatchingService",
    "PipelineStageWorkContext",
    "prepare_downstream_dispatches",
    "resolve_routing_key_for_event",
]
