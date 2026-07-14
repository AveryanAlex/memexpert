"""Telegram inline-mode search router for sending cached meme media."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from aiogram import Router
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultGif,
    InlineQueryResultMpeg4Gif,
    InlineQueryResultPhoto,
    InlineQueryResultUnion,
)
from sqlalchemy import select

from memexpert.bot.analytics import record_telegram_interaction_event, telegram_user_hash
from memexpert.bot.meme_search_factory import build_default_meme_search_service_factory
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.core.storage import StorageConfigurationError, build_s3_client, get_pipeline_storage_settings
from memexpert.models.content import MemeFile
from memexpert.models.enums import AccountStatus, AccountType, AnalyticsEventType, TelegramMediaFormat
from memexpert.models.user import User
from memexpert.services import CollectionService, CollectionServiceError, ProviderNotConfiguredError
from memexpert.services.telegram_inline import (
    MPEG4_GIF_MIME_TYPE,
    MemeSearchServiceFactory,
    TelegramInlineMediaResult,
    TelegramInlineMediaUrlProvider,
    TelegramInlineSearchPage,
    TelegramInlineService,
    TelegramInlineServiceFactory,
    public_https_url,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.schemas.meme import MemeCardRead, MemeFileRead

logger = logging.getLogger(__name__)

INLINE_SEARCH_LIMIT = 20
INLINE_CACHE_TIME_SECONDS = 5
LIBRARY_CALLBACK_PREFIX = "lib"


type InlineResult = InlineQueryResultUnion


class InlineMediaUrlProvider(TelegramInlineMediaUrlProvider, Protocol):
    pass


class S3PresignedInlineMediaUrlProvider:
    """Generate Telegram-fetchable URLs for private S3-compatible original objects."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    async def get_media_url(self, file: MemeFileRead) -> str | None:
        try:
            storage_settings = get_pipeline_storage_settings(self._settings)
            client = self._client or build_s3_client(self._settings)
            self._client = client
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": storage_settings.bucket, "Key": file.s3_original_key},
                ExpiresIn=300,
            )
        except (StorageConfigurationError, ValueError, TypeError) as exc:
            logger.warning(
                "Telegram inline private media URL generation is unavailable for meme_file_id=%s: %s",
                file.id,
                exc,
            )
            return None
        except Exception:
            logger.exception("Telegram inline private media URL generation failed for meme_file_id=%s.", file.id)
            return None

        media_url = public_https_url(url)
        if media_url is None:
            logger.warning(
                "Telegram inline private media URL for meme_file_id=%s is not HTTPS and was skipped.",
                file.id,
            )
        return media_url


def build_inline_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    meme_search_service_factory: MemeSearchServiceFactory | None = None,
    inline_media_url_provider: InlineMediaUrlProvider | None = None,
) -> Router:
    """Build the inline-mode router backed by the shared meme search service."""

    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_service_factory = meme_search_service_factory or build_default_meme_search_service_factory(
        resolved_settings,
    )
    resolved_media_url_provider = inline_media_url_provider or S3PresignedInlineMediaUrlProvider(resolved_settings)
    bot_scope = _build_bot_scope(resolved_settings)
    resolved_inline_service_factory = _build_default_inline_service_factory(
        meme_search_service_factory=resolved_service_factory,
        inline_media_url_provider=resolved_media_url_provider,
        bot_scope=bot_scope,
    )

    router = Router(name="inline-search")

    @router.inline_query()
    async def handle_inline_query(inline_query: InlineQuery) -> None:
        await answer_inline_query(
            inline_query=inline_query,
            session_factory=resolved_session_factory,
            inline_service_factory=resolved_inline_service_factory,
        )

    @router.chosen_inline_result()
    async def handle_chosen_inline_result(chosen_result: ChosenInlineResult) -> None:
        await record_chosen_inline_result(
            chosen_result=chosen_result,
            session_factory=resolved_session_factory,
        )

    @router.callback_query(
        lambda callback_query: (
            callback_query.data is not None and callback_query.data.startswith(f"{LIBRARY_CALLBACK_PREFIX}:")
        )
    )
    async def handle_library_callback(callback_query: CallbackQuery) -> None:
        await handle_inline_library_callback(
            callback_query=callback_query,
            session_factory=resolved_session_factory,
        )

    return router


