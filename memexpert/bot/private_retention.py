"""Telegram private-chat retention, settings, status, and stats router."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from memexpert.bot.analytics import record_telegram_interaction_event, telegram_user_hash
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.models.enums import (
    AnalyticsEventType,
    UserLanguage,
)
from memexpert.services import (
    ChannelSuggestionService,
    InvalidChannelSuggestionError,
    UserNotFoundError,
    UserService,
)
from memexpert.services.analytics import AnalyticsService
from memexpert.services.telegram_accounts import resolve_or_create_active_telegram_user

if TYPE_CHECKING:
    from aiogram.types import MaybeInaccessibleMessage
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.models.user import User
    from memexpert.schemas.auth import ProfileStatsRead
    from memexpert.schemas.user import UserRead

CALLBACK_PREFIX = "pmr"
logger = logging.getLogger(__name__)

type ChannelSuggestionServiceFactory = Callable[[AsyncSession], ChannelSuggestionService]
type UserServiceFactory = Callable[[AsyncSession], UserService]


def build_private_retention_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    channel_suggestion_service_factory: ChannelSuggestionServiceFactory | None = None,
    user_service_factory: UserServiceFactory | None = None,
) -> Router:
    """Build the private-message retention/settings router."""

    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_suggestion_factory = channel_suggestion_service_factory or (
        lambda session: ChannelSuggestionService(session)
    )
    resolved_user_factory = user_service_factory or (lambda session: UserService(session))

    router = Router(name="private-retention")

    @router.message(F.chat.type == "private", Command("settings"))
    async def handle_settings(message: Message) -> None:
        await show_settings(
            message=message,
            session_factory=resolved_session_factory,
        )

    @router.callback_query(lambda callback_query: _has_retention_callback(callback_query.data))
    async def handle_retention_callback(callback_query: CallbackQuery) -> None:
        await handle_settings_callback(
            callback_query=callback_query,
            session_factory=resolved_session_factory,
            user_service_factory=resolved_user_factory,
        )

    @router.message(F.chat.type == "private", Command("suggest_channel"))
    async def handle_suggest_channel(message: Message, command: CommandObject) -> None:
        await suggest_channel(
            message=message,
            channel=(command.args or "").strip(),
            session_factory=resolved_session_factory,
            channel_suggestion_service_factory=resolved_suggestion_factory,
        )

    @router.message(F.chat.type == "private", Command("account"))
    async def handle_account(message: Message) -> None:
        await show_account_status(
            message=message,
            settings=resolved_settings,
            session_factory=resolved_session_factory,
        )

    @router.message(F.chat.type == "private", Command("miniapp"))
    async def handle_miniapp(message: Message) -> None:
        await show_miniapp_links(message=message, settings=resolved_settings, session_factory=resolved_session_factory)

    @router.message(F.chat.type == "private", Command("stats"))
    async def handle_stats(message: Message) -> None:
        await show_stats(message=message, session_factory=resolved_session_factory)

    return router


async def show_settings(*, message: Message, session_factory: AsyncSessionFactory) -> None:
    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(_unlinked_message("settings"))
        return

    await message.answer(_render_settings(user), reply_markup=_settings_keyboard(user))


async def handle_settings_callback(
    *,
    callback_query: CallbackQuery,
    session_factory: AsyncSessionFactory,
    user_service_factory: UserServiceFactory,
) -> None:
    user = await _resolve_user_for_callback(callback_query, session_factory=session_factory)
    if user is None:
        await callback_query.answer(_unlinked_message("settings"), show_alert=True)
        return

    parsed = _parse_callback(callback_query.data)
    if parsed is None:
        await callback_query.answer("Эта кнопка устарела.", show_alert=True)
        return

    action, value = parsed
    try:
        async with session_factory() as session:
            user_service = user_service_factory(session)
            if action == "nsfw":
                updated = await user_service.update_preferences(user_id=user.id, nsfw_enabled=value == "1")
            elif action == "lang":
                updated = await user_service.update_preferences(user_id=user.id, language=UserLanguage(value))
            else:
                await callback_query.answer("Эта кнопка устарела.", show_alert=True)
                return
            await _record_retention_event(
                session,
                _settings_click_event(
                    user_id=user.id,
                    telegram_user_id=callback_query.from_user.id,
                    action=action,
                    value=value,
                ),
            )
    except ValueError:
        await callback_query.answer("Эта кнопка устарела.", show_alert=True)
        return
    except UserNotFoundError:
        await callback_query.answer("Аккаунт MemeXpert не найден. Перепривяжите Telegram.", show_alert=True)
        return

    await _edit_or_answer_callback(
        callback_query,
        text=_render_settings(updated),
        reply_markup=_settings_keyboard(updated),
    )


async def suggest_channel(
    *,
    message: Message,
    channel: str,
    session_factory: AsyncSessionFactory,
    channel_suggestion_service_factory: ChannelSuggestionServiceFactory,
) -> None:
    if not channel:
        await message.answer("Используйте: /suggest_channel <@handle или URL канала>")
        return

    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(_unlinked_message("suggest_channel"))
        return

    try:
        async with session_factory() as session:
            service = channel_suggestion_service_factory(session)
            result = await service.submit_channel_suggestion(user_id=user.id, channel=channel)
    except InvalidChannelSuggestionError as exc:
        await message.answer(f"Не удалось принять канал: {exc}")
        return
    except UserNotFoundError:
        await message.answer("Аккаунт MemeXpert не найден. Перепривяжите Telegram и попробуйте снова.")
        return

    suggestion = result.suggestion
    if result.created:
        await message.answer(
            "Канал отправлен на проверку.\n"
            f"Платформа: {suggestion.platform.value}\n"
            f"URL: {suggestion.channel_url}\n"
            "Статус: pending"
        )
    else:
        await message.answer(
            "Такой канал уже есть в очереди предложений.\n"
            f"Платформа: {suggestion.platform.value}\n"
            f"URL: {suggestion.channel_url}\n"
            f"Текущий статус: {suggestion.status.value}"
        )
    await _record_retention_event_with_factory(
        session_factory,
        {
            "event_type": AnalyticsEventType.CHANNEL_SUGGEST,
            "user_id": user.id,
            "surface": "telegram_pm_settings",
            "refs": {"channel_suggestion_id": suggestion.id},
            "properties": {
                "action": "suggest_channel",
                "status": suggestion.status.value,
                "platform": suggestion.platform.value,
                "created": result.created,
                "telegram_user_hash": _message_telegram_user_hash(message),
            },
        },
    )


async def show_account_status(
    *,
    message: Message,
    settings: Settings,
    session_factory: AsyncSessionFactory,
) -> None:
    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(
            _render_unlinked_account_status(settings=settings),
            reply_markup=_miniapp_keyboard(settings),
        )
        return

    await message.answer(_render_account_status(user, settings=settings), reply_markup=_miniapp_keyboard(settings))


async def show_miniapp_links(*, message: Message, settings: Settings, session_factory: AsyncSessionFactory) -> None:
    bot_username = _bot_username(settings)
    if bot_username is None:
        await message.answer("Mini App ссылки недоступны: auth_telegram_bot_username не настроен.")
        return
    await message.answer(_render_miniapp_links(bot_username), reply_markup=_miniapp_keyboard(settings))
    await _record_retention_event_with_factory(
        session_factory,
        {
            "event_type": AnalyticsEventType.MINIAPP_OPEN,
            "surface": "telegram_pm_miniapp",
            "properties": {
                "action": "link_display",
                "telegram_user_hash": _message_telegram_user_hash(message),
            },
        },
    )


async def show_stats(*, message: Message, session_factory: AsyncSessionFactory) -> None:
    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(_unlinked_message("stats"))
        return

    async with session_factory() as session:
        stats = await AnalyticsService(session).profile_stats(user_id=user.id)
    await message.answer(_render_stats(stats))


async def _resolve_user_for_message(message: Message, *, session_factory: AsyncSessionFactory) -> User | None:
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.id <= 0:
        return None
    async with session_factory() as session:
        account_resolution = await resolve_or_create_active_telegram_user(session, telegram_user_id=telegram_user.id)
        return account_resolution.user if account_resolution.is_active else None


async def _resolve_user_for_callback(
    callback_query: CallbackQuery,
    *,
    session_factory: AsyncSessionFactory,
) -> User | None:
    if callback_query.from_user.id <= 0:
        return None
    async with session_factory() as session:
        account_resolution = await resolve_or_create_active_telegram_user(
            session,
            telegram_user_id=callback_query.from_user.id,
        )
        return account_resolution.user if account_resolution.is_active else None


async def _record_retention_event_with_factory(
    session_factory: AsyncSessionFactory,
    event: dict[str, object],
) -> None:
    try:
        async with session_factory() as session:
            await _record_retention_event(session, event)
    except Exception:
        logger.exception(
            "Telegram retention telemetry setup failed.",
            extra={
                "event": "telegram_analytics_session_failed",
                "surface": event.get("surface"),
                "analytics_event_type": getattr(event.get("event_type"), "value", str(event.get("event_type"))),
            },
        )


async def _record_retention_event(session: AsyncSession, event: dict[str, object]) -> None:
    refs = event.get("refs")
    if not isinstance(refs, dict):
        refs = {}
    event_type = event.get("event_type")
    await record_telegram_interaction_event(
        session,
        event,
        log_context={
            "analytics_event_type": getattr(event_type, "value", str(event_type)),
            "surface": event.get("surface"),
            "user_id": str(event.get("user_id")) if event.get("user_id") else None,
            "channel_suggestion_id": (
                str(refs.get("channel_suggestion_id")) if refs.get("channel_suggestion_id") else None
            ),
        },
    )


def _settings_click_event(*, user_id: object, telegram_user_id: int, action: str, value: str) -> dict[str, object]:
    return {
        "event_type": AnalyticsEventType.CLICK,
        "user_id": user_id,
        "surface": "telegram_pm_settings",
        "properties": {
            "action": action,
            "value": value,
            "telegram_user_hash": telegram_user_hash(telegram_user_id),
        },
    }


def _message_telegram_user_hash(message: Message) -> str | None:
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.id <= 0:
        return None
    return telegram_user_hash(telegram_user.id)


def _render_settings(user: User | UserRead) -> str:
    return (
        "Настройки MemeXpert\n"
        f"NSFW по умолчанию: {'включено' if user.nsfw_enabled else 'выключено'}\n"
        f"Язык контента: {_language_label(user.language)}\n\n"
        "Изменения сохраняются в вашем аккаунте и применяются к персональным поверхностям."
    )


def _settings_keyboard(user: User | UserRead) -> InlineKeyboardMarkup:
    language_rows = [
        InlineKeyboardButton(
            text=_language_button_text(user, UserLanguage.ANY),
            callback_data=_callback("lang", "any"),
        ),
        InlineKeyboardButton(text=_language_button_text(user, UserLanguage.EN), callback_data=_callback("lang", "en")),
        InlineKeyboardButton(text=_language_button_text(user, UserLanguage.RU), callback_data=_callback("lang", "ru")),
    ]
    nsfw_text = "Выключить NSFW" if user.nsfw_enabled else "Включить NSFW"
    nsfw_value = "0" if user.nsfw_enabled else "1"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=nsfw_text, callback_data=_callback("nsfw", nsfw_value))],
            language_rows,
        ]
    )


def _render_account_status(user: User, *, settings: Settings) -> str:
    google = "привязан" if user.google_id else "не привязан"
    email = "привязан" if user.email else "не привязан"
    telegram = "привязан" if user.telegram_id else "не привязан"
    miniapp_line = _miniapp_status_line(settings)
    return (
        "Аккаунт MemeXpert\n"
        f"Тип: {user.account_type.value}\n"
        f"Статус: {user.status.value}\n"
        f"Telegram: {telegram}\n"
        f"Google: {google}\n"
        f"Email: {email}\n"
        f"Mini App: {miniapp_line}"
    )


def _render_unlinked_account_status(*, settings: Settings) -> str:
    return (
        "Telegram пока не привязан к активному аккаунту MemeXpert.\n"
        "Откройте ссылку привязки из сайта или Mini App, затем вернитесь в личный чат с ботом.\n"
        f"Mini App: {_miniapp_status_line(settings)}"
    )


def _render_miniapp_links(bot_username: str) -> str:
    return (
        "Mini App entry points\n"
        f"Профиль: {_miniapp_url(bot_username, 'profile')}\n"
        f"Коллекции: {_miniapp_url(bot_username, 'collections')}\n"
        f"Invite payload template: {_miniapp_url(bot_username, 'invite_<token>')}"
    )


def _render_stats(stats: ProfileStatsRead) -> str:
    lines = [
        "Статистика MemeXpert",
        f"Viewed: {stats.viewed}",
        f"Sent: {stats.sent}",
        f"Saved: {stats.saved}",
        f"Downloaded: {stats.downloaded}",
        f"Days active: {stats.days_active}",
        f"Top tags: {_format_top_tags(stats)}",
        f"Top templates: {_format_top_templates(stats)}",
    ]
    if stats.viewed == 0 and stats.sent == 0 and stats.saved == 0 and stats.downloaded == 0:
        lines.append("Пока мало данных: взаимодействуйте с мемами, чтобы заполнить статистику.")
    if stats.metadata.notes:
        lines.append("Notes: " + " | ".join(stats.metadata.notes))
    return "\n".join(lines)


def _format_top_tags(stats: ProfileStatsRead) -> str:
    if not stats.top_tags:
        return "нет данных"
    return ", ".join(f"{item.tag} ({item.count})" for item in stats.top_tags)


def _format_top_templates(stats: ProfileStatsRead) -> str:
    if not stats.top_templates:
        return "нет данных"
    return ", ".join(f"{item.name} ({item.count})" for item in stats.top_templates)


def _miniapp_keyboard(settings: Settings) -> InlineKeyboardMarkup | None:
    bot_username = _bot_username(settings)
    if bot_username is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open Profile", url=_miniapp_url(bot_username, "profile"))],
            [InlineKeyboardButton(text="Open Collections", url=_miniapp_url(bot_username, "collections"))],
            [InlineKeyboardButton(text="Invite Entry", url=_miniapp_url(bot_username, "invite"))],
        ]
    )


def _miniapp_status_line(settings: Settings) -> str:
    bot_username = _bot_username(settings)
    if bot_username is None:
        return "ссылки недоступны, bot username не настроен"
    return f"доступен: {_miniapp_url(bot_username)}"


def _miniapp_url(bot_username: str, startapp: str | None = None) -> str:
    base_url = f"https://t.me/{bot_username}/app"
    if startapp is None:
        return base_url
    return f"{base_url}?startapp={startapp}"


def _bot_username(settings: Settings) -> str | None:
    username = settings.auth_telegram_bot_username
    return username.strip().lstrip("@") if username else None


def _language_button_text(user: User | UserRead, language: UserLanguage) -> str:
    prefix = "* " if user.language is language else ""
    return f"{prefix}{_language_label(language)}"


def _language_label(language: UserLanguage) -> str:
    return {
        UserLanguage.ANY: "любой",
        UserLanguage.EN: "English",
        UserLanguage.RU: "Русский",
    }[language]


async def _edit_or_answer_callback(
    callback_query: CallbackQuery,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    message: MaybeInaccessibleMessage | None = callback_query.message
    if isinstance(message, Message):
        await message.edit_text(text, reply_markup=reply_markup)
        await callback_query.answer()
        return
    await callback_query.answer(text.splitlines()[0], show_alert=True)


def _callback(action: str, value: str) -> str:
    return f"{CALLBACK_PREFIX}:{action}:{value}"


def _has_retention_callback(data: str | None) -> bool:
    return data is not None and data.startswith(f"{CALLBACK_PREFIX}:")


def _parse_callback(data: str | None) -> tuple[str, str] | None:
    if not _has_retention_callback(data):
        return None
    assert data is not None
    parts = data.split(":", maxsplit=2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def _unlinked_message(command: str) -> str:
    return f"Telegram аккаунт MemeXpert недоступен или неактивен. Проверьте статус аккаунта, затем откройте /{command}."


__all__ = [
    "ChannelSuggestionServiceFactory",
    "UserServiceFactory",
    "build_private_retention_router",
    "handle_settings_callback",
    "show_account_status",
    "show_miniapp_links",
    "show_settings",
    "show_stats",
    "suggest_channel",
]
