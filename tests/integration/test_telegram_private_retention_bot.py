"""Focused tests for Telegram private-chat retention/settings handlers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.core.config import Settings
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    CollectionKind,
    CollectionMembershipRole,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    SourcePlatform,
    UserLanguage,
)
from memexpert.models.user import ChannelSuggestion, InlineUsageEvent, User
from memexpert.services import UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


BOT_TOKEN = "123456:telegram-retention-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/link/telegram/complete"
JWT_SECRET = "retention-test-auth-secret-with-32-byte-minimum"
TELEGRAM_ID = 810_220_330


class RecordingTelegramSession(BaseSession):
    """Capture bot methods while satisfying aiogram's session contract."""

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

        if isinstance(method, SendMessage | EditMessageText | AnswerCallbackQuery):
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


def build_bot_settings(database_url: str, *, bot_username: str | None = BOT_USERNAME) -> Settings:
    return Settings(
        database_url=database_url,
        auth_jwt_secret=SecretStr(JWT_SECRET),
        auth_telegram_bot_token=SecretStr(BOT_TOKEN),
        auth_telegram_bot_username=bot_username,
        auth_telegram_link_return_url=TypeAdapter(AnyHttpUrl).validate_python(RETURN_URL),
    )


def build_retention_dispatcher(
    *,
    settings: Settings,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    return build_dispatcher(settings=settings, session_factory=postgres_session_factory)


async def dispatch_private_message(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
    update_id: int = 1,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "message": {
                "message_id": 700 + update_id,
                "date": 1_700_000_000,
                "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Retention"},
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Retention"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split(maxsplit=1)[0])}],
            },
        },
    )


async def dispatch_private_callback(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    data: str,
    update_id: int = 20,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Retention"},
                "chat_instance": "retention-chat-instance",
                "message": {
                    "message_id": 900 + update_id,
                    "date": 1_700_000_000,
                    "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Retention"},
                    "text": "Previous settings screen",
                },
                "data": data,
            },
        },
    )


def last_message(session: RecordingTelegramSession) -> SendMessage:
    messages = [method for method in session.sent_methods if isinstance(method, SendMessage)]
    assert messages, "Expected a bot message."
    return messages[-1]


def last_edit(session: RecordingTelegramSession) -> EditMessageText:
    edits = [method for method in session.sent_methods if isinstance(method, EditMessageText)]
    assert edits, "Expected an edited bot message."
    return edits[-1]


@pytest.mark.asyncio
async def test_settings_callbacks_persist_nsfw_and_language(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(
        user_service,
        telegram_id=TELEGRAM_ID,
        language=UserLanguage.ANY,
        nsfw_enabled=False,
    )
    await migrated_db_session.commit()
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_retention_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/settings")
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data="pmr:nsfw:1")
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data="pmr:lang:ru", update_id=21)
    finally:
        await bot.session.close()

    await migrated_db_session.refresh(await migrated_db_session.get(User, linked_user.id))
    persisted = await migrated_db_session.get(User, linked_user.id)
    assert persisted is not None
    assert persisted.nsfw_enabled is True
    assert persisted.language is UserLanguage.RU
    assert "Настройки MemeXpert" in str(last_message(telegram_session).text)
    assert "NSFW по умолчанию: включено" in str(last_edit(telegram_session).text)
    assert "Язык контента: Русский" in str(last_edit(telegram_session).text)


