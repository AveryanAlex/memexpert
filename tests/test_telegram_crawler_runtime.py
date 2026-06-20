"""Focused tests for the dedicated Telegram crawler process runtime."""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, Any

import pytest

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.main import (
    TelegramCrawlerControlSignal,
    TelegramCrawlerSignalController,
    run_telegram_crawler_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class FakeEngine:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("engine.dispose")


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
    ) -> None:
        self._events = events
        self._signal_controller = signal_controller
        self._signals_after_start = signals_after_start
        self._signals_after_reload = signals_after_reload

    async def catch_up_all(self) -> Sequence[object]:
        self._events.append("manager.catch_up_all")
        return [object(), object()]

    async def start_live_all(self) -> None:
        self._events.append("manager.start_live_all")
        for control_signal in self._signals_after_start:
            if self._signal_controller is not None:
                self._signal_controller.request(control_signal)

    async def reload(self) -> None:
        self._events.append("manager.reload")
        for control_signal in self._signals_after_reload:
            if self._signal_controller is not None:
                self._signal_controller.request(control_signal)

    async def shutdown(self) -> None:
        self._events.append("manager.shutdown")


class FailingShutdownManager(FakeManager):
    async def shutdown(self) -> None:
        self._events.append("manager.shutdown")
        raise RuntimeError("shutdown failed")


class FailingCatchupManager(FakeManager):
    async def catch_up_all(self) -> Sequence[object]:
        self._events.append("manager.catch_up_all")
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
        "manager.catch_up_all",
        "manager.start_live_all",
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
        "manager.catch_up_all",
        "manager.start_live_all",
        "signal.wait:stop",
        "signal.close",
        "manager.shutdown",
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
        "manager.catch_up_all",
        "manager.start_live_all",
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
        "manager.catch_up_all",
        "signal.close",
        "manager.shutdown",
        "engine.dispose",
    ]


@pytest.mark.asyncio
async def test_runtime_skips_live_start_when_stop_is_requested_during_catchup() -> None:
    events: list[str] = []
    signal_controller = FakeSignalController(events)

    class StopDuringCatchupManager(FakeManager):
        async def catch_up_all(self) -> Sequence[object]:
            self._events.append("manager.catch_up_all")
            signal_controller.request(TelegramCrawlerControlSignal.STOP)
            await asyncio.sleep(0)
            return []

    await run_telegram_crawler_runtime(
        settings=Settings(),
        engine=FakeEngine(events),
        manager=StopDuringCatchupManager(events),
        signal_controller=signal_controller,
    )

    assert events == [
        "signal.install",
        "manager.catch_up_all",
        "signal.wait:stop",
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
        "manager.catch_up_all",
        "manager.start_live_all",
        "signal.wait:reload",
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