async def answer_inline_query(
    *,
    inline_query: InlineQuery,
    session_factory: AsyncSessionFactory,
    inline_service_factory: TelegramInlineServiceFactory | None = None,
    meme_search_service_factory: MemeSearchServiceFactory | None = None,
    inline_media_url_provider: InlineMediaUrlProvider | None = None,
    bot_scope: str | None = None,
) -> None:
    """Search memes for a Telegram inline query and answer with sendable media results."""

    offset = _parse_offset(inline_query.offset)
    query = inline_query.query.strip()

    async with session_factory() as session:
        try:
            resolved_inline_service_factory = inline_service_factory
            if resolved_inline_service_factory is None:
                if meme_search_service_factory is None or inline_media_url_provider is None or bot_scope is None:
                    raise ProviderNotConfiguredError("Telegram inline service factory is not configured.")
                resolved_inline_service_factory = _build_default_inline_service_factory(
                    meme_search_service_factory=meme_search_service_factory,
                    inline_media_url_provider=inline_media_url_provider,
                    bot_scope=bot_scope,
                )
            inline_service = resolved_inline_service_factory(session)
            page = await inline_service.search_inline_memes(
                telegram_user_id=inline_query.from_user.id,
                query=query,
                limit=INLINE_SEARCH_LIMIT,
                offset=offset,
            )
            served_items: list[TelegramInlineMediaResult] = []
            results: list[InlineResult] = []
            for item in page.items:
                result = _to_inline_result(item)
                if result is None:
                    continue
                served_items.append(item)
                results.append(result)
        except Exception:
            logger.exception("Telegram inline meme search failed; returning an empty inline answer.")
            page = TelegramInlineSearchPage(
                items=[],
                limit=INLINE_SEARCH_LIMIT,
                offset=offset,
                total=0,
                has_more=False,
                is_personal=False,
            )
            served_items = []
            results = []
        # Search pagination is offset-based, but inline conversion may filter out
        # unsupported/private-storage items. Do not ask Telegram for another page
        # after returning zero sendable results; that can otherwise produce empty
        # scroll pages or skip supported media that the user never saw.
        next_offset = str(offset + page.limit) if page.has_more and results else ""
        await inline_query.answer(
            results,
            cache_time=INLINE_CACHE_TIME_SECONDS,
            is_personal=page.is_personal,
            next_offset=next_offset,
        )
        await _record_inline_query_event(
            session,
            inline_query=inline_query,
            offset=offset,
            result_count=len(results),
            has_more=page.has_more,
        )
        await _record_inline_served_events(
            session,
            inline_query=inline_query,
            items=served_items,
            offset=offset,
            result_count=len(results),
            is_personal=page.is_personal,
        )


async def record_chosen_inline_result(
    *,
    chosen_result: ChosenInlineResult,
    session_factory: AsyncSessionFactory,
) -> None:
    """Record Telegram's chosen-inline-result signal as the MVP send event.

    Telegram does not provide a separate delivery confirmation for inline sends;
    ``chosen_inline_result`` is the closest Bot API event available after the user
    taps an inline result.
    """

    file_id = _parse_result_file_id(chosen_result.result_id)
    impression_id = _parse_result_impression_id(chosen_result.result_id)
    async with session_factory() as session:
        meme_id = None
        if file_id is not None:
            meme_id = await session.scalar(select(MemeFile.meme_id).where(MemeFile.id == file_id))

        user_id = await _resolve_linked_user_id(session, telegram_user_id=chosen_result.from_user.id)
        if file_id is None or meme_id is None:
            return

        for event_type in (
            AnalyticsEventType.INLINE_CHOSEN,
            AnalyticsEventType.INLINE_SENT,
            AnalyticsEventType.MEME_SEND,
        ):
            await _record_chosen_inline_event(
                session,
                event_type=event_type,
                user_id=user_id,
                meme_id=meme_id,
                file_id=file_id,
                chosen_result=chosen_result,
                impression_id=impression_id,
            )


