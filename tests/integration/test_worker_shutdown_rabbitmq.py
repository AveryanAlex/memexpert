"""Linux process-signal regression for RabbitMQ worker shutdown and redelivery."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import signal
import sys
import traceback
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from faststream import AckPolicy
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage  # noqa: TC002 - FastStream resolves this annotation at runtime.
from sqlalchemy import select
from testcontainers.core.container import DockerContainer  # pyright: ignore[reportMissingTypeStubs]
from testcontainers.core.waiting_utils import (  # pyright: ignore[reportMissingTypeStubs]
    WaitStrategy,
    WaitStrategyTarget,
)

from memexpert.core.broker import build_pipeline_broker
from memexpert.core.config import Settings
from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    IngestFileOrigin,
)
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineEventType
from memexpert.workers.pipeline_runtime import PIPELINE_REASON_WORKER_SHUTDOWN, build_pipeline_runtime
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from collections.abc import Iterator
    from multiprocessing.connection import Connection

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.media.contracts import NormalizedMediaResult, UploadMediaDetails


RABBITMQ_IMAGE: Final = "rabbitmq:4.3.1-alpine"
RABBITMQ_PORT: Final = 5672
RABBITMQ_USER: Final = "memexpert_test"
RABBITMQ_PASSWORD: Final = "memexpert_test"
RABBITMQ_STARTUP_TIMEOUT_SECONDS: Final = 90
RABBITMQ_POLL_INTERVAL_SECONDS: Final = 0.5
WORKER_GRACEFUL_TIMEOUT_SECONDS: Final = 1.5
CONTROL_MESSAGE_TIMEOUT_SECONDS: Final = 20.0
WORKER_JOIN_TIMEOUT_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class _ShutdownTopology:
    exchange: str
    routing_key_prefix: str
    media_inspect_queue: str
    transcode_queue: str
    retry_exchange: str
    retry_queue: str
    dead_letter_exchange: str
    dead_letter_queue: str

    @classmethod
    def unique(cls) -> _ShutdownTopology:
        suffix = uuid.uuid4().hex
        return cls(
            exchange=f"test.shutdown.{suffix}",
            routing_key_prefix=f"test.shutdown.{suffix}",
            media_inspect_queue=f"test.shutdown.{suffix}.media-inspect",
            transcode_queue=f"test.shutdown.{suffix}.transcode",
            retry_exchange=f"test.shutdown.{suffix}.retry",
            retry_queue=f"test.shutdown.{suffix}.retry-legacy",
            dead_letter_exchange=f"test.shutdown.{suffix}.dlx",
            dead_letter_queue=f"test.shutdown.{suffix}.dlq",
        )

    @property
    def transcode_routing_key(self) -> str:
        return f"{self.routing_key_prefix}.transcode"

    @property
    def transcode_retry_routing_key(self) -> str:
        return f"{self.routing_key_prefix}.retry.transcode"


class _RabbitMQStartupWaitStrategy(WaitStrategy):
    """Wait for RabbitMQ's startup marker and fail clearly if the container exits."""

    def __init__(self) -> None:
        super().__init__()
        self.with_startup_timeout(RABBITMQ_STARTUP_TIMEOUT_SECONDS)
        self.with_poll_interval(RABBITMQ_POLL_INTERVAL_SECONDS)

    def wait_until_ready(self, container: WaitStrategyTarget) -> None:
        last_output = "RabbitMQ did not emit logs"

        def startup_completed() -> bool:
            nonlocal last_output
            container.reload()
            stdout, stderr = container.get_logs()
            combined = stdout + stderr
            last_output = combined.decode(errors="replace").strip()[-4000:]
            if b"Server startup complete" in combined:
                return True
            if container.status in {"dead", "exited"}:
                raise RuntimeError(f"RabbitMQ container exited during startup:\n{last_output}")
            return False

        if not self._poll(startup_completed):
            raise TimeoutError(
                "RabbitMQ testcontainer did not become ready after "
                f"{RABBITMQ_STARTUP_TIMEOUT_SECONDS}s. Last logs:\n{last_output}"
            )


