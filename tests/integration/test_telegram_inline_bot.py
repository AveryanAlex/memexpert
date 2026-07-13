"""Focused tests for Telegram inline meme search."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, AnswerInlineQuery, GetMe, TelegramMethod
from aiogram.types import (
    InlineQueryResultCachedGif,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultPhoto,
)
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import func, select, update

from memexpert.bot import inline as inline_module
from memexpert.bot.inline import S3PresignedInlineMediaUrlProvider
from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.core.config import Settings
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile, MemeSource, MemeSourceEngagementSnapshot, TelegramFileIdCache
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionMembershipRole,
    ContentKind,
    ContentLanguage,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
    TelegramMediaFormat,
)
from memexpert.models.user import AnalyticsEvent, InlineUsageEvent, User
from memexpert.schemas.meme import (
    MemeCardRead,
    MemeFileRead,
    MemeResultAttributionRead,
    MemeSearchPageRead,
    MemeSearchResultRead,
    MemeSearchScoreRead,
)
from memexpert.services import UserService
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchScope, MemeSearchService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOT_TOKEN = "123456:telegram-inline-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/account/telegram/complete"
JWT_SECRET = "inline-test-auth-secret-with-32-byte-minimum"
TELEGRAM_ID = 810_220_330
_DERIVED_POPULARITY_ATTR = "_derived_popularity_score"


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

        if isinstance(method, AnswerCallbackQuery):
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

    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "telegram_inline_search",
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


class FakeInlineMediaUrlProvider:
    def __init__(self, urls_by_key: dict[str, str] | None = None) -> None:
        self.urls_by_key = urls_by_key or {}
        self.calls: list[MemeFileRead] = []

    async def get_media_url(self, file: MemeFileRead) -> str | None:
        self.calls.append(file)
        return self.urls_by_key.get(file.s3_original_key)


class FakePresignClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.calls: list[dict[str, Any]] = []

    def generate_presigned_url(self, operation_name: str, **kwargs: Any) -> str:
        self.calls.append({"operation_name": operation_name, **kwargs})
        return self.url


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


def _analytics_properties(event: AnalyticsEvent) -> dict[str, object]:
    return cast("dict[str, object]", event.payload["properties"])


def _analytics_refs(event: AnalyticsEvent) -> dict[str, object]:
    return cast("dict[str, object]", event.payload["refs"])


async def create_meme_file(
    session: AsyncSession,
    *,
    media_type: ContentKind,
    mime_type: str,
    is_public: bool = True,
    uploader_user_id: uuid.UUID | None = None,
    s3_original_key: str | None = None,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=media_type,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=[media_type.value],
        is_public=is_public,
    )
    setattr(meme, _DERIVED_POPULARITY_ATTR, 42.0)
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        s3_original_key=s3_original_key or f"memes/{meme_id}",
        mime_type=mime_type,
        width=640,
        height=480,
        quality_score=0.9,
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()
    if uploader_user_id is not None:
        collection_id = await session.scalar(
            select(User.active_save_collection_id).where(User.id == uploader_user_id)
        )
        if collection_id is None:
            collection = Collection(owner_id=uploader_user_id, title=f"Inline bot upload {meme.id}")
            session.add(collection)
            await session.flush()
            collection_id = collection.id
        session.add(
            CollectionMeme(collection_id=collection_id, meme_id=meme.id, added_by_user_id=uploader_user_id)
        )
        await session.flush()
    return meme, file


async def set_source_popularity_score(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    score: float,
) -> None:
    source = MemeSource(
        file_id=meme_file.id,
        platform=SourcePlatform.TELEGRAM,
        source_id=f"inline-bot-{meme_file.id.hex}",
        post_id="1",
        is_first_source=True,
        source_alive=True,
    )
    session.add(source)
    await session.flush()
    session.add(
        MemeSourceEngagementSnapshot(
            meme_source_id=source.id,
            capture_reason=SourceEngagementCaptureReason.MANUAL_REFRESH,
            view_count=max(1, int(score)),
            reactions={},
            reaction_count=0,
            comment_count=None,
            forward_count=0,
            comments_state=SourceEngagementCommentsState.UNKNOWN,
            fetch_status=SourceEngagementFetchStatus.SUCCESS,
            source_alive=True,
            raw_metrics={"test": True},
        )
    )
    await session.flush()


def search_page_for(
    entries: list[tuple[Meme, MemeFile]],
    *,
    limit: int = 20,
    offset: int = 0,
    total: int | None = None,
    has_more: bool = False,
) -> MemeSearchPageRead:
    now = datetime.now(UTC)
    request_id = "req_inline_test"
    items = []
    for rank, (meme, file) in enumerate(entries, start=offset + 1):
        items.append(
            MemeSearchResultRead(
                meme=MemeCardRead(
                    id=meme.id,
                    media_type=meme.media_type,
                    language=meme.language,
                    is_nsfw=meme.is_nsfw,
                    popularity_score=float(getattr(meme, "_derived_popularity_score", 0.0)),
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
                attribution=MemeResultAttributionRead(
                    request_id=request_id,
                    impression_id=f"imp_inline_{rank}",
                    surface="telegram_inline_search",
                    source_algorithm="hybrid_search",
                    rank=rank,
                    query="cats",
                    score=1.0,
                    score_components={"semantic": 1.0, "text": 1.0, "popularity": 1.0, "total": 1.0},
                ),
            )
        )
    return MemeSearchPageRead(
        items=items,
        limit=limit,
        offset=offset,
        total=len(items) if total is None else total,
        has_more=has_more,
        request_id=request_id,
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
    telegram_user_id: int = TELEGRAM_ID,
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
                    "id": telegram_user_id,
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


async def dispatch_library_callback(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    data: str,
    update_id: int = 30,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {
                    "id": TELEGRAM_ID,
                    "is_bot": False,
                    "first_name": "Inline",
                    "username": "inline_user",
                },
                "chat_instance": "inline-chat-instance",
                "inline_message_id": f"inline-message-{update_id}",
                "data": data,
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

    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert answer.next_offset == ""
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedPhoto)
    assert result.id == f"p:{file.id.hex}:imp_inline_1"
    assert len(result.id) <= 64
    assert result.photo_file_id == "cached-photo-id"
    assert result.reply_markup is not None
    assert [button.text for button in result.reply_markup.inline_keyboard[0]] == ["Favorite", "Save", "Pin"]

    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))
        inline_event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.INLINE_QUERY)
        )
        served_event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.INLINE_SERVED)
        )
        inline_usage_events = await session.scalar(select(func.count()).select_from(InlineUsageEvent))
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
            "limit": 20,
            "offset": 0,
            "surface": "telegram_inline_search",
        }
    ]
    assert inline_event is not None
    assert inline_event.user_id == created_user.id
    inline_properties = _analytics_properties(inline_event)
    assert inline_event.payload["surface"] == "telegram_inline"
    assert inline_event.payload["query"] == "grumpy cat"
    assert inline_properties["result_count"] == 1
    assert inline_properties["offset"] == 0
    assert inline_properties["has_more"] is False
    assert inline_properties["chat_type"] == "sender"
    assert (
        inline_properties["telegram_user_hash"]
        == hashlib.sha256(f"telegram_user:{TELEGRAM_ID}".encode()).hexdigest()
    )
    assert "telegram_user_id" not in inline_properties
    assert served_event is not None
    assert served_event.user_id == created_user.id
    served_properties = _analytics_properties(served_event)
    served_refs = _analytics_refs(served_event)
    assert served_event.payload["surface"] == "telegram_inline_search"
    assert served_refs == {"meme_file_id": str(file.id), "meme_id": str(meme.id)}
    assert served_event.payload["request_id"] == "req_inline_test"
    assert served_event.payload["impression_id"] == "imp_inline_1"
    assert served_event.payload["source_algorithm"] == "hybrid_search"
    assert served_event.payload["rank"] == 1
    assert served_properties["media_format"] == "photo"
    assert served_properties["is_personal"] is True
    assert inline_usage_events == 0


@pytest.mark.asyncio
async def test_inline_plain_text_query_uses_linked_full_user_scope_and_nsfw_preference(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    linked_user = User(
        status=AccountStatus.ACTIVE,
        telegram_id=TELEGRAM_ID,
        nsfw_enabled=True,
    )
    migrated_db_session.add(linked_user)
    meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
    await add_file_id_cache(
        migrated_db_session,
        file=file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="linked-user-cached-photo-id",
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
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="linked scope")
    finally:
        await bot.session.close()

    assert fake_service.calls == [
        {
            "query": "linked scope",
            "viewer_user_id": linked_user.id,
            "scope": MemeSearchScope.ALL,
            "include_nsfw": True,
            "limit": 20,
            "offset": 0,
            "surface": "telegram_inline_search",
        }
    ]
    assert last_inline_answer(telegram_session).is_personal is True


@pytest.mark.asyncio
async def test_inline_plain_text_query_shows_private_and_shared_memes_only_to_authorized_user(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    viewer = User(telegram_id=TELEGRAM_ID)
    stranger = User(email="inline-bot-stranger@example.com")
    migrated_db_session.add_all([viewer, stranger])
    await migrated_db_session.flush()
    public_meme, public_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    authored_private, authored_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        is_public=False,
        uploader_user_id=viewer.id,
    )
    shared_private, shared_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        is_public=False,
        uploader_user_id=stranger.id,
    )
    unauthorized_private, unauthorized_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        is_public=False,
        uploader_user_id=stranger.id,
    )
    await set_source_popularity_score(migrated_db_session, meme_file=authored_file, score=30.0)
    await set_source_popularity_score(migrated_db_session, meme_file=shared_file, score=20.0)
    await set_source_popularity_score(migrated_db_session, meme_file=public_file, score=10.0)
    await set_source_popularity_score(migrated_db_session, meme_file=unauthorized_file, score=100.0)
    shared_collection = Collection(owner_id=stranger.id, title="Inline bot shared search")
    unauthorized_collection = Collection(owner_id=stranger.id, title="Inline bot unauthorized search")
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
    for file, cached_id in [
        (authored_file, "cached-inline-bot-authored"),
        (shared_file, "cached-inline-bot-shared"),
        (public_file, "cached-inline-bot-public"),
        (unauthorized_file, "cached-inline-bot-unauthorized"),
    ]:
        await add_file_id_cache(
            migrated_db_session,
            file=file,
            media_format=TelegramMediaFormat.PHOTO,
            telegram_file_id=cached_id,
        )
    await migrated_db_session.commit()

    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: MemeSearchService(session),
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="private", update_id=101)
        authorized_answer = last_inline_answer(telegram_session)
        await dispatch_inline_query(
            dispatcher=dispatcher,
            bot=bot,
            query="private",
            telegram_user_id=TELEGRAM_ID + 1,
            update_id=102,
        )
        unauthorized_answer = last_inline_answer(telegram_session)
    finally:
        await bot.session.close()

    assert authorized_answer.is_personal is True
    authorized_file_ids = [
        result.photo_file_id
        for result in authorized_answer.results
        if isinstance(result, InlineQueryResultCachedPhoto)
    ]
    assert authorized_file_ids == [
        "cached-inline-bot-authored",
        "cached-inline-bot-shared",
        "cached-inline-bot-public",
    ]
    assert unauthorized_answer.is_personal is True
    unauthorized_file_ids = [
        result.photo_file_id
        for result in unauthorized_answer.results
        if isinstance(result, InlineQueryResultCachedPhoto)
    ]
    assert unauthorized_file_ids == ["cached-inline-bot-public"]


@pytest.mark.asyncio
async def test_inline_analytics_failure_keeps_answer_and_logs_structured_context(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
    await add_file_id_cache(
        migrated_db_session,
        file=file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-failure-photo-id",
    )
    await migrated_db_session.commit()

    writer_calls: list[object] = []
    logged_extras: list[dict[str, object]] = []

    async def fail_record_interaction_event(self: object, event: object) -> object:
        _ = (self, event)
        writer_calls.append(event)
        raise RuntimeError("analytics database is unavailable")

    def record_log_exception(message: str, *args: object, **kwargs: object) -> None:
        _ = (message, args)
        extra = kwargs.get("extra")
        if isinstance(extra, dict):
            logged_extras.append(cast("dict[str, object]", extra))

    monkeypatch.setattr(
        "memexpert.services.analytics.AnalyticsService.record_interaction_event",
        fail_record_interaction_event,
    )
    monkeypatch.setattr("memexpert.bot.analytics.logger.exception", record_log_exception)
    fake_service = FakeMemeSearchService(search_page_for([(meme, file)]))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="still answer")
    finally:
        await bot.session.close()

    answer = last_inline_answer(telegram_session)
    assert len(answer.results) == 1
    assert writer_calls
    assert any(extra.get("event") == "telegram_analytics_write_failed" for extra in logged_extras)
    assert any(extra.get("analytics_event_type") == "inline_served" for extra in logged_extras)


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

    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))

    assert created_user is not None
    assert fake_service.calls == [
        {
            "query": "cats",
            "viewer_user_id": created_user.id,
            "scope": MemeSearchScope.ALL,
            "include_nsfw": False,
            "limit": 20,
            "offset": 20,
            "surface": "telegram_inline_search",
        }
    ]
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert answer.next_offset == "40"


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
    assert result.id == f"a:{gif_file.id.hex}:imp_inline_1"
    assert len(result.id) <= 64
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
async def test_inline_uses_injected_media_url_provider_for_private_uncached_media(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    private_meme, private_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        s3_original_key="private/original.jpg",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(private_meme, private_file)]))
    media_provider = FakeInlineMediaUrlProvider(
        {"private/original.jpg": "https://storage.example.test/private/original.jpg?sig=1"}
    )
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
        inline_media_url_provider=media_provider,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="private meme")
    finally:
        await bot.session.close()

    assert [file.id for file in media_provider.calls] == [private_file.id]
    answer = last_inline_answer(telegram_session)
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultPhoto)
    assert result.id == f"p:{private_file.id.hex}:imp_inline_1"
    assert str(result.photo_url) == "https://storage.example.test/private/original.jpg?sig=1"


@pytest.mark.asyncio
async def test_inline_keeps_public_https_media_sendable_without_media_url_provider_call(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    public_meme, public_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        s3_original_key="https://cdn.example.test/public/original.jpg",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(public_meme, public_file)]))
    media_provider = FakeInlineMediaUrlProvider()
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
        inline_media_url_provider=media_provider,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="public meme")
    finally:
        await bot.session.close()

    assert media_provider.calls == []
    answer = last_inline_answer(telegram_session)
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultPhoto)
    assert str(result.photo_url) == "https://cdn.example.test/public/original.jpg"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("presigned_url", "expected_url"),
    [
        (
            "https://storage.example.test/private/original.jpg?sig=1",
            "https://storage.example.test/private/original.jpg?sig=1",
        ),
        ("http://storage.example.test/private/original.jpg?sig=1", None),
    ],
)
async def test_s3_presigned_inline_media_url_provider_returns_only_https_presigned_urls(
    postgres_async_url: str,
    monkeypatch: pytest.MonkeyPatch,
    presigned_url: str,
    expected_url: str | None,
) -> None:
    fake_client = FakePresignClient(presigned_url)
    monkeypatch.setattr(inline_module, "build_s3_client", lambda settings: fake_client)
    monkeypatch.setattr(
        inline_module,
        "get_pipeline_storage_settings",
        lambda settings: SimpleNamespace(bucket="inline-test-bucket"),
    )
    file = MemeFileRead(
        id=uuid.uuid4(),
        mime_type="image/jpeg",
        width=640,
        height=480,
        file_size_bytes=None,
        s3_original_key="private/original.jpg",
        s3_web_video_key=None,
        blur_hash=None,
        quality_score=0.9,
    )

    provider = S3PresignedInlineMediaUrlProvider(build_bot_settings(postgres_async_url))

    assert await provider.get_media_url(file) == expected_url
    assert fake_client.calls == [
        {
            "operation_name": "get_object",
            "Params": {"Bucket": "inline-test-bucket", "Key": "private/original.jpg"},
            "ExpiresIn": 300,
        }
    ]


@pytest.mark.asyncio
async def test_inline_skips_private_uncached_media_when_media_url_provider_is_unavailable(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    private_meme, private_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
        s3_original_key="private/missing-url.jpg",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(search_page_for([(private_meme, private_file)], total=2, has_more=True))
    media_provider = FakeInlineMediaUrlProvider()
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
        inline_media_url_provider=media_provider,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="private meme")
    finally:
        await bot.session.close()

    assert [file.id for file in media_provider.calls] == [private_file.id]
    answer = last_inline_answer(telegram_session)
    assert answer.results == []
    assert answer.next_offset == ""


@pytest.mark.asyncio
async def test_inline_empty_query_for_linked_user_returns_pins_then_popular_and_is_personal(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = User(telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    pinned_meme, pinned_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    popular_meme, popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=popular_file, score=900.0)
    migrated_db_session.add(PinnedMeme(user=user, meme=pinned_meme, position=1))
    await add_file_id_cache(
        migrated_db_session,
        file=pinned_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-pinned-photo-id",
    )
    await add_file_id_cache(
        migrated_db_session,
        file=popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-popular-photo-id",
    )
    await migrated_db_session.commit()

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

    assert fake_service.calls == []
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert len(answer.results) == 2
    first_result = answer.results[0]
    second_result = answer.results[1]
    assert isinstance(first_result, InlineQueryResultCachedPhoto)
    assert isinstance(second_result, InlineQueryResultCachedPhoto)
    assert first_result.photo_file_id == "cached-pinned-photo-id"
    assert second_result.photo_file_id == "cached-popular-photo-id"


@pytest.mark.asyncio
async def test_inline_empty_query_for_linked_user_returns_recent_then_popular_when_no_pins(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = User(telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    recent_meme, recent_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    popular_meme, popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=popular_file, score=900.0)
    migrated_db_session.add(
        AnalyticsEvent(
            user=user,
            event_type=AnalyticsEventType.MEME_SEND,
            payload={"meme_id": str(recent_meme.id), "meme_file_id": str(recent_file.id)},
        )
    )
    await add_file_id_cache(
        migrated_db_session,
        file=recent_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-recent-photo-id",
    )
    await add_file_id_cache(
        migrated_db_session,
        file=popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-popular-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="")
    finally:
        await bot.session.close()

    assert fake_service.calls == []
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert len(answer.results) == 2
    first_result = answer.results[0]
    second_result = answer.results[1]
    assert isinstance(first_result, InlineQueryResultCachedPhoto)
    assert isinstance(second_result, InlineQueryResultCachedPhoto)
    assert first_result.photo_file_id == "cached-recent-photo-id"
    assert second_result.photo_file_id == "cached-popular-photo-id"


@pytest.mark.asyncio
async def test_inline_empty_query_recent_send_personalization_reads_strict_refs(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = User(telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    recent_meme, recent_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    popular_meme, popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=popular_file, score=900.0)
    migrated_db_session.add(
        AnalyticsEvent(
            user=user,
            event_type=AnalyticsEventType.MEME_SEND,
            payload={
                "surface": "telegram_inline",
                "refs": {"meme_id": str(recent_meme.id), "meme_file_id": str(recent_file.id)},
            },
        )
    )
    await add_file_id_cache(
        migrated_db_session,
        file=recent_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-strict-recent-photo-id",
    )
    await add_file_id_cache(
        migrated_db_session,
        file=popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-strict-popular-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="")
    finally:
        await bot.session.close()

    answer = last_inline_answer(telegram_session)
    assert len(answer.results) == 2
    first_result = answer.results[0]
    assert isinstance(first_result, InlineQueryResultCachedPhoto)
    assert first_result.photo_file_id == "cached-strict-recent-photo-id"


@pytest.mark.asyncio
async def test_inline_empty_query_for_linked_user_falls_back_to_popular_when_no_pins_or_recents(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = User(status=AccountStatus.ACTIVE, telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    popular_meme, popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=popular_file, score=900.0)
    await add_file_id_cache(
        migrated_db_session,
        file=popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-linked-popular-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="")
    finally:
        await bot.session.close()

    assert fake_service.calls == []
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedPhoto)
    assert result.id.startswith(f"p:{popular_file.id.hex}:imp_")
    assert len(result.id) <= 64
    assert result.photo_file_id == "cached-linked-popular-photo-id"


@pytest.mark.asyncio
async def test_inline_empty_query_skips_unsendable_pin_and_uses_later_popular_fallback(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = User(status=AccountStatus.ACTIVE, telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    video_meme, _ = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.VIDEO,
        mime_type="video/mp4",
    )
    popular_meme, popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=popular_file, score=900.0)
    migrated_db_session.add(PinnedMeme(user=user, meme=video_meme, position=1))
    await add_file_id_cache(
        migrated_db_session,
        file=popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-popular-after-unsendable-pin-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="")
    finally:
        await bot.session.close()

    assert fake_service.calls == []
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedPhoto)
    assert result.photo_file_id == "cached-popular-after-unsendable-pin-id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        AccountStatus.DELETION_PENDING,
        AccountStatus.DELETED,
    ],
)
async def test_inline_empty_query_treats_ineligible_linked_users_as_guests(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    status: AccountStatus,
) -> None:
    user = User(status=status, telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    popular_meme, popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=popular_file, score=900.0)
    await add_file_id_cache(
        migrated_db_session,
        file=popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-guest-treated-popular-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="")
    finally:
        await bot.session.close()

    assert fake_service.calls == []
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is False
    assert len(answer.results) == 1
    result = answer.results[0]
    assert isinstance(result, InlineQueryResultCachedPhoto)
    assert result.photo_file_id == "cached-guest-treated-popular-photo-id"


@pytest.mark.asyncio
async def test_inline_empty_query_for_new_user_returns_popular_public_memes_personal(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    less_popular_meme, less_popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=less_popular_file, score=10.0)
    more_popular_meme, more_popular_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    await set_source_popularity_score(migrated_db_session, meme_file=more_popular_file, score=99.0)
    await add_file_id_cache(
        migrated_db_session,
        file=less_popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-less-popular-photo-id",
    )
    await add_file_id_cache(
        migrated_db_session,
        file=more_popular_file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-more-popular-photo-id",
    )
    await migrated_db_session.commit()

    fake_service = FakeMemeSearchService(MemeSearchPageRead(items=[], limit=20, offset=0, total=0, has_more=False))
    telegram_session = RecordingTelegramSession()
    bot = build_bot(build_bot_settings(postgres_async_url), session=telegram_session)
    dispatcher = build_dispatcher(
        settings=build_bot_settings(postgres_async_url),
        session_factory=postgres_session_factory,
        meme_search_service_factory=lambda session: fake_service,
    )

    try:
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query=" ")
    finally:
        await bot.session.close()

    assert fake_service.calls == []
    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))

    assert created_user is not None
    assert created_user.account_type is AccountType.FULL
    assert created_user.status is AccountStatus.ACTIVE
    assert created_user.nsfw_enabled is False
    answer = last_inline_answer(telegram_session)
    assert answer.is_personal is True
    assert len(answer.results) == 2
    first_result = answer.results[0]
    assert isinstance(first_result, InlineQueryResultCachedPhoto)
    assert first_result.photo_file_id == "cached-more-popular-photo-id"


@pytest.mark.asyncio
async def test_chosen_inline_result_records_strict_chosen_sent_and_recent_send_events(
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
    result_id = f"p:{file.id.hex}:imp_chosen_1"

    try:
        await dispatch_chosen_inline_result(dispatcher=dispatcher, bot=bot, result_id=result_id)
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        events = (
            await session.execute(
                select(AnalyticsEvent).where(
                    AnalyticsEvent.event_type.in_(
                        [
                            AnalyticsEventType.INLINE_CHOSEN,
                            AnalyticsEventType.INLINE_SENT,
                            AnalyticsEventType.MEME_SEND,
                        ]
                    )
                )
            )
        ).scalars().all()
    events_by_type = {event.event_type: event for event in events}
    assert set(events_by_type) == {
        AnalyticsEventType.INLINE_CHOSEN,
        AnalyticsEventType.INLINE_SENT,
        AnalyticsEventType.MEME_SEND,
    }
    for event in events_by_type.values():
        assert event.user_id is None
        properties = _analytics_properties(event)
        refs = _analytics_refs(event)
        assert event.payload["surface"] == "telegram_inline"
        assert refs == {"meme_file_id": str(file.id), "meme_id": str(meme.id)}
        assert event.payload["query"] == "cats"
        assert event.payload["impression_id"] == "imp_chosen_1"
        assert properties["result_id"] == result_id
        assert (
            properties["telegram_user_hash"]
            == hashlib.sha256(f"telegram_user:{TELEGRAM_ID}".encode()).hexdigest()
        )
        assert "telegram_user_id" not in properties


@pytest.mark.asyncio
async def test_chosen_inline_result_records_linked_telegram_user(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    _meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
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

    try:
        await dispatch_chosen_inline_result(dispatcher=dispatcher, bot=bot, result_id=f"p:{file.id.hex}")
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_SEND)
        )
    assert event is not None
    assert event.user_id == linked_user.id


@pytest.mark.asyncio
async def test_chosen_inline_result_after_inline_auto_create_records_created_user(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
    await add_file_id_cache(
        migrated_db_session,
        file=file,
        media_format=TelegramMediaFormat.PHOTO,
        telegram_file_id="cached-auto-created-chosen-photo-id",
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
        await dispatch_inline_query(dispatcher=dispatcher, bot=bot, query="cats")
        answer = last_inline_answer(telegram_session)
        await dispatch_chosen_inline_result(dispatcher=dispatcher, bot=bot, result_id=answer.results[0].id)
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))
        event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_SEND)
        )
    assert created_user is not None
    assert event is not None
    assert event.user_id == created_user.id


@pytest.mark.asyncio
async def test_inline_library_callbacks_call_collection_service_paths(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    favorite_meme, favorite_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
    save_meme, save_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.IMAGE,
        mime_type="image/jpeg",
    )
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

    try:
        await dispatch_library_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"lib:fav:p:{favorite_file.id.hex}",
        )
        await dispatch_library_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"lib:save:p:{save_file.id.hex}",
            update_id=31,
        )
        await dispatch_library_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"lib:pin:p:{favorite_file.id.hex}",
            update_id=32,
        )
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        saved_meme_ids = await session.scalars(select(CollectionMeme.meme_id).order_by(CollectionMeme.meme_id.asc()))
        pinned_meme_ids = await session.scalars(select(PinnedMeme.meme_id).where(PinnedMeme.user_id == linked_user.id))
        like_count = await session.scalar(select(Meme.like_count).where(Meme.id == favorite_meme.id))
        events = (
            await session.execute(
                select(AnalyticsEvent).where(
                    AnalyticsEvent.event_type.in_(
                        [AnalyticsEventType.MEME_LIKE, AnalyticsEventType.MEME_SAVE, AnalyticsEventType.MEME_PIN]
                    )
                )
            )
        ).scalars().all()

    assert set(saved_meme_ids) == {favorite_meme.id, save_meme.id}
    assert list(pinned_meme_ids) == [favorite_meme.id]
    assert like_count == 1
    events_by_type = {event.event_type: event for event in events}
    like_event = events_by_type[AnalyticsEventType.MEME_LIKE]
    save_event = events_by_type[AnalyticsEventType.MEME_SAVE]
    pin_event = events_by_type[AnalyticsEventType.MEME_PIN]
    assert events_by_type[AnalyticsEventType.MEME_LIKE].payload["surface"] == "telegram_inline_library"
    assert _analytics_refs(like_event)["meme_id"] == str(favorite_meme.id)
    assert _analytics_properties(like_event)["action"] == "fav"
    assert _analytics_refs(save_event)["meme_id"] == str(save_meme.id)
    assert _analytics_refs(pin_event)["meme_id"] == str(favorite_meme.id)


@pytest.mark.asyncio
async def test_inline_library_callback_requires_linked_telegram_user(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
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

    try:
        await dispatch_library_callback(dispatcher=dispatcher, bot=bot, data=f"lib:fav:p:{file.id.hex}")
    finally:
        await bot.session.close()

    callback_answers = [method for method in telegram_session.sent_methods if isinstance(method, AnswerCallbackQuery)]
    assert callback_answers[-1].show_alert is True
    assert "Link your Telegram account" in str(callback_answers[-1].text)

    async with postgres_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionMeme)) == 0


@pytest.mark.asyncio
async def test_inline_library_callback_rejects_inactive_linked_user_without_writes(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    _ = await migrated_db_session.execute(
        update(User).where(User.id == linked_user.id).values(status=AccountStatus.DELETION_PENDING)
    )
    _meme, file = await create_meme_file(migrated_db_session, media_type=ContentKind.IMAGE, mime_type="image/jpeg")
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

    try:
        await dispatch_library_callback(dispatcher=dispatcher, bot=bot, data=f"lib:fav:p:{file.id.hex}")
    finally:
        await bot.session.close()

    callback_answers = [method for method in telegram_session.sent_methods if isinstance(method, AnswerCallbackQuery)]
    assert callback_answers[-1].show_alert is True
    assert "not active" in str(callback_answers[-1].text)

    async with postgres_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionMeme)) == 0
        assert await session.scalar(select(Meme.like_count).where(Meme.id == file.meme_id)) == 0
