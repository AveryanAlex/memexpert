"""Telegram inline-mode search router for sending cached meme media."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

from aiogram import Router
from aiogram.types import (
    ChosenInlineResult,
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

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantUserSearchClient
from memexpert.core.voyage import PipelineVoyageClient
from memexpert.models.content import MemeFile, TelegramFileIdCache
from memexpert.models.enums import AnalyticsEventType, ContentKind, TelegramMediaFormat
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.meme import MemeCardRead, MemeFileRead, MemeSearchPageRead
from memexpert.services import ProviderNotConfiguredError
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)

INLINE_SEARCH_LIMIT = 20
INLINE_CACHE_TIME_SECONDS = 5
MPEG4_GIF_MIME_TYPE = "video/mp4"


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


def build_inline_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    meme_search_service_factory: MemeSearchServiceFactory | None = None,
) -> Router:
    """Build the inline-mode router backed by the shared meme search service."""

    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_service_factory = meme_search_service_factory or _build_default_search_service_factory(resolved_settings)
    bot_scope = _build_bot_scope(resolved_settings)

    router = Router(name="inline-search")

    @router.inline_query()
    async def handle_inline_query(inline_query: InlineQuery) -> None:
        await answer_inline_query(
            inline_query=inline_query,
            session_factory=resolved_session_factory,
            meme_search_service_factory=resolved_service_factory,
            bot_scope=bot_scope,
        )

    @router.chosen_inline_result()
    async def handle_chosen_inline_result(chosen_result: ChosenInlineResult) -> None:
        await record_chosen_inline_result(
            chosen_result=chosen_result,
            session_factory=resolved_session_factory,
        )

    return router


async def answer_inline_query(
    *,
    inline_query: InlineQuery,
    session_factory: AsyncSessionFactory,
    meme_search_service_factory: MemeSearchServiceFactory,
    bot_scope: str,
) -> None:
    """Search memes for a Telegram inline query and answer with sendable media results."""

    offset = _parse_offset(inline_query.offset)
    query = inline_query.query.strip()

    async with session_factory() as session:
        try:
            search_service = meme_search_service_factory(session)
            page = await search_service.search_memes(query, limit=INLINE_SEARCH_LIMIT, offset=offset)
        except Exception:
            logger.exception("Telegram inline meme search failed; returning an empty inline answer.")
            page = MemeSearchPageRead(items=[], limit=INLINE_SEARCH_LIMIT, offset=offset, total=0, has_more=False)
            results = []
        else:
            results = await _build_inline_results(session=session, page=page, bot_scope=bot_scope)

        # Search pagination is offset-based, but inline conversion may filter out
        # unsupported/private-storage items. Do not ask Telegram for another page
        # after returning zero sendable results; that can otherwise produce empty
        # scroll pages or skip supported media that the user never saw.
        next_offset = str(offset + page.limit) if page.has_more and results else ""
        await inline_query.answer(
            results,
            cache_time=INLINE_CACHE_TIME_SECONDS,
            is_personal=False,
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
        result = _to_inline_result(candidate, cached_file_id=cached_file_id)
        if result is not None:
            results.append(result)
    return results


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


def _to_inline_result(candidate: _InlineCandidate, *, cached_file_id: str | None) -> InlineResult | None:
    result_id = _build_result_id(media_format=candidate.media_format, file_id=candidate.file.id)
    title = _build_title(candidate.meme)

    if cached_file_id is not None:
        if candidate.media_format is TelegramMediaFormat.PHOTO:
            return InlineQueryResultCachedPhoto(
                id=result_id,
                photo_file_id=cached_file_id,
                title=title,
                description=_build_description(candidate.meme),
            )
        if _is_mpeg4_gif(candidate.file):
            return InlineQueryResultCachedMpeg4Gif(
                id=result_id,
                mpeg4_file_id=cached_file_id,
                title=title,
            )
        return InlineQueryResultCachedGif(id=result_id, gif_file_id=cached_file_id, title=title)

    media_url = _public_https_url(candidate.file.s3_original_key)
    if media_url is None:
        # First-send upload is deferred for the MVP: inline answers cannot upload
        # local/object-storage media, and the current DTO only exposes object keys,
        # not a public or presigned HTTPS URL that Telegram can fetch.
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
        )
    if _is_mpeg4_gif(candidate.file):
        return InlineQueryResultMpeg4Gif(
            id=result_id,
            mpeg4_url=media_url,
            thumbnail_url=media_url,
            mpeg4_width=candidate.file.width,
            mpeg4_height=candidate.file.height,
            title=title,
        )
    return InlineQueryResultGif(
        id=result_id,
        gif_url=media_url,
        thumbnail_url=media_url,
        gif_width=candidate.file.width,
        gif_height=candidate.file.height,
        title=title,
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
    "MemeSearchServiceFactory",
    "answer_inline_query",
    "build_inline_router",
    "record_chosen_inline_result",
]
