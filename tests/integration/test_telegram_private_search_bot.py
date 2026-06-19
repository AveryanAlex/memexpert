"""Focused tests for Telegram private-chat meme search."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage, TelegramMethod
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import func, select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.bot.private_search import PRIVATE_SEARCH_LIMIT
from memexpert.core.config import Settings
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import Meme, MemeFile, MemeSeoPage
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionMembershipRole,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
)
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.meme import (
    MemeCardRead,
    MemeFileRead,
    MemeResultAttributionRead,
    MemeSearchPageRead,
    MemeSearchResultRead,
    MemeSearchScoreRead,
)
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchScope, MemeSearchService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


BOT_TOKEN = "123456:telegram-private-search-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/link/telegram/complete"
JWT_SECRET = "private-search-test-auth-secret-with-32-byte-minimum"
TELEGRAM_ID = 870_220_330


class RecordingTelegramSession(BaseSession):
    """Capture outgoing PM search messages while satisfying aiogram's session contract."""

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

        if isinstance(method, SendMessage):
            return True

        if isinstance(method, GetMe):
            return TelegramUser.model_validate(
                {
                    "id": 999004,
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

    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "service_search",
    ) -> MemeSearchPageRead:
        self.calls.append(
            {
                "query": query,
                "viewer_user_id": viewer_user_id,
                "scope": filters.scope if filters is not None else None,
                "include_nsfw": filters.include_nsfw if filters is not None else None,
                "limit": limit,
                "offset": offset,
                "surface": surface,
            }
        )
        return self.page


def build_bot_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        auth_jwt_secret=SecretStr(JWT_SECRET),
        auth_telegram_bot_token=SecretStr(BOT_TOKEN),
        auth_telegram_bot_username=BOT_USERNAME,
        auth_telegram_link_return_url=TypeAdapter(AnyHttpUrl).validate_python(RETURN_URL),
    )


async def dispatch_private_search(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
    telegram_user_id: int = TELEGRAM_ID,
    update_id: int = 1,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "message": {
                "message_id": 700 + update_id,
                "date": 1_700_000_000,
                "chat": {"id": telegram_user_id, "type": "private", "first_name": "Search"},
                "from": {"id": telegram_user_id, "is_bot": False, "first_name": "Search"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split(maxsplit=1)[0])}],
            },
        },
    )


async def create_meme_file(
    session: AsyncSession,
    *,
    media_type: ContentKind = ContentKind.IMAGE,
    mime_type: str = "image/jpeg",
    is_public: bool = True,
    author_user_id: uuid.UUID | None = None,
    tags: list[str] | None = None,
    caption: str | None = None,
    popularity_score: float = 0.0,
    s3_original_key: str | None = None,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=media_type,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=tags or ["search"],
        popularity_score=popularity_score,
        is_public=is_public,
        author_user_id=author_user_id,
    )
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=s3_original_key or f"private/originals/{meme_id}/secret-original.jpg",
        mime_type=mime_type,
        width=640,
        height=480,
        file_size_bytes=123,
        quality_score=0.9,
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    if caption is not None:
        meme.seo_page = MemeSeoPage(
            meme_id=meme.id,
            slug=f"meme-{meme.id.hex}",
            page_title=caption,
            meta_description=caption,
            alt_text=caption,
            caption=caption,
            tags=tags or [],
            model_id="test-model",
            prompt_version="test-v1",
        )
    await session.flush()
    return meme, file


def search_page_for(entries: list[tuple[Meme, MemeFile]]) -> MemeSearchPageRead:
    now = datetime.now(UTC)
    items = []
    for rank, (meme, file) in enumerate(entries, start=1):
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
                    caption=meme.seo_page.caption if meme.seo_page else None,
                    seo_page_slug=meme.seo_page.slug if meme.seo_page else None,
                    created_at=meme.created_at or now,
                    updated_at=meme.updated_at or now,
                ),
                score=MemeSearchScoreRead(semantic=0.0, text=0.0, popularity=1.0, total=1.0),
                attribution=MemeResultAttributionRead(
                    request_id="req_private_search_test",
                    impression_id=f"imp_private_search_{rank}",
                    surface="telegram_pm_search",
                    rank=rank,
                ),
            )
        )
    return MemeSearchPageRead(
        items=items,
        limit=PRIVATE_SEARCH_LIMIT,
        offset=0,
        total=len(items),
        has_more=False,
        request_id="req_private_search_test",
    )


