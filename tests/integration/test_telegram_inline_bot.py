"""Focused tests for Telegram inline meme search."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerInlineQuery, GetMe, TelegramMethod
from aiogram.types import InlineQueryResultCachedGif, InlineQueryResultCachedMpeg4Gif, InlineQueryResultCachedPhoto
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import func, select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.core.config import Settings
from memexpert.models.content import Meme, MemeFile, TelegramFileIdCache
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage, TelegramMediaFormat
from memexpert.models.user import AnalyticsEvent, InlineUsageEvent
from memexpert.schemas.meme import (
    MemeCardRead,
    MemeFileRead,
    MemeSearchPageRead,
    MemeSearchResultRead,
    MemeSearchScoreRead,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOT_TOKEN = "123456:telegram-inline-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/link/telegram/complete"
JWT_SECRET = "inline-test-auth-secret-with-32-byte-minimum"
TELEGRAM_ID = 810_220_330


class RecordingTelegramSession(BaseSession):
    """Capture inline answers while satisfying aiogram's session contract."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_methods: list[TelegramMethod[Any]] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        _ = timeout
        self.sent_methods.append(method)

        if isinstance(method, AnswerInlineQuery):
            return True

        if isinstance(method, GetMe):
            return TelegramUser.model_validate(
                {
                    "id": 999001,
                    "is_bot": True,
                    "first_name": "MemeXpert",
                    "username": BOT_USERNAME,
                },
                context={"bot": bot},
            )

        raise AssertionError(f"Unexpected Telegram API method: {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        _ = (url, headers, timeout, chunk_size, raise_for_status)
        if False:
            yield b""


class FakeMemeSearchService:
    def __init__(self, page: MemeSearchPageRead) -> None:
        self.page = page
        self.calls: list[dict[str, object]] = []

    async def search_memes(self, query: str, *, limit: int = 20, offset: int = 0) -> MemeSearchPageRead:
        self.calls.append({"query": query, "limit": limit, "offset": offset})
        return self.page


def build_bot_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        auth_jwt_secret=SecretStr(JWT_SECRET),
        auth_telegram_bot_token=SecretStr(BOT_TOKEN),
        auth_telegram_bot_username=BOT_USERNAME,
        auth_telegram_link_return_url=TypeAdapter(AnyHttpUrl).validate_python(RETURN_URL),
    )


def bot_scope() -> str:
    return hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()


async def create_meme_file(
    session: AsyncSession,
    *,
    media_type: ContentKind,
    mime_type: str,
    s3_original_key: str | None = None,
) -> tuple[Meme, MemeFile]:
    meme = Meme(media_type=media_type, language=ContentLanguage.EN, tags=[media_type.value], popularity_score=42.0)
    session.add(meme)
    await session.flush()
    file = MemeFile(
        meme_id=meme.id,
        s3_original_key=s3_original_key or f"memes/{meme.id}",
        mime_type=mime_type,
        width=640,
        height=480,
        quality_score=0.9,
        is_primary=True,
    )
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
    await session.flush()
    return meme, file


def search_page_for(
    entries: list[tuple[Meme, MemeFile]],
    *,
    limit: int = 20,
    offset: int = 0,
    total: int | None = None,
    has_more: bool = False,
) -> MemeSearchPageRead:
    now = datetime.now(UTC)
    items = []
    for meme, file in entries:
        items.append(
            MemeSearchResultRead(
                meme=MemeCardRead(
                    id=meme.id,
                    media_type=meme.media_type,
                    language=meme.language,
                    is_nsfw=meme.is_nsfw,
                    popularity_score=meme.popularity_score,
                    like_count=meme.like_count,
                    tags=list(meme.tags),
                    primary_file=MemeFileRead(
                        id=file.id,
                        mime_type=file.mime_type,
                        width=file.width,
                        height=file.height,
                        file_size_bytes=file.file_size_bytes,
                        s3_original_key=file.s3_original_key,
                        s3_web_video_key=file.s3_web_video_key,
                        blur_hash=file.blur_hash,
                        quality_score=file.quality_score,
                    ),
                    caption=f"{meme.media_type.value} meme",
                    created_at=meme.created_at or now,
                    updated_at=meme.updated_at or now,
                ),
                score=MemeSearchScoreRead(semantic=1.0, text=1.0, popularity=1.0, total=1.0),
            )
        )
    return MemeSearchPageRead(
        items=items,
        limit=limit,
        offset=offset,
        total=len(items) if total is None else total,
        has_more=has_more,
    )


async def add_file_id_cache(
    session: AsyncSession,
    *,
    file: MemeFile,
    media_format: TelegramMediaFormat,
    telegram_file_id: str,
) -> None:
    session.add(
        TelegramFileIdCache(
            meme_file_id=file.id,
            bot_scope=bot_scope(),
            media_format=media_format,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=f"unique-{telegram_file_id}",
        )
    )


async def dispatch_inline_query(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    query: str,
    offset: str = "",
    update_id: int = 1,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "inline_query": {
                "id": f"inline-{update_id}",
                "from": {
                    "id": TELEGRAM_ID,
                    "is_bot": False,
                    "first_name": "Inline",
                    "username": "inline_user",
                },
                "query": query,
                "offset": offset,
                "chat_type": "sender",
            },
        },
    )


