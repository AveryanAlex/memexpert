"""Tests for Telegram bot command menu registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

import memexpert.bot.commands as bot_commands_module
from memexpert.bot.commands import build_bot_commands, build_private_command_scope, register_bot_commands

if TYPE_CHECKING:
    from aiogram import Bot

EXPECTED_COMMAND_NAMES = [
    "start",
    "search",
    "library",
    "favorites",
    "pins",
    "active",
    "collections",
    "collection_create",
    "invite_accept",
    "settings",
    "suggest_channel",
    "account",
    "miniapp",
    "stats",
]


class RecordingCommandBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_my_commands(
        self,
        commands: list[BotCommand],
        *,
        scope: BotCommandScopeAllPrivateChats | None = None,
        language_code: str | None = None,
        request_timeout: int | None = None,
    ) -> bool:
        self.calls.append(
            {
                "commands": commands,
                "scope": scope,
                "language_code": language_code,
                "request_timeout": request_timeout,
            }
        )
        return True


class FailingCommandBot:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def set_my_commands(
        self,
        commands: list[BotCommand],
        *,
        scope: BotCommandScopeAllPrivateChats | None = None,
        language_code: str | None = None,
        request_timeout: int | None = None,
    ) -> bool:
        _ = (commands, scope, language_code, request_timeout)
        raise self.exc


def test_build_bot_commands_matches_private_slash_menu() -> None:
    commands = build_bot_commands()
    command_names = [command.command for command in commands]

    assert command_names == EXPECTED_COMMAND_NAMES
    assert len(command_names) == len(set(command_names))
    assert all(isinstance(command, BotCommand) for command in commands)
    assert all(3 <= len(command.description) <= 256 for command in commands)
    assert "help" not in command_names
    assert "upload" not in command_names
    assert "inline" not in command_names


def test_build_private_command_scope_uses_all_private_chats() -> None:
    scope = build_private_command_scope()

    assert isinstance(scope, BotCommandScopeAllPrivateChats)
    assert scope.type.value == "all_private_chats"


@pytest.mark.asyncio
async def test_register_bot_commands_sends_commands_with_private_scope() -> None:
    bot = RecordingCommandBot()

    await register_bot_commands(cast("Bot", bot))

    assert len(bot.calls) == 1
    call = bot.calls[0]
    assert [command.command for command in call["commands"]] == EXPECTED_COMMAND_NAMES
    assert isinstance(call["scope"], BotCommandScopeAllPrivateChats)
    assert call["language_code"] is None
    assert call["request_timeout"] is None


@pytest.mark.asyncio
async def test_register_bot_commands_logs_and_reraises_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = FailingCommandBot(RuntimeError("setMyCommands failed"))
    exception_calls: list[dict[str, Any]] = []

    def record_exception(message: str, *args: object, **kwargs: Any) -> None:
        exception_calls.append({"message": message, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(bot_commands_module.logger, "exception", record_exception)

    with pytest.raises(
        RuntimeError,
        match="setMyCommands failed",
    ):
        await register_bot_commands(cast("Bot", bot))

    assert len(exception_calls) == 1
    call = exception_calls[0]
    assert call["message"] == "Failed to register Telegram bot command menu."
    assert call["args"] == ()
    extra = call["kwargs"]["extra"]
    assert extra == {
        "event": "telegram_bot_command_registration_failed",
        "scope": "all_private_chats",
        "command_names": EXPECTED_COMMAND_NAMES,
    }
