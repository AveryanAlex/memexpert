# ruff: noqa: TC003
"""API-safe raw ingest accept service.

This module intentionally uses only stdlib hashing, config, storage, and ORM
models. Heavy media inspection/materialization stays in future workers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, Self, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from memexpert.core.config import Settings, get_settings
from memexpert.core.storage import (
    PipelineStorageSettings,
    build_temp_original_object_key,
    delete_object_if_present,
    get_pipeline_storage_settings,
    get_s3_client,
    upload_object_bytes,
)
from memexpert.ingest.collection_targets import (
    save_meme_to_target_collection,
    validate_target_collection_write,
    visible_meme_clause,
)
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptResult, IngestAcceptSource, IngestRequestRead
from memexpert.ingest.source_metadata import source_forward_ids, source_published_at, source_reactions
from memexpert.ingest.target_collection_metadata import (
    TargetCollectionMetadataError,
    parse_target_collection_id,
)
from memexpert.messaging.rabbitmq_outbox import RabbitPublisher, relay_rabbitmq_outbox_messages_best_effort
from memexpert.models.content import Meme, MemeFile, MemeSource, PipelineIngestRequest
from memexpert.models.enums import (
    ContentProcessingStatus,
    PipelineIngestRequestStatus,
    SourceAttachReason,
)
from memexpert.pipeline.events import build_media_inspect_message_spec
from memexpert.schemas.pipeline_base import MAX_TELEGRAM_CONTENT_TYPE_LENGTH, MAX_TELEGRAM_FILENAME_LENGTH
from memexpert.services.errors import (
    PipelineIngestError,
    PipelinePayloadTooLargeError,
    PipelinePayloadValidationError,
    PipelineSourceConflictError,
    PipelineStorageError,
    PipelineUnsupportedMediaTypeError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.messaging.rabbitmq_outbox import RabbitBrokerProtocol

logger = logging.getLogger(__name__)


class ObjectStorageClient(Protocol):
    """Minimal S3-compatible client surface used by the raw accept path."""

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


class PipelineIngestAcceptService:
    """Accept raw bytes into temporary storage and durable ingest-request rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        broker: RabbitBrokerProtocol | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._broker = broker
        self._storage_settings: PipelineStorageSettings = get_pipeline_storage_settings(self._settings)
        self._storage_client = storage_client or cast("ObjectStorageClient", get_s3_client())
        self._rabbit_publisher = RabbitPublisher(broker=broker, settings=self._settings)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        broker: RabbitBrokerProtocol | None = None,
    ) -> Self:
        """Build the API-safe ingest accept service from shared settings."""

        return cls(session, settings=settings, storage_client=storage_client, broker=broker)

    async def accept_bytes(
        self,
        *,
        source: IngestAcceptSource,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> IngestAcceptResult:
        """Accept raw upload bytes or synchronously resolve an exact SHA duplicate."""

        existing_request = await self._find_existing_request(source)
        if existing_request is not None:
            return self._result(existing_request, IngestAcceptOutcome.SOURCE_REPLAY)

        normalized_filename = self._normalize_filename(filename)
        normalized_content_type = self._normalize_content_type(content_type)
        file_size_bytes = len(media_bytes)
        self._validate_declared_upload_size(
            content_type=normalized_content_type,
            file_size_bytes=file_size_bytes,
        )
        sha256_hex = hashlib.sha256(media_bytes).hexdigest()
        user_metadata = self._normalize_metadata(source.user_metadata, field_name="user_metadata")
        source_metadata = self._normalize_metadata(source.source_metadata, field_name="source_metadata")
        source_metadata["views"] = source.views
        target_collection_id = self._parse_target_collection_id(user_metadata)
        await validate_target_collection_write(
            self._session,
            owner_user_id=source.owner_user_id,
            target_collection_id=target_collection_id,
        )

        matched_file = await self._find_sha256_match(sha256_hex, owner_user_id=source.owner_user_id)
        if matched_file is not None:
            return await self._accept_sha_duplicate(
                source=source,
                filename=normalized_filename,
                content_type=normalized_content_type,
                sha256_hex=sha256_hex,
                file_size_bytes=file_size_bytes,
                matched_file=matched_file,
                user_metadata=user_metadata,
                source_metadata=source_metadata,
                target_collection_id=target_collection_id,
            )

        return await self._accept_new_bytes(
            source=source,
            filename=normalized_filename,
            content_type=normalized_content_type,
            media_bytes=media_bytes,
            sha256_hex=sha256_hex,
            file_size_bytes=file_size_bytes,
            user_metadata=user_metadata,
            source_metadata=source_metadata,
        )

    async def _accept_sha_duplicate(
        self,
        *,
        source: IngestAcceptSource,
        filename: str,
        content_type: str,
        sha256_hex: str,
        file_size_bytes: int,
        matched_file: MemeFile,
        user_metadata: dict[str, object],
        source_metadata: dict[str, object],
        target_collection_id: uuid.UUID | None,
    ) -> IngestAcceptResult:
        attach_reason = self._sha_match_attach_reason(matched_file)
        ingest_request = PipelineIngestRequest(
            source_platform=source.source_platform,
            source_id=source.source_id,
            post_id=source.post_id,
            owner_user_id=source.owner_user_id,
            user_metadata=user_metadata,
            source_metadata=source_metadata,
            declared_filename=filename,
            declared_content_type=content_type,
            temp_original_object_key=None,
            sha256_hex=sha256_hex,
            file_size_bytes=file_size_bytes,
            status=PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE,
            attempt_count=0,
            materialized_meme_id=matched_file.meme_id,
            materialized_meme_file_id=matched_file.id,
            matched_meme_file_id=matched_file.id,
            source_attach_reason=attach_reason,
        )
        forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(source_metadata)
        source_row = MemeSource(
            file_id=matched_file.id,
            platform=source.source_platform,
            source_id=source.source_id,
            post_id=source.post_id,
            views=source.views,
            reactions=source_reactions(source_metadata),
            is_first_source=False,
            source_alive=True,
            published_at=source_published_at(source_metadata),
            forwarded_from_source_id=forwarded_from_source_id,
            forwarded_from_post_id=forwarded_from_post_id,
            attach_reason=attach_reason,
            matched_meme_file_id=matched_file.id,
        )

        try:
            self._session.add_all([ingest_request, source_row])
            if attach_reason is SourceAttachReason.SHA256_EXACT_EXISTING_FILE:
                await save_meme_to_target_collection(
                    self._session,
                    owner_user_id=source.owner_user_id,
                    target_collection_id=target_collection_id,
                    meme_id=matched_file.meme_id,
                )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            replay_result = await self._recover_source_replay(source)
            if replay_result is not None:
                return replay_result
            if self._is_source_identity_error(exc):
                raise PipelineSourceConflictError(
                    "Source platform/source_id/post_id is already attached to durable pipeline state.",
                ) from exc
            raise PipelineIngestError("Failed to persist SHA256 duplicate ingest request.") from exc
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist SHA256 duplicate ingest request.") from exc

        return self._result(ingest_request, IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE)

    async def _accept_new_bytes(
        self,
        *,
        source: IngestAcceptSource,
        filename: str,
        content_type: str,
        media_bytes: bytes,
        sha256_hex: str,
        file_size_bytes: int,
        user_metadata: dict[str, object],
        source_metadata: dict[str, object],
    ) -> IngestAcceptResult:
        ingest_request_id = uuid.uuid7()
        temp_object_key = build_temp_original_object_key(
            ingest_request_id,
            filename,
            settings=self._settings,
        )
        await self._put_temp_object(
            key=temp_object_key,
            body=media_bytes,
            content_type=content_type,
        )

        ingest_request = PipelineIngestRequest(
            id=ingest_request_id,
            source_platform=source.source_platform,
            source_id=source.source_id,
            post_id=source.post_id,
            owner_user_id=source.owner_user_id,
            user_metadata=user_metadata,
            source_metadata=source_metadata,
            declared_filename=filename,
            declared_content_type=content_type,
            temp_original_object_key=temp_object_key,
            sha256_hex=sha256_hex,
            file_size_bytes=file_size_bytes,
            status=PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
            attempt_count=0,
        )
        try:
            self._session.add(ingest_request)
            outbox_message_id = await self._rabbit_publisher.publish(
                build_media_inspect_message_spec(ingest_request, settings=self._settings),
                session=self._session,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            await self._cleanup_temp_object(temp_object_key)
            replay_result = await self._recover_source_replay(source)
            if replay_result is not None:
                return replay_result
            if self._is_source_identity_error(exc):
                raise PipelineSourceConflictError(
                    "Source platform/source_id/post_id is already attached to durable pipeline state.",
                ) from exc
            raise PipelineIngestError("Failed to persist raw ingest request and outbox event.") from exc
        except SQLAlchemyError as exc:
            await self._session.rollback()
            await self._cleanup_temp_object(temp_object_key)
            raise PipelineIngestError("Failed to persist raw ingest request and outbox event.") from exc
        except Exception as exc:
            await self._session.rollback()
            await self._cleanup_temp_object(temp_object_key)
            raise PipelineIngestError("Failed to persist raw ingest request and outbox event.") from exc

        _ = await relay_rabbitmq_outbox_messages_best_effort(
            self._session,
            (outbox_message_id,),
            settings=self._settings,
            broker=self._broker,
            logger=logger,
        )
        return self._result(ingest_request, IngestAcceptOutcome.ACCEPTED_ASYNC)

    async def _find_existing_request(self, source: IngestAcceptSource) -> PipelineIngestRequest | None:
        result = await self._session.execute(
            select(PipelineIngestRequest)
            .where(
                PipelineIngestRequest.source_platform == source.source_platform,
                PipelineIngestRequest.source_id == source.source_id,
                PipelineIngestRequest.post_id == source.post_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _recover_source_replay(self, source: IngestAcceptSource) -> IngestAcceptResult | None:
        existing_request = await self._find_existing_request(source)
        if existing_request is None:
            return None
        return self._result(existing_request, IngestAcceptOutcome.SOURCE_REPLAY)

    async def _find_sha256_match(self, sha256_hex: str, *, owner_user_id: uuid.UUID | None) -> MemeFile | None:
        result = await self._session.execute(
            select(MemeFile)
            .join(Meme, Meme.id == MemeFile.meme_id)
            .where(MemeFile.sha256_hex == sha256_hex)
            .where(visible_meme_clause(owner_user_id))
            .order_by(Meme.is_public.desc(), MemeFile.created_at.asc(), MemeFile.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _parse_target_collection_id(user_metadata: dict[str, object]) -> uuid.UUID | None:
        try:
            return parse_target_collection_id(user_metadata)
        except TargetCollectionMetadataError as exc:
            raise PipelinePayloadValidationError(str(exc)) from exc

    async def _put_temp_object(self, *, key: str, body: bytes, content_type: str) -> None:
        try:
            await upload_object_bytes(
                self._storage_client,
                bucket=self._storage_settings.bucket,
                key=key,
                body=body,
                content_type=content_type,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to store the raw original in temporary S3-compatible storage.") from exc

    async def _cleanup_temp_object(self, key: str) -> None:
        await delete_object_if_present(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=key,
        )

    def _validate_declared_upload_size(self, *, content_type: str, file_size_bytes: int) -> None:
        if file_size_bytes <= 0:
            raise PipelinePayloadValidationError("Uploaded file must not be empty.")

        upload_limit = self._upload_limit_for_content_type(content_type)
        if file_size_bytes > upload_limit:
            raise PipelinePayloadTooLargeError(f"Uploaded file exceeds the {upload_limit}-byte limit.")

    def _upload_limit_for_content_type(self, content_type: str) -> int:
        if content_type == "image/gif":
            return self._settings.pipeline_gif_upload_max_bytes
        if content_type.startswith("video/"):
            return self._settings.pipeline_video_upload_max_bytes
        if content_type.startswith("image/"):
            return self._settings.pipeline_image_upload_max_bytes
        return max(
            self._settings.pipeline_image_upload_max_bytes,
            self._settings.pipeline_gif_upload_max_bytes,
            self._settings.pipeline_video_upload_max_bytes,
        )

    def _normalize_content_type(self, content_type: str | None) -> str:
        if content_type is None:
            raise PipelineUnsupportedMediaTypeError("Uploaded file must include a media type.")

        normalized_content_type = content_type.strip().lower().split(";", maxsplit=1)[0].strip()
        if not normalized_content_type:
            raise PipelineUnsupportedMediaTypeError("Uploaded file must include a media type.")
        if len(normalized_content_type) > MAX_TELEGRAM_CONTENT_TYPE_LENGTH:
            raise PipelineUnsupportedMediaTypeError("Uploaded media type is too long.")
        if normalized_content_type not in self._settings.pipeline_allowed_mime_types:
            raise PipelineUnsupportedMediaTypeError("Uploaded media type is not supported.")
        return normalized_content_type

    @staticmethod
    def _normalize_filename(filename: str | None) -> str:
        if filename is None:
            raise PipelinePayloadValidationError("Uploaded file must include a filename.")

        normalized_filename = PurePosixPath(filename).name.strip()
        if not normalized_filename:
            raise PipelinePayloadValidationError("Uploaded file must include a filename.")
        if len(normalized_filename) > MAX_TELEGRAM_FILENAME_LENGTH:
            raise PipelinePayloadValidationError("Uploaded filename is too long.")
        return normalized_filename

    @staticmethod
    def _normalize_metadata(value: dict[str, object], *, field_name: str) -> dict[str, object]:
        try:
            return cast("dict[str, object]", json.loads(json.dumps(value)))
        except (TypeError, ValueError) as exc:
            raise PipelinePayloadValidationError(f"{field_name} must be JSON-serializable.") from exc

    @staticmethod
    def _sha_match_attach_reason(matched_file: MemeFile) -> SourceAttachReason:
        if (
            matched_file.status is ContentProcessingStatus.FAILED
            and matched_file.blocked_perceptual_hash_id is not None
        ):
            return SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE
        return SourceAttachReason.SHA256_EXACT_EXISTING_FILE

    @staticmethod
    def _is_source_identity_error(exc: IntegrityError) -> bool:
        constraint_names = {
            "uq_pipeline_ingest_requests_source_identity",
            "uq_meme_sources_platform_source_post",
        }
        candidates = (
            exc,
            exc.orig,
            getattr(exc.orig, "__cause__", None),
            getattr(exc.orig, "__context__", None),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            if getattr(candidate, "constraint_name", None) in constraint_names:
                return True
            diag = getattr(candidate, "diag", None)
            if getattr(diag, "constraint_name", None) in constraint_names:
                return True
            if any(name in str(candidate) for name in constraint_names):
                return True
        return False

    @staticmethod
    def _result(ingest_request: PipelineIngestRequest, outcome: IngestAcceptOutcome) -> IngestAcceptResult:
        return IngestAcceptResult(
            ingest_request=IngestRequestRead.model_validate(ingest_request),
            outcome=outcome,
        )


__all__ = ["ObjectStorageClient", "PipelineIngestAcceptService"]
