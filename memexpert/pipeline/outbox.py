# ruff: noqa: TC001,TC003
"""Transactional outbox row builders and publisher helpers for pipeline events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.config import Settings
from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile, PipelineIngestRequest, PipelineOutboxEvent
from memexpert.models.enums import PipelineOutboxEventStatus
from memexpert.pipeline.events import (
    MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
    PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
    PIPELINE_MEME_FILE_AGGREGATE_TYPE,
    build_media_inspect_requested_payload,
    build_media_inspect_routing_key,
    build_meme_created_transcode_dispatch_event,
    build_stage_routing_key,
)
from memexpert.pipeline.helpers import trim_error_text
from memexpert.schemas.content_pipeline import ContentPipelineEventType
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class OutboxBrokerProtocol(Protocol):
    """Minimal async broker surface used by the transactional-outbox publisher."""

    async def publish(self, payload: object, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class OutboxPublishBatchResult:
    """Counts returned after one outbox publish sweep."""

    claimed: int
    published: int
    failed: int


def build_media_inspect_outbox_event(
    ingest_request: PipelineIngestRequest,
    *,
    settings: Settings,
) -> PipelineOutboxEvent:
    """Return a pending outbox row for future raw media inspection."""

    if ingest_request.sha256_hex is None:
        raise ValueError("media-inspect outbox payload requires ingest_request.sha256_hex.")

    now = utcnow()
    event_id = uuid.uuid7()
    return PipelineOutboxEvent(
        id=event_id,
        aggregate_type=PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
        aggregate_id=ingest_request.id,
        event_type=MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
        routing_key=build_media_inspect_routing_key(settings),
        payload=build_media_inspect_requested_payload(
            event_id=event_id,
            ingest_request_id=ingest_request.id,
            source_platform=ingest_request.source_platform,
            sha256_hex=ingest_request.sha256_hex,
            created_at=now,
        ),
        status=PipelineOutboxEventStatus.PENDING,
        attempt_count=0,
        next_retry_at=now,
    )


def build_meme_created_transcode_outbox_event(
    meme_file: MemeFile,
    *,
    event_id: uuid.UUID,
    created_at: datetime,
    settings: Settings,
) -> PipelineOutboxEvent:
    """Return a pending outbox row for the first materialized transcode dispatch."""

    dispatch_event = build_meme_created_transcode_dispatch_event(
        event_id=event_id,
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        original_object_key=meme_file.s3_original_key,
        created_at=created_at,
    )
    return PipelineOutboxEvent(
        id=event_id,
        aggregate_type=PIPELINE_MEME_FILE_AGGREGATE_TYPE,
        aggregate_id=meme_file.id,
        event_type=ContentPipelineEventType.MEME_CREATED.value,
        routing_key=build_stage_routing_key(settings, dispatch_event.stage),
        payload=dispatch_event.model_dump(mode="json"),
        status=PipelineOutboxEventStatus.PENDING,
        attempt_count=0,
        next_retry_at=created_at,
    )


class PipelineOutboxPublisher:
    """Small transactional-outbox publisher with retry-aware claiming."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        broker: OutboxBrokerProtocol,
        settings: Settings,
    ) -> None:
        self._session = session
        self._broker = broker
        self._settings = settings

    async def claim_pending(self, *, limit: int = 100) -> tuple[PipelineOutboxEvent, ...]:
        """Claim due pending/failed rows for this publisher process."""

        now = utcnow()
        safe_limit = max(1, limit)
        try:
            result = await self._session.execute(
                select(PipelineOutboxEvent)
                .where(
                    PipelineOutboxEvent.status.in_(
                        (PipelineOutboxEventStatus.PENDING, PipelineOutboxEventStatus.FAILED)
                    ),
                    or_(
                        PipelineOutboxEvent.next_retry_at.is_(None),
                        PipelineOutboxEvent.next_retry_at <= now,
                    ),
                )
                .order_by(PipelineOutboxEvent.created_at.asc(), PipelineOutboxEvent.id.asc())
                .with_for_update(skip_locked=True)
                .limit(safe_limit)
            )
            events = tuple(result.scalars().all())
            for event in events:
                event.status = PipelineOutboxEventStatus.PUBLISHING
                event.attempt_count += 1
                event.next_retry_at = None
                event.last_error_text = None
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to claim pipeline outbox events for publishing.") from exc
        return events

    async def publish_batch(self, *, limit: int = 100) -> OutboxPublishBatchResult:
        """Claim, publish, and mark one batch of due outbox events."""

        events = await self.claim_pending(limit=limit)
        published = 0
        failed = 0
        for event in events:
            try:
                await self._publish_event(event)
            except Exception as exc:  # noqa: BLE001 - every broker failure becomes retry metadata.
                await self._mark_failed(event, error=exc)
                failed += 1
                continue
            await self._mark_published(event)
            published += 1
        return OutboxPublishBatchResult(claimed=len(events), published=published, failed=failed)

    async def recover_stale_publishing(self, *, stale_before: datetime) -> int:
        """Move stranded publishing rows back into retryable failed state."""

        now = utcnow()
        try:
            result = await self._session.execute(
                select(PipelineOutboxEvent)
                .where(
                    PipelineOutboxEvent.status == PipelineOutboxEventStatus.PUBLISHING,
                    PipelineOutboxEvent.updated_at < stale_before,
                )
                .with_for_update(skip_locked=True)
            )
            events = tuple(result.scalars().all())
            for event in events:
                event.status = PipelineOutboxEventStatus.FAILED
                event.next_retry_at = now
                event.last_error_text = "Outbox event was recovered from a stale publishing claim."
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to recover stale pipeline outbox publishing claims.") from exc
        return len(events)

    async def _publish_event(self, event: PipelineOutboxEvent) -> None:
        _ = await self._broker.publish(
            event.payload,
            exchange=self._settings.pipeline_broker_exchange,
            routing_key=event.routing_key,
            persist=True,
            content_type="application/json",
            message_id=str(event.id),
            timestamp=event.created_at,
            mandatory=True,
        )

    async def _mark_published(self, event: PipelineOutboxEvent) -> None:
        event.status = PipelineOutboxEventStatus.PUBLISHED
        event.published_at = utcnow()
        event.next_retry_at = None
        event.last_error_text = None
        await self._commit_event_update("Failed to mark pipeline outbox event as published.")

    async def _mark_failed(self, event: PipelineOutboxEvent, *, error: Exception) -> None:
        event.status = PipelineOutboxEventStatus.FAILED
        event.published_at = None
        event.next_retry_at = utcnow() + timedelta(seconds=self._settings.pipeline_broker_retry_backoff_seconds)
        event.last_error_text = trim_error_text(str(error) or error.__class__.__name__)
        await self._commit_event_update("Failed to mark pipeline outbox event publish failure.")

    async def _commit_event_update(self, failure_message: str) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(failure_message) from exc


__all__ = [
    "OutboxBrokerProtocol",
    "OutboxPublishBatchResult",
    "PipelineOutboxPublisher",
    "build_media_inspect_outbox_event",
    "build_meme_created_transcode_outbox_event",
]
