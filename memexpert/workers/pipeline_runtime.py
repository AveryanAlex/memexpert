# ruff: noqa: TC002
"""FastStream RabbitMQ runtime for the real transcode and OCR worker stages."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, cast

from faststream import AckPolicy
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage
from pydantic import ValidationError

from memexpert.core.broker import PipelineBrokerSettings, get_pipeline_broker, get_pipeline_broker_settings
from memexpert.core.classification import (
    ClassificationClientProtocol,
    ClassificationError,
    ClassificationProviderUnavailableError,
    ClassificationTimeoutError,
    PipelineClassificationClient,
)
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import AsyncSessionFactory, get_async_session_factory
from memexpert.core.media import (
    MediaTimeoutError,
    MediaValidationError,
    PipelineMediaProcessor,
    PipelineMediaProcessorProtocol,
)
from memexpert.core.ocr import (
    OCRProcessingError,
    OCRProcessorProtocol,
    OCRProviderUnavailableError,
    OCRTimeoutError,
    PipelineOCRProcessor,
)
from memexpert.core.qdrant import (
    PipelineQdrantClient,
    QdrantMalformedResponseError,
    QdrantProviderUnavailableError,
    QdrantSimilarityClientProtocol,
    QdrantSimilarityError,
    QdrantTimeoutError,
)
from memexpert.core.storage import (
    delete_object_if_present,
    download_object_bytes,
    get_pipeline_storage_settings,
    get_s3_client,
    upload_object_bytes,
)
from memexpert.core.voyage import (
    PipelineVoyageClient,
    VoyageClientProtocol,
    VoyageMalformedResponseError,
    VoyageProviderUnavailableError,
    VoyageTimeoutError,
)
from memexpert.models.enums import ContentPipelineStage
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import ContentPipelineService, PipelineIngestError, PipelineMergeTransactionError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.classification import ClassificationResult
    from memexpert.core.media import NormalizedMediaResult
    from memexpert.core.ocr import OCRExtractionResult
    from memexpert.core.qdrant import QdrantSimilarityMatch
    from memexpert.core.voyage import VoyageEmbeddingResult
    from memexpert.services.content_merge import MergeOutcome
    from memexpert.services.content_pipeline import PipelineStageWorkContext

PIPELINE_REASON_CLASSIFY_FAILED = "classify_stage_failed"
PIPELINE_REASON_CLASSIFY_MALFORMED = "classify_malformed_result"
PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED = "classify_provider_blocked"
PIPELINE_REASON_CLASSIFY_TIMEOUT = "classify_timeout"
PIPELINE_REASON_EMBED_FAILED = "embed_stage_failed"
PIPELINE_REASON_EMBED_MALFORMED_VECTOR = "embed_malformed_vector"
PIPELINE_REASON_EMBED_MERGE_TRANSACTION = "embed_merge_transaction_failed"
PIPELINE_REASON_EMBED_PROVIDER_BLOCKED = "embed_provider_blocked"
PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED = "embed_similarity_blocked"
PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED = "embed_similarity_malformed"
PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT = "embed_similarity_timeout"
PIPELINE_REASON_EMBED_TIMEOUT = "embed_timeout"
PIPELINE_REASON_FORCED_CLASSIFY_FAILURE = "forced_classify_failure"
PIPELINE_REASON_FORCED_EMBED_FAILURE = "forced_embed_failure"
PIPELINE_REASON_FORCED_TRANSCODE_FAILURE = "forced_transcode_failure"
PIPELINE_REASON_MALFORMED_EVENT = "malformed_dispatch_event"
PIPELINE_REASON_OCR_FAILED = "ocr_stage_failed"
PIPELINE_REASON_OCR_PROVIDER_BLOCKED = "ocr_provider_blocked"
PIPELINE_REASON_OCR_TIMEOUT = "ocr_timeout"
PIPELINE_REASON_TRANSCODE_FAILED = "transcode_stage_failed"
PIPELINE_REASON_TRANSCODE_INVALID_MEDIA = "transcode_invalid_media"
PIPELINE_REASON_TRANSCODE_TIMEOUT = "transcode_timeout"
PIPELINE_REASON_UNSUPPORTED_STAGE = "unsupported_stage"


type DeadLetterPayload = str | bytes | bytearray | int | float | bool | None


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


class ForcedTranscodeFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one transcode attempt to fail."""


class ForcedEmbedFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one embed attempt to fail."""


class ForcedClassifyFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one classify attempt to fail."""


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
    transcode_queue: RabbitQueue
    ocr_queue: RabbitQueue
    embed_queue: RabbitQueue
    classify_queue: RabbitQueue
    transcode_retry_queue: RabbitQueue
    ocr_retry_queue: RabbitQueue
    embed_retry_queue: RabbitQueue
    classify_retry_queue: RabbitQueue
    dead_letter_queue: RabbitQueue
    storage_client: ObjectStorageClientLike
    media_processor: PipelineMediaProcessorProtocol
    ocr_processor: OCRProcessorProtocol
    voyage_client: VoyageClientProtocol
    qdrant_client: QdrantSimilarityClientProtocol
    classification_client: ClassificationClientProtocol

    async def declare_topology(self) -> None:
        """Declare the heavy-worker queues, retry queues, and DLQ topology explicitly."""

        exchange = await self.broker.declare_exchange(self.pipeline_exchange)
        retry_exchange = await self.broker.declare_exchange(self.retry_exchange)
        dead_letter_exchange = await self.broker.declare_exchange(self.dead_letter_exchange)
        transcode_queue = await self.broker.declare_queue(self.transcode_queue)
        ocr_queue = await self.broker.declare_queue(self.ocr_queue)
        embed_queue = await self.broker.declare_queue(self.embed_queue)
        classify_queue = await self.broker.declare_queue(self.classify_queue)
        transcode_retry_queue = await self.broker.declare_queue(self.transcode_retry_queue)
        ocr_retry_queue = await self.broker.declare_queue(self.ocr_retry_queue)
        embed_retry_queue = await self.broker.declare_queue(self.embed_retry_queue)
        classify_retry_queue = await self.broker.declare_queue(self.classify_retry_queue)
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

        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.meme_created_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.stage_replay_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.transcode_retry_routing_key)
        _ = await ocr_queue.bind(exchange, routing_key=self.broker_settings.ocr_routing_key)
        _ = await ocr_queue.bind(exchange, routing_key=self.broker_settings.ocr_retry_routing_key)
        _ = await embed_queue.bind(exchange, routing_key=self.broker_settings.embed_routing_key)
        _ = await embed_queue.bind(exchange, routing_key=embed_retry_return_routing_key)
        _ = await classify_queue.bind(exchange, routing_key=self.broker_settings.classify_routing_key)
        _ = await classify_queue.bind(exchange, routing_key=classify_retry_return_routing_key)
        _ = await transcode_retry_queue.bind(retry_exchange, routing_key=self.broker_settings.retry_routing_key)
        _ = await ocr_retry_queue.bind(retry_exchange, routing_key=self.broker_settings.ocr_retry_request_routing_key)
        _ = await embed_retry_queue.bind(retry_exchange, routing_key=embed_retry_request_routing_key)
        _ = await classify_retry_queue.bind(retry_exchange, routing_key=classify_retry_request_routing_key)
        _ = await dead_letter_queue.bind(
            dead_letter_exchange,
            routing_key=self.broker_settings.dead_letter_routing_key,
        )

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

    async def _handle_stage_message(
        self,
        *,
        payload: object,
        message: RabbitMessageLike,
        expected_stage: ContentPipelineStage,
    ) -> None:
        dispatch_event = self._validate_event_payload(payload)
        if dispatch_event is None:
            await self._record_malformed_event_failure(payload)
            await self._dead_letter_or_requeue(
                self._coerce_dead_letter_payload(payload),
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
                self._coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
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
            normalized_reason = self._normalize_failure_reason(expected_stage, exc)
            retryable = self._is_replayable_failure(expected_stage, exc)
            await self._mark_stage_failed(
                meme_file_id=dispatch_event.meme_file_id,
                stage=dispatch_event.stage,
                attempt=effective_attempt,
                event_id=dispatch_event.event_id,
                normalized_reason=normalized_reason,
                last_error_text=self._render_error_text(exc),
                retryable=retryable,
            )

            should_queue_retry = retryable and effective_attempt < self.broker_settings.retry_max_attempts
            if should_queue_retry:
                await message.reject(requeue=False)
                return

            await self._dead_letter_or_requeue(
                self._coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
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
        if dispatch_event.stage is ContentPipelineStage.TRANSCODE:
            self._maybe_force_transcode_failure(dispatch_event)
            await self._run_transcode_stage(
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=attempt,
            )
            return
        if dispatch_event.stage is ContentPipelineStage.OCR:
            await self._run_ocr_stage(
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=attempt,
            )
            return
        if dispatch_event.stage is ContentPipelineStage.EMBED:
            self._maybe_force_embed_failure(dispatch_event)
            await self._run_embed_stage(
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=attempt,
            )
            return
        if dispatch_event.stage is ContentPipelineStage.CLASSIFY:
            self._maybe_force_classify_failure(dispatch_event)
            await self._run_classify_stage(
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
            storage_client=self.storage_client,
            media_processor=self.media_processor,
            ocr_processor=self.ocr_processor,
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

    @staticmethod
    def _validate_event_payload(payload: object) -> ContentPipelineDispatchEvent | None:
        try:
            return ContentPipelineDispatchEvent.model_validate(payload)
        except ValidationError:
            return None

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
        return None

    def _retry_cycle_count(self, stage: ContentPipelineStage, headers: dict[str, Any]) -> int:
        raw_x_death = headers.get("x-death")
        if not isinstance(raw_x_death, list):
            return 0

        retry_queue_name = self._retry_queue_name_for_stage(stage)
        if retry_queue_name is None:
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
        reference = self._extract_event_reference(payload)
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
        payload: DeadLetterPayload,
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

    @staticmethod
    def _coerce_dead_letter_payload(payload: object) -> DeadLetterPayload:
        if payload is None:
            return None
        if isinstance(payload, (str, bytes, bytearray, int, float, bool)):
            return payload
        if isinstance(payload, dict):
            return json.dumps(payload, sort_keys=True)
        return str(payload)

    @staticmethod
    def _normalize_failure_reason(stage: ContentPipelineStage, exc: Exception) -> str:
        if stage is ContentPipelineStage.TRANSCODE:
            if isinstance(exc, ForcedTranscodeFailure):
                return PIPELINE_REASON_FORCED_TRANSCODE_FAILURE
            if isinstance(exc, MediaTimeoutError):
                return PIPELINE_REASON_TRANSCODE_TIMEOUT
            if isinstance(exc, MediaValidationError):
                return PIPELINE_REASON_TRANSCODE_INVALID_MEDIA
            return PIPELINE_REASON_TRANSCODE_FAILED

        if stage is ContentPipelineStage.OCR:
            if isinstance(exc, OCRTimeoutError):
                return PIPELINE_REASON_OCR_TIMEOUT
            if isinstance(exc, OCRProviderUnavailableError):
                return PIPELINE_REASON_OCR_PROVIDER_BLOCKED
            return PIPELINE_REASON_OCR_FAILED

        if stage is ContentPipelineStage.EMBED:
            if isinstance(exc, ForcedEmbedFailure):
                return PIPELINE_REASON_FORCED_EMBED_FAILURE
            if isinstance(exc, VoyageTimeoutError):
                return PIPELINE_REASON_EMBED_TIMEOUT
            if isinstance(exc, VoyageMalformedResponseError):
                return PIPELINE_REASON_EMBED_MALFORMED_VECTOR
            if isinstance(exc, VoyageProviderUnavailableError):
                return PIPELINE_REASON_EMBED_PROVIDER_BLOCKED
            if isinstance(exc, PipelineMergeTransactionError):
                return PIPELINE_REASON_EMBED_MERGE_TRANSACTION
            if isinstance(exc, QdrantTimeoutError):
                return PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT
            if isinstance(exc, QdrantMalformedResponseError):
                return PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED
            if isinstance(exc, QdrantProviderUnavailableError):
                return PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED
            if isinstance(exc, QdrantSimilarityError):
                return PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED
            return PIPELINE_REASON_EMBED_FAILED

        if stage is ContentPipelineStage.CLASSIFY:
            if isinstance(exc, ForcedClassifyFailure):
                return PIPELINE_REASON_FORCED_CLASSIFY_FAILURE
            if isinstance(exc, ClassificationTimeoutError):
                return PIPELINE_REASON_CLASSIFY_TIMEOUT
            if isinstance(exc, ClassificationProviderUnavailableError):
                return PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED
            if isinstance(exc, ClassificationError):
                return PIPELINE_REASON_CLASSIFY_MALFORMED
            return PIPELINE_REASON_CLASSIFY_FAILED

        return PIPELINE_REASON_OCR_FAILED

    @staticmethod
    def _is_replayable_failure(stage: ContentPipelineStage, exc: Exception) -> bool:
        if stage is ContentPipelineStage.TRANSCODE:
            return not isinstance(exc, MediaValidationError)
        if stage is ContentPipelineStage.EMBED:
            # Merge-transaction failures roll back the single embed transaction and
            # must stay replayable. Genuine contract violations (malformed vectors,
            # impossible state transitions) surface the base PipelineIngestError and
            # dead-letter. Qdrant malformed responses match the VoyageMalformed
            # behavior because the payload is structurally untrustworthy.
            if isinstance(exc, PipelineMergeTransactionError):
                return True
            return not isinstance(
                exc,
                (VoyageMalformedResponseError, QdrantMalformedResponseError, PipelineIngestError),
            )
        if stage is ContentPipelineStage.CLASSIFY:
            return not isinstance(exc, PipelineIngestError)
        return not isinstance(exc, PipelineIngestError)

    @staticmethod
    def _render_error_text(exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            return message
        return exc.__class__.__name__

    @staticmethod
    def _extract_event_reference(
        payload: object,
    ) -> tuple[uuid.UUID, ContentPipelineStage, int, uuid.UUID] | None:
        if not isinstance(payload, dict):
            return None

        raw_meme_file_id = payload.get("meme_file_id")
        raw_stage = payload.get("stage")
        if not isinstance(raw_meme_file_id, str) or not isinstance(raw_stage, str):
            return None

        try:
            meme_file_id = uuid.UUID(raw_meme_file_id)
            stage = ContentPipelineStage(raw_stage)
        except (ValueError, TypeError):
            return None

        raw_attempt = payload.get("attempt")
        attempt = raw_attempt if isinstance(raw_attempt, int) and raw_attempt >= 1 else 1

        raw_event_id = payload.get("event_id")
        try:
            event_id = uuid.UUID(raw_event_id) if isinstance(raw_event_id, str) else uuid.uuid7()
        except ValueError:
            event_id = uuid.uuid7()

        return meme_file_id, stage, attempt, event_id


def _build_pipeline_exchange(broker_settings: PipelineBrokerSettings) -> RabbitExchange:
    return RabbitExchange(
        broker_settings.exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )


def _build_retry_exchange(broker_settings: PipelineBrokerSettings) -> RabbitExchange:
    return RabbitExchange(
        broker_settings.retry_exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )


def _build_dead_letter_exchange(broker_settings: PipelineBrokerSettings) -> RabbitExchange:
    return RabbitExchange(
        broker_settings.dead_letter_exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )


def _build_stage_queue(
    *,
    queue_name: str,
    routing_key: str,
    retry_request_routing_key: str,
    retry_exchange: str,
) -> RabbitQueue:
    return RabbitQueue(
        queue_name,
        durable=True,
        routing_key=routing_key,
        arguments={
            "x-dead-letter-exchange": retry_exchange,
            "x-dead-letter-routing-key": retry_request_routing_key,
        },
    )


def _build_retry_queue(
    *,
    queue_name: str,
    retry_backoff_milliseconds: int,
    exchange: str,
    retry_return_routing_key: str,
) -> RabbitQueue:
    return RabbitQueue(
        queue_name,
        durable=True,
        arguments={
            "x-message-ttl": retry_backoff_milliseconds,
            "x-dead-letter-exchange": exchange,
            "x-dead-letter-routing-key": retry_return_routing_key,
        },
    )


def _build_dead_letter_queue(broker_settings: PipelineBrokerSettings) -> RabbitQueue:
    return RabbitQueue(
        broker_settings.dead_letter_queue,
        durable=True,
    )


def build_pipeline_runtime(
    *,
    settings: Settings | None = None,
    broker: RabbitBroker | None = None,
    session_factory: AsyncSessionFactory | None = None,
    storage_client: ObjectStorageClientLike | None = None,
    media_processor: PipelineMediaProcessorProtocol | None = None,
    ocr_processor: OCRProcessorProtocol | None = None,
    voyage_client: VoyageClientProtocol | None = None,
    qdrant_client: QdrantSimilarityClientProtocol | None = None,
    classification_client: ClassificationClientProtocol | None = None,
) -> PipelineRuntime:
    """Build the RabbitMQ heavy-worker runtime and register its FastStream subscribers."""

    resolved_settings = settings or get_settings()
    resolved_broker_settings = get_pipeline_broker_settings(resolved_settings)
    resolved_broker = broker or get_pipeline_broker()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_storage_client = storage_client or cast("ObjectStorageClientLike", get_s3_client())
    resolved_media_processor = media_processor or PipelineMediaProcessor(settings=resolved_settings)
    resolved_ocr_processor = ocr_processor or PipelineOCRProcessor(
        settings=resolved_settings,
        media_processor=resolved_media_processor,
    )
    resolved_voyage_client = voyage_client or PipelineVoyageClient(settings=resolved_settings)
    resolved_qdrant_client = qdrant_client or PipelineQdrantClient(settings=resolved_settings)
    resolved_classification_client = classification_client or PipelineClassificationClient(
        settings=resolved_settings,
    )
    transcode_retry_queue_name = f"{resolved_broker_settings.transcode_queue}.retry"
    ocr_retry_queue_name = f"{resolved_broker_settings.ocr_queue}.retry"
    embed_retry_queue_name = f"{resolved_broker_settings.embed_queue}.retry"
    classify_retry_queue_name = f"{resolved_broker_settings.classify_queue}.retry"
    embed_retry_request_routing_key = resolved_broker_settings.retry_queue_routing_key_for_stage(
        ContentPipelineStage.EMBED,
    )
    classify_retry_request_routing_key = resolved_broker_settings.retry_queue_routing_key_for_stage(
        ContentPipelineStage.CLASSIFY,
    )
    embed_retry_return_routing_key = resolved_broker_settings.retry_return_routing_key_for_stage(
        ContentPipelineStage.EMBED,
    )
    classify_retry_return_routing_key = resolved_broker_settings.retry_return_routing_key_for_stage(
        ContentPipelineStage.CLASSIFY,
    )

    runtime = PipelineRuntime(
        settings=resolved_settings,
        broker=resolved_broker,
        session_factory=resolved_session_factory,
        broker_settings=resolved_broker_settings,
        pipeline_exchange=_build_pipeline_exchange(resolved_broker_settings),
        retry_exchange=_build_retry_exchange(resolved_broker_settings),
        dead_letter_exchange=_build_dead_letter_exchange(resolved_broker_settings),
        transcode_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.transcode_queue,
            routing_key=resolved_broker_settings.meme_created_routing_key,
            retry_request_routing_key=resolved_broker_settings.retry_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        ocr_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.ocr_queue,
            routing_key=resolved_broker_settings.ocr_routing_key,
            retry_request_routing_key=resolved_broker_settings.ocr_retry_request_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        embed_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.embed_queue,
            routing_key=resolved_broker_settings.embed_routing_key,
            retry_request_routing_key=embed_retry_request_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        classify_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.classify_queue,
            routing_key=resolved_broker_settings.classify_routing_key,
            retry_request_routing_key=classify_retry_request_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        transcode_retry_queue=_build_retry_queue(
            queue_name=transcode_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=resolved_broker_settings.transcode_retry_routing_key,
        ),
        ocr_retry_queue=_build_retry_queue(
            queue_name=ocr_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=resolved_broker_settings.ocr_retry_routing_key,
        ),
        embed_retry_queue=_build_retry_queue(
            queue_name=embed_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=embed_retry_return_routing_key,
        ),
        classify_retry_queue=_build_retry_queue(
            queue_name=classify_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=classify_retry_return_routing_key,
        ),
        dead_letter_queue=_build_dead_letter_queue(resolved_broker_settings),
        storage_client=resolved_storage_client,
        media_processor=resolved_media_processor,
        ocr_processor=resolved_ocr_processor,
        voyage_client=resolved_voyage_client,
        qdrant_client=resolved_qdrant_client,
        classification_client=resolved_classification_client,
    )

    @resolved_broker.subscriber(
        runtime.transcode_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_transcode(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_transcode_message(payload, rabbit_message)

    @resolved_broker.subscriber(
        runtime.ocr_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_ocr(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_ocr_message(payload, rabbit_message)

    @resolved_broker.subscriber(
        runtime.embed_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_embed(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_embed_message(payload, rabbit_message)

    @resolved_broker.subscriber(
        runtime.classify_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_classify(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_classify_message(payload, rabbit_message)

    return runtime


async def run_pipeline_runtime(*, settings: Settings | None = None) -> None:
    """Start the real RabbitMQ-backed content-pipeline worker runtime."""

    runtime = build_pipeline_runtime(settings=settings)
    await runtime.run()


__all__ = [
    "PIPELINE_REASON_CLASSIFY_FAILED",
    "PIPELINE_REASON_CLASSIFY_MALFORMED",
    "PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED",
    "PIPELINE_REASON_CLASSIFY_TIMEOUT",
    "PIPELINE_REASON_EMBED_FAILED",
    "PIPELINE_REASON_EMBED_MALFORMED_VECTOR",
    "PIPELINE_REASON_EMBED_PROVIDER_BLOCKED",
    "PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED",
    "PIPELINE_REASON_EMBED_TIMEOUT",
    "PIPELINE_REASON_FORCED_CLASSIFY_FAILURE",
    "PIPELINE_REASON_FORCED_EMBED_FAILURE",
    "PIPELINE_REASON_FORCED_TRANSCODE_FAILURE",
    "PIPELINE_REASON_MALFORMED_EVENT",
    "PIPELINE_REASON_OCR_FAILED",
    "PIPELINE_REASON_OCR_PROVIDER_BLOCKED",
    "PIPELINE_REASON_OCR_TIMEOUT",
    "PIPELINE_REASON_TRANSCODE_FAILED",
    "PIPELINE_REASON_TRANSCODE_INVALID_MEDIA",
    "PIPELINE_REASON_TRANSCODE_TIMEOUT",
    "PIPELINE_REASON_UNSUPPORTED_STAGE",
    "ForcedClassifyFailure",
    "ForcedEmbedFailure",
    "ForcedTranscodeFailure",
    "PipelineRuntime",
    "RabbitMessageLike",
    "build_pipeline_runtime",
    "run_pipeline_runtime",
]