def empty_search_page() -> MemeSearchPageRead:
    return MemeSearchPageRead(
        items=[],
        limit=PRIVATE_SEARCH_LIMIT,
        offset=0,
        total=0,
        has_more=False,
        request_id="req_private_search_empty_test",
    )


def last_message(session: RecordingTelegramSession) -> SendMessage:
    messages = [method for method in session.sent_methods if isinstance(method, SendMessage)]
    assert messages, "Expected a bot message."
    return messages[-1]


def sent_texts(session: RecordingTelegramSession) -> list[str]:
    return [str(method.text) for method in session.sent_methods if isinstance(method, SendMessage)]


def _analytics_properties(event: AnalyticsEvent) -> dict[str, object]:
    return cast("dict[str, object]", event.payload["properties"])


def _short_id(value: uuid.UUID) -> str:
    return value.hex[-8:]


@pytest.mark.asyncio
async def test_private_search_creates_telegram_user_calls_shared_search_and_records_query_event(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    meme, file = await create_meme_file(
        migrated_db_session,
        tags=["grumpy", "cat"],
        caption="Grumpy cat reaction",
        s3_original_key="pipeline/private/do-not-leak-original.jpg",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(meme, file)]))
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_private_search(dispatcher=dispatcher, bot=bot, text="/search  grumpy cat  ")
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))
        search_event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.SEARCH_QUERY)
        )

    assert created_user is not None
    assert created_user.account_type is AccountType.FULL
    assert created_user.status is AccountStatus.ACTIVE
    assert created_user.nsfw_enabled is False
    assert fake_service.calls == [
        {
            "query": "grumpy cat",
            "viewer_user_id": created_user.id,
            "scope": MemeSearchScope.ALL,
            "include_nsfw": False,
            "limit": PRIVATE_SEARCH_LIMIT,
            "offset": 0,
            "surface": "telegram_pm_search",
        }
    ]
    bot_text = str(last_message(telegram_session).text)
    assert "Grumpy cat reaction" in bot_text
    assert "grumpy, cat" in bot_text
    assert f"meme:{_short_id(meme.id)}" in bot_text
    assert "image" in bot_text
    assert "do-not-leak-original" not in bot_text
    assert "pipeline/private" not in bot_text
    assert search_event is not None
    assert search_event.user_id == created_user.id
    assert search_event.payload["surface"] == "telegram_pm_search"
    assert search_event.payload["query"] == "grumpy cat"
    assert search_event.payload["request_id"] == "req_private_search_test"
    properties = _analytics_properties(search_event)
    assert properties["result_count"] == 1
    assert properties["total"] == 1
    assert properties["telegram_user_hash"] == hashlib.sha256(f"telegram_user:{TELEGRAM_ID}".encode()).hexdigest()
    assert "telegram_user_id" not in properties


