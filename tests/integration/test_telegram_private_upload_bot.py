"""Focused tests for Telegram private-chat quick-save uploads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage, TelegramMethod
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.bot.private_upload import TelegramDownloadError
from memexpert.core.config import Settings
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptResult, IngestAcceptSource, IngestRequestRead
from memexpert.ingest.target_collection_metadata import TARGET_COLLECTION_ID_METADATA_KEY
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionKind,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    IngestSourceKind,
    PipelineIngestRequestStatus,
    SourceAttachReason,
    SourcePlatform,
)
from memexpert.models.user import AnalyticsEvent, User
from memexpert.services import CollectionService, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _analytics_properties(event: AnalyticsEvent) -> dict[str, object]:
    return cast("dict[str, object]", event.payload["properties"])


def _analytics_refs(event: AnalyticsEvent) -> dict[str, object]:
    return cast("dict[str, object]", event.payload["refs"])


BOT_TOKEN = "123456:telegram-upload-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/account/telegram/complete"
JWT_SECRET = "upload-test-auth-secret-with-32-byte-minimum"
TELEGRAM_ID = 880_220_330


class RecordingTelegramSession(BaseSession):
    """Capture outgoing bot messages while satisfying aiogram's session contract."""

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
                    "id": 999002,
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
class FakeTelegramFileDownloader:
    media_bytes: bytes = b"telegram-media-bytes"
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def download_file(self, bot: Bot, *, file_id: str) -> bytes:
        _ = bot
        self.calls.append(file_id)
        if self.error is not None:
            raise self.error
        return self.media_bytes


@dataclass(slots=True)
class FakePrivateUploadAcceptService:
    accept_result: IngestAcceptResult | None = None
    accept_error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def accept_bytes(
        self,
        *,
        source: IngestAcceptSource,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> IngestAcceptResult:
        self.calls.append(
            {
                "source": source,
                "filename": filename,
                "content_type": content_type,
                "media_bytes": media_bytes,
            }
        )
        if self.accept_error is not None:
            raise self.accept_error
        assert self.accept_result is not None
        return self.accept_result


def build_bot_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        auth_jwt_secret=SecretStr(JWT_SECRET),
        auth_telegram_bot_token=SecretStr(BOT_TOKEN),
        auth_telegram_bot_username=BOT_USERNAME,
        auth_telegram_link_return_url=TypeAdapter(AnyHttpUrl).validate_python(RETURN_URL),
        pipeline_image_upload_max_bytes=10,
        pipeline_gif_upload_max_bytes=20,
    )


async def seed_meme(
    session: AsyncSession,
    *,
    is_public: bool,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        is_public=is_public,
    )
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.PENDING,
        width=100,
        height=100,
        file_size_bytes=5,
        mime_type="image/png",
        s3_original_key=f"pipeline/originals/{meme_id}/original.png",
        perceptual_hash=f"hash-{meme_id}",
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()
    return meme, file


