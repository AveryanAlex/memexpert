"""Console entry point for the Telegram bot."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher

from memexpert.bot.commands import register_bot_commands
from memexpert.bot.inline import (
    InlineMediaUrlProvider,
    MemeSearchServiceFactory,
    RecommendationServiceFactory,
    build_inline_router,
)
from memexpert.bot.linking import AccountLinkServiceFactory, build_linking_router
from memexpert.bot.private_library import (
    PrivateLibraryCollectionServiceFactory,
    build_private_library_router,
)
from memexpert.bot.private_retention import build_private_retention_router
from memexpert.bot.private_search import build_private_search_router
from memexpert.bot.private_upload import (
    CollectionServiceFactory,
    PrivateUploadAcceptServiceFactory,
    TelegramFileDownloader,
    build_private_upload_router,
)
from memexpert.core.config import Settings, get_settings
from memexpert.core.qdrant import reset_async_qdrant_state
from memexpert.services import ProviderNotConfiguredError

if TYPE_CHECKING:
    from aiogram.client.session.base import BaseSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.services.recommendations.telegram_sessions import TelegramInlineSessionStore


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
    meme_search_service_factory: MemeSearchServiceFactory | None = None,
    recommendation_service_factory: RecommendationServiceFactory | None = None,
    inline_media_url_provider: InlineMediaUrlProvider | None = None,
    inline_sessions: TelegramInlineSessionStore | None = None,
    private_upload_accept_service_factory: PrivateUploadAcceptServiceFactory | None = None,
    private_upload_collection_service_factory: CollectionServiceFactory | None = None,
    private_library_collection_service_factory: PrivateLibraryCollectionServiceFactory | None = None,
    telegram_file_downloader: TelegramFileDownloader | None = None,
) -> Dispatcher:
    """Build the dispatcher for account linking, inline search, and PM chat handlers."""

    resolved_settings = settings or get_settings()
    dispatcher = Dispatcher()
    _ = dispatcher.include_router(
        build_linking_router(
            settings=resolved_settings,
            session_factory=session_factory,
            account_link_service_factory=account_link_service_factory,
        )
    )
    _ = dispatcher.include_router(
        build_inline_router(
            settings=resolved_settings,
            session_factory=session_factory,
            meme_search_service_factory=meme_search_service_factory,
            recommendation_service_factory=recommendation_service_factory,
            inline_media_url_provider=inline_media_url_provider,
            inline_sessions=inline_sessions,
        )
    )
    _ = dispatcher.include_router(
        build_private_search_router(
            settings=resolved_settings,
            session_factory=session_factory,
            meme_search_service_factory=meme_search_service_factory,
        )
    )
    _ = dispatcher.include_router(
        build_private_library_router(
            settings=resolved_settings,
            session_factory=session_factory,
            collection_service_factory=private_library_collection_service_factory,
        )
    )
    _ = dispatcher.include_router(
        build_private_retention_router(
            settings=resolved_settings,
            session_factory=session_factory,
        )
    )
    _ = dispatcher.include_router(
        build_private_upload_router(
            settings=resolved_settings,
            session_factory=session_factory,
            accept_service_factory=private_upload_accept_service_factory,
            collection_service_factory=private_upload_collection_service_factory,
            telegram_file_downloader=telegram_file_downloader,
        )
    )
    return dispatcher


async def run_bot(*, settings: Settings | None = None) -> None:
    """Start polling Telegram updates for the bot runtime."""

    resolved_settings = settings or get_settings()
    bot = build_bot(resolved_settings)
    dispatcher = build_dispatcher(settings=resolved_settings)

    try:
        await register_bot_commands(bot)
        await dispatcher.start_polling(bot)
    finally:
        try:
            await bot.session.close()
        finally:
            await reset_async_qdrant_state()


def main() -> None:
    """Run the Telegram bot."""

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
