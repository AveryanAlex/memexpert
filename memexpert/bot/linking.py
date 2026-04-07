"""Minimal Telegram /start handler for guest account linking."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.models.base import utcnow
from memexpert.services import (
    AccountLinkAlreadyCompletedError,
    AccountLinkInvariantError,
    AccountLinkResult,
    AccountLinkService,
    AuthConfigurationError,
    ServiceError,
)
from memexpert.services.account_link_service import TELEGRAM_LINK_START_PREFIX
from memexpert.services.provider_auth_service import TelegramIdentity

if TYPE_CHECKING:
    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory


type AccountLinkServiceFactory = Callable[[AsyncSession], AccountLinkService]

BOT_LINK_REDEMPTION_TIMEOUT_SECONDS = 10.0


def build_linking_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    account_link_service_factory: AccountLinkServiceFactory | None = None,
) -> Router:
    """Build the focused router that completes Telegram guest-link handoffs."""

    resolved_settings = settings or get_settings()
    return_url = _require_return_url(resolved_settings)
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_service_factory = account_link_service_factory or (
        lambda session: AccountLinkService.from_settings(session, settings=resolved_settings)
    )

    router = Router(name="account-linking")

    @router.message(CommandStart(deep_link=True))
    async def handle_start_with_payload(message: Message, command: CommandObject) -> None:
        await handle_start_command(
            message=message,
            start_argument=command.args,
            return_url=return_url,
            session_factory=resolved_session_factory,
            account_link_service_factory=resolved_service_factory,
        )

    @router.message(CommandStart())
    async def handle_plain_start(message: Message) -> None:
        await message.answer(_build_help_message(return_url=return_url))

    return router


async def handle_start_command(
    *,
    message: Message,
    start_argument: str | None,
    return_url: str,
    session_factory: AsyncSessionFactory,
    account_link_service_factory: AccountLinkServiceFactory,
) -> None:
    """Handle `/start` payloads and redeem real guest-link codes when present."""

    normalized_argument = (start_argument or "").strip()
    if not normalized_argument or not normalized_argument.startswith(TELEGRAM_LINK_START_PREFIX):
        await message.answer(_build_help_message(return_url=return_url))
        return

    telegram_id = _extract_telegram_id(message)
    if telegram_id is None:
        await message.answer(_build_missing_identity_message(return_url=return_url))
        return

    code = normalized_argument.removeprefix(TELEGRAM_LINK_START_PREFIX)

    try:
        async with session_factory() as session:
            link_service = account_link_service_factory(session)
            async with asyncio.timeout(BOT_LINK_REDEMPTION_TIMEOUT_SECONDS):
                link_result = await link_service.redeem_telegram_link_code(
                    code=code,
                    identity=TelegramIdentity(telegram_id=telegram_id, auth_date=utcnow()),
                )
    except TimeoutError:
        await message.answer(_build_timeout_message(return_url=return_url))
        return
    except AccountLinkAlreadyCompletedError:
        await message.answer(_build_completed_elsewhere_message(return_url=return_url))
        return
    except AccountLinkInvariantError:
        await message.answer(_build_retry_message(return_url=return_url))
        return
    except ServiceError:
        await message.answer(_build_service_error_message(return_url=return_url))
        return

    await message.answer(_build_success_message(link_result=link_result, return_url=return_url))


def _extract_telegram_id(message: Message) -> int | None:
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.id <= 0:
        return None
    return telegram_user.id


def _build_help_message(*, return_url: str) -> str:
    return (
        "Привет! Этот бот завершает привязку Telegram к аккаунту MemeXpert.\n"
        "Откройте ссылку из сайта или Mini App и вернитесь сюда по новой ссылке.\n"
        f"Вернуться в MemeXpert: {return_url}"
    )


def _build_missing_identity_message(*, return_url: str) -> str:
    return (
        "Не удалось определить ваш Telegram-профиль, поэтому привязка не была запущена.\n"
        "Откройте ссылку заново из личного чата с ботом и попробуйте ещё раз.\n"
        f"Вернуться в MemeXpert: {return_url}"
    )


def _build_completed_elsewhere_message(*, return_url: str) -> str:
    return (
        "Эта привязка уже завершилась в другом окне или устройстве.\n"
        "Вернитесь в MemeXpert, обновите страницу или Mini App и продолжайте с текущим аккаунтом — "
        "новую ссылку запрашивать не нужно.\n"
        f"Вернуться в MemeXpert: {return_url}"
    )


def _build_retry_message(*, return_url: str) -> str:
    return (
        "Не удалось завершить привязку: ссылка недействительна, истекла, уже была использована "
        "или аккаунт сейчас нельзя безопасно объединить.\n"
        "Запросите новую ссылку в MemeXpert и попробуйте снова — избранное и история не потеряются.\n"
        f"Вернуться в MemeXpert: {return_url}"
    )


def _build_timeout_message(*, return_url: str) -> str:
    return (
        "Связать аккаунт сейчас не получилось из-за таймаута.\n"
        "Ничего не потеряно: запросите новую ссылку в MemeXpert и попробуйте ещё раз.\n"
        f"Вернуться в MemeXpert: {return_url}"
    )


def _build_service_error_message(*, return_url: str) -> str:
    return (
        "Не удалось завершить привязку из-за временной ошибки сервиса.\n"
        "Попробуйте снова из MemeXpert немного позже — текущие данные остались в безопасном состоянии.\n"
        f"Вернуться в MemeXpert: {return_url}"
    )


def _build_success_message(*, link_result: AccountLinkResult, return_url: str) -> str:
    merge_performed = link_result.merge_performed
    telegram_linked = link_result.linked_providers.telegram_linked
    favorites_transferred = link_result.favorites_transferred
    duplicate_favorites_skipped = link_result.duplicate_favorites_skipped
    analytics_events_transferred = link_result.analytics_events_transferred
    inline_usage_events_transferred = link_result.inline_usage_events_transferred
    views_transferred = link_result.views_transferred

    if merge_performed:
        headline = "✅ Аккаунты объединены. Telegram привязан к вашему основному аккаунту MemeXpert."
    elif telegram_linked:
        headline = "✅ Telegram привязан. Теперь этот аккаунт MemeXpert сохранён за вашим Telegram-профилем."
    else:
        headline = "✅ Привязка завершена."

    preserved_guest_state = any(
        (
            merge_performed,
            favorites_transferred > 0,
            duplicate_favorites_skipped > 0,
            analytics_events_transferred > 0,
            inline_usage_events_transferred > 0,
            views_transferred > 0,
        )
    )
    if preserved_guest_state:
        reassurance = "Избранное и история из гостевого профиля сохранены."
    else:
        reassurance = "Избранное и история остались на месте."

    return f"{headline}\n{reassurance}\nВернуться в MemeXpert: {return_url}"


def _require_return_url(settings: Settings) -> str:
    if settings.auth_telegram_link_return_url is None:
        raise AuthConfigurationError("Telegram link return URL is not configured.")
    return str(settings.auth_telegram_link_return_url)


__all__ = ["AccountLinkServiceFactory", "build_linking_router", "handle_start_command"]
