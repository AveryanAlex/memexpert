"""Worker signal handling and bounded RabbitMQ drain lifecycle tests."""

from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import memexpert.workers.main as worker_main_module
import memexpert.workers.pipeline_runtime.runtime as pipeline_runtime_module
from memexpert.core.config import Settings
from memexpert.workers.main import WorkerSignalController, run_worker_runtime
from memexpert.workers.pipeline_runtime.runtime import PipelineRuntime
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


@dataclass(slots=True)
class FakeLoop:
    added: list[tuple[signal.Signals, Callable[..., object], tuple[object, ...]]] = field(default_factory=list)
    removed: list[signal.Signals] = field(default_factory=list)

    def add_signal_handler(
        self,
        sig: signal.Signals,
        callback: Callable[..., object],
        *args: object,
    ) -> None:
        self.added.append((sig, callback, args))

    def remove_signal_handler(self, sig: signal.Signals) -> bool:
        self.removed.append(sig)
        return True


@dataclass(slots=True)
class FakeSubscriber:
    name: str
    events: list[str]
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def stop(self) -> None:
        self.events.append(f"subscriber.{self.name}.stop")
        self.stopped.set()


@dataclass(slots=True)
class BlockingSubscriber:
    name: str
    events: list[str]
    all_started: asyncio.Event
    release: asyncio.Event
    peer_started: Callable[[], bool]
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def stop(self) -> None:
        self.events.append(f"subscriber.{self.name}.stop_started")
        self.stopped.set()
        if self.peer_started():
            self.all_started.set()
        await self.release.wait()
        self.events.append(f"subscriber.{self.name}.stop_finished")


@dataclass(slots=True)
class FakeBroker:
    subscribers: tuple[Any, ...]
    events: list[str]

    async def connect(self) -> None:
        self.events.append("broker.connect")

    async def start(self) -> None:
        self.events.append("broker.start")

    async def stop(self) -> None:
        self.events.append("broker.stop")


