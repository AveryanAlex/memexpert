"""Telegram private-chat quick-save upload ingestion router."""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from aiogram import F, Router
from pydantic import ValidationError
from sqlalchemy import select

from memexpert.bot.analytics import record_telegram_interaction_event, telegram_user_hash
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.ingest.accept_service import PipelineIngestAcceptService
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptResult, IngestAcceptSource
from memexpert.ingest.target_collection_metadata import user_metadata_with_target_collection
from memexpert.models.content import Meme, MemeFile, MemeSource
from memexpert.models.enums import (
    AnalyticsEventType,
    ContentKind,
    SourceAttachReason,
    SourcePlatform,
)
from memexpert.services import (
    CollectionNotFoundError,
    CollectionService,
    CollectionServiceError,
    CollectionWriteAccessError,
    GuestCollectionAccessError,
    MemeNotFoundError,
    PipelinePayloadTooLargeError,
    PipelinePayloadValidationError,
    PipelineServiceError,
    PipelineSourceConflictError,
    PipelineStorageError,
    PipelineUnsupportedMediaTypeError,
    UserNotFoundError,
)
from memexpert.services.telegram_accounts import resolve_or_create_active_telegram_user

if TYPE_CHECKING:
    import uuid

    from aiogram import Bot
    from aiogram.types import Animation, Document, Message, PhotoSize
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.models.user import User
    from memexpert.schemas import CollectionRead

logger = logging.getLogger(__name__)

_SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_SUPPORTED_GIF_MIME_TYPES = frozenset({"image/gif", "video/mp4"})
type ActiveCollectionSaveResult = Literal["saved", "not_visible", "collection_error"]


class PrivateUploadAcceptService(Protocol):
    async def accept_bytes(
        self,
        *,
        source: IngestAcceptSource,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> IngestAcceptResult: ...


class ActiveCollectionSaver(Protocol):
    async def get_active_save_collection(self, *, user_id: object) -> CollectionRead: ...

    async def save_meme_to_active_collection(self, *, user_id: object, meme_id: object) -> object: ...


class TelegramFileDownloader(Protocol):
    async def download_file(self, bot: Bot, *, file_id: str) -> bytes: ...


type PrivateUploadAcceptServiceFactory = Callable[[AsyncSession], PrivateUploadAcceptService]
type CollectionServiceFactory = Callable[[AsyncSession], ActiveCollectionSaver]


class TelegramDownloadError(RuntimeError):
    """Raised when Telegram file metadata or bytes cannot be fetched."""


class AiogramTelegramFileDownloader:
    """Download Telegram files through Bot API calls behind a testable boundary."""

    async def download_file(self, bot: Bot, *, file_id: str) -> bytes:
        try:
            telegram_file = await bot.get_file(file_id)
            if telegram_file.file_path is None:
                raise TelegramDownloadError("Telegram did not return a downloadable file path.")

            destination = io.BytesIO()
            _ = await bot.download_file(telegram_file.file_path, destination=destination)
            return destination.getvalue()
        except TelegramDownloadError:
            raise
        except Exception as exc:
            raise TelegramDownloadError("Telegram file download failed.") from exc


@dataclass(frozen=True, slots=True)
class TelegramUploadMedia:
    file_id: str
    file_unique_id: str
    filename: str
    content_type: str
    file_size: int | None
    limit_kind: ContentKind


def build_private_upload_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    accept_service_factory: PrivateUploadAcceptServiceFactory | None = None,
    collection_service_factory: CollectionServiceFactory | None = None,
    telegram_file_downloader: TelegramFileDownloader | None = None,
) -> Router:
    """Build the private-message media upload router."""

    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_accept_factory = accept_service_factory or (
        lambda session: PipelineIngestAcceptService.from_settings(session, settings=resolved_settings)
    )
    resolved_collection_factory = collection_service_factory or (lambda session: CollectionService(session))
    resolved_downloader = telegram_file_downloader or AiogramTelegramFileDownloader()

    router = Router(name="private-upload")

    @router.message(F.chat.type == "private", F.photo)
    async def handle_photo_upload(message: Message, bot: Bot) -> None:
        await handle_private_upload_message(
            message=message,
            bot=bot,
            settings=resolved_settings,
            session_factory=resolved_session_factory,
            accept_service_factory=resolved_accept_factory,
            collection_service_factory=resolved_collection_factory,
            telegram_file_downloader=resolved_downloader,
        )

    @router.message(F.chat.type == "private", F.animation)
    async def handle_animation_upload(message: Message, bot: Bot) -> None:
        await handle_private_upload_message(
            message=message,
            bot=bot,
            settings=resolved_settings,
            session_factory=resolved_session_factory,
            accept_service_factory=resolved_accept_factory,
            collection_service_factory=resolved_collection_factory,
            telegram_file_downloader=resolved_downloader,
        )

    @router.message(F.chat.type == "private", F.document)
    async def handle_document_upload(message: Message, bot: Bot) -> None:
        await handle_private_upload_message(
            message=message,
            bot=bot,
            settings=resolved_settings,
            session_factory=resolved_session_factory,
            accept_service_factory=resolved_accept_factory,
            collection_service_factory=resolved_collection_factory,
            telegram_file_downloader=resolved_downloader,
        )

    return router


