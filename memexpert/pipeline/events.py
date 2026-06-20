# ruff: noqa: TC001,TC003
"""API-safe pipeline event DTO builders for transactional outbox rows."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from memexpert.core.config import Settings
from memexpert.messaging.rabbitmq_outbox import RabbitMessageSpec
from memexpert.models.base import utcnow
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentSourceKind,
    SourceEngagementScheduleLabel,
    SourcePlatform,
)
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType

if TYPE_CHECKING:
    from memexpert.models.content import MemeFile, MemeSource, PipelineIngestRequest

PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE: Final = "pipeline_ingest_request"
PIPELINE_MEME_FILE_AGGREGATE_TYPE: Final = "meme_file"
PIPELINE_MEME_SOURCE_AGGREGATE_TYPE: Final = "meme_source"
MEDIA_INSPECT_REQUESTED_EVENT_TYPE: Final = "media_inspect_requested"
SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE: Final = "source_engagement_capture_requested"
_SOURCE_ENGAGEMENT_SESSION_KEY_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")
_SOURCE_ENGAGEMENT_SESSION_KEY_PREFIX_MAX_LENGTH: Final = 48
_SOURCE_ENGAGEMENT_SESSION_KEY_HASH_LENGTH: Final = 12


class MediaInspectRequestedEvent(BaseModel):
    """Validated payload consumed by the worker-side media-inspect handler."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["media_inspect_requested"]
    ingest_request_id: uuid.UUID
    source_platform: SourcePlatform
    sha256_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class SourceEngagementCaptureRequestedEvent(BaseModel):
    """Validated payload consumed by the source-engagement capture worker."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["source_engagement_capture_requested"]
    meme_source_id: uuid.UUID
    source_platform: SourcePlatform
    source_id: str = Field(min_length=1, max_length=255)
    post_id: str = Field(min_length=1, max_length=255)
    scheduled_for: datetime
    schedule_label: SourceEngagementScheduleLabel
    telegram_session_id: uuid.UUID
    session_name: str = Field(min_length=1, max_length=64)
    session_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    created_at: datetime


def build_media_inspect_routing_key(settings: Settings) -> str:
    """Return the future media-inspect worker routing key."""

    return f"{settings.pipeline_broker_routing_key_prefix}.media_inspect"


def build_source_engagement_session_key(telegram_session_id: uuid.UUID, session_name: str) -> str:
    """Return the stable RabbitMQ-safe source engagement suffix for one Telegram session."""

    normalized_name = session_name.strip()
    if not normalized_name:
        raise ValueError("source engagement session key requires a non-blank session_name.")

    sanitized_name = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized_name).strip("._-")
    if not sanitized_name:
        sanitized_name = "session"
    sanitized_name = sanitized_name[:_SOURCE_ENGAGEMENT_SESSION_KEY_PREFIX_MAX_LENGTH].strip("._-") or "session"
    session_hash = hashlib.sha256(str(telegram_session_id).encode("ascii")).hexdigest()[
        :_SOURCE_ENGAGEMENT_SESSION_KEY_HASH_LENGTH
    ]
    return f"{sanitized_name}.{session_hash}"


def build_source_engagement_capture_routing_key(settings: Settings, *, session_key: str) -> str:
    """Return the worker routing key for source engagement capture work."""

    normalized_session_key = session_key.strip()
    if _SOURCE_ENGAGEMENT_SESSION_KEY_RE.fullmatch(normalized_session_key) is None:
        raise ValueError(
            "source engagement session_key may contain only letters, numbers, dots, underscores, and hyphens.",
        )
    return f"{settings.pipeline_broker_routing_key_prefix}.source_engagement_capture.{normalized_session_key}"


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


def build_source_engagement_capture_requested_payload(
    *,
    event_id: uuid.UUID,
    meme_source_id: uuid.UUID,
    source_platform: SourcePlatform,
    source_id: str,
    post_id: str,
    scheduled_for: datetime,
    schedule_label: SourceEngagementScheduleLabel,
    telegram_session_id: uuid.UUID,
    session_name: str,
    created_at: datetime,
) -> dict[str, object]:
    """Build the JSONB payload for a scheduled source engagement capture request."""

    session_key = build_source_engagement_session_key(telegram_session_id, session_name)

    return SourceEngagementCaptureRequestedEvent(
        event_id=event_id,
        event_type=SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE,
        meme_source_id=meme_source_id,
        source_platform=source_platform,
        source_id=source_id,
        post_id=post_id,
        scheduled_for=scheduled_for,
        schedule_label=schedule_label,
        telegram_session_id=telegram_session_id,
        session_name=session_name,
        session_key=session_key,
        created_at=created_at,
    ).model_dump(mode="json")


def build_source_engagement_capture_message_spec(
    source: MemeSource,
    *,
    scheduled_for: datetime,
    schedule_label: SourceEngagementScheduleLabel,
    settings: Settings,
    telegram_session_id: uuid.UUID,
    session_name: str,
) -> RabbitMessageSpec:
    """Return a durable RabbitMQ message spec for scheduled source engagement capture."""

    event_id = uuid.uuid7()
    created_at = utcnow()
    payload = build_source_engagement_capture_requested_payload(
        event_id=event_id,
        meme_source_id=source.id,
        source_platform=source.platform,
        source_id=source.source_id,
        post_id=source.post_id,
        scheduled_for=scheduled_for,
        schedule_label=schedule_label,
        telegram_session_id=telegram_session_id,
        session_name=session_name,
        created_at=created_at,
    )
    return RabbitMessageSpec(
        exchange=settings.pipeline_broker_exchange,
        routing_key=build_source_engagement_capture_routing_key(settings, session_key=str(payload["session_key"])),
        payload=payload,
        message_id=str(event_id),
        event_type=SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE,
        aggregate_type=PIPELINE_MEME_SOURCE_AGGREGATE_TYPE,
        aggregate_id=source.id,
        ordering_key=str(source.id),
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
    "PIPELINE_MEME_SOURCE_AGGREGATE_TYPE",
    "SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE",
    "MediaInspectRequestedEvent",
    "SourceEngagementCaptureRequestedEvent",
    "build_media_inspect_message_spec",
    "build_meme_created_transcode_dispatch_event",
    "build_meme_created_transcode_message_spec",
    "build_media_inspect_requested_payload",
    "build_media_inspect_routing_key",
    "build_source_engagement_capture_message_spec",
    "build_source_engagement_capture_requested_payload",
    "build_source_engagement_capture_routing_key",
    "build_source_engagement_session_key",
    "build_stage_routing_key",
]
