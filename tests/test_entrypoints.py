"""Tests for console entry points."""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from unittest.mock import patch

from memexpert.api.main import main as api_main
from memexpert.bot.main import main as bot_main
from memexpert.core.config import Settings
from memexpert.crawlers.telegram.main import main as telegram_crawler_main
from memexpert.scheduler.main import main as scheduler_main
from memexpert.workers.main import main as workers_main
from scripts.analytics import main as analytics_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_workers_main_runs_async_pipeline_runtime() -> None:
    with patch("memexpert.workers.main.asyncio.run") as asyncio_run:
        workers_main()

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()


def test_scheduler_main_runs_async_scheduler_runtime() -> None:
    with patch("memexpert.scheduler.main.asyncio.run") as asyncio_run:
        scheduler_main()

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()


def test_telegram_crawler_main_runs_async_crawler_runtime() -> None:
    with patch("memexpert.crawlers.telegram.main.asyncio.run") as asyncio_run:
        telegram_crawler_main()

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()


def test_telegram_crawler_console_script_is_registered() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["memexpert-telegram-crawler"] == "memexpert.crawlers.telegram.main:main"


def test_analytics_main_runs_refresh_trends_command() -> None:
    with (
        patch("scripts.analytics.argparse.ArgumentParser.parse_args") as parse_args,
        patch("scripts.analytics.asyncio.run") as asyncio_run,
    ):
        parse_args.return_value.command = "refresh-trends"
        parse_args.return_value.no_concurrently = False
        analytics_main()

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()
