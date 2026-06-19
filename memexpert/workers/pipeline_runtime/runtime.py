# ruff: noqa: TC001,TC002,TC003
"""FastStream RabbitMQ runtime for the real transcode, OCR, embed, classify, and sync stages."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue
from pydantic import ValidationError

from memexpert.core.broker import PipelineBrokerSettings
from memexpert.core.classification import ClassificationClientProtocol
from memexpert.core.config import Settings
from memexpert.core.database import AsyncSessionFactory
from memexpert.core.meilisearch import MeilisearchSyncClientProtocol
from memexpert.core.ocr import OCRProcessorProtocol
from memexpert.core.qdrant import (
    QdrantSimilarityClientProtocol,
    QdrantSyncClientProtocol,
)
from memexpert.core.voyage import VoyageClientProtocol
from memexpert.models.enums import ContentPipelineStage
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.pipeline.events import MediaInspectRequestedEvent, SourceEngagementCaptureRequestedEvent
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import PipelineIngestError
from memexpert.services.source_engagement_capture import (
    SourceEngagementTelegramClientFactory,
    capture_source_engagement_request,
)
from memexpert.workers.pipeline_runtime.constants import (
    PIPELINE_REASON_MALFORMED_EVENT,
    PIPELINE_REASON_MEDIA_INSPECT_FAILED,
    PIPELINE_REASON_SOURCE_ENGAGEMENT_CAPTURE_FAILED,
    PIPELINE_REASON_UNSUPPORTED_STAGE,
)
from memexpert.workers.pipeline_runtime.errors import (
    coerce_dead_letter_payload,
    extract_event_reference,
    is_replayable_failure,
    normalize_failure_reason,
    render_error_text,
    validate_event_payload,
)
from memexpert.workers.pipeline_runtime.stage_registry import get_stage_handler
from memexpert.workers.pipeline_runtime.stages.context import PipelineStageHandlerContext
from memexpert.workers.pipeline_runtime.stages.media_inspect import run_media_inspect_stage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.media.contracts import PipelineMediaProcessorProtocol
    from memexpert.workers.pipeline_runtime.stages.context import ObjectStorageClientLike


class RabbitMessageLike(Protocol):
    """Minimal RabbitMQ message surface used by the runtime handler and tests."""

    headers: dict[str, Any]
    content_type: str | None
    message_id: str | None

    async def ack(self, multiple: bool = False) -> None: ...

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None: ...

    async def reject(self, requeue: bool = False) -> None: ...


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
    source_engagement_capture_queue: RabbitQueue
    transcode_queue: RabbitQueue
    ocr_queue: RabbitQueue
    embed_queue: RabbitQueue
    classify_queue: RabbitQueue
    sync_qdrant_queue: RabbitQueue
    sync_meili_queue: RabbitQueue
    media_inspect_retry_queue: RabbitQueue
    source_engagement_capture_retry_queue: RabbitQueue
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
    source_engagement_telegram_client_factory: SourceEngagementTelegramClientFactory

    async def declare_topology(self) -> None:
        """Declare the heavy-worker queues, retry queues, and DLQ topology explicitly."""

        exchange = await self.broker.declare_exchange(self.pipeline_exchange)
        retry_exchange = await self.broker.declare_exchange(self.retry_exchange)
        dead_letter_exchange = await self.broker.declare_exchange(self.dead_letter_exchange)
        media_inspect_queue = await self.broker.declare_queue(self.media_inspect_queue)
        source_engagement_capture_queue = await self.broker.declare_queue(self.source_engagement_capture_queue)
        transcode_queue = await self.broker.declare_queue(self.transcode_queue)
        ocr_queue = await self.broker.declare_queue(self.ocr_queue)
        embed_queue = await self.broker.declare_queue(self.embed_queue)
        classify_queue = await self.broker.declare_queue(self.classify_queue)
        sync_qdrant_queue = await self.broker.declare_queue(self.sync_qdrant_queue)
        sync_meili_queue = await self.broker.declare_queue(self.sync_meili_queue)
        media_inspect_retry_queue = await self.broker.declare_queue(self.media_inspect_retry_queue)
        source_engagement_capture_retry_queue = await self.broker.declare_queue(
            self.source_engagement_capture_retry_queue,
        )
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
        _ = await source_engagement_capture_queue.bind(
            exchange,
            routing_key=self.broker_settings.source_engagement_capture_routing_key,
        )
        _ = await source_engagement_capture_queue.bind(
            exchange,
            routing_key=self.broker_settings.source_engagement_capture_retry_routing_key,
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
        _ = await sync_qdrant_queue.bind(exchange, routing_key=self.broker_settings.sync_qdrant_routing_key)
        _ = await sync_qdrant_queue.bind(exchange, routing_key=sync_qdrant_retry_return_routing_key)
        _ = await sync_meili_queue.bind(exchange, routing_key=self.broker_settings.sync_meili_routing_key)
        _ = await sync_meili_queue.bind(exchange, routing_key=sync_meili_retry_return_routing_key)
        _ = await media_inspect_retry_queue.bind(
            retry_exchange,
            routing_key=self.broker_settings.media_inspect_retry_request_routing_key,
        )
        _ = await source_engagement_capture_retry_queue.bind(
            retry_exchange,
            routing_key=self.broker_settings.source_engagement_capture_retry_request_routing_key,
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
            await run_media_inspect_stage(
                self._stage_handler_context(),
                inspect_event=inspect_event,
            )
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

    async def handle_source_engagement_capture_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one scheduled source-engagement capture request."""

        try:
            capture_event = SourceEngagementCaptureRequestedEvent.model_validate(payload)
        except ValidationError:
            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(payload),
                message=message,
                normalized_reason=PIPELINE_REASON_MALFORMED_EVENT,
            )
            return

        try:
            _ = await capture_source_engagement_request(
                self.session_factory,
                capture_event,
                telegram_client_factory=self.source_engagement_telegram_client_factory,
            )
        except Exception:
            effective_attempt = self._source_engagement_capture_effective_attempt(message)
            if effective_attempt < self.broker_settings.retry_max_attempts:
                await message.reject(requeue=False)
                return

            await self._dead_letter_or_requeue(
                coerce_dead_letter_payload(capture_event.model_dump(mode="json")),
                message=message,
                normalized_reason=PIPELINE_REASON_SOURCE_ENGAGEMENT_CAPTURE_FAILED,
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
            await stage_handler(
                self._stage_handler_context(),
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=attempt,
            )
            return

        raise PipelineIngestError(
            f"Pipeline runtime cannot execute work for stage {dispatch_event.stage.value!r}.",
        )

    def _stage_handler_context(self) -> PipelineStageHandlerContext:
        return PipelineStageHandlerContext(
            settings=self.settings,
            session_factory=self.session_factory,
            storage_client=self.storage_client,
            media_processor=self.media_processor,
            ocr_processor=self.ocr_processor,
            voyage_client=self.voyage_client,
            qdrant_client=self.qdrant_client,
            qdrant_sync_client=self.qdrant_sync_client,
            meilisearch_sync_client=self.meilisearch_sync_client,
            classification_client=self.classification_client,
            broker=self.broker,
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
            service = self._build_stage_completion_service(session)
            return await service.start_stage_processing(
                meme_file_id=meme_file_id,
                stage=stage,
                attempt=attempt,
                event_id=event_id,
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
                service = self._build_stage_completion_service(session)
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

    def _build_stage_completion_service(self, session: AsyncSession) -> PipelineStageCompletionService:
        return PipelineStageCompletionService(
            session,
            settings=self.settings,
            broker=self.broker,
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

    def _source_engagement_capture_effective_attempt(self, message: RabbitMessageLike) -> int:
        return max(
            1 + self._retry_cycle_count_for_queue(self.source_engagement_capture_retry_queue.name, message.headers),
            1,
        )

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
            # Direct publish exception: keep the original delivery unacked until DLX transfer succeeds.
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
    "PipelineRuntime",
    "RabbitMessageLike",
]
