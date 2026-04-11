"""Foundational content-pipeline enums, error codes, and length constants.

Kept in a dedicated module so downstream schema files (``pipeline_ingest`` and
``content_pipeline``) and other call sites can share the same machine-readable
vocabulary without pulling in the full runtime/read-model surface. The typed
content-pipeline package re-exports every name from here via
``memexpert.schemas.content_pipeline`` for backward compatibility.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from memexpert.models.enums import ContentPipelineStage

MAX_OBJECT_KEY_LENGTH = 1024
MAX_PIPELINE_REASON_LENGTH = 128
MAX_PIPELINE_ERROR_LENGTH = 4000
MAX_SOURCE_ID_LENGTH = 255
MAX_POST_ID_LENGTH = 255
MAX_TELEGRAM_CHANNEL_TITLE_LENGTH = 255
MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH = 255
MAX_TELEGRAM_FILENAME_LENGTH = 255
MAX_TELEGRAM_CONTENT_TYPE_LENGTH = 255
MAX_TELEGRAM_SESSION_NAME_LENGTH = 64


class ContentPipelineEventType(StrEnum):
    """Machine-readable broker event names used by the content pipeline."""

    MEME_CREATED = "meme_created"
    MEME_TRANSCODED = "meme_transcoded"
    MEME_OCR_DONE = "meme_ocr_done"
    MEME_EMBEDDED = "meme_embedded"
    MEME_READY = "meme_ready"
    MEME_QDRANT_SYNCED = "meme_qdrant_synced"
    MEME_MEILI_SYNCED = "meme_meili_synced"
    STAGE_REPLAY_REQUESTED = "stage_replay_requested"


_PIPELINE_EVENT_ALLOWED_STAGES: dict[ContentPipelineEventType, frozenset[ContentPipelineStage]] = {
    ContentPipelineEventType.MEME_CREATED: frozenset({ContentPipelineStage.TRANSCODE}),
    ContentPipelineEventType.MEME_TRANSCODED: frozenset({ContentPipelineStage.OCR}),
    ContentPipelineEventType.MEME_OCR_DONE: frozenset({ContentPipelineStage.EMBED}),
    ContentPipelineEventType.MEME_EMBEDDED: frozenset({ContentPipelineStage.CLASSIFY}),
    ContentPipelineEventType.MEME_READY: frozenset(
        {
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        }
    ),
    ContentPipelineEventType.MEME_QDRANT_SYNCED: frozenset({ContentPipelineStage.SYNC_QDRANT}),
    ContentPipelineEventType.MEME_MEILI_SYNCED: frozenset({ContentPipelineStage.SYNC_MEILI}),
    ContentPipelineEventType.STAGE_REPLAY_REQUESTED: frozenset(
        {
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        }
    ),
}


class ContentPipelineErrorCode(StrEnum):
    """Machine-readable pipeline error codes returned by operator-facing routes."""

    INVALID_OPERATOR_TOKEN = "invalid_operator_token"
    ITEM_NOT_FOUND = "pipeline_item_not_found"
    PAYLOAD_INVALID = "pipeline_payload_invalid"
    PAYLOAD_TOO_LARGE = "pipeline_payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "pipeline_unsupported_media_type"
    SOURCE_CONFLICT = "pipeline_source_conflict"
    STORAGE_FAILURE = "pipeline_storage_failure"
    INGEST_FAILURE = "pipeline_ingest_failure"
    PUBLISH_FAILURE = "pipeline_publish_failure"
    REPLAY_NOT_ALLOWED = "pipeline_replay_not_allowed"
    CRAWLER_CHANNEL_NOT_FOUND = "crawler_channel_not_found"
    CRAWLER_CHANNEL_NOT_TRACKED = "crawler_channel_not_tracked"
    CRAWLER_INVALID_SESSION = "crawler_invalid_session"
    CRAWLER_SESSION_NOT_RUNNABLE = "crawler_session_not_runnable"
    TELEGRAM_FLOOD_WAIT = "telegram_flood_wait"
    TELEGRAM_SESSION_BANNED = "telegram_session_banned"
    TELEGRAM_PROVIDER_UNAVAILABLE = "telegram_provider_unavailable"
    TELEGRAM_MALFORMED_MESSAGE = "telegram_malformed_message"


class ContentPipelineItemFilter(StrEnum):
    """List filters exposed by the operator-only inspect surface."""

    ALL = "all"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    STUCK = "stuck"


class ContentPipelineErrorResponse(BaseModel):
    """Machine-readable content-pipeline error payload used by HTTP routes."""

    code: ContentPipelineErrorCode
    detail: str


__all__ = [
    "MAX_OBJECT_KEY_LENGTH",
    "MAX_PIPELINE_ERROR_LENGTH",
    "MAX_PIPELINE_REASON_LENGTH",
    "MAX_POST_ID_LENGTH",
    "MAX_SOURCE_ID_LENGTH",
    "MAX_TELEGRAM_CHANNEL_TITLE_LENGTH",
    "MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH",
    "MAX_TELEGRAM_CONTENT_TYPE_LENGTH",
    "MAX_TELEGRAM_FILENAME_LENGTH",
    "MAX_TELEGRAM_SESSION_NAME_LENGTH",
    "ContentPipelineErrorCode",
    "ContentPipelineErrorResponse",
    "ContentPipelineEventType",
    "ContentPipelineItemFilter",
    "_PIPELINE_EVENT_ALLOWED_STAGES",
]
