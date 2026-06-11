"""Focused tests for Telegram private-chat quick-save uploads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage, TelegramMethod
from aiogram.types import User as TelegramUser
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import select

from memexpert.bot.main import build_bot, build_dispatcher
from memexpert.bot.private_upload import TelegramDownloadError
from memexpert.core.config import Settings
from memexpert.models.base import utcnow
from memexpert.models.collection import CollectionMeme
from memexpert.models.content import Meme, MemeFile, MemeSource
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    SourcePlatform,
)
from memexpert.models.user import User
from memexpert.schemas.content_pipeline import (
    ContentPipelineStageJournalRead,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
)
from memexpert.services import CollectionService, PipelineSourceConflictError, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


BOT_TOKEN = "123456:telegram-upload-test-bot-token"
BOT_USERNAME = "memexpertbot"
RETURN_URL = "https://memexpert.test/link/telegram/complete"
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
class FakePrivateUploadPipelineService:
    upload_result: ContentPipelineUploadRead | None = None
    upload_error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def create_upload(
        self,
        *,
        metadata: ContentPipelineUploadMetadata,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> ContentPipelineUploadRead:
        self.calls.append(
            {
                "metadata": metadata,
                "filename": filename,
                "content_type": content_type,
                "media_bytes": media_bytes,
            }
        )
        if self.upload_error is not None:
            raise self.upload_error
        assert self.upload_result is not None
        return self.upload_result


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
    author_user_id: uuid.UUID | None = None,
) -> tuple[Meme, MemeFile]:
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
        status=ContentProcessingStatus.PENDING,
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
    return meme, file


async def ensure_active_collection(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    _ = await CollectionService(session).ensure_favorites_collection(user_id)


def upload_read_for(
    *,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID | None = None,
    status: ContentPipelineStageStatus = ContentPipelineStageStatus.PENDING,
) -> ContentPipelineUploadRead:
    now = utcnow()
    resolved_meme_file_id = meme_file_id or uuid.uuid7()
    current_stage = (
        ContentPipelineStage.INGEST
        if status is ContentPipelineStageStatus.DUPLICATE
        else ContentPipelineStage.TRANSCODE
    )
    normalized_reason = "duplicate_perceptual_hash" if status is ContentPipelineStageStatus.DUPLICATE else None
    return ContentPipelineUploadRead(
        meme_id=meme_id,
        meme_file_id=resolved_meme_file_id,
        current_stage=current_stage,
        current_status=status,
        original_object_key=f"pipeline/originals/{resolved_meme_file_id}/original.png",
        web_video_object_key=None,
        last_event_id=uuid.uuid7(),
        normalized_reason=normalized_reason,
        last_error_text=None,
        attempt_count=1 if status is ContentPipelineStageStatus.DUPLICATE else 0,
        stages=(
            ContentPipelineStageJournalRead(
                id=uuid.uuid7(),
                meme_file_id=resolved_meme_file_id,
                stage=current_stage,
                status=status,
                attempt_count=1 if status is ContentPipelineStageStatus.DUPLICATE else 0,
                last_event_id=uuid.uuid7(),
                normalized_reason=normalized_reason,
                last_error_text=None,
                is_retryable=status is ContentPipelineStageStatus.PENDING,
                retry_after=None,
                started_at=now if status is ContentPipelineStageStatus.DUPLICATE else None,
                finished_at=now if status is ContentPipelineStageStatus.DUPLICATE else None,
                created_at=now,
                updated_at=now,
            ),
        ),
    )


def build_upload_dispatcher(
    *,
    settings: Settings,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    pipeline_service: FakePrivateUploadPipelineService,
    downloader: FakeTelegramFileDownloader,
) -> Dispatcher:
    return build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        private_upload_pipeline_service_factory=lambda session: pipeline_service,
        telegram_file_downloader=downloader,
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
async def test_private_photo_upload_creates_private_meme_and_saves_active_collection(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    meme, _file = await seed_meme(migrated_db_session, is_public=False, author_user_id=user.id)
    await migrated_db_session.commit()

    pipeline_service = FakePrivateUploadPipelineService(upload_result=upload_read_for(meme_id=meme.id))
    downloader = FakeTelegramFileDownloader(media_bytes=b"photo-bytes")
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert downloader.calls == ["photo-file-1"]
    assert len(pipeline_service.calls) == 1
    metadata = pipeline_service.calls[0]["metadata"]
    assert isinstance(metadata, ContentPipelineUploadMetadata)
    assert metadata.owner_user_id == user.id
    assert metadata.source_platform is SourcePlatform.TELEGRAM
    assert metadata.source_id == f"telegram_pm:{TELEGRAM_ID}:{TELEGRAM_ID}"
    assert metadata.post_id == "message:701:file:photo-unique-1"
    assert pipeline_service.calls[0]["filename"] == "telegram-photo-701-photo-unique-1.jpg"
    assert pipeline_service.calls[0]["content_type"] == "image/jpeg"
    assert pipeline_service.calls[0]["media_bytes"] == b"photo-bytes"
    assert "Обработка запущена" in last_bot_text(recording_session)
    saved_meme_id = await migrated_db_session.scalar(
        select(CollectionMeme.meme_id).where(CollectionMeme.meme_id == meme.id)
    )
    assert saved_meme_id == meme.id


@pytest.mark.asyncio
async def test_private_upload_public_duplicate_saves_existing_public_meme(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    public_meme, _file = await seed_meme(migrated_db_session, is_public=True)
    await migrated_db_session.commit()

    pipeline_service = FakePrivateUploadPipelineService(
        upload_result=upload_read_for(meme_id=public_meme.id, status=ContentPipelineStageStatus.DUPLICATE)
    )
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_document_upload(dispatcher=dispatcher, bot=bot, mime_type="image/gif")

    assert "публичным мемом" in last_bot_text(recording_session)
    assert await migrated_db_session.scalar(
        select(CollectionMeme.meme_id).where(CollectionMeme.meme_id == public_meme.id)
    ) == public_meme.id


@pytest.mark.asyncio
async def test_private_duplicate_not_visible_is_not_reported_as_public_or_saved(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await ensure_active_collection(migrated_db_session, user_id=user.id)
    other_user = await create_full_user_via_upgrade(user_service, email="private-owner@example.com")
    private_meme, _file = await seed_meme(migrated_db_session, is_public=False, author_user_id=other_user.id)
    await migrated_db_session.commit()

    pipeline_service = FakePrivateUploadPipelineService(
        upload_result=upload_read_for(meme_id=private_meme.id, status=ContentPipelineStageStatus.DUPLICATE)
    )
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    bot_text = last_bot_text(recording_session)
    assert "доступной версии" in bot_text
    assert "приват" not in bot_text
    assert "публичным" not in bot_text
    assert await migrated_db_session.scalar(select(CollectionMeme.meme_id)) is None


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
    migrated_db_session.add(
        MemeSource(
            file_id=file.id,
            platform=SourcePlatform.TELEGRAM,
            source_id=f"telegram_pm:{TELEGRAM_ID}:{TELEGRAM_ID}",
            post_id="message:701:file:photo-unique-1",
            views=0,
            reactions={},
            is_first_source=True,
            source_alive=True,
        )
    )
    await migrated_db_session.commit()

    pipeline_service = FakePrivateUploadPipelineService(upload_error=PipelineSourceConflictError("duplicate source"))
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert "уже было обработано" in last_bot_text(recording_session)
    saved_meme_id = await migrated_db_session.scalar(
        select(CollectionMeme.meme_id).where(CollectionMeme.meme_id == meme.id)
    )
    assert saved_meme_id == meme.id


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

    pipeline_service = FakePrivateUploadPipelineService()
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_document_upload(dispatcher=dispatcher, bot=bot, mime_type="video/mp4")

    assert "Поддерживаются только изображения" in last_bot_text(recording_session)
    assert downloader.calls == []
    assert pipeline_service.calls == []


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

    pipeline_service = FakePrivateUploadPipelineService()
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_animation_upload(dispatcher=dispatcher, bot=bot, file_size=21)

    assert "Файл слишком большой" in last_bot_text(recording_session)
    assert downloader.calls == []
    assert pipeline_service.calls == []


@pytest.mark.asyncio
async def test_private_upload_unlinked_user_is_rejected_before_download(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    _ = migrated_db_session
    pipeline_service = FakePrivateUploadPipelineService()
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert "Сначала привяжите Telegram" in last_bot_text(recording_session)
    assert downloader.calls == []
    assert pipeline_service.calls == []


@pytest.mark.asyncio
async def test_private_upload_missing_active_collection_is_rejected_before_download(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_async_url: str,
) -> None:
    user_service = UserService(migrated_db_session)
    _user = await create_full_user_via_upgrade(user_service, telegram_id=TELEGRAM_ID)
    await migrated_db_session.commit()

    pipeline_service = FakePrivateUploadPipelineService()
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert "активную коллекцию" in last_bot_text(recording_session)
    assert downloader.calls == []
    assert pipeline_service.calls == []


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

    pipeline_service = FakePrivateUploadPipelineService()
    downloader = FakeTelegramFileDownloader(error=TelegramDownloadError("boom"))
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert "Не удалось скачать файл" in last_bot_text(recording_session)
    assert downloader.calls == ["photo-file-1"]
    assert pipeline_service.calls == []


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

    def raising_pipeline_factory(session: AsyncSession) -> FakePrivateUploadPipelineService:
        _ = session
        raise RuntimeError("pipeline config is unavailable")

    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=postgres_session_factory,
        private_upload_pipeline_service_factory=raising_pipeline_factory,
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
    meme, _file = await seed_meme(migrated_db_session, is_public=False, author_user_id=user.id)
    persisted_user = await migrated_db_session.scalar(select(User).where(User.id == user.id))
    assert persisted_user is not None
    persisted_user.active_save_collection_id = other_collection.id
    await migrated_db_session.commit()

    pipeline_service = FakePrivateUploadPipelineService(upload_result=upload_read_for(meme_id=meme.id))
    downloader = FakeTelegramFileDownloader()
    settings = build_bot_settings(postgres_async_url)
    recording_session = RecordingTelegramSession()
    bot = build_bot(settings, session=recording_session)
    dispatcher = build_upload_dispatcher(
        settings=settings,
        postgres_session_factory=postgres_session_factory,
        pipeline_service=pipeline_service,
        downloader=downloader,
    )

    await dispatch_photo_upload(dispatcher=dispatcher, bot=bot)

    assert "сохранить мем в активную коллекцию не удалось" in last_bot_text(recording_session)
    assert await migrated_db_session.scalar(select(CollectionMeme.meme_id)) is None
