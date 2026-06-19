# ruff: noqa: TC001,TC003
"""API-safe pipeline event DTO builders for transactional outbox rows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from memexpert.core.config import Settings
from memexpert.models.enums import ContentPipelineStage, ContentSourceKind, SourcePlatform
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType

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


__all__ = [
    "MEDIA_INSPECT_REQUESTED_EVENT_TYPE",
    "PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE",
    "PIPELINE_MEME_FILE_AGGREGATE_TYPE",
    "MediaInspectRequestedEvent",
    "build_meme_created_transcode_dispatch_event",
    "build_media_inspect_requested_payload",
    "build_media_inspect_routing_key",
    "build_stage_routing_key",
]
