"""Telegram private-chat meme search router."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandObject

from memexpert.bot.analytics import record_telegram_interaction_event, telegram_user_hash
from memexpert.bot.meme_search_factory import build_default_meme_search_service_factory
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.models.enums import AnalyticsEventType
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchScope
from memexpert.services.telegram_accounts import (
    TelegramAccountResolutionStatus,
    resolve_or_create_active_telegram_user,
)

if TYPE_CHECKING:
    import uuid

    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.models.user import User
    from memexpert.schemas.meme import MemeCardRead, MemeSearchPageRead
    from memexpert.services.telegram_inline import MemeSearchServiceFactory

logger = logging.getLogger(__name__)

PRIVATE_SEARCH_LIMIT = 5


def build_private_search_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    meme_search_service_factory: MemeSearchServiceFactory | None = None,
) -> Router:
    """Build the private-message search router backed by the shared meme search service."""

    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_service_factory = meme_search_service_factory or build_default_meme_search_service_factory(
        resolved_settings,
    )

    router = Router(name="private-search")

    @router.message(F.chat.type == "private", Command("search"))
    async def handle_private_search(message: Message, command: CommandObject) -> None:
        await search_private_memes(
            message=message,
            query=(command.args or "").strip(),
            session_factory=resolved_session_factory,
            meme_search_service_factory=resolved_service_factory,
        )

    return router


async def search_private_memes(
    *,
    message: Message,
    query: str,
    session_factory: AsyncSessionFactory,
    meme_search_service_factory: MemeSearchServiceFactory,
) -> None:
    """Run a safe text-only PM meme search for one Telegram message."""

    normalized_query = query.strip()
    if not normalized_query:
        await message.answer(_usage_message())
        return

    telegram_user_id = _extract_telegram_user_id(message)
    if telegram_user_id is None:
        await message.answer(_missing_identity_message())
        return

    async with session_factory() as session:
        account_resolution = await resolve_or_create_active_telegram_user(
            session,
            telegram_user_id=telegram_user_id,
        )
        if account_resolution.status is TelegramAccountResolutionStatus.INVALID_TELEGRAM_ID:
            await message.answer(_missing_identity_message())
            return
        if not account_resolution.is_active:
            await message.answer(_account_unavailable_message())
            return

        user = account_resolution.user
        assert user is not None
        try:
            search_service = meme_search_service_factory(session)
            page = await search_service.search_memes(
                normalized_query,
                viewer_user_id=user.id,
                filters=MemeSearchFilters(
                    include_nsfw=user.nsfw_enabled,
                    scope=MemeSearchScope.ALL,
                ),
                limit=PRIVATE_SEARCH_LIMIT,
                offset=0,
                surface="telegram_pm_search",
            )
        except Exception:
            logger.exception("Telegram PM meme search failed for user_id=%s.", user.id)
            await message.answer(_search_unavailable_message())
            return

        await _record_pm_search_event(
            session,
            user=user,
            telegram_user_id=telegram_user_id,
            query=normalized_query,
            page=page,
        )

    await message.answer(_render_search_response(query=normalized_query, page=page))


async def _record_pm_search_event(
    session: AsyncSession,
    *,
    user: User,
    telegram_user_id: int,
    query: str,
    page: MemeSearchPageRead,
) -> None:
    await record_telegram_interaction_event(
        session,
        {
            "event_type": AnalyticsEventType.SEARCH_QUERY,
            "user_id": user.id,
            "surface": "telegram_pm_search",
            "request_id": page.request_id,
            "query": query,
            "properties": {
                "telegram_user_hash": telegram_user_hash(telegram_user_id),
                "result_count": len(page.items),
                "total": page.total,
                "has_more": page.has_more,
                "limit": page.limit,
                "offset": page.offset,
            },
        },
        log_context={
            "analytics_event_type": AnalyticsEventType.SEARCH_QUERY.value,
            "surface": "telegram_pm_search",
            "user_id": str(user.id),
        },
    )


def _render_search_response(*, query: str, page: MemeSearchPageRead) -> str:
    safe_query = _compact_text(query, limit=80)
    if not page.items:
        return f'No memes found for "{safe_query}".'

    shown = page.offset + len(page.items)
    lines = [f'Results for "{safe_query}" ({shown}/{page.total}):']
    for index, item in enumerate(page.items, start=page.offset + 1):
        lines.append(_render_meme_result(index=index, meme=item.meme))
    if page.has_more:
        lines.append("More results are available in MemeXpert search.")
    return "\n".join(lines)


def _render_meme_result(*, index: int, meme: MemeCardRead) -> str:
    parts = [f"{index}. {meme.media_type.value} meme:{_short_id(meme.id)}"]
    caption = _compact_text(meme.caption, limit=120)
    if caption:
        parts.append(f"caption: {caption}")
    tags = _format_tags(meme.tags)
    if tags:
        parts.append(f"tags: {tags}")
    return " | ".join(parts)


def _format_tags(tags: list[str]) -> str:
    safe_tags = [_compact_text(tag, limit=24) for tag in tags[:5]]
    safe_tags = [tag for tag in safe_tags if tag]
    return ", ".join(safe_tags)


def _extract_telegram_user_id(message: Message) -> int | None:
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.id <= 0:
        return None
    return telegram_user.id


def _compact_text(value: str | None, *, limit: int) -> str:
    if value is None:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(0, limit - 3)]}..."


def _short_id(value: uuid.UUID) -> str:
    return value.hex[-8:]


def _usage_message() -> str:
    return "Use: /search <query>"


def _missing_identity_message() -> str:
    return "Could not identify your Telegram account. Open this in a private chat and try again."


def _account_unavailable_message() -> str:
    return "Your MemeXpert account is inactive or unavailable, so private search is disabled."


def _search_unavailable_message() -> str:
    return "Search is temporarily unavailable. Please try again later."


__all__ = [
    "PRIVATE_SEARCH_LIMIT",
    "build_private_search_router",
    "search_private_memes",
]
