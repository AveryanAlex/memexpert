# ruff: noqa: TC001,TC003
"""Dispatch publishing and downstream fan-out helpers for pipeline stages."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Self

from memexpert.core.broker import get_pipeline_broker_settings
from memexpert.core.config import Settings
from memexpert.messaging.rabbitmq_outbox import (
    RabbitBrokerProtocol,
    RabbitMessageSpec,
    RabbitPublisher,
    relay_rabbitmq_outbox_messages_best_effort,
)
from memexpert.models.content import PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentSourceKind,
)
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.events import PIPELINE_MEME_FILE_AGGREGATE_TYPE
from memexpert.pipeline.state import PipelineDatabaseService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.broker import PipelineBrokerSettings
    from memexpert.models.content import MemeFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineStageWorkContext:
    """Compact worker context returned after a stage is marked processing."""

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    stage: ContentPipelineStage
    mime_type: str | None
    original_object_key: str
    web_video_object_key: str | None
    recovery_item_id: uuid.UUID | None = None
    preserve_ready: bool = False
    retry_limit: int = 3


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
        broker: RabbitBrokerProtocol | None = None,
    ) -> None:
        super().__init__(session, settings=settings)
        self._broker_settings = get_pipeline_broker_settings(self._settings)
        self._broker = broker
        self._rabbit_publisher = RabbitPublisher(broker=broker, settings=self._settings)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        broker: RabbitBrokerProtocol | None = None,
    ) -> Self:
        """Build the dispatching service from shared runtime settings."""

        return cls(
            session,
            settings=settings,
            broker=broker,
        )

    async def _enqueue_dispatch_event(self, event: ContentPipelineDispatchEvent) -> uuid.UUID:
        return await self._rabbit_publisher.publish(
            build_pipeline_dispatch_message_spec(event, broker_settings=self._broker_settings),
            session=self._session,
        )

    async def _relay_outbox_messages_after_commit(self, message_ids: tuple[uuid.UUID, ...]) -> None:
        _ = await relay_rabbitmq_outbox_messages_best_effort(
            self._session,
            message_ids,
            settings=self._settings,
            broker=self._broker,
            logger=logger,
        )


def build_pipeline_dispatch_message_spec(
    event: ContentPipelineDispatchEvent,
    *,
    broker_settings: PipelineBrokerSettings,
) -> RabbitMessageSpec:
    """Build the generic RabbitMQ outbox spec for one pipeline dispatch event."""

    return RabbitMessageSpec(
        exchange=broker_settings.exchange,
        routing_key=resolve_routing_key_for_event(event, broker_settings=broker_settings),
        payload=event.model_dump(mode="json"),
        message_id=str(event.event_id),
        event_type=event.event_type.value,
        aggregate_type=PIPELINE_MEME_FILE_AGGREGATE_TYPE,
        aggregate_id=event.meme_file_id,
        ordering_key=str(event.meme_file_id),
        created_at=event.created_at,
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
    "DownstreamStageDispatch",
    "PipelineDispatchingService",
    "PipelineStageWorkContext",
    "build_pipeline_dispatch_message_spec",
    "prepare_downstream_dispatches",
    "resolve_routing_key_for_event",
]