@pytest.fixture(scope="module")
def rabbitmq_container_url() -> Iterator[str]:
    """Yield an AMQP URL backed by the pinned RabbitMQ integration image."""

    with (
        DockerContainer(RABBITMQ_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", RABBITMQ_USER)
        .with_env("RABBITMQ_DEFAULT_PASS", RABBITMQ_PASSWORD)
        .with_exposed_ports(RABBITMQ_PORT)
        .waiting_for(_RabbitMQStartupWaitStrategy()) as rabbitmq
    ):
        host = rabbitmq.get_container_host_ip()
        port = rabbitmq.get_exposed_port(RABBITMQ_PORT)
        yield f"amqp://{RABBITMQ_USER}:{RABBITMQ_PASSWORD}@{host}:{port}/"


@dataclass(slots=True)
class _StorageBody:
    payload: bytes

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        return None


@dataclass(slots=True)
class _ChildStorageClient:
    object_key: str
    object_bytes: bytes

    def get_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        if Key != self.object_key:
            raise AssertionError(f"Worker requested unexpected test object {Key!r}.")
        return {"Body": _StorageBody(self.object_bytes)}

    def put_object(self, **_kwargs: object) -> object:
        raise AssertionError("The blocking transcode must not upload a derivative.")

    def delete_object(self, **_kwargs: object) -> object:
        return {}


@dataclass(slots=True)
class _BlockingMediaProcessor:
    control: Connection
    expected_bytes: bytes

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("The shutdown regression should not receive media-inspect work.")

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
        generation_id: uuid.UUID | None = None,
    ) -> NormalizedMediaResult:
        _ = (filename, content_type, generation_id)
        if media_bytes != self.expected_bytes:
            raise AssertionError("Worker downloaded unexpected test media bytes.")
        self.control.send(("delivery_started", str(meme_file_id)))
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.control.send(("delivery_cancelled", str(meme_file_id)))
            raise
        raise AssertionError("The blocking media processor unexpectedly resumed.")

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("The shutdown regression should not extract a preview frame.")


def _worker_settings(rabbitmq_url: str, topology: _ShutdownTopology) -> Settings:
    return Settings(
        rabbitmq_url=rabbitmq_url,
        pipeline_broker_exchange=topology.exchange,
        pipeline_broker_routing_key_prefix=topology.routing_key_prefix,
        pipeline_broker_media_inspect_queue=topology.media_inspect_queue,
        pipeline_broker_transcode_queue=topology.transcode_queue,
        pipeline_broker_retry_exchange=topology.retry_exchange,
        pipeline_broker_retry_queue=topology.retry_queue,
        pipeline_broker_dead_letter_exchange=topology.dead_letter_exchange,
        pipeline_broker_dead_letter_queue=topology.dead_letter_queue,
        pipeline_broker_connection_timeout_seconds=5.0,
        pipeline_broker_retry_backoff_seconds=60.0,
        pipeline_worker_prefetch_count=1,
        pipeline_worker_graceful_shutdown_timeout_seconds=WORKER_GRACEFUL_TIMEOUT_SECONDS,
    )


async def _run_child_pipeline_runtime(
    *,
    control: Connection,
    rabbitmq_url: str,
    postgres_url: str,
    topology: _ShutdownTopology,
    object_key: str,
    object_bytes: bytes,
    role: WorkerRole,
    stop_event: asyncio.Event,
    force_stop_event: asyncio.Event,
) -> None:
    if role is not WorkerRole.MEDIA:
        raise AssertionError(f"Unexpected worker role {role!r}.")

    settings = _worker_settings(rabbitmq_url, topology)
    engine = build_async_engine(postgres_url, connect_timeout=5.0)
    broker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        role=role,
        broker=broker,
        session_factory=build_async_session_factory(engine),
        storage_client=_ChildStorageClient(object_key=object_key, object_bytes=object_bytes),
        media_processor=_BlockingMediaProcessor(control=control, expected_bytes=object_bytes),
    )
    runtime_type = type(runtime)
    real_quiesce_consumers = runtime_type._quiesce_consumers

    async def report_ready(_settings: Settings, observed_stop_event: asyncio.Event) -> None:
        control.send(("ready", len(broker.subscribers)))
        await observed_stop_event.wait()

    async def observe_quiesce(runtime_instance: Any, deadline: float) -> bool:
        quiesced = await real_quiesce_consumers(runtime_instance, deadline)
        control.send(("intake_quiesced", len(broker.subscribers), quiesced))
        return quiesced

    runtime_type._quiesce_consumers = cast("Any", observe_quiesce)
    try:
        await runtime.run(
            stop_event=stop_event,
            force_stop_event=force_stop_event,
            background_runners=(report_ready,),
        )
    finally:
        runtime_type._quiesce_consumers = real_quiesce_consumers
        await engine.dispose()


