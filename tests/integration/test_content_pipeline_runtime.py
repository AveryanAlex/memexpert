"""Integration tests for the RabbitMQ-backed transcode and OCR runtime."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import pytest
from PIL import Image
from sqlalchemy import select

import memexpert.services.content_pipeline as content_pipeline_module
from memexpert.core.broker import build_pipeline_broker
from memexpert.core.classification import (
    ClassificationProviderUnavailableError,
    ClassificationResult,
)
from memexpert.core.config import Settings
from memexpert.core.media import (
    NormalizedMediaResult,
    PipelineMediaProcessorProtocol,
    UploadMediaDetails,
)
from memexpert.core.ocr import OCRExtractionResult, OCRTimeoutError
from memexpert.core.qdrant import (
    QdrantMalformedResponseError,
    QdrantProviderUnavailableError,
    QdrantSimilarityMatch,
    QdrantTimeoutError,
)
from memexpert.core.voyage import (
    VoyageEmbeddingResult,
    VoyageMalformedResponseError,
    VoyageProviderUnavailableError,
    VoyageTimeoutError,
)
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeMergeLog,
    MemePopularitySnapshot,
    MemeSource,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    SourcePlatform,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineItemRead,
    ContentPipelineStageJournalRead,
    ContentPipelineUploadMetadata,
)
from memexpert.services import ContentPipelineService
from memexpert.workers.pipeline_runtime import (
    PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED,
    PIPELINE_REASON_EMBED_MALFORMED_VECTOR,
    PIPELINE_REASON_EMBED_MERGE_TRANSACTION,
    PIPELINE_REASON_EMBED_PROVIDER_BLOCKED,
    PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED,
    PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED,
    PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT,
    PIPELINE_REASON_EMBED_TIMEOUT,
    PIPELINE_REASON_FORCED_TRANSCODE_FAILURE,
    PIPELINE_REASON_MALFORMED_EVENT,
    PIPELINE_REASON_OCR_TIMEOUT,
    build_pipeline_runtime,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
class RecordingPublisher:
    """Async publisher double that captures upload and replay dispatch events."""

    events: list[ContentPipelineDispatchEvent] = field(default_factory=list)

    async def __call__(self, event: ContentPipelineDispatchEvent) -> None:
        self.events.append(event)


@dataclass(slots=True)
class PublishingBroker:
    """Small broker double used to observe downstream stage dispatches."""

    publish_calls: list[dict[str, object]] = field(default_factory=list)

    async def publish(self, payload: object, **kwargs: object) -> None:
        self.publish_calls.append({"payload": payload, **kwargs})


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

    async def extract_text(
        self,
        *,
        filename: str,
        mime_type: str,
        media_bytes: bytes,
        source_object_key: str,
    ) -> OCRExtractionResult:
        _ = (filename, mime_type, media_bytes, source_object_key)
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
        limit: int | None = None,
    ) -> tuple[QdrantSimilarityMatch, ...]:
        self.calls.append({"vector_len": len(vector), "meme_file_id": current_meme_file_id, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.matches


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


def build_normalized_media_result(meme_file_id: uuid.UUID) -> NormalizedMediaResult:
    """Create a stable normalized transcode artifact for runtime assertions."""

    return NormalizedMediaResult(
        mime_type="video/mp4",
        width=720,
        height=720,
        file_size_bytes=4096,
        quality_score=0.82,
        blur_hash="L4AS~q00~q.8%MRjM{Rj00IU%MRj",
        web_video_object_key=f"pipeline/derived/{meme_file_id}/web.mp4",
        web_video_bytes=b"normalized-web-video",
    )


def build_ocr_result(*, source_object_key: str) -> OCRExtractionResult:
    """Create a stable OCR result for runtime assertions."""

    return OCRExtractionResult(
        engine="paddleocr",
        fallback_engine="qwen2.5-vl-2b",
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
        service = ContentPipelineService(session, settings=settings)
        return await service.get_item(meme_file_id)


async def _seed_ocr_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    publisher: RecordingPublisher,
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    service = ContentPipelineService(
        session,
        storage_client=storage_client,
        publisher=publisher,
    )
    item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="ocr-runtime-source",
            post_id="8001",
            views=11,
        ),
        filename="ocr-runtime.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(60, 70, 80)),
    )
    normalized = build_normalized_media_result(item.meme_file_id)
    storage_client.objects[normalized.web_video_object_key] = StoredObject(
        body=normalized.web_video_bytes,
        content_type=normalized.mime_type,
    )
    await service.complete_transcode_stage(
        meme_file_id=item.meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    return item.meme_file_id, publisher.events[-1], normalized


async def test_pipeline_runtime_declares_explicit_retry_and_dlx_topology() -> None:
    settings = Settings()
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        broker=broker,
        storage_client=FakeStorageClient(),
        media_processor=FakeMediaProcessor(normalize_result=build_normalized_media_result(uuid.uuid7())),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key="pipeline/derived/example/web.mp4")),
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
    transcode_queue_arguments = declared_queue_arguments[runtime.transcode_queue.name] or {}
    ocr_queue_arguments = declared_queue_arguments[runtime.ocr_queue.name] or {}
    transcode_retry_queue_arguments = declared_queue_arguments[runtime.transcode_retry_queue.name] or {}
    ocr_retry_queue_arguments = declared_queue_arguments[runtime.ocr_retry_queue.name] or {}

    assert transcode_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.retry_routing_key
    assert ocr_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.ocr_retry_request_routing_key
    assert transcode_retry_queue_arguments["x-message-ttl"] == runtime.broker_settings.retry_backoff_milliseconds
    assert (
        transcode_retry_queue_arguments["x-dead-letter-routing-key"]
        == runtime.broker_settings.transcode_retry_routing_key
    )
    assert ocr_retry_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.ocr_retry_routing_key
    assert recorded_queues[runtime.transcode_queue.name].bindings == [
        (runtime.pipeline_exchange.name, runtime.broker_settings.meme_created_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.stage_replay_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.transcode_retry_routing_key),
    ]
    assert recorded_queues[runtime.ocr_queue.name].bindings == [
        (runtime.pipeline_exchange.name, runtime.broker_settings.ocr_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.ocr_retry_routing_key),
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


async def test_pipeline_runtime_forced_transcode_failure_then_replay_then_success(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    initial_publisher = RecordingPublisher()
    setup_service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=initial_publisher,
    )
    item = await setup_service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="runtime-channel",
            post_id="7001",
            views=5,
        ),
        filename="runtime.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(255, 0, 0)),
    )
    initial_event = initial_publisher.events[0]

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

    replay_publisher = RecordingPublisher()
    async with postgres_session_factory() as replay_session:
        replay_service = ContentPipelineService(
            replay_session,
            settings=failing_settings,
            storage_client=storage_client,
            publisher=replay_publisher,
        )
        first_replay = await replay_service.replay_item(item.meme_file_id)
        second_replay = await replay_service.replay_item(item.meme_file_id)

    assert len(replay_publisher.events) == 1
    replay_event = replay_publisher.events[0]
    assert first_replay.replay_event_id == replay_event.event_id
    assert first_replay.attempt == 2
    assert second_replay == first_replay

    successful_settings = Settings()
    downstream_broker = PublishingBroker()

    async def fake_ensure_pipeline_broker_started(*_: object, **__: object) -> object:
        return downstream_broker

    monkeypatch.setattr(content_pipeline_module, "ensure_pipeline_broker_started", fake_ensure_pipeline_broker_started)

    normalized = build_normalized_media_result(item.meme_file_id)
    successful_runtime = build_pipeline_runtime(
        settings=successful_settings,
        broker=build_pipeline_broker(successful_settings),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
    )
    success_message = FakeRabbitMessage(message_id=str(replay_event.event_id))

    await successful_runtime.handle_transcode_message(replay_event.model_dump(mode="json"), success_message)

    succeeded_item = await _fetch_item(postgres_session_factory, item.meme_file_id, settings=successful_settings)
    assert succeeded_item.current_status is ContentPipelineStageStatus.PENDING
    assert succeeded_item.current_stage is ContentPipelineStage.OCR
    assert succeeded_item.web_video_object_key == normalized.web_video_object_key
    assert succeeded_item.normalized_reason is None
    assert len(downstream_broker.publish_calls) == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == successful_runtime.broker_settings.ocr_routing_key
    assert storage_client.objects[normalized.web_video_object_key].body == normalized.web_video_bytes
    assert success_message.ack_count == 1
    assert success_message.reject_calls == []
    assert success_message.nack_calls == []


async def test_pipeline_runtime_ocr_success_persists_fallback_result_and_dispatches_embed(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    meme_file_id, ocr_event, normalized = await _seed_ocr_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    downstream_broker = PublishingBroker()

    async def fake_ensure_pipeline_broker_started(*_: object, **__: object) -> object:
        return downstream_broker

    monkeypatch.setattr(content_pipeline_module, "ensure_pipeline_broker_started", fake_ensure_pipeline_broker_started)

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    assert persisted_ocr.source_object_key == normalized.web_video_object_key


async def test_pipeline_runtime_ocr_failure_then_replay_then_success(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    meme_file_id, ocr_event, normalized = await _seed_ocr_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
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

    replay_publisher = RecordingPublisher()
    async with postgres_session_factory() as replay_session:
        replay_service = ContentPipelineService(
            replay_session,
            storage_client=storage_client,
            publisher=replay_publisher,
        )
        replay_response = await replay_service.replay_item(meme_file_id, stage=ContentPipelineStage.OCR)

    assert replay_response.stage is ContentPipelineStage.OCR
    assert len(replay_publisher.events) == 1
    replay_event = replay_publisher.events[0]

    downstream_broker = PublishingBroker()

    async def fake_ensure_pipeline_broker_started(*_: object, **__: object) -> object:
        return downstream_broker

    monkeypatch.setattr(content_pipeline_module, "ensure_pipeline_broker_started", fake_ensure_pipeline_broker_started)

    successful_runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    setup_publisher = RecordingPublisher()
    setup_service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=setup_publisher,
    )
    item = await setup_service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="runtime-malformed-channel",
            post_id="7002",
        ),
        filename="runtime-malformed.png",
        content_type="image/png",
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
        queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        _ = queue
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
    """Build unique upload metadata so repeated ingests get distinct perceptual hashes."""

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
    publisher: RecordingPublisher,
    source_id: str = "embed-runtime-source",
    post_id: str = "8500",
    phash_tag: str | None = None,
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    """Create a pipeline item and drive it to the EMBED-pending state via the service."""

    media_processor: PipelineMediaProcessorProtocol | None = None
    if phash_tag is not None:
        media_processor = FakeMediaProcessor(
            inspect_result=_make_distinct_upload_media_details(tag=phash_tag),
        )
    service = ContentPipelineService(
        session,
        storage_client=storage_client,
        publisher=publisher,
        media_processor=media_processor,
    )
    item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id=source_id,
            post_id=post_id,
            views=13,
        ),
        filename="embed-runtime.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(30, 40, 50)),
    )
    normalized = build_normalized_media_result(item.meme_file_id)
    storage_client.objects[normalized.web_video_object_key] = StoredObject(
        body=normalized.web_video_bytes,
        content_type=normalized.mime_type,
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
        result=build_ocr_result(source_object_key=normalized.web_video_object_key),
    )
    return item.meme_file_id, publisher.events[-1], normalized


async def _seed_classify_pending_item(
    session: AsyncSession,
    *,
    storage_client: FakeStorageClient,
    publisher: RecordingPublisher,
    source_id: str = "classify-runtime-source",
    post_id: str = "8600",
) -> tuple[uuid.UUID, ContentPipelineDispatchEvent, NormalizedMediaResult]:
    """Create a pipeline item and drive it to the CLASSIFY-pending state via the service."""

    meme_file_id, _, normalized = await _seed_embed_pending_item(
        session,
        storage_client=storage_client,
        publisher=publisher,
        source_id=source_id,
        post_id=post_id,
    )
    service = ContentPipelineService(
        session,
        storage_client=storage_client,
        publisher=publisher,
    )
    _ = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="d" * 64),
        similarity_matches=(),
    )
    return meme_file_id, publisher.events[-1], normalized


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
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    downstream_broker = PublishingBroker()

    async def fake_ensure_pipeline_broker_started(*_: object, **__: object) -> object:
        return downstream_broker

    monkeypatch.setattr(content_pipeline_module, "ensure_pipeline_broker_started", fake_ensure_pipeline_broker_started)

    embedding_result = build_voyage_embedding_result(input_hash="e" * 64)
    voyage_client = FakeVoyageClient(result=embedding_result)
    qdrant_client = FakeQdrantClient()
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        _ = queue
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
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    meme_file_id, classify_event, normalized = await _seed_classify_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    downstream_broker = PublishingBroker()

    async def fake_ensure_pipeline_broker_started(*_: object, **__: object) -> object:
        return downstream_broker

    monkeypatch.setattr(content_pipeline_module, "ensure_pipeline_broker_started", fake_ensure_pipeline_broker_started)

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, classify_event, normalized = await _seed_classify_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    seed_publisher = RecordingPublisher()

    # Drive the older meme all the way to EMBED-succeeded so its row exists in
    # Qdrant's perspective — that gives us a plausible similarity match for the
    # newer meme to collide with.
    older_meme_file_id, _, older_normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=seed_publisher,
        source_id="runtime-merge-rollback-older",
        post_id="9700",
        phash_tag="o",
    )
    async with postgres_session_factory() as stash_session:
        older_service = ContentPipelineService(
            stash_session,
            storage_client=storage_client,
            publisher=seed_publisher,
        )
        _ = await older_service.complete_embed_stage(
            meme_file_id=older_meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            embedding_result=build_voyage_embedding_result(input_hash="2" * 64),
            similarity_matches=(),
        )
        older_file_row = await stash_session.scalar(
            select(MemeFile).where(MemeFile.id == older_meme_file_id)
        )
        assert older_file_row is not None
        older_meme_id = older_file_row.meme_id

    # Baseline snapshot: files, sources, merge-log, popularity for the older meme
    # should remain unchanged after the merge rollback on the newer file.
    async with postgres_session_factory() as baseline_session:
        baseline_files = (
            await baseline_session.execute(select(MemeFile).where(MemeFile.meme_id == older_meme_id))
        ).scalars().all()
        baseline_file_ids = {row.id for row in baseline_files}
        baseline_sources = (
            await baseline_session.execute(
                select(MemeSource).where(MemeSource.file_id.in_(baseline_file_ids))
            )
        ).scalars().all()
        baseline_merge_logs = (
            await baseline_session.execute(select(MemeMergeLog))
        ).scalars().all()
        baseline_popularity = (
            await baseline_session.execute(
                select(MemePopularitySnapshot).where(MemePopularitySnapshot.meme_id == older_meme_id)
            )
        ).scalars().all()

    newer_publisher = RecordingPublisher()
    newer_meme_file_id, embed_event, newer_normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=newer_publisher,
        source_id="runtime-merge-rollback-newer",
        post_id="9701",
        phash_tag="n",
    )

    # Force the merge transaction to fail inside _transfer_meme_files. The
    # runtime wraps this into a PipelineMergeTransactionError via the service
    # layer and the classifier must keep it replayable.
    from memexpert.services import content_merge as content_merge_module

    async def fake_transfer(self: object, **_: object) -> tuple[uuid.UUID, ...]:
        _ = self
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
            result=build_ocr_result(source_object_key=newer_normalized.web_video_object_key),
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
    # sources, no merge-log emitted, popularity rows untouched.
    async with postgres_session_factory() as verify_session:
        post_files = (
            await verify_session.execute(select(MemeFile).where(MemeFile.meme_id == older_meme_id))
        ).scalars().all()
        post_file_ids = {row.id for row in post_files}
        post_sources = (
            await verify_session.execute(
                select(MemeSource).where(MemeSource.file_id.in_(post_file_ids))
            )
        ).scalars().all()
        post_merge_logs = (
            await verify_session.execute(select(MemeMergeLog))
        ).scalars().all()
        post_popularity = (
            await verify_session.execute(
                select(MemePopularitySnapshot).where(MemePopularitySnapshot.meme_id == older_meme_id)
            )
        ).scalars().all()
        # The embed cache row for the newer file must have been rolled back so
        # a replay starts from a clean durable state.
        post_cache_rows = (
            await verify_session.execute(
                select(EmbeddingCache).where(EmbeddingCache.source_file_id == newer_meme_file_id)
            )
        ).scalars().all()

    assert post_file_ids == baseline_file_ids
    assert {row.id for row in post_sources} == {row.id for row in baseline_sources}
    assert {row.id for row in post_merge_logs} == {row.id for row in baseline_merge_logs}
    assert {row.id for row in post_popularity} == {row.id for row in baseline_popularity}
    assert post_cache_rows == []

    # Undo the monkeypatch so replay_item can execute a normal transaction path.
    monkeypatch.undo()

    replay_publisher = RecordingPublisher()
    async with postgres_session_factory() as replay_session:
        replay_service = ContentPipelineService(
            replay_session,
            storage_client=storage_client,
            publisher=replay_publisher,
        )
        replay_response = await replay_service.replay_item(
            newer_meme_file_id,
            stage=ContentPipelineStage.EMBED,
        )

    assert replay_response.stage is ContentPipelineStage.EMBED
    assert len(replay_publisher.events) == 1


async def test_pipeline_runtime_embed_voyage_timeout_keeps_stage_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Voyage embed timeout must surface with a timeout-flavored reason and
    keep the stage replayable so the runtime can retry transiently."""

    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        _ = queue
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
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    runtime = build_pipeline_runtime(
        settings=Settings(),
        broker=build_pipeline_broker(Settings()),
        session_factory=postgres_session_factory,
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(normalize_result=normalized),
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
    publisher = RecordingPublisher()
    meme_file_id, embed_event, normalized = await _seed_embed_pending_item(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )
    dead_letters: list[Any] = []

    async def publish_dead_letter(
        payload: object,
        queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        headers: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        _ = queue
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
        ocr_processor=FakeOCRProcessor(result=build_ocr_result(source_object_key=normalized.web_video_object_key)),
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