async def ensure_active_collection(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    _ = await CollectionService(session).ensure_favorites_collection(user_id)


def ingest_result_for(
    *,
    outcome: IngestAcceptOutcome = IngestAcceptOutcome.ACCEPTED_ASYNC,
    uploader_user_id: uuid.UUID | None = None,
    meme_id: uuid.UUID | None = None,
    meme_file_id: uuid.UUID | None = None,
    source_attach_reason: SourceAttachReason | None = None,
    source_id: str = f"telegram_pm:{TELEGRAM_ID}:{TELEGRAM_ID}",
    post_id: str = "message:701:file:photo-unique-1",
) -> IngestAcceptResult:
    now = utcnow()
    status = (
        PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
        if meme_id is None
        else PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE
    )
    resolved_meme_file_id = meme_file_id if meme_id is not None else None
    return IngestAcceptResult(
        outcome=outcome,
        ingest_request=IngestRequestRead(
            id=uuid.uuid7(),
            source_platform=SourcePlatform.TELEGRAM,
            source_id=source_id,
            post_id=post_id,
            source_kind=IngestSourceKind.USER_UPLOAD,
            uploader_user_id=uploader_user_id,
            user_metadata={},
            source_metadata={"view_count": 0},
            declared_filename="telegram-photo-701-photo-unique-1.jpg",
            declared_content_type="image/jpeg",
            temp_original_object_key="pipeline/temp-originals/fake/raw.jpg" if meme_id is None else None,
            sha256_hex="a" * 64,
            file_size_bytes=5,
            status=status,
            failure_code=None,
            failure_detail=None,
            attempt_count=0,
            locked_at=None,
            materialized_meme_id=meme_id,
            materialized_meme_file_id=resolved_meme_file_id,
            matched_meme_file_id=resolved_meme_file_id,
            source_attach_reason=source_attach_reason,
            created_at=now,
            updated_at=now,
        ),
    )


def build_upload_dispatcher(
    *,
    settings: Settings,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    accept_service: FakePrivateUploadAcceptService,
    downloader: FakeTelegramFileDownloader,
) -> Dispatcher:
    return build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        private_upload_accept_service_factory=lambda session: accept_service,
        telegram_file_downloader=downloader,
    )


@dataclass(slots=True)
class _UploadBotContext:
    settings: Settings
    recording_session: RecordingTelegramSession
    bot: Bot
    dispatcher: Dispatcher
    downloader: FakeTelegramFileDownloader


def _build_upload_bot_context(
    *,
    database_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    accept_service: FakePrivateUploadAcceptService,
    downloader: FakeTelegramFileDownloader | None = None,
) -> _UploadBotContext:
    settings = build_bot_settings(database_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    resolved_downloader = downloader if downloader is not None else FakeTelegramFileDownloader()
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
        downloader=resolved_downloader,
    )
    return _UploadBotContext(
        settings=settings,
        recording_session=recording_session,
        bot=bot,
        dispatcher=dispatcher,
        downloader=resolved_downloader,
    )


async def dispatch_photo_upload(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    file_size: int = 5,
    update_id: int = 1,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "message": {
                "message_id": 700 + update_id,
                "date": 1_700_000_000,
                "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Upload"},
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Upload"},
                "photo": [
                    {
                        "file_id": f"photo-file-{update_id}",
                        "file_unique_id": f"photo-unique-{update_id}",
                        "width": 100,
                        "height": 100,
                        "file_size": file_size,
                    }
                ],
            },
        },
    )


async def dispatch_document_upload(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    mime_type: str,
    file_size: int = 5,
    update_id: int = 20,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "message": {
                "message_id": 800 + update_id,
                "date": 1_700_000_000,
                "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Upload"},
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Upload"},
                "document": {
                    "file_id": f"doc-file-{update_id}",
                    "file_unique_id": f"doc-unique-{update_id}",
                    "file_name": f"document-{update_id}.png",
                    "mime_type": mime_type,
                    "file_size": file_size,
                },
            },
        },
    )


async def dispatch_animation_upload(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    file_size: int = 5,
    update_id: int = 40,
) -> None:
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": update_id,
            "message": {
                "message_id": 900 + update_id,
                "date": 1_700_000_000,
                "chat": {"id": TELEGRAM_ID, "type": "private", "first_name": "Upload"},
                "from": {"id": TELEGRAM_ID, "is_bot": False, "first_name": "Upload"},
                "animation": {
                    "file_id": f"animation-file-{update_id}",
                    "file_unique_id": f"animation-unique-{update_id}",
                    "width": 100,
                    "height": 100,
                    "duration": 1,
                    "file_name": f"animation-{update_id}.gif",
                    "mime_type": "image/gif",
                    "file_size": file_size,
                },
            },
        },
    )