@dataclass(slots=True)
class FakeHealthReporter:
    events: list[str]
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        self.events.append("health.start")

    async def stop(self) -> None:
        self.events.append("health.stop")

    def mark_ready(self) -> None:
        self.events.append("health.running")
        self.ready.set()

    def mark_draining(self) -> None:
        self.events.append("health.draining")

    @asynccontextmanager
    async def operation(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        _ = (name, timeout_seconds)
        yield


@dataclass(slots=True)
class FakeTelegramSessionManager:
    events: list[str]

    async def shutdown(self) -> None:
        self.events.append("telegram.shutdown")


def _runtime(
    *,
    broker: FakeBroker,
    health_reporter: FakeHealthReporter,
    telegram_manager: FakeTelegramSessionManager,
    timeout_seconds: float = 1.0,
) -> PipelineRuntime:
    values = {item.name: cast("Any", object()) for item in fields(PipelineRuntime) if item.init}
    values.update(
        role=WorkerRole.OCR,
        settings=Settings(pipeline_worker_graceful_shutdown_timeout_seconds=timeout_seconds),
        broker=broker,
        health_reporter=health_reporter,
        source_engagement_telegram_session_manager=telegram_manager,
    )
    return PipelineRuntime(**values)


def _patch_startup(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    async def declare_topology(_runtime: PipelineRuntime) -> None:
        events.append("runtime.declare")

    async def verify_readiness(_runtime: PipelineRuntime) -> None:
        events.append("runtime.verify")

    monkeypatch.setattr(PipelineRuntime, "declare_topology", declare_topology)
    monkeypatch.setattr(PipelineRuntime, "verify_readiness", verify_readiness)


def _patch_blocking_startup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: str,
    events: list[str],
    entered: asyncio.Event,
) -> None:
    async def block_phase(name: str) -> None:
        events.append(f"{name}.started")
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append(f"{name}.cancelled")
            raise

    async def broker_start(_broker: FakeBroker) -> None:
        if phase == "broker":
            await block_phase("broker.start")
        else:
            events.append("broker.start")

    async def declare_topology(_runtime: PipelineRuntime) -> None:
        if phase == "topology":
            await block_phase("runtime.declare")
        else:
            events.append("runtime.declare")

    async def verify_readiness(_runtime: PipelineRuntime) -> None:
        if phase == "readiness":
            await block_phase("runtime.verify")
        else:
            events.append("runtime.verify")

    monkeypatch.setattr(FakeBroker, "start", broker_start)
    monkeypatch.setattr(PipelineRuntime, "declare_topology", declare_topology)
    monkeypatch.setattr(PipelineRuntime, "verify_readiness", verify_readiness)


def _handler_for(loop: FakeLoop, sig: signal.Signals) -> tuple[Callable[..., object], tuple[object, ...]]:
    for registered_signal, callback, args in loop.added:
        if registered_signal is sig:
            return callback, args
    raise AssertionError(f"Signal {sig!r} was not registered.")


async def test_worker_signal_controller_registers_graceful_then_forced_shutdown() -> None:
    loop = FakeLoop()
    controller = WorkerSignalController(loop=loop)

    controller.install()
    assert [registered[0] for registered in loop.added] == [signal.SIGINT, signal.SIGTERM]

    callback, args = _handler_for(loop, signal.SIGTERM)
    callback(*args)
    assert controller.stop_event.is_set()
    assert not controller.force_stop_event.is_set()

    callback(*args)
    assert controller.force_stop_event.is_set()

    controller.close()
    assert loop.removed == [signal.SIGINT, signal.SIGTERM]


async def test_run_worker_runtime_passes_signal_events_and_always_removes_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = FakeLoop()
    controller = WorkerSignalController(loop=loop)
    run_pipeline_runtime = AsyncMock(side_effect=RuntimeError("startup failed"))
    monkeypatch.setattr(worker_main_module, "run_pipeline_runtime", run_pipeline_runtime)

    with pytest.raises(RuntimeError, match="startup failed"):
        await run_worker_runtime(role=WorkerRole.SYNC, signal_controller=controller)

    run_pipeline_runtime.assert_awaited_once_with(
        role=WorkerRole.SYNC,
        stop_event=controller.stop_event,
        force_stop_event=controller.force_stop_event,
    )
    assert loop.removed == [signal.SIGINT, signal.SIGTERM]


@pytest.mark.parametrize("phase", ["broker", "topology", "readiness"])
@pytest.mark.parametrize("shutdown_event_name", ["stop", "force"])
async def test_worker_observes_graceful_and_forced_shutdown_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    shutdown_event_name: str,
) -> None:
    events: list[str] = []
    entered = asyncio.Event()
    subscriber = FakeSubscriber("ocr", events)
    broker = FakeBroker((subscriber,), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
    )
    _patch_blocking_startup(
        monkeypatch,
        phase=phase,
        events=events,
        entered=entered,
    )
    stop_event = asyncio.Event()
    force_stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event, force_stop_event=force_stop_event))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    requested_event = stop_event if shutdown_event_name == "stop" else force_stop_event
    requested_event.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    phase_event = {
        "broker": "broker.start",
        "topology": "runtime.declare",
        "readiness": "runtime.verify",
    }[phase]
    assert f"{phase_event}.cancelled" in events
    assert "health.running" not in events
    assert events.index("health.draining") < events.index("subscriber.ocr.stop")
    assert events.index(f"{phase_event}.cancelled") < events.index("broker.stop")
    assert events.index("broker.stop") < events.index("telegram.shutdown")
    assert events.index("telegram.shutdown") < events.index("health.stop")