async def handle_inline_library_callback(
    *,
    callback_query: CallbackQuery,
    session_factory: AsyncSessionFactory,
) -> None:
    """Handle the simple Favorite/Save/Pin buttons attached to inline meme results."""

    action, file_id = _parse_library_callback_data(callback_query.data)
    if action is None or file_id is None:
        await callback_query.answer("This meme action is no longer supported.", show_alert=True)
        return

    async with session_factory() as session:
        meme_id = await session.scalar(select(MemeFile.meme_id).where(MemeFile.id == file_id))
        if meme_id is None:
            await callback_query.answer("Meme was not found.", show_alert=True)
            return

        user_id, inactive_user = await _resolve_active_linked_user_id(
            session,
            telegram_user_id=callback_query.from_user.id,
        )
        if user_id is None:
            if inactive_user:
                await callback_query.answer("Your MemeXpert account is not active.", show_alert=True)
                return
            await callback_query.answer("Link your Telegram account to use meme library actions.", show_alert=True)
            return

        service = CollectionService(session)
        try:
            if action == "fav":
                mutation = await service.favorite_meme_result(user_id=user_id, meme_id=meme_id)
                text = "Added to Favorites." if mutation.changed else "Already in Favorites."
                event_type = AnalyticsEventType.MEME_LIKE
                collection_id = mutation.item.collection_id
            elif action == "save":
                mutation = await service.save_meme_to_active_collection_result(user_id=user_id, meme_id=meme_id)
                text = "Saved to active collection." if mutation.changed else "Already saved there."
                event_type = AnalyticsEventType.MEME_SAVE
                collection_id = mutation.item.collection_id
            elif action == "pin":
                mutation = await service.pin_meme_result(user_id=user_id, meme_id=meme_id)
                text = "Pinned." if mutation.changed else "Already pinned."
                event_type = AnalyticsEventType.MEME_PIN
                collection_id = None
            else:
                await callback_query.answer("This meme action is no longer supported.", show_alert=True)
                return
        except CollectionServiceError as exc:
            logger.info("Telegram inline library callback rejected: %s", exc)
            await callback_query.answer(str(exc), show_alert=True)
            return
        if not mutation.changed:
            await callback_query.answer(text)
            return
        await record_telegram_interaction_event(
            session,
            {
                "event_type": event_type,
                "user_id": user_id,
                "surface": "telegram_inline_library",
                "refs": {"collection_id": collection_id, "meme_id": meme_id},
                "properties": {
                    "action": action,
                    "telegram_user_hash": telegram_user_hash(callback_query.from_user.id),
                },
            },
            log_context={
                "analytics_event_type": event_type.value,
                "surface": "telegram_inline_library",
                "meme_id": str(meme_id),
                "user_id": str(user_id),
            },
        )

    await callback_query.answer(text)


def _build_default_inline_service_factory(
    *,
    meme_search_service_factory: MemeSearchServiceFactory,
    inline_media_url_provider: InlineMediaUrlProvider,
    bot_scope: str,
) -> TelegramInlineServiceFactory:
    def factory(session: AsyncSession) -> TelegramInlineService:
        return TelegramInlineService(
            session,
            meme_search_service=meme_search_service_factory(session),
            media_url_provider=inline_media_url_provider,
            bot_scope=bot_scope,
        )

    return factory


async def _record_inline_query_event(
    session: AsyncSession,
    *,
    inline_query: InlineQuery,
    offset: int,
    result_count: int,
    has_more: bool,
) -> None:
    user_id = await _resolve_linked_user_id(session, telegram_user_id=inline_query.from_user.id)
    await record_telegram_interaction_event(
        session,
        {
            "event_type": AnalyticsEventType.INLINE_QUERY,
            "user_id": user_id,
            "surface": "telegram_inline",
            "query": inline_query.query.strip(),
            "properties": {
                "telegram_user_hash": telegram_user_hash(inline_query.from_user.id),
                "offset": offset,
                "result_count": result_count,
                "has_more": has_more,
                "chat_type": inline_query.chat_type,
            },
        },
        log_context={
            "analytics_event_type": AnalyticsEventType.INLINE_QUERY.value,
            "surface": "telegram_inline",
            "user_id": str(user_id) if user_id else None,
        },
    )


