# ruff: noqa: TC001,TC002,TC003
"""Shared dependency context for focused pipeline stage handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from memexpert.core.classification import ClassificationClientProtocol
from memexpert.core.config import Settings
from memexpert.core.database import AsyncSessionFactory
from memexpert.core.meilisearch import MeilisearchSyncClientProtocol
from memexpert.core.ocr import OCRProcessorProtocol
from memexpert.core.qdrant import QdrantSimilarityClientProtocol, QdrantSyncClientProtocol
from memexpert.core.storage import download_object_bytes, get_pipeline_storage_settings
from memexpert.core.voyage import VoyageClientProtocol
from memexpert.media.contracts import PipelineMediaProcessorProtocol
from memexpert.messaging.rabbitmq_outbox import RabbitBrokerProtocol
from memexpert.pipeline.dispatch import PipelineStageWorkContext
from memexpert.services import PipelineIngestError


class ObjectStorageClientLike(Protocol):
    """Small S3-compatible surface used by pipeline worker stage handlers."""

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


@dataclass(frozen=True, slots=True)
class PipelineStageHandlerContext:
    """Injected runtime dependencies available to focused stage handlers."""

    settings: Settings
    session_factory: AsyncSessionFactory
    storage_client: ObjectStorageClientLike
    media_processor: PipelineMediaProcessorProtocol
    ocr_processor: OCRProcessorProtocol
    voyage_client: VoyageClientProtocol
    qdrant_client: QdrantSimilarityClientProtocol
    qdrant_sync_client: QdrantSyncClientProtocol
    meilisearch_sync_client: MeilisearchSyncClientProtocol
    classification_client: ClassificationClientProtocol
    broker: RabbitBrokerProtocol | None = None


async def load_preview_frame(
    context: PipelineStageHandlerContext,
    stage_context: PipelineStageWorkContext,
) -> bytes:
    """Load source media and extract the PNG preview used by image-only stages."""

    source_object_key = stage_context.web_video_object_key or stage_context.original_object_key
    source_mime_type = stage_context.mime_type
    if source_mime_type is None:
        raise PipelineIngestError("Pipeline item is missing the media type required for embed/classify work.")

    storage_settings = get_pipeline_storage_settings(context.settings)
    source_bytes = await download_object_bytes(
        context.storage_client,
        bucket=storage_settings.bucket,
        key=source_object_key,
    )
    return await context.media_processor.extract_preview_frame(
        filename=PurePosixPath(source_object_key).name,
        content_type=source_mime_type,
        media_bytes=source_bytes,
    )


__all__ = [
    "ObjectStorageClientLike",
    "PipelineStageHandlerContext",
    "load_preview_frame",
]
