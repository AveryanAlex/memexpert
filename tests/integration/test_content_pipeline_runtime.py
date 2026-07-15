"""Integration tests for the RabbitMQ-backed transcode and OCR runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import pytest
from PIL import Image
from sqlalchemy import select

from memexpert.core.broker import build_pipeline_broker, get_pipeline_broker_settings
from memexpert.core.classification import (
    ClassificationProviderUnavailableError,
    ClassificationResult,
)
from memexpert.core.config import Settings
from memexpert.core.meilisearch import (
    MeilisearchSyncConflictError,
    MeilisearchSyncMalformedResponseError,
    MeilisearchSyncProviderUnavailableError,
    MeilisearchSyncTimeoutError,
    PipelineMeilisearchDocument,
)
from memexpert.core.ocr import OCRExtractionResult, OCRTimeoutError
from memexpert.core.qdrant import (
    QdrantMalformedResponseError,
    QdrantProviderUnavailableError,
    QdrantSimilarityMatch,
    QdrantSyncConflictError,
    QdrantSyncMalformedResponseError,
    QdrantSyncPayload,
    QdrantSyncProviderUnavailableError,
    QdrantSyncTimeoutError,
    QdrantTimeoutError,
)
from memexpert.core.voyage import (
    VoyageEmbeddingResult,
    VoyageMalformedResponseError,
    VoyageProviderUnavailableError,
    VoyageTimeoutError,
)
from memexpert.crawlers.telegram.client import FakeTelegramClient, PipelineTelegramFloodWaitError, RawTelegramMessage
from memexpert.media.contracts import (
    NormalizedMediaResult,
    UploadMediaDetails,
)
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemeSeoPage,
    MemeSource,
    MemeSourceEngagementSnapshot,
    MemeTemplate,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    TelegramSession,
)
from memexpert.models.enums import (
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    IngestFileOrigin,
    IngestSourceKind,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    SourceAttachReason,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramSessionStatus,
)
from memexpert.models.operations import PipelineStageAttempt
from memexpert.models.user import User
from memexpert.pipeline.events import (
    SourceEngagementCaptureRequestedEvent,
    build_media_inspect_requested_payload,
    build_source_engagement_session_key,
)
from memexpert.pipeline.items import PipelineItemReadService
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineItemRead,
    ContentPipelineStageJournalRead,
    ContentPipelineSyncTargetPreview,
)
from memexpert.services import PipelineIngestError
from memexpert.services.search_index_sync import SEARCH_INDEX_ALGORITHM_VERSION
from memexpert.workers.pipeline_runtime import (
    PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED,
    PIPELINE_REASON_EMBED_MALFORMED_VECTOR,
    PIPELINE_REASON_EMBED_MERGE_TRANSACTION,
    PIPELINE_REASON_EMBED_PROVIDER_BLOCKED,
    PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED,
    PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED,
    PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT,
    PIPELINE_REASON_EMBED_TIMEOUT,
    PIPELINE_REASON_FORCED_SYNC_MEILI_FAILURE,
    PIPELINE_REASON_FORCED_SYNC_QDRANT_FAILURE,
    PIPELINE_REASON_FORCED_TRANSCODE_FAILURE,
    PIPELINE_REASON_MALFORMED_EVENT,
    PIPELINE_REASON_MEDIA_INSPECT_FAILED,
    PIPELINE_REASON_OCR_TIMEOUT,
    PIPELINE_REASON_SYNC_MEILI_CONFLICT,
    PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD,
    PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED,
    PIPELINE_REASON_SYNC_MEILI_TIMEOUT,
    PIPELINE_REASON_SYNC_QDRANT_CONFLICT,
    PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD,
    PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED,
    PIPELINE_REASON_SYNC_QDRANT_TIMEOUT,
    build_pipeline_runtime,
)
from memexpert.workers.pipeline_runtime.stage_registry import PIPELINE_STAGE_HANDLERS, RUNNABLE_DOWNSTREAM_STAGES
from memexpert.workers.pipeline_runtime.stages.classify import run_classify_stage
from memexpert.workers.pipeline_runtime.stages.embed import run_embed_stage
from memexpert.workers.pipeline_runtime.stages.ocr import run_ocr_stage
from memexpert.workers.pipeline_runtime.stages.sync_meili import run_sync_meili_stage
from memexpert.workers.pipeline_runtime.stages.sync_qdrant import run_sync_qdrant_stage
from memexpert.workers.pipeline_runtime.stages.transcode import run_transcode_stage
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from aio_pika.abc import HeadersType
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from memexpert.core.search_index_prefilter import SearchIndexPrefilter


@dataclass(slots=True)
class StoredObject:
    body: bytes
    content_type: str


@dataclass(slots=True)
class FakeStorageBody:
    payload: bytes

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        return None


@dataclass(slots=True)
class FakeStorageClient:
    """Small S3-compatible client used by runtime-backed tests."""

    objects: dict[str, StoredObject] = field(default_factory=dict)
    put_calls: list[dict[str, object]] = field(default_factory=list)
    delete_calls: list[dict[str, object]] = field(default_factory=list)

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object:
        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
                "ContentLength": ContentLength,
            }
        )
        self.objects[Key] = StoredObject(body=Body, content_type=ContentType)
        return {"ETag": "fake"}

    def get_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        stored = self.objects[Key]
        return {
            "Body": FakeStorageBody(stored.body),
            "ContentType": stored.content_type,
            "ContentLength": len(stored.body),
        }

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop(Key, None)
        return {"DeleteMarker": True}


@dataclass(slots=True)
class RecordingBroker:
    """Broker double that captures relayed content-pipeline dispatch events."""

    events: list[ContentPipelineDispatchEvent] = field(default_factory=list)
    publish_calls: list[dict[str, object]] = field(default_factory=list)

    async def publish(
        self,
        message: object,
        /,
        queue: str = "",
        exchange: str | None = None,
        *,
        routing_key: str = "",
        mandatory: bool = True,
        persist: bool = False,
        content_type: str | None = None,
        headers: HeadersType | None = None,
        message_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> object:
        self.publish_calls.append(
            {
                "payload": message,
                "queue": queue,
                "exchange": exchange,
                "routing_key": routing_key,
                "mandatory": mandatory,
                "persist": persist,
                "content_type": content_type,
                "headers": headers,
                "message_id": message_id,
                "timestamp": timestamp,
            }
        )
        self.events.append(ContentPipelineDispatchEvent.model_validate(message))
        return None


@dataclass(slots=True)
class PublishingBroker:
    """Small broker double used to observe downstream stage dispatches."""

    publish_calls: list[dict[str, object]] = field(default_factory=list)
    fail_on_routing_keys: set[str] = field(default_factory=set)

    async def publish(self, payload: object, **kwargs: object) -> None:
        self.publish_calls.append({"payload": payload, **kwargs})
        routing_key = kwargs.get("routing_key")
        if routing_key in self.fail_on_routing_keys:
            raise RuntimeError(f"simulated publish failure for {routing_key}")

    def subscriber(self, *_args: object, **_kwargs: object) -> object:
        def decorator(handler: object) -> object:
            return handler

        return decorator


@dataclass(slots=True)
class FakeMediaProcessor:
    """Typed media boundary double used to make runtime transcode tests deterministic."""

    normalize_result: NormalizedMediaResult | None = None
    normalize_error: Exception | None = None
    preview_frame_bytes: bytes = b"fake-preview-frame-bytes"
    inspect_result: UploadMediaDetails | None = None

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        if self.inspect_result is not None:
            return self.inspect_result
        raise AssertionError("inspect_upload should not be called by runtime tests")

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult:
        _ = (meme_file_id, filename, content_type, media_bytes)
        if self.normalize_error is not None:
            raise self.normalize_error
        assert self.normalize_result is not None
        return self.normalize_result

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        _ = (filename, content_type, media_bytes)
        return self.preview_frame_bytes


@dataclass(slots=True)
class FakeOCRProcessor:
    """Typed OCR boundary double used to make runtime OCR tests deterministic."""

    result: OCRExtractionResult | None = None
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def extract_text(
        self,
        *,
        filename: str,
        mime_type: str,
        media_bytes: bytes,
        source_object_key: str,
    ) -> OCRExtractionResult:
        self.calls.append(
            {
                "filename": filename,
                "mime_type": mime_type,
                "media_bytes": media_bytes,
                "source_object_key": source_object_key,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeVoyageClient:
    """Typed Voyage boundary double used to make runtime embed tests deterministic."""

    result: VoyageEmbeddingResult | None = None
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def embed_image(self, *, image_bytes: bytes, mime_type: str) -> VoyageEmbeddingResult:
        self.calls.append({"mime_type": mime_type, "image_size": len(image_bytes)})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    async def embed_text(self, *, text: str) -> VoyageEmbeddingResult:
        self.calls.append({"text": text})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeQdrantClient:
    """Typed Qdrant boundary double used to make runtime embed tests deterministic."""

    matches: tuple[QdrantSimilarityMatch, ...] = ()
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def find_similar_memes(
        self,
        *,
        vector: tuple[float, ...],
        current_meme_file_id: uuid.UUID,
        scope: object | None = None,
        limit: int | None = None,
    ) -> tuple[QdrantSimilarityMatch, ...]:
        _ = scope
        self.calls.append({"vector_len": len(vector), "meme_file_id": current_meme_file_id, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.matches


@dataclass(slots=True)
class FakeQdrantSyncClient:
    """Typed Qdrant sync boundary double used to make runtime sync tests deterministic."""

    upsert_error: Exception | None = None
    fetch_error: Exception | None = None
    fetch_preview: ContentPipelineSyncTargetPreview | None = None
    upsert_calls: list[dict[str, object]] = field(default_factory=list)
    fetch_calls: list[uuid.UUID] = field(default_factory=list)
    delete_calls: list[uuid.UUID] = field(default_factory=list)

    async def upsert_meme_point(
        self,
        payload: QdrantSyncPayload,
        vector: tuple[float, ...],
    ) -> None:
        self.upsert_calls.append(
            {
                "meme_file_id": payload.meme_file_id,
                "meme_id": payload.meme_id,
                "search_index_algorithm_version": payload.search_index_algorithm_version,
                "is_public": payload.is_public,
                "uploader_user_ids": list(payload.uploader_user_ids),
                "media_type": payload.media_type,
                "language": payload.language,
                "tags": list(payload.tags),
                "is_nsfw": payload.is_nsfw,
                "template_slug": payload.template_slug,
                "popularity_score": payload.popularity_score,
                "like_count": payload.like_count,
                "collection_ids": list(payload.collection_ids),
                "public_collection_ids": list(payload.public_collection_ids),
                "unlisted_collection_ids": list(payload.unlisted_collection_ids),
                "private_collection_ids": list(payload.private_collection_ids),
                "shared_collection_ids": list(payload.shared_collection_ids),
                "collection_owner_user_ids": list(payload.collection_owner_user_ids),
                "collection_member_user_ids": list(payload.collection_member_user_ids),
                "vector_len": len(vector),
            }
        )
        if self.upsert_error is not None:
            raise self.upsert_error

    async def fetch_meme_point(
        self,
        meme_file_id: uuid.UUID,
    ) -> ContentPipelineSyncTargetPreview | None:
        self.fetch_calls.append(meme_file_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_preview

    async def delete_meme_point(self, meme_file_id: uuid.UUID) -> None:
        self.delete_calls.append(meme_file_id)


@dataclass(slots=True)
class FakeMeilisearchSyncClient:
    """Typed Meilisearch sync boundary double used to make runtime sync tests deterministic."""

    upsert_error: Exception | None = None
    fetch_error: Exception | None = None
    fetch_preview: ContentPipelineSyncTargetPreview | None = None
    upsert_calls: list[dict[str, object]] = field(default_factory=list)
    fetch_calls: list[uuid.UUID] = field(default_factory=list)
    delete_calls: list[uuid.UUID] = field(default_factory=list)

    async def upsert_document(
        self,
        document: PipelineMeilisearchDocument,
    ) -> None:
        self.upsert_calls.append(
            {
                "id": document.id,
                "meme_id": document.meme_id,
                "meme_file_id": document.meme_file_id,
                "search_index_algorithm_version": document.search_index_algorithm_version,
                "is_public": document.is_public,
                "media_type": document.media_type,
                "tags": list(document.tags),
                "is_nsfw": document.is_nsfw,
                "language": document.language,
                "template_slug": document.template_slug,
                "popularity_score": document.popularity_score,
                "like_count": document.like_count,
                "collection_ids": list(document.collection_ids),
                "public_collection_ids": list(document.public_collection_ids),
                "unlisted_collection_ids": list(document.unlisted_collection_ids),
                "private_collection_ids": list(document.private_collection_ids),
                "shared_collection_ids": list(document.shared_collection_ids),
                "collection_owner_user_ids": list(document.collection_owner_user_ids),
                "collection_member_user_ids": list(document.collection_member_user_ids),
            }
        )
        if self.upsert_error is not None:
            raise self.upsert_error

    async def fetch_document(
        self,
        meme_file_id: uuid.UUID,
    ) -> ContentPipelineSyncTargetPreview | None:
        self.fetch_calls.append(meme_file_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_preview

    async def delete_document(self, meme_file_id: uuid.UUID) -> None:
        self.delete_calls.append(meme_file_id)

    async def ensure_index(self) -> None:
        return None

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        prefilter: SearchIndexPrefilter | None = None,
    ) -> list[dict[str, Any]]:
        _ = query
        _ = limit
        _ = prefilter
        return []


@dataclass(slots=True)
class FakeClassificationClient:
    """Typed classification boundary double used to make runtime classify tests deterministic."""

    result: ClassificationResult | None = None
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def classify_image(self, *, image_bytes: bytes, mime_type: str) -> ClassificationResult:
        self.calls.append({"mime_type": mime_type, "image_size": len(image_bytes)})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeRabbitMessage:
    """Small RabbitMQ message double used to verify worker ack decisions."""

    headers: dict[str, Any] = field(default_factory=dict)
    content_type: str | None = "application/json"
    message_id: str | None = None
    ack_count: int = 0
    reject_calls: list[bool] = field(default_factory=list)
    nack_calls: list[bool] = field(default_factory=list)

    async def ack(self, multiple: bool = False) -> None:
        _ = multiple
        self.ack_count += 1

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None:
        _ = multiple
        self.nack_calls.append(requeue)

    async def reject(self, requeue: bool = False) -> None:
        self.reject_calls.append(requeue)


@dataclass(slots=True)
class RecordedExchange:
    """Exchange stub returned by declare_exchange during topology tests."""

    name: str


@dataclass(slots=True)
class RecordedQueue:
    """Queue stub returned by declare_queue during topology tests."""

    name: str
    bindings: list[tuple[str, str]] = field(default_factory=list)

    async def bind(self, exchange: RecordedExchange, *, routing_key: str) -> None:
        self.bindings.append((exchange.name, routing_key))


def build_png_bytes(*, color: tuple[int, int, int]) -> bytes:
    """Generate a tiny PNG image payload entirely in memory for runtime tests."""

    image = Image.new("RGB", (8, 8), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def build_jpeg_bytes(*, color: tuple[int, int, int]) -> bytes:
    """Generate a tiny JPEG image payload entirely in memory for runtime tests."""

    image = Image.new("RGB", (8, 8), color=color)
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def build_seeded_png_bytes(*, seed: str) -> bytes:
    """Generate deterministic but byte-distinct PNG payloads for seeded files."""

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    image = Image.new("RGB", (8, 8))
    for pixel_index in range(8 * 8):
        offset = (pixel_index * 3) % len(digest)
        image.putpixel(
            (pixel_index % 8, pixel_index // 8),
            (
                digest[offset],
                digest[(offset + 1) % len(digest)],
                digest[(offset + 2) % len(digest)],
            ),
        )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


async def seed_raw_ingest_request_for_runtime(
    session: AsyncSession,
    storage_client: FakeStorageClient,
    *,
    media_bytes: bytes,
    source_id: str,
    post_id: str,
) -> PipelineIngestRequest:
    ingest_request_id = uuid.uuid7()
    temp_key = f"pipeline/temp-originals/{ingest_request_id}/original.png"
    storage_client.objects[temp_key] = StoredObject(body=media_bytes, content_type="image/png")
    ingest_request = PipelineIngestRequest(
        id=ingest_request_id,
        source_platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        source_metadata={"view_count": 9},
        declared_filename="runtime-raw.png",
        declared_content_type="image/png",
        temp_original_object_key=temp_key,
        sha256_hex=hashlib.sha256(media_bytes).hexdigest(),
        file_size_bytes=len(media_bytes),
        status=PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
        attempt_count=0,
    )
    session.add(ingest_request)
    await session.commit()
    return ingest_request


async def _seed_transcode_pending_item(
    session: AsyncSession,
    storage_client: FakeStorageClient,
    *,
    source_id: str,
    post_id: str,
    filename: str,
    media_bytes: bytes,
    content_type: str = "image/png",
    phash_tag: str = "a",
    source_kind: IngestSourceKind = IngestSourceKind.OPERATOR_UPLOAD,
) -> tuple[ContentPipelineItemRead, ContentPipelineDispatchEvent]:
    details = _make_distinct_upload_media_details(tag=phash_tag)
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    event_id = uuid.uuid7()
    now = utcnow()
    original_object_key = f"pipeline/originals/{meme_file_id}/original.{filename.rsplit('.', 1)[-1]}"
    storage_client.objects[original_object_key] = StoredObject(body=media_bytes, content_type=content_type)
    dispatch_event = ContentPipelineDispatchEvent(
        event_id=event_id,
        event_type=ContentPipelineEventType.MEME_CREATED,
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=original_object_key,
        attempt=1,
        created_at=now,
    )
    session.add(
        Meme(
            id=meme_id,
            media_type=details.media_type,
            primary_file_id=meme_file_id,
            language=ContentLanguage.NONE,
            is_public=source_kind is IngestSourceKind.PUBLIC_CRAWLER,
        )
    )
    await session.flush()
    session.add_all(
        [
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.PENDING,
                width=details.width,
                height=details.height,
                file_size_bytes=len(media_bytes),
                mime_type=content_type,
                s3_original_key=original_object_key,
                perceptual_hash=details.perceptual_hash,
                sha256_hex=hashlib.sha256(media_bytes).hexdigest(),
                ingest_origin=IngestFileOrigin.NEW_MEME,
            ),
            MemeSource(
                file_id=meme_file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id=source_id,
                post_id=post_id,
                source_kind=source_kind,
                is_first_source=True,
                source_alive=True,
                attach_reason=SourceAttachReason.NEW_FILE,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=event_id,
                is_retryable=False,
                started_at=now,
                finished_at=now,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.PENDING,
                attempt_count=0,
                last_event_id=event_id,
                is_retryable=True,
            ),
        ]
    )
    await session.commit()
    return await PipelineItemReadService(session).get_item(meme_file_id), dispatch_event


def build_normalized_media_result(meme_file_id: uuid.UUID, *, web_video: bool = True) -> NormalizedMediaResult:
    """Create a stable normalized artifact for runtime assertions."""

    return NormalizedMediaResult(
        quality_score=0.82,
        blur_hash="L4AS~q00~q.8%MRjM{Rj00IU%MRj",
        preview_image_object_key=f"pipeline/derived/{meme_file_id}/preview.png" if web_video else None,
        preview_image_bytes=b"normalized-preview-image" if web_video else None,
        web_video_object_key=f"pipeline/derived/{meme_file_id}/web.mp4" if web_video else None,
        web_video_bytes=b"normalized-web-video" if web_video else None,
    )


def _web_video_key(result: NormalizedMediaResult) -> str:
    assert result.web_video_object_key is not None
    return result.web_video_object_key


def _web_video_bytes(result: NormalizedMediaResult) -> bytes:
    assert result.web_video_bytes is not None
    return result.web_video_bytes


def _preview_image_key(result: NormalizedMediaResult) -> str:
    assert result.preview_image_object_key is not None
    return result.preview_image_object_key


def _preview_image_bytes(result: NormalizedMediaResult) -> bytes:
    assert result.preview_image_bytes is not None
    return result.preview_image_bytes


def build_ocr_result(*, source_object_key: str) -> OCRExtractionResult:
    """Create a stable OCR result for runtime assertions."""

    return OCRExtractionResult(
        engine="paddleocr",
        fallback_engine="ocr-command",
        fallback_used=True,
        low_confidence=True,
        confidence=0.41,
        language=ContentLanguage.EN,
        extracted_text="deadline\nmonday",
        source_object_key=source_object_key,
    )


async def _fetch_item(
    session_factory: async_sessionmaker[AsyncSession],
    meme_file_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> ContentPipelineItemRead:
    async with session_factory() as session:
        service = PipelineItemReadService(session, settings=settings)
        return await service.get_item(meme_file_id)


async def _seed_ocr_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    item, _ = await _seed_transcode_pending_item(
        session,
        storage_client,
        source_id="ocr-runtime-source",
        post_id="8001",
        filename="ocr-runtime.png",
        media_bytes=build_png_bytes(color=(60, 70, 80)),
    )
    service = PipelineStageCompletionService(
        session,
        broker=broker,
    )
    normalized = build_normalized_media_result(item.meme_file_id)
    storage_client.objects[_web_video_key(normalized)] = StoredObject(
        body=_web_video_bytes(normalized),
        content_type="video/mp4",
    )
    await service.complete_transcode_stage(
        meme_file_id=item.meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    return item.meme_file_id, broker.events[-1], normalized


async def test_pipeline_runtime_declares_explicit_retry_and_dlx_topology() -> None:
    settings = Settings(pipeline_worker_prefetch_count=3)
    broker = build_pipeline_broker(settings)
    session_a_key = build_source_engagement_session_key(
        uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb"),
        "session-a",
    )
    session_b_key = build_source_engagement_session_key(
        uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acc"),
        "session-b",
    )
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(normalize_result=build_normalized_media_result(uuid.uuid7())),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="pipeline/derived/example/web.mp4")),
        source_engagement_session_keys=(session_a_key, session_b_key),
    )
    subscriber_channels = [cast("Any", subscriber).channel for subscriber in broker.subscribers]

    assert len(subscriber_channels) == 9
    assert len({id(channel) for channel in subscriber_channels}) == 9
    assert all(channel.prefetch_count == 3 for channel in subscriber_channels)
    assert all(channel.global_qos is False for channel in subscriber_channels)
    assert all(
        cast("Any", subscriber).consume_args == WorkerRole.ALL.consumer_arguments()
        for subscriber in broker.subscribers
    )

    declared_exchanges: list[str] = []
    declared_queue_arguments: dict[str, dict[str, object] | None] = {}
    recorded_queues: dict[str, RecordedQueue] = {}

    async def declare_exchange(exchange: object) -> RecordedExchange:
        exchange_name = cast("Any", exchange).name
        declared_exchanges.append(exchange_name)
        return RecordedExchange(name=exchange_name)

    async def declare_queue(queue: object) -> RecordedQueue:
        queue_name = cast("Any", queue).name
        declared_queue_arguments[queue_name] = cast("Any", queue).arguments
        recorded_queue = RecordedQueue(name=queue_name)
        recorded_queues[queue_name] = recorded_queue
        return recorded_queue

    cast("Any", broker).declare_exchange = declare_exchange
    cast("Any", broker).declare_queue = declare_queue

    await runtime.declare_topology()

    assert declared_exchanges == [
        runtime.pipeline_exchange.name,
        runtime.retry_exchange.name,
        runtime.dead_letter_exchange.name,
    ]
    media_inspect_queue_arguments = declared_queue_arguments[runtime.media_inspect_queue.name] or {}
    source_engagement_queue_arguments = {
        queue.name: declared_queue_arguments[queue.name] or {} for queue in runtime.source_engagement_capture_queues
    }
    transcode_queue_arguments = declared_queue_arguments[runtime.transcode_queue.name] or {}
    ocr_queue_arguments = declared_queue_arguments[runtime.ocr_queue.name] or {}
    media_inspect_retry_queue_arguments = declared_queue_arguments[runtime.media_inspect_retry_queue.name] or {}
    source_engagement_retry_queue_arguments = {
        queue.name: declared_queue_arguments[queue.name] or {}
        for queue in runtime.source_engagement_capture_retry_queues
    }
    transcode_retry_queue_arguments = declared_queue_arguments[runtime.transcode_retry_queue.name] or {}
    ocr_retry_queue_arguments = declared_queue_arguments[runtime.ocr_retry_queue.name] or {}

    assert (
        media_inspect_queue_arguments["x-dead-letter-routing-key"]
        == runtime.broker_settings.media_inspect_retry_request_routing_key
    )
    assert len(runtime.source_engagement_capture_queues) == 2
    assert len(runtime.source_engagement_capture_retry_queues) == 2
    assert {queue.name for queue in runtime.source_engagement_capture_queues} == {
        runtime.broker_settings.source_engagement_capture_queue_for_session(session_a_key),
        runtime.broker_settings.source_engagement_capture_queue_for_session(session_b_key),
    }
    for session_key in (session_a_key, session_b_key):
        queue_name = runtime.broker_settings.source_engagement_capture_queue_for_session(session_key)
        assert source_engagement_queue_arguments[queue_name]["x-single-active-consumer"] is True
        assert source_engagement_queue_arguments[queue_name][
            "x-dead-letter-routing-key"
        ] == runtime.broker_settings.source_engagement_capture_retry_request_routing_key_for_session(session_key)
    assert transcode_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.retry_routing_key
    assert ocr_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.ocr_retry_request_routing_key
    assert media_inspect_retry_queue_arguments["x-message-ttl"] == runtime.broker_settings.retry_backoff_milliseconds
    assert (
        media_inspect_retry_queue_arguments["x-dead-letter-routing-key"]
        == runtime.broker_settings.media_inspect_retry_routing_key
    )
    for session_key in (session_a_key, session_b_key):
        retry_queue_name = runtime.broker_settings.source_engagement_capture_retry_queue_for_session(session_key)
        assert source_engagement_retry_queue_arguments[retry_queue_name]["x-message-ttl"] == (
            runtime.broker_settings.retry_backoff_milliseconds
        )
        assert source_engagement_retry_queue_arguments[retry_queue_name]["x-dead-letter-routing-key"] == (
            runtime.broker_settings.source_engagement_capture_retry_routing_key_for_session(session_key)
        )
    assert transcode_retry_queue_arguments["x-message-ttl"] == runtime.broker_settings.retry_backoff_milliseconds
    assert (
        transcode_retry_queue_arguments["x-dead-letter-routing-key"]
        == runtime.broker_settings.transcode_retry_routing_key
    )
    assert ocr_retry_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.ocr_retry_routing_key
    assert recorded_queues[runtime.media_inspect_queue.name].bindings == [
        (runtime.pipeline_exchange.name, runtime.broker_settings.media_inspect_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.media_inspect_retry_routing_key),
    ]
    for session_key in (session_a_key, session_b_key):
        queue_name = runtime.broker_settings.source_engagement_capture_queue_for_session(session_key)
        assert recorded_queues[queue_name].bindings == [
            (
                runtime.pipeline_exchange.name,
                runtime.broker_settings.source_engagement_capture_binding_key_for_session(session_key),
            ),
            (
                runtime.pipeline_exchange.name,
                runtime.broker_settings.source_engagement_capture_retry_routing_key_for_session(session_key),
            ),
        ]
    assert recorded_queues[runtime.transcode_queue.name].bindings == [
        (runtime.pipeline_exchange.name, runtime.broker_settings.meme_created_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.stage_replay_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.transcode_retry_routing_key),
    ]
    assert recorded_queues[runtime.ocr_queue.name].bindings == [
        (runtime.pipeline_exchange.name, runtime.broker_settings.ocr_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.ocr_retry_routing_key),
    ]
    assert recorded_queues[runtime.media_inspect_retry_queue.name].bindings == [
        (runtime.retry_exchange.name, runtime.broker_settings.media_inspect_retry_request_routing_key),
    ]
    for session_key in (session_a_key, session_b_key):
        retry_queue_name = runtime.broker_settings.source_engagement_capture_retry_queue_for_session(session_key)
        assert recorded_queues[retry_queue_name].bindings == [
            (
                runtime.retry_exchange.name,
                runtime.broker_settings.source_engagement_capture_retry_request_routing_key_for_session(session_key),
            ),
        ]
    assert recorded_queues[runtime.transcode_retry_queue.name].bindings == [
        (runtime.retry_exchange.name, runtime.broker_settings.retry_routing_key),
    ]
    assert recorded_queues[runtime.ocr_retry_queue.name].bindings == [
        (runtime.retry_exchange.name, runtime.broker_settings.ocr_retry_request_routing_key),
    ]
    assert recorded_queues[runtime.dead_letter_queue.name].bindings == [
        (runtime.dead_letter_exchange.name, runtime.broker_settings.dead_letter_routing_key),
    ]


def test_pipeline_runtime_stage_registry_covers_all_downstream_stages() -> None:
    expected_stages = frozenset(ContentPipelineStage) - {ContentPipelineStage.INGEST}

    assert frozenset(RUNNABLE_DOWNSTREAM_STAGES) == expected_stages
    assert frozenset(PIPELINE_STAGE_HANDLERS) == expected_stages
    assert PIPELINE_STAGE_HANDLERS[ContentPipelineStage.TRANSCODE] is run_transcode_stage
    assert PIPELINE_STAGE_HANDLERS[ContentPipelineStage.OCR] is run_ocr_stage
    assert PIPELINE_STAGE_HANDLERS[ContentPipelineStage.EMBED] is run_embed_stage
    assert PIPELINE_STAGE_HANDLERS[ContentPipelineStage.CLASSIFY] is run_classify_stage
    assert PIPELINE_STAGE_HANDLERS[ContentPipelineStage.SYNC_QDRANT] is run_sync_qdrant_stage
    assert PIPELINE_STAGE_HANDLERS[ContentPipelineStage.SYNC_MEILI] is run_sync_meili_stage


async def test_pipeline_runtime_stage_dispatch_rejects_unsupported_stage() -> None:
    settings = Settings()
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=build_pipeline_broker(settings),
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
    )
    dispatch_event = ContentPipelineDispatchEvent.model_construct(
        event_id=uuid.uuid7(),
        event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
        meme_id=uuid.uuid7(),
        meme_file_id=uuid.uuid7(),
        stage=ContentPipelineStage.INGEST,
        source_kind=ContentSourceKind.TELEGRAM,
        original_object_key="pipeline/original/unsupported.png",
        attempt=1,
        created_at=datetime.now(UTC),
    )

    with pytest.raises(PipelineIngestError, match="Pipeline runtime cannot execute work for stage 'ingest'."):
        await runtime._run_stage_for(
            dispatch_event=dispatch_event,
            stage_context=cast("Any", object()),
            attempt=1,
        )


async def test_pipeline_runtime_media_inspect_handler_materializes_and_acks(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings()
    storage_client = FakeStorageClient()
    raw_bytes = b"runtime-media-inspect-success"
    ingest_request = await seed_raw_ingest_request_for_runtime(
        migrated_db_session,
        storage_client,
        media_bytes=raw_bytes,
        source_id="runtime-media-inspect-source",
        post_id="9001",
    )
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(
            inspect_result=UploadMediaDetails(
                media_type=ContentKind.IMAGE,
                mime_type="image/png",
                width=8,
                height=8,
                file_size_bytes=len(raw_bytes),
                perceptual_hash="9" * 16,
            )
        ),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
    )
    event_id = uuid.uuid7()
    message = FakeRabbitMessage(message_id=str(event_id))
    payload = build_media_inspect_requested_payload(
        event_id=event_id,
        ingest_request_id=ingest_request.id,
        source_platform=ingest_request.source_platform,
        sha256_hex=ingest_request.sha256_hex or "0" * 64,
        created_at=utcnow(),
    )

    await runtime.handle_media_inspect_message(payload, message)

    assert message.ack_count == 1
    assert message.reject_calls == []
    assert message.nack_calls == []
    assert ingest_request.temp_original_object_key not in storage_client.objects

    async with postgres_session_factory() as session:
        request = await session.get(PipelineIngestRequest, ingest_request.id)
        outbox_rows = (await session.execute(select(RabbitMQOutboxMessage))).scalars().all()

    assert request is not None
    assert request.status is PipelineIngestRequestStatus.MATERIALIZED
    assert request.materialized_meme_file_id is not None
    assert len(outbox_rows) == 1
    assert outbox_rows[0].event_type == ContentPipelineEventType.MEME_CREATED.value
    assert outbox_rows[0].routing_key == runtime.broker_settings.transcode_routing_key


async def test_pipeline_runtime_source_engagement_capture_handler_fetches_stats_only_and_acks(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings()
    published_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    scheduled_for = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    source_id = uuid.uuid7()
    migrated_db_session.add(Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=meme_file_id))
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.READY,
                s3_original_key=f"pipeline/originals/{meme_file_id}/original.png",
            ),
            MemeSource(
                id=source_id,
                file_id=meme_file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id="runtime-engagement-channel",
                post_id="9101",
                source_alive=True,
                published_at=published_at,
                next_engagement_check_at=scheduled_for,
                engagement_check_locked_at=scheduled_for,
                engagement_check_lock_owner="runtime-test",
            ),
        ]
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient()
    fake.pin_single_message(
        channel_id="runtime-engagement-channel",
        post_id="9101",
        message=RawTelegramMessage(
            message_id="9101",
            channel_id="runtime-engagement-channel",
            channel_username="runtime_engagement",
            channel_title="Runtime Engagement",
            published_at=published_at,
            media_type="photo",
            view_count=77,
            reactions={},
            forward_count=0,
            comment_count=0,
            comments_state=SourceEngagementCommentsState.ENABLED,
        ),
        media=b"must-not-download",
    )
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
        source_engagement_telegram_client_factory=lambda _event: fake,
    )
    telegram_session_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    session_name = "session-a"
    event = SourceEngagementCaptureRequestedEvent(
        event_id=uuid.uuid7(),
        event_type="source_engagement_capture_requested",
        meme_source_id=source_id,
        source_platform=SourcePlatform.TELEGRAM,
        source_id="runtime-engagement-channel",
        post_id="9101",
        scheduled_for=scheduled_for,
        schedule_label=SourceEngagementScheduleLabel.PLUS_1H,
        telegram_session_id=telegram_session_id,
        session_name=session_name,
        session_key=build_source_engagement_session_key(telegram_session_id, session_name),
        created_at=utcnow(),
    )
    message = FakeRabbitMessage(message_id=str(event.event_id))

    await runtime.handle_source_engagement_capture_message(event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    assert message.reject_calls == []
    assert message.nack_calls == []
    assert fake.downloaded_message_ids == []
    async with postgres_session_factory() as session:
        source = await session.get(MemeSource, source_id)
        snapshot = await session.scalar(select(MemeSourceEngagementSnapshot))

    assert source is not None
    assert source.engagement_check_locked_at is None
    assert snapshot is not None
    assert snapshot.fetch_status is SourceEngagementFetchStatus.SUCCESS
    assert snapshot.view_count == 77
    assert snapshot.reaction_count == 0


async def test_pipeline_runtime_source_engagement_flood_wait_parks_session_and_acks(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings()
    scheduled_for = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    captured_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    source_id = uuid.uuid7()
    telegram_session_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    healthy_session_id = uuid.uuid7()
    session_name = "session-a"
    migrated_db_session.add(Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=meme_file_id))
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            TelegramSession(
                id=telegram_session_id,
                name=session_name,
                display_name="Session A",
                status=TelegramSessionStatus.ACTIVE,
            ),
            TelegramSession(
                id=healthy_session_id,
                name="session-b",
                display_name="Session B",
                status=TelegramSessionStatus.ACTIVE,
            ),
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.READY,
                s3_original_key=f"pipeline/originals/{meme_file_id}/original.png",
            ),
            MemeSource(
                id=source_id,
                file_id=meme_file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id="runtime-flood-channel",
                post_id="9102",
                source_alive=True,
                published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                next_engagement_check_at=scheduled_for,
                engagement_check_locked_at=captured_at - timedelta(minutes=5),
                engagement_check_lock_owner="runtime-test",
            ),
        ]
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient(next_error=PipelineTelegramFloodWaitError("cool down", wait_seconds=90))
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
        source_engagement_telegram_client_factory=lambda _event: fake,
    )
    event = SourceEngagementCaptureRequestedEvent(
        event_id=uuid.uuid7(),
        event_type="source_engagement_capture_requested",
        meme_source_id=source_id,
        source_platform=SourcePlatform.TELEGRAM,
        source_id="runtime-flood-channel",
        post_id="9102",
        scheduled_for=scheduled_for,
        schedule_label=SourceEngagementScheduleLabel.PLUS_1H,
        telegram_session_id=telegram_session_id,
        session_name=session_name,
        session_key=build_source_engagement_session_key(telegram_session_id, session_name),
        created_at=utcnow(),
    )
    message = FakeRabbitMessage(message_id=str(event.event_id))
    monkeypatch.setattr("memexpert.services.source_engagement_capture.utcnow", lambda: captured_at)

    await runtime.handle_source_engagement_capture_message(event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    assert message.reject_calls == []
    assert message.nack_calls == []
    async with postgres_session_factory() as session:
        source = await session.get(MemeSource, source_id)
        parked_session = await session.get(TelegramSession, telegram_session_id)
        healthy_session = await session.get(TelegramSession, healthy_session_id)
        snapshots = (await session.execute(select(MemeSourceEngagementSnapshot))).scalars().all()

    assert source is not None
    assert source.engagement_check_locked_at is None
    assert parked_session is not None
    assert parked_session.status is TelegramSessionStatus.FLOOD_WAIT
    assert parked_session.flood_wait_until == captured_at + timedelta(seconds=90)
    assert parked_session.last_error_class == "PipelineTelegramFloodWaitError"
    assert healthy_session is not None
    assert healthy_session.status is TelegramSessionStatus.ACTIVE
    assert snapshots == []


async def test_pipeline_runtime_media_inspect_handler_dead_letters_malformed_payload() -> None:
    settings = Settings()
    broker = build_pipeline_broker(settings)
    durable_dead_letters: list[dict[str, object]] = []

    async def record_dead_letter(**kwargs: object) -> uuid.UUID:
        durable_dead_letters.append(dict(kwargs))
        return uuid.uuid7()

    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(inspect_result=None),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
        dead_letter_recorder=record_dead_letter,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    cast("Any", broker).publish = publish_dead_letter
    message = FakeRabbitMessage(message_id="bad-media-inspect")

    await runtime.handle_media_inspect_message({"bad": "payload"}, message)

    assert message.ack_count == 1
    assert message.reject_calls == []
    assert message.nack_calls == []
    assert durable_dead_letters == [
        {
            "session_factory": runtime.session_factory,
            "payload": json.dumps({"bad": "payload"}, sort_keys=True),
            "headers": {},
            "broker_message_id": "bad-media-inspect",
            "normalized_reason": PIPELINE_REASON_MALFORMED_EVENT,
        }
    ]
    assert dead_letters == [
        {
            "payload": json.dumps({"bad": "payload"}, sort_keys=True),
            "exchange": runtime.dead_letter_exchange.name,
            "routing_key": runtime.broker_settings.dead_letter_routing_key,
            "headers": {"x-memexpert-failure-reason": PIPELINE_REASON_MALFORMED_EVENT},
        }
    ]


async def test_pipeline_runtime_media_inspect_retries_and_dead_letters_transient_failures(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings.model_validate({"pipeline_broker_retry_max_attempts": 2})
    storage_client = FakeStorageClient()
    ingest_request = await seed_raw_ingest_request_for_runtime(
        migrated_db_session,
        storage_client,
        media_bytes=b"runtime-media-inspect-missing-temp",
        source_id="runtime-media-inspect-retry-source",
        post_id="9002",
    )
    storage_client.objects.clear()
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(inspect_result=None),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
    )
    event_id = uuid.uuid7()
    payload = build_media_inspect_requested_payload(
        event_id=event_id,
        ingest_request_id=ingest_request.id,
        source_platform=ingest_request.source_platform,
        sha256_hex=ingest_request.sha256_hex or "0" * 64,
        created_at=utcnow(),
    )
    first_message = FakeRabbitMessage(message_id=str(event_id))

    await runtime.handle_media_inspect_message(payload, first_message)

    assert first_message.ack_count == 0
    assert first_message.reject_calls == [False]
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    cast("Any", broker).publish = publish_dead_letter
    exhausted_message = FakeRabbitMessage(
        headers={
            "x-death": [
                {
                    "queue": runtime.media_inspect_retry_queue.name,
                    "reason": "expired",
                    "count": 1,
                }
            ]
        },
        message_id=str(event_id),
    )

    await runtime.handle_media_inspect_message(payload, exhausted_message)

    assert exhausted_message.ack_count == 1
    assert exhausted_message.reject_calls == []
    assert exhausted_message.nack_calls == []
    assert len(dead_letters) == 1
    dead_letter = dead_letters[0]
    assert dead_letter["exchange"] == runtime.dead_letter_exchange.name
    assert dead_letter["routing_key"] == runtime.broker_settings.dead_letter_routing_key
    assert dead_letter["headers"] == {
        "x-death": [
            {
                "queue": runtime.media_inspect_retry_queue.name,
                "reason": "expired",
                "count": 1,
            }
        ],
        "x-memexpert-failure-reason": PIPELINE_REASON_MEDIA_INSPECT_FAILED,
    }
    decoded_payload = json.loads(dead_letter["payload"])
    assert decoded_payload["event_id"] == str(event_id)
    assert decoded_payload["ingest_request_id"] == str(ingest_request.id)
    assert decoded_payload["event_type"] == "media_inspect_requested"


async def test_pipeline_runtime_forced_transcode_failure_then_replay_then_success(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    item, initial_event = await _seed_transcode_pending_item(
        migrated_db_session,
        storage_client,
        source_id="runtime-channel",
        post_id="7001",
        filename="runtime.png",
        media_bytes=build_png_bytes(color=(255, 0, 0)),
    )

    failing_settings = Settings.model_validate(
        {"pipeline_worker_fail_transcode_for_meme_file_id": str(item.meme_file_id)}
    )
    failing_runtime = build_pipeline_runtime(
        settings=failing_settings,
        broker=build_pipeline_broker(failing_settings),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=build_normalized_media_result(item.meme_file_id)),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
    )
    failure_message = FakeRabbitMessage(message_id=str(initial_event.event_id))

    await failing_runtime.handle_transcode_message(initial_event.model_dump(mode="json"), failure_message)

    failed_item = await _fetch_item(postgres_session_factory, item.meme_file_id, settings=failing_settings)
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.current_stage is ContentPipelineStage.TRANSCODE
    assert failed_item.normalized_reason == PIPELINE_REASON_FORCED_TRANSCODE_FAILURE
    assert failed_item.attempt_count == 1
    assert failure_message.reject_calls == [False]
    assert failure_message.ack_count == 0
    assert failure_message.nack_calls == []

    replay_broker = RecordingBroker()
    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            settings=failing_settings,
            broker=replay_broker,
        )
        first_replay = await replay_service.replay_item(item.meme_file_id)
        second_replay = await replay_service.replay_item(item.meme_file_id)

    assert len(replay_broker.events) == 1
    replay_event = replay_broker.events[0]
    assert first_replay.replay_event_id == replay_event.event_id
    assert first_replay.attempt == 2
    assert second_replay == first_replay

    successful_settings = Settings()
    downstream_broker = PublishingBroker()

    normalized = build_normalized_media_result(item.meme_file_id)
    successful_runtime = build_pipeline_runtime(
        settings=successful_settings,
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
    )
    success_message = FakeRabbitMessage(message_id=str(replay_event.event_id))

    await successful_runtime.handle_transcode_message(replay_event.model_dump(mode="json"), success_message)

    succeeded_item = await _fetch_item(postgres_session_factory, item.meme_file_id, settings=successful_settings)
    assert succeeded_item.current_status is ContentPipelineStageStatus.PENDING
    assert succeeded_item.current_stage is ContentPipelineStage.OCR
    assert succeeded_item.web_video_object_key == _web_video_key(normalized)
    assert succeeded_item.normalized_reason is None
    assert len(downstream_broker.publish_calls) == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == successful_runtime.broker_settings.ocr_routing_key
    assert storage_client.objects[_preview_image_key(normalized)].body == _preview_image_bytes(normalized)
    assert storage_client.objects[_web_video_key(normalized)].body == _web_video_bytes(normalized)
    assert success_message.ack_count == 1
    assert success_message.reject_calls == []
    assert success_message.nack_calls == []


async def test_pipeline_runtime_transcode_static_image_skips_web_video_upload(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    item, transcode_event = await _seed_transcode_pending_item(
        migrated_db_session,
        storage_client,
        source_id="static-runtime-source",
        post_id="static-runtime-post",
        filename="static-runtime.jpg",
        media_bytes=build_jpeg_bytes(color=(40, 50, 60)),
        content_type="image/jpeg",
    )
    normalized = build_normalized_media_result(item.meme_file_id, web_video=False)
    downstream_broker = PublishingBroker()
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
    )
    message = FakeRabbitMessage(message_id=str(transcode_event.event_id))

    await runtime.handle_transcode_message(transcode_event.model_dump(mode="json"), message)

    succeeded_item = await _fetch_item(postgres_session_factory, item.meme_file_id)
    assert succeeded_item.current_stage is ContentPipelineStage.OCR
    assert succeeded_item.current_status is ContentPipelineStageStatus.PENDING
    assert succeeded_item.web_video_object_key is None
    assert storage_client.put_calls == []
    assert message.ack_count == 1


async def test_pipeline_runtime_ocr_success_persists_fallback_result_and_dispatches_embed(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, ocr_event, normalized = await _seed_ocr_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )

    downstream_broker = PublishingBroker()

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
    )
    message = FakeRabbitMessage(message_id=str(ocr_event.event_id))

    await runtime.handle_ocr_message(ocr_event.model_dump(mode="json"), message)

    persisted_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert persisted_item.current_stage is ContentPipelineStage.EMBED
    assert persisted_item.current_status is ContentPipelineStageStatus.PENDING
    assert message.ack_count == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == runtime.broker_settings.embed_routing_key

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        persisted_ocr = await session.scalar(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == meme_file_id)
        )
        assert persisted_file is not None
        assert persisted_ocr is not None
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == persisted_file.meme_id))

    assert persisted_meme is not None
    assert persisted_ocr.fallback_used is True
    assert persisted_ocr.low_confidence is True
    assert persisted_ocr.confidence == pytest.approx(0.41)
    assert persisted_ocr.source_object_key == _web_video_key(normalized)


async def test_pipeline_runtime_duplicate_successful_event_acks_without_reexecuting_provider(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, ocr_event, normalized = await _seed_ocr_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    downstream_broker = PublishingBroker()
    ocr_processor = FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized)))
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=ocr_processor,
    )
    payload = ocr_event.model_dump(mode="json")
    first_message = FakeRabbitMessage(message_id=str(ocr_event.event_id))
    duplicate_message = FakeRabbitMessage(message_id=str(ocr_event.event_id))

    await runtime.handle_ocr_message(payload, first_message)
    await runtime.handle_ocr_message(payload, duplicate_message)

    assert first_message.ack_count == 1
    assert duplicate_message.ack_count == 1
    assert len(ocr_processor.calls) == 1
    assert len(downstream_broker.publish_calls) == 1
    async with postgres_session_factory() as session:
        attempts = (
            (
                await session.execute(
                    select(PipelineStageAttempt).where(
                        PipelineStageAttempt.meme_file_id == meme_file_id,
                        PipelineStageAttempt.stage == ContentPipelineStage.OCR,
                        PipelineStageAttempt.event_id == ocr_event.event_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(attempts) == 1
    assert attempts[0].outcome.value == "succeeded"


async def test_pipeline_runtime_ocr_static_image_uses_original_object_and_original_mime(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    item, _ = await _seed_transcode_pending_item(
        migrated_db_session,
        storage_client,
        source_id="ocr-static-source",
        post_id="ocr-static-post",
        filename="ocr-static.jpg",
        media_bytes=build_jpeg_bytes(color=(90, 40, 20)),
        content_type="image/jpeg",
    )
    service = PipelineStageCompletionService(migrated_db_session, broker=broker)
    await service.complete_transcode_stage(
        meme_file_id=item.meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_normalized_media_result(item.meme_file_id, web_video=False),
    )
    ocr_event = broker.events[-1]
    ocr_processor = FakeOCRProcessor(result=build_ocr_result(source_object_key=item.original_object_key))
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", PublishingBroker()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=build_normalized_media_result(item.meme_file_id)),
        ocr_processor=ocr_processor,
    )

    await runtime.handle_ocr_message(
        ocr_event.model_dump(mode="json"),
        FakeRabbitMessage(message_id=str(ocr_event.event_id)),
    )

    assert ocr_processor.calls[-1]["source_object_key"] == item.original_object_key
    assert ocr_processor.calls[-1]["mime_type"] == "image/jpeg"


async def test_pipeline_runtime_ocr_web_video_uses_derived_object_and_video_mime(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    _meme_file_id, ocr_event, normalized = await _seed_ocr_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    ocr_processor = FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized)))
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", PublishingBroker()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=ocr_processor,
    )

    await runtime.handle_ocr_message(
        ocr_event.model_dump(mode="json"),
        FakeRabbitMessage(message_id=str(ocr_event.event_id)),
    )

    assert ocr_processor.calls[-1]["source_object_key"] == _web_video_key(normalized)
    assert ocr_processor.calls[-1]["mime_type"] == "video/mp4"


async def test_pipeline_runtime_ocr_failure_then_replay_then_success(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, ocr_event, normalized = await _seed_ocr_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )

    failing_runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(error=OCRTimeoutError("sidecar timed out")),
    )
    failure_message = FakeRabbitMessage(message_id=str(ocr_event.event_id))

    await failing_runtime.handle_ocr_message(ocr_event.model_dump(mode="json"), failure_message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.OCR
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_OCR_TIMEOUT
    assert failure_message.reject_calls == [False]

    replay_broker = RecordingBroker()
    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            broker=replay_broker,
        )
        replay_response = await replay_service.replay_item(meme_file_id, stage=ContentPipelineStage.OCR)

    assert replay_response.stage is ContentPipelineStage.OCR
    assert len(replay_broker.events) == 1
    replay_event = replay_broker.events[0]

    downstream_broker = PublishingBroker()

    successful_runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
    )
    success_message = FakeRabbitMessage(message_id=str(replay_event.event_id))

    await successful_runtime.handle_ocr_message(replay_event.model_dump(mode="json"), success_message)

    succeeded_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert succeeded_item.current_stage is ContentPipelineStage.EMBED
    assert succeeded_item.current_status is ContentPipelineStageStatus.PENDING
    assert success_message.ack_count == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == successful_runtime.broker_settings.embed_routing_key


async def test_pipeline_runtime_dead_letters_malformed_dispatch_payloads_and_marks_journal_failure(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    item, _ = await _seed_transcode_pending_item(
        migrated_db_session,
        storage_client,
        source_id="runtime-malformed-channel",
        post_id="7002",
        filename="runtime-malformed.png",
        media_bytes=build_png_bytes(color=(0, 255, 0)),
    )

    settings = Settings()
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=build_normalized_media_result(item.meme_file_id)),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="unused")),
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            cast(
                "Any",
                {
                    "payload": payload,
                    "exchange": getattr(exchange, "name", exchange),
                    "routing_key": routing_key,
                    "headers": headers,
                },
            )
        )

    cast("Any", broker).publish = publish_dead_letter
    malformed_message = FakeRabbitMessage(message_id="malformed-message")
    malformed_payload = {
        "meme_file_id": str(item.meme_file_id),
        "stage": "transcode",
        "attempt": 1,
        "event_id": str(uuid.uuid7()),
    }

    await runtime.handle_transcode_message(malformed_payload, malformed_message)

    failed_item = await _fetch_item(postgres_session_factory, item.meme_file_id, settings=settings)
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_MALFORMED_EVENT
    assert failed_item.last_error_text == "Worker received a malformed content-pipeline dispatch payload."
    assert failed_item.attempt_count == 1
    assert malformed_message.ack_count == 1
    assert malformed_message.reject_calls == []
    assert malformed_message.nack_calls == []
    assert dead_letters == [
        {
            "payload": json.dumps(malformed_payload, sort_keys=True),
            "exchange": runtime.dead_letter_exchange.name,
            "routing_key": runtime.broker_settings.dead_letter_routing_key,
            "headers": {"x-memexpert-failure-reason": PIPELINE_REASON_MALFORMED_EVENT},
        }
    ]


def build_voyage_embedding_result(
    *,
    vector: tuple[float, ...] | None = None,
    dimensions: int = 1024,
    input_hash: str = "c" * 64,
) -> VoyageEmbeddingResult:
    """Create a deterministic embedding result for runtime tests."""

    resolved_vector = vector if vector is not None else tuple(0.005 * index for index in range(dimensions))
    return VoyageEmbeddingResult(
        model="voyage-multimodal-3.5",
        dimensions=dimensions,
        vector=resolved_vector,
        input_hash=input_hash,
    )


def build_classification_result(*, is_nsfw: bool = False, nsfw_score: float = 0.1) -> ClassificationResult:
    """Create a deterministic classification result for runtime tests."""

    return ClassificationResult(
        model="memexpert-nsfw-v1",
        is_nsfw=is_nsfw,
        nsfw_score=nsfw_score,
    )


def _make_distinct_upload_media_details(*, tag: str) -> UploadMediaDetails:
    """Build unique media metadata so repeated seed rows get distinct perceptual hashes."""

    perceptual_hash = (tag * 16)[:16]
    return UploadMediaDetails(
        media_type=ContentKind.IMAGE,
        mime_type="image/png",
        width=128,
        height=128,
        file_size_bytes=64,
        perceptual_hash=perceptual_hash,
    )


async def _seed_embed_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
    source_id: str = "embed-runtime-source",
    post_id: str = "8500",
    phash_tag: str | None = None,
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    """Create a pipeline item and drive it to the EMBED-pending state via the service."""

    initial_event_count = len(broker.events)
    item, _ = await _seed_transcode_pending_item(
        session,
        storage_client,
        source_id=source_id,
        post_id=post_id,
        filename="embed-runtime.png",
        media_bytes=build_seeded_png_bytes(
            seed=f"embed-runtime:{source_id}:{post_id}:{phash_tag or ''}",
        ),
        phash_tag=phash_tag or "a",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )
    service = PipelineStageCompletionService(
        session,
        broker=broker,
    )
    normalized = build_normalized_media_result(item.meme_file_id)
    storage_client.objects[_web_video_key(normalized)] = StoredObject(
        body=_web_video_bytes(normalized),
        content_type="video/mp4",
    )
    await service.complete_transcode_stage(
        meme_file_id=item.meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    await service.complete_ocr_stage(
        meme_file_id=item.meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_ocr_result(source_object_key=_web_video_key(normalized)),
    )
    embed_event = next(
        (
            event
            for event in reversed(broker.events[initial_event_count:])
            if event.meme_file_id == item.meme_file_id and event.stage is ContentPipelineStage.EMBED
        ),
        None,
    )
    assert embed_event is not None, "seed item must dispatch a fresh EMBED event"
    return item.meme_file_id, embed_event, normalized


async def _seed_classify_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
    source_id: str = "classify-runtime-source",
    post_id: str = "8600",
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    """Create a pipeline item and drive it to the CLASSIFY-pending state via the service."""

    meme_file_id, _, normalized = await _seed_embed_pending_item(
        session,
        storage_client=storage_client,
        broker=broker,
        source_id=source_id,
        post_id=post_id,
    )
    service = PipelineStageCompletionService(
        session,
        broker=broker,
    )
    _ = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="d" * 64),
        similarity_matches=(),
    )
    return meme_file_id, broker.events[-1], normalized


async def _seed_sync_qdrant_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
    source_id: str = "sync-qdrant-runtime-source",
    post_id: str = "8700",
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    """Create a pipeline item and drive it to the SYNC_QDRANT-pending state via the service."""

    meme_file_id, _, normalized = await _seed_classify_pending_item(
        session,
        storage_client=storage_client,
        broker=broker,
        source_id=source_id,
        post_id=post_id,
    )
    service = PipelineStageCompletionService(
        session,
        broker=broker,
    )
    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(),
    )
    sync_qdrant_event = next(event for event in broker.events if event.stage is ContentPipelineStage.SYNC_QDRANT)
    return meme_file_id, sync_qdrant_event, normalized


async def _seed_sync_meili_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    broker: RecordingBroker,
    source_id: str = "sync-meili-runtime-source",
    post_id: str = "8800",
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    """Create a pipeline item and drive it to the SYNC_MEILI-pending state via the service.

    T03 ships the dual-target fan-out so this helper just selects the Meili
    dispatch event from the classify-completion publish pair.
    """

    meme_file_id, _, normalized = await _seed_classify_pending_item(
        session,
        storage_client=storage_client,
        broker=broker,
        source_id=source_id,
        post_id=post_id,
    )
    service = PipelineStageCompletionService(
        session,
        broker=broker,
    )
    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(),
    )
    sync_meili_event = next(event for event in broker.events if event.stage is ContentPipelineStage.SYNC_MEILI)
    return meme_file_id, sync_meili_event, normalized


async def test_pipeline_runtime_declares_embed_and_classify_queues_and_retry_topology() -> None:
    settings = Settings()
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(normalize_result=build_normalized_media_result(uuid.uuid7())),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="pipeline/derived/example/web.mp4")),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )

    declared_queue_arguments: dict[str, dict[str, object] | None] = {}
    recorded_queues: dict[str, RecordedQueue] = {}

    async def declare_exchange(exchange: object) -> RecordedExchange:
        return RecordedExchange(name=cast("Any", exchange).name)

    async def declare_queue(queue: object) -> RecordedQueue:
        queue_name = cast("Any", queue).name
        declared_queue_arguments[queue_name] = cast("Any", queue).arguments
        recorded_queue = RecordedQueue(name=queue_name)
        recorded_queues[queue_name] = recorded_queue
        return recorded_queue

    cast("Any", broker).declare_exchange = declare_exchange
    cast("Any", broker).declare_queue = declare_queue

    await runtime.declare_topology()

    assert runtime.embed_queue.name in declared_queue_arguments
    assert runtime.classify_queue.name in declared_queue_arguments
    assert runtime.embed_retry_queue.name in declared_queue_arguments
    assert runtime.classify_retry_queue.name in declared_queue_arguments
    embed_queue_arguments = declared_queue_arguments[runtime.embed_queue.name] or {}
    classify_queue_arguments = declared_queue_arguments[runtime.classify_queue.name] or {}
    embed_retry_queue_arguments = declared_queue_arguments[runtime.embed_retry_queue.name] or {}
    classify_retry_queue_arguments = declared_queue_arguments[runtime.classify_retry_queue.name] or {}

    assert embed_queue_arguments["x-dead-letter-exchange"] == runtime.broker_settings.retry_exchange
    assert classify_queue_arguments["x-dead-letter-exchange"] == runtime.broker_settings.retry_exchange
    assert embed_retry_queue_arguments["x-message-ttl"] == runtime.broker_settings.retry_backoff_milliseconds
    assert classify_retry_queue_arguments["x-message-ttl"] == runtime.broker_settings.retry_backoff_milliseconds


async def test_pipeline_runtime_embed_success_persists_cache_and_dispatches_classify(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    downstream_broker = PublishingBroker()

    embedding_result = build_voyage_embedding_result(input_hash="e" * 64)
    voyage_client = FakeVoyageClient(result=embedding_result)
    qdrant_client = FakeQdrantClient()
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=voyage_client,
        qdrant_client=qdrant_client,
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    persisted_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert persisted_item.current_stage is ContentPipelineStage.CLASSIFY
    assert persisted_item.current_status is ContentPipelineStageStatus.PENDING
    assert message.ack_count == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == runtime.broker_settings.classify_routing_key
    assert voyage_client.calls == [
        {"mime_type": "image/png", "image_size": len(b"fake-preview-frame-bytes")},
    ]
    assert qdrant_client.calls[0]["meme_file_id"] == meme_file_id

    async with postgres_session_factory() as session:
        persisted_cache_row = await session.scalar(
            select(EmbeddingCache).where(EmbeddingCache.source_file_id == meme_file_id)
        )
    assert persisted_cache_row is not None
    assert persisted_cache_row.embedding == embedding_result.embedding_bytes


async def test_pipeline_runtime_embed_provider_unavailable_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(error=VoyageProviderUnavailableError("quota")),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_PROVIDER_BLOCKED
    assert message.reject_calls == [False]
    assert message.ack_count == 0


async def test_pipeline_runtime_embed_malformed_vector_marks_non_retryable_failure(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    broker = build_pipeline_broker(Settings())
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(
            error=VoyageMalformedResponseError("wrong dimensions"),
        ),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    cast("Any", broker).publish = publish_dead_letter
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_MALFORMED_VECTOR
    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(dead_letters) == 1
    assert dead_letters[0]["headers"] == {
        "x-memexpert-failure-reason": PIPELINE_REASON_EMBED_MALFORMED_VECTOR,
    }


async def test_pipeline_runtime_classify_success_emits_meme_ready_and_marks_file_ready(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, classify_event, normalized = await _seed_classify_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    downstream_broker = PublishingBroker()

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(
            result=build_classification_result(is_nsfw=True, nsfw_score=0.81),
        ),
    )
    message = FakeRabbitMessage(message_id=str(classify_event.event_id))

    await runtime.handle_classify_message(classify_event.model_dump(mode="json"), message)

    persisted_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert persisted_item.current_stage is ContentPipelineStage.SYNC_QDRANT
    assert persisted_item.current_status is ContentPipelineStageStatus.PENDING
    assert message.ack_count == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == runtime.broker_settings.sync_qdrant_routing_key

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        assert persisted_file is not None
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == persisted_file.meme_id))

    assert persisted_file.status is ContentProcessingStatus.READY
    assert persisted_meme is not None
    assert persisted_meme.is_nsfw is True
    assert persisted_meme.ocr_text == "deadline\nmonday"
    assert persisted_meme.language is ContentLanguage.EN


async def test_pipeline_runtime_classify_provider_unavailable_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, classify_event, normalized = await _seed_classify_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(
            error=ClassificationProviderUnavailableError("429 too many requests"),
        ),
    )
    message = FakeRabbitMessage(message_id=str(classify_event.event_id))

    await runtime.handle_classify_message(classify_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.CLASSIFY
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED
    assert message.reject_calls == [False]


def _select_stage_row(
    item: ContentPipelineItemRead,
    stage: ContentPipelineStage,
) -> ContentPipelineStageJournalRead:
    row = next((entry for entry in item.stages if entry.stage is stage), None)
    assert row is not None
    return row


async def test_pipeline_runtime_embed_merge_transaction_failure_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    """A merge-time failure (Qdrant outage mid-merge, row-lock conflict, or any transient
    exception during ``maybe_merge_after_embed``) must roll back the single embed
    transaction but leave the stage row replayable — the runtime must not dead-letter
    it, and ``replay_item`` must accept a replay against a clean durable state.
    """

    storage_client = FakeStorageClient()
    seed_broker = RecordingBroker()

    # Drive the older meme all the way to EMBED-succeeded so its row exists in
    # Qdrant's perspective — that gives us a plausible similarity match for the
    # newer meme to collide with.
    older_meme_file_id, _, older_normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=seed_broker,
        source_id="runtime-merge-rollback-older",
        post_id="9700",
        phash_tag="o",
    )
    async with postgres_session_factory() as stash_session:
        older_service = PipelineStageCompletionService(
            stash_session,
            broker=seed_broker,
        )
        _ = await older_service.complete_embed_stage(
            meme_file_id=older_meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            embedding_result=build_voyage_embedding_result(input_hash="2" * 64),
            similarity_matches=(),
        )
        older_file_row = await stash_session.scalar(select(MemeFile).where(MemeFile.id == older_meme_file_id))
        assert older_file_row is not None
        older_meme_id = older_file_row.meme_id

    # Baseline snapshot: files, sources, merge-log, and source engagement rows for the older meme
    # should remain unchanged after the merge rollback on the newer file.
    async with postgres_session_factory() as baseline_session:
        baseline_files = (
            (await baseline_session.execute(select(MemeFile).where(MemeFile.meme_id == older_meme_id))).scalars().all()
        )
        baseline_file_ids = {row.id for row in baseline_files}
        baseline_sources = (
            (await baseline_session.execute(select(MemeSource).where(MemeSource.file_id.in_(baseline_file_ids))))
            .scalars()
            .all()
        )
        baseline_merge_logs = (await baseline_session.execute(select(MemeMergeLog))).scalars().all()
        baseline_source_snapshot_ids = (
            (
                await baseline_session.execute(
                    select(MemeSourceEngagementSnapshot.id).where(
                        MemeSourceEngagementSnapshot.meme_source_id.in_([source.id for source in baseline_sources])
                    )
                )
            )
            .scalars()
            .all()
        )

    newer_broker = RecordingBroker()
    newer_meme_file_id, embed_event, newer_normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=newer_broker,
        source_id="runtime-merge-rollback-newer",
        post_id="9701",
        phash_tag="n",
    )

    # Force the merge transaction to fail inside _transfer_meme_files. The
    # runtime wraps this into a PipelineMergeTransactionError via the service
    # layer and the classifier must keep it replayable.
    from memexpert.services import content_merge as content_merge_module

    async def fake_transfer(_self: object, **_kwargs: object) -> tuple[uuid.UUID, ...]:
        raise RuntimeError("forced runtime merge-transfer failure")

    monkeypatch.setattr(
        content_merge_module.ContentMergeService,
        "_transfer_meme_files",
        fake_transfer,
    )

    similarity_match = QdrantSimilarityMatch(
        meme_file_id=older_meme_file_id,
        meme_id=older_meme_id,
        similarity_score=0.96,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=newer_normalized),
        ocr_processor=FakeOCRProcessor(
            result=build_ocr_result(source_object_key=_web_video_key(newer_normalized)),
        ),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result(input_hash="3" * 64)),
        qdrant_client=FakeQdrantClient(matches=(similarity_match,)),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, newer_meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_MERGE_TRANSACTION
    # Critical: merge-transaction failures must stay replayable so the runtime
    # can retry without operator intervention.
    embed_stage_row = _select_stage_row(failed_item, ContentPipelineStage.EMBED)
    assert embed_stage_row.is_retryable is True
    assert message.reject_calls == [False]
    assert message.ack_count == 0

    # The source (older) meme must remain bit-for-bit intact: same files, same
    # sources, no merge-log emitted, source engagement rows untouched.
    async with postgres_session_factory() as verify_session:
        post_files = (
            (await verify_session.execute(select(MemeFile).where(MemeFile.meme_id == older_meme_id))).scalars().all()
        )
        post_file_ids = {row.id for row in post_files}
        post_sources = (
            (await verify_session.execute(select(MemeSource).where(MemeSource.file_id.in_(post_file_ids))))
            .scalars()
            .all()
        )
        post_merge_logs = (await verify_session.execute(select(MemeMergeLog))).scalars().all()
        post_source_snapshot_ids = (
            (
                await verify_session.execute(
                    select(MemeSourceEngagementSnapshot.id).where(
                        MemeSourceEngagementSnapshot.meme_source_id.in_([source.id for source in post_sources])
                    )
                )
            )
            .scalars()
            .all()
        )
        # The embed cache row for the newer file must have been rolled back so
        # a replay starts from a clean durable state.
        post_cache_rows = (
            (
                await verify_session.execute(
                    select(EmbeddingCache).where(EmbeddingCache.source_file_id == newer_meme_file_id)
                )
            )
            .scalars()
            .all()
        )

    assert post_file_ids == baseline_file_ids
    assert {row.id for row in post_sources} == {row.id for row in baseline_sources}
    assert {row.id for row in post_merge_logs} == {row.id for row in baseline_merge_logs}
    assert set(post_source_snapshot_ids) == set(baseline_source_snapshot_ids)
    assert post_cache_rows == []

    # Undo the monkeypatch so replay_item can execute a normal transaction path.
    monkeypatch.undo()

    replay_broker = RecordingBroker()
    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            broker=replay_broker,
        )
        replay_response = await replay_service.replay_item(
            newer_meme_file_id,
            stage=ContentPipelineStage.EMBED,
        )

    assert replay_response.stage is ContentPipelineStage.EMBED
    assert len(replay_broker.events) == 1


async def test_pipeline_runtime_embed_voyage_timeout_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Voyage embed timeout must surface with a timeout-flavored reason and
    keep the stage replayable so the runtime can retry transiently."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(error=VoyageTimeoutError("took too long")),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_TIMEOUT
    embed_stage_row = _select_stage_row(failed_item, ContentPipelineStage.EMBED)
    assert embed_stage_row.is_retryable is True
    assert message.reject_calls == [False]
    assert message.ack_count == 0


async def test_pipeline_runtime_embed_qdrant_timeout_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Qdrant timeout must surface distinctly from a generic provider outage
    and keep the stage replayable so the runtime can retry transiently."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(error=QdrantTimeoutError("qdrant lookup timed out")),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT
    embed_stage_row = _select_stage_row(failed_item, ContentPipelineStage.EMBED)
    assert embed_stage_row.is_retryable is True
    assert message.reject_calls == [False]
    assert message.ack_count == 0


async def test_pipeline_runtime_embed_qdrant_malformed_response_dead_letters(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A structurally untrustworthy Qdrant response must dead-letter with a
    distinct reason and the stage must be marked non-retryable — the same
    "never replayable" behavior as VoyageMalformedResponseError."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    broker = build_pipeline_broker(Settings())
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(
            error=QdrantMalformedResponseError("response is not a sequence"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    cast("Any", broker).publish = publish_dead_letter
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED
    embed_stage_row = _select_stage_row(failed_item, ContentPipelineStage.EMBED)
    assert embed_stage_row.is_retryable is False
    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(dead_letters) == 1
    assert dead_letters[0]["headers"] == {
        "x-memexpert-failure-reason": PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED,
    }


async def test_pipeline_runtime_embed_qdrant_provider_unavailable_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: the existing provider-unavailable branch still keeps
    the stage replayable and reports the similarity-blocked reason code."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(error=QdrantProviderUnavailableError("qdrant down")),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED
    embed_stage_row = _select_stage_row(failed_item, ContentPipelineStage.EMBED)
    assert embed_stage_row.is_retryable is True
    assert message.reject_calls == [False]
    assert message.ack_count == 0


async def test_pipeline_runtime_embed_contract_violation_dead_letters_non_retryable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Contract violations at the service layer (malformed vector that passes the
    adapter but gets rejected by ``_validate_embedding_contract``) must still
    dead-letter as non-retryable. This locks in the "PipelineIngestError is
    terminal, PipelineMergeTransactionError is replayable" split."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    malformed_dimensions_result = VoyageEmbeddingResult(
        model="voyage-multimodal-3.5",
        dimensions=256,  # Settings.pipeline_voyage_output_dimensions is 1024.
        vector=tuple(0.001 * index for index in range(256)),
        input_hash="f" * 64,
    )

    broker = build_pipeline_broker(Settings())
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=_web_video_key(normalized))),
        voyage_client=FakeVoyageClient(result=malformed_dimensions_result),
        qdrant_client=FakeQdrantClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    cast("Any", broker).publish = publish_dead_letter
    message = FakeRabbitMessage(message_id=str(embed_event.event_id))

    await runtime.handle_embed_message(embed_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.EMBED
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    # The service layer converts the dimensionality rejection into a plain
    # PipelineIngestError which the runtime classifies as the generic embed
    # failure reason — the important invariant is it is terminal.
    embed_stage_row = _select_stage_row(failed_item, ContentPipelineStage.EMBED)
    assert embed_stage_row.is_retryable is False
    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(dead_letters) == 1


# --- S03 sync_qdrant consumer ----------------------------------------------


async def _load_sync_target_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
) -> MemeFileSyncTargetSnapshot | None:
    async with session_factory() as session:
        snapshot: MemeFileSyncTargetSnapshot | None = await session.scalar(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == target,
            )
        )
        return snapshot


async def test_pipeline_runtime_sync_qdrant_success_records_snapshot_and_publishes_synced_event(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: the sync_qdrant consumer upserts to Qdrant, writes the snapshot, acks."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )

    downstream_broker = PublishingBroker()

    fetched_preview = ContentPipelineSyncTargetPreview(
        target=SyncTargetKind.QDRANT,
        preview_fields={"meme_file_id": str(meme_file_id), "is_nsfw": False, "tags": []},
        preview_fetched_at=datetime.now(tz=UTC),
    )
    qdrant_sync_client = FakeQdrantSyncClient(fetch_preview=fetched_preview)
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=qdrant_sync_client,
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(qdrant_sync_client.upsert_calls) == 1
    assert qdrant_sync_client.upsert_calls[0]["meme_file_id"] == meme_file_id
    assert qdrant_sync_client.fetch_calls == [meme_file_id]

    snapshot = await _load_sync_target_snapshot(
        postgres_session_factory,
        meme_file_id,
        SyncTargetKind.QDRANT,
    )
    assert snapshot is not None
    assert snapshot.status is SyncTargetStatus.SYNCED
    assert snapshot.attempt_count == 1
    assert snapshot.last_success_at is not None
    preview_fields = snapshot.last_payload_preview.get("preview_fields")
    assert isinstance(preview_fields, dict)
    assert "meme_file_id" in preview_fields

    # Exactly one MEME_QDRANT_SYNCED dispatch event was published to the broker.
    synced_publishes = [
        call
        for call in downstream_broker.publish_calls
        if isinstance(payload := call.get("payload"), dict)
        and payload.get("event_type") == ContentPipelineEventType.MEME_QDRANT_SYNCED.value
    ]
    assert len(synced_publishes) == 1


async def test_pipeline_runtime_sync_qdrant_rebuilds_collection_aware_payload_from_current_db_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-rebuild-state",
        post_id="8701",
    )

    author = User()
    collection_owner = User()
    collaborator = User()
    migrated_db_session.add_all([author, collection_owner, collaborator])
    await migrated_db_session.flush()

    meme_file = await migrated_db_session.get(MemeFile, meme_file_id)
    assert meme_file is not None
    canonical_meme = await migrated_db_session.get(Meme, meme_file.meme_id)
    assert canonical_meme is not None

    template = MemeTemplate(slug="runtime-frog-template", name="Runtime Frog Template")
    collection = Collection(
        owner_id=collection_owner.id,
        title="Runtime shared",
        visibility=CollectionVisibility.UNLISTED,
    )
    migrated_db_session.add_all([template, collection])
    await migrated_db_session.flush()

    canonical_meme.is_public = True
    canonical_meme.tags = ["runtime", "fresh"]
    canonical_meme.like_count = 9
    canonical_meme.template_id = template.id
    source = await migrated_db_session.scalar(select(MemeSource).where(MemeSource.file_id == meme_file_id))
    assert source is not None
    source.source_kind = IngestSourceKind.USER_UPLOAD
    source.uploader_user_id = author.id
    migrated_db_session.add(
        MemeSourceEngagementSnapshot(
            meme_source_id=source.id,
            capture_reason=SourceEngagementCaptureReason.MANUAL_REFRESH,
            view_count=18,
            reactions={},
            reaction_count=0,
            comment_count=None,
            forward_count=0,
            comments_state=SourceEngagementCommentsState.UNKNOWN,
            fetch_status=SourceEngagementFetchStatus.SUCCESS,
            source_alive=True,
            raw_metrics={"test": True},
        )
    )
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=canonical_meme.id,
            slug="runtime-fresh",
            page_title="Runtime Fresh",
            meta_description="Runtime updated payload",
            alt_text="runtime fresh",
            model_id="test-model",
            prompt_version="v1",
        )
    )
    migrated_db_session.add_all(
        [
            CollectionMeme(
                collection_id=collection.id,
                meme_id=canonical_meme.id,
                added_by_user_id=collection_owner.id,
            ),
            CollectionMember(
                collection_id=collection.id,
                user_id=collaborator.id,
                role=CollectionMembershipRole.VIEWER,
            ),
        ]
    )
    await migrated_db_session.commit()

    downstream_broker = PublishingBroker()

    settings = Settings()
    qdrant_sync_client = FakeQdrantSyncClient()
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=qdrant_sync_client,
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    assert len(qdrant_sync_client.upsert_calls) == 1
    qdrant_popularity_score = cast("float", qdrant_sync_client.upsert_calls[0]["popularity_score"])
    assert qdrant_popularity_score > 0.0
    assert qdrant_sync_client.upsert_calls[0] == {
        "meme_file_id": meme_file_id,
        "meme_id": canonical_meme.id,
        "search_index_algorithm_version": SEARCH_INDEX_ALGORITHM_VERSION,
        "is_public": True,
        "uploader_user_ids": [str(author.id)],
        "media_type": ContentKind.IMAGE.value,
        "language": ContentLanguage.EN.value,
        "tags": ["runtime", "fresh"],
        "is_nsfw": False,
        "template_slug": "runtime-frog-template",
        "popularity_score": qdrant_popularity_score,
        "like_count": 9,
        "collection_ids": [str(collection.id)],
        "public_collection_ids": [],
        "unlisted_collection_ids": [str(collection.id)],
        "private_collection_ids": [],
        "shared_collection_ids": [str(collection.id)],
        "collection_owner_user_ids": [str(collection_owner.id)],
        "collection_member_user_ids": [str(collaborator.id)],
        "vector_len": settings.pipeline_voyage_output_dimensions,
    }


async def test_pipeline_runtime_sync_qdrant_provider_unavailable_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transient provider outage surfaces the provider-blocked reason and stays replayable."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-unavailable",
        post_id="8710",
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(
            upsert_error=QdrantSyncProviderUnavailableError("qdrant down"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.current_stage is ContentPipelineStage.SYNC_QDRANT
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.normalized_reason == PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED
    stage_row = _select_stage_row(failed_item, ContentPipelineStage.SYNC_QDRANT)
    assert stage_row.is_retryable is True
    assert message.reject_calls == [False]

    snapshot = await _load_sync_target_snapshot(
        postgres_session_factory,
        meme_file_id,
        SyncTargetKind.QDRANT,
    )
    assert snapshot is not None
    assert snapshot.status is SyncTargetStatus.FAILED
    assert snapshot.normalized_reason == PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED


async def test_pipeline_runtime_sync_qdrant_timeout_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-timeout",
        post_id="8711",
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(
            upsert_error=QdrantSyncTimeoutError("took too long"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.normalized_reason == PIPELINE_REASON_SYNC_QDRANT_TIMEOUT
    stage_row = _select_stage_row(failed_item, ContentPipelineStage.SYNC_QDRANT)
    assert stage_row.is_retryable is True
    assert message.reject_calls == [False]


async def test_pipeline_runtime_sync_qdrant_conflict_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-conflict",
        post_id="8712",
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(
            upsert_error=QdrantSyncConflictError("409 conflict"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.normalized_reason == PIPELINE_REASON_SYNC_QDRANT_CONFLICT
    stage_row = _select_stage_row(failed_item, ContentPipelineStage.SYNC_QDRANT)
    assert stage_row.is_retryable is True
    assert message.reject_calls == [False]


async def test_pipeline_runtime_sync_qdrant_malformed_response_dead_letters(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-malformed",
        post_id="8713",
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    broker = build_pipeline_broker(Settings())
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(
            upsert_error=QdrantSyncMalformedResponseError("bad schema"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    cast("Any", broker).publish = publish_dead_letter
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id)
    assert failed_item.normalized_reason == PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD
    stage_row = _select_stage_row(failed_item, ContentPipelineStage.SYNC_QDRANT)
    assert stage_row.is_retryable is False
    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(dead_letters) == 1
    assert dead_letters[0]["headers"] == {
        "x-memexpert-failure-reason": PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD,
    }


async def test_pipeline_runtime_sync_qdrant_forced_failure_knob_marks_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-forced",
        post_id="8714",
    )
    failing_settings = Settings.model_validate({"pipeline_worker_fail_sync_qdrant_for_meme_file_id": str(meme_file_id)})
    runtime = build_pipeline_runtime(
        settings=failing_settings,
        broker=build_pipeline_broker(failing_settings),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    failed_item = await _fetch_item(postgres_session_factory, meme_file_id, settings=failing_settings)
    assert failed_item.normalized_reason == PIPELINE_REASON_FORCED_SYNC_QDRANT_FAILURE
    stage_row = _select_stage_row(failed_item, ContentPipelineStage.SYNC_QDRANT)
    assert stage_row.is_retryable is True
    assert message.reject_calls == [False]


async def test_pipeline_runtime_sync_qdrant_best_effort_preview_fetch_failure_still_succeeds(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview fetch failures degrade to an empty preview, never failing the stage."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_qdrant_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-qdrant-preview-fail",
        post_id="8715",
    )

    downstream_broker = PublishingBroker()
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(
            fetch_error=QdrantSyncTimeoutError("preview retrieve timed out"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_qdrant_message(sync_event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    snapshot = await _load_sync_target_snapshot(
        postgres_session_factory,
        meme_file_id,
        SyncTargetKind.QDRANT,
    )
    assert snapshot is not None
    assert snapshot.status is SyncTargetStatus.SYNCED
    # The preview is intentionally empty because the best-effort retrieve failed.
    assert snapshot.last_payload_preview == {}


# --- T03: sync_meili runtime tests ------------------------------------------


async def test_pipeline_runtime_sync_meili_success_records_snapshot_and_publishes_synced_event(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: the sync_meili consumer upserts to Meilisearch, writes snapshot, acks."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
    )

    downstream_broker = PublishingBroker()

    fetched_preview = ContentPipelineSyncTargetPreview(
        target=SyncTargetKind.MEILISEARCH,
        preview_fields={"id": meme_file_id.hex, "is_nsfw": False, "tags": []},
        preview_fetched_at=datetime.now(tz=UTC),
    )
    meili_sync_client = FakeMeilisearchSyncClient(fetch_preview=fetched_preview)
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=meili_sync_client,
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(meili_sync_client.upsert_calls) == 1
    assert meili_sync_client.upsert_calls[0]["id"] == meme_file_id.hex
    assert meili_sync_client.fetch_calls == [meme_file_id]

    snapshot = await _load_sync_target_snapshot(
        postgres_session_factory,
        meme_file_id,
        SyncTargetKind.MEILISEARCH,
    )
    assert snapshot is not None
    assert snapshot.status is SyncTargetStatus.SYNCED
    assert snapshot.attempt_count == 1
    assert snapshot.last_success_at is not None

    synced_publishes = [
        call
        for call in downstream_broker.publish_calls
        if isinstance(payload := call.get("payload"), dict)
        and payload.get("event_type") == ContentPipelineEventType.MEME_MEILI_SYNCED.value
    ]
    assert len(synced_publishes) == 1


async def test_pipeline_runtime_sync_meili_rebuilds_collection_aware_document_from_current_db_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-meili-rebuild-state",
        post_id="8801",
    )

    author = User()
    collection_owner = User()
    collaborator = User()
    migrated_db_session.add_all([author, collection_owner, collaborator])
    await migrated_db_session.flush()

    meme_file = await migrated_db_session.get(MemeFile, meme_file_id)
    assert meme_file is not None
    canonical_meme = await migrated_db_session.get(Meme, meme_file.meme_id)
    assert canonical_meme is not None

    template = MemeTemplate(slug="runtime-meili-template", name="Runtime Meili Template")
    collection = Collection(
        owner_id=collection_owner.id,
        title="Runtime public",
        visibility=CollectionVisibility.PUBLIC,
    )
    migrated_db_session.add_all([template, collection])
    await migrated_db_session.flush()

    canonical_meme.is_public = False
    canonical_meme.tags = ["meili", "updated"]
    canonical_meme.like_count = 5
    canonical_meme.template_id = template.id
    source = await migrated_db_session.scalar(select(MemeSource).where(MemeSource.file_id == meme_file_id))
    assert source is not None
    migrated_db_session.add(
        MemeSourceEngagementSnapshot(
            meme_source_id=source.id,
            capture_reason=SourceEngagementCaptureReason.MANUAL_REFRESH,
            view_count=33,
            reactions={},
            reaction_count=0,
            comment_count=None,
            forward_count=0,
            comments_state=SourceEngagementCommentsState.UNKNOWN,
            fetch_status=SourceEngagementFetchStatus.SUCCESS,
            source_alive=True,
            raw_metrics={"test": True},
        )
    )
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=canonical_meme.id,
            slug="runtime-meili",
            page_title="Runtime Meili",
            meta_description="Runtime meili payload",
            alt_text="runtime meili",
            model_id="test-model",
            prompt_version="v1",
        )
    )
    migrated_db_session.add_all(
        [
            CollectionMeme(
                collection_id=collection.id,
                meme_id=canonical_meme.id,
                added_by_user_id=collection_owner.id,
            ),
            CollectionMember(
                collection_id=collection.id,
                user_id=collaborator.id,
                role=CollectionMembershipRole.EDITOR,
            ),
        ]
    )
    await migrated_db_session.commit()

    downstream_broker = PublishingBroker()

    settings = Settings()
    meili_sync_client = FakeMeilisearchSyncClient()
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=cast("Any", downstream_broker),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=meili_sync_client,
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    assert message.ack_count == 1
    assert len(meili_sync_client.upsert_calls) == 1
    meili_popularity_score = cast("float", meili_sync_client.upsert_calls[0]["popularity_score"])
    assert meili_popularity_score > 0.0
    assert meili_sync_client.upsert_calls[0] == {
        "id": meme_file_id.hex,
        "meme_id": str(canonical_meme.id),
        "meme_file_id": str(meme_file_id),
        "search_index_algorithm_version": SEARCH_INDEX_ALGORITHM_VERSION,
        "is_public": False,
        "media_type": ContentKind.IMAGE.value,
        "tags": ["meili", "updated"],
        "is_nsfw": False,
        "language": ContentLanguage.EN.value,
        "template_slug": "runtime-meili-template",
        "popularity_score": meili_popularity_score,
        "like_count": 5,
        "collection_ids": [str(collection.id)],
        "public_collection_ids": [str(collection.id)],
        "unlisted_collection_ids": [],
        "private_collection_ids": [],
        "shared_collection_ids": [str(collection.id)],
        "collection_owner_user_ids": [str(collection_owner.id)],
        "collection_member_user_ids": [str(collaborator.id)],
    }


async def _load_sync_meili_stage_row(
    session_factory: async_sessionmaker[AsyncSession],
    meme_file_id: uuid.UUID,
) -> PipelineStageJournal:
    """Fetch the durable sync_meili stage-journal row directly.

    The ``_fetch_item`` helper returns the ``ContentPipelineItemRead`` view
    whose ``current_stage``/``normalized_reason`` are chosen by the active
    stage resolver. With the T03 dual fan-out, classify creates BOTH
    sync_qdrant and sync_meili rows, so the resolver may pick sync_qdrant
    (PENDING) over sync_meili (FAILED) and the item-level view loses the
    Meili failure details. Tests that need the Meili row specifically must
    read it directly from the journal.
    """

    async with session_factory() as session:
        stage_row = await session.scalar(
            select(PipelineStageJournal).where(
                PipelineStageJournal.meme_file_id == meme_file_id,
                PipelineStageJournal.stage == ContentPipelineStage.SYNC_MEILI,
            )
        )
    assert stage_row is not None
    return stage_row


async def test_pipeline_runtime_sync_meili_provider_unavailable_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-meili-unavailable",
        post_id="8810",
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=FakeMeilisearchSyncClient(
            upsert_error=MeilisearchSyncProviderUnavailableError("meili down"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    stage_row = await _load_sync_meili_stage_row(postgres_session_factory, meme_file_id)
    assert stage_row.status is ContentPipelineStageStatus.FAILED
    assert stage_row.normalized_reason == PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED
    assert stage_row.is_retryable is True
    assert message.reject_calls == [False]

    snapshot = await _load_sync_target_snapshot(
        postgres_session_factory,
        meme_file_id,
        SyncTargetKind.MEILISEARCH,
    )
    assert snapshot is not None
    assert snapshot.status is SyncTargetStatus.FAILED
    assert snapshot.normalized_reason == PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED


async def test_pipeline_runtime_sync_meili_timeout_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-meili-timeout",
        post_id="8811",
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=FakeMeilisearchSyncClient(
            upsert_error=MeilisearchSyncTimeoutError("deadline"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    stage_row = await _load_sync_meili_stage_row(postgres_session_factory, meme_file_id)
    assert stage_row.normalized_reason == PIPELINE_REASON_SYNC_MEILI_TIMEOUT
    assert stage_row.is_retryable is True
    assert message.reject_calls == [False]


async def test_pipeline_runtime_sync_meili_conflict_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-meili-conflict",
        post_id="8812",
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=FakeMeilisearchSyncClient(
            upsert_error=MeilisearchSyncConflictError("409 conflict"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    stage_row = await _load_sync_meili_stage_row(postgres_session_factory, meme_file_id)
    assert stage_row.normalized_reason == PIPELINE_REASON_SYNC_MEILI_CONFLICT
    assert stage_row.is_retryable is True


async def test_pipeline_runtime_sync_meili_malformed_response_dead_letters(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-meili-malformed",
        post_id="8813",
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        _queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        dead_letters.append(
            {
                "payload": payload,
                "exchange": getattr(exchange, "name", exchange),
                "routing_key": routing_key,
                "headers": headers,
            }
        )

    broker = build_pipeline_broker(Settings())
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=broker,
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=FakeMeilisearchSyncClient(
            upsert_error=MeilisearchSyncMalformedResponseError("bad schema"),
        ),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    cast("Any", broker).publish = publish_dead_letter
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    stage_row = await _load_sync_meili_stage_row(postgres_session_factory, meme_file_id)
    assert stage_row.normalized_reason == PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD
    assert stage_row.is_retryable is False
    assert message.ack_count == 1
    assert message.reject_calls == []
    assert len(dead_letters) == 1
    assert dead_letters[0]["headers"] == {
        "x-memexpert-failure-reason": PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD,
    }


async def test_pipeline_runtime_sync_meili_forced_failure_knob_marks_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, sync_event, _ = await _seed_sync_meili_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="sync-meili-forced",
        post_id="8814",
    )
    failing_settings = Settings.model_validate({"pipeline_worker_fail_sync_meili_for_meme_file_id": str(meme_file_id)})
    runtime = build_pipeline_runtime(
        settings=failing_settings,
        broker=build_pipeline_broker(failing_settings),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(),
        ocr_processor=FakeOCRProcessor(),
        voyage_client=FakeVoyageClient(result=build_voyage_embedding_result()),
        qdrant_client=FakeQdrantClient(),
        qdrant_sync_client=FakeQdrantSyncClient(),
        meilisearch_sync_client=FakeMeilisearchSyncClient(),
        classification_client=FakeClassificationClient(result=build_classification_result()),
    )
    message = FakeRabbitMessage(message_id=str(sync_event.event_id))

    await runtime.handle_sync_meili_message(sync_event.model_dump(mode="json"), message)

    stage_row = await _load_sync_meili_stage_row(postgres_session_factory, meme_file_id)
    assert stage_row.normalized_reason == PIPELINE_REASON_FORCED_SYNC_MEILI_FAILURE
    assert stage_row.is_retryable is True


# --- T03: classify fan-out atomicity + dual-target outcomes ------------------


async def test_classify_completion_fans_out_both_sync_stages_and_publishes_both_events(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Classify success creates BOTH sync stage rows AND publishes BOTH dispatches."""

    storage_client = FakeStorageClient()
    broker = RecordingBroker()
    meme_file_id, _, _ = await _seed_classify_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=broker,
        source_id="classify-fanout-happy",
        post_id="9000",
    )
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )

    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(),
    )

    # Both downstream stage rows exist after the atomic commit.
    async with postgres_session_factory() as session:
        stage_values = {
            row.stage
            for row in (
                await session.execute(
                    select(PipelineStageJournal).where(
                        PipelineStageJournal.meme_file_id == meme_file_id,
                    )
                )
            )
            .scalars()
            .all()
        }
    assert ContentPipelineStage.SYNC_QDRANT in stage_values
    assert ContentPipelineStage.SYNC_MEILI in stage_values

    # Both MEME_READY dispatches were published exactly once.
    meme_ready_stages = {
        event.stage for event in broker.events if event.event_type is ContentPipelineEventType.MEME_READY
    }
    assert meme_ready_stages == {
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    }


