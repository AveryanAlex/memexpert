# ruff: noqa: TC001,TC003
"""API-safe pipeline event DTO builders for transactional outbox rows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from memexpert.core.config import Settings
from memexpert.messaging.rabbitmq_outbox import RabbitMessageSpec
from memexpert.models.base import utcnow
from memexpert.models.enums import ContentPipelineStage, ContentSourceKind, SourcePlatform
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType

if TYPE_CHECKING:
    from memexpert.models.content import MemeFile, PipelineIngestRequest

PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE: Final = "pipeline_ingest_request"
PIPELINE_MEME_FILE_AGGREGATE_TYPE: Final = "meme_file"
MEDIA_INSPECT_REQUESTED_EVENT_TYPE: Final = "media_inspect_requested"


class MediaInspectRequestedEvent(BaseModel):
    """Validated payload consumed by the worker-side media-inspect handler."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["media_inspect_requested"]
    ingest_request_id: uuid.UUID
    source_platform: SourcePlatform
    sha256_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


def build_media_inspect_routing_key(settings: Settings) -> str:
    """Return the future media-inspect worker routing key."""

    return f"{settings.pipeline_broker_routing_key_prefix}.media_inspect"


def build_stage_routing_key(settings: Settings, stage: ContentPipelineStage) -> str:
    """Return the worker routing key for one materialized content stage."""

    return f"{settings.pipeline_broker_routing_key_prefix}.{stage.value}"


def build_media_inspect_requested_payload(
    *,
    event_id: uuid.UUID,
    ingest_request_id: uuid.UUID,
    source_platform: SourcePlatform,
    sha256_hex: str,
    created_at: datetime,
) -> dict[str, object]:
    """Build the JSONB payload for a raw-ingest media inspection request."""

    return {
        "event_id": str(event_id),
        "event_type": MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
        "ingest_request_id": str(ingest_request_id),
        "source_platform": source_platform.value,
        "sha256_hex": sha256_hex,
        "created_at": created_at.isoformat(),
    }


def build_media_inspect_message_spec(
    ingest_request: PipelineIngestRequest,
    *,
    settings: Settings,
) -> RabbitMessageSpec:
    """Return a durable RabbitMQ message spec for future raw media inspection."""

    if ingest_request.sha256_hex is None:
        raise ValueError("media-inspect outbox payload requires ingest_request.sha256_hex.")

    event_id = uuid.uuid7()
    created_at = utcnow()
    return RabbitMessageSpec(
        exchange=settings.pipeline_broker_exchange,
        routing_key=build_media_inspect_routing_key(settings),
        payload=build_media_inspect_requested_payload(
            event_id=event_id,
            ingest_request_id=ingest_request.id,
            source_platform=ingest_request.source_platform,
            sha256_hex=ingest_request.sha256_hex,
            created_at=created_at,
        ),
        message_id=str(event_id),
        event_type=MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
        aggregate_type=PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
        aggregate_id=ingest_request.id,
        ordering_key=str(ingest_request.id),
        created_at=created_at,
    )


def build_meme_created_transcode_dispatch_event(
    *,
    event_id: uuid.UUID,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID,
    original_object_key: str,
    created_at: datetime,
) -> ContentPipelineDispatchEvent:
    """Build the first materialized stage dispatch for a newly-created meme file."""

    return ContentPipelineDispatchEvent(
        event_id=event_id,
        event_type=ContentPipelineEventType.MEME_CREATED,
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=original_object_key,
        attempt=1,
        created_at=created_at,
    )


def build_meme_created_transcode_message_spec(
    meme_file: MemeFile,
    *,
    event_id: uuid.UUID,
    created_at: datetime,
    settings: Settings,
) -> RabbitMessageSpec:
    """Return a durable RabbitMQ message spec for the first transcode dispatch."""

    dispatch_event = build_meme_created_transcode_dispatch_event(
        event_id=event_id,
        meme_id=meme_file.meme_id,
        meme_file_id=meme_file.id,
        original_object_key=meme_file.s3_original_key,
        created_at=created_at,
    )
    return RabbitMessageSpec(
        exchange=settings.pipeline_broker_exchange,
        routing_key=build_stage_routing_key(settings, dispatch_event.stage),
        payload=dispatch_event.model_dump(mode="json"),
        message_id=str(event_id),
        event_type=ContentPipelineEventType.MEME_CREATED.value,
        aggregate_type=PIPELINE_MEME_FILE_AGGREGATE_TYPE,
        aggregate_id=meme_file.id,
        ordering_key=str(meme_file.id),
        created_at=created_at,
    )


__all__ = [
    "MEDIA_INSPECT_REQUESTED_EVENT_TYPE",
    "PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE",
    "PIPELINE_MEME_FILE_AGGREGATE_TYPE",
    "MediaInspectRequestedEvent",
    "build_media_inspect_message_spec",
    "build_meme_created_transcode_dispatch_event",
    "build_meme_created_transcode_message_spec",
    "build_media_inspect_requested_payload",
    "build_media_inspect_routing_key",
    "build_stage_routing_key",
]