@pytest.mark.asyncio
async def test_suggest_channel_writes_pending_row_and_reports_duplicates(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await migrated_db_session.commit()
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_retention_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/suggest_channel @memexpert_source")
        await dispatch_private_message(
            dispatcher=dispatcher,
            bot=bot,
            text="/suggest_channel https://t.me/memexpert_source",
            update_id=2,
        )
    finally:
        await bot.session.close()

    suggestions = (
        await migrated_db_session.execute(select(ChannelSuggestion).where(ChannelSuggestion.user_id == linked_user.id))
    ).scalars().all()
    sent_texts = [str(method.text) for method in telegram_session.sent_methods if isinstance(method, SendMessage)]
    assert len(suggestions) == 1
    assert suggestions[0].platform is SourcePlatform.TELEGRAM
    assert suggestions[0].channel_url == "https://t.me/memexpert_source"
    assert "Канал отправлен на проверку" in sent_texts[0]
    assert "Такой канал уже есть" in sent_texts[1]


@pytest.mark.asyncio
async def test_suggest_channel_rejects_invalid_input_without_writing_row(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await migrated_db_session.commit()
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_retention_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/suggest_channel https://example.com/memes")
    finally:
        await bot.session.close()

    suggestions = (await migrated_db_session.execute(select(ChannelSuggestion))).scalars().all()
    assert suggestions == []
    assert "Не удалось принять канал" in str(last_message(telegram_session).text)


@pytest.mark.asyncio
async def test_account_status_and_miniapp_links_include_provider_state_and_entry_buttons(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    await create_full_user_via_upgrade(
        user_service,
        telegram_id=TELEGRAM_ID,
        google_id="google-subject",
        email="retention@example.com",
        email_verified_at=datetime.now(UTC),
    )
    await migrated_db_session.commit()
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_retention_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/account")
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/miniapp", update_id=2)
    finally:
        await bot.session.close()

    account_message = [method for method in telegram_session.sent_methods if isinstance(method, SendMessage)][0]
    miniapp_message = last_message(telegram_session)
    assert "Telegram: привязан" in str(account_message.text)
    assert "Google: привязан" in str(account_message.text)
    assert "Email: привязан" in str(account_message.text)
    assert f"https://t.me/{BOT_USERNAME}/app?startapp=profile" in str(miniapp_message.text)
    assert isinstance(miniapp_message.reply_markup, InlineKeyboardMarkup)
    button_urls = [button.url for row in miniapp_message.reply_markup.inline_keyboard for button in row]
    assert f"https://t.me/{BOT_USERNAME}/app?startapp=collections" in button_urls
    assert f"https://t.me/{BOT_USERNAME}/app?startapp=invite" in button_urls


@pytest.mark.asyncio
async def test_stats_show_persisted_counts_and_no_data_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await seed_stats_data(migrated_db_session, user_id=linked_user.id)
    await migrated_db_session.commit()
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_retention_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/stats")
    finally:
        await bot.session.close()

    stats_text = str(last_message(telegram_session).text)
    assert "Favorites: 1" in stats_text
    assert "Сохранений в коллекциях: 2" in stats_text
    assert "Pins: 1" in stats_text
    assert "Коллекций: 2" in stats_text
    assert "Загрузок/авторских мемов: 1" in stats_text
    assert "Inline sends/events: 1" in stats_text
    assert "Предложений каналов: 1 (1 pending)" in stats_text
    assert "Топ тег по сохранениям: cats" in stats_text


@pytest.mark.asyncio
async def test_stats_show_honest_no_data_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await migrated_db_session.commit()
    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_retention_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/stats")
    finally:
        await bot.session.close()

    stats_text = str(last_message(telegram_session).text)
    assert "Favorites: 0" in stats_text
    assert "Пока мало данных" in stats_text


async def seed_stats_data(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    user = await session.get(User, user_id)
    assert user is not None
    user.created_at = utcnow() - timedelta(days=4)
    user.last_active_at = utcnow()
    favorites = Collection(owner_id=user_id, title="Favorites", kind=CollectionKind.FAVORITES)
    custom = Collection(owner_id=user_id, title="Custom", kind=CollectionKind.CUSTOM)
    session.add_all([favorites, custom])
    await session.flush()
    session.add_all(
        [
            CollectionMember(
                collection_id=favorites.id,
                user_id=user_id,
                role=CollectionMembershipRole.OWNER,
            ),
            CollectionMember(
                collection_id=custom.id,
                user_id=user_id,
                role=CollectionMembershipRole.OWNER,
            ),
        ]
    )
    meme = Meme(media_type=ContentKind.IMAGE, language=ContentLanguage.EN, author_user_id=user_id, tags=["cats"])
    second_meme = Meme(media_type=ContentKind.IMAGE, language=ContentLanguage.EN, tags=["cats", "dogs"])
    session.add_all([meme, second_meme])
    await session.flush()
    session.add(
        MemeFile(
            meme_id=meme.id,
            status=ContentProcessingStatus.READY,
            width=100,
            height=100,
            file_size_bytes=5,
            mime_type="image/png",
            s3_original_key=f"pipeline/originals/{meme.id}/original.png",
            perceptual_hash=f"hash-{meme.id}",
            is_primary=True,
        )
    )
    session.add_all(
        [
            CollectionMeme(collection_id=favorites.id, meme_id=meme.id, added_by_user_id=user_id),
            CollectionMeme(collection_id=custom.id, meme_id=second_meme.id, added_by_user_id=user_id),
            PinnedMeme(user_id=user_id, meme_id=meme.id, position=1),
            InlineUsageEvent(user_id=user_id, group_hash="feedbeef1234"),
            ChannelSuggestion(user_id=user_id, platform=SourcePlatform.TELEGRAM, channel_url="https://t.me/cats"),
        ]
    )
