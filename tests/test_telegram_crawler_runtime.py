"""Focused tests for the dedicated Telegram crawler process runtime."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.main import (
    TelegramCrawlerControlSignal,
    TelegramCrawlerSignalController,
    run_telegram_crawler_runtime,
)
from memexpert.crawlers.telegram.manager import TelegramCrawlerReloadResult
from memexpert.crawlers.telegram.runtime import CrawlerCatchupReport

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class FakeEngine:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("engine.dispose")


class FakeHealthReporter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def start(self) -> None:
        self._events.append("health.start")

    def mark_ready(self) -> None:
        self._events.append("health.ready")

    async def stop(self) -> None:
        self._events.append("health.stop")


class FakeSignalController:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._control_signals: asyncio.Queue[TelegramCrawlerControlSignal] = asyncio.Queue()

    def install(self) -> None:
        self._events.append("signal.install")

    async def wait(self) -> TelegramCrawlerControlSignal:
        control_signal = await self._control_signals.get()
        self._events.append(f"signal.wait:{control_signal.value}")
        return control_signal

    def close(self) -> None:
        self._events.append("signal.close")

    def request(self, control_signal: TelegramCrawlerControlSignal) -> None:
        self._control_signals.put_nowait(control_signal)


class FakeManager:
    def __init__(
        self,
        events: list[str],
        *,
        signal_controller: FakeSignalController | None = None,
        signals_after_start: Sequence[TelegramCrawlerControlSignal] = (),
        signals_after_reload: Sequence[TelegramCrawlerControlSignal] = (),
        configuration_snapshots: Sequence[object] = ("configuration",),
        reload_results: Sequence[TelegramCrawlerReloadResult] = (
            TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=()),
        ),
        retry_results: Sequence[TelegramCrawlerReloadResult] = (
            TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=()),
        ),
        signals_after_retry: Sequence[TelegramCrawlerControlSignal] = (),
    ) -> None:
        self._events = events
        self._signal_controller = signal_controller
        self._signals_after_start = signals_after_start
        self._signals_after_reload = signals_after_reload
        self._configuration_snapshots = tuple(configuration_snapshots)
        self._configuration_snapshot_index = 0
        self._reload_results = tuple(reload_results)
        self._reload_index = 0
        self._retry_results = tuple(retry_results)
        self._retry_index = 0
        self._signals_after_retry = signals_after_retry

    async def configuration_snapshot(self) -> object:
        self._events.append("manager.configuration_snapshot")
        index = min(self._configuration_snapshot_index, len(self._configuration_snapshots) - 1)
        self._configuration_snapshot_index += 1
        return self._configuration_snapshots[index]

    async def reload(
        self,
        *,
        on_listeners_ready: Callable[[], None] | None = None,
    ) -> TelegramCrawlerReloadResult:
        self._events.append("manager.reload")
        if on_listeners_ready is not None:
            on_listeners_ready()
        signals = self._signals_after_start if self._reload_index == 0 else self._signals_after_reload
        result_index = min(self._reload_index, len(self._reload_results) - 1)
        self._reload_index += 1
        self._schedule_signals(signals)
        return self._reload_results[result_index]

    async def retry_incomplete(self) -> TelegramCrawlerReloadResult:
        self._events.append("manager.retry_incomplete")
        result_index = min(self._retry_index, len(self._retry_results) - 1)
        self._retry_index += 1
        self._schedule_signals(self._signals_after_retry)
        return self._retry_results[result_index]

    async def process_backfill_jobs(self) -> int:
        return 0

    def _schedule_signals(self, signals: Sequence[TelegramCrawlerControlSignal]) -> None:
        if self._signal_controller is None or not signals:
            return

        async def _emit() -> None:
            await asyncio.sleep(0)
            for control_signal in signals:
                if self._signal_controller is not None:
                    self._signal_controller.request(control_signal)

        _ = asyncio.create_task(_emit())

    async def shutdown(self) -> None:
        self._events.append("manager.shutdown")


class FailingShutdownManager(FakeManager):
    async def shutdown(self) -> None:
        self._events.append("manager.shutdown")
        raise RuntimeError("shutdown failed")


class FailingCatchupManager(FakeManager):
    async def reload(
        self,
        *,
        on_listeners_ready: Callable[[], None] | None = None,
    ) -> TelegramCrawlerReloadResult:
        self._events.append("manager.reload")
        if on_listeners_ready is not None:
            on_listeners_ready()
        raise RuntimeError("catch-up failed")


class FakeLoop:
    def __init__(self) -> None:
        self.added: list[tuple[signal.Signals, Callable[..., object], tuple[object, ...]]] = []
        self.removed: list[signal.Signals] = []

    def add_signal_handler(self, sig: signal.Signals, callback: Callable[..., object], *args: object) -> None:
        self.added.append((sig, callback, args))

    def remove_signal_handler(self, sig: signal.Signals) -> bool:
        self.removed.append(sig)
        return True


def test_reload_result_retries_transient_errors_but_not_malformed_messages() -> None:
    now = datetime.now(UTC)

    transient = TelegramCrawlerReloadResult(
        catchup_reports=(
            CrawlerCatchupReport(
                session_name="primary",
                channel_id="channel",
                started_at=now,
                finished_at=now,
                errors=("provider_unavailable:temporary outage",),
            ),
        ),
        failed_session_names=(),
    )
    malformed = TelegramCrawlerReloadResult(
        catchup_reports=(
            CrawlerCatchupReport(
                session_name="primary",
                channel_id="channel",
                started_at=now,
                finished_at=now,
                errors=("download_malformed:42:bad payload",),
            ),
        ),
        failed_session_names=(),
    )

    assert transient.retry_required is True
    assert malformed.retry_required is False


@pytest.mark.asyncio
async def test_runtime_builds_manager_and_disposes_owned_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    settings = Settings()
    engine = FakeEngine(events)
    session_factory = object()
    signal_controller = FakeSignalController(events)

    def fake_build_async_engine(database_url: str) -> FakeEngine:
        events.append("build_async_engine")
        assert database_url == settings.database_url
        return engine

    def fake_build_async_session_factory(actual_engine: object) -> object:
        events.append("build_async_session_factory")
        assert actual_engine is engine
        return session_factory

    def fake_get_settings() -> Settings:
        events.append("get_settings")
        return settings

    class BuiltManager(FakeManager):
        def __init__(self, *, settings: Settings, session_factory: object) -> None:
            events.append("manager.build")
            assert settings is settings_fixture
            assert session_factory is session_factory_fixture
            super().__init__(
                events,
                signal_controller=signal_controller,
                signals_after_start=[TelegramCrawlerControlSignal.STOP],
            )

    settings_fixture = settings
    session_factory_fixture = session_factory

    monkeypatch.setattr("memexpert.crawlers.telegram.main.get_settings", fake_get_settings)
    monkeypatch.setattr("memexpert.crawlers.telegram.main.build_async_engine", fake_build_async_engine)
    monkeypatch.setattr(
        "memexpert.crawlers.telegram.main.build_async_session_factory",
        fake_build_async_session_factory,
    )
    monkeypatch.setattr("memexpert.crawlers.telegram.main.TelegramSessionManager", BuiltManager)

    await run_telegram_crawler_runtime(
        signal_controller=signal_controller,
    )

    assert events == [
        "get_settings",
        "build_async_engine",
        "build_async_session_factory",
        "manager.build",
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
        "engine.dispose",
    ]


@pytest.mark.asyncio
async def test_telegram_crawler_runtime_uses_injected_fakes_for_startup_and_shutdown() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_start=[TelegramCrawlerControlSignal.STOP],
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_marks_health_ready_when_live_listeners_start() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_start=[TelegramCrawlerControlSignal.STOP],
        ),
        signal_controller=signal_controller,
        health_reporter=FakeHealthReporter(events),
    )

    assert events == [
        "health.start",
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "health.ready",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
        "health.stop",
    ]


@pytest.mark.asyncio
async def test_runtime_disposes_owned_engine_when_shutdown_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    engine = FakeEngine(events)
    signal_controller = FakeSignalController(events)

    def fake_build_async_engine(database_url: str) -> FakeEngine:
        events.append("build_async_engine")
        assert database_url == Settings().database_url
        return engine

    monkeypatch.setattr("memexpert.crawlers.telegram.main.build_async_engine", fake_build_async_engine)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await run_telegram_crawler_runtime(
            settings=Settings(),
            manager=FailingShutdownManager(
                events,
                signal_controller=signal_controller,
                signals_after_start=[TelegramCrawlerControlSignal.STOP],
            ),
            signal_controller=signal_controller,
        )

    assert events == [
        "build_async_engine",
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
        "engine.dispose",
    ]


@pytest.mark.asyncio
async def test_runtime_shutdown_and_owned_engine_dispose_run_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    engine = FakeEngine(events)
    signal_controller = FakeSignalController(events)

    def fake_build_async_engine(database_url: str) -> FakeEngine:
        events.append("build_async_engine")
        assert database_url == Settings().database_url
        return engine

    monkeypatch.setattr("memexpert.crawlers.telegram.main.build_async_engine", fake_build_async_engine)

    with pytest.raises(RuntimeError, match="catch-up failed"):
        await run_telegram_crawler_runtime(
            settings=Settings(),
            manager=FailingCatchupManager(events),
            signal_controller=signal_controller,
        )

    assert events == [
        "build_async_engine",
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.close",
        "manager.shutdown",
        "engine.dispose",
    ]


@pytest.mark.asyncio
async def test_runtime_cancels_startup_reconcile_when_stop_is_requested() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    class StopDuringCatchupManager(FakeManager):
        async def reload(
            self,
            *,
            on_listeners_ready: Callable[[], None] | None = None,
        ) -> TelegramCrawlerReloadResult:
            self._events.append("manager.reload")
            if on_listeners_ready is not None:
                on_listeners_ready()
            signal_controller.request(TelegramCrawlerControlSignal.STOP)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._events.append("manager.reload.cancelled")
                raise
            raise AssertionError("startup reconcile should be cancelled")

    await run_telegram_crawler_runtime(
        settings=Settings(),
        engine=FakeEngine(events),
        manager=StopDuringCatchupManager(events),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "manager.reload.cancelled",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_telegram_crawler_runtime_reloads_on_sighup_event_then_stops() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_start=[TelegramCrawlerControlSignal.RELOAD],
            signals_after_reload=[TelegramCrawlerControlSignal.STOP],
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:reload",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_survives_sighup_reload_failure_and_waits_for_stop() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    class _FailSecondReloadManager(FakeManager):
        async def reload(
            self,
            *,
            on_listeners_ready: Callable[[], None] | None = None,
        ) -> TelegramCrawlerReloadResult:
            if self._reload_index == 1:
                self._events.append("manager.reload")
                self._reload_index += 1
                raise RuntimeError("reload failed")
            return await super().reload(on_listeners_ready=on_listeners_ready)

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=_FailSecondReloadManager(
            events,
            signal_controller=signal_controller,
            signals_after_start=[TelegramCrawlerControlSignal.RELOAD],
            signals_after_reload=[TelegramCrawlerControlSignal.STOP],
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:reload",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_reconciles_changed_configuration_without_signal() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_reload=[TelegramCrawlerControlSignal.RELOAD, TelegramCrawlerControlSignal.STOP],
            configuration_snapshots=("before", "after"),
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_leaves_backfill_jobs_to_the_telegram_worker_role() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    class _BackfillOwnershipManager(FakeManager):
        async def supervise_live_listeners(self) -> tuple[str, ...]:
            self._events.append("manager.supervise_live_listeners")
            signal_controller.request(TelegramCrawlerControlSignal.STOP)
            return ()

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=_BackfillOwnershipManager(events),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.supervise_live_listeners",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_supervises_live_listeners_without_configuration_change() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    class _ListenerSupervisingManager(FakeManager):
        async def supervise_live_listeners(self) -> tuple[str, ...]:
            self._events.append("manager.supervise_live_listeners")
            signal_controller.request(TelegramCrawlerControlSignal.STOP)
            return ()

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=_ListenerSupervisingManager(events),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.supervise_live_listeners",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_retries_incomplete_startup_catchup_without_configuration_change() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_retry=[TelegramCrawlerControlSignal.STOP],
            reload_results=(TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=("primary",)),),
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.retry_incomplete",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_uses_full_reload_when_configuration_changes_while_retry_is_pending() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_reload=[TelegramCrawlerControlSignal.STOP],
            configuration_snapshots=("before", "after"),
            reload_results=(
                TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=("primary",)),
                TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=()),
            ),
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_runtime_preserves_sighup_received_during_incomplete_retry() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    await run_telegram_crawler_runtime(
        settings=Settings(crawler_reconcile_interval_seconds=0.001),
        engine=FakeEngine(events),
        manager=FakeManager(
            events,
            signal_controller=signal_controller,
            signals_after_reload=[TelegramCrawlerControlSignal.STOP],
            signals_after_retry=[TelegramCrawlerControlSignal.RELOAD],
            reload_results=(
                TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=("primary",)),
                TelegramCrawlerReloadResult(catchup_reports=(), failed_session_names=()),
            ),
        ),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.configuration_snapshot",
        "manager.reload",
        "manager.configuration_snapshot",
        "manager.retry_incomplete",
        "signal.wait:reload",
        "manager.configuration_snapshot",
        "manager.reload",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
    ]


@pytest.mark.asyncio
async def test_telegram_crawler_signal_controller_registers_and_removes_handlers() -> None:
    loop = FakeLoop()
    controller = TelegramCrawlerSignalController(loop=loop)

    controller.install()

    assert [registered[0] for registered in loop.added] == [signal.SIGHUP, signal.SIGINT, signal.SIGTERM]

    reload_callback, reload_args = _handler_for(loop, signal.SIGHUP)
    reload_callback(*reload_args)
    assert await asyncio.wait_for(controller.wait(), timeout=0.1) is TelegramCrawlerControlSignal.RELOAD

    stop_callback, stop_args = _handler_for(loop, signal.SIGTERM)
    stop_callback(*stop_args)
    assert await asyncio.wait_for(controller.wait(), timeout=0.1) is TelegramCrawlerControlSignal.STOP

    controller.close()

    assert loop.removed == [signal.SIGHUP, signal.SIGINT, signal.SIGTERM]


def _handler_for(loop: FakeLoop, sig: signal.Signals) -> tuple[Callable[..., object], tuple[Any, ...]]:
    for registered_signal, callback, args in loop.added:
        if registered_signal is sig:
            return callback, args
    raise AssertionError(f"Signal {sig!r} was not registered.")
