# ruff: noqa: TC001,TC002,TC003
"""FastStream RabbitMQ runtime for the real transcode, OCR, embed, classify, and sync stages."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue
from pydantic import ValidationError

from memexpert.core.broker import PipelineBrokerSettings
from memexpert.core.classification import ClassificationClientProtocol
from memexpert.core.config import Settings
from memexpert.core.database import AsyncSessionFactory
from memexpert.core.meilisearch import (
    MeilisearchSyncClientProtocol,
    PipelineMeilisearchDocument,
)
from memexpert.core.ocr import OCRProcessingError, OCRProcessorProtocol
from memexpert.core.qdrant import (
    QdrantSimilarityClientProtocol,
    QdrantSyncClientProtocol,
    QdrantSyncPayload,
)
from memexpert.core.storage import (
    delete_object_if_present,
    download_object_bytes,
    get_pipeline_storage_settings,
    upload_object_bytes,
)
from memexpert.core.voyage import VoyageClientProtocol
from memexpert.ingest.materializer import PipelineIngestMaterializer
from memexpert.media.contracts import MediaValidationError
from memexpert.models.enums import ContentPipelineStage
from memexpert.pipeline.events import MediaInspectRequestedEvent
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import (
    ContentPipelineService,
    PipelineIngestError,
    PipelinePublishError,
)
from memexpert.services.search_index_sync import (
    build_meilisearch_document,
    build_qdrant_sync_payload,
    load_search_index_state,
)
from memexpert.workers.pipeline_runtime.constants import (
    PIPELINE_REASON_MALFORMED_EVENT,
    PIPELINE_REASON_MEDIA_INSPECT_FAILED,
    PIPELINE_REASON_UNSUPPORTED_STAGE,
)
from memexpert.workers.pipeline_runtime.errors import (
    ForcedClassifyFailure,
    ForcedEmbedFailure,
    ForcedSyncMeiliFailure,
    ForcedSyncQdrantFailure,
    ForcedTranscodeFailure,
    coerce_dead_letter_payload,
    extract_event_reference,
    is_replayable_failure,
    normalize_failure_reason,
    render_error_text,
    validate_event_payload,
)
from memexpert.workers.pipeline_runtime.stage_registry import get_stage_handler

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.classification import ClassificationResult
    from memexpert.core.ocr import OCRExtractionResult
    from memexpert.core.qdrant import QdrantSimilarityMatch
    from memexpert.core.voyage import VoyageEmbeddingResult
    from memexpert.media.contracts import NormalizedMediaResult, PipelineMediaProcessorProtocol
    from memexpert.services.content_merge import MergeOutcome
    from memexpert.services.content_pipeline import PipelineStageWorkContext


class RabbitMessageLike(Protocol):
    """Minimal RabbitMQ message surface used by the runtime handler and tests."""

    headers: dict[str, Any]
    content_type: str | None
    message_id: str | None

    async def ack(self, multiple: bool = False) -> None: ...

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None: ...

    async def reject(self, requeue: bool = False) -> None: ...


class ObjectStorageClientLike(Protocol):
    """Small S3-compatible surface used by the runtime."""

    def get_object(self, *, Bucket: str, Key: str) -> object: ...

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


@dataclass(slots=True)
class SyncQdrantInputs:
    """Compact bundle of the canonical state the sync_qdrant stage needs per attempt."""

    payload: QdrantSyncPayload
    vector: tuple[float, ...]


@dataclass(slots=True)
class SyncMeiliInputs:
    """Compact bundle of the canonical state the sync_meili stage needs per attempt."""

    document: PipelineMeilisearchDocument


@dataclass(slots=True)
class PipelineRuntime:
    """RabbitMQ-backed runtime that consumes the real heavy-worker pipeline stages."""

    settings: Settings
    broker: RabbitBroker
    session_factory: AsyncSessionFactory
    broker_settings: PipelineBrokerSettings
    pipeline_exchange: RabbitExchange
    retry_exchange: RabbitExchange
    dead_letter_exchange: RabbitExchange
    media_inspect_queue: RabbitQueue
    transcode_queue: RabbitQueue
    ocr_queue: RabbitQueue
    embed_queue: RabbitQueue
    classify_queue: RabbitQueue
    sync_qdrant_queue: RabbitQueue
    sync_meili_queue: RabbitQueue
    media_inspect_retry_queue: RabbitQueue
    transcode_retry_queue: RabbitQueue
    ocr_retry_queue: RabbitQueue
    embed_retry_queue: RabbitQueue
    classify_retry_queue: RabbitQueue
    sync_qdrant_retry_queue: RabbitQueue
    sync_meili_retry_queue: RabbitQueue
    dead_letter_queue: RabbitQueue
    storage_client: ObjectStorageClientLike
    media_processor: PipelineMediaProcessorProtocol
    ocr_processor: OCRProcessorProtocol
    voyage_client: VoyageClientProtocol
    qdrant_client: QdrantSimilarityClientProtocol
    qdrant_sync_client: QdrantSyncClientProtocol
    meilisearch_sync_client: MeilisearchSyncClientProtocol
    classification_client: ClassificationClientProtocol

    async def declare_topology(self) -> None:
        """Declare the heavy-worker queues, retry queues, and DLQ topology explicitly."""

        exchange = await self.broker.declare_exchange(self.pipeline_exchange)
        retry_exchange = await self.broker.declare_exchange(self.retry_exchange)
        dead_letter_exchange = await self.broker.declare_exchange(self.dead_letter_exchange)
        media_inspect_queue = await self.broker.declare_queue(self.media_inspect_queue)
        transcode_queue = await self.broker.declare_queue(self.transcode_queue)
        ocr_queue = await self.broker.declare_queue(self.ocr_queue)
        embed_queue = await self.broker.declare_queue(self.embed_queue)
        classify_queue = await self.broker.declare_queue(self.classify_queue)
        sync_qdrant_queue = await self.broker.declare_queue(self.sync_qdrant_queue)
        sync_meili_queue = await self.broker.declare_queue(self.sync_meili_queue)
        media_inspect_retry_queue = await self.broker.declare_queue(self.media_inspect_retry_queue)
        transcode_retry_queue = await self.broker.declare_queue(self.transcode_retry_queue)
        ocr_retry_queue = await self.broker.declare_queue(self.ocr_retry_queue)
        embed_retry_queue = await self.broker.declare_queue(self.embed_retry_queue)
        classify_retry_queue = await self.broker.declare_queue(self.classify_retry_queue)
        sync_qdrant_retry_queue = await self.broker.declare_queue(self.sync_qdrant_retry_queue)
        sync_meili_retry_queue = await self.broker.declare_queue(self.sync_meili_retry_queue)
        dead_letter_queue = await self.broker.declare_queue(self.dead_letter_queue)

        embed_retry_return_routing_key = self.broker_settings.retry_return_routing_key_for_stage(
            ContentPipelineStage.EMBED,
        )
        embed_retry_request_routing_key = self.broker_settings.retry_queue_routing_key_for_stage(
            ContentPipelineStage.EMBED,
        )
        classify_retry_return_routing_key = self.broker_settings.retry_return_routing_key_for_stage(
            ContentPipelineStage.CLASSIFY,
        )
        classify_retry_request_routing_key = self.broker_settings.retry_queue_routing_key_for_stage(
            ContentPipelineStage.CLASSIFY,
        )
        sync_qdrant_retry_return_routing_key = self.broker_settings.retry_return_routing_key_for_stage(
            ContentPipelineStage.SYNC_QDRANT,
        )
        sync_qdrant_retry_request_routing_key = self.broker_settings.retry_queue_routing_key_for_stage(
            ContentPipelineStage.SYNC_QDRANT,
        )
        sync_meili_retry_return_routing_key = self.broker_settings.retry_return_routing_key_for_stage(
            ContentPipelineStage.SYNC_MEILI,
        )
        sync_meili_retry_request_routing_key = self.broker_settings.retry_queue_routing_key_for_stage(
            ContentPipelineStage.SYNC_MEILI,
        )

        _ = await media_inspect_queue.bind(exchange, routing_key=self.broker_settings.media_inspect_routing_key)
        _ = await media_inspect_queue.bind(exchange, routing_key=self.broker_settings.media_inspect_retry_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.meme_created_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.stage_replay_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.transcode_retry_routing_key)
        _ = await ocr_queue.bind(exchange, routing_key=self.broker_settings.ocr_routing_key)
        _ = await ocr_queue.bind(exchange, routing_key=self.broker_settings.ocr_retry_routing_key)
        _ = await embed_queue.bind(exchange, routing_key=self.broker_settings.embed_routing_key)
        _ = await embed_queue.bind(exchange, routing_key=embed_retry_return_routing_key)
        _ = await classify_queue.bind(exchange, routing_key=self.broker_settings.classify_routing_key)
        _ = await classify_queue.bind(exchange, routing_key=classify_retry_return_routing_key)
        _ = await sync_qdrant_queue.bind(exchange, routing_key=self.broker_settings.sync_qdrant_routing_key)
        _ = await sync_qdrant_queue.bind(exchange, routing_key=sync_qdrant_retry_return_routing_key)
        _ = await sync_meili_queue.bind(exchange, routing_key=self.broker_settings.sync_meili_routing_key)
        _ = await sync_meili_queue.bind(exchange, routing_key=sync_meili_retry_return_routing_key)
        _ = await media_inspect_retry_queue.bind(
            retry_exchange,
            routing_key=self.broker_settings.media_inspect_retry_request_routing_key,
        )
        _ = await transcode_retry_queue.bind(retry_exchange, routing_key=self.broker_settings.retry_routing_key)
        _ = await ocr_retry_queue.bind(retry_exchange, routing_key=self.broker_settings.ocr_retry_request_routing_key)
        _ = await embed_retry_queue.bind(retry_exchange, routing_key=embed_retry_request_routing_key)
        _ = await classify_retry_queue.bind(retry_exchange, routing_key=classify_retry_request_routing_key)
        _ = await sync_qdrant_retry_queue.bind(retry_exchange, routing_key=sync_qdrant_retry_request_routing_key)
        _ = await sync_meili_retry_queue.bind(retry_exchange, routing_key=sync_meili_retry_request_routing_key)
        _ = await dead_letter_queue.bind(
            dead_letter_exchange,
            routing_key=self.broker_settings.dead_letter_routing_key,
        )

    async def handle_media_inspect_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one raw-ingest media-inspect request and materialize content state."""

        try:
            inspect_event = MediaInspectRequestedEvent.model_validate(payload)
        except ValidationError:
            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(payload),
                message=message,
                normalized_reason=PIPELINE_REASON_MALFORMED_EVENT,
            )
            return

        effective_attempt = self._media_inspect_effective_attempt(message)
        try:
            async with self.session_factory() as session:
                materializer = PipelineIngestMaterializer(
                    session,
                    settings=self.settings,
                    storage_client=self.storage_client,
                    media_processor=self.media_processor,
                )
                _ = await materializer.materialize(inspect_event.ingest_request_id)
        except Exception:
            if effective_attempt < self.broker_settings.retry_max_attempts:
                await message.reject(requeue=False)
                return

            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(inspect_event.model_dump(mode="json")),
                message=message,
                normalized_reason=PIPELINE_REASON_MEDIA_INSPECT_FAILED,
            )
            return

        await message.ack()

    async def handle_transcode_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one transcode-stage dispatch, persisting durable stage truth as it changes."""

        await self._handle_stage_message(
            payload=payload,
            message=message,
            expected_stage=ContentPipelineStage.TRANSCODE,
        )

    async def handle_ocr_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one OCR-stage dispatch, persisting durable stage truth as it changes."""

        await self._handle_stage_message(
            payload=payload,
            message=message,
            expected_stage=ContentPipelineStage.OCR,
        )

    async def handle_embed_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one embed-stage dispatch, persisting durable stage truth as it changes."""

        await self._handle_stage_message(
            payload=payload,
            message=message,
            expected_stage=ContentPipelineStage.EMBED,
        )

    async def handle_classify_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one classify-stage dispatch, persisting durable stage truth as it changes."""

        await self._handle_stage_message(
            payload=payload,
            message=message,
            expected_stage=ContentPipelineStage.CLASSIFY,
        )

    async def handle_sync_qdrant_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one sync_qdrant-stage dispatch, persisting per-target sync truth."""

        await self._handle_stage_message(
            payload=payload,
            message=message,
            expected_stage=ContentPipelineStage.SYNC_QDRANT,
        )

    async def handle_sync_meili_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one sync_meili-stage dispatch, persisting per-target sync truth."""

        await self._handle_stage_message(
            payload=payload,
            message=message,
            expected_stage=ContentPipelineStage.SYNC_MEILI,
        )

    async def _handle_stage_message(
        self,
        *,
        payload: object,
        message: RabbitMessageLike,
        expected_stage: ContentPipelineStage,
    ) -> None:
        dispatch_event = validate_event_payload(payload)
        if dispatch_event is None:
            await self._record_malformed_event_failure(payload)
            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(payload),
                message=message,
                normalized_reason=PIPELINE_REASON_MALFORMED_EVENT,
            )
            return

        effective_attempt = self._effective_attempt(dispatch_event, message)
        if dispatch_event.stage is not expected_stage:
            await self._record_terminal_failure(
                dispatch_event,
                attempt=effective_attempt,
                normalized_reason=PIPELINE_REASON_UNSUPPORTED_STAGE,
                last_error_text=(
                    f"The runtime handler for {expected_stage.value!r} received "
                    f"{dispatch_event.stage.value!r}."
                ),
                retryable=False,
            )
            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
                message=message,
                normalized_reason=PIPELINE_REASON_UNSUPPORTED_STAGE,
            )
            return

        try:
            stage_context = await self._start_stage_processing(
                meme_file_id=dispatch_event.meme_file_id,
                stage=dispatch_event.stage,
                attempt=effective_attempt,
                event_id=dispatch_event.event_id,
            )
            await self._run_stage_for(
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=effective_attempt,
            )
        except Exception as exc:
            normalized_reason = normalize_failure_reason(expected_stage, exc)
            retryable = is_replayable_failure(expected_stage, exc)
            await self._mark_stage_failed(
                meme_file_id=dispatch_event.meme_file_id,
                stage=dispatch_event.stage,
                attempt=effective_attempt,
                event_id=dispatch_event.event_id,
                normalized_reason=normalized_reason,
                last_error_text=render_error_text(exc),
                retryable=retryable,
            )

            should_queue_retry = retryable and effective_attempt < self.broker_settings.retry_max_attempts
            if should_queue_retry:
                await message.reject(requeue=False)
                return

            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
                message=message,
                normalized_reason=normalized_reason,
            )
            return

        await message.ack()

    async def _run_stage_for(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        stage_handler = get_stage_handler(dispatch_event.stage)
        if stage_handler is not None:
            await stage_handler.run(
                self,
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=attempt,
            )
            return

        raise PipelineIngestError(
            f"Pipeline runtime cannot execute work for stage {dispatch_event.stage.value!r}.",
        )

    async def _run_transcode_stage(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        resolved_context = stage_context
        if resolved_context.mime_type is None:
            raise MediaValidationError("Pipeline item is missing the original media type required for transcode.")

        storage_settings = get_pipeline_storage_settings(self.settings)
        original_bytes = await download_object_bytes(
            self.storage_client,
            bucket=storage_settings.bucket,
            key=resolved_context.original_object_key,
        )
        normalized = await self.media_processor.normalize_for_web(
            meme_file_id=dispatch_event.meme_file_id,
            filename=PurePosixPath(resolved_context.original_object_key).name,
            content_type=resolved_context.mime_type,
            media_bytes=original_bytes,
        )
        await upload_object_bytes(
            self.storage_client,
            bucket=storage_settings.bucket,
            key=normalized.web_video_object_key,
            body=normalized.web_video_bytes,
            content_type=normalized.mime_type,
        )
        try:
            await self._complete_transcode_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                normalized=normalized,
            )
        except Exception:
            await delete_object_if_present(
                self.storage_client,
                bucket=storage_settings.bucket,
                key=normalized.web_video_object_key,
            )
            raise

    async def _run_ocr_stage(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        resolved_context = stage_context
        source_object_key = resolved_context.web_video_object_key or resolved_context.original_object_key
        source_mime_type = resolved_context.mime_type
        if source_mime_type is None:
            raise OCRProcessingError("Pipeline item is missing the media type required for OCR.")

        storage_settings = get_pipeline_storage_settings(self.settings)
        source_bytes = await download_object_bytes(
            self.storage_client,
            bucket=storage_settings.bucket,
            key=source_object_key,
        )
        ocr_result = await self.ocr_processor.extract_text(
            filename=PurePosixPath(source_object_key).name,
            mime_type=source_mime_type,
            media_bytes=source_bytes,
            source_object_key=source_object_key,
        )
        await self._complete_ocr_stage(
            meme_file_id=dispatch_event.meme_file_id,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            ocr_result=ocr_result,
        )

    async def _run_embed_stage(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        preview_frame_bytes = await self._load_preview_frame(stage_context)
        embedding_result = await self.voyage_client.embed_image(
            image_bytes=preview_frame_bytes,
            mime_type="image/png",
        )
        similarity_matches = await self.qdrant_client.find_similar_memes(
            vector=embedding_result.vector,
            current_meme_file_id=dispatch_event.meme_file_id,
        )
        _ = await self._complete_embed_stage(
            meme_file_id=dispatch_event.meme_file_id,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            embedding_result=embedding_result,
            similarity_matches=similarity_matches,
        )

    async def _run_sync_qdrant_stage(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        """Load canonical state, upsert to Qdrant, and record per-target sync truth.

        Failures from the adapter bubble up so ``_handle_stage_message`` can
        run the shared normalize-and-classify path; before re-raising, we
        always call :meth:`ContentPipelineService.fail_sync_qdrant_stage` so
        the per-target snapshot row stays truthful even when the stage-journal
        path is about to dead-letter.
        """

        _ = stage_context
        try:
            sync_inputs = await self._load_sync_qdrant_inputs(dispatch_event.meme_file_id)
        except Exception:
            await self._record_sync_qdrant_failure(
                dispatch_event=dispatch_event,
                attempt=attempt,
                exc=PipelineIngestError(
                    f"Canonical state for {dispatch_event.meme_file_id} "
                    f"is missing or unreadable for sync_qdrant.",
                ),
            )
            raise

        try:
            await self.qdrant_sync_client.upsert_meme_point(
                payload=sync_inputs.payload,
                vector=sync_inputs.vector,
            )
        except Exception as exc:
            await self._record_sync_qdrant_failure(
                dispatch_event=dispatch_event,
                attempt=attempt,
                exc=exc,
            )
            raise

        # Best-effort preview refresh — if the post-upsert read fails we log
        # and proceed: the sync itself already succeeded and the snapshot row
        # must reflect that. Callers see ``last_preview=None`` until the next
        # successful sync.
        preview_payload: dict[str, object] = {}
        try:
            fetched_preview = await self.qdrant_sync_client.fetch_meme_point(dispatch_event.meme_file_id)
        except Exception as exc:  # noqa: BLE001 - best-effort, any failure degrades to empty preview.
            import logging

            logging.getLogger(__name__).warning(
                "qdrant sync preview fetch failed for %s: %s",
                dispatch_event.meme_file_id,
                exc,
            )
            fetched_preview = None

        if fetched_preview is not None:
            preview_payload = dict(fetched_preview.preview_fields)

        try:
            await self._complete_sync_qdrant_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                payload_preview=preview_payload,
            )
        except PipelinePublishError:
            # Publish failure of the MEME_QDRANT_SYNCED notification is
            # classified and re-raised so the standard dispatcher records a
            # publish_failed stage reason and keeps the stage replayable.
            raise
        except Exception as exc:
            await self._record_sync_qdrant_failure(
                dispatch_event=dispatch_event,
                attempt=attempt,
                exc=exc,
            )
            raise

    async def _run_sync_meili_stage(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        """Load canonical state, upsert to Meilisearch, and record per-target sync truth.

        Mirrors :meth:`_run_sync_qdrant_stage` exactly: every failure branch
        calls :meth:`ContentPipelineService.fail_sync_meili_stage` so the
        per-target snapshot row is truthful before the dispatcher runs the
        shared normalize-and-classify path. The post-upsert preview fetch is
        best-effort — a fetch failure must never fail the stage because the
        upsert already succeeded.
        """

        _ = stage_context
        try:
            sync_inputs = await self._load_sync_meili_inputs(dispatch_event.meme_file_id)
        except Exception:
            await self._record_sync_meili_failure(
                dispatch_event=dispatch_event,
                attempt=attempt,
                exc=PipelineIngestError(
                    f"Canonical state for {dispatch_event.meme_file_id} "
                    f"is missing or unreadable for sync_meili.",
                ),
            )
            raise

        try:
            await self.meilisearch_sync_client.upsert_document(sync_inputs.document)
        except Exception as exc:
            await self._record_sync_meili_failure(
                dispatch_event=dispatch_event,
                attempt=attempt,
                exc=exc,
            )
            raise

        # Best-effort preview refresh — same contract as the Qdrant path: a
        # failed retrieve must not fail the stage because the upsert already
        # landed. Callers see ``last_preview=None`` until the next successful
        # sync attempt refreshes the snapshot.
        preview_payload: dict[str, object] = {}
        try:
            fetched_preview = await self.meilisearch_sync_client.fetch_document(
                dispatch_event.meme_file_id,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, any failure degrades to empty preview.
            import logging

            logging.getLogger(__name__).warning(
                "meilisearch sync preview fetch failed for %s: %s",
                dispatch_event.meme_file_id,
                exc,
            )
            fetched_preview = None

        if fetched_preview is not None:
            preview_payload = dict(fetched_preview.preview_fields)

        try:
            await self._complete_sync_meili_stage(
                meme_file_id=dispatch_event.meme_file_id,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                payload_preview=preview_payload,
            )
        except PipelinePublishError:
            raise
        except Exception as exc:
            await self._record_sync_meili_failure(
                dispatch_event=dispatch_event,
                attempt=attempt,
                exc=exc,
            )
            raise

    async def _run_classify_stage(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        stage_context: PipelineStageWorkContext,
        attempt: int,
    ) -> None:
        preview_frame_bytes = await self._load_preview_frame(stage_context)
        classification_result = await self.classification_client.classify_image(
            image_bytes=preview_frame_bytes,
            mime_type="image/png",
        )
        await self._complete_classify_stage(
            meme_file_id=dispatch_event.meme_file_id,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            classification_result=classification_result,
        )

    async def _load_preview_frame(self, stage_context: PipelineStageWorkContext) -> bytes:
        source_object_key = stage_context.web_video_object_key or stage_context.original_object_key
        source_mime_type = stage_context.mime_type
        if source_mime_type is None:
            raise PipelineIngestError("Pipeline item is missing the media type required for embed/classify work.")

        storage_settings = get_pipeline_storage_settings(self.settings)
        source_bytes = await download_object_bytes(
            self.storage_client,
            bucket=storage_settings.bucket,
            key=source_object_key,
        )
        return await self.media_processor.extract_preview_frame(
            filename=PurePosixPath(source_object_key).name,
            content_type=source_mime_type,
            media_bytes=source_bytes,
        )

    async def _start_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> PipelineStageWorkContext:
        async with self.session_factory() as session:
            service = self._build_service(session)
            return await service.start_stage_processing(
                meme_file_id=meme_file_id,
                stage=stage,
                attempt=attempt,
                event_id=event_id,
            )

    async def _complete_transcode_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized: NormalizedMediaResult,
    ) -> None:
        async with self.session_factory() as session:
            service = self._build_service(session)
            await service.complete_transcode_stage(
                meme_file_id=meme_file_id,
                attempt=attempt,
                event_id=event_id,
                result=normalized,
            )

    async def _complete_ocr_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        ocr_result: OCRExtractionResult,
    ) -> None:
        async with self.session_factory() as session:
            service = self._build_service(session)
            await service.complete_ocr_stage(
                meme_file_id=meme_file_id,
                attempt=attempt,
                event_id=event_id,
                result=ocr_result,
            )

    async def _complete_embed_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        embedding_result: VoyageEmbeddingResult,
        similarity_matches: tuple[QdrantSimilarityMatch, ...],
    ) -> MergeOutcome:
        async with self.session_factory() as session:
            service = self._build_service(session)
            return await service.complete_embed_stage(
                meme_file_id=meme_file_id,
                attempt=attempt,
                event_id=event_id,
                embedding_result=embedding_result,
                similarity_matches=similarity_matches,
            )

    async def _complete_classify_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        classification_result: ClassificationResult,
    ) -> None:
        async with self.session_factory() as session:
            service = self._build_service(session)
            await service.complete_classify_stage(
                meme_file_id=meme_file_id,
                attempt=attempt,
                event_id=event_id,
                classification_result=classification_result,
            )

    async def _complete_sync_qdrant_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: dict[str, object],
    ) -> None:
        async with self.session_factory() as session:
            service = self._build_service(session)
            _ = await service.complete_sync_qdrant_stage(
                meme_file_id=meme_file_id,
                attempt=attempt,
                event_id=event_id,
                payload_preview=payload_preview,
            )

    async def _complete_sync_meili_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: dict[str, object],
    ) -> None:
        async with self.session_factory() as session:
            service = self._build_service(session)
            _ = await service.complete_sync_meili_stage(
                meme_file_id=meme_file_id,
                attempt=attempt,
                event_id=event_id,
                payload_preview=payload_preview,
            )

    async def _load_sync_qdrant_inputs(
        self,
        meme_file_id: uuid.UUID,
    ) -> SyncQdrantInputs:
        """Load canonical meme + embedding vector + primary-file OCR text for sync_qdrant."""

        async with self.session_factory() as session:
            loaded_state = await load_search_index_state(
                session,
                meme_file_id,
                vector_dimensions=self.settings.pipeline_voyage_output_dimensions,
            )
        if loaded_state.vector is None:
            raise PipelineIngestError(
                f"Sync_qdrant consumer could not decode an embedding vector for {meme_file_id}.",
            )
        return SyncQdrantInputs(
            payload=build_qdrant_sync_payload(loaded_state.canonical),
            vector=loaded_state.vector,
        )

    async def _record_sync_qdrant_failure(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        attempt: int,
        exc: Exception,
    ) -> None:
        """Mark the per-target snapshot row as failed before re-raising to the dispatcher.

        The runtime's ``_handle_stage_message`` already records the stage-row
        failure via ``mark_stage_failed``; this helper adds the per-target
        snapshot row so operators see the sync truth alongside the journal.
        """

        normalized_reason = normalize_failure_reason(ContentPipelineStage.SYNC_QDRANT, exc)
        last_error_text = render_error_text(exc)
        try:
            async with self.session_factory() as session:
                service = self._build_service(session)
                _ = await service.fail_sync_qdrant_stage(
                    meme_file_id=dispatch_event.meme_file_id,
                    attempt=attempt,
                    event_id=dispatch_event.event_id,
                    normalized_reason=normalized_reason,
                    last_error_text=last_error_text,
                )
        except Exception:  # noqa: BLE001 - snapshot upsert is best-effort before re-raise.
            return

    async def _load_sync_meili_inputs(
        self,
        meme_file_id: uuid.UUID,
    ) -> SyncMeiliInputs:
        """Load canonical meme + primary-file OCR text for sync_meili.

        The Meilisearch document is derived from the same canonical meme
        truth as the Qdrant payload — every field the reporting + smoke
        route surfaces should be present. We deliberately omit the embedding
        vector because Meilisearch uses its own text index; only the ocr
        text and the tag/language/nsfw metadata travel over.
        """

        async with self.session_factory() as session:
            loaded_state = await load_search_index_state(session, meme_file_id)
        return SyncMeiliInputs(document=build_meilisearch_document(loaded_state.canonical))

    async def _record_sync_meili_failure(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        attempt: int,
        exc: Exception,
    ) -> None:
        """Mark the per-target snapshot row as failed before re-raising to the dispatcher.

        Mirrors :meth:`_record_sync_qdrant_failure` so the two sync paths
        share the same "snapshot row stays truthful" invariant across the
        dispatcher's normalize-and-classify step.
        """

        normalized_reason = normalize_failure_reason(ContentPipelineStage.SYNC_MEILI, exc)
        last_error_text = render_error_text(exc)
        try:
            async with self.session_factory() as session:
                service = self._build_service(session)
                _ = await service.fail_sync_meili_stage(
                    meme_file_id=dispatch_event.meme_file_id,
                    attempt=attempt,
                    event_id=dispatch_event.event_id,
                    normalized_reason=normalized_reason,
                    last_error_text=last_error_text,
                )
        except Exception:  # noqa: BLE001 - snapshot upsert is best-effort before re-raise.
            return

    async def _mark_stage_failed(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
        retryable: bool,
    ) -> None:
        try:
            async with self.session_factory() as session:
                service = self._build_service(session)
                await service.mark_stage_failed(
                    meme_file_id=meme_file_id,
                    stage=stage,
                    attempt=attempt,
                    event_id=event_id,
                    normalized_reason=normalized_reason,
                    last_error_text=last_error_text,
                    retryable=retryable,
                )
        except Exception:
            return

    def _build_service(self, session: AsyncSession) -> ContentPipelineService:
        return ContentPipelineService(
            session,
            settings=self.settings,
        )

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Start the FastStream broker, declare topology, and block until shutdown."""

        resolved_stop_event = stop_event or asyncio.Event()
        await self.broker.start()
        try:
            await self.declare_topology()
            await resolved_stop_event.wait()
        finally:
            await self.broker.stop()

    def _maybe_force_transcode_failure(self, dispatch_event: ContentPipelineDispatchEvent) -> None:
        forced_target = self.settings.pipeline_worker_fail_transcode_for_meme_file_id
        if forced_target is None:
            return
        if forced_target == str(dispatch_event.meme_file_id):
            raise ForcedTranscodeFailure(
                "Forced transcode failure requested by pipeline_worker_fail_transcode_for_meme_file_id.",
            )

    def _maybe_force_embed_failure(self, dispatch_event: ContentPipelineDispatchEvent) -> None:
        forced_target = self.settings.pipeline_worker_fail_embed_for_meme_file_id
        if forced_target is None:
            return
        if forced_target == str(dispatch_event.meme_file_id):
            raise ForcedEmbedFailure(
                "Forced embed failure requested by pipeline_worker_fail_embed_for_meme_file_id.",
            )

    def _maybe_force_classify_failure(self, dispatch_event: ContentPipelineDispatchEvent) -> None:
        forced_target = self.settings.pipeline_worker_fail_classify_for_meme_file_id
        if forced_target is None:
            return
        if forced_target == str(dispatch_event.meme_file_id):
            raise ForcedClassifyFailure(
                "Forced classify failure requested by pipeline_worker_fail_classify_for_meme_file_id.",
            )

    def _maybe_force_sync_qdrant_failure(self, dispatch_event: ContentPipelineDispatchEvent) -> None:
        forced_target = self.settings.pipeline_worker_fail_sync_qdrant_for_meme_file_id
        if forced_target is None:
            return
        if forced_target == str(dispatch_event.meme_file_id):
            raise ForcedSyncQdrantFailure(
                "Forced sync_qdrant failure requested by "
                "pipeline_worker_fail_sync_qdrant_for_meme_file_id.",
            )

    def _maybe_force_sync_meili_failure(self, dispatch_event: ContentPipelineDispatchEvent) -> None:
        forced_target = self.settings.pipeline_worker_fail_sync_meili_for_meme_file_id
        if forced_target is None:
            return
        if forced_target == str(dispatch_event.meme_file_id):
            raise ForcedSyncMeiliFailure(
                "Forced sync_meili failure requested by "
                "pipeline_worker_fail_sync_meili_for_meme_file_id.",
            )

    def _effective_attempt(
        self,
        dispatch_event: ContentPipelineDispatchEvent,
        message: RabbitMessageLike,
    ) -> int:
        retry_count = self._retry_cycle_count(dispatch_event.stage, message.headers)
        return max(dispatch_event.attempt + retry_count, 1)

    def _retry_queue_name_for_stage(self, stage: ContentPipelineStage) -> str | None:
        if stage is ContentPipelineStage.TRANSCODE:
            return self.transcode_retry_queue.name
        if stage is ContentPipelineStage.OCR:
            return self.ocr_retry_queue.name
        if stage is ContentPipelineStage.EMBED:
            return self.embed_retry_queue.name
        if stage is ContentPipelineStage.CLASSIFY:
            return self.classify_retry_queue.name
        if stage is ContentPipelineStage.SYNC_QDRANT:
            return self.sync_qdrant_retry_queue.name
        if stage is ContentPipelineStage.SYNC_MEILI:
            return self.sync_meili_retry_queue.name
        return None

    def _media_inspect_effective_attempt(self, message: RabbitMessageLike) -> int:
        return max(1 + self._retry_cycle_count_for_queue(self.media_inspect_retry_queue.name, message.headers), 1)

    def _retry_cycle_count(self, stage: ContentPipelineStage, headers: dict[str, Any]) -> int:
        raw_x_death = headers.get("x-death")
        if not isinstance(raw_x_death, list):
            return 0

        retry_queue_name = self._retry_queue_name_for_stage(stage)
        if retry_queue_name is None:
            return 0
        return self._retry_cycle_count_for_queue(retry_queue_name, headers)

    @staticmethod
    def _retry_cycle_count_for_queue(retry_queue_name: str, headers: dict[str, Any]) -> int:
        raw_x_death = headers.get("x-death")
        if not isinstance(raw_x_death, list):
            return 0

        for death_entry in raw_x_death:
            if not isinstance(death_entry, dict):
                continue
            if death_entry.get("queue") != retry_queue_name:
                continue
            if death_entry.get("reason") != "expired":
                continue

            raw_count = death_entry.get("count")
            if isinstance(raw_count, int):
                return max(raw_count, 0)
            if isinstance(raw_count, float):
                return max(int(raw_count), 0)
            if isinstance(raw_count, str):
                try:
                    return max(int(raw_count), 0)
                except ValueError:
                    return 0
            return 0

        return 0

    async def _record_malformed_event_failure(self, payload: object) -> None:
        reference = extract_event_reference(payload)
        if reference is None:
            return

        meme_file_id, stage, attempt, event_id = reference
        await self._mark_stage_failed(
            meme_file_id=meme_file_id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=PIPELINE_REASON_MALFORMED_EVENT,
            last_error_text="Worker received a malformed content-pipeline dispatch payload.",
            retryable=False,
        )

    async def _record_terminal_failure(
        self,
        dispatch_event: ContentPipelineDispatchEvent,
        *,
        attempt: int,
        normalized_reason: str,
        last_error_text: str,
        retryable: bool,
    ) -> None:
        await self._mark_stage_failed(
            meme_file_id=dispatch_event.meme_file_id,
            stage=dispatch_event.stage,
            attempt=attempt,
            event_id=dispatch_event.event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            retryable=retryable,
        )

    async def _dead_letter_or_requeue(
        self,
        payload: Any,
        *,
        message: RabbitMessageLike,
        normalized_reason: str,
    ) -> None:
        try:
            _ = await self.broker.publish(
                payload,
                exchange=self.dead_letter_exchange,
                routing_key=self.broker_settings.dead_letter_routing_key,
                persist=True,
                content_type=message.content_type,
                headers={
                    **message.headers,
                    "x-memexpert-failure-reason": normalized_reason,
                },
                message_id=message.message_id,
                mandatory=True,
            )
        except Exception:
            await message.nack(requeue=True)
            return

        await message.ack()


__all__ = [
    "ObjectStorageClientLike",
    "PipelineRuntime",
    "RabbitMessageLike",
    "SyncMeiliInputs",
    "SyncQdrantInputs",
]