def last_bot_text(session: RecordingTelegramSession) -> str:
    assert session.sent_methods, "Expected a bot response."
    method = session.sent_methods[-1]
    assert isinstance(method, SendMessage)
    assert isinstance(method.text, str)
    return method.text


@pytest.mark.asyncio
async def test_private_photo_upload_queues_raw_ingest_with_active_collection_metadata(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(
        accept_result=ingest_result_for(uploader_user_id=user.id),
    )
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
        downloader=FakeTelegramFileDownloader(media_bytes=b"photo-bytes"),
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert upload_bot.downloader.calls == ["photo-file-1"]
    assert len(accept_service.calls) == 1
    source = accept_service.calls[0]["source"]
    assert isinstance(source, IngestAcceptSource)
    assert source.uploader_user_id == user.id
    assert source.source_kind is IngestSourceKind.USER_UPLOAD
    active_collection_id = await migrated_db_session.scalar(
        select(User.active_save_collection_id).where(User.id == user.id)
    )
    assert active_collection_id is not None
    assert source.user_metadata[TARGET_COLLECTION_ID_METADATA_KEY] == str(active_collection_id)
    assert source.source_platform is SourcePlatform.TELEGRAM
    assert source.source_id == f"telegram_pm:{TELEGRAM_ID}:{TELEGRAM_ID}"
    assert source.post_id == "message:701:file:photo-unique-1"
    assert source.view_count == 0
    assert accept_service.calls[0]["filename"] == "telegram-photo-701-photo-unique-1.jpg"
    assert accept_service.calls[0]["content_type"] == "image/jpeg"
    assert accept_service.calls[0]["media_bytes"] == b"photo-bytes"
    assert "Результат появится" in last_bot_text(upload_bot.recording_session)
    assert await migrated_db_session.scalar(select(CollectionMeme.meme_id)) is None
    async with postgres_session_factory() as session:
        event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.COLLECTION_ACTION)
        )
        persisted_active_collection_id = await session.scalar(
            select(User.active_save_collection_id).where(User.id == user.id)
        )
    assert event is not None
    assert event.user_id == user.id
    assert event.payload["surface"] == "telegram_pm_upload"
    refs = _analytics_refs(event)
    properties = _analytics_properties(event)
    assert persisted_active_collection_id is not None
    assert refs["collection_id"] == str(persisted_active_collection_id)
    assert properties["action"] == "upload_queued"
    assert properties["media_kind"] == "image"
    assert properties["content_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_private_upload_public_duplicate_saves_existing_public_meme(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    public_meme, file = await seed_meme(migrated_db_session, is_public=True)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(
        accept_result=ingest_result_for(
            outcome=IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE,
            uploader_user_id=user.id,
            meme_id=public_meme.id,
            meme_file_id=file.id,
            source_attach_reason=SourceAttachReason.SHA256_EXACT_EXISTING_FILE,
            post_id="message:821:file:doc-unique-21",
        )
    )
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_document_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot, mime_type="image/gif")

    assert "публичным мемом" in last_bot_text(upload_bot.recording_session)
    assert await migrated_db_session.scalar(
        select(CollectionMeme.meme_id).where(CollectionMeme.meme_id == public_meme.id)
    ) == public_meme.id
    async with postgres_session_factory() as session:
        event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_SAVE)
        )
        active_collection_id = await session.scalar(select(User.active_save_collection_id).where(User.id == user.id))
    assert event is not None
    assert event.user_id == user.id
    assert event.payload["surface"] == "telegram_pm_upload"
    refs = _analytics_refs(event)
    properties = _analytics_properties(event)
    assert refs["meme_id"] == str(public_meme.id)
    assert active_collection_id is not None
    assert refs["collection_id"] == str(active_collection_id)
    assert properties["action"] == "duplicate_saved"
    assert properties["outcome"] == "resolved_sha_duplicate"


