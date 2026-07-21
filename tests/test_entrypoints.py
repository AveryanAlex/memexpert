"""Tests for console entry points."""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import BotCommandScopeAllPrivateChats

import memexpert.bot.commands as bot_commands_module
from memexpert.api.main import main as api_main
from memexpert.bot.commands import COMMAND_DEFINITIONS
from memexpert.bot.main import main as bot_main
from memexpert.bot.main import run_bot
from memexpert.core.config import Settings
from memexpert.crawlers.telegram.main import main as telegram_crawler_main
from memexpert.scheduler.main import main as scheduler_main
from memexpert.workers.main import main as workers_main
from memexpert.workers.roles import WorkerRole
from scripts.analytics import main as analytics_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _RuntimeBotSession:
    def __init__(self, events: list[str]) -> None:
        self.closed = False
        self._events = events

    async def close(self) -> None:
        self.closed = True
        self._events.append("close")


class _RuntimeBot:
    def __init__(self, *, registration_error: Exception | None = None) -> None:
        self.events: list[str] = []
        self.registration_error = registration_error
        self.session = _RuntimeBotSession(self.events)
        self.commands: list[Any] | None = None
        self.scope: Any = None

    async def set_my_commands(
        self,
        commands: list[Any],
        *,
        scope: Any = None,
        language_code: str | None = None,
        request_timeout: int | None = None,
    ) -> bool:
        _ = (language_code, request_timeout)
        self.events.append("register")
        if self.registration_error is not None:
            raise self.registration_error
        self.commands = list(commands)
        self.scope = scope
        return True


class _RuntimeDispatcher:
    def __init__(self, expected_bot: _RuntimeBot) -> None:
        self.expected_bot = expected_bot
        self.polling_started = False

    async def start_polling(self, bot: object) -> None:
        assert bot is self.expected_bot
        assert self.expected_bot.events == ["register"]
        self.polling_started = True
        self.expected_bot.events.append("poll")


def test_api_main_runs_uvicorn_with_factory_settings() -> None:
    startup_events: list[str] = []
    log_config = {"version": 1}

    def build_log_config() -> dict[str, int]:
        startup_events.append("logging.configure")
        return log_config

    with (
        patch("memexpert.api.main.build_uvicorn_logging_config", side_effect=build_log_config),
        patch("memexpert.api.main.get_settings", return_value=Settings(app_host="127.0.0.1", app_port=9001)),
        patch("uvicorn.run", side_effect=lambda *_args, **_kwargs: startup_events.append("uvicorn.run")) as uvicorn_run,
    ):
        api_main()

    assert startup_events == ["logging.configure", "uvicorn.run"]
    uvicorn_run.assert_called_once_with(
        "memexpert.api.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=9001,
        log_config=log_config,
    )


def test_bot_main_runs_async_bot_runtime() -> None:
    with patch("memexpert.bot.main.asyncio.run") as asyncio_run:
        bot_main()

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()


@pytest.mark.asyncio
async def test_run_bot_registers_commands_before_polling() -> None:
    bot = _RuntimeBot()
    dispatcher = _RuntimeDispatcher(bot)

    with (
        patch("memexpert.bot.main.build_bot", return_value=bot),
        patch("memexpert.bot.main.build_dispatcher", return_value=dispatcher),
    ):
        await run_bot(settings=Settings())

    assert dispatcher.polling_started
    assert bot.session.closed
    assert bot.events == ["register", "poll", "close"]
    assert [command.command for command in bot.commands or []] == [name for name, _ in COMMAND_DEFINITIONS]
    assert isinstance(bot.scope, BotCommandScopeAllPrivateChats)


@pytest.mark.asyncio
async def test_run_bot_command_registration_failure_logs_raises_and_skips_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _RuntimeBot(registration_error=RuntimeError("setMyCommands failed"))
    dispatcher = _RuntimeDispatcher(bot)
    exception_calls: list[dict[str, Any]] = []

    def record_exception(message: str, *args: object, **kwargs: Any) -> None:
        exception_calls.append({"message": message, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(bot_commands_module.logger, "exception", record_exception)

    with (
        patch("memexpert.bot.main.build_bot", return_value=bot),
        patch("memexpert.bot.main.build_dispatcher", return_value=dispatcher),
        pytest.raises(RuntimeError, match="setMyCommands failed"),
    ):
        await run_bot(settings=Settings())

    assert not dispatcher.polling_started
    assert bot.session.closed
    assert bot.events == ["register", "close"]
    assert len(exception_calls) == 1
    extra = exception_calls[0]["kwargs"]["extra"]
    assert extra["event"] == "telegram_bot_command_registration_failed"


def test_workers_main_runs_async_pipeline_runtime() -> None:
    with (
        patch("memexpert.workers.main.asyncio.run") as asyncio_run,
        patch("memexpert.workers.main.run_worker_runtime", new_callable=AsyncMock) as run_worker_runtime,
    ):
        workers_main(["--role", "ocr"])

    asyncio_run.assert_called_once()
    coroutine = asyncio_run.call_args.args[0]
    assert inspect.iscoroutine(coroutine)
    coroutine.close()
    run_worker_runtime.assert_called_once_with(role=WorkerRole.OCR)


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
