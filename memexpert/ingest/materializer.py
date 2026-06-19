"""Worker-only raw ingest-request materializer facade.

This module is intentionally not re-exported from ``memexpert.ingest`` so API
startup keeps importing only the API-safe accept/read services. Worker runtime
code injects the heavy media processor that performs Pillow/ImageHash/FFmpeg
inspection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.config import get_settings
from memexpert.ingest.materialization.blocked import (
    find_blocked_perceptual_hash_match,
    materialize_blocked_request,
)
from memexpert.ingest.materialization.invalid import mark_invalid_media
from memexpert.ingest.materialization.media import MaterializationMediaPreparer
from memexpert.ingest.materialization.models import (
    FAILED_BLOCKED_PHASH_CODE,
    FAILED_INVALID_MEDIA_CODE,
    FAILED_MEDIA_TOO_LARGE_CODE,
    ObjectStorageClient,
    PipelineIngestMaterializationResult,
    build_materialization_result,
)
from memexpert.ingest.materialization.normal import materialize_transcodable_request
from memexpert.ingest.materialization.objects import MaterializationObjectStore
from memexpert.ingest.materialization.requests import (
    is_materialization_eligible,
    load_locked_ingest_request,
    mark_materialization_attempt_started,
)
from memexpert.media.contracts import MediaProcessingError
from memexpert.messaging.rabbitmq_outbox import relay_rabbitmq_outbox_messages_best_effort
from memexpert.models.base import utcnow
from memexpert.services.errors import PipelineIngestError, PipelinePayloadTooLargeError

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.media.contracts import PipelineMediaProcessorProtocol
    from memexpert.messaging.rabbitmq_outbox import RabbitBrokerProtocol

logger = logging.getLogger(__name__)


class PipelineIngestMaterializer:
    """Materialize raw ingest requests into durable content-pipeline rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        media_processor: PipelineMediaProcessorProtocol,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        broker: RabbitBrokerProtocol | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._broker = broker
        self._objects = MaterializationObjectStore(settings=self._settings, storage_client=storage_client)
        self._media_preparer = MaterializationMediaPreparer(
            settings=self._settings,
            media_processor=media_processor,
        )

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        media_processor: PipelineMediaProcessorProtocol,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        broker: RabbitBrokerProtocol | None = None,
    ) -> Self:
        """Build the worker-only materializer from shared settings."""

        return cls(
            session,
            media_processor=media_processor,
            settings=settings,
            storage_client=storage_client,
            broker=broker,
        )

    async def materialize(self, ingest_request_id: uuid.UUID) -> PipelineIngestMaterializationResult:
        """Inspect raw temp bytes and persist post-materialization state atomically."""

        ingest_request = await load_locked_ingest_request(self._session, ingest_request_id)
        if not is_materialization_eligible(ingest_request):
            return build_materialization_result(ingest_request)

        now = utcnow()
        mark_materialization_attempt_started(ingest_request, started_at=now)

        temp_object_key = ingest_request.temp_original_object_key
        if temp_object_key is None:
            await mark_invalid_media(
                self._session,
                ingest_request,
                code=FAILED_INVALID_MEDIA_CODE,
                detail="Raw ingest request is missing its temporary original object key.",
            )
            return build_materialization_result(ingest_request)

        original_bytes = await self._objects.download_temp_original(temp_object_key)
        try:
            prepared = await self._media_preparer.prepare(
                ingest_request=ingest_request,
                media_bytes=original_bytes,
            )
        except MediaProcessingError as exc:
            await mark_invalid_media(
                self._session,
                ingest_request,
                code=FAILED_INVALID_MEDIA_CODE,
                detail=str(exc),
            )
            return build_materialization_result(ingest_request)
        except PipelinePayloadTooLargeError as exc:
            await mark_invalid_media(
                self._session,
                ingest_request,
                code=FAILED_MEDIA_TOO_LARGE_CODE,
                detail=str(exc),
            )
            return build_materialization_result(ingest_request)

        blocked_match = await find_blocked_perceptual_hash_match(self._session, prepared.perceptual_hash)
        await self._objects.put_canonical_original(prepared=prepared, media_bytes=original_bytes)
        try:
            if blocked_match is not None:
                await materialize_blocked_request(
                    self._session,
                    ingest_request=ingest_request,
                    prepared=prepared,
                    blocked_match=blocked_match,
                    created_at=now,
                )
                outbox_message_id = None
            else:
                outbox_message_id = await materialize_transcodable_request(
                    self._session,
                    ingest_request=ingest_request,
                    prepared=prepared,
                    created_at=now,
                    settings=self._settings,
                )
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            await self._objects.cleanup_canonical_original(prepared.object_key)
            raise PipelineIngestError("Failed to persist materialized ingest request state.") from exc
        except Exception:
            await self._session.rollback()
            await self._objects.cleanup_canonical_original(prepared.object_key)
            raise

        await self._objects.cleanup_temp_original(temp_object_key)
        if outbox_message_id is not None:
            _ = await relay_rabbitmq_outbox_messages_best_effort(
                self._session,
                (outbox_message_id,),
                settings=self._settings,
                broker=self._broker,
                logger=logger,
            )
        return build_materialization_result(ingest_request, outbox_message_id=outbox_message_id)


__all__ = [
    "FAILED_BLOCKED_PHASH_CODE",
    "FAILED_INVALID_MEDIA_CODE",
    "FAILED_MEDIA_TOO_LARGE_CODE",
    "ObjectStorageClient",
    "PipelineIngestMaterializationResult",
    "PipelineIngestMaterializer",
]
