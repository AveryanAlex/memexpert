"""Integration tests for the Telegram bot guest-link completion flow."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage, TelegramMethod
from aiogram.types import Message as TelegramMessage
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import func, select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.core.config import Settings
from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.content import Meme
from memexpert.models.enums import AccountType, AnalyticsEventType, CollectionKind, ContentKind
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, InlineUsageEvent, TelegramLinkCode, User
from memexpert.services import (
    AccountLinkInvariantError,
    AccountLinkService,
    AuthConfigurationError,
    ProviderNotConfiguredError,
    UserService,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOT_TOKEN = "123456:telegram-route-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/link/telegram/complete"
PASSWORD = "correct-horse-battery"
TELEGRAM_ID = 777_888_999
JWT_SECRET = "route-test-auth-secret-with-32-byte-minimum"


class RecordingTelegramSession(BaseSession):
    """Capture outgoing bot replies while satisfying aiogram's session contract."""

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
            return TelegramMessage.model_validate(
                {
                    "message_id": len(self.sent_methods),
                    "date": int(time.time()),
                    "chat": {"id": method.chat_id, "type": "private"},
                    "from": {
                        "id": 999001,
                        "is_bot": True,
                        "first_name": "MemeXpert",
                        "username": BOT_USERNAME,
                    },
                    "text": method.text,
                },
                context={"bot": bot},
            )

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


class ExplodingBotAccountLinkService(AccountLinkService):
    """Force a rollback after merge-side updates begin so the bot path proves retry safety."""

    async def _reassign_inline_usage_events(
        self,
        *,
        source_user_id: uuid.UUID,
        target_user_id: uuid.UUID,
    ) -> int:
        _ = await super()._reassign_inline_usage_events(
            source_user_id=source_user_id,
            target_user_id=target_user_id,
        )
        raise AccountLinkInvariantError("forced rollback after partial transfer")


def build_bot_settings(
    database_url: str,
    *,
    bot_token: str | None = BOT_TOKEN,
    return_url: str | None = RETURN_URL,
) -> Settings:
    """Construct deterministic bot settings for dispatcher tests."""

    return Settings(
        database_url=database_url,
        auth_jwt_secret=SecretStr(JWT_SECRET),
        auth_refresh_cookie_name="route_refresh_token",
        auth_refresh_cookie_samesite="strict",
        auth_refresh_cookie_secure=True,
        auth_telegram_bot_token=SecretStr(bot_token) if bot_token is not None else None,
        auth_telegram_bot_username=BOT_USERNAME,
        auth_telegram_link_return_url=cast("AnyHttpUrl", return_url) if return_url is not None else None,
    )


async def issue_link_code(session: AsyncSession, *, settings: Settings, guest_user_id: uuid.UUID) -> str:
    service = AccountLinkService.from_settings(session, settings=settings)
    result = await service.issue_telegram_link_code(guest_user_id=guest_user_id)
    return result.code


async def create_meme(session: AsyncSession) -> Meme:
    meme = Meme(media_type=ContentKind.IMAGE)
    session.add(meme)
    await session.flush()
    return meme


async def add_saved_meme(
    session: AsyncSession,
    *,
    collection_id: uuid.UUID,
    meme_id: uuid.UUID,
    added_by_user_id: uuid.UUID,
) -> None:
    session.add(
        CollectionMeme(
            collection_id=collection_id,
            meme_id=meme_id,
            added_by_user_id=added_by_user_id,
        )
    )
    await session.flush()


async def add_guest_history(
    session: AsyncSession,
    *,
    guest_user_id: uuid.UUID,
    meme_id: str,
) -> None:
    session.add_all(
        [
            AnalyticsEvent(
                user_id=guest_user_id,
                event_type=AnalyticsEventType.MEME_VIEW,
                payload={"meme_id": meme_id},
            ),
            AnalyticsEvent(
                user_id=guest_user_id,
                event_type=AnalyticsEventType.CLICK,
                payload={"surface": "favorites"},
            ),
            InlineUsageEvent(user_id=guest_user_id, group_hash="feedbeef1234"),
        ]
    )
    await session.flush()


