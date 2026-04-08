"""Integration tests for the RabbitMQ-backed stub transcode runtime."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import memexpert.services.content_pipeline as content_pipeline_module
from PIL import Image

from memexpert.core.broker import build_pipeline_broker
from memexpert.core.config import Settings
from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus, SourcePlatform
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineItemRead,
    ContentPipelineUploadMetadata,
)
from memexpert.services import ContentPipelineService
from memexpert.workers.pipeline_runtime import (
    PIPELINE_REASON_FORCED_TRANSCODE_FAILURE,
    PIPELINE_REASON_MALFORMED_EVENT,
    build_pipeline_runtime,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class FakeStorageClient:
    """Small sync S3-compatible client used by runtime-backed service tests."""

    put_calls: list[dict[str, object]] = field(default_factory=list)

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
        return {"ETag": "fake"}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
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
class FakeRabbitMessage:
    """Small RabbitMQ message double used to verify worker ack decisions."""

    headers: dict[str, Any] = field(default_factory=dict)
    content_type: str | None = "application/json"
    message_id: str | None = None
    ack_count: int = 0
    reject_calls: list[bool] = field(default_factory=list)
    nack_calls: list[bool] = field(default_factory=list)

    async def ack(self, multiple: bool = False) -> None:
        self.ack_count += 1

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None:
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


async def _fetch_item(
    session_factory: async_sessionmaker[AsyncSession],
    meme_file_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> ContentPipelineItemRead:
    async with session_factory() as session:
        service = ContentPipelineService(session, settings=settings)
        return await service.get_item(meme_file_id)


async def test_pipeline_runtime_declares_explicit_retry_and_dlx_topology() -> None:
    settings = Settings()
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(settings=settings, broker=broker)

    declared_exchanges: list[str] = []
    declared_queue_arguments: dict[str, dict[str, object] | None] = {}
    recorded_queues: dict[str, RecordedQueue] = {}

    async def declare_exchange(exchange: Any) -> RecordedExchange:
        declared_exchanges.append(exchange.name)
        return RecordedExchange(name=exchange.name)

    async def declare_queue(queue: Any) -> RecordedQueue:
        declared_queue_arguments[queue.name] = queue.arguments
        recorded_queue = RecordedQueue(name=queue.name)
        recorded_queues[queue.name] = recorded_queue
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
    retry_queue_arguments = declared_queue_arguments[runtime.retry_queue.name] or {}

    assert transcode_queue_arguments["x-dead-letter-exchange"] == runtime.retry_exchange.name
    assert transcode_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.retry_routing_key
    assert retry_queue_arguments["x-message-ttl"] == runtime.broker_settings.retry_backoff_milliseconds
    assert retry_queue_arguments["x-dead-letter-exchange"] == runtime.pipeline_exchange.name
    assert retry_queue_arguments["x-dead-letter-routing-key"] == runtime.broker_settings.transcode_retry_routing_key
    assert recorded_queues[runtime.transcode_queue.name].bindings == [
        (runtime.pipeline_exchange.name, runtime.broker_settings.meme_created_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.stage_replay_routing_key),
        (runtime.pipeline_exchange.name, runtime.broker_settings.transcode_retry_routing_key),
    ]
    assert recorded_queues[runtime.retry_queue.name].bindings == [
        (runtime.retry_exchange.name, runtime.broker_settings.retry_routing_key),
    ]
    assert recorded_queues[runtime.dead_letter_queue.name].bindings == [
        (runtime.dead_letter_exchange.name, runtime.broker_settings.dead_letter_routing_key),
    ]


async def test_pipeline_runtime_forced_failure_then_idempotent_replay_then_success(
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
    )
    failure_message = FakeRabbitMessage(message_id=str(initial_event.event_id))

    await failing_runtime.handle_transcode_message(initial_event.model_dump(mode="json"), failure_message)

    failed_item = await _fetch_item(postgres_session_factory, item.meme_file_id, settings=failing_settings)
    assert failed_item.current_status is ContentPipelineStageStatus.FAILED
    assert failed_item.current_stage is ContentPipelineStage.TRANSCODE
    assert failed_item.normalized_reason == PIPELINE_REASON_FORCED_TRANSCODE_FAILURE
    assert failed_item.last_error_text == (
        "Forced transcode failure requested by pipeline_worker_fail_transcode_for_meme_file_id."
    )
    assert failed_item.attempt_count == 1
    transcode_stage = next(stage for stage in failed_item.stages if stage.stage is ContentPipelineStage.TRANSCODE)
    assert transcode_stage.retry_after is not None
    assert failure_message.reject_calls == [False]
    assert failure_message.ack_count == 0
    assert failure_message.nack_calls == []

    replay_publisher = RecordingPublisher()
    async with postgres_session_factory() as replay_session:
        replay_service = ContentPipelineService(
            replay_session,
            settings=failing_settings,
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

    successful_runtime = build_pipeline_runtime(
        settings=successful_settings,
        broker=build_pipeline_broker(successful_settings),
        session_factory=postgres_session_factory,
    )
    success_message = FakeRabbitMessage(message_id=str(replay_event.event_id))

    await successful_runtime.handle_transcode_message(replay_event.model_dump(mode="json"), success_message)

    succeeded_item = await _fetch_item(postgres_session_factory, item.meme_file_id, settings=successful_settings)
    assert succeeded_item.current_status is ContentPipelineStageStatus.PENDING
    assert succeeded_item.current_stage is ContentPipelineStage.OCR
    assert succeeded_item.original_object_key == item.original_object_key
    assert succeeded_item.web_video_object_key is not None
    assert succeeded_item.web_video_object_key.endswith(f"/{item.meme_file_id}/web.mp4")
    assert succeeded_item.normalized_reason is None
    assert succeeded_item.last_error_text is None
    assert succeeded_item.attempt_count == 0
    assert tuple((stage.stage, stage.status) for stage in succeeded_item.stages) == (
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.SUCCEEDED),
        (ContentPipelineStage.TRANSCODE, ContentPipelineStageStatus.SUCCEEDED),
        (ContentPipelineStage.OCR, ContentPipelineStageStatus.PENDING),
    )
    assert len(downstream_broker.publish_calls) == 1
    assert downstream_broker.publish_calls[0]["routing_key"] == successful_runtime.broker_settings.ocr_routing_key
    assert success_message.ack_count == 1
    assert success_message.reject_calls == []
    assert success_message.nack_calls == []


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
    )
    dead_letters: list[dict[str, object]] = []

    async def publish_dead_letter(
        payload: object,
        queue: object = "",
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
