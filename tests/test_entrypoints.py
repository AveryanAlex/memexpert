"""Tests for console entry point stubs."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from unittest.mock import patch

from memexpert.api.main import main as api_main
from memexpert.bot.main import main as bot_main
from memexpert.core.config import Settings
from memexpert.workers.main import main as workers_main

if TYPE_CHECKING:
    import pytest


def test_api_main_runs_uvicorn_with_factory_settings() -> None:
    with (
        patch("memexpert.api.main.get_settings", return_value=Settings(app_host="127.0.0.1", app_port=9001)),
        patch("uvicorn.run") as uvicorn_run,
    ):
        api_main()

    uvicorn_run.assert_called_once_with(
        "memexpert.api.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=9001,
    )


def test_bot_main_runs_async_bot_runtime() -> None:
    with patch("memexpert.bot.main.asyncio.run") as asyncio_run:
        bot_main()

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()


def test_workers_main_prints_placeholder_message(capsys: pytest.CaptureFixture[str]) -> None:
    workers_main()

    captured = capsys.readouterr()
    assert "memexpert-workers: not implemented yet" in captured.out