async def get_favorites_collection_id(session: AsyncSession, *, owner_id: uuid.UUID) -> uuid.UUID | None:
    result = await session.execute(
        select(Collection.id).where(
            Collection.owner_id == owner_id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    return result.scalar_one_or_none()


async def count_favorites_rows(session: AsyncSession, *, owner_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .where(
            Collection.owner_id == owner_id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    return int(result.scalar_one())


def build_start_update(
    *,
    text: str,
    telegram_id: int | None,
    update_id: int,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": {"id": 10_000 + update_id, "type": "private"},
        "text": text,
    }
    if telegram_id is not None:
        message["from"] = {
            "id": telegram_id,
            "is_bot": False,
            "first_name": "Link",
            "username": "link_user",
        }
    return {"update_id": update_id, "message": message}


async def dispatch_start(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
    telegram_id: int | None,
    update_id: int,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        build_start_update(text=text, telegram_id=telegram_id, update_id=update_id),
    )


def last_reply_text(session: RecordingTelegramSession) -> str:
    assert session.sent_methods, "Expected the bot to send at least one reply."
    method = session.sent_methods[-1]
    assert isinstance(method, SendMessage)
    assert method.text is not None
    return method.text


@pytest.mark.asyncio
async def test_start_link_upgrades_guest_in_place_and_returns_return_url(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = build_bot_settings(postgres_async_url)
    user_service = UserService(migrated_db_session)
    guest_user = await user_service.create_guest_user()
    code = await issue_link_code(migrated_db_session, settings=settings, guest_user_id=guest_user.id)

    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(settings=settings, session_factory=postgres_session_factory)

    try:
        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{code}",
            telegram_id=TELEGRAM_ID,
            update_id=1,
        )
    finally:
        await bot.session.close()

    reply_text = last_reply_text(telegram_session)
    assert "Telegram привязан" in reply_text
    assert "Избранное и история остались на месте." in reply_text
    assert RETURN_URL in reply_text

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user.id))
        link_code_result = await session.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.guest_user_id == guest_user.id)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        persisted_user = persisted_user_result.scalar_one()
        link_code = link_code_result.scalar_one()

        assert persisted_user.account_type is AccountType.FULL
        assert persisted_user.telegram_id == TELEGRAM_ID
        assert link_code.redeemed_at is not None
        assert link_code.redeemed_by_telegram_id == TELEGRAM_ID
        assert merge_log_count_result.scalar_one() == 0


@pytest.mark.asyncio
async def test_start_link_reuses_existing_full_strictly_by_telegram_id_and_preserves_guest_state(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = build_bot_settings(postgres_async_url)
    user_service = UserService(migrated_db_session)

    first_guest = await user_service.create_guest_user()
    first_code = await issue_link_code(migrated_db_session, settings=settings, guest_user_id=first_guest.id)

    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(settings=settings, session_factory=postgres_session_factory)

    try:
        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{first_code}",
            telegram_id=TELEGRAM_ID,
            update_id=10,
        )

        async with postgres_session_factory() as setup_session:
            setup_user_service = UserService(setup_session)
            second_guest = await setup_user_service.create_guest_user()
            second_guest_favorites_id = await get_favorites_collection_id(setup_session, owner_id=second_guest.id)
            assert second_guest_favorites_id is not None

            guest_only_meme = await create_meme(setup_session)
            await add_saved_meme(
                setup_session,
                collection_id=second_guest_favorites_id,
                meme_id=guest_only_meme.id,
                added_by_user_id=second_guest.id,
            )
            await add_guest_history(
                setup_session,
                guest_user_id=second_guest.id,
                meme_id=str(guest_only_meme.id),
            )
            await setup_session.commit()

            second_code = await issue_link_code(setup_session, settings=settings, guest_user_id=second_guest.id)
        telegram_session.sent_methods.clear()

        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{second_code}",
            telegram_id=TELEGRAM_ID,
            update_id=11,
        )
    finally:
        await bot.session.close()

    reply_text = last_reply_text(telegram_session)
    assert "Аккаунты объединены" in reply_text
    assert "Избранное и история из гостевого профиля сохранены." in reply_text
    assert RETURN_URL in reply_text

    async with postgres_session_factory() as session:
        canonical_user_result = await session.execute(select(User).where(User.id == first_guest.id))
        deleted_guest_result = await session.execute(select(User).where(User.id == second_guest.id))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))
        second_code_result = await session.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.guest_user_id == second_guest.id)
        )

        canonical_user = canonical_user_result.scalar_one()
        second_code_row = second_code_result.scalar_one()

        assert canonical_user.account_type is AccountType.FULL
        assert canonical_user.telegram_id == TELEGRAM_ID
        assert deleted_guest_result.scalar_one_or_none() is None
        assert await count_favorites_rows(session, owner_id=canonical_user.id) == 1
        assert merge_log_count_result.scalar_one() == 1
        assert second_code_row.redeemed_at is not None
        assert second_code_row.redeemed_by_telegram_id == TELEGRAM_ID


@pytest.mark.asyncio
async def test_start_link_rejects_replay_after_a_successful_redemption(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = build_bot_settings(postgres_async_url)
    guest_user = await UserService(migrated_db_session).create_guest_user()
    code = await issue_link_code(migrated_db_session, settings=settings, guest_user_id=guest_user.id)

    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(settings=settings, session_factory=postgres_session_factory)

    try:
        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{code}",
            telegram_id=TELEGRAM_ID,
            update_id=20,
        )
        telegram_session.sent_methods.clear()
        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{code}",
            telegram_id=TELEGRAM_ID,
            update_id=21,
        )
    finally:
        await bot.session.close()

    reply_text = last_reply_text(telegram_session)
    assert "Не удалось завершить привязку" in reply_text
    assert "избранное и история не потеряются" in reply_text.lower()
    assert RETURN_URL in reply_text

    async with postgres_session_factory() as session:
        link_code_result = await session.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.guest_user_id == guest_user.id)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        link_code = link_code_result.scalar_one()

        assert link_code.redeemed_at is not None
        assert link_code.redeemed_by_telegram_id == TELEGRAM_ID
        assert merge_log_count_result.scalar_one() == 0


