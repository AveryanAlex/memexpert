"""Shared materialization dataclasses and protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid

    from memexpert.models.content import BlockedPerceptualHash, PipelineIngestRequest, PipelineOutboxEvent
    from memexpert.models.enums import ContentKind, PipelineIngestRequestStatus


FAILED_INVALID_MEDIA_CODE = "invalid_media"
FAILED_MEDIA_TOO_LARGE_CODE = "payload_too_large"
FAILED_BLOCKED_PHASH_CODE = "blocked_perceptual_hash"


class ObjectStorageClient(Protocol):
    """Minimal S3-compatible surface used by materialization."""

    def get_object(self, *, Bucket: str, Key: str) -> object: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


@dataclass(frozen=True, slots=True)
class PreparedMaterialization:
    """Inspected upload metadata plus the canonical original object key."""

    filename: str
    media_type: ContentKind
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    perceptual_hash: str
    sha256_hex: str
    object_key: str


@dataclass(frozen=True, slots=True)
class BlockedPerceptualHashMatch:
    """Active blocked pHash row plus its computed distance to incoming media."""

    blocked_hash: BlockedPerceptualHash
    hamming_distance: int


@dataclass(frozen=True, slots=True)
class PipelineIngestMaterializationResult:
    """Outcome summary returned by one materialization attempt."""

    ingest_request_id: uuid.UUID
    status: PipelineIngestRequestStatus
    materialized_meme_id: uuid.UUID | None = None
    materialized_meme_file_id: uuid.UUID | None = None
    matched_meme_file_id: uuid.UUID | None = None
    outbox_event_id: uuid.UUID | None = None


def build_materialization_result(
    ingest_request: PipelineIngestRequest,
    *,
    outbox_event: PipelineOutboxEvent | None = None,
) -> PipelineIngestMaterializationResult:
    """Build the public result from persisted ingest-request state."""

    return PipelineIngestMaterializationResult(
        ingest_request_id=ingest_request.id,
        status=ingest_request.status,
        materialized_meme_id=ingest_request.materialized_meme_id,
        materialized_meme_file_id=ingest_request.materialized_meme_file_id,
        matched_meme_file_id=ingest_request.matched_meme_file_id,
        outbox_event_id=outbox_event.id if outbox_event is not None else None,
    )


__all__ = [
    "FAILED_BLOCKED_PHASH_CODE",
    "FAILED_INVALID_MEDIA_CODE",
    "FAILED_MEDIA_TOO_LARGE_CODE",
    "BlockedPerceptualHashMatch",
    "ObjectStorageClient",
    "PipelineIngestMaterializationResult",
    "PreparedMaterialization",
    "build_materialization_result",
]
