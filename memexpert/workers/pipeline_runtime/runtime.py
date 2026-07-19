# ruff: noqa: TC001,TC002,TC003
"""FastStream RabbitMQ runtime for the real transcode, OCR, embed, classify, and sync stages."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
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
from memexpert.pipeline.stage_completion import (
    CancelledStageDisposition,
    CancelledStageResolution,
    PipelineStageCompletionService,
)
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import PipelineIngestError
from memexpert.services.pipeline_reliability import (
    DEPENDENCY_BY_STAGE,
    DependencyCircuitOpenError,
    acquire_dependency_circuit,
    record_dependency_failure,
    record_dependency_success,
)
from memexpert.services.source_engagement_capture import (
    SourceEngagementTelegramClientFactory,
    capture_source_engagement_request,
)
from memexpert.workers.pipeline_runtime.constants import (
    PIPELINE_REASON_MALFORMED_EVENT,
    PIPELINE_REASON_MEDIA_INSPECT_FAILED,
    PIPELINE_REASON_SOURCE_ENGAGEMENT_CAPTURE_FAILED,
    PIPELINE_REASON_UNSUPPORTED_STAGE,
    PIPELINE_REASON_WORKER_SHUTDOWN,
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
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.crawlers.telegram.manager import TelegramSessionManager
    from memexpert.media.contracts import PipelineMediaProcessorProtocol
    from memexpert.runtime_health import RuntimeHealthReporter
    from memexpert.workers.pipeline_runtime.stages.context import ObjectStorageClientLike

type WorkerBackgroundRunner = Callable[[Settings, asyncio.Event], Awaitable[None]]
type DeadLetterRecorder = Callable[..., Awaitable[uuid.UUID]]

logger = logging.getLogger(__name__)

_FORCED_TASK_CLEANUP_TIMEOUT_SECONDS = 10.0
_CANCELLED_DELIVERY_CLEANUP_TIMEOUT_SECONDS = 10.0
_DEPENDENCY_CLEANUP_TIMEOUT_SECONDS = 5.0

_OCR_CANARY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAAAwCAIAAABWluXpAAAAeElEQVR42u3bMQrAIBBFwfyQ+195PYFCLFaQea2g4KBgY"
    "arq0bleWwAAgAAAEAAAAgBAvX2zgSR/51o8qi+bbXstJ8AVJAAABACAAAAQAAAABACAAAAQAAACAEAAAAgAAAEAIAAABODm4puqE"
    "wBAAAAIAAABAKDeBuQAElucUbrxAAAAAElFTkSuQmCC"
)


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

    role: WorkerRole
    settings: Settings
    broker: RabbitBroker
    session_factory: AsyncSessionFactory
    broker_settings: PipelineBrokerSettings
    pipeline_exchange: RabbitExchange
    retry_exchange: RabbitExchange
    dead_letter_exchange: RabbitExchange
    media_inspect_queue: RabbitQueue
    source_engagement_capture_queues: tuple[RabbitQueue, ...]
    transcode_queue: RabbitQueue
    ocr_queue: RabbitQueue
    embed_queue: RabbitQueue
    classify_queue: RabbitQueue
    sync_qdrant_queue: RabbitQueue
    sync_meili_queue: RabbitQueue
    media_inspect_retry_queue: RabbitQueue
    source_engagement_capture_retry_queues: tuple[RabbitQueue, ...]
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
    source_engagement_close_telegram_client_after_capture: bool
    dead_letter_recorder: DeadLetterRecorder
    source_engagement_telegram_session_manager: TelegramSessionManager | None = None
    health_reporter: RuntimeHealthReporter | None = None
    _stop_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _force_stop_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _draining: bool = field(default=False, init=False, repr=False)
    _shutdown_started: bool = field(default=False, init=False, repr=False)
    _shutdown_deadline: float | None = field(default=None, init=False, repr=False)
    _broker_start_attempted: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _consumer_quiesce_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _active_delivery_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _active_delivery_changed: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    async def declare_topology(self) -> None:
        """Declare the heavy-worker queues, retry queues, and DLQ topology explicitly."""

        exchange = await self.broker.declare_exchange(self.pipeline_exchange)
        retry_exchange = await self.broker.declare_exchange(self.retry_exchange)
        dead_letter_exchange = await self.broker.declare_exchange(self.dead_letter_exchange)
        media_inspect_queue = (
            await self.broker.declare_queue(self.media_inspect_queue) if self.role.consumes_media_inspect else None
        )
        source_engagement_capture_queues = (
            [await self.broker.declare_queue(queue) for queue in self.source_engagement_capture_queues]
            if self.role.consumes_source_engagement
            else []
        )
        transcode_queue = (
            await self.broker.declare_queue(self.transcode_queue)
            if self.role.consumes_stage(ContentPipelineStage.TRANSCODE)
            else None
        )
        ocr_queue = (
            await self.broker.declare_queue(self.ocr_queue)
            if self.role.consumes_stage(ContentPipelineStage.OCR)
            else None
        )
        embed_queue = (
            await self.broker.declare_queue(self.embed_queue)
            if self.role.consumes_stage(ContentPipelineStage.EMBED)
            else None
        )
        classify_queue = (
            await self.broker.declare_queue(self.classify_queue)
            if self.role.consumes_stage(ContentPipelineStage.CLASSIFY)
            else None
        )
        sync_qdrant_queue = (
            await self.broker.declare_queue(self.sync_qdrant_queue)
            if self.role.consumes_stage(ContentPipelineStage.SYNC_QDRANT)
            else None
        )
        sync_meili_queue = (
            await self.broker.declare_queue(self.sync_meili_queue)
            if self.role.consumes_stage(ContentPipelineStage.SYNC_MEILI)
            else None
        )
        media_inspect_retry_queue = (
            await self.broker.declare_queue(self.media_inspect_retry_queue)
            if self.role.consumes_media_inspect
            else None
        )
        source_engagement_capture_retry_queues = (
            [await self.broker.declare_queue(queue) for queue in self.source_engagement_capture_retry_queues]
            if self.role.consumes_source_engagement
            else []
        )
        transcode_retry_queue = (
            await self.broker.declare_queue(self.transcode_retry_queue)
            if self.role.consumes_stage(ContentPipelineStage.TRANSCODE)
            else None
        )
        ocr_retry_queue = (
            await self.broker.declare_queue(self.ocr_retry_queue)
            if self.role.consumes_stage(ContentPipelineStage.OCR)
            else None
        )
        embed_retry_queue = (
            await self.broker.declare_queue(self.embed_retry_queue)
            if self.role.consumes_stage(ContentPipelineStage.EMBED)
            else None
        )
        classify_retry_queue = (
            await self.broker.declare_queue(self.classify_retry_queue)
            if self.role.consumes_stage(ContentPipelineStage.CLASSIFY)
            else None
        )
        sync_qdrant_retry_queue = (
            await self.broker.declare_queue(self.sync_qdrant_retry_queue)
            if self.role.consumes_stage(ContentPipelineStage.SYNC_QDRANT)
            else None
        )
        sync_meili_retry_queue = (
            await self.broker.declare_queue(self.sync_meili_retry_queue)
            if self.role.consumes_stage(ContentPipelineStage.SYNC_MEILI)
            else None
        )
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

        if media_inspect_queue is not None:
            _ = await media_inspect_queue.bind(exchange, routing_key=self.broker_settings.media_inspect_routing_key)
            _ = await media_inspect_queue.bind(
                exchange,
                routing_key=self.broker_settings.media_inspect_retry_routing_key,
            )
        for source_engagement_capture_queue in source_engagement_capture_queues:
            session_key = self._source_engagement_session_key_for_queue(source_engagement_capture_queue.name)
            _ = await source_engagement_capture_queue.bind(
                exchange,
                routing_key=self.broker_settings.source_engagement_capture_binding_key_for_session(session_key),
            )
            _ = await source_engagement_capture_queue.bind(
                exchange,
                routing_key=self.broker_settings.source_engagement_capture_retry_routing_key_for_session(session_key),
            )
        if transcode_queue is not None:
            _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.meme_created_routing_key)
            _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.stage_replay_routing_key)
            _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.transcode_retry_routing_key)
        if ocr_queue is not None:
            _ = await ocr_queue.bind(exchange, routing_key=self.broker_settings.ocr_routing_key)
            _ = await ocr_queue.bind(exchange, routing_key=self.broker_settings.ocr_retry_routing_key)
        if embed_queue is not None:
            _ = await embed_queue.bind(exchange, routing_key=self.broker_settings.embed_routing_key)
            _ = await embed_queue.bind(exchange, routing_key=embed_retry_return_routing_key)
        if classify_queue is not None:
            _ = await classify_queue.bind(exchange, routing_key=self.broker_settings.classify_routing_key)
            _ = await classify_queue.bind(exchange, routing_key=classify_retry_return_routing_key)
        if sync_qdrant_queue is not None:
            _ = await sync_qdrant_queue.bind(exchange, routing_key=self.broker_settings.sync_qdrant_routing_key)
            _ = await sync_qdrant_queue.bind(exchange, routing_key=sync_qdrant_retry_return_routing_key)
        if sync_meili_queue is not None:
            _ = await sync_meili_queue.bind(exchange, routing_key=self.broker_settings.sync_meili_routing_key)
            _ = await sync_meili_queue.bind(exchange, routing_key=sync_meili_retry_return_routing_key)
        if media_inspect_retry_queue is not None:
            _ = await media_inspect_retry_queue.bind(
                retry_exchange,
                routing_key=self.broker_settings.media_inspect_retry_request_routing_key,
            )
        for source_engagement_capture_retry_queue in source_engagement_capture_retry_queues:
            session_key = self._source_engagement_session_key_for_retry_queue(
                source_engagement_capture_retry_queue.name,
            )
            _ = await source_engagement_capture_retry_queue.bind(
                retry_exchange,
                routing_key=self.broker_settings.source_engagement_capture_retry_request_routing_key_for_session(
                    session_key,
                ),
            )
        if transcode_retry_queue is not None:
            _ = await transcode_retry_queue.bind(retry_exchange, routing_key=self.broker_settings.retry_routing_key)
        if ocr_retry_queue is not None:
            _ = await ocr_retry_queue.bind(
                retry_exchange,
                routing_key=self.broker_settings.ocr_retry_request_routing_key,
            )
        if embed_retry_queue is not None:
            _ = await embed_retry_queue.bind(retry_exchange, routing_key=embed_retry_request_routing_key)
        if classify_retry_queue is not None:
            _ = await classify_retry_queue.bind(retry_exchange, routing_key=classify_retry_request_routing_key)
        if sync_qdrant_retry_queue is not None:
            _ = await sync_qdrant_retry_queue.bind(retry_exchange, routing_key=sync_qdrant_retry_request_routing_key)
        if sync_meili_retry_queue is not None:
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
                close_telegram_client_after_capture=self.source_engagement_close_telegram_client_after_capture,
            )
        except Exception:
            effective_attempt = self._source_engagement_capture_effective_attempt(capture_event, message)
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
                    f"The runtime handler for {expected_stage.value!r} received {dispatch_event.stage.value!r}."
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
            await self._process_stage_delivery(
                dispatch_event=dispatch_event,
                message=message,
                expected_stage=expected_stage,
                attempt=effective_attempt,
            )
        except asyncio.CancelledError:
            await self._abandon_cancelled_stage_delivery(
                dispatch_event,
                message=message,
                attempt=effective_attempt,
            )
            raise

    async def _process_stage_delivery(
        self,
        *,
        dispatch_event: ContentPipelineDispatchEvent,
        message: RabbitMessageLike,
        expected_stage: ContentPipelineStage,
        attempt: int,
    ) -> None:
        dependency = DEPENDENCY_BY_STAGE.get(dispatch_event.stage)
        try:
            stage_context = await self._start_stage_processing(
                meme_file_id=dispatch_event.meme_file_id,
                stage=dispatch_event.stage,
                attempt=attempt,
                event_id=dispatch_event.event_id,
            )
            if stage_context is None:
                await message.ack()
                return
            if dependency is not None:
                await acquire_dependency_circuit(
                    self.session_factory,
                    dependency=dependency,
                    owner=self._circuit_owner(),
                )
            await self._run_stage_for(
                dispatch_event=dispatch_event,
                stage_context=stage_context,
                attempt=attempt,
            )
            if dependency is not None:
                try:
                    await record_dependency_success(self.session_factory, dependency=dependency)
                except Exception:
                    logger.exception(
                        "pipeline_dependency_circuit_success_record_failed",
                        extra={
                            "event": "pipeline_dependency_circuit_success_record_failed",
                            "dependency": dependency,
                        },
                    )
        except Exception as exc:
            normalized_reason = normalize_failure_reason(expected_stage, exc)
            retryable = is_replayable_failure(expected_stage, exc)
            if dependency is not None and retryable and not isinstance(exc, DependencyCircuitOpenError):
                try:
                    await record_dependency_failure(
                        self.session_factory,
                        dependency=dependency,
                        error=exc,
                        failure_threshold=self.settings.pipeline_circuit_failure_threshold,
                        cooldown_seconds=self.settings.pipeline_circuit_cooldown_seconds,
                    )
                except Exception:
                    logger.exception(
                        "pipeline_dependency_circuit_failure_record_failed",
                        extra={
                            "event": "pipeline_dependency_circuit_failure_record_failed",
                            "dependency": dependency,
                        },
                    )
            await self._mark_stage_failed(
                meme_file_id=dispatch_event.meme_file_id,
                stage=dispatch_event.stage,
                attempt=attempt,
                event_id=dispatch_event.event_id,
                normalized_reason=normalized_reason,
                last_error_text=render_error_text(exc),
                retryable=retryable,
            )

            should_queue_retry = retryable and attempt < self.broker_settings.retry_max_attempts
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

    async def _abandon_cancelled_stage_delivery(
        self,
        dispatch_event: ContentPipelineDispatchEvent,
        *,
        message: RabbitMessageLike,
        attempt: int,
    ) -> None:
        resolution: CancelledStageResolution | None = None
        try:
            async with asyncio.timeout(_CANCELLED_DELIVERY_CLEANUP_TIMEOUT_SECONDS):
                async with self.session_factory() as session:
                    service = self._build_stage_completion_service(session)
                    resolution = await service.abandon_stage_processing(
                        meme_file_id=dispatch_event.meme_file_id,
                        stage=dispatch_event.stage,
                        attempt=attempt,
                        event_id=dispatch_event.event_id,
                        normalized_reason=PIPELINE_REASON_WORKER_SHUTDOWN,
                        last_error_text="Worker shutdown interrupted this stage before RabbitMQ acknowledgement.",
                    )
        except Exception:  # noqa: BLE001 - channel close and stale recovery remain the fallback.
            logger.exception(
                "worker_shutdown_stage_abandon_failed",
                extra={
                    "event": "worker_shutdown_stage_abandon_failed",
                    "role": self.role.value,
                    "stage": dispatch_event.stage.value,
                    "meme_file_id": str(dispatch_event.meme_file_id),
                    "message_id": message.message_id,
                },
            )
            return

        disposition = resolution.disposition
        try:
            if disposition is CancelledStageDisposition.REQUEUE:
                await message.nack(requeue=True)
            elif disposition is CancelledStageDisposition.DEAD_LETTER:
                if resolution.normalized_reason is None:
                    logger.error(
                        "worker_shutdown_terminal_stage_reason_missing",
                        extra={
                            "event": "worker_shutdown_terminal_stage_reason_missing",
                            "role": self.role.value,
                            "stage": dispatch_event.stage.value,
                            "meme_file_id": str(dispatch_event.meme_file_id),
                            "message_id": message.message_id,
                        },
                    )
                    return
                await self._dead_letter_or_requeue(
                    coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
                    message=message,
                    normalized_reason=resolution.normalized_reason,
                )
            else:
                await message.ack()
        except Exception:  # noqa: BLE001 - broker close automatically requeues an unacked delivery.
            logger.exception(
                "worker_shutdown_stage_disposition_failed",
                extra={
                    "event": "worker_shutdown_stage_disposition_failed",
                    "role": self.role.value,
                    "stage": dispatch_event.stage.value,
                    "meme_file_id": str(dispatch_event.meme_file_id),
                    "message_id": message.message_id,
                    "disposition": disposition.value,
                },
            )

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
    ) -> PipelineStageWorkContext | None:
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

    def _build_stage_completion_service(self, session: AsyncSession) -> PipelineStageCompletionService:
        return PipelineStageCompletionService(
            session,
            settings=self.settings,
            broker=self.broker,
            worker_role=self.role.value,
            worker_instance_id=(self.health_reporter.boot_id if self.health_reporter is not None else None),
        )

    def _circuit_owner(self) -> str:
        instance_id = self.health_reporter.boot_id if self.health_reporter is not None else str(id(self))
        return f"{self.role.value}:{instance_id}"

    @asynccontextmanager
    async def operation(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        """Track one bounded subscriber operation when health reporting is enabled."""

        if self.health_reporter is None:
            yield
            return
        async with self.health_reporter.operation(name, timeout_seconds=timeout_seconds):
            yield

    @asynccontextmanager
    async def consumer_operation(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[bool]:
        """Admit and track one delivery unless process-wide draining has begun."""

        stop_requested = self._stop_event is not None and self._stop_event.is_set()
        if self._draining or stop_requested:
            yield False
            return

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("RabbitMQ delivery is not running inside an asyncio task.")

        self._active_delivery_tasks.add(task)
        try:
            async with self.operation(name, timeout_seconds=timeout_seconds):
                yield True
        finally:
            self._active_delivery_tasks.discard(task)
            self._active_delivery_changed.set()

    async def verify_readiness(self) -> None:
        """Run role-specific startup canaries before the process reports ready."""

        if not self.role.needs_ocr:
            return
        async with self.operation(
            "ocr_startup_canary",
            timeout_seconds=self.settings.pipeline_ocr_timeout_seconds,
        ):
            _ = await self.ocr_processor.extract_text(
                filename="runtime-health-canary.png",
                mime_type="image/png",
                media_bytes=_OCR_CANARY_PNG,
                source_object_key="runtime-health/canary.png",
            )

    async def run(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        force_stop_event: asyncio.Event | None = None,
        background_runners: Sequence[WorkerBackgroundRunner] = (),
    ) -> None:
        """Start the FastStream broker, declare topology, and block until shutdown."""

        resolved_stop_event = stop_event or asyncio.Event()
        resolved_force_stop_event = force_stop_event or asyncio.Event()
        self._stop_event = resolved_stop_event
        self._force_stop_event = resolved_force_stop_event
        background_tasks: list[asyncio.Task[None]] = []
        if self.health_reporter is not None:
            await self.health_reporter.start()
        try:
            startup_completed = await self._run_startup_until_stopped(
                resolved_stop_event,
                resolved_force_stop_event,
            )
            if not startup_completed:
                return

            if self.health_reporter is not None:
                self.health_reporter.mark_ready()
            background_tasks = [
                asyncio.create_task(
                    self._run_background_runner(runner, resolved_stop_event),
                    name=f"worker-{self.role.value}-background-{index}",
                )
                for index, runner in enumerate(background_runners)
            ]
            await self._wait_for_stop_or_background_failure(resolved_stop_event, background_tasks)
        finally:
            try:
                await self._shutdown(background_tasks)
            finally:
                if self.health_reporter is not None:
                    await self.health_reporter.stop()

    async def _run_startup_until_stopped(
        self,
        stop_event: asyncio.Event,
        force_stop_event: asyncio.Event,
    ) -> bool:
        """Complete broker startup unless graceful or forced shutdown wins the race."""

        if stop_event.is_set() or force_stop_event.is_set():
            _ = self._ensure_shutdown_deadline()
            return False

        connected = await self._connect_broker_until_stopped(
            stop_event,
            force_stop_event,
        )
        if not connected:
            return False

        if stop_event.is_set() or force_stop_event.is_set():
            _ = self._ensure_shutdown_deadline()
            return False

        startup_task = asyncio.create_task(
            self._start_broker_and_verify(),
            name=f"worker-{self.role.value}-startup",
        )
        stop_waiter = asyncio.create_task(
            stop_event.wait(),
            name=f"worker-{self.role.value}-startup-stop-waiter",
        )
        force_waiter = asyncio.create_task(
            force_stop_event.wait(),
            name=f"worker-{self.role.value}-startup-force-stop-waiter",
        )
        try:
            done, _ = await asyncio.wait(
                (startup_task, stop_waiter, force_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            self._cancel_and_track_startup(startup_task)
            raise
        finally:
            for waiter in (stop_waiter, force_waiter):
                if not waiter.done():
                    waiter.cancel()
            _ = await asyncio.gather(stop_waiter, force_waiter, return_exceptions=True)

        if startup_task in done:
            await startup_task
            # A signal can arrive while the completed waiter tasks are being
            # reaped above. There is no await between this check and readiness.
            if not stop_event.is_set() and not force_stop_event.is_set():
                return True

        _ = self._ensure_shutdown_deadline()
        self._cancel_and_track_startup(startup_task)
        return False

    async def _connect_broker_until_stopped(
        self,
        stop_event: asyncio.Event,
        force_stop_event: asyncio.Event,
    ) -> bool:
        """Own the bounded aio-pika connection before allowing startup cancellation."""

        self._broker_start_attempted = True
        connect_task = asyncio.create_task(
            self.broker.connect(),
            name=f"worker-{self.role.value}-broker-connect",
        )
        stop_waiter = asyncio.create_task(
            stop_event.wait(),
            name=f"worker-{self.role.value}-connect-stop-waiter",
        )
        force_waiter = asyncio.create_task(
            force_stop_event.wait(),
            name=f"worker-{self.role.value}-connect-force-stop-waiter",
        )
        try:
            done, _ = await asyncio.wait(
                (connect_task, stop_waiter, force_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            _ = self._ensure_shutdown_deadline()
            await self._await_initial_connect_without_cancelling(connect_task)
            raise
        finally:
            for waiter in (stop_waiter, force_waiter):
                if not waiter.done():
                    waiter.cancel()
            _ = await asyncio.gather(stop_waiter, force_waiter, return_exceptions=True)

        shutdown_requested = stop_event.is_set() or force_stop_event.is_set()
        if connect_task in done and not shutdown_requested:
            await connect_task
            return True

        _ = self._ensure_shutdown_deadline()
        try:
            await self._await_initial_connect_without_cancelling(connect_task)
        except Exception:  # noqa: BLE001 - an intentional stop already won the startup race.
            logger.warning(
                "worker_broker_connect_failed_during_shutdown",
                extra={
                    "event": "worker_broker_connect_failed_during_shutdown",
                    "role": self.role.value,
                },
                exc_info=True,
            )
        return False

    @staticmethod
    async def _await_initial_connect_without_cancelling(connect_task: asyncio.Task[Any]) -> None:
        """Reap robust connect safely, bounded by RabbitBroker's configured connection timeout."""

        while not connect_task.done():
            try:
                await asyncio.shield(connect_task)
            except asyncio.CancelledError:
                if connect_task.done():
                    break
                continue
        await connect_task

    async def _start_broker_and_verify(self) -> None:
        await self.broker.start()
        await self.declare_topology()
        await self.verify_readiness()

    def _cancel_and_track_startup(self, startup_task: asyncio.Task[None]) -> None:
        if self._startup_cleanup_task is not None:
            return
        startup_task.cancel()
        self._startup_cleanup_task = asyncio.create_task(
            self._reap_cancelled_startup(startup_task),
            name=f"worker-{self.role.value}-startup-cleanup",
        )

    async def _reap_cancelled_startup(self, startup_task: asyncio.Task[None]) -> None:
        try:
            await startup_task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - shutdown still needs to close partially started dependencies.
            logger.exception(
                "worker_startup_cancellation_failed",
                extra={"event": "worker_startup_cancellation_failed", "role": self.role.value},
            )

    def _ensure_shutdown_deadline(self) -> float:
        if self._shutdown_deadline is None:
            self._shutdown_deadline = (
                asyncio.get_running_loop().time()
                + self.settings.pipeline_worker_graceful_shutdown_timeout_seconds
            )
        return self._shutdown_deadline

    async def _wait_for_stop_or_background_failure(
        self,
        stop_event: asyncio.Event,
        background_tasks: Sequence[asyncio.Task[None]],
    ) -> None:
        if not background_tasks:
            await stop_event.wait()
            return

        stop_waiter = asyncio.create_task(stop_event.wait(), name=f"worker-{self.role.value}-stop-waiter")
        try:
            done, _ = await asyncio.wait(
                (stop_waiter, *background_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_waiter in done:
                return
            for task in done:
                await task
        finally:
            stop_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await stop_waiter

    async def _shutdown(self, background_tasks: Sequence[asyncio.Task[None]]) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._draining = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self.health_reporter is not None:
            self.health_reporter.mark_draining()

        timeout_seconds = self.settings.pipeline_worker_graceful_shutdown_timeout_seconds
        deadline = self._ensure_shutdown_deadline()
        shutdown_tasks = list(background_tasks)
        if self._startup_cleanup_task is not None and not self._startup_cleanup_task.done():
            shutdown_tasks.append(self._startup_cleanup_task)
        logger.info(
            "worker_shutdown_started",
            extra={
                "event": "worker_shutdown_started",
                "role": self.role.value,
                "timeout_seconds": timeout_seconds,
                "active_deliveries": len(self._active_delivery_tasks),
            },
        )

        drain_completed = False
        shutdown_error: Exception | None = None
        try:
            consumers_quiesced = True
            if self._broker_start_attempted:
                consumers_quiesced = await self._quiesce_consumers(deadline)
            if self._consumer_quiesce_task is not None and not self._consumer_quiesce_task.done():
                shutdown_tasks.append(self._consumer_quiesce_task)
            if consumers_quiesced:
                drain_completed = await self._wait_for_drain(shutdown_tasks, deadline)
        except Exception as exc:  # noqa: BLE001 - cleanup continues before surfacing lifecycle failures.
            shutdown_error = exc
            logger.exception(
                "worker_shutdown_drain_failed",
                extra={"event": "worker_shutdown_drain_failed", "role": self.role.value},
            )

        if not drain_completed:
            await self._cancel_remaining_work(shutdown_tasks)

        await self._close_broker_and_dependencies()

        logger.info(
            "worker_shutdown_completed",
            extra={
                "event": "worker_shutdown_completed",
                "role": self.role.value,
                "drain_completed": drain_completed,
                "active_deliveries": len(self._active_delivery_tasks),
            },
        )
        if shutdown_error is not None:
            raise shutdown_error

    async def _close_broker_and_dependencies(self) -> None:
        if self._broker_start_attempted:
            try:
                async with asyncio.timeout(_DEPENDENCY_CLEANUP_TIMEOUT_SECONDS):
                    await self.broker.stop()
            except Exception:  # noqa: BLE001 - process exit still closes the AMQP connection.
                logger.exception(
                    "worker_shutdown_broker_close_failed",
                    extra={"event": "worker_shutdown_broker_close_failed", "role": self.role.value},
                )

        if self.source_engagement_telegram_session_manager is not None:
            try:
                async with asyncio.timeout(_DEPENDENCY_CLEANUP_TIMEOUT_SECONDS):
                    await self.source_engagement_telegram_session_manager.shutdown()
            except Exception:  # noqa: BLE001 - bounded shutdown must still reach process exit.
                logger.exception(
                    "worker_shutdown_telegram_close_failed",
                    extra={"event": "worker_shutdown_telegram_close_failed", "role": self.role.value},
                )

    async def _quiesce_consumers(self, deadline: float) -> bool:
        subscribers = tuple(self.broker.subscribers)
        if not subscribers:
            self._log_consumers_quiesced(consumer_count=0)
            return True

        quiesce_task = asyncio.create_task(
            self._stop_consumers(subscribers),
            name=f"worker-{self.role.value}-consumer-quiesce",
        )
        self._consumer_quiesce_task = quiesce_task
        force_waiter = asyncio.create_task(
            self._wait_for_force_stop(),
            name=f"worker-{self.role.value}-quiesce-force-stop-waiter",
        )
        try:
            remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
            done, _ = await asyncio.wait(
                (quiesce_task, force_waiter),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if quiesce_task in done:
                try:
                    await quiesce_task
                except Exception:  # noqa: BLE001 - force cancellation and broker close remain available.
                    logger.exception(
                        "worker_consumers_quiesce_failed",
                        extra={
                            "event": "worker_consumers_quiesce_failed",
                            "role": self.role.value,
                            "consumer_count": len(subscribers),
                        },
                    )
                    return False
                self._log_consumers_quiesced(consumer_count=len(subscribers))
                return True

            if force_waiter in done:
                logger.warning(
                    "worker_shutdown_force_requested",
                    extra={"event": "worker_shutdown_force_requested", "role": self.role.value},
                )
            else:
                logger.warning(
                    "worker_consumers_quiesce_timed_out",
                    extra={
                        "event": "worker_consumers_quiesce_timed_out",
                        "role": self.role.value,
                        "consumer_count": len(subscribers),
                    },
                )
            quiesce_task.cancel()
            return False
        finally:
            if not force_waiter.done():
                force_waiter.cancel()
            _ = await asyncio.gather(force_waiter, return_exceptions=True)

    async def _stop_consumers(self, subscribers: Sequence[Any]) -> None:
        results = await asyncio.gather(
            *(subscriber.stop() for subscriber in subscribers),
            return_exceptions=True,
        )
        errors: list[Exception] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                errors.append(RuntimeError("RabbitMQ consumer stop was cancelled."))
            elif isinstance(result, Exception):
                errors.append(result)
        if errors:
            raise ExceptionGroup("One or more RabbitMQ consumers failed to quiesce.", errors)

    def _log_consumers_quiesced(self, *, consumer_count: int) -> None:
        logger.info(
            "worker_consumers_quiesced",
            extra={
                "event": "worker_consumers_quiesced",
                "role": self.role.value,
                "consumer_count": consumer_count,
            },
        )

    async def _wait_for_drain(
        self,
        background_tasks: Sequence[asyncio.Task[None]],
        deadline: float,
    ) -> bool:
        drain_waiter = asyncio.create_task(
            self._wait_for_drain_completion(background_tasks),
            name=f"worker-{self.role.value}-drain-waiter",
        )
        force_waiter = asyncio.create_task(
            self._wait_for_force_stop(),
            name=f"worker-{self.role.value}-force-stop-waiter",
        )
        try:
            remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
            done, _ = await asyncio.wait(
                (drain_waiter, force_waiter),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if drain_waiter in done:
                await drain_waiter
                logger.info(
                    "worker_shutdown_drain_completed",
                    extra={"event": "worker_shutdown_drain_completed", "role": self.role.value},
                )
                return True
            if force_waiter in done:
                logger.warning(
                    "worker_shutdown_force_requested",
                    extra={"event": "worker_shutdown_force_requested", "role": self.role.value},
                )
                return False

            logger.warning(
                "worker_shutdown_drain_timed_out",
                extra={
                    "event": "worker_shutdown_drain_timed_out",
                    "role": self.role.value,
                    "active_deliveries": len(self._active_delivery_tasks),
                },
            )
            return False
        finally:
            for waiter in (drain_waiter, force_waiter):
                if not waiter.done():
                    waiter.cancel()
            # Do not let a cancellation-resistant background runner extend the
            # global drain deadline before broker close/requeue can proceed.
            _ = await asyncio.wait((drain_waiter, force_waiter), timeout=0)

    async def _wait_for_drain_completion(self, background_tasks: Sequence[asyncio.Task[None]]) -> None:
        await asyncio.gather(self._wait_for_active_deliveries(), *background_tasks)

    async def _wait_for_active_deliveries(self) -> None:
        while self._active_delivery_tasks:
            self._active_delivery_changed.clear()
            if not self._active_delivery_tasks:
                return
            await self._active_delivery_changed.wait()

    async def _wait_for_force_stop(self) -> None:
        if self._force_stop_event is None:
            await asyncio.Future()
        else:
            await self._force_stop_event.wait()

    async def _cancel_remaining_work(self, background_tasks: Sequence[asyncio.Task[None]]) -> None:
        tasks = {
            task
            for task in (*background_tasks, *self._active_delivery_tasks)
            if not task.done()
        }
        for task in tasks:
            task.cancel()
        if not tasks:
            return

        completed, pending = await asyncio.wait(tasks, timeout=_FORCED_TASK_CLEANUP_TIMEOUT_SECONDS)
        for task in completed:
            try:
                _ = task.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown continues after recording cleanup failures.
                logger.exception(
                    "worker_shutdown_task_cleanup_failed",
                    extra={
                        "event": "worker_shutdown_task_cleanup_failed",
                        "role": self.role.value,
                        "task": task.get_name(),
                    },
                )
        if pending:
            logger.error(
                "worker_shutdown_task_cleanup_timed_out",
                extra={
                    "event": "worker_shutdown_task_cleanup_timed_out",
                    "role": self.role.value,
                    "remaining_tasks": len(pending),
                },
            )

    async def _run_background_runner(
        self,
        runner: WorkerBackgroundRunner,
        stop_event: asyncio.Event,
    ) -> None:
        await runner(self.settings, stop_event)
        if not stop_event.is_set():
            raise RuntimeError(f"Worker role {self.role.value!r} background runner exited before shutdown.")

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

    def _source_engagement_capture_effective_attempt(
        self,
        capture_event: SourceEngagementCaptureRequestedEvent,
        message: RabbitMessageLike,
    ) -> int:
        retry_queue_name = self.broker_settings.source_engagement_capture_retry_queue_for_session(
            capture_event.session_key,
        )
        return max(
            1 + self._retry_cycle_count_for_queue(retry_queue_name, message.headers),
            1,
        )

    def _source_engagement_session_key_for_queue(self, queue_name: str) -> str:
        prefix = f"{self.broker_settings.source_engagement_capture_queue}."
        if not queue_name.startswith(prefix):
            raise ValueError(f"Unexpected source engagement queue name {queue_name!r}.")
        return queue_name.removeprefix(prefix)

    def _source_engagement_session_key_for_retry_queue(self, queue_name: str) -> str:
        prefix = f"{self.broker_settings.source_engagement_capture_queue}."
        suffix = ".retry"
        if not queue_name.startswith(prefix) or not queue_name.endswith(suffix):
            raise ValueError(f"Unexpected source engagement retry queue name {queue_name!r}.")
        return queue_name.removeprefix(prefix).removesuffix(suffix)

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
            _ = await self.dead_letter_recorder(
                session_factory=self.session_factory,
                payload=payload,
                headers=message.headers,
                broker_message_id=message.message_id,
                normalized_reason=normalized_reason,
            )
        except Exception:
            logger.exception(
                "pipeline_dead_letter_persistence_failed",
                extra={
                    "event": "pipeline_dead_letter_persistence_failed",
                    "message_id": message.message_id,
                    "normalized_reason": normalized_reason,
                },
            )
            await message.nack(requeue=True)
            return

        try:
            # Keep publishing to the legacy RabbitMQ DLQ during the PostgreSQL-ledger transition.
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
            logger.exception(
                "pipeline_legacy_dead_letter_publish_failed",
                extra={
                    "event": "pipeline_legacy_dead_letter_publish_failed",
                    "message_id": message.message_id,
                    "normalized_reason": normalized_reason,
                },
            )

        await message.ack()


__all__ = [
    "DeadLetterRecorder",
    "PipelineRuntime",
    "RabbitMessageLike",
]