async def handle_private_upload_message(
    *,
    message: Message,
    bot: Bot,
    settings: Settings,
    session_factory: AsyncSessionFactory,
    accept_service_factory: PrivateUploadAcceptServiceFactory,
    collection_service_factory: CollectionServiceFactory,
    telegram_file_downloader: TelegramFileDownloader,
) -> None:
    telegram_user_id = _extract_telegram_user_id(message)
    if telegram_user_id is None:
        await message.answer(_missing_identity_message())
        return

    media = _extract_upload_media(message)
    if media is None:
        await message.answer(_unsupported_media_message())
        return

    upload_limit = _upload_limit_for_media(settings, media.limit_kind)
    if media.file_size is not None and media.file_size > upload_limit:
        await message.answer(_oversize_message(upload_limit=upload_limit))
        return

    async with session_factory() as session:
        account_resolution = await resolve_or_create_active_telegram_user(
            session,
            telegram_user_id=telegram_user_id,
        )
        if not account_resolution.is_active:
            await message.answer(_account_unavailable_message())
            return
        linked_user = account_resolution.user
        assert linked_user is not None

        try:
            accept_service = accept_service_factory(session)
            collection_service = collection_service_factory(session)
            target_collection = await collection_service.get_active_save_collection(user_id=linked_user.id)
        except (CollectionServiceError, UserNotFoundError):
            await message.answer(_collection_setup_failure_message())
            return
        except Exception:
            logger.exception("Telegram private upload service setup failed for user_id=%s.", linked_user.id)
            await message.answer(_pipeline_setup_failure_message())
            return

        source = _build_upload_source(
            message=message,
            media=media,
            telegram_user_id=telegram_user_id,
            owner_user_id=linked_user.id,
            target_collection_id=target_collection.id,
        )

        try:
            media_bytes = await telegram_file_downloader.download_file(bot, file_id=media.file_id)
        except TelegramDownloadError:
            await message.answer(_download_failure_message())
            return

        try:
            accept_result = await accept_service.accept_bytes(
                source=source,
                filename=media.filename,
                content_type=media.content_type,
                media_bytes=media_bytes,
            )
        except PipelineSourceConflictError:
            await _handle_duplicate_source_replay(
                message=message,
                session=session,
                collection_service=collection_service,
                user=linked_user,
                media=media,
                telegram_user_id=telegram_user_id,
                source=source,
            )
            return
        except PipelinePayloadTooLargeError:
            await message.answer(_oversize_message(upload_limit=upload_limit))
            return
        except PipelineUnsupportedMediaTypeError:
            await message.answer(_unsupported_media_message())
            return
        except PipelineStorageError:
            await message.answer(_storage_failure_message())
            return
        except PipelinePayloadValidationError:
            await message.answer(_invalid_media_message())
            return
        except PipelineServiceError:
            logger.exception("Telegram private upload ingestion failed for user_id=%s.", linked_user.id)
            await message.answer(_pipeline_failure_message())
            return

        await _handle_accept_result(
            message=message,
            session=session,
            collection_service=collection_service,
            user=linked_user,
            media=media,
            telegram_user_id=telegram_user_id,
            result=accept_result,
        )