@pytest.mark.asyncio
async def test_private_exact_duplicate_is_saved_after_atomic_membership_attachment(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    private_meme, file = await seed_meme(migrated_db_session, is_public=False)
    active_collection_id = await migrated_db_session.scalar(
        select(User.active_save_collection_id).where(User.id == user.id)
    )
    assert active_collection_id is not None
    migrated_db_session.add(
        CollectionMeme(
            collection_id=active_collection_id,
            meme_id=private_meme.id,
            added_by_user_id=user.id,
        )
    )
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(
        accept_result=ingest_result_for(
            outcome=IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE,
            uploader_user_id=user.id,
            meme_id=private_meme.id,
            meme_file_id=file.id,
            source_attach_reason=SourceAttachReason.SHA256_EXACT_EXISTING_FILE,
        )
    )
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    bot_text = last_bot_text(upload_bot.recording_session)
    assert "приват" in bot_text
    assert "публичным" not in bot_text
    assert await migrated_db_session.get(CollectionMeme, (active_collection_id, private_meme.id)) is not None


@pytest.mark.asyncio
async def test_private_upload_duplicate_source_replay_resolves_existing_source_and_saves(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    meme, file = await seed_meme(migrated_db_session, is_public=True)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(
        accept_result=ingest_result_for(
            outcome=IngestAcceptOutcome.SOURCE_REPLAY,
            uploader_user_id=user.id,
            meme_id=meme.id,
            meme_file_id=file.id,
            source_attach_reason=SourceAttachReason.SHA256_EXACT_EXISTING_FILE,
        )
    )
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "уже было обработано" in last_bot_text(upload_bot.recording_session)
    saved_meme_id = await migrated_db_session.scalar(
        select(CollectionMeme.meme_id).where(CollectionMeme.meme_id == meme.id)
    )
    assert saved_meme_id == meme.id


@pytest.mark.asyncio
async def test_private_upload_source_replay_without_materialized_meme_reports_processing(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(
        accept_result=ingest_result_for(
            outcome=IngestAcceptOutcome.SOURCE_REPLAY,
            uploader_user_id=user.id,
        )
    )
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "уже в обработке" in last_bot_text(upload_bot.recording_session)
    assert await migrated_db_session.scalar(select(CollectionMeme.meme_id)) is None


@pytest.mark.asyncio
async def test_private_upload_rejects_unsupported_document_without_download(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService()
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_document_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot, mime_type="video/mp4")

    assert "Поддерживаются только изображения" in last_bot_text(upload_bot.recording_session)
    assert upload_bot.downloader.calls == []
    assert accept_service.calls == []


@pytest.mark.asyncio
async def test_private_upload_rejects_oversize_animation_before_download(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService()
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_animation_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot, file_size=21)

    assert "Файл слишком большой" in last_bot_text(upload_bot.recording_session)
    assert upload_bot.downloader.calls == []
    assert accept_service.calls == []


@pytest.mark.asyncio
async def test_private_upload_auto_creates_user_and_favorites_before_ingest(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    _ = migrated_db_session
    accept_service = FakePrivateUploadAcceptService(accept_result=ingest_result_for())
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "Результат появится" in last_bot_text(upload_bot.recording_session)
    assert upload_bot.downloader.calls == ["photo-file-1"]
    assert len(accept_service.calls) == 1
    source = accept_service.calls[0]["source"]
    assert isinstance(source, IngestAcceptSource)
    async with postgres_session_factory() as session:
        created_user = await session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))
        assert created_user is not None
        favorites = await session.scalar(
            select(Collection).where(
                Collection.owner_id == created_user.id,
                Collection.kind == CollectionKind.FAVORITES,
            )
        )
    assert created_user.account_type is AccountType.FULL
    assert created_user.status is AccountStatus.ACTIVE
    assert favorites is not None
    assert created_user.active_save_collection_id == favorites.id
    assert source.uploader_user_id == created_user.id
    assert source.user_metadata[TARGET_COLLECTION_ID_METADATA_KEY] == str(favorites.id)


@pytest.mark.asyncio
async def test_private_upload_existing_user_without_active_collection_defaults_to_favorites(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(accept_result=ingest_result_for(uploader_user_id=user.id))
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "Результат появится" in last_bot_text(upload_bot.recording_session)
    assert upload_bot.downloader.calls == ["photo-file-1"]
    assert len(accept_service.calls) == 1
    source = accept_service.calls[0]["source"]
    assert isinstance(source, IngestAcceptSource)
    async with postgres_session_factory() as session:
        persisted_user = await session.scalar(select(User).where(User.id == user.id))
        favorites = await session.scalar(
            select(Collection).where(Collection.owner_id == user.id, Collection.kind == CollectionKind.FAVORITES)
        )
    assert persisted_user is not None
    assert favorites is not None
    assert persisted_user.active_save_collection_id == favorites.id
    assert source.user_metadata[TARGET_COLLECTION_ID_METADATA_KEY] == str(favorites.id)


@pytest.mark.asyncio
async def test_private_upload_deletion_pending_user_is_rejected_before_download(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    migrated_db_session.add(User(telegram_id=TELEGRAM_ID, status=AccountStatus.DELETION_PENDING))
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService()
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "недоступен или неактивен" in last_bot_text(upload_bot.recording_session)
    assert upload_bot.downloader.calls == []
    assert accept_service.calls == []
    async with postgres_session_factory() as session:
        users = list(await session.scalars(select(User).where(User.telegram_id == TELEGRAM_ID)))
    assert len(users) == 1
    assert users[0].status is AccountStatus.DELETION_PENDING


@pytest.mark.asyncio
async def test_private_upload_reports_download_failure_without_pipeline_call(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService()
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
        downloader=FakeTelegramFileDownloader(error=TelegramDownloadError("boom")),
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "Не удалось скачать файл" in last_bot_text(upload_bot.recording_session)
    assert upload_bot.downloader.calls == ["photo-file-1"]
    assert accept_service.calls == []


@pytest.mark.asyncio
async def test_private_upload_reports_pipeline_factory_setup_failure_before_download(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    await migrated_db_session.commit()

    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)

    def raising_accept_factory(session: AsyncSession) -> FakePrivateUploadAcceptService:
        _ = session
        raise RuntimeError("pipeline config is unavailable")

    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        private_upload_accept_service_factory=raising_accept_factory,
        telegram_file_downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert "Сервис загрузки сейчас недоступен" in last_bot_text(recording_session)
    assert downloader.calls == []


@pytest.mark.asyncio
async def test_private_upload_reports_active_collection_save_failure(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    other_user = await create_full_user_via_upgrade(user_service, email="collection-owner@example.com")
    other_collection = await collection_service.create_custom_collection(owner_user_id=other_user.id, title="Other")
    meme, file = await seed_meme(migrated_db_session, is_public=False)
    persisted_user = await migrated_db_session.scalar(select(User).where(User.id == user.id))
    assert persisted_user is not None
    persisted_user.active_save_collection_id = other_collection.id
    await migrated_db_session.commit()

    accept_service = FakePrivateUploadAcceptService(
        accept_result=ingest_result_for(
            outcome=IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE,
            uploader_user_id=user.id,
            meme_id=meme.id,
            meme_file_id=file.id,
            source_attach_reason=SourceAttachReason.SHA256_EXACT_EXISTING_FILE,
        )
    )
    upload_bot = _build_upload_bot_context(
        database_url=postgres_async_url,
        postgres_session_factory=postgres_session_factory,
        accept_service=accept_service,
    )

    await dispatch_photo_upload(dispatcher=upload_bot.dispatcher, bot=upload_bot.bot)

    assert "сохранить мем в активную коллекцию не удалось" in last_bot_text(upload_bot.recording_session)
    assert await migrated_db_session.scalar(select(CollectionMeme.meme_id)) is None