def _worker_process_target(
    control: Connection,
    rabbitmq_url: str,
    postgres_url: str,
    topology: _ShutdownTopology,
    object_key: str,
    object_bytes: bytes,
) -> None:
    """Run the real signal controller around an injected real-broker runtime."""

    from memexpert.workers import main as worker_main

    async def run_test_runtime(
        *,
        role: WorkerRole,
        stop_event: asyncio.Event,
        force_stop_event: asyncio.Event,
    ) -> None:
        await _run_child_pipeline_runtime(
            control=control,
            rabbitmq_url=rabbitmq_url,
            postgres_url=postgres_url,
            topology=topology,
            object_key=object_key,
            object_bytes=object_bytes,
            role=role,
            stop_event=stop_event,
            force_stop_event=force_stop_event,
        )

    worker_main.run_pipeline_runtime = cast("Any", run_test_runtime)
    try:
        asyncio.run(worker_main.run_worker_runtime(role=WorkerRole.MEDIA))
    except BaseException as exc:
        control.send(("child_error", repr(exc), traceback.format_exc()))
        raise
    else:
        control.send(("clean_exit",))
    finally:
        control.close()


async def _receive_control_message(
    connection: Connection,
    expected_kind: str,
    *,
    timeout: float = CONTROL_MESSAGE_TIMEOUT_SECONDS,
) -> tuple[object, ...]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0 or not await asyncio.to_thread(connection.poll, max(remaining, 0.0)):
            pytest.fail(f"Timed out waiting for worker control message {expected_kind!r}.")
        try:
            message = cast("tuple[object, ...]", connection.recv())
        except EOFError:
            pytest.fail(f"Worker control pipe closed before {expected_kind!r}.")
        kind = message[0]
        if kind == "child_error":
            pytest.fail(f"Worker child failed: {message[1]}\n{message[2]}")
        if kind == expected_kind:
            return message