async def _handle_accept_result(
    *,
    message: Message,
    session: AsyncSession,
    collection_service: ActiveCollectionSaver,
    user: User,
    media: TelegramUploadMedia,
    telegram_user_id: int,
    result: IngestAcceptResult,
) -> None:
    ingest_request = result.ingest_request
    if result.outcome is IngestAcceptOutcome.ACCEPTED_ASYNC:
        await message.answer(_private_upload_queued_message())
        await _record_upload_event(
            session,
            _upload_collection_event(
                user=user,
                telegram_user_id=telegram_user_id,
                action="upload_queued",
                media=media,
            ),
        )
        return

    if ingest_request.materialized_meme_id is None:
        await message.answer(_source_replay_queued_message())
        return

    meme = await _get_meme(session, ingest_request.materialized_meme_id)
    if meme is None:
        await message.answer(_source_replay_unknown_message())
        return

    if result.outcome is IngestAcceptOutcome.SOURCE_REPLAY:
        save_result = await _save_to_active_collection(
            collection_service,
            user_id=user.id,
            meme_id=ingest_request.materialized_meme_id,
        )
        if save_result == "saved":
            await message.answer(_source_replay_saved_message())
            await _record_upload_event(
                session,
                _upload_meme_save_event(
                    user=user,
                    telegram_user_id=telegram_user_id,
                    action="source_replay_saved",
                    outcome=result.outcome.value,
                    meme_id=ingest_request.materialized_meme_id,
                    media=media,
                ),
            )
            return
        if save_result == "not_visible":
            await message.answer(_private_duplicate_not_visible_message())
            return
        await message.answer(_source_replay_not_saved_message())
        return

    is_exact_existing_file = ingest_request.source_attach_reason in {
        SourceAttachReason.SHA256_EXACT_EXISTING_FILE,
        SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE,
    }
    if result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE or is_exact_existing_file:
        save_result = await _save_to_active_collection(
            collection_service,
            user_id=user.id,
            meme_id=ingest_request.materialized_meme_id,
        )
        if save_result == "saved" and meme.is_public:
            await message.answer(_public_duplicate_saved_message())
            await _record_upload_event(
                session,
                _upload_meme_save_event(
                    user=user,
                    telegram_user_id=telegram_user_id,
                    action="duplicate_saved",
                    outcome=result.outcome.value,
                    meme_id=ingest_request.materialized_meme_id,
                    media=media,
                    extra_properties={"visibility": "public"},
                ),
            )
            return
        if save_result == "saved":
            await message.answer(_private_duplicate_saved_message())
            await _record_upload_event(
                session,
                _upload_meme_save_event(
                    user=user,
                    telegram_user_id=telegram_user_id,
                    action="duplicate_saved",
                    outcome=result.outcome.value,
                    meme_id=ingest_request.materialized_meme_id,
                    media=media,
                    extra_properties={"visibility": "private"},
                ),
            )
            return
        if save_result == "collection_error":
            await message.answer(_collection_save_failure_message())
            return
        await message.answer(_private_duplicate_not_visible_message())
        return

    await message.answer(_private_upload_queued_message())


async def _handle_duplicate_source_replay(
    *,
    message: Message,
    session: AsyncSession,
    collection_service: ActiveCollectionSaver,
    user: User,
    media: TelegramUploadMedia,
    telegram_user_id: int,
    source: IngestAcceptSource,
) -> None:
    meme_id = await _resolve_meme_id_for_source(session, source=source)
    if meme_id is None:
        await message.answer(_source_replay_unknown_message())
        return

    save_result = await _save_to_active_collection(collection_service, user_id=user.id, meme_id=meme_id)
    if save_result == "saved":
        await message.answer(_source_replay_saved_message())
        await _record_upload_event(
            session,
            _upload_meme_save_event(
                user=user,
                telegram_user_id=telegram_user_id,
                action="duplicate_source_replay_saved",
                outcome="saved",
                meme_id=meme_id,
                media=media,
            ),
        )
        return
    if save_result == "not_visible":
        await message.answer(_private_duplicate_not_visible_message())
        return
    await message.answer(_source_replay_not_saved_message())


async def _save_to_active_collection(
    collection_service: ActiveCollectionSaver,
    *,
    user_id: object,
    meme_id: object,
) -> ActiveCollectionSaveResult:
    try:
        _ = await collection_service.save_meme_to_active_collection(user_id=user_id, meme_id=meme_id)
    except MemeNotFoundError:
        return "not_visible"
    except (
        CollectionNotFoundError,
        CollectionWriteAccessError,
        GuestCollectionAccessError,
        UserNotFoundError,
    ):
        return "collection_error"
    except CollectionServiceError:
        return "collection_error"
    return "saved"


async def _get_meme(session: AsyncSession, meme_id: object) -> Meme | None:
    result = await session.execute(select(Meme).where(Meme.id == meme_id))
    return result.scalar_one_or_none()


