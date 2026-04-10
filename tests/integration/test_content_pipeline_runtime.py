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
from memexpert.core.config import Settings
from memexpert.core.media import NormalizedMediaResult, UploadMediaDetails
from memexpert.core.ocr import OCRExtractionResult, OCRTimeoutError
from memexpert.models.content import Meme, MemeFile, MemeFileOCRResult
from memexpert.models.enums import ContentLanguage, ContentPipelineStage, ContentPipelineStageStatus, SourcePlatform
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineItemRead,
    ContentPipelineUploadMetadata,
)
from memexpert.services import ContentPipelineService
from memexpert.workers.pipeline_runtime import (
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

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
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
        raise AssertionError("extract_preview_frame should not be called by transcode tests")


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