async def _seed_transcode_delivery(
    session: AsyncSession,
) -> tuple[ContentPipelineDispatchEvent, str, bytes]:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    event_id = uuid.uuid7()
    object_key = f"shutdown-test/{meme_file_id}/original.png"
    object_bytes = f"shutdown-test-{meme_file_id}".encode()
    now = utcnow()
    event = ContentPipelineDispatchEvent(
        event_id=event_id,
        event_type=ContentPipelineEventType.MEME_CREATED,
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        source_kind=ContentSourceKind.MANUAL_UPLOAD,
        original_object_key=object_key,
        attempt=1,
        created_at=now,
    )
    session.add(
        Meme(
            id=meme_id,
            media_type=ContentKind.IMAGE,
            primary_file_id=meme_file_id,
            language=ContentLanguage.NONE,
            is_public=False,
        )
    )
    await session.flush()
    session.add_all(
        [
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.PENDING,
                width=8,
                height=8,
                file_size_bytes=len(object_bytes),
                mime_type="image/png",
                s3_original_key=object_key,
                perceptual_hash="f" * 16,
                sha256_hex=hashlib.sha256(object_bytes).hexdigest(),
                ingest_origin=IngestFileOrigin.NEW_MEME,
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
    return event, object_key, object_bytes


def _transcode_queue(topology: _ShutdownTopology) -> RabbitQueue:
    return RabbitQueue(
        topology.transcode_queue,
        durable=True,
        routing_key=topology.transcode_routing_key,
        arguments={
            "x-dead-letter-exchange": topology.retry_exchange,
            "x-dead-letter-routing-key": topology.transcode_retry_routing_key,
        },
    )


def _pipeline_exchange(topology: _ShutdownTopology) -> RabbitExchange:
    return RabbitExchange(topology.exchange, type=ExchangeType.TOPIC, durable=True)


async def _publish(
    broker: RabbitBroker,
    topology: _ShutdownTopology,
    payload: object,
    *,
    message_id: str,
) -> None:
    await broker.publish(
        payload,
        exchange=_pipeline_exchange(topology),
        routing_key=topology.transcode_routing_key,
        persist=True,
        message_id=message_id,
    )


async def _consume_with_replacement(
    rabbitmq_url: str,
    topology: _ShutdownTopology,
    *,
    expected_count: int,
) -> dict[str, dict[str, object]]:
    broker = RabbitBroker(rabbitmq_url, timeout=5.0)
    received: dict[str, dict[str, object]] = {}
    all_received = asyncio.Event()

    @broker.subscriber(
        _transcode_queue(topology),
        _pipeline_exchange(topology),
        ack_policy=AckPolicy.MANUAL,
    )
    async def consume(payload: object, message: RabbitMessage) -> None:
        message_id = message.message_id
        if message_id is None:
            raise AssertionError("Shutdown regression messages must retain their message IDs.")
        received[message_id] = {
            "payload": payload,
            "redelivered": bool(cast("Any", message.raw_message).redelivered),
        }
        await message.ack()
        if len(received) == expected_count:
            all_received.set()

    await broker.start()
    try:
        await asyncio.wait_for(all_received.wait(), timeout=10.0)
    finally:
        await broker.stop()
    return received


@pytest.mark.skipif(sys.platform == "win32", reason="asyncio process signal handlers are unsupported on Windows")
async def test_sigterm_quiesces_worker_and_requeues_inflight_manual_ack_delivery(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    rabbitmq_container_url: str,
) -> None:
    """A first SIGTERM drains intake, then the deadline requeues unfinished work."""

    event, object_key, object_bytes = await _seed_transcode_delivery(migrated_db_session)
    topology = _ShutdownTopology.unique()
    context = multiprocessing.get_context("spawn")
    receive_control, send_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker_process_target,
        args=(
            send_control,
            rabbitmq_container_url,
            postgres_async_url,
            topology,
            object_key,
            object_bytes,
        ),
        name="memexpert-worker-shutdown-regression",
    )
    publisher = RabbitBroker(rabbitmq_container_url, timeout=5.0)
    late_message_id = f"late-{uuid.uuid4()}"

    process.start()
    send_control.close()
    try:
        ready = await _receive_control_message(receive_control, "ready")
        assert ready[1] == 2  # media-inspect and transcode consumers owned by the media role

        await publisher.start()
        await _publish(
            publisher,
            topology,
            event.model_dump(mode="json"),
            message_id=str(event.event_id),
        )
        started = await _receive_control_message(receive_control, "delivery_started")
        assert started[1] == str(event.meme_file_id)

        assert process.pid is not None
        os.kill(process.pid, signal.SIGTERM)
        quiesced = await _receive_control_message(receive_control, "intake_quiesced")
        assert quiesced[1] == 2
        assert quiesced[2] is True

        await _publish(
            publisher,
            topology,
            {"probe": "published-after-intake-quiesced"},
            message_id=late_message_id,
        )
        cancelled = await _receive_control_message(receive_control, "delivery_cancelled")
        assert cancelled[1] == str(event.meme_file_id)
        _ = await _receive_control_message(receive_control, "clean_exit")
        await asyncio.to_thread(process.join, WORKER_JOIN_TIMEOUT_SECONDS)

        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        await publisher.stop()
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, WORKER_JOIN_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, WORKER_JOIN_TIMEOUT_SECONDS)
        process.close()
        receive_control.close()

    migrated_db_session.expire_all()
    stage = await migrated_db_session.scalar(
        select(PipelineStageJournal).where(
            PipelineStageJournal.meme_file_id == event.meme_file_id,
            PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
        )
    )
    assert stage is not None
    assert stage.status is ContentPipelineStageStatus.FAILED
    assert stage.normalized_reason == PIPELINE_REASON_WORKER_SHUTDOWN
    assert stage.is_retryable is True
    assert stage.last_event_id == event.event_id

    received = await _consume_with_replacement(
        rabbitmq_container_url,
        topology,
        expected_count=2,
    )
    assert set(received) == {str(event.event_id), late_message_id}
    assert received[str(event.event_id)]["redelivered"] is True
    assert received[str(event.event_id)]["payload"] == event.model_dump(mode="json")
    assert received[late_message_id] == {
        "payload": {"probe": "published-after-intake-quiesced"},
        "redelivered": False,
    }