async def test_classify_completion_fan_out_publish_failure_commits_stage_rows_and_retryable_outbox(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Immediate Meilisearch publish failure must not roll back committed fan-out state."""

    storage_client = FakeStorageClient()
    setup_broker = RecordingBroker()
    meme_file_id, _, _ = await _seed_classify_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        broker=setup_broker,
        source_id="classify-fanout-rollback",
        post_id="9001",
    )

    settings = Settings()
    broker_settings = get_pipeline_broker_settings(settings)
    downstream_broker = PublishingBroker(fail_on_routing_keys={broker_settings.sync_meili_routing_key})

    fail_service = PipelineStageCompletionService(
        migrated_db_session,
        settings=settings,
        broker=cast("Any", downstream_broker),
    )

    await fail_service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(),
    )

    async with postgres_session_factory() as session:
        stage_rows = {
            row.stage: row
            for row in (
                await session.execute(
                    select(PipelineStageJournal).where(
                        PipelineStageJournal.meme_file_id == meme_file_id,
                        PipelineStageJournal.stage.in_(
                            (
                                ContentPipelineStage.CLASSIFY,
                                ContentPipelineStage.SYNC_QDRANT,
                                ContentPipelineStage.SYNC_MEILI,
                            )
                        ),
                    )
                )
            )
            .scalars()
            .all()
        }
        outbox_rows = (
            (
                await session.execute(
                    select(RabbitMQOutboxMessage).where(
                        RabbitMQOutboxMessage.aggregate_id == str(meme_file_id),
                        RabbitMQOutboxMessage.event_type == ContentPipelineEventType.MEME_READY.value,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert stage_rows[ContentPipelineStage.CLASSIFY].status is ContentPipelineStageStatus.SUCCEEDED
    assert stage_rows[ContentPipelineStage.SYNC_QDRANT].status is ContentPipelineStageStatus.PENDING
    assert stage_rows[ContentPipelineStage.SYNC_MEILI].status is ContentPipelineStageStatus.PENDING

    outbox_by_routing_key = {row.routing_key: row for row in outbox_rows}
    assert set(outbox_by_routing_key) == {
        broker_settings.sync_qdrant_routing_key,
        broker_settings.sync_meili_routing_key,
    }
    qdrant_outbox = outbox_by_routing_key[broker_settings.sync_qdrant_routing_key]
    assert qdrant_outbox.status is RabbitMQOutboxMessageStatus.PUBLISHED
    meili_outbox = outbox_by_routing_key[broker_settings.sync_meili_routing_key]
    assert meili_outbox.status is RabbitMQOutboxMessageStatus.FAILED
    assert meili_outbox.next_retry_at is not None
    assert "simulated publish failure" in (meili_outbox.last_error_text or "")
    assert [call["routing_key"] for call in downstream_broker.publish_calls] == [
        broker_settings.sync_qdrant_routing_key,
        broker_settings.sync_meili_routing_key,
    ]
