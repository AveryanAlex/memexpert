"""Focused tests for Telegram private-chat library management."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage, TelegramMethod
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import func, select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.core.config import Settings
from memexpert.models.collection import Collection, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    AccountStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
)
from memexpert.models.user import User
from memexpert.schemas import CollectionMemeRead, CollectionRead, CollectionSummaryRead, MemeLibraryRead, PinnedMemeRead
from memexpert.services import CollectionService, CollectionWriteAccessError, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


BOT_TOKEN = "123456:telegram-library-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/link/telegram/complete"
JWT_SECRET = "library-test-auth-secret-with-32-byte-minimum"
TELEGRAM_ID = 890_220_330


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
                    "id": 999003,
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


@dataclass(slots=True)
class FakePrivateLibraryCollectionService:
    favorites: list[CollectionMemeRead] = field(default_factory=list)
    pins: list[PinnedMemeRead] = field(default_factory=list)
    collections: list[CollectionRead] = field(default_factory=list)
    active_collection_id: uuid.UUID | None = None
    update_active_error: Exception | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def get_meme_library(self, *, user_id: object) -> MemeLibraryRead:
        self.calls.append(("get_meme_library", {"user_id": user_id}))
        summaries = [_summary_for(collection) for collection in self.collections]
        active = next((item for item in summaries if item.id == self.active_collection_id), None)
        return MemeLibraryRead(collections=summaries, active_save_collection=active)

    async def list_favorite_memes(self, *, user_id: object) -> list[CollectionMemeRead]:
        self.calls.append(("list_favorite_memes", {"user_id": user_id}))
        return self.favorites

    async def unfavorite_meme(self, *, user_id: object, meme_id: object) -> bool:
        self.calls.append(("unfavorite_meme", {"user_id": user_id, "meme_id": meme_id}))
        self.favorites = [item for item in self.favorites if item.meme_id != meme_id]
        return True

    async def list_collection_memes(self, *, collection_id: object, user_id: object) -> list[CollectionMemeRead]:
        self.calls.append(("list_collection_memes", {"collection_id": collection_id, "user_id": user_id}))
        return []

    async def remove_meme_from_active_collection(self, *, user_id: object, meme_id: object) -> bool:
        self.calls.append(("remove_meme_from_active_collection", {"user_id": user_id, "meme_id": meme_id}))
        return True

    async def list_pinned_memes(self, *, user_id: object) -> list[PinnedMemeRead]:
        self.calls.append(("list_pinned_memes", {"user_id": user_id}))
        return self.pins

    async def pin_meme(self, *, user_id: object, meme_id: object) -> PinnedMemeRead:
        self.calls.append(("pin_meme", {"user_id": user_id, "meme_id": meme_id}))
        pin = PinnedMemeRead(
            user_id=_as_uuid(user_id),
            meme_id=_as_uuid(meme_id),
            position=len(self.pins) + 1,
            pinned_at=datetime.now(UTC),
        )
        self.pins.append(pin)
        return pin

    async def unpin_meme(self, *, user_id: object, meme_id: object) -> bool:
        self.calls.append(("unpin_meme", {"user_id": user_id, "meme_id": meme_id}))
        self.pins = [item for item in self.pins if item.meme_id != meme_id]
        return True

    async def reorder_pins(self, *, user_id: object, meme_ids: list[uuid.UUID]) -> list[PinnedMemeRead]:
        self.calls.append(("reorder_pins", {"user_id": user_id, "meme_ids": meme_ids}))
        pinned_at_by_id = {item.meme_id: item.pinned_at for item in self.pins}
        self.pins = [
            PinnedMemeRead(
                user_id=_as_uuid(user_id),
                meme_id=meme_id,
                position=index,
                pinned_at=pinned_at_by_id[meme_id],
            )
            for index, meme_id in enumerate(meme_ids, start=1)
        ]
        return self.pins

    async def list_collections_for_user(self, *, user_id: object) -> list[CollectionRead]:
        self.calls.append(("list_collections_for_user", {"user_id": user_id}))
        return self.collections

    async def create_custom_collection(self, *, owner_user_id: object, title: str) -> CollectionRead:
        self.calls.append(("create_custom_collection", {"owner_user_id": owner_user_id, "title": title}))
        collection = collection_read(title=title, owner_id=owner_user_id, kind=CollectionKind.CUSTOM)
        self.collections.append(collection)
        return collection

    async def delete_custom_collection(self, *, collection_id: object, user_id: object) -> bool:
        self.calls.append(("delete_custom_collection", {"collection_id": collection_id, "user_id": user_id}))
        self.collections = [item for item in self.collections if item.id != collection_id]
        return True

    async def update_active_save_collection(self, *, user_id: object, collection_id: object) -> object:
        self.calls.append(("update_active_save_collection", {"user_id": user_id, "collection_id": collection_id}))
        if self.update_active_error is not None:
            raise self.update_active_error
        self.active_collection_id = _as_uuid(collection_id)
        return object()


def build_bot_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        auth_jwt_secret=SecretStr(JWT_SECRET),
        auth_telegram_bot_token=SecretStr(BOT_TOKEN),
        auth_telegram_bot_username=BOT_USERNAME,
        auth_telegram_link_return_url=TypeAdapter(AnyHttpUrl).validate_python(RETURN_URL),
    )


def build_library_dispatcher(
    *,
    settings: Settings,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    service: FakePrivateLibraryCollectionService | None = None,
) -> Dispatcher:
    return build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        private_library_collection_service_factory=None if service is None else lambda session: service,
    )


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
                "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Library"},
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Library"},
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
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Library"},
                "chat_instance": "library-chat-instance",
                "message": {
                    "message_id": 900 + update_id,
                    "date": 1_700_000_000,
                    "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Library"},
                    "text": "Previous library screen",
                },
                "data": data,
            },
        },
    )


async def dispatch_private_callback_without_message(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    data: str,
    update_id: int = 40,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Library"},
                "chat_instance": "library-chat-instance",
                "inline_message_id": f"inline-library-{update_id}",
                "data": data,
            },
        },
    )


def collection_meme_read(meme_id: uuid.UUID, *, collection_id: uuid.UUID | None = None) -> CollectionMemeRead:
    return CollectionMemeRead(
        collection_id=collection_id or uuid.uuid7(),
        meme_id=meme_id,
        added_by_user_id=None,
        added_at=datetime.now(UTC),
    )


def pinned_meme_read(user_id: uuid.UUID, meme_id: uuid.UUID, *, position: int) -> PinnedMemeRead:
    return PinnedMemeRead(user_id=user_id, meme_id=meme_id, position=position, pinned_at=datetime.now(UTC))


def collection_read(
    *,
    title: str,
    owner_id: object,
    kind: CollectionKind = CollectionKind.CUSTOM,
) -> CollectionRead:
    now = datetime.now(UTC)
    return CollectionRead(
        id=uuid.uuid7(),
        owner_id=_as_uuid(owner_id),
        title=title,
        description=None,
        kind=kind,
        visibility=CollectionVisibility.PRIVATE,
        memberships=[],
        invites=[],
        created_at=now,
        updated_at=now,
    )


def _as_uuid(value: object) -> uuid.UUID:
    return cast("uuid.UUID", value)


def _summary_for(collection: CollectionRead) -> CollectionSummaryRead:
    return CollectionSummaryRead(
        id=collection.id,
        owner_id=collection.owner_id,
        title=collection.title,
        description=collection.description,
        kind=collection.kind,
        visibility=collection.visibility,
        role=CollectionMembershipRole.OWNER,
        can_write=True,
        saved_meme_count=0,
        created_at=collection.created_at,
        updated_at=collection.updated_at,
    )


async def create_meme_file(
    session: AsyncSession,
    *,
    author_user_id: uuid.UUID | None = None,
    is_public: bool = True,
) -> Meme:
    meme = Meme(
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        is_public=is_public,
        author_user_id=author_user_id,
    )
    session.add(meme)
    await session.flush()
    file = MemeFile(
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
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
    await session.flush()
    return meme


def last_message(session: RecordingTelegramSession) -> SendMessage:
    messages = [method for method in session.sent_methods if isinstance(method, SendMessage)]
    assert messages, "Expected a bot message."
    return messages[-1]


def last_edit(session: RecordingTelegramSession) -> EditMessageText:
    edits = [method for method in session.sent_methods if isinstance(method, EditMessageText)]
    assert edits, "Expected an edited bot message."
    return edits[-1]


def last_callback_answer(session: RecordingTelegramSession) -> AnswerCallbackQuery:
    answers = [method for method in session.sent_methods if isinstance(method, AnswerCallbackQuery)]
    assert answers, "Expected a callback answer."
    return answers[-1]


@pytest.mark.asyncio
async def test_private_library_menu_and_favorites_pagination_use_injected_service(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    collection_id = uuid.uuid7()
    favorite_ids = [uuid.uuid7() for _ in range(6)]
    fake_service = FakePrivateLibraryCollectionService(
        favorites=[collection_meme_read(meme_id, collection_id=collection_id) for meme_id in favorite_ids],
        collections=[collection_read(title="Favorites", owner_id=linked_user.id, kind=CollectionKind.FAVORITES)],
    )
    fake_service.active_collection_id = fake_service.collections[0].id
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        service=fake_service,
    )

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/library")
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/favorites", update_id=2)
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data="pml:f:1")
    finally:
        await bot.session.close()

    sent_texts = [method.text for method in telegram_session.sent_methods if isinstance(method, SendMessage)]
    assert "Библиотека MemeXpert" in str(sent_texts[0])
    assert "Page 1/2" in str(sent_texts[1])
    assert "Page 2/2" in str(last_edit(telegram_session).text)
    assert ("get_meme_library", {"user_id": linked_user.id}) in fake_service.calls
    assert fake_service.calls.count(("list_favorite_memes", {"user_id": linked_user.id})) == 2


@pytest.mark.asyncio
async def test_private_library_callback_surfaces_service_permission_errors(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    collection = collection_read(title="Read only", owner_id=linked_user.id)
    fake_service = FakePrivateLibraryCollectionService(
        collections=[collection],
        update_active_error=CollectionWriteAccessError("No write access."),
    )
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        service=fake_service,
    )

    try:
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data=f"pml:as:{collection.id.hex}:0")
    finally:
        await bot.session.close()

    answer = last_callback_answer(telegram_session)
    assert answer.show_alert is True
    assert answer.text == "No write access."
    assert (
        "update_active_save_collection",
        {"user_id": linked_user.id, "collection_id": collection.id},
    ) in fake_service.calls


@pytest.mark.asyncio
async def test_private_library_callback_reports_hidden_meme_without_escaping(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    other_user = await create_full_user_via_upgrade(user_service, email="private-owner@example.com")
    hidden_meme = await create_meme_file(
        migrated_db_session,
        author_user_id=other_user.id,
        is_public=False,
    )
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data=f"pml:pn:{hidden_meme.id.hex}:f0")
    finally:
        await bot.session.close()

    answer = last_callback_answer(telegram_session)
    assert answer.show_alert is True
    assert answer.text == "Мем не найден или недоступен."
    async with postgres_session_factory() as session:
        pin_count = await session.scalar(
            select(func.count()).select_from(PinnedMeme).where(PinnedMeme.user_id == linked_user.id)
        )
    assert pin_count == 0


@pytest.mark.asyncio
async def test_private_library_callback_without_editable_message_answers_with_alert(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    fake_service = FakePrivateLibraryCollectionService(
        collections=[collection_read(title="Favorites", owner_id=linked_user.id, kind=CollectionKind.FAVORITES)],
    )
    fake_service.active_collection_id = fake_service.collections[0].id
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        service=fake_service,
    )

    try:
        await dispatch_private_callback_without_message(dispatcher=dispatcher, bot=bot, data="pml:m")
    finally:
        await bot.session.close()

    answer = last_callback_answer(telegram_session)
    assert answer.show_alert is True
    assert answer.text == "Библиотека MemeXpert"
    assert not any(isinstance(method, EditMessageText) for method in telegram_session.sent_methods)


@pytest.mark.asyncio
async def test_private_library_callbacks_mutate_real_collection_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    memes = [await create_meme_file(migrated_db_session, author_user_id=linked_user.id) for _ in range(3)]
    custom = await collection_service.create_custom_collection(owner_user_id=linked_user.id, title="Active saves")
    _ = await collection_service.favorite_meme(user_id=linked_user.id, meme_id=memes[0].id)
    _ = await collection_service.favorite_meme(user_id=linked_user.id, meme_id=memes[1].id)
    _ = await collection_service.update_active_save_collection(user_id=linked_user.id, collection_id=custom.id)
    _ = await collection_service.save_meme_to_active_collection(user_id=linked_user.id, meme_id=memes[1].id)
    for meme in memes:
        _ = await collection_service.pin_meme(user_id=linked_user.id, meme_id=meme.id)
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data=f"pml:pd:{memes[0].id.hex}:0")
        await dispatch_private_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"pml:up:{memes[0].id.hex}:p0",
            update_id=21,
        )
        await dispatch_private_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"pml:uf:{memes[1].id.hex}:0",
            update_id=22,
        )
        await dispatch_private_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"pml:rs:{memes[1].id.hex}:0",
            update_id=23,
        )
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        pins = list(
            await session.scalars(
                select(PinnedMeme).where(PinnedMeme.user_id == linked_user.id).order_by(PinnedMeme.position.asc())
            )
        )
        favorite_meme_ids = set(
            await session.scalars(
                select(CollectionMeme.meme_id).join(Collection).where(Collection.kind == CollectionKind.FAVORITES)
            )
        )
        active_meme_ids = set(
            await session.scalars(select(CollectionMeme.meme_id).where(CollectionMeme.collection_id == custom.id))
        )

    assert [pin.meme_id for pin in pins] == [memes[1].id, memes[2].id]
    assert [pin.position for pin in pins] == [1, 2]
    assert favorite_meme_ids == {memes[0].id}
    assert active_meme_ids == set()
    assert "Active Save Collection" in str(last_edit(telegram_session).text)


@pytest.mark.asyncio
async def test_private_library_create_set_active_and_delete_collections_keep_favorites(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    favorites = await collection_service.ensure_favorites_collection(linked_user.id)
    await migrated_db_session.commit()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(settings=settings, postgres_session_factory=postgres_session_factory)

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/collection_create Bot saves")
        async with postgres_session_factory() as session:
            custom_id = await session.scalar(
                select(Collection.id).where(Collection.owner_id == linked_user.id, Collection.title == "Bot saves")
            )
        assert custom_id is not None
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data=f"pml:as:{custom_id.hex}:0")
        await dispatch_private_callback(
            dispatcher=dispatcher,
            bot=bot,
            data=f"pml:cd:{favorites.id.hex}:0",
            update_id=21,
        )
        await dispatch_private_callback(dispatcher=dispatcher, bot=bot, data=f"pml:cd:{custom_id.hex}:0", update_id=22)
    finally:
        await bot.session.close()

    async with postgres_session_factory() as session:
        active_collection_id = await session.scalar(
            select(User.active_save_collection_id).where(User.id == linked_user.id)
        )
        favorites_exists = await session.scalar(select(Collection.id).where(Collection.id == favorites.id))
        custom_exists = await session.scalar(select(Collection.id).where(Collection.id == custom_id))

    assert "Коллекция создана: Bot saves" in str(last_message(telegram_session).text)
    assert active_collection_id is None
    assert favorites_exists == favorites.id
    assert custom_exists is None
    assert any(
        isinstance(method, EditMessageText) and "Favorites нельзя удалить" in str(method.text)
        for method in telegram_session.sent_methods
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_kwargs", "expected_user_count"),
    [
        (None, 0),
        ({"status": AccountStatus.ACTIVE}, 1),
        ({"status": AccountStatus.DELETION_PENDING, "telegram_id": TELEGRAM_ID}, 1),
    ],
)
async def test_private_library_requires_active_full_linked_telegram_user_without_creating_guests(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
    user_kwargs: dict[str, object] | None,
    expected_user_count: int,
) -> None:
    if user_kwargs is not None:
        migrated_db_session.add(User(**user_kwargs))
    await migrated_db_session.commit()
    fake_service = FakePrivateLibraryCollectionService()

    settings = build_bot_settings(postgres_async_url)
    telegram_session = RecordingTelegramSession()
    bot = build_bot(settings, session=telegram_session)
    dispatcher = build_library_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        service=fake_service,
    )

    try:
        await dispatch_private_message(dispatcher=dispatcher, bot=bot, text="/library")
    finally:
        await bot.session.close()

    assert "Сначала привяжите Telegram" in str(last_message(telegram_session).text)
    assert fake_service.calls == []
    async with postgres_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == expected_user_count