async def _record_inline_served_events(
    session: AsyncSession,
    *,
    inline_query: InlineQuery,
    items: list[TelegramInlineMediaResult],
    offset: int,
    result_count: int,
    is_personal: bool,
) -> None:
    if not items:
        return

    user_id = await _resolve_linked_user_id(session, telegram_user_id=inline_query.from_user.id)
    user_hash = telegram_user_hash(inline_query.from_user.id)
    for item in items:
        attribution = item.attribution
        surface = attribution.surface or "telegram_inline"
        await record_telegram_interaction_event(
            session,
            {
                "event_type": AnalyticsEventType.INLINE_SERVED,
                "user_id": user_id,
                "surface": surface,
                "refs": {"meme_id": item.meme.id, "meme_file_id": item.file.id},
                "request_id": attribution.request_id,
                "impression_id": attribution.impression_id,
                "source_algorithm": attribution.source_algorithm,
                "query": attribution.query,
                "rank": attribution.rank,
                "score": attribution.score,
                "score_components": attribution.score_components,
                "reason": attribution.reason,
                "properties": {
                    "telegram_user_hash": user_hash,
                    "chat_type": inline_query.chat_type,
                    "offset": offset,
                    "result_count": result_count,
                    "media_format": item.media_format.value,
                    "is_personal": is_personal,
                },
            },
            log_context={
                "analytics_event_type": AnalyticsEventType.INLINE_SERVED.value,
                "surface": surface,
                "meme_id": str(item.meme.id),
                "meme_file_id": str(item.file.id),
                "request_id": attribution.request_id,
                "impression_id": attribution.impression_id,
                "user_id": str(user_id) if user_id else None,
            },
        )


async def _record_chosen_inline_event(
    session: AsyncSession,
    *,
    event_type: AnalyticsEventType,
    user_id: uuid.UUID | None,
    meme_id: uuid.UUID,
    file_id: uuid.UUID,
    chosen_result: ChosenInlineResult,
    impression_id: str | None,
) -> None:
    await record_telegram_interaction_event(
        session,
        {
            "event_type": event_type,
            "user_id": user_id,
            "surface": "telegram_inline",
            "refs": {"meme_id": meme_id, "meme_file_id": file_id},
            "impression_id": impression_id,
            "query": chosen_result.query,
            "properties": {
                "telegram_user_hash": telegram_user_hash(chosen_result.from_user.id),
                "result_id": chosen_result.result_id,
            },
        },
        log_context={
            "analytics_event_type": event_type.value,
            "surface": "telegram_inline",
            "meme_id": str(meme_id),
            "meme_file_id": str(file_id),
            "impression_id": impression_id,
            "user_id": str(user_id) if user_id else None,
        },
    )


async def _resolve_linked_user_id(session: AsyncSession, *, telegram_user_id: int) -> uuid.UUID | None:
    user_id: uuid.UUID | None = await session.scalar(select(User.id).where(User.telegram_id == telegram_user_id))
    return user_id


async def _resolve_active_linked_user_id(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> tuple[uuid.UUID | None, bool]:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_user_id))
    if user is None:
        return None, False
    if user.account_type is not AccountType.FULL:
        return None, False
    if user.status is not AccountStatus.ACTIVE:
        return None, True
    return user.id, False

def _to_inline_result(candidate: TelegramInlineMediaResult) -> InlineResult | None:
    result_id = _build_result_id(
        media_format=candidate.media_format,
        file_id=candidate.file.id,
        impression_id=candidate.attribution.impression_id,
    )
    title = _build_title(candidate.meme)
    reply_markup = _build_library_reply_markup(result_id)

    if candidate.cached_file_id is not None:
        if candidate.media_format is TelegramMediaFormat.PHOTO:
            return InlineQueryResultCachedPhoto(
                id=result_id,
                photo_file_id=candidate.cached_file_id,
                title=title,
                description=_build_description(candidate.meme),
                reply_markup=reply_markup,
            )
        if _is_mpeg4_gif(candidate.file):
            return InlineQueryResultCachedMpeg4Gif(
                id=result_id,
                mpeg4_file_id=candidate.cached_file_id,
                title=title,
                reply_markup=reply_markup,
            )
        return InlineQueryResultCachedGif(
            id=result_id,
            gif_file_id=candidate.cached_file_id,
            title=title,
            reply_markup=reply_markup,
        )

    media_url = candidate.media_url
    if media_url is None:
        return None

    if candidate.media_format is TelegramMediaFormat.PHOTO:
        return InlineQueryResultPhoto(
            id=result_id,
            photo_url=media_url,
            thumbnail_url=media_url,
            photo_width=candidate.file.width,
            photo_height=candidate.file.height,
            title=title,
            description=_build_description(candidate.meme),
            reply_markup=reply_markup,
        )
    if _is_mpeg4_gif(candidate.file):
        return InlineQueryResultMpeg4Gif(
            id=result_id,
            mpeg4_url=media_url,
            thumbnail_url=media_url,
            mpeg4_width=candidate.file.width,
            mpeg4_height=candidate.file.height,
            title=title,
            reply_markup=reply_markup,
        )
    return InlineQueryResultGif(
        id=result_id,
        gif_url=media_url,
        thumbnail_url=media_url,
        gif_width=candidate.file.width,
        gif_height=candidate.file.height,
        title=title,
        reply_markup=reply_markup,
    )


