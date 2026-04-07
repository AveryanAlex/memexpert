"""Console entry point for the Telegram account-linking bot."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher

from memexpert.bot.linking import AccountLinkServiceFactory, build_linking_router
from memexpert.core.config import Settings, get_settings
from memexpert.services import ProviderNotConfiguredError

if TYPE_CHECKING:
    from aiogram.client.session.base import BaseSession

    from memexpert.core.database import AsyncSessionFactory


def build_bot(settings: Settings | None = None, *, session: BaseSession | None = None) -> Bot:
    """Build the aiogram bot instance from runtime settings."""

    resolved_settings = settings or get_settings()
    bot_token = _require_bot_token(resolved_settings)
    return Bot(token=bot_token, session=session)


def build_dispatcher(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    account_link_service_factory: AccountLinkServiceFactory | None = None,
) -> Dispatcher:
    """Build the minimal dispatcher that only serves Telegram account linking."""

    resolved_settings = settings or get_settings()
    dispatcher = Dispatcher()
    _ = dispatcher.include_router(
        build_linking_router(
            settings=resolved_settings,
            session_factory=session_factory,
            account_link_service_factory=account_link_service_factory,
        )
    )
    return dispatcher


async def run_bot(*, settings: Settings | None = None) -> None:
    """Start polling Telegram updates for the linking-only bot runtime."""

    resolved_settings = settings or get_settings()
    bot = build_bot(resolved_settings)
    dispatcher = build_dispatcher(settings=resolved_settings)

    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    """Run the Telegram account-linking bot."""

    asyncio.run(run_bot())


def _require_bot_token(settings: Settings) -> str:
    secret = settings.auth_telegram_bot_token
    if secret is None:
        raise ProviderNotConfiguredError("Telegram bot token is not configured.")

    bot_token = secret.get_secret_value().strip()
    if not bot_token:
        raise ProviderNotConfiguredError("Telegram bot token is not configured.")
    return bot_token


__all__ = ["build_bot", "build_dispatcher", "main", "run_bot"]
