"""Console entry point and process runtime for the Telegram crawler."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from memexpert.core.config import get_settings
from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.crawlers.telegram.log_config import configure_telegram_crawler_logging
from memexpert.crawlers.telegram.manager import TelegramSessionManager

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


logger = logging.getLogger(__name__)


class TelegramCrawlerControlSignal(StrEnum):
    """Process control events handled by the crawler runtime."""

    RELOAD = "reload"
    STOP = "stop"


class TelegramCrawlerManagerLike(Protocol):
    """Manager seam used by runtime tests to avoid Telethon/Postgres."""

    async def catch_up_all(self) -> Sequence[object]: ...

    async def start_live_all(self) -> None: ...

    async def reload(self) -> None: ...

    async def shutdown(self) -> None: ...


class TelegramCrawlerSignalControllerLike(Protocol):
    """Signal/event controller seam for process tests."""

    def install(self) -> None: ...

    async def wait(self) -> TelegramCrawlerControlSignal: ...

    def close(self) -> None: ...


class _TelegramCrawlerControlMonitor:
    """Continuously collect process control signals for startup and runtime checks."""

    def __init__(self, signal_controller: TelegramCrawlerSignalControllerLike) -> None:
        self._signal_controller = signal_controller
        self._queue: asyncio.Queue[TelegramCrawlerControlSignal] = asyncio.Queue()
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="telegram-crawler-control-monitor")

    async def wait(self) -> TelegramCrawlerControlSignal:
        if self._task is not None and self._task.done() and self._queue.empty():
            self._task.result()
        return await self._queue.get()

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        else:
            with suppress(Exception):
                self._task.result()

    def raise_if_failed(self) -> None:
        if self._task is not None and self._task.done():
            self._task.result()

    async def _run(self) -> None:
        while True:
            control_signal = await self._signal_controller.wait()
            if control_signal is TelegramCrawlerControlSignal.STOP:
                self._stop_requested.set()
            self._queue.put_nowait(control_signal)


class AsyncEngineLike(Protocol):
    """Minimal engine surface owned by this process runtime."""

    async def dispose(self) -> None: ...


class _SignalHandlingLoop(Protocol):
    def add_signal_handler(self, sig: signal.Signals, callback: Callable[..., object], *args: object) -> None: ...

    def remove_signal_handler(self, sig: signal.Signals) -> bool: ...


_SIGNAL_ACTIONS: tuple[tuple[signal.Signals, TelegramCrawlerControlSignal], ...] = (
    (signal.SIGHUP, TelegramCrawlerControlSignal.RELOAD),
    (signal.SIGINT, TelegramCrawlerControlSignal.STOP),
    (signal.SIGTERM, TelegramCrawlerControlSignal.STOP),
)


class TelegramCrawlerSignalController:
    """Register process signal handlers and expose them as async control events."""

    def __init__(self, *, loop: _SignalHandlingLoop | None = None) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[TelegramCrawlerControlSignal] = asyncio.Queue()
        self._registered_signals: list[signal.Signals] = []
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return

        loop = self._loop or asyncio.get_running_loop()
        for sig, control_signal in _SIGNAL_ACTIONS:
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self._request, control_signal, sig.name)
                self._registered_signals.append(sig)
        self._loop = loop
        self._installed = True

    async def wait(self) -> TelegramCrawlerControlSignal:
        return await self._queue.get()

    def close(self) -> None:
        if self._loop is None:
            return

        for sig in self._registered_signals:
            with suppress(NotImplementedError, RuntimeError, ValueError):
                self._loop.remove_signal_handler(sig)
        self._registered_signals.clear()
        self._installed = False

    def _request(self, control_signal: TelegramCrawlerControlSignal, signal_name: str) -> None:
        if control_signal is TelegramCrawlerControlSignal.RELOAD:
            logger.info(
                "telegram_crawler_reload_requested",
                extra={"event": "telegram_crawler_reload_requested", "signal": signal_name},
            )
        else:
            logger.info(
                "telegram_crawler_stop_requested",
                extra={"event": "telegram_crawler_stop_requested", "signal": signal_name},
            )
        self._queue.put_nowait(control_signal)


async def run_telegram_crawler_runtime(
    *,
    settings: Settings | None = None,
    engine: AsyncEngineLike | None = None,
    session_factory: AsyncSessionFactory | None = None,
    manager: TelegramCrawlerManagerLike | None = None,
    signal_controller: TelegramCrawlerSignalControllerLike | None = None,
) -> None:
    """Run catch-up once, start live listeners, then wait for process signals."""

    configure_telegram_crawler_logging()
    resolved_settings = settings or get_settings()
    owns_engine = engine is None
    resolved_engine: AsyncEngineLike | None = None
    manager_instance: TelegramCrawlerManagerLike | None = None
    controller: TelegramCrawlerSignalControllerLike | None = None
    control_monitor: _TelegramCrawlerControlMonitor | None = None

    try:
        resolved_engine = engine or build_async_engine(resolved_settings.database_url)
        if manager is None:
            resolved_session_factory = session_factory or build_async_session_factory(
                cast("AsyncEngine", resolved_engine),
            )
            manager_instance = TelegramSessionManager(
                settings=resolved_settings,
                session_factory=resolved_session_factory,
            )
        else:
            manager_instance = manager

        controller = signal_controller or TelegramCrawlerSignalController()
        controller.install()
        control_monitor = _TelegramCrawlerControlMonitor(controller)
        control_monitor.start()

        logger.info("telegram_crawler_runtime_starting", extra={"event": "telegram_crawler_runtime_starting"})
        await _yield_to_control_monitor(control_monitor)
        if control_monitor.stop_requested:
            return

        catchup_reports = await manager_instance.catch_up_all()
        logger.info(
            "telegram_crawler_catchup_completed",
            extra={"event": "telegram_crawler_catchup_completed", "catchup_reports": len(catchup_reports)},
        )
        await _yield_to_control_monitor(control_monitor)
        if control_monitor.stop_requested:
            return

        await manager_instance.start_live_all()
        logger.info("telegram_crawler_runtime_started", extra={"event": "telegram_crawler_runtime_started"})

        await _wait_for_control_signals(manager_instance, control_monitor)
    finally:
        if control_monitor is not None:
            await control_monitor.close()
        if controller is not None:
            controller.close()
        try:
            if manager_instance is not None:
                await manager_instance.shutdown()
        finally:
            if owns_engine and resolved_engine is not None:
                await resolved_engine.dispose()
            logger.info("telegram_crawler_runtime_stopped", extra={"event": "telegram_crawler_runtime_stopped"})


async def _yield_to_control_monitor(control_monitor: _TelegramCrawlerControlMonitor) -> None:
    await asyncio.sleep(0)
    control_monitor.raise_if_failed()


async def _wait_for_control_signals(
    manager: TelegramCrawlerManagerLike,
    control_monitor: _TelegramCrawlerControlMonitor,
) -> None:
    while True:
        control_signal = await control_monitor.wait()
        if control_signal is TelegramCrawlerControlSignal.STOP:
            return

        logger.info("telegram_crawler_reload_started", extra={"event": "telegram_crawler_reload_started"})
        await manager.reload()
        logger.info("telegram_crawler_reload_completed", extra={"event": "telegram_crawler_reload_completed"})


def main() -> None:
    """Run the Telegram crawler process."""

    asyncio.run(run_telegram_crawler_runtime())


__all__ = [
    "TelegramCrawlerControlSignal",
    "TelegramCrawlerSignalController",
    "main",
    "run_telegram_crawler_runtime",
]


if __name__ == "__main__":
    main()