async def test_second_signal_does_not_cancel_unowned_initial_broker_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connect_entered = asyncio.Event()
    release_connect = asyncio.Event()
    broker = FakeBroker((FakeSubscriber("ocr", events),), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
    )
    _patch_startup(monkeypatch, events)

    async def connect(_broker: FakeBroker) -> None:
        events.append("broker.connect.started")
        connect_entered.set()
        try:
            await release_connect.wait()
        except asyncio.CancelledError:
            events.append("broker.connect.cancelled")
            raise
        events.append("broker.connect.finished")

    monkeypatch.setattr(FakeBroker, "connect", connect)
    stop_event = asyncio.Event()
    force_stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event, force_stop_event=force_stop_event))
    await asyncio.wait_for(connect_entered.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.sleep(0)
    force_stop_event.set()
    await asyncio.sleep(0)

    assert not run_task.done()
    assert "broker.connect.cancelled" not in events

    release_connect.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert "broker.connect.cancelled" not in events
    assert "broker.start" not in events
    assert events.index("broker.connect.finished") < events.index("broker.stop")


async def test_initial_connect_failure_after_shutdown_request_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connect_entered = asyncio.Event()
    release_connect = asyncio.Event()
    broker = FakeBroker((FakeSubscriber("ocr", events),), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
    )
    _patch_startup(monkeypatch, events)

    async def connect(_broker: FakeBroker) -> None:
        connect_entered.set()
        await release_connect.wait()
        raise TimeoutError("RabbitMQ connect timed out")

    monkeypatch.setattr(FakeBroker, "connect", connect)
    warning = Mock()
    monkeypatch.setattr(pipeline_runtime_module.logger, "warning", warning)
    stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event))
    await asyncio.wait_for(connect_entered.wait(), timeout=1.0)
    stop_event.set()
    release_connect.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    warning.assert_called_once()
    assert warning.call_args.args == ("worker_broker_connect_failed_during_shutdown",)
    assert "broker.start" not in events
    assert "broker.stop" in events
    assert events.index("broker.stop") < events.index("health.stop")


async def test_initial_connect_failure_during_normal_startup_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    broker = FakeBroker((FakeSubscriber("ocr", events),), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
    )

    async def connect(_broker: FakeBroker) -> None:
        raise ConnectionError("RabbitMQ unavailable")

    monkeypatch.setattr(FakeBroker, "connect", connect)

    with pytest.raises(ConnectionError, match="RabbitMQ unavailable"):
        await runtime.run()

    assert "broker.start" not in events
    assert "broker.stop" in events
    assert events.index("broker.stop") < events.index("health.stop")