async def _resolve_meme_id_for_source(
    session: AsyncSession,
    *,
    source: IngestAcceptSource,
) -> object | None:
    result = await session.execute(
        select(MemeFile.meme_id)
        .join(MemeSource, MemeSource.file_id == MemeFile.id)
        .where(
            MemeSource.platform == source.source_platform,
            MemeSource.source_id == source.source_id,
            MemeSource.post_id == source.post_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _record_upload_event(session: AsyncSession, event: dict[str, object]) -> None:
    refs = event.get("refs")
    if not isinstance(refs, dict):
        refs = {}
    event_type = event.get("event_type")
    await record_telegram_interaction_event(
        session,
        event,
        log_context={
            "analytics_event_type": getattr(event_type, "value", str(event_type)),
            "surface": event.get("surface"),
            "user_id": str(event.get("user_id")) if event.get("user_id") else None,
            "collection_id": str(refs.get("collection_id")) if refs.get("collection_id") else None,
            "meme_id": str(refs.get("meme_id")) if refs.get("meme_id") else None,
        },
    )


def _upload_collection_event(
    *,
    user: User,
    telegram_user_id: int,
    action: str,
    media: TelegramUploadMedia,
) -> dict[str, object]:
    return {
        "event_type": AnalyticsEventType.COLLECTION_ACTION,
        "user_id": user.id,
        "surface": "telegram_pm_upload",
        "refs": {"collection_id": user.active_save_collection_id},
        "properties": {
            "action": action,
            "telegram_user_hash": telegram_user_hash(telegram_user_id),
            "media_kind": media.limit_kind.value,
            "content_type": media.content_type,
        },
    }


def _upload_meme_save_event(
    *,
    user: User,
    telegram_user_id: int,
    action: str,
    outcome: str,
    meme_id: object,
    media: TelegramUploadMedia,
    extra_properties: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "event_type": AnalyticsEventType.MEME_SAVE,
        "user_id": user.id,
        "surface": "telegram_pm_upload",
        "refs": {"collection_id": user.active_save_collection_id, "meme_id": meme_id},
        "properties": {
            "action": action,
            "outcome": outcome,
            "telegram_user_hash": telegram_user_hash(telegram_user_id),
            "media_kind": media.limit_kind.value,
            "content_type": media.content_type,
            **(extra_properties or {}),
        },
    }


def _extract_upload_media(message: Message) -> TelegramUploadMedia | None:
    if message.photo:
        return _media_from_photo(message.photo, message_id=message.message_id)
    if message.animation is not None:
        return _media_from_animation(message.animation, message_id=message.message_id)
    if message.document is not None:
        return _media_from_document(message.document, message_id=message.message_id)
    return None


def _media_from_photo(photos: list[PhotoSize], *, message_id: int) -> TelegramUploadMedia | None:
    if not photos:
        return None
    photo = max(photos, key=lambda candidate: candidate.file_size or candidate.width * candidate.height)
    return TelegramUploadMedia(
        file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        filename=f"telegram-photo-{message_id}-{photo.file_unique_id}.jpg",
        content_type="image/jpeg",
        file_size=photo.file_size,
        limit_kind=ContentKind.IMAGE,
    )


def _media_from_animation(animation: Animation, *, message_id: int) -> TelegramUploadMedia | None:
    content_type = _normalize_mime_type(animation.mime_type) or "image/gif"
    if content_type not in _SUPPORTED_GIF_MIME_TYPES:
        return None
    extension = _extension_for_content_type(content_type, fallback="gif")
    filename = _safe_telegram_filename(
        animation.file_name,
        fallback=f"telegram-animation-{message_id}-{animation.file_unique_id}.{extension}",
    )
    return TelegramUploadMedia(
        file_id=animation.file_id,
        file_unique_id=animation.file_unique_id,
        filename=filename,
        content_type=content_type,
        file_size=animation.file_size,
        limit_kind=ContentKind.GIF,
    )


def _media_from_document(document: Document, *, message_id: int) -> TelegramUploadMedia | None:
    content_type = _normalize_mime_type(document.mime_type)
    if content_type in _SUPPORTED_IMAGE_MIME_TYPES:
        limit_kind = ContentKind.IMAGE
    elif content_type == "image/gif":
        limit_kind = ContentKind.GIF
    else:
        return None

    extension = _extension_for_content_type(content_type, fallback="bin")
    filename = _safe_telegram_filename(
        document.file_name,
        fallback=f"telegram-document-{message_id}-{document.file_unique_id}.{extension}",
    )
    return TelegramUploadMedia(
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        filename=filename,
        content_type=content_type,
        file_size=document.file_size,
        limit_kind=limit_kind,
    )


def _build_upload_source(
    *,
    message: Message,
    media: TelegramUploadMedia,
    telegram_user_id: int,
    owner_user_id: uuid.UUID,
    target_collection_id: uuid.UUID,
) -> IngestAcceptSource:
    try:
        return IngestAcceptSource(
            source_platform=SourcePlatform.TELEGRAM,
            source_id=f"telegram_pm:{telegram_user_id}:{message.chat.id}",
            post_id=f"message:{message.message_id}:file:{media.file_unique_id}",
            owner_user_id=owner_user_id,
            user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection_id),
            view_count=0,
        )
    except ValidationError as exc:  # pragma: no cover - defensive; constructed from Bot API bounded fields.
        raise PipelinePayloadValidationError("Telegram upload source metadata is invalid.") from exc


def _extract_telegram_user_id(message: Message) -> int | None:
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.id <= 0:
        return None
    return telegram_user.id


def _upload_limit_for_media(settings: Settings, media_kind: ContentKind) -> int:
    if media_kind is ContentKind.GIF:
        return settings.pipeline_gif_upload_max_bytes
    return settings.pipeline_image_upload_max_bytes


def _normalize_mime_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _extension_for_content_type(content_type: str | None, *, fallback: str) -> str:
    return {
        "image/gif": "gif",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "video/mp4": "mp4",
    }.get(content_type or "", fallback)


def _safe_telegram_filename(value: str | None, *, fallback: str) -> str:
    if value is None:
        return fallback
    filename = PurePosixPath(value).name.strip()
    return filename or fallback


def _missing_identity_message() -> str:
    return "Не удалось определить ваш Telegram-профиль. Отправьте файл из личного чата с ботом."


def _account_unavailable_message() -> str:
    return "Telegram аккаунт MemeXpert недоступен или неактивен. Проверьте статус аккаунта и попробуйте снова."


def _collection_setup_failure_message() -> str:
    return "Не удалось подготовить активную коллекцию для сохранения. Проверьте статус аккаунта и попробуйте снова."


def _unsupported_media_message() -> str:
    return "Поддерживаются только изображения (JPEG, PNG, WebP) и GIF. Отправьте файл в личный чат с ботом."


def _oversize_message(*, upload_limit: int) -> str:
    limit_mb = upload_limit / (1024 * 1024)
    return f"Файл слишком большой. Лимит для этого типа: {limit_mb:.0f} МБ."


def _download_failure_message() -> str:
    return "Не удалось скачать файл из Telegram. Попробуйте отправить его ещё раз позже."


def _invalid_media_message() -> str:
    return "Файл не удалось распознать как поддерживаемое изображение или GIF."


def _storage_failure_message() -> str:
    return "Не удалось сохранить оригинал файла в хранилище. Попробуйте позже."


def _pipeline_failure_message() -> str:
    return "Не удалось поставить файл в обработку из-за временной ошибки сервиса. Попробуйте позже."


def _pipeline_setup_failure_message() -> str:
    return "Сервис загрузки сейчас недоступен из-за настройки провайдера или хранилища. Попробуйте позже."


def _collection_save_failure_message() -> str:
    return "Файл загружен, но сохранить мем в активную коллекцию не удалось. Проверьте доступ к коллекции."


def _private_upload_queued_message() -> str:
    return "Поставил файл в обработку. Результат появится в MemeXpert после пайплайна."


def _public_duplicate_saved_message() -> str:
    return "Это совпадает с уже известным публичным мемом. Сохранил его в вашу активную коллекцию."


def _private_duplicate_saved_message() -> str:
    return "Это совпадает с уже доступным вам приватным мемом. Сохранил его в активную коллекцию."


def _private_duplicate_not_visible_message() -> str:
    return "Такой файл уже встречался, но доступной версии для сохранения нет. Ничего не сохранил."


def _source_replay_saved_message() -> str:
    return "Это сообщение уже было обработано ранее. Сохранил найденный мем в активную коллекцию."


def _source_replay_unknown_message() -> str:
    return "Это сообщение уже было обработано ранее, но связанный мем не удалось найти."


def _source_replay_queued_message() -> str:
    return "Это сообщение уже в обработке. Результат появится после пайплайна."


def _source_replay_not_saved_message() -> str:
    return "Это сообщение уже было обработано ранее, но сохранить найденный мем в активную коллекцию не удалось."



__all__ = [
    "AiogramTelegramFileDownloader",
    "CollectionServiceFactory",
    "PrivateUploadAcceptServiceFactory",
    "TelegramDownloadError",
    "TelegramFileDownloader",
    "build_private_upload_router",
    "handle_private_upload_message",
]
