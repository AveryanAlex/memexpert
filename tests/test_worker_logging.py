"""Structured logging coverage for the background worker process."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING

import memexpert.workers.main as worker_main_module
from memexpert.workers.logging import configure_worker_logging
from memexpert.workers.main import WorkerSignalController, run_worker_runtime
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    import pytest
    from _pytest.capture import CaptureFixture


def test_configure_worker_logging_emits_structured_info_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    root_logger = logging.getLogger("memexpert-test-worker-root")
    original_get_logger = logging.getLogger

    def fake_get_logger(name: str | None = None) -> logging.Logger:
        return root_logger if name is None else original_get_logger(name)

    monkeypatch.setattr("logging.getLogger", fake_get_logger)
    root_logger.handlers.clear()
    root_logger.setLevel(logging.NOTSET)
    root_logger.propagate = False

    try:
        configure_worker_logging()

        assert len(root_logger.handlers) == 1
        handler = root_logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout
        assert root_logger.level == logging.INFO

        root_logger.info(
            "worker_shutdown_started",
            extra={
                "event": "worker_shutdown_started",
                "signal": "SIGTERM",
                "role": "ocr",
                "timeout_seconds": 210.0,
                "active_deliveries": 2,
                "consumer_count": 3,
                "drain_completed": False,
                "remaining_tasks": 1,
                "stage": "ocr",
                "meme_file_id": "019f5c1a-5fd6-7000-8000-000000000001",
                "message_id": "pipeline-message-1",
                "dependency": "paddleocr",
                "normalized_reason": "worker_shutdown",
            },
        )

        captured = capsys.readouterr()
        assert captured.err == ""
        payload = json.loads(captured.out)
        assert payload == {
            "level": "INFO",
            "logger": "memexpert-test-worker-root",
            "message": "worker_shutdown_started",
            "event": "worker_shutdown_started",
            "signal": "SIGTERM",
            "role": "ocr",
            "timeout_seconds": 210.0,
            "active_deliveries": 2,
            "consumer_count": 3,
            "drain_completed": False,
            "remaining_tasks": 1,
            "stage": "ocr",
            "meme_file_id": "019f5c1a-5fd6-7000-8000-000000000001",
            "message_id": "pipeline-message-1",
            "dependency": "paddleocr",
            "normalized_reason": "worker_shutdown",
        }
    finally:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.NOTSET)
        root_logger.propagate = True


async def test_worker_configures_logging_before_signal_and_pipeline_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingSignalController(WorkerSignalController):
        def __init__(self) -> None:
            super().__init__()

        def install(self) -> None:
            events.append("signals.install")

        def close(self) -> None:
            events.append("signals.close")

    async def fake_run_pipeline_runtime(**_kwargs: object) -> None:
        events.append("pipeline.run")

    monkeypatch.setattr(
        worker_main_module,
        "configure_worker_logging",
        lambda: events.append("logging.configure"),
    )
    monkeypatch.setattr(worker_main_module, "run_pipeline_runtime", fake_run_pipeline_runtime)

    await run_worker_runtime(
        role=WorkerRole.OCR,
        signal_controller=RecordingSignalController(),
    )

    assert events == [
        "logging.configure",
        "signals.install",
        "pipeline.run",
        "signals.close",
    ]
