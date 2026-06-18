# ruff: noqa: TC001
"""Transactional outbox row builders for API-safe pipeline entrypoints."""

from __future__ import annotations

import uuid

from memexpert.core.config import Settings
from memexpert.models.base import utcnow
from memexpert.models.content import PipelineIngestRequest, PipelineOutboxEvent
from memexpert.models.enums import PipelineOutboxEventStatus
from memexpert.pipeline.events import (
    MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
    PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
    build_media_inspect_requested_payload,
    build_media_inspect_routing_key,
)


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


__all__ = ["build_media_inspect_outbox_event"]