@pytest.mark.asyncio
async def test_private_search_uses_existing_user_nsfw_preference(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    linked_user = User(telegram_id=TELEGRAM_ID, nsfw_enabled=True)
    migrated_db_session.add(linked_user)
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(empty_search_page())
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_private_search(dispatcher=dispatcher, bot=bot, text="/search nsfw ok")
    finally:
        await bot.session.close()

    assert fake_service.calls == [
        {
            "query": "nsfw ok",
            "viewer_user_id": linked_user.id,
            "scope": MemeSearchScope.ALL,
            "include_nsfw": True,
            "limit": PRIVATE_SEARCH_LIMIT,
            "offset": 0,
            "surface": "telegram_pm_search",
        }
    ]
    assert "No memes found" in str(last_message(telegram_session).text)


@pytest.mark.asyncio
async def test_private_search_real_service_returns_only_accessible_public_owned_and_shared_memes(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    viewer = User(telegram_id=TELEGRAM_ID)
    stranger = User(email="pm-search-stranger@example.com")
    migrated_db_session.add_all([viewer, stranger])
    await migrated_db_session.flush()
    public_meme, _public_file = await create_meme_file(
        migrated_db_session,
        tags=["public"],
        caption="Public fallback result",
        popularity_score=10.0,
    )
    authored_private, _authored_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        author_user_id=viewer.id,
        tags=["owned"],
        caption="Owned private result",
        popularity_score=30.0,
    )
    shared_private, _shared_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        author_user_id=stranger.id,
        tags=["shared"],
        caption="Shared private result",
        popularity_score=20.0,
    )
    unauthorized_private, _unauthorized_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        author_user_id=stranger.id,
        tags=["hidden"],
        caption="Unauthorized private result",
        popularity_score=100.0,
    )
    shared_collection = Collection(owner_id=stranger.id, title="PM shared search")
    unauthorized_collection = Collection(owner_id=stranger.id, title="PM unauthorized search")
    migrated_db_session.add_all([shared_collection, unauthorized_collection])
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            CollectionMember(
                collection_id=shared_collection.id,
                user_id=viewer.id,
                role=CollectionMembershipRole.VIEWER,
            ),
            CollectionMeme(collection_id=shared_collection.id, meme_id=shared_private.id, added_by_user_id=stranger.id),
            CollectionMeme(
                collection_id=unauthorized_collection.id,
                meme_id=unauthorized_private.id,
                added_by_user_id=stranger.id,
            ),
        ]
    )
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: MemeSearchService(session),
    )

    try:
        await dispatch_private_search(dispatcher=dispatcher, bot=bot, text="/search private", update_id=101)
        await dispatch_private_search(
            dispatcher=dispatcher,
            bot=bot,
            text="/search private",
            telegram_user_id=TELEGRAM_ID + 1,
            update_id=102,
        )
    finally:
        await bot.session.close()

    authorized_text, public_only_text = sent_texts(telegram_session)
    assert "Owned private result" in authorized_text
    assert "Shared private result" in authorized_text
    assert "Public fallback result" in authorized_text
    assert f"meme:{_short_id(authored_private.id)}" in authorized_text
    assert f"meme:{_short_id(shared_private.id)}" in authorized_text
    assert f"meme:{_short_id(public_meme.id)}" in authorized_text
    assert "Unauthorized private result" not in authorized_text
    assert f"meme:{_short_id(unauthorized_private.id)}" not in authorized_text
    assert "Public fallback result" in public_only_text
    assert f"meme:{_short_id(public_meme.id)}" in public_only_text
    assert "Owned private result" not in public_only_text
    assert "Shared private result" not in public_only_text
    assert "Unauthorized private result" not in public_only_text

    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID + 1))
    assert created_user is not None
    assert created_user.account_type is AccountType.FULL
    assert created_user.status is AccountStatus.ACTIVE


@pytest.mark.asyncio
async def test_private_search_missing_query_sends_usage_and_does_not_call_search_or_create_user(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    _ = migrated_db_session
    fake_service = FakeMemeSearchService(empty_search_page())
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_private_search(dispatcher=dispatcher, bot=bot, text="/search")
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        user_count = await session.scalar(select(func.count()).select_from(User).where(User.telegram_id == TELEGRAM_ID))

    assert str(last_message(telegram_session).text) == "Use: /search <query>"
    assert fake_service.calls == []
    assert user_count == 0


@pytest.mark.asyncio
async def test_private_search_inactive_telegram_user_does_not_duplicate_or_search(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    inactive_user = User(telegram_id=TELEGRAM_ID, status=AccountStatus.DELETION_PENDING)
    migrated_db_session.add(inactive_user)
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(empty_search_page())
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_private_search(dispatcher=dispatcher, bot=bot, text="/search private")
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        user_count = await session.scalar(select(func.count()).select_from(User).where(User.telegram_id == TELEGRAM_ID))

    assert "inactive or unavailable" in str(last_message(telegram_session).text)
    assert fake_service.calls == []
    assert user_count == 1
