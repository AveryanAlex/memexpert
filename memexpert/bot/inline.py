"""Telegram inline-mode search router for sending cached meme media."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantUserSearchClient
from memexpert.core.storage import StorageConfigurationError, build_s3_client, get_pipeline_storage_settings
from memexpert.core.voyage import PipelineVoyageClient
from memexpert.models.collection import PinnedMeme
from memexpert.models.content import Meme as MemeModel
from memexpert.models.content import MemeFile, TelegramFileIdCache
from memexpert.models.enums import AccountStatus, AccountType, AnalyticsEventType, ContentKind, TelegramMediaFormat
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.meme import (
    MemeCardRead,
    MemeFileRead,
    MemeSearchPageRead,
    MemeSearchResultRead,
    MemeSearchScoreRead,
)
from memexpert.services import CollectionService, CollectionServiceError, ProviderNotConfiguredError
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)

INLINE_SEARCH_LIMIT = 20
INLINE_CACHE_TIME_SECONDS = 5
MPEG4_GIF_MIME_TYPE = "video/mp4"
LIBRARY_CALLBACK_PREFIX = "lib"


class InlineMemeSearchService(Protocol):
    async def search_memes(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> MemeSearchPageRead: ...


type MemeSearchServiceFactory = Callable[[AsyncSession], InlineMemeSearchService]
type InlineResult = InlineQueryResultUnion


class InlineMediaUrlProvider(Protocol):
    async def get_media_url(self, file: MemeFileRead) -> str | None: ...


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

        media_url = _public_https_url(url)
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
    resolved_service_factory = meme_search_service_factory or _build_default_search_service_factory(resolved_settings)
    resolved_media_url_provider = inline_media_url_provider or S3PresignedInlineMediaUrlProvider(resolved_settings)
    bot_scope = _build_bot_scope(resolved_settings)

    router = Router(name="inline-search")

    @router.inline_query()
    async def handle_inline_query(inline_query: InlineQuery) -> None:
        await answer_inline_query(
            inline_query=inline_query,
            session_factory=resolved_session_factory,
            meme_search_service_factory=resolved_service_factory,
            inline_media_url_provider=resolved_media_url_provider,
            bot_scope=bot_scope,
        )

    @router.chosen_inline_result()
    async def handle_chosen_inline_result(chosen_result: ChosenInlineResult) -> None:
        await record_chosen_inline_result(
            chosen_result=chosen_result,
            session_factory=resolved_session_factory,
        )

    @router.callback_query(
        lambda callback_query: callback_query.data is not None
        and callback_query.data.startswith(f"{LIBRARY_CALLBACK_PREFIX}:")
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
    meme_search_service_factory: MemeSearchServiceFactory,
    inline_media_url_provider: InlineMediaUrlProvider,
    bot_scope: str,
) -> None:
    """Search memes for a Telegram inline query and answer with sendable media results."""

    offset = _parse_offset(inline_query.offset)
    query = inline_query.query.strip()

    async with session_factory() as session:
        try:
            is_personal = False
            if query:
                search_service = meme_search_service_factory(session)
                page = await search_service.search_memes(query, limit=INLINE_SEARCH_LIMIT, offset=offset)
                results = await _build_inline_results(
                    session=session,
                    page=page,
                    bot_scope=bot_scope,
                    inline_media_url_provider=inline_media_url_provider,
                )
            else:
                linked_user = await _resolve_linked_user(session, telegram_user_id=inline_query.from_user.id)
                page, results = await _build_empty_query_results(
                    session,
                    linked_user=linked_user,
                    limit=INLINE_SEARCH_LIMIT,
                    offset=offset,
                    bot_scope=bot_scope,
                    inline_media_url_provider=inline_media_url_provider,
                )
                is_personal = linked_user is not None
        except Exception:
            logger.exception("Telegram inline meme search failed; returning an empty inline answer.")
            page = MemeSearchPageRead(items=[], limit=INLINE_SEARCH_LIMIT, offset=offset, total=0, has_more=False)
            results = []
            is_personal = False
        # Search pagination is offset-based, but inline conversion may filter out
        # unsupported/private-storage items. Do not ask Telegram for another page
        # after returning zero sendable results; that can otherwise produce empty
        # scroll pages or skip supported media that the user never saw.
        next_offset = str(offset + page.limit) if page.has_more and results else ""
        await inline_query.answer(
            results,
            cache_time=INLINE_CACHE_TIME_SECONDS,
            is_personal=is_personal,
            next_offset=next_offset,
        )
        await _record_inline_query_event(
            session,
            inline_query=inline_query,
            offset=offset,
            result_count=len(results),
            has_more=page.has_more,
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
    async with session_factory() as session:
        meme_id = None
        if file_id is not None:
            meme_id = await session.scalar(select(MemeFile.meme_id).where(MemeFile.id == file_id))

        user_id = await _resolve_linked_user_id(session, telegram_user_id=chosen_result.from_user.id)
        session.add(
            AnalyticsEvent(
                user_id=user_id,
                event_type=AnalyticsEventType.MEME_SEND,
                payload={
                    "surface": "telegram_inline",
                    "telegram_user_id": chosen_result.from_user.id,
                    "query": chosen_result.query,
                    "result_id": chosen_result.result_id,
                    "meme_id": str(meme_id) if meme_id is not None else None,
                    "meme_file_id": str(file_id) if file_id is not None else None,
                },
            )
        )
        await _commit_analytics(session)


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
                _ = await service.favorite_meme(user_id=user_id, meme_id=meme_id)
                text = "Added to Favorites."
            elif action == "save":
                _ = await service.save_meme_to_active_collection(user_id=user_id, meme_id=meme_id)
                text = "Saved to active collection."
            elif action == "pin":
                _ = await service.pin_meme(user_id=user_id, meme_id=meme_id)
                text = "Pinned."
            else:
                await callback_query.answer("This meme action is no longer supported.", show_alert=True)
                return
        except CollectionServiceError as exc:
            logger.info("Telegram inline library callback rejected: %s", exc)
            await callback_query.answer(str(exc), show_alert=True)
            return

    await callback_query.answer(text)


def _build_default_search_service_factory(settings: Settings) -> MemeSearchServiceFactory:
    def factory(session: AsyncSession) -> MemeSearchService:
        return MemeSearchService(
            session,
            text_client=PipelineMeilisearchSyncClient(),
            semantic_client=PipelineQdrantUserSearchClient(),
            query_embedding_client=CachedTextQueryEmbeddingService(
                session,
                provider=PipelineVoyageClient(),
                cache_session_factory=get_async_session_factory(),
                settings=settings,
            ),
        )

    return factory


async def _build_inline_results(
    *,
    session: AsyncSession,
    page: MemeSearchPageRead,
    bot_scope: str,
    inline_media_url_provider: InlineMediaUrlProvider,
) -> list[InlineResult]:
    candidates = [_to_inline_candidate(item.meme) for item in page.items]
    supported_candidates = [candidate for candidate in candidates if candidate is not None]
    cache = await _load_file_id_cache(
        session,
        file_formats={candidate.file.id: candidate.media_format for candidate in supported_candidates},
        bot_scope=bot_scope,
    )

    results: list[InlineResult] = []
    for candidate in supported_candidates:
        cached_file_id = cache.get((candidate.file.id, candidate.media_format))
        media_url = (
            None
            if cached_file_id is not None
            else await _resolve_inline_media_url(candidate.file, inline_media_url_provider)
        )
        result = _to_inline_result(candidate, cached_file_id=cached_file_id, media_url=media_url)
        if result is not None:
            results.append(result)
    return results


async def _resolve_inline_media_url(file: MemeFileRead, provider: InlineMediaUrlProvider) -> str | None:
    media_url = _public_https_url(file.s3_original_key)
    if media_url is not None:
        return media_url

    media_url = await provider.get_media_url(file)
    if media_url is None:
        logger.info(
            "Telegram inline private media was skipped because no sendable URL exists for meme_file_id=%s.",
            file.id,
        )
    return media_url


async def _build_empty_query_results(
    session: AsyncSession,
    *,
    linked_user: User | None,
    limit: int,
    offset: int,
    bot_scope: str,
    inline_media_url_provider: InlineMediaUrlProvider,
) -> tuple[MemeSearchPageRead, list[InlineResult]]:
    selected_page = MemeSearchPageRead(items=[], limit=limit, offset=offset, total=0, has_more=False)

    if linked_user is not None:
        page = await _build_pinned_memes_page(session, user=linked_user, limit=limit, offset=offset)
        selected_page = page
        results = await _build_inline_results(
            session=session,
            page=page,
            bot_scope=bot_scope,
            inline_media_url_provider=inline_media_url_provider,
        )
        if results:
            return page, results

        page = await _build_recent_sends_page(session, user=linked_user, limit=limit, offset=offset)
        selected_page = page
        results = await _build_inline_results(
            session=session,
            page=page,
            bot_scope=bot_scope,
            inline_media_url_provider=inline_media_url_provider,
        )
        if results:
            return page, results

    page = await _build_popular_memes_page(
        session,
        include_nsfw=linked_user.nsfw_enabled if linked_user else False,
        limit=limit,
        offset=offset,
    )
    selected_page = page
    results = await _build_inline_results(
        session=session,
        page=page,
        bot_scope=bot_scope,
        inline_media_url_provider=inline_media_url_provider,
    )
    if results:
        return page, results

    return selected_page, []


async def _build_pinned_memes_page(
    session: AsyncSession,
    *,
    user: User,
    limit: int,
    offset: int,
) -> MemeSearchPageRead:
    stmt = (
        select(MemeModel)
        .join(PinnedMeme, PinnedMeme.meme_id == MemeModel.id)
        .options(selectinload(MemeModel.primary_file), selectinload(MemeModel.seo_page))
        .where(PinnedMeme.user_id == user.id)
        .order_by(PinnedMeme.position.asc(), PinnedMeme.pinned_at.desc())
    )
    if not user.nsfw_enabled:
        stmt = stmt.where(MemeModel.is_nsfw.is_(False))

    result = await session.execute(stmt)
    memes = list(result.scalars().unique().all())
    return _page_from_memes(memes, limit=limit, offset=offset)


async def _build_recent_sends_page(
    session: AsyncSession,
    *,
    user: User,
    limit: int,
    offset: int,
) -> MemeSearchPageRead:
    result = await session.execute(
        select(AnalyticsEvent.payload)
        .where(AnalyticsEvent.user_id == user.id, AnalyticsEvent.event_type == AnalyticsEventType.MEME_SEND)
        .order_by(AnalyticsEvent.occurred_at.desc())
        .limit(200)
    )

    meme_ids: list[uuid.UUID] = []
    file_ids: list[uuid.UUID] = []
    for payload in result.scalars():
        meme_id = _parse_payload_uuid(payload.get("meme_id"))
        if meme_id is not None and meme_id not in meme_ids:
            meme_ids.append(meme_id)
            continue

        file_id = _parse_payload_uuid(payload.get("meme_file_id"))
        if file_id is not None and file_id not in file_ids:
            file_ids.append(file_id)

    if file_ids:
        file_result = await session.execute(
            select(MemeFile.id, MemeFile.meme_id).where(MemeFile.id.in_(tuple(file_ids)))
        )
        for file_id, meme_id in file_result.all():
            _ = file_id
            if meme_id not in meme_ids:
                meme_ids.append(meme_id)

    if not meme_ids:
        return MemeSearchPageRead(items=[], limit=limit, offset=offset, total=0, has_more=False)

    stmt = (
        select(MemeModel)
        .options(selectinload(MemeModel.primary_file), selectinload(MemeModel.seo_page))
        .where(MemeModel.id.in_(tuple(meme_ids)))
    )
    if not user.nsfw_enabled:
        stmt = stmt.where(MemeModel.is_nsfw.is_(False))
    meme_result = await session.execute(stmt)
    memes_by_id = {meme.id: meme for meme in meme_result.scalars().unique().all()}
    memes = [memes_by_id[meme_id] for meme_id in meme_ids if meme_id in memes_by_id]
    return _page_from_memes(memes, limit=limit, offset=offset)


async def _build_popular_memes_page(
    session: AsyncSession,
    *,
    include_nsfw: bool,
    limit: int,
    offset: int,
) -> MemeSearchPageRead:
    stmt = (
        select(MemeModel)
        .options(selectinload(MemeModel.primary_file), selectinload(MemeModel.seo_page))
        .where(MemeModel.is_public.is_(True))
        .order_by(MemeModel.popularity_score.desc(), MemeModel.created_at.desc())
    )
    if not include_nsfw:
        stmt = stmt.where(MemeModel.is_nsfw.is_(False))

    result = await session.execute(stmt)
    memes = list(result.scalars().unique().all())
    return _page_from_memes(memes, limit=limit, offset=offset)


def _page_from_memes(memes: list[MemeModel], *, limit: int, offset: int) -> MemeSearchPageRead:
    page_memes = memes[offset : offset + limit]
    return MemeSearchPageRead(
        items=[_to_search_result_read(meme) for meme in page_memes],
        limit=limit,
        offset=offset,
        total=len(memes),
        has_more=offset + limit < len(memes),
    )


def _to_search_result_read(meme: MemeModel) -> MemeSearchResultRead:
    return MemeSearchResultRead(
        meme=MemeCardRead(
            id=meme.id,
            media_type=meme.media_type,
            language=meme.language,
            is_nsfw=meme.is_nsfw,
            popularity_score=meme.popularity_score,
            like_count=meme.like_count,
            tags=list(meme.tags),
            primary_file=_to_file_read(meme.primary_file) if meme.primary_file else None,
            caption=meme.seo_page.caption if meme.seo_page else None,
            created_at=meme.created_at,
            updated_at=meme.updated_at,
        ),
        score=MemeSearchScoreRead(
            semantic=0.0,
            text=0.0,
            popularity=meme.popularity_score,
            total=meme.popularity_score,
        ),
    )


def _to_file_read(file: MemeFile) -> MemeFileRead:
    return MemeFileRead(
        id=file.id,
        mime_type=file.mime_type,
        width=file.width,
        height=file.height,
        file_size_bytes=file.file_size_bytes,
        s3_original_key=file.s3_original_key,
        s3_web_video_key=file.s3_web_video_key,
        blur_hash=file.blur_hash,
        quality_score=file.quality_score,
    )


async def _load_file_id_cache(
    session: AsyncSession,
    *,
    file_formats: dict[uuid.UUID, TelegramMediaFormat],
    bot_scope: str,
) -> dict[tuple[uuid.UUID, TelegramMediaFormat], str]:
    if not file_formats:
        return {}

    result = await session.execute(
        select(TelegramFileIdCache).where(
            TelegramFileIdCache.meme_file_id.in_(tuple(file_formats)),
            TelegramFileIdCache.bot_scope == bot_scope,
        )
    )
    cache: dict[tuple[uuid.UUID, TelegramMediaFormat], str] = {}
    for row in result.scalars():
        expected_format = file_formats.get(row.meme_file_id)
        if row.media_format == expected_format:
            cache[(row.meme_file_id, row.media_format)] = row.telegram_file_id
    return cache


async def _record_inline_query_event(
    session: AsyncSession,
    *,
    inline_query: InlineQuery,
    offset: int,
    result_count: int,
    has_more: bool,
) -> None:
    user_id = await _resolve_linked_user_id(session, telegram_user_id=inline_query.from_user.id)
    session.add(
        AnalyticsEvent(
            user_id=user_id,
            event_type=AnalyticsEventType.INLINE_QUERY,
            payload={
                "surface": "telegram_inline",
                "telegram_user_id": inline_query.from_user.id,
                "query": inline_query.query.strip(),
                "offset": offset,
                "result_count": result_count,
                "has_more": has_more,
                "chat_type": inline_query.chat_type,
            },
        )
    )
    await _commit_analytics(session)


async def _resolve_linked_user_id(session: AsyncSession, *, telegram_user_id: int) -> uuid.UUID | None:
    user_id: uuid.UUID | None = await session.scalar(select(User.id).where(User.telegram_id == telegram_user_id))
    return user_id


async def _resolve_linked_user(session: AsyncSession, *, telegram_user_id: int) -> User | None:
    user: User | None = await session.scalar(
        select(User).where(
            User.telegram_id == telegram_user_id,
            User.account_type == AccountType.FULL,
            User.status == AccountStatus.ACTIVE,
        )
    )
    return user


async def _resolve_active_linked_user_id(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> tuple[uuid.UUID | None, bool]:
    row = await session.execute(
        select(User.id, User.account_type, User.status).where(User.telegram_id == telegram_user_id)
    )
    user = row.one_or_none()
    if user is None:
        return None, False
    user_id, account_type, status = user
    if account_type is not AccountType.FULL:
        return None, False
    if status is not AccountStatus.ACTIVE:
        return None, True
    return user_id, False


async def _commit_analytics(session: AsyncSession) -> None:
    try:
        await session.commit()
    except SQLAlchemyError:
        logger.exception("Telegram inline analytics write failed.")
        await session.rollback()


class _InlineCandidate:
    def __init__(self, *, meme: MemeCardRead, file: MemeFileRead, media_format: TelegramMediaFormat) -> None:
        self.meme = meme
        self.file = file
        self.media_format = media_format


def _to_inline_candidate(meme: MemeCardRead) -> _InlineCandidate | None:
    file = meme.primary_file
    if file is None:
        return None

    media_format = _resolve_telegram_media_format(meme=meme, file=file)
    if media_format is None:
        return None
    return _InlineCandidate(meme=meme, file=file, media_format=media_format)


def _to_inline_result(
    candidate: _InlineCandidate,
    *,
    cached_file_id: str | None,
    media_url: str | None,
) -> InlineResult | None:
    result_id = _build_result_id(media_format=candidate.media_format, file_id=candidate.file.id)
    title = _build_title(candidate.meme)
    reply_markup = _build_library_reply_markup(result_id)

    if cached_file_id is not None:
        if candidate.media_format is TelegramMediaFormat.PHOTO:
            return InlineQueryResultCachedPhoto(
                id=result_id,
                photo_file_id=cached_file_id,
                title=title,
                description=_build_description(candidate.meme),
                reply_markup=reply_markup,
            )
        if _is_mpeg4_gif(candidate.file):
            return InlineQueryResultCachedMpeg4Gif(
                id=result_id,
                mpeg4_file_id=cached_file_id,
                title=title,
                reply_markup=reply_markup,
            )
        return InlineQueryResultCachedGif(
            id=result_id,
            gif_file_id=cached_file_id,
            title=title,
            reply_markup=reply_markup,
        )

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


def _resolve_telegram_media_format(*, meme: MemeCardRead, file: MemeFileRead) -> TelegramMediaFormat | None:
    if meme.media_type is ContentKind.GIF or file.mime_type in {"image/gif", MPEG4_GIF_MIME_TYPE}:
        return TelegramMediaFormat.ANIMATION
    if meme.media_type is ContentKind.IMAGE:
        return TelegramMediaFormat.PHOTO
    return None


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


def _public_https_url(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    return None


def _parse_payload_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _parse_offset(value: str) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def _build_result_id(*, media_format: TelegramMediaFormat, file_id: uuid.UUID) -> str:
    prefix = "p" if media_format is TelegramMediaFormat.PHOTO else "a"
    return f"{prefix}:{file_id.hex}"


def _parse_result_file_id(result_id: str) -> uuid.UUID | None:
    try:
        _, raw_file_id = result_id.split(":", maxsplit=1)
        return uuid.UUID(hex=raw_file_id)
    except (ValueError, AttributeError):
        return None


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