@pytest.mark.asyncio
async def test_start_link_malformed_inputs_do_not_mutate_guest_or_redeem_code(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = build_bot_settings(postgres_async_url)
    user_service = UserService(migrated_db_session)
    guest_user = await user_service.create_guest_user()
    issued_code = await issue_link_code(migrated_db_session, settings=settings, guest_user_id=guest_user.id)

    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(settings=settings, session_factory=postgres_session_factory)

    try:
        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text="/start promo_offer",
            telegram_id=TELEGRAM_ID,
            update_id=30,
        )
        assert "Этот бот завершает привязку" in last_reply_text(telegram_session)
        telegram_session.sent_methods.clear()

        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text="/start link_bad!code",
            telegram_id=TELEGRAM_ID,
            update_id=31,
        )
        assert "Не удалось завершить привязку" in last_reply_text(telegram_session)
        telegram_session.sent_methods.clear()

        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{issued_code}",
            telegram_id=None,
            update_id=32,
        )
    finally:
        await bot.session.close()

    reply_text = last_reply_text(telegram_session)
    assert "Не удалось определить ваш Telegram-профиль" in reply_text
    assert RETURN_URL in reply_text

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user.id))
        link_code_result = await session.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.guest_user_id == guest_user.id)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        persisted_user = persisted_user_result.scalar_one()
        link_code = link_code_result.scalar_one()

        assert persisted_user.account_type is AccountType.GUEST
        assert link_code.redeemed_at is None
        assert link_code.redeemed_by_telegram_id is None
        assert merge_log_count_result.scalar_one() == 0


@pytest.mark.asyncio
async def test_start_link_service_failure_keeps_code_and_guest_retry_safe(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = build_bot_settings(postgres_async_url)
    user_service = UserService(migrated_db_session)
    guest_user = await user_service.create_guest_user()
    canonical_user = await user_service.create_full_user(telegram_id=TELEGRAM_ID)
    canonical_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=canonical_user.id)
    guest_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=guest_user.id)
    assert canonical_favorites_id is not None
    assert guest_favorites_id is not None

    transferable_meme = await create_meme(migrated_db_session)
    await add_saved_meme(
        migrated_db_session,
        collection_id=guest_favorites_id,
        meme_id=transferable_meme.id,
        added_by_user_id=guest_user.id,
    )
    await add_guest_history(
        migrated_db_session,
        guest_user_id=guest_user.id,
        meme_id=str(transferable_meme.id),
    )
    await migrated_db_session.commit()

    code = await issue_link_code(migrated_db_session, settings=settings, guest_user_id=guest_user.id)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        account_link_service_factory=lambda session: ExplodingBotAccountLinkService.from_settings(
            session,
            settings=settings,
        ),
    )

    try:
        await dispatch_start(
            dispatcher=dispatcher,
            bot=bot,
            text=f"/start link_{code}",
            telegram_id=TELEGRAM_ID,
            update_id=40,
        )
    finally:
        await bot.session.close()

    reply_text = last_reply_text(telegram_session)
    assert "Не удалось завершить привязку" in reply_text
    assert "избранное и история не потеряются" in reply_text.lower()
    assert RETURN_URL in reply_text

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == guest_user.id))
        persisted_canonical_result = await session.execute(select(User).where(User.id == canonical_user.id))
        link_code_result = await session.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.guest_user_id == guest_user.id)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))
        guest_analytics_result = await session.execute(
            select(func.count()).select_from(AnalyticsEvent).where(AnalyticsEvent.user_id == guest_user.id)
        )
        guest_inline_result = await session.execute(
            select(func.count()).select_from(InlineUsageEvent).where(InlineUsageEvent.user_id == guest_user.id)
        )

        persisted_guest = persisted_guest_result.scalar_one()
        persisted_canonical = persisted_canonical_result.scalar_one()
        link_code = link_code_result.scalar_one()

        assert persisted_guest.account_type is AccountType.GUEST
        assert persisted_canonical.telegram_id == TELEGRAM_ID
        assert link_code.redeemed_at is None
        assert link_code.redeemed_by_telegram_id is None
        assert merge_log_count_result.scalar_one() == 0
        assert guest_analytics_result.scalar_one() == 2
        assert guest_inline_result.scalar_one() == 1


def test_bot_bootstrap_requires_token_and_return_url(postgres_async_url: str) -> None:
    with pytest.raises(ProviderNotConfiguredError, match="bot token"):
        _ = build_bot(build_bot_settings(postgres_async_url, bot_token=None))

    with pytest.raises(AuthConfigurationError, match="return URL"):
        _ = build_dispatcher(settings=build_bot_settings(postgres_async_url, return_url=None))
