"""Pipeline runtime exception types and stateless failure-classification helpers.

Split out from :mod:`memexpert.workers.pipeline_runtime.runtime` so the pure
"which exception maps to which reason string" logic lives next to the
``Forced*Failure`` dev-only knobs that feed it. Nothing in this module touches
RabbitMQ, the database, or any runtime state — every function takes its inputs
explicitly and returns a plain value, so the failure taxonomy can be unit-
tested without spinning up a ``PipelineRuntime``.
"""

from __future__ import annotations

import json
import uuid

from pydantic import ValidationError

from memexpert.core.classification import (
    ClassificationError,
    ClassificationProviderUnavailableError,
    ClassificationTimeoutError,
)
from memexpert.core.meilisearch import (
    MeilisearchSyncConflictError,
    MeilisearchSyncMalformedResponseError,
    MeilisearchSyncProviderUnavailableError,
    MeilisearchSyncTimeoutError,
)
from memexpert.core.ocr import OCRProviderUnavailableError, OCRTimeoutError
from memexpert.core.qdrant import (
    QdrantMalformedResponseError,
    QdrantProviderUnavailableError,
    QdrantSimilarityError,
    QdrantSyncConflictError,
    QdrantSyncMalformedResponseError,
    QdrantSyncProviderUnavailableError,
    QdrantSyncTimeoutError,
    QdrantTimeoutError,
)
from memexpert.core.storage import StorageObjectMissingError
from memexpert.core.voyage import (
    VoyageMalformedResponseError,
    VoyageProviderUnavailableError,
    VoyageTimeoutError,
)
from memexpert.media.contracts import MediaTimeoutError, MediaValidationError
from memexpert.models.enums import ContentPipelineStage
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import PipelineIngestError, PipelineMergeTransactionError
from memexpert.workers.pipeline_runtime.constants import (
    PIPELINE_REASON_CLASSIFY_FAILED,
    PIPELINE_REASON_CLASSIFY_MALFORMED,
    PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED,
    PIPELINE_REASON_CLASSIFY_TIMEOUT,
    PIPELINE_REASON_EMBED_FAILED,
    PIPELINE_REASON_EMBED_MALFORMED_VECTOR,
    PIPELINE_REASON_EMBED_MERGE_TRANSACTION,
    PIPELINE_REASON_EMBED_PROVIDER_BLOCKED,
    PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED,
    PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED,
    PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT,
    PIPELINE_REASON_EMBED_TIMEOUT,
    PIPELINE_REASON_FORCED_CLASSIFY_FAILURE,
    PIPELINE_REASON_FORCED_EMBED_FAILURE,
    PIPELINE_REASON_FORCED_SYNC_MEILI_FAILURE,
    PIPELINE_REASON_FORCED_SYNC_QDRANT_FAILURE,
    PIPELINE_REASON_FORCED_TRANSCODE_FAILURE,
    PIPELINE_REASON_OCR_FAILED,
    PIPELINE_REASON_OCR_PROVIDER_BLOCKED,
    PIPELINE_REASON_OCR_TIMEOUT,
    PIPELINE_REASON_SOURCE_OBJECT_MISSING,
    PIPELINE_REASON_SYNC_MEILI_CONFLICT,
    PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD,
    PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED,
    PIPELINE_REASON_SYNC_MEILI_TIMEOUT,
    PIPELINE_REASON_SYNC_QDRANT_CONFLICT,
    PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD,
    PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED,
    PIPELINE_REASON_SYNC_QDRANT_TIMEOUT,
    PIPELINE_REASON_TRANSCODE_FAILED,
    PIPELINE_REASON_TRANSCODE_INVALID_MEDIA,
    PIPELINE_REASON_TRANSCODE_TIMEOUT,
    DeadLetterPayload,
)


class ForcedTranscodeFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one transcode attempt to fail."""


class ForcedEmbedFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one embed attempt to fail."""


class ForcedClassifyFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one classify attempt to fail."""


class ForcedSyncQdrantFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one sync_qdrant attempt to fail."""


class ForcedSyncMeiliFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one sync_meili attempt to fail."""


def validate_event_payload(payload: object) -> ContentPipelineDispatchEvent | None:
    """Return a validated dispatch event or ``None`` when the payload is malformed."""

    try:
        return ContentPipelineDispatchEvent.model_validate(payload)
    except ValidationError:
        return None


def coerce_dead_letter_payload(payload: object) -> DeadLetterPayload:
    """Coerce an arbitrary broker payload into the narrow dead-letter wire shape."""

    if payload is None:
        return None
    if isinstance(payload, (str, bytes, bytearray, int, float, bool)):
        return payload
    if isinstance(payload, dict):
        return json.dumps(payload, sort_keys=True)
    return str(payload)


def normalize_failure_reason(stage: ContentPipelineStage, exc: Exception) -> str:
    """Map an exception raised inside a stage runner to a normalized reason string."""

    if isinstance(exc, StorageObjectMissingError):
        return PIPELINE_REASON_SOURCE_OBJECT_MISSING

    if stage is ContentPipelineStage.TRANSCODE:
        if isinstance(exc, ForcedTranscodeFailure):
            return PIPELINE_REASON_FORCED_TRANSCODE_FAILURE
        if isinstance(exc, MediaTimeoutError):
            return PIPELINE_REASON_TRANSCODE_TIMEOUT
        if isinstance(exc, MediaValidationError):
            return PIPELINE_REASON_TRANSCODE_INVALID_MEDIA
        return PIPELINE_REASON_TRANSCODE_FAILED

    if stage is ContentPipelineStage.OCR:
        if isinstance(exc, OCRTimeoutError):
            return PIPELINE_REASON_OCR_TIMEOUT
        if isinstance(exc, OCRProviderUnavailableError):
            return PIPELINE_REASON_OCR_PROVIDER_BLOCKED
        return PIPELINE_REASON_OCR_FAILED

    if stage is ContentPipelineStage.EMBED:
        if isinstance(exc, ForcedEmbedFailure):
            return PIPELINE_REASON_FORCED_EMBED_FAILURE
        if isinstance(exc, VoyageTimeoutError):
            return PIPELINE_REASON_EMBED_TIMEOUT
        if isinstance(exc, VoyageMalformedResponseError):
            return PIPELINE_REASON_EMBED_MALFORMED_VECTOR
        if isinstance(exc, VoyageProviderUnavailableError):
            return PIPELINE_REASON_EMBED_PROVIDER_BLOCKED
        if isinstance(exc, PipelineMergeTransactionError):
            return PIPELINE_REASON_EMBED_MERGE_TRANSACTION
        if isinstance(exc, QdrantTimeoutError):
            return PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT
        if isinstance(exc, QdrantMalformedResponseError):
            return PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED
        if isinstance(exc, QdrantProviderUnavailableError):
            return PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED
        if isinstance(exc, QdrantSimilarityError):
            return PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED
        return PIPELINE_REASON_EMBED_FAILED

    if stage is ContentPipelineStage.CLASSIFY:
        if isinstance(exc, ForcedClassifyFailure):
            return PIPELINE_REASON_FORCED_CLASSIFY_FAILURE
        if isinstance(exc, ClassificationTimeoutError):
            return PIPELINE_REASON_CLASSIFY_TIMEOUT
        if isinstance(exc, ClassificationProviderUnavailableError):
            return PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED
        if isinstance(exc, ClassificationError):
            return PIPELINE_REASON_CLASSIFY_MALFORMED
        return PIPELINE_REASON_CLASSIFY_FAILED

    if stage is ContentPipelineStage.SYNC_QDRANT:
        if isinstance(exc, ForcedSyncQdrantFailure):
            return PIPELINE_REASON_FORCED_SYNC_QDRANT_FAILURE
        if isinstance(exc, QdrantSyncTimeoutError):
            return PIPELINE_REASON_SYNC_QDRANT_TIMEOUT
        if isinstance(exc, QdrantSyncConflictError):
            return PIPELINE_REASON_SYNC_QDRANT_CONFLICT
        if isinstance(exc, QdrantSyncMalformedResponseError):
            return PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD
        if isinstance(exc, QdrantSyncProviderUnavailableError):
            return PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED
        return PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED

    if stage is ContentPipelineStage.SYNC_MEILI:
        if isinstance(exc, ForcedSyncMeiliFailure):
            return PIPELINE_REASON_FORCED_SYNC_MEILI_FAILURE
        if isinstance(exc, MeilisearchSyncTimeoutError):
            return PIPELINE_REASON_SYNC_MEILI_TIMEOUT
        if isinstance(exc, MeilisearchSyncConflictError):
            return PIPELINE_REASON_SYNC_MEILI_CONFLICT
        if isinstance(exc, MeilisearchSyncMalformedResponseError):
            return PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD
        if isinstance(exc, MeilisearchSyncProviderUnavailableError):
            return PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED
        return PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED

    return PIPELINE_REASON_OCR_FAILED


def is_replayable_failure(stage: ContentPipelineStage, exc: Exception) -> bool:
    """Return ``True`` iff the exception should be requeued for another attempt."""

    if isinstance(exc, StorageObjectMissingError):
        return False

    if stage is ContentPipelineStage.TRANSCODE:
        return not isinstance(exc, MediaValidationError)
    if stage is ContentPipelineStage.EMBED:
        # Merge-transaction failures roll back the single embed transaction and
        # must stay replayable. Genuine contract violations (malformed vectors,
        # impossible state transitions) surface the base PipelineIngestError and
        # dead-letter. Qdrant malformed responses match the VoyageMalformed
        # behavior because the payload is structurally untrustworthy.
        if isinstance(exc, PipelineMergeTransactionError):
            return True
        return not isinstance(
            exc,
            (VoyageMalformedResponseError, QdrantMalformedResponseError, PipelineIngestError),
        )
    if stage is ContentPipelineStage.CLASSIFY:
        return not isinstance(exc, PipelineIngestError)
    if stage is ContentPipelineStage.SYNC_QDRANT:
        # Malformed sync responses are terminal (dead-letter); timeout,
        # provider-unavailable, and 409 conflicts are transient and
        # replayable — including forced-failure injection so the dev
        # knob exercises the full retry path.
        if isinstance(exc, QdrantSyncMalformedResponseError):
            return False
        return not isinstance(exc, PipelineIngestError)
    if stage is ContentPipelineStage.SYNC_MEILI:
        # Mirrors the Qdrant branch exactly: malformed dead-letters,
        # everything else (including forced-failure injection) stays
        # replayable until attempts are exhausted.
        if isinstance(exc, MeilisearchSyncMalformedResponseError):
            return False
        return not isinstance(exc, PipelineIngestError)
    return not isinstance(exc, PipelineIngestError)


def render_error_text(exc: Exception) -> str:
    """Return a non-empty one-line error message suitable for durable logging."""

    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def extract_event_reference(
    payload: object,
) -> tuple[uuid.UUID, ContentPipelineStage, int, uuid.UUID] | None:
    """Return ``(meme_file_id, stage, attempt, event_id)`` from a loosely-typed payload."""

    if not isinstance(payload, dict):
        return None

    raw_meme_file_id = payload.get("meme_file_id")
    raw_stage = payload.get("stage")
    if not isinstance(raw_meme_file_id, str) or not isinstance(raw_stage, str):
        return None

    try:
        meme_file_id = uuid.UUID(raw_meme_file_id)
        stage = ContentPipelineStage(raw_stage)
    except (ValueError, TypeError):
        return None

    raw_attempt = payload.get("attempt")
    attempt = raw_attempt if isinstance(raw_attempt, int) and raw_attempt >= 1 else 1

    raw_event_id = payload.get("event_id")
    try:
        event_id = uuid.UUID(raw_event_id) if isinstance(raw_event_id, str) else uuid.uuid7()
    except ValueError:
        event_id = uuid.uuid7()

    return meme_file_id, stage, attempt, event_id


__all__ = [
    "ForcedClassifyFailure",
    "ForcedEmbedFailure",
    "ForcedSyncMeiliFailure",
    "ForcedSyncQdrantFailure",
    "ForcedTranscodeFailure",
    "coerce_dead_letter_payload",
    "extract_event_reference",
    "is_replayable_failure",
    "normalize_failure_reason",
    "render_error_text",
    "validate_event_payload",
]
