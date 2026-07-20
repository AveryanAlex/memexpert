# ruff: noqa: TC002
"""RabbitMQ topology builders and the factory that wires up the pipeline runtime.

The FastStream subscribers defined inside ``build_pipeline_runtime`` are the
only place where the queue objects on the ``PipelineRuntime`` dataclass are
bound to the broker. Keeping the factory next to the exchange/queue builders
makes the topology easy to audit: one file shows every queue, every retry
queue, and every routing-key binding used by the heavy-worker stages.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING, cast

from faststream import AckPolicy
from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage
from sqlalchemy import select

from memexpert.core.broker import PipelineBrokerSettings, build_pipeline_broker, get_pipeline_broker_settings
from memexpert.core.classification import (
    ClassificationClientProtocol,
    build_pipeline_classification_client,
)
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import AsyncSessionFactory, get_async_session_factory
from memexpert.core.meilisearch import (
    MeilisearchSyncClientProtocol,
    PipelineMeilisearchSyncClient,
)
from memexpert.core.ocr import OCRProcessorProtocol, build_pipeline_ocr_processor
from memexpert.core.qdrant import (
    PipelineQdrantClient,
    PipelineQdrantSyncClient,
    QdrantSimilarityClientProtocol,
    QdrantSyncClientProtocol,
)
from memexpert.core.storage import get_s3_client
from memexpert.core.voyage import VoyageClientProtocol, build_pipeline_voyage_client
from memexpert.crawlers.telegram.manager import TelegramSessionManager
from memexpert.media.inspect import PipelineMediaProcessor
from memexpert.models.content import TelegramSession
from memexpert.models.enums import ContentPipelineStage
from memexpert.pipeline.events import build_source_engagement_session_key
from memexpert.runtime_health import RuntimeHealthReporter
from memexpert.services.pipeline_reliability import record_pipeline_dead_letter
from memexpert.services.source_channel_audience_capture import (
    build_pipeline_source_channel_audience_telegram_client_factory,
)
from memexpert.services.source_engagement_capture import (
    build_pipeline_source_engagement_telegram_client_factory,
)
from memexpert.workers.pipeline_runtime.runtime import DeadLetterRecorder, PipelineRuntime, RabbitMessageLike
from memexpert.workers.roles import WorkerRole

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from memexpert.media.contracts import PipelineMediaProcessorProtocol
    from memexpert.services.source_channel_audience_capture import SourceChannelAudienceTelegramClientFactory
    from memexpert.services.source_engagement_capture import SourceEngagementTelegramClientFactory
    from memexpert.workers.pipeline_runtime.stages.context import ObjectStorageClientLike

type WorkerBackgroundRunner = Callable[[Settings, asyncio.Event], Awaitable[None]]

logger = logging.getLogger(__name__)

_PRE_RUNTIME_CLEANUP_TIMEOUT_SECONDS = 5.0


class _UnavailableDependency:
    """Fail loudly if role isolation ever routes work to an uninitialized provider."""

    def __init__(self, *, role: WorkerRole, name: str) -> None:
        self._role = role
        self._name = name

    def __getattr__(self, attribute: str) -> object:
        raise RuntimeError(
            f"Worker role {self._role.value!r} does not initialize dependency {self._name!r} "
            f"(attempted attribute {attribute!r})."
        )

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"Worker role {self._role.value!r} does not initialize dependency {self._name!r}.")


def _resolve_dependency[DependencyT](
    explicit: DependencyT | None,
    *,
    enabled: bool,
    factory: Callable[[], DependencyT],
    role: WorkerRole,
    name: str,
) -> DependencyT:
    if explicit is not None:
        return explicit
    if enabled:
        return factory()
    return cast("DependencyT", _UnavailableDependency(role=role, name=name))


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


def _build_source_engagement_capture_queue(
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
            "x-single-active-consumer": True,
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
    role: WorkerRole = WorkerRole.ALL,
    broker: RabbitBroker | None = None,
    session_factory: AsyncSessionFactory | None = None,
    storage_client: ObjectStorageClientLike | None = None,
    media_processor: PipelineMediaProcessorProtocol | None = None,
    ocr_processor: OCRProcessorProtocol | None = None,
    voyage_client: VoyageClientProtocol | None = None,
    qdrant_client: QdrantSimilarityClientProtocol | None = None,
    qdrant_sync_client: QdrantSyncClientProtocol | None = None,
    meilisearch_sync_client: MeilisearchSyncClientProtocol | None = None,
    classification_client: ClassificationClientProtocol | None = None,
    source_engagement_telegram_client_factory: SourceEngagementTelegramClientFactory | None = None,
    source_engagement_close_telegram_client_after_capture: bool | None = None,
    source_channel_audience_telegram_client_factory: SourceChannelAudienceTelegramClientFactory | None = None,
    source_channel_audience_close_telegram_client_after_capture: bool | None = None,
    source_engagement_telegram_session_manager: TelegramSessionManager | None = None,
    source_engagement_session_keys: Sequence[str] = (),
    health_reporter: RuntimeHealthReporter | None = None,
    dead_letter_recorder: DeadLetterRecorder | None = None,
) -> PipelineRuntime:
    """Build the RabbitMQ heavy-worker runtime and register its FastStream subscribers."""

    resolved_settings = settings or get_settings()
    resolved_role = WorkerRole(role)
    resolved_broker_settings = get_pipeline_broker_settings(resolved_settings)
    resolved_broker = broker or build_pipeline_broker(resolved_settings)
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_storage_client = _resolve_dependency(
        storage_client,
        enabled=resolved_role.needs_storage,
        factory=lambda: cast("ObjectStorageClientLike", get_s3_client()),
        role=resolved_role,
        name="storage_client",
    )
    resolved_media_processor = _resolve_dependency(
        media_processor,
        enabled=resolved_role.needs_media_processor,
        factory=lambda: PipelineMediaProcessor(settings=resolved_settings),
        role=resolved_role,
        name="media_processor",
    )
    resolved_ocr_processor = _resolve_dependency(
        ocr_processor,
        enabled=resolved_role.needs_ocr,
        factory=lambda: build_pipeline_ocr_processor(
            settings=resolved_settings,
            media_processor=resolved_media_processor,
        ),
        role=resolved_role,
        name="ocr_processor",
    )
    resolved_voyage_client = _resolve_dependency(
        voyage_client,
        enabled=resolved_role.needs_enrichment,
        factory=lambda: build_pipeline_voyage_client(settings=resolved_settings),
        role=resolved_role,
        name="voyage_client",
    )
    resolved_qdrant_client = _resolve_dependency(
        qdrant_client,
        enabled=resolved_role.needs_enrichment,
        factory=lambda: PipelineQdrantClient(settings=resolved_settings),
        role=resolved_role,
        name="qdrant_client",
    )
    resolved_qdrant_sync_client = _resolve_dependency(
        qdrant_sync_client,
        enabled=resolved_role.needs_sync,
        factory=lambda: PipelineQdrantSyncClient(settings=resolved_settings),
        role=resolved_role,
        name="qdrant_sync_client",
    )
    resolved_meilisearch_sync_client = _resolve_dependency(
        meilisearch_sync_client,
        enabled=resolved_role.needs_sync,
        factory=lambda: PipelineMeilisearchSyncClient(settings=resolved_settings),
        role=resolved_role,
        name="meilisearch_sync_client",
    )
    resolved_classification_client = _resolve_dependency(
        classification_client,
        enabled=resolved_role.needs_enrichment,
        factory=lambda: build_pipeline_classification_client(settings=resolved_settings),
        role=resolved_role,
        name="classification_client",
    )
    needs_shared_telegram_manager = resolved_role.consumes_source_engagement and (
        source_engagement_telegram_client_factory is None
        or source_channel_audience_telegram_client_factory is None
    )
    resolved_source_engagement_telegram_session_manager = source_engagement_telegram_session_manager
    if resolved_source_engagement_telegram_session_manager is None and needs_shared_telegram_manager:
        resolved_source_engagement_telegram_session_manager = TelegramSessionManager(
            settings=resolved_settings,
            session_factory=resolved_session_factory,
        )

    if source_engagement_telegram_client_factory is None and resolved_role.consumes_source_engagement:
        if resolved_source_engagement_telegram_session_manager is None:  # pragma: no cover - guarded above.
            raise RuntimeError("source engagement capture requires a Telegram session manager.")
        resolved_source_engagement_telegram_client_factory = build_pipeline_source_engagement_telegram_client_factory(
            resolved_settings,
            session_manager=resolved_source_engagement_telegram_session_manager,
        )
        resolved_source_engagement_close_telegram_client_after_capture = False
    elif source_engagement_telegram_client_factory is not None:
        resolved_source_engagement_telegram_client_factory = source_engagement_telegram_client_factory
        resolved_source_engagement_close_telegram_client_after_capture = True
    else:
        resolved_source_engagement_telegram_client_factory = cast(
            "SourceEngagementTelegramClientFactory",
            _UnavailableDependency(role=resolved_role, name="source_engagement_telegram_client_factory"),
        )
        resolved_source_engagement_close_telegram_client_after_capture = False
    if source_engagement_close_telegram_client_after_capture is not None:
        resolved_source_engagement_close_telegram_client_after_capture = (
            source_engagement_close_telegram_client_after_capture
        )
    if source_channel_audience_telegram_client_factory is None and resolved_role.consumes_source_engagement:
        if resolved_source_engagement_telegram_session_manager is None:  # pragma: no cover - guarded above.
            raise RuntimeError("source channel audience capture requires a Telegram session manager.")
        resolved_source_channel_audience_telegram_client_factory = (
            build_pipeline_source_channel_audience_telegram_client_factory(
                resolved_settings,
                session_manager=resolved_source_engagement_telegram_session_manager,
            )
        )
        resolved_source_channel_audience_close_telegram_client_after_capture = False
    elif source_channel_audience_telegram_client_factory is not None:
        resolved_source_channel_audience_telegram_client_factory = (
            source_channel_audience_telegram_client_factory
        )
        resolved_source_channel_audience_close_telegram_client_after_capture = True
    else:
        resolved_source_channel_audience_telegram_client_factory = cast(
            "SourceChannelAudienceTelegramClientFactory",
            _UnavailableDependency(
                role=resolved_role,
                name="source_channel_audience_telegram_client_factory",
            ),
        )
        resolved_source_channel_audience_close_telegram_client_after_capture = False
    if source_channel_audience_close_telegram_client_after_capture is not None:
        resolved_source_channel_audience_close_telegram_client_after_capture = (
            source_channel_audience_close_telegram_client_after_capture
        )
    resolved_source_engagement_session_keys = (
        tuple(dict.fromkeys(source_engagement_session_keys)) if resolved_role.consumes_source_engagement else ()
    )
    transcode_retry_queue_name = f"{resolved_broker_settings.transcode_queue}.retry"
    ocr_retry_queue_name = f"{resolved_broker_settings.ocr_queue}.retry"
    embed_retry_queue_name = f"{resolved_broker_settings.embed_queue}.retry"
    classify_retry_queue_name = f"{resolved_broker_settings.classify_queue}.retry"
    sync_qdrant_retry_queue_name = f"{resolved_broker_settings.sync_qdrant_queue}.retry"
    sync_meili_retry_queue_name = f"{resolved_broker_settings.sync_meili_queue}.retry"
    media_inspect_retry_queue_name = f"{resolved_broker_settings.media_inspect_queue}.retry"
    embed_retry_request_routing_key = resolved_broker_settings.retry_queue_routing_key_for_stage(
        ContentPipelineStage.EMBED,
    )
    classify_retry_request_routing_key = resolved_broker_settings.retry_queue_routing_key_for_stage(
        ContentPipelineStage.CLASSIFY,
    )
    sync_qdrant_retry_request_routing_key = resolved_broker_settings.retry_queue_routing_key_for_stage(
        ContentPipelineStage.SYNC_QDRANT,
    )
    sync_meili_retry_request_routing_key = resolved_broker_settings.retry_queue_routing_key_for_stage(
        ContentPipelineStage.SYNC_MEILI,
    )
    embed_retry_return_routing_key = resolved_broker_settings.retry_return_routing_key_for_stage(
        ContentPipelineStage.EMBED,
    )
    classify_retry_return_routing_key = resolved_broker_settings.retry_return_routing_key_for_stage(
        ContentPipelineStage.CLASSIFY,
    )
    sync_qdrant_retry_return_routing_key = resolved_broker_settings.retry_return_routing_key_for_stage(
        ContentPipelineStage.SYNC_QDRANT,
    )
    sync_meili_retry_return_routing_key = resolved_broker_settings.retry_return_routing_key_for_stage(
        ContentPipelineStage.SYNC_MEILI,
    )

    runtime = PipelineRuntime(
        role=resolved_role,
        settings=resolved_settings,
        broker=resolved_broker,
        session_factory=resolved_session_factory,
        broker_settings=resolved_broker_settings,
        pipeline_exchange=_build_pipeline_exchange(resolved_broker_settings),
        retry_exchange=_build_retry_exchange(resolved_broker_settings),
        dead_letter_exchange=_build_dead_letter_exchange(resolved_broker_settings),
        media_inspect_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.media_inspect_queue,
            routing_key=resolved_broker_settings.media_inspect_routing_key,
            retry_request_routing_key=resolved_broker_settings.media_inspect_retry_request_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        source_engagement_capture_queues=tuple(
            _build_source_engagement_capture_queue(
                queue_name=resolved_broker_settings.source_engagement_capture_queue_for_session(session_key),
                routing_key=resolved_broker_settings.source_engagement_capture_binding_key_for_session(session_key),
                retry_request_routing_key=(
                    resolved_broker_settings.source_engagement_capture_retry_request_routing_key_for_session(
                        session_key
                    )
                ),
                retry_exchange=resolved_broker_settings.retry_exchange,
            )
            for session_key in resolved_source_engagement_session_keys
        ),
        source_channel_audience_capture_queues=tuple(
            _build_source_engagement_capture_queue(
                queue_name=(
                    resolved_broker_settings.source_channel_audience_capture_queue_for_session(session_key)
                ),
                routing_key=(
                    resolved_broker_settings.source_channel_audience_capture_binding_key_for_session(session_key)
                ),
                retry_request_routing_key=(
                    resolved_broker_settings.source_channel_audience_capture_retry_request_routing_key_for_session(
                        session_key,
                    )
                ),
                retry_exchange=resolved_broker_settings.retry_exchange,
            )
            for session_key in resolved_source_engagement_session_keys
        ),
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
        sync_qdrant_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.sync_qdrant_queue,
            routing_key=resolved_broker_settings.sync_qdrant_routing_key,
            retry_request_routing_key=sync_qdrant_retry_request_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        sync_meili_queue=_build_stage_queue(
            queue_name=resolved_broker_settings.sync_meili_queue,
            routing_key=resolved_broker_settings.sync_meili_routing_key,
            retry_request_routing_key=sync_meili_retry_request_routing_key,
            retry_exchange=resolved_broker_settings.retry_exchange,
        ),
        media_inspect_retry_queue=_build_retry_queue(
            queue_name=media_inspect_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=resolved_broker_settings.media_inspect_retry_routing_key,
        ),
        source_engagement_capture_retry_queues=tuple(
            _build_retry_queue(
                queue_name=resolved_broker_settings.source_engagement_capture_retry_queue_for_session(session_key),
                retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
                exchange=resolved_broker_settings.exchange,
                retry_return_routing_key=(
                    resolved_broker_settings.source_engagement_capture_retry_routing_key_for_session(session_key)
                ),
            )
            for session_key in resolved_source_engagement_session_keys
        ),
        source_channel_audience_capture_retry_queues=tuple(
            _build_retry_queue(
                queue_name=(
                    resolved_broker_settings.source_channel_audience_capture_retry_queue_for_session(session_key)
                ),
                retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
                exchange=resolved_broker_settings.exchange,
                retry_return_routing_key=(
                    resolved_broker_settings.source_channel_audience_capture_retry_routing_key_for_session(
                        session_key,
                    )
                ),
            )
            for session_key in resolved_source_engagement_session_keys
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
        sync_qdrant_retry_queue=_build_retry_queue(
            queue_name=sync_qdrant_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=sync_qdrant_retry_return_routing_key,
        ),
        sync_meili_retry_queue=_build_retry_queue(
            queue_name=sync_meili_retry_queue_name,
            retry_backoff_milliseconds=resolved_broker_settings.retry_backoff_milliseconds,
            exchange=resolved_broker_settings.exchange,
            retry_return_routing_key=sync_meili_retry_return_routing_key,
        ),
        dead_letter_queue=_build_dead_letter_queue(resolved_broker_settings),
        storage_client=resolved_storage_client,
        media_processor=resolved_media_processor,
        ocr_processor=resolved_ocr_processor,
        voyage_client=resolved_voyage_client,
        qdrant_client=resolved_qdrant_client,
        qdrant_sync_client=resolved_qdrant_sync_client,
        meilisearch_sync_client=resolved_meilisearch_sync_client,
        classification_client=resolved_classification_client,
        source_engagement_telegram_client_factory=resolved_source_engagement_telegram_client_factory,
        source_engagement_close_telegram_client_after_capture=(
            resolved_source_engagement_close_telegram_client_after_capture
        ),
        source_channel_audience_telegram_client_factory=(
            resolved_source_channel_audience_telegram_client_factory
        ),
        source_channel_audience_close_telegram_client_after_capture=(
            resolved_source_channel_audience_close_telegram_client_after_capture
        ),
        dead_letter_recorder=dead_letter_recorder or record_pipeline_dead_letter,
        source_engagement_telegram_session_manager=resolved_source_engagement_telegram_session_manager,
        health_reporter=health_reporter,
    )

    def worker_channel() -> Channel:
        return Channel(prefetch_count=resolved_settings.pipeline_worker_prefetch_count)

    def consume_args() -> dict[str, str]:
        return resolved_role.consumer_arguments()

    if resolved_role.consumes_media_inspect:

        @resolved_broker.subscriber(
            runtime.media_inspect_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_media_inspect(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("media_inspect") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_media_inspect_message(payload, rabbit_message)

    if resolved_role.consumes_source_engagement:
        for source_engagement_capture_queue in runtime.source_engagement_capture_queues:

            @resolved_broker.subscriber(
                source_engagement_capture_queue,
                runtime.pipeline_exchange,
                channel=worker_channel(),
                consume_args=consume_args(),
                ack_policy=AckPolicy.MANUAL,
            )
            async def _consume_source_engagement_capture(payload: object, message: RabbitMessage) -> None:
                rabbit_message = cast("RabbitMessageLike", cast("object", message))
                async with runtime.consumer_operation("source_engagement_capture") as admitted:
                    if not admitted:
                        await rabbit_message.nack(requeue=True)
                        return
                    await runtime.handle_source_engagement_capture_message(payload, rabbit_message)

        for audience_capture_queue in runtime.source_channel_audience_capture_queues:

            @resolved_broker.subscriber(
                audience_capture_queue,
                runtime.pipeline_exchange,
                channel=worker_channel(),
                consume_args=consume_args(),
                ack_policy=AckPolicy.MANUAL,
            )
            async def _consume_source_channel_audience_capture(payload: object, message: RabbitMessage) -> None:
                rabbit_message = cast("RabbitMessageLike", cast("object", message))
                async with runtime.consumer_operation("source_channel_audience_capture") as admitted:
                    if not admitted:
                        await rabbit_message.nack(requeue=True)
                        return
                    await runtime.handle_source_channel_audience_capture_message(payload, rabbit_message)

    if resolved_role.consumes_stage(ContentPipelineStage.TRANSCODE):

        @resolved_broker.subscriber(
            runtime.transcode_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_transcode(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("transcode") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_transcode_message(payload, rabbit_message)

    if resolved_role.consumes_stage(ContentPipelineStage.OCR):

        @resolved_broker.subscriber(
            runtime.ocr_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_ocr(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("ocr") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_ocr_message(payload, rabbit_message)

    if resolved_role.consumes_stage(ContentPipelineStage.EMBED):

        @resolved_broker.subscriber(
            runtime.embed_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_embed(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("embed") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_embed_message(payload, rabbit_message)

    if resolved_role.consumes_stage(ContentPipelineStage.CLASSIFY):

        @resolved_broker.subscriber(
            runtime.classify_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_classify(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("classify") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_classify_message(payload, rabbit_message)

    if resolved_role.consumes_stage(ContentPipelineStage.SYNC_QDRANT):

        @resolved_broker.subscriber(
            runtime.sync_qdrant_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_sync_qdrant(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("sync_qdrant") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_sync_qdrant_message(payload, rabbit_message)

    if resolved_role.consumes_stage(ContentPipelineStage.SYNC_MEILI):

        @resolved_broker.subscriber(
            runtime.sync_meili_queue,
            runtime.pipeline_exchange,
            channel=worker_channel(),
            consume_args=consume_args(),
            ack_policy=AckPolicy.MANUAL,
        )
        async def _consume_sync_meili(payload: object, message: RabbitMessage) -> None:
            rabbit_message = cast("RabbitMessageLike", cast("object", message))
            async with runtime.consumer_operation("sync_meili") as admitted:
                if not admitted:
                    await rabbit_message.nack(requeue=True)
                    return
                await runtime.handle_sync_meili_message(payload, rabbit_message)

    return runtime


async def _load_source_engagement_session_keys(session_factory: AsyncSessionFactory) -> tuple[str, ...]:
    async with session_factory() as session:
        result = await session.execute(
            select(TelegramSession.id, TelegramSession.name)
            .where(
                TelegramSession.enabled.is_(True),
                TelegramSession.engagement_enabled.is_(True),
            )
            .order_by(TelegramSession.name.asc(), TelegramSession.id.asc())
        )
        return tuple(
            build_source_engagement_session_key(session_id, session_name) for session_id, session_name in result.all()
        )


async def _load_source_engagement_session_keys_until_stopped(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings,
    role: WorkerRole,
    stop_event: asyncio.Event,
    force_stop_event: asyncio.Event,
) -> tuple[str, ...] | None:
    """Bound pre-runtime DB discovery and let process signals cancel it."""

    if stop_event.is_set() or force_stop_event.is_set():
        return None

    load_task = asyncio.create_task(
        _load_source_engagement_session_keys(session_factory),
        name=f"worker-{role.value}-source-session-discovery",
    )
    stop_waiter = asyncio.create_task(
        stop_event.wait(),
        name=f"worker-{role.value}-source-session-stop-waiter",
    )
    force_waiter = asyncio.create_task(
        force_stop_event.wait(),
        name=f"worker-{role.value}-source-session-force-stop-waiter",
    )
    try:
        done, _ = await asyncio.wait(
            (load_task, stop_waiter, force_waiter),
            timeout=settings.database_connect_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        shutdown_requested = stop_event.is_set() or force_stop_event.is_set()
        if load_task in done:
            if shutdown_requested:
                _ = await asyncio.gather(load_task, return_exceptions=True)
                return None
            return await load_task

        load_task.cancel()
        completed, _ = await asyncio.wait(
            (load_task,),
            timeout=_PRE_RUNTIME_CLEANUP_TIMEOUT_SECONDS,
        )
        if completed:
            _ = await asyncio.gather(load_task, return_exceptions=True)
        else:
            logger.error(
                "worker_source_session_discovery_cleanup_timed_out",
                extra={
                    "event": "worker_source_session_discovery_cleanup_timed_out",
                    "role": role.value,
                },
            )

        if stop_event.is_set() or force_stop_event.is_set():
            return None
        raise TimeoutError(
            "Timed out while discovering Telegram source-engagement sessions "
            f"after {settings.database_connect_timeout_seconds:.2f}s."
        )
    finally:
        for waiter in (stop_waiter, force_waiter):
            if not waiter.done():
                waiter.cancel()
        _ = await asyncio.gather(stop_waiter, force_waiter, return_exceptions=True)


def _load_role_background_runners(role: WorkerRole) -> tuple[WorkerBackgroundRunner, ...]:
    if role not in {WorkerRole.TELEGRAM, WorkerRole.ALL}:
        return ()
    try:
        recovery_runtime = importlib.import_module("memexpert.services.recovery_runtime")
    except ModuleNotFoundError as exc:
        if exc.name == "memexpert.services.recovery_runtime":
            return ()
        raise
    run_telegram_recovery_loop = cast(
        "WorkerBackgroundRunner",
        recovery_runtime.run_telegram_recovery_loop,
    )
    return (run_telegram_recovery_loop,)


async def run_pipeline_runtime(
    *,
    settings: Settings | None = None,
    role: WorkerRole = WorkerRole.ALL,
    stop_event: asyncio.Event | None = None,
    force_stop_event: asyncio.Event | None = None,
    background_runners: Sequence[WorkerBackgroundRunner] = (),
) -> None:
    """Start the real RabbitMQ-backed content-pipeline worker runtime."""

    resolved_settings = settings or get_settings()
    resolved_role = WorkerRole(role)
    resolved_stop_event = stop_event or asyncio.Event()
    resolved_force_stop_event = force_stop_event or asyncio.Event()
    session_factory = get_async_session_factory()
    health_reporter = RuntimeHealthReporter.from_settings(
        resolved_settings,
        service="memexpert-workers",
        role=resolved_role.value,
    )
    runtime: PipelineRuntime | None = None
    health_prestarted = False
    runtime_manages_health = False
    try:
        if resolved_role.consumes_source_engagement:
            await health_reporter.start()
            health_prestarted = True
            source_engagement_session_keys = await _load_source_engagement_session_keys_until_stopped(
                session_factory,
                settings=resolved_settings,
                role=resolved_role,
                stop_event=resolved_stop_event,
                force_stop_event=resolved_force_stop_event,
            )
            if source_engagement_session_keys is None:
                health_reporter.mark_draining()
                return
        else:
            source_engagement_session_keys = ()

        runtime = build_pipeline_runtime(
            settings=resolved_settings,
            role=resolved_role,
            session_factory=session_factory,
            source_engagement_session_keys=source_engagement_session_keys,
            health_reporter=health_reporter,
        )
        resolved_background_runners = (*_load_role_background_runners(resolved_role), *background_runners)
        runtime_manages_health = True
        await runtime.run(
            stop_event=resolved_stop_event,
            force_stop_event=resolved_force_stop_event,
            background_runners=resolved_background_runners,
        )
    finally:
        if health_prestarted and not runtime_manages_health:
            await health_reporter.stop()


__all__ = ["WorkerBackgroundRunner", "build_pipeline_runtime", "run_pipeline_runtime"]
