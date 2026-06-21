"""Telegram slash-command menu registration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

COMMAND_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("start", "Link your MemeXpert account"),
    ("search", "Search memes by text"),
    ("library", "Open your library menu"),
    ("favorites", "Show favorite memes"),
    ("pins", "Show pinned memes"),
    ("active", "Show active collection"),
    ("collections", "Manage collections"),
    ("collection_create", "Create a new collection"),
    ("invite_accept", "Accept a collection invite"),
    ("settings", "Change bot settings"),
    ("suggest_channel", "Suggest a source channel"),
    ("account", "Show account status"),
    ("miniapp", "Open MemeXpert Mini App"),
    ("stats", "Show your MemeXpert stats"),
)


def build_bot_commands() -> list[BotCommand]:
    """Build the current private-chat slash-command menu."""

    return [BotCommand(command=command, description=description) for command, description in COMMAND_DEFINITIONS]


def build_private_command_scope() -> BotCommandScopeAllPrivateChats:
    """Build the command scope used for all private chats with the bot."""

    return BotCommandScopeAllPrivateChats()


async def register_bot_commands(bot: Bot) -> None:
    """Register bot commands at startup, failing loudly if Telegram rejects them."""

    commands = build_bot_commands()
    scope = build_private_command_scope()
    try:
        _ = await bot.set_my_commands(commands, scope=scope)
    except Exception:
        logger.exception(
            "Failed to register Telegram bot command menu.",
            extra={
                "event": "telegram_bot_command_registration_failed",
                "scope": "all_private_chats",
                "command_names": [command.command for command in commands],
            },
        )
        raise


__all__ = [
    "COMMAND_DEFINITIONS",
    "build_bot_commands",
    "build_private_command_scope",
    "register_bot_commands",
]