def _build_library_reply_markup(result_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Favorite", callback_data=_build_library_callback_data("fav", result_id)),
                InlineKeyboardButton(text="Save", callback_data=_build_library_callback_data("save", result_id)),
                InlineKeyboardButton(text="Pin", callback_data=_build_library_callback_data("pin", result_id)),
            ]
        ]
    )

def _is_mpeg4_gif(file: MemeFileRead) -> bool:
    return file.mime_type == MPEG4_GIF_MIME_TYPE


def _build_title(meme: MemeCardRead) -> str:
    if meme.caption:
        return meme.caption[:64]
    if meme.tags:
        return ", ".join(meme.tags[:3])[:64]
    return "Meme"


def _build_description(meme: MemeCardRead) -> str | None:
    parts = []
    if meme.tags:
        parts.append("Tags: " + ", ".join(meme.tags[:5]))
    parts.append(f"Popularity: {meme.popularity_score:g}")
    return " | ".join(parts)[:128]

def _parse_offset(value: str) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def _build_result_id(*, media_format: TelegramMediaFormat, file_id: uuid.UUID, impression_id: str | None = None) -> str:
    prefix = "p" if media_format is TelegramMediaFormat.PHOTO else "a"
    if not impression_id:
        return f"{prefix}:{file_id.hex}"
    result_id = f"{prefix}:{file_id.hex}:{impression_id}"
    if len(result_id) <= 64:
        return result_id
    compact_impression_id = "imp_" + hashlib.sha256(impression_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{file_id.hex}:{compact_impression_id}"


def _parse_result_file_id(result_id: str) -> uuid.UUID | None:
    try:
        _, raw_file_id, *_ = result_id.split(":")
        return uuid.UUID(hex=raw_file_id)
    except ValueError, AttributeError:
        return None


def _parse_result_impression_id(result_id: str) -> str | None:
    try:
        _prefix, _raw_file_id, raw_impression_id = result_id.split(":", maxsplit=2)
    except ValueError:
        return None
    return raw_impression_id or None


def _build_library_callback_data(action: str, result_id: str) -> str:
    return f"{LIBRARY_CALLBACK_PREFIX}:{action}:{result_id}"


def _parse_library_callback_data(value: str | None) -> tuple[str | None, uuid.UUID | None]:
    if value is None:
        return None, None
    try:
        prefix, action, result_id = value.split(":", maxsplit=2)
    except ValueError:
        return None, None
    if prefix != LIBRARY_CALLBACK_PREFIX:
        return None, None
    return action, _parse_result_file_id(result_id)


def _build_bot_scope(settings: Settings) -> str:
    secret = settings.auth_telegram_bot_token
    if secret is None:
        raise ProviderNotConfiguredError("Telegram bot token is not configured.")

    bot_token = secret.get_secret_value().strip()
    if not bot_token:
        raise ProviderNotConfiguredError("Telegram bot token is not configured.")
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()


__all__ = [
    "INLINE_SEARCH_LIMIT",
    "InlineMediaUrlProvider",
    "LIBRARY_CALLBACK_PREFIX",
    "MemeSearchServiceFactory",
    "S3PresignedInlineMediaUrlProvider",
    "answer_inline_query",
    "build_inline_router",
    "handle_inline_library_callback",
    "record_chosen_inline_result",
]
