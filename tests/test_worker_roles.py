"""Worker role isolation, topology ownership, and dependency-loading tests."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

import memexpert.workers.pipeline_runtime.topology as topology_module
from memexpert.core.broker import build_pipeline_broker
from memexpert.core.config import Settings
from memexpert.models.enums import ContentPipelineStage
from memexpert.pipeline.events import build_source_engagement_session_key
from memexpert.workers.pipeline_runtime import build_pipeline_runtime
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from faststream.rabbit import RabbitBroker


@pytest.mark.parametrize(
    ("role", "expected_queue_attributes"),
    [
        (WorkerRole.MEDIA, {"media_inspect_queue", "transcode_queue"}),
        (WorkerRole.OCR, {"ocr_queue"}),
        (WorkerRole.ENRICHMENT, {"embed_queue", "classify_queue"}),
        (WorkerRole.SYNC, {"sync_qdrant_queue", "sync_meili_queue"}),
        (
            WorkerRole.TELEGRAM,
            {"source_engagement_capture_queues", "source_channel_audience_capture_queues"},
        ),
        (
            WorkerRole.ALL,
            {
                "media_inspect_queue",
                "source_engagement_capture_queues",
                "source_channel_audience_capture_queues",
                "transcode_queue",
                "ocr_queue",
                "embed_queue",
                "classify_queue",
                "sync_qdrant_queue",
                "sync_meili_queue",
            },
        ),
    ],
)
def test_worker_role_registers_only_owned_subscribers(
    role: WorkerRole,
    expected_queue_attributes: set[str],
) -> None:
    settings = Settings()
    broker = build_pipeline_broker(settings)
    source_session_key = build_source_engagement_session_key(uuid.uuid7(), "worker-role-test")
    runtime = build_pipeline_runtime(
        settings=settings,
        role=role,
        broker=broker,
        storage_client=cast("Any", object()),
        media_processor=cast("Any", object()),
        ocr_processor=cast("Any", object()),
        voyage_client=cast("Any", object()),
        qdrant_client=cast("Any", object()),
        qdrant_sync_client=cast("Any", object()),
        meilisearch_sync_client=cast("Any", object()),
        classification_client=cast("Any", object()),
        source_engagement_telegram_client_factory=cast("Any", object()),
        source_engagement_session_keys=(source_session_key,),
    )

    expected_queue_names: set[str] = set()
    for attribute_name in expected_queue_attributes:
        value = getattr(runtime, attribute_name)
        if isinstance(value, tuple):
            expected_queue_names.update(queue.name for queue in value)
        else:
            expected_queue_names.add(value.name)
    subscribers = cast("Any", broker).subscribers
    observed_queue_names = {subscriber.queue.name for subscriber in subscribers}

    assert observed_queue_names == expected_queue_names
    assert all(subscriber.consume_args == role.consumer_arguments() for subscriber in subscribers)
    assert len({id(subscriber.channel) for subscriber in subscribers}) == len(subscribers)


def test_sync_role_does_not_initialize_media_ocr_enrichment_or_telegram_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dependency(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("sync role initialized an unrelated dependency")

    monkeypatch.setattr(topology_module, "get_s3_client", unexpected_dependency)
    monkeypatch.setattr(topology_module, "PipelineMediaProcessor", unexpected_dependency)
    monkeypatch.setattr(topology_module, "build_pipeline_ocr_processor", unexpected_dependency)
    monkeypatch.setattr(topology_module, "build_pipeline_voyage_client", unexpected_dependency)
    monkeypatch.setattr(topology_module, "PipelineQdrantClient", unexpected_dependency)
    monkeypatch.setattr(topology_module, "build_pipeline_classification_client", unexpected_dependency)
    monkeypatch.setattr(topology_module, "TelegramSessionManager", unexpected_dependency)

    runtime = build_pipeline_runtime(
        settings=Settings(),
        role=WorkerRole.SYNC,
        broker=build_pipeline_broker(Settings()),
        qdrant_sync_client=cast("Any", object()),
        meilisearch_sync_client=cast("Any", object()),
    )

    assert runtime.role is WorkerRole.SYNC


@dataclass(slots=True)
class _DeclaredExchange:
    name: str


@dataclass(slots=True)
class _DeclaredQueue:
    name: str
    bindings: list[tuple[str, str]] = field(default_factory=list)

    async def bind(self, exchange: _DeclaredExchange, *, routing_key: str) -> None:
        self.bindings.append((exchange.name, routing_key))


async def test_sync_role_declares_only_sync_retry_and_dead_letter_queues() -> None:
    settings = Settings()
    broker: RabbitBroker = build_pipeline_broker(settings)
    runtime = build_pipeline_runtime(
        settings=settings,
        role=WorkerRole.SYNC,
        broker=broker,
        qdrant_sync_client=cast("Any", object()),
        meilisearch_sync_client=cast("Any", object()),
    )
    declared_queue_names: list[str] = []

    async def declare_exchange(exchange: Any) -> _DeclaredExchange:
        return _DeclaredExchange(name=exchange.name)

    async def declare_queue(queue: Any) -> _DeclaredQueue:
        declared_queue_names.append(queue.name)
        return _DeclaredQueue(name=queue.name)

    cast("Any", broker).declare_exchange = declare_exchange
    cast("Any", broker).declare_queue = declare_queue

    await runtime.declare_topology()

    assert set(declared_queue_names) == {
        runtime.sync_qdrant_queue.name,
        runtime.sync_meili_queue.name,
        runtime.sync_qdrant_retry_queue.name,
        runtime.sync_meili_retry_queue.name,
        runtime.dead_letter_queue.name,
    }


@dataclass(slots=True)
class _CanaryOCRProcessor:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def extract_text(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return object()


async def test_ocr_role_runs_bundled_startup_canary_before_readiness() -> None:
    settings = Settings()
    ocr_processor = _CanaryOCRProcessor()
    runtime = build_pipeline_runtime(
        settings=settings,
        role=WorkerRole.OCR,
        broker=build_pipeline_broker(settings),
        storage_client=cast("Any", object()),
        media_processor=cast("Any", object()),
        ocr_processor=cast("Any", ocr_processor),
    )

    await runtime.verify_readiness()

    assert len(ocr_processor.calls) == 1
    assert ocr_processor.calls[0]["filename"] == "runtime-health-canary.png"
    assert ocr_processor.calls[0]["mime_type"] == "image/png"
    assert len(cast("bytes", ocr_processor.calls[0]["media_bytes"])) > 100


async def test_telegram_role_observes_stop_during_pre_runtime_session_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    discovery_started = asyncio.Event()

    class FakeHealthReporter:
        async def start(self) -> None:
            events.append("health.start")

        async def stop(self) -> None:
            events.append("health.stop")

        def mark_draining(self) -> None:
            events.append("health.draining")

    async def load_session_keys(_session_factory: object) -> tuple[str, ...]:
        events.append("discovery.started")
        discovery_started.set()
        try:
            await asyncio.Future()
            raise AssertionError("blocked session discovery unexpectedly resumed")
        except asyncio.CancelledError:
            events.append("discovery.cancelled")
            raise

    monkeypatch.setattr(topology_module, "get_async_session_factory", object)
    monkeypatch.setattr(topology_module, "_load_source_engagement_session_keys", load_session_keys)
    monkeypatch.setattr(
        topology_module.RuntimeHealthReporter,
        "from_settings",
        lambda *_args, **_kwargs: FakeHealthReporter(),
    )
    monkeypatch.setattr(
        topology_module,
        "build_pipeline_runtime",
        lambda **_kwargs: pytest.fail("runtime must not be built after pre-start shutdown"),
    )
    stop_event = asyncio.Event()

    run_task = asyncio.create_task(
        topology_module.run_pipeline_runtime(
            settings=Settings(),
            role=WorkerRole.TELEGRAM,
            stop_event=stop_event,
        )
    )
    await asyncio.wait_for(discovery_started.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert events == [
        "health.start",
        "discovery.started",
        "discovery.cancelled",
        "health.draining",
        "health.stop",
    ]


def test_worker_role_stage_ownership_is_non_overlapping_outside_all() -> None:
    concrete_roles = (WorkerRole.MEDIA, WorkerRole.OCR, WorkerRole.ENRICHMENT, WorkerRole.SYNC, WorkerRole.TELEGRAM)
    owners = {
        stage: [role for role in concrete_roles if role.consumes_stage(stage)]
        for stage in ContentPipelineStage
        if stage is not ContentPipelineStage.INGEST
    }

    assert all(len(stage_owners) == 1 for stage_owners in owners.values())
    assert WorkerRole.ALL.stages == frozenset(owners)
