# ruff: noqa: TC002
"""RabbitMQ topology builders and the factory that wires up the pipeline runtime.

The FastStream subscribers defined inside ``build_pipeline_runtime`` are the
only place where the queue objects on the ``PipelineRuntime`` dataclass are
bound to the broker. Keeping the factory next to the exchange/queue builders
makes the topology easy to audit: one file shows every queue, every retry
queue, and every routing-key binding used by the heavy-worker stages.
"""

from __future__ import annotations

from typing import cast

from faststream import AckPolicy
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage

from memexpert.core.broker import PipelineBrokerSettings, get_pipeline_broker, get_pipeline_broker_settings
from memexpert.core.classification import (
    ClassificationClientProtocol,
    build_pipeline_classification_client,
)
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import AsyncSessionFactory, get_async_session_factory
from memexpert.core.media import PipelineMediaProcessor, PipelineMediaProcessorProtocol
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
from memexpert.models.enums import ContentPipelineStage
from memexpert.workers.pipeline_runtime.runtime import (
    ObjectStorageClientLike,
    PipelineRuntime,
    RabbitMessageLike,
)


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
    qdrant_sync_client: QdrantSyncClientProtocol | None = None,
    meilisearch_sync_client: MeilisearchSyncClientProtocol | None = None,
    classification_client: ClassificationClientProtocol | None = None,
) -> PipelineRuntime:
    """Build the RabbitMQ heavy-worker runtime and register its FastStream subscribers."""

    resolved_settings = settings or get_settings()
    resolved_broker_settings = get_pipeline_broker_settings(resolved_settings)
    resolved_broker = broker or get_pipeline_broker()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_storage_client = storage_client or cast("ObjectStorageClientLike", get_s3_client())
    resolved_media_processor = media_processor or PipelineMediaProcessor(settings=resolved_settings)
    resolved_ocr_processor = ocr_processor or build_pipeline_ocr_processor(
        settings=resolved_settings,
        media_processor=resolved_media_processor,
    )
    resolved_voyage_client = voyage_client or build_pipeline_voyage_client(settings=resolved_settings)
    resolved_qdrant_client = qdrant_client or PipelineQdrantClient(settings=resolved_settings)
    resolved_qdrant_sync_client = qdrant_sync_client or PipelineQdrantSyncClient(settings=resolved_settings)
    resolved_meilisearch_sync_client = meilisearch_sync_client or PipelineMeilisearchSyncClient(
        settings=resolved_settings,
    )
    resolved_classification_client = classification_client or build_pipeline_classification_client(
        settings=resolved_settings,
    )
    transcode_retry_queue_name = f"{resolved_broker_settings.transcode_queue}.retry"
    ocr_retry_queue_name = f"{resolved_broker_settings.ocr_queue}.retry"
    embed_retry_queue_name = f"{resolved_broker_settings.embed_queue}.retry"
    classify_retry_queue_name = f"{resolved_broker_settings.classify_queue}.retry"
    sync_qdrant_retry_queue_name = f"{resolved_broker_settings.sync_qdrant_queue}.retry"
    sync_meili_retry_queue_name = f"{resolved_broker_settings.sync_meili_queue}.retry"
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

    @resolved_broker.subscriber(
        runtime.sync_qdrant_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_sync_qdrant(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_sync_qdrant_message(payload, rabbit_message)

    @resolved_broker.subscriber(
        runtime.sync_meili_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_sync_meili(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_sync_meili_message(payload, rabbit_message)

    return runtime


async def run_pipeline_runtime(*, settings: Settings | None = None) -> None:
    """Start the real RabbitMQ-backed content-pipeline worker runtime."""

    runtime = build_pipeline_runtime(settings=settings)
    await runtime.run()


__all__ = ["build_pipeline_runtime", "run_pipeline_runtime"]
