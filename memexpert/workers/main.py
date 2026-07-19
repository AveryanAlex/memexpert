"""Console entry point for the background workers process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

from memexpert.workers.logging import configure_worker_logging
from memexpert.workers.pipeline_runtime import run_pipeline_runtime
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


logger = logging.getLogger(__name__)


class _SignalHandlingLoop(Protocol):
    """Small event-loop protocol kept concrete for simple test doubles."""

    def add_signal_handler(self, sig: signal.Signals, callback: Callable[..., object], *args: object) -> None: ...

    def remove_signal_handler(self, sig: signal.Signals) -> bool: ...


class WorkerSignalController:
    """Translate process signals into graceful and forced worker shutdown requests."""

    def __init__(self, *, loop: _SignalHandlingLoop | None = None) -> None:
        self.stop_event = asyncio.Event()
        self.force_stop_event = asyncio.Event()
        self._loop = loop
        self._registered_signals: list[signal.Signals] = []
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return

        loop = self._loop or asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self._request_stop, sig.name)
                self._registered_signals.append(sig)
        self._loop = loop
        self._installed = True

    def close(self) -> None:
        if self._loop is None:
            return
        for sig in self._registered_signals:
            with suppress(NotImplementedError, RuntimeError, ValueError):
                self._loop.remove_signal_handler(sig)
        self._registered_signals.clear()
        self._installed = False

    def _request_stop(self, signal_name: str) -> None:
        if self.stop_event.is_set():
            self.force_stop_event.set()
            logger.warning(
                "worker_shutdown_force_requested",
                extra={"event": "worker_shutdown_force_requested", "signal": signal_name},
            )
            return

        self.stop_event.set()
        logger.info(
            "worker_shutdown_requested",
            extra={"event": "worker_shutdown_requested", "signal": signal_name},
        )


async def run_worker_runtime(
    *,
    role: WorkerRole,
    signal_controller: WorkerSignalController | None = None,
) -> None:
    """Run one worker role with PID-1-safe SIGINT and SIGTERM handling."""

    configure_worker_logging()
    controller = signal_controller or WorkerSignalController()
    controller.install()
    try:
        await run_pipeline_runtime(
            role=role,
            stop_event=controller.stop_event,
            force_stop_event=controller.force_stop_event,
        )
    finally:
        controller.close()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the RabbitMQ-backed content-pipeline worker runtime."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        type=WorkerRole,
        choices=tuple(WorkerRole),
        default=WorkerRole.ALL,
        help="consumer/dependency role to run (default: all)",
    )
    args = parser.parse_args(argv)
    asyncio.run(run_worker_runtime(role=args.role))


__all__ = ["WorkerSignalController", "main", "run_worker_runtime"]


if __name__ == "__main__":
    main()
