"""Console entry point and process runtime for the Telegram crawler."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from memexpert.core.config import get_settings
from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.crawlers.telegram.log_config import configure_telegram_crawler_logging
from memexpert.crawlers.telegram.manager import TelegramCrawlerReloadResult, TelegramSessionManager
from memexpert.runtime_health import RuntimeHealthReporter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PendingConfiguration:
    """A durable snapshot whose catch-up/listener work needs another attempt."""

    snapshot: object


@dataclass(frozen=True, slots=True)
class _PendingFullReload:
    """A durable snapshot whose full client/listener rebuild failed."""

    snapshot: object


@dataclass(frozen=True, slots=True)
class _CrawlerReconcileOutcome:
    """Updated applied state plus whether queued reload signals are redundant."""

    applied_configuration_snapshot: object
    full_reload_performed: bool


class _CrawlerStopRequested(Exception):
    """Internal control flow used to cancel startup/reconciliation cleanly."""


class TelegramCrawlerControlSignal(StrEnum):
    """Process control events handled by the crawler runtime."""

    RELOAD = "reload"
    STOP = "stop"


class TelegramCrawlerManagerLike(Protocol):
    """Manager seam used by runtime tests to avoid Telethon/Postgres."""

    async def configuration_snapshot(self) -> object: ...

    async def reload(self) -> TelegramCrawlerReloadResult: ...

    async def retry_incomplete(self) -> TelegramCrawlerReloadResult: ...

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

    async def wait_for_stop(self) -> None:
        await self._stop_requested.wait()

    async def discard_pending_reload_requests(self) -> None:
        """Coalesce reload signals queued while reconciliation was already running."""

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop_pending = False
        while True:
            try:
                control_signal = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if control_signal is TelegramCrawlerControlSignal.STOP:
                stop_pending = True
        if stop_pending:
            self._queue.put_nowait(TelegramCrawlerControlSignal.STOP)

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
    health_reporter: RuntimeHealthReporter | None = None,
) -> None:
    """Run catch-up once, start live listeners, then wait for process signals."""

    configure_telegram_crawler_logging()
    resolved_settings = settings or get_settings()
    owns_engine = engine is None
    resolved_engine: AsyncEngineLike | None = None
    manager_instance: TelegramCrawlerManagerLike | None = None
    controller: TelegramCrawlerSignalControllerLike | None = None
    control_monitor: _TelegramCrawlerControlMonitor | None = None

    if health_reporter is not None:
        await health_reporter.start()
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
        try:
            configuration_snapshot = await _await_operation_or_stop(
                manager_instance.configuration_snapshot(),
                control_monitor,
            )
            reload_result = cast(
                "TelegramCrawlerReloadResult",
                await _await_operation_or_stop(manager_instance.reload(), control_monitor),
            )
        except _CrawlerStopRequested:
            return
        logger.info(
            "telegram_crawler_catchup_completed",
            extra={
                "event": "telegram_crawler_catchup_completed",
                "catchup_reports": len(reload_result.catchup_reports),
                "failed_session_names": reload_result.failed_session_names,
                "retry_required": reload_result.retry_required,
            },
        )
        logger.info("telegram_crawler_runtime_started", extra={"event": "telegram_crawler_runtime_started"})
        if health_reporter is not None:
            health_reporter.mark_ready()

        await _wait_for_control_signals(
            manager_instance,
            control_monitor,
            reconcile_interval_seconds=resolved_settings.crawler_reconcile_interval_seconds,
            applied_configuration_snapshot=(
                _PendingConfiguration(configuration_snapshot)
                if reload_result.retry_required
                else configuration_snapshot
            ),
        )
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
            if health_reporter is not None:
                await health_reporter.stop()


async def _await_operation_or_stop(
    operation: Awaitable[object],
    control_monitor: _TelegramCrawlerControlMonitor,
) -> object:
    """Await one manager operation while allowing SIGINT/SIGTERM cancellation."""

    operation_task = asyncio.ensure_future(operation)
    stop_task = asyncio.create_task(control_monitor.wait_for_stop())
    done, _pending = await asyncio.wait(
        (operation_task, stop_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if stop_task in done:
        operation_task.cancel()
        _ = await asyncio.gather(operation_task, return_exceptions=True)
        raise _CrawlerStopRequested

    stop_task.cancel()
    _ = await asyncio.gather(stop_task, return_exceptions=True)
    return operation_task.result()


async def _wait_for_control_signals(
    manager: TelegramCrawlerManagerLike,
    control_monitor: _TelegramCrawlerControlMonitor,
    *,
    reconcile_interval_seconds: float,
    applied_configuration_snapshot: object,
) -> None:
    observed_snapshot = applied_configuration_snapshot
    while True:
        try:
            control_signal = await asyncio.wait_for(
                control_monitor.wait(),
                timeout=reconcile_interval_seconds,
            )
        except TimeoutError:
            if control_monitor.stop_requested:
                return
            try:
                reconcile_outcome = await _reconcile_if_configuration_changed(
                    manager,
                    control_monitor=control_monitor,
                    applied_configuration_snapshot=observed_snapshot,
                )
            except _CrawlerStopRequested:
                return
            observed_snapshot = reconcile_outcome.applied_configuration_snapshot
            if reconcile_outcome.full_reload_performed:
                await control_monitor.discard_pending_reload_requests()
            supervise_live_listeners = getattr(manager, "supervise_live_listeners", None)
            if supervise_live_listeners is not None:
                try:
                    failed_listener_names = await _await_operation_or_stop(
                        supervise_live_listeners(),
                        control_monitor,
                    )
                except _CrawlerStopRequested:
                    return
                except Exception:  # noqa: BLE001 - the next reconcile tick retries supervision.
                    logger.exception(
                        "telegram_live_listener_supervision_failed",
                        extra={"event": "telegram_live_listener_supervision_failed"},
                    )
                else:
                    if failed_listener_names:
                        logger.warning(
                            "telegram_live_listener_restart_failed",
                            extra={
                                "event": "telegram_live_listener_restart_failed",
                                "failed_session_names": failed_listener_names,
                                "retryable": True,
                            },
                        )
            continue
        if control_signal is TelegramCrawlerControlSignal.STOP:
            return

        logger.info("telegram_crawler_reload_started", extra={"event": "telegram_crawler_reload_started"})
        try:
            snapshot_before_reload = await _await_operation_or_stop(
                manager.configuration_snapshot(),
                control_monitor,
            )
        except _CrawlerStopRequested:
            return
        except Exception:  # noqa: BLE001 - a later poll or signal can retry the snapshot read.
            logger.exception(
                "telegram_crawler_reload_failed",
                extra={"event": "telegram_crawler_reload_failed"},
            )
            observed_snapshot = _PendingFullReload(observed_snapshot)
            continue
        try:
            reload_result = cast(
                "TelegramCrawlerReloadResult",
                await _await_operation_or_stop(manager.reload(), control_monitor),
            )
        except _CrawlerStopRequested:
            return
        except Exception:  # noqa: BLE001 - a later poll or signal can retry durable desired state.
            logger.exception(
                "telegram_crawler_reload_failed",
                extra={"event": "telegram_crawler_reload_failed"},
            )
            observed_snapshot = _PendingFullReload(snapshot_before_reload)
            continue
        observed_snapshot = (
            _PendingConfiguration(snapshot_before_reload) if reload_result.retry_required else snapshot_before_reload
        )
        await control_monitor.discard_pending_reload_requests()
        logger.info(
            "telegram_crawler_reload_completed",
            extra={
                "event": "telegram_crawler_reload_completed",
                "catchup_reports": len(reload_result.catchup_reports),
                "failed_session_names": reload_result.failed_session_names,
                "retry_required": reload_result.retry_required,
            },
        )


async def _reconcile_if_configuration_changed(
    manager: TelegramCrawlerManagerLike,
    *,
    control_monitor: _TelegramCrawlerControlMonitor,
    applied_configuration_snapshot: object,
) -> _CrawlerReconcileOutcome:
    try:
        current_snapshot = await _await_operation_or_stop(
            manager.configuration_snapshot(),
            control_monitor,
        )
    except _CrawlerStopRequested:
        raise
    except Exception:  # noqa: BLE001 - a later poll can retry the durable snapshot read.
        logger.exception(
            "telegram_crawler_reconcile_failed",
            extra={"event": "telegram_crawler_reconcile_failed"},
        )
        return _CrawlerReconcileOutcome(
            applied_configuration_snapshot=applied_configuration_snapshot,
            full_reload_performed=False,
        )

    pending_configuration = (
        applied_configuration_snapshot if isinstance(applied_configuration_snapshot, _PendingConfiguration) else None
    )
    pending_full_reload = (
        applied_configuration_snapshot if isinstance(applied_configuration_snapshot, _PendingFullReload) else None
    )
    if (
        pending_configuration is None
        and pending_full_reload is None
        and current_snapshot == applied_configuration_snapshot
    ):
        return _CrawlerReconcileOutcome(
            applied_configuration_snapshot=applied_configuration_snapshot,
            full_reload_performed=False,
        )

    retry_incomplete = (
        pending_configuration is not None
        and pending_full_reload is None
        and current_snapshot == pending_configuration.snapshot
    )
    logger.info(
        "telegram_crawler_reconcile_started",
        extra={
            "event": "telegram_crawler_reconcile_started",
            "retry_incomplete": retry_incomplete,
        },
    )
    try:
        reload_result = cast(
            "TelegramCrawlerReloadResult",
            await _await_operation_or_stop(
                manager.retry_incomplete() if retry_incomplete else manager.reload(),
                control_monitor,
            ),
        )
    except _CrawlerStopRequested:
        raise
    except Exception:  # noqa: BLE001 - durable desired state makes the next poll a safe retry.
        logger.exception(
            "telegram_crawler_reconcile_failed",
            extra={"event": "telegram_crawler_reconcile_failed"},
        )
        failed_snapshot = applied_configuration_snapshot if retry_incomplete else _PendingFullReload(current_snapshot)
        return _CrawlerReconcileOutcome(
            applied_configuration_snapshot=failed_snapshot,
            full_reload_performed=False,
        )

    logger.info(
        "telegram_crawler_reconcile_completed",
        extra={
            "event": "telegram_crawler_reconcile_completed",
            "catchup_reports": len(reload_result.catchup_reports),
            "failed_session_names": reload_result.failed_session_names,
            "retry_required": reload_result.retry_required,
        },
    )
    # Keep the snapshot captured before reconciliation. A concurrent admin
    # change will differ on the next poll and trigger another pass.
    applied_snapshot = _PendingConfiguration(current_snapshot) if reload_result.retry_required else current_snapshot
    return _CrawlerReconcileOutcome(
        applied_configuration_snapshot=applied_snapshot,
        full_reload_performed=not retry_incomplete,
    )


def main() -> None:
    """Run the Telegram crawler process."""

    settings = get_settings()
    health_reporter = RuntimeHealthReporter.from_settings(
        settings,
        service="memexpert-telegram-crawler",
    )
    asyncio.run(run_telegram_crawler_runtime(settings=settings, health_reporter=health_reporter))


__all__ = [
    "TelegramCrawlerControlSignal",
    "TelegramCrawlerSignalController",
    "main",
    "run_telegram_crawler_runtime",
]


if __name__ == "__main__":
    main()