async def dispatch_chosen_inline_result(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    result_id: str,
    query: str = "cats",
    update_id: int = 20,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "chosen_inline_result": {
                "result_id": result_id,
                "from": {
                    "id": TELEGRAM_ID,
                    "is_bot": False,
                    "first_name": "Inline",
                    "username": "inline_user",
                },
                "query": query,
            },
        },
    )


def last_inline_answer(session: RecordingTelegramSession) -> AnswerInlineQuery:
    assert session.sent_methods, "Expected an inline answer."
    method = session.sent_methods[-1]
    assert isinstance(method, AnswerInlineQuery)
    return method


@pytest.mark.asyncio
async def test_inline_plain_text_query_calls_shared_search_service_and_records_query_event(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
    await add_file_id_cache(
        migrated_db_session,
        file=file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(meme, file)]))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="  grumpy cat  ")
    finally:
        await bot.session.close()

    assert fake_service.calls == [{"query": "grumpy cat", "limit": 20, "offset": 0}]
    answer = last_inline_answer(telegram_session)
    assert answer.next_offset == ""
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedPhoto)
    assert result.photo_file_id == "cached-photo-id"

    async with postgres_session_factory() as session:
        inline_events = await session.scalar(
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(AnalyticsEvent.event_type == AnalyticsEventType.INLINE_QUERY)
        )
        inline_usage_events = await session.scalar(select(func.count()).select_from(InlineUsageEvent))
    assert inline_events == 1
    assert inline_usage_events == 0


@pytest.mark.asyncio
async def test_inline_pagination_uses_offset_and_next_offset(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
    await add_file_id_cache(
        migrated_db_session,
        file=file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-page-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(meme, file)], offset=20, total=45, has_more=True))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="cats", offset="20")
    finally:
        await bot.session.close()

    assert fake_service.calls == [{"query": "cats", "limit": 20, "offset": 20}]
    assert last_inline_answer(telegram_session).next_offset == "40"


@pytest.mark.asyncio
async def test_inline_pagination_stops_when_filtering_returns_no_sendable_results(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    video_meme, video_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.VIDEO,
        mime_type="video/mp4",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(video_meme, video_file)], total=2, has_more=True))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="videos")
    finally:
        await bot.session.close()

    answer = last_inline_answer(telegram_session)
    assert answer.results == []
    assert answer.next_offset == ""


@pytest.mark.asyncio
async def test_inline_filters_unsupported_media_and_reuses_cached_gif_file_id(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gif_meme, gif_file = await create_meme_file(migrated_db_session, media_type=ContentKind.GIF, mime_type="image/gif")
    video_meme, video_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.VIDEO,
        mime_type="video/mp4",
    )
    uncached_meme, uncached_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        s3_original_key="private/object-only.jpg",
    )
    await add_file_id_cache(
        migrated_db_session,
        file=gif_file,
        media_format=TelegramMediaFormat.ANIMATION,
        telegram_file_id="cached-gif-id",
    )
    await add_file_id_cache(
        migrated_db_session,
        file=video_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="should-not-be-used",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(
        search_page_for([(gif_meme, gif_file), (video_meme, video_file), (uncached_meme, uncached_file)])
    )
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="reaction")
    finally:
        await bot.session.close()

    answer = last_inline_answer(telegram_session)
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedGif)
    assert result.gif_file_id == "cached-gif-id"


@pytest.mark.asyncio
async def test_inline_uses_cached_mpeg4_gif_for_gif_media_stored_as_mp4(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gif_meme, gif_file = await create_meme_file(migrated_db_session, media_type=ContentKind.GIF, mime_type="video/mp4")
    await add_file_id_cache(
        migrated_db_session,
        file=gif_file,
        media_format=TelegramMediaFormat.ANIMATION,
        telegram_file_id="cached-mpeg4-gif-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(gif_meme, gif_file)]))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="animated reaction")
    finally:
        await bot.session.close()

    answer = last_inline_answer(telegram_session)
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedMpeg4Gif)
    assert result.mpeg4_file_id == "cached-mpeg4-gif-id"


@pytest.mark.asyncio
async def test_inline_empty_query_uses_shared_search_fallback_path(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _ = migrated_db_session
    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="   ")
    finally:
        await bot.session.close()

    assert fake_service.calls == [{"query": "", "limit": 20, "offset": 0}]
    assert last_inline_answer(telegram_session).results == []


@pytest.mark.asyncio
async def test_chosen_inline_result_records_meme_send_analytics(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
    await migrated_db_session.commit()

    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: FakeMemeSearchService(
            MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False)
        ),
    )
    result_id = f"p:{file.id.hex}"

    try:
        await dispatch_chosen_inline_result(dispatcher=dispatcher, bot=bot, result_id=result_id)
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_SEND)
        )
    assert event is not None
    assert event.user_id is None
    assert event.payload["meme_id"] == str(meme.id)
    assert event.payload["meme_file_id"] == str(file.id)
    assert event.payload["result_id"] == result_id