async def test_second_signal_forces_cancellation_resistant_startup_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    startup_entered = asyncio.Event()
    first_cancellation = asyncio.Event()
    second_cancellation = asyncio.Event()
    broker = FakeBroker((FakeSubscriber("ocr", events),), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
        timeout_seconds=5.0,
    )

    async def declare_topology(_runtime: PipelineRuntime) -> None:
        events.append("runtime.declare.started")
        startup_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("runtime.declare.first_cancel")
            first_cancellation.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("runtime.declare.second_cancel")
            second_cancellation.set()
            raise

    async def verify_readiness(_runtime: PipelineRuntime) -> None:
        raise AssertionError("Readiness must not run after startup shutdown was requested.")

    monkeypatch.setattr(PipelineRuntime, "declare_topology", declare_topology)
    monkeypatch.setattr(PipelineRuntime, "verify_readiness", verify_readiness)
    stop_event = asyncio.Event()
    force_stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event, force_stop_event=force_stop_event))
    await asyncio.wait_for(startup_entered.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.wait_for(first_cancellation.wait(), timeout=1.0)
    assert not run_task.done()

    force_stop_event.set()
    await asyncio.wait_for(second_cancellation.wait(), timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert events.index("runtime.declare.second_cancel") < events.index("broker.stop")
    assert "health.running" not in events


async def test_worker_quiesces_all_consumers_then_drains_before_closing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    first = FakeSubscriber("first", events)
    second = FakeSubscriber("second", events)
    broker = FakeBroker((first, second), events)
    health = FakeHealthReporter(events)
    telegram = FakeTelegramSessionManager(events)
    runtime = _runtime(broker=broker, health_reporter=health, telegram_manager=telegram)
    _patch_startup(monkeypatch, events)
    stop_event = asyncio.Event()
    force_stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event, force_stop_event=force_stop_event))
    await asyncio.wait_for(health.ready.wait(), timeout=1.0)

    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    async def consume_delivery() -> None:
        async with runtime.consumer_operation("ocr") as admitted:
            assert admitted
            events.append("delivery.started")
            delivery_started.set()
            await release_delivery.wait()
            events.append("delivery.finished")

    delivery_task = asyncio.create_task(consume_delivery())
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.wait_for(asyncio.gather(first.stopped.wait(), second.stopped.wait()), timeout=1.0)

    async with runtime.consumer_operation("late-delivery") as admitted:
        assert not admitted
    assert "broker.stop" not in events
    assert "telegram.shutdown" not in events

    release_delivery.set()
    await asyncio.wait_for(delivery_task, timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert events.index("health.draining") < events.index("subscriber.first.stop")
    assert events.index("health.draining") < events.index("subscriber.second.stop")
    assert events.index("delivery.finished") < events.index("broker.stop")
    assert events.index("broker.stop") < events.index("telegram.shutdown")
    assert events.index("telegram.shutdown") < events.index("health.stop")


async def test_worker_quiesces_multiple_subscribers_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    all_started = asyncio.Event()
    release = asyncio.Event()
    subscribers: list[BlockingSubscriber] = []
    first = BlockingSubscriber(
        "first",
        events,
        all_started,
        release,
        peer_started=lambda: len(subscribers) == 2 and subscribers[1].stopped.is_set(),
    )
    second = BlockingSubscriber(
        "second",
        events,
        all_started,
        release,
        peer_started=lambda: subscribers[0].stopped.is_set(),
    )
    subscribers.extend((first, second))
    broker = FakeBroker(tuple(subscribers), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
    )
    _patch_startup(monkeypatch, events)
    stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event))
    await asyncio.wait_for(health.ready.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.wait_for(all_started.wait(), timeout=1.0)

    release.set()
    await asyncio.wait_for(run_task, timeout=1.0)
    assert events.index("subscriber.first.stop_started") < events.index("subscriber.first.stop_finished")
    assert events.index("subscriber.second.stop_started") < events.index("subscriber.first.stop_finished")


@pytest.mark.parametrize("force", [False, True], ids=["deadline", "second-signal"])
async def test_worker_bounds_or_forces_blocked_consumer_quiescing(
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    events: list[str] = []
    all_started = asyncio.Event()
    release = asyncio.Event()
    subscribers: list[BlockingSubscriber] = []
    first = BlockingSubscriber(
        "first",
        events,
        all_started,
        release,
        peer_started=lambda: len(subscribers) == 2 and subscribers[1].stopped.is_set(),
    )
    second = BlockingSubscriber(
        "second",
        events,
        all_started,
        release,
        peer_started=lambda: subscribers[0].stopped.is_set(),
    )
    subscribers.extend((first, second))
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=FakeBroker(tuple(subscribers), events),
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
        timeout_seconds=5.0 if force else 0.03,
    )
    _patch_startup(monkeypatch, events)
    stop_event = asyncio.Event()
    force_stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event, force_stop_event=force_stop_event))
    await asyncio.wait_for(health.ready.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.wait_for(all_started.wait(), timeout=1.0)
    if force:
        force_stop_event.set()

    await asyncio.wait_for(run_task, timeout=1.0)

    assert "subscriber.first.stop_finished" not in events
    assert "subscriber.second.stop_finished" not in events
    assert "broker.stop" in events
    assert events.index("broker.stop") < events.index("telegram.shutdown")


@pytest.mark.parametrize("force", [False, True], ids=["deadline", "second-signal"])
async def test_worker_cancels_inflight_delivery_at_global_deadline_or_force_signal(
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    events: list[str] = []
    subscriber = FakeSubscriber("ocr", events)
    broker = FakeBroker((subscriber,), events)
    health = FakeHealthReporter(events)
    runtime = _runtime(
        broker=broker,
        health_reporter=health,
        telegram_manager=FakeTelegramSessionManager(events),
        timeout_seconds=5.0 if force else 0.03,
    )
    _patch_startup(monkeypatch, events)
    stop_event = asyncio.Event()
    force_stop_event = asyncio.Event()

    run_task = asyncio.create_task(runtime.run(stop_event=stop_event, force_stop_event=force_stop_event))
    await asyncio.wait_for(health.ready.wait(), timeout=1.0)
    delivery_started = asyncio.Event()

    async def blocked_delivery() -> None:
        try:
            async with runtime.consumer_operation("ocr") as admitted:
                assert admitted
                delivery_started.set()
                await asyncio.Future()
        except asyncio.CancelledError:
            events.append("delivery.cancelled")
            raise

    delivery_task = asyncio.create_task(blocked_delivery())
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)
    stop_event.set()
    await asyncio.wait_for(subscriber.stopped.wait(), timeout=1.0)
    if force:
        force_stop_event.set()

    await asyncio.wait_for(run_task, timeout=1.0)
    assert delivery_task.cancelled()
    assert "delivery.cancelled" in events
    assert events.index("delivery.cancelled") < events.index("broker.stop")
