# ruff: noqa: TC001,TC003
"""API-safe pipeline event DTO builders for transactional outbox rows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from memexpert.core.config import Settings
from memexpert.models.enums import SourcePlatform

PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE: Final = "pipeline_ingest_request"
MEDIA_INSPECT_REQUESTED_EVENT_TYPE: Final = "media_inspect_requested"


def build_media_inspect_routing_key(settings: Settings) -> str:
    """Return the future media-inspect worker routing key."""

    return f"{settings.pipeline_broker_routing_key_prefix}.media_inspect"


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


__all__ = [
    "MEDIA_INSPECT_REQUESTED_EVENT_TYPE",
    "PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE",
    "build_media_inspect_requested_payload",
    "build_media_inspect_routing_key",
]
