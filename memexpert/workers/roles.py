"""Worker-role contracts for isolating pipeline dependencies and consumers."""

from __future__ import annotations

from enum import StrEnum

from memexpert.models.enums import ContentPipelineStage

RABBITMQ_WORKER_ROLE_ARGUMENT = "x-memexpert-worker-role"


class WorkerRole(StrEnum):
    """Deployable worker roles backed by the shared worker image."""

    ALL = "all"
    MEDIA = "media"
    OCR = "ocr"
    ENRICHMENT = "enrichment"
    SYNC = "sync"
    TELEGRAM = "telegram"

    @property
    def stages(self) -> frozenset[ContentPipelineStage]:
        """Return the durable pipeline stages consumed by this role."""

        if self is WorkerRole.MEDIA:
            return frozenset({ContentPipelineStage.TRANSCODE})
        if self is WorkerRole.OCR:
            return frozenset({ContentPipelineStage.OCR})
        if self is WorkerRole.ENRICHMENT:
            return frozenset({ContentPipelineStage.EMBED, ContentPipelineStage.CLASSIFY})
        if self is WorkerRole.SYNC:
            return frozenset({ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI})
        if self is WorkerRole.TELEGRAM:
            return frozenset()
        return frozenset(
            {
                ContentPipelineStage.TRANSCODE,
                ContentPipelineStage.OCR,
                ContentPipelineStage.EMBED,
                ContentPipelineStage.CLASSIFY,
                ContentPipelineStage.SYNC_QDRANT,
                ContentPipelineStage.SYNC_MEILI,
            }
        )

    @property
    def consumes_media_inspect(self) -> bool:
        """Whether the role materializes retained raw-ingest media."""

        return self in {WorkerRole.ALL, WorkerRole.MEDIA}

    @property
    def consumes_source_engagement(self) -> bool:
        """Whether the role consumes session-affined Telegram metrics work."""

        return self in {WorkerRole.ALL, WorkerRole.TELEGRAM}

    @property
    def needs_storage(self) -> bool:
        """Whether the role downloads or uploads S3-compatible objects."""

        return self in {WorkerRole.ALL, WorkerRole.MEDIA, WorkerRole.OCR, WorkerRole.ENRICHMENT}

    @property
    def needs_media_processor(self) -> bool:
        """Whether the role uses FFmpeg/image inspection or preview extraction."""

        return self in {WorkerRole.ALL, WorkerRole.MEDIA, WorkerRole.OCR, WorkerRole.ENRICHMENT}

    @property
    def needs_ocr(self) -> bool:
        """Whether the role initializes the OCR provider."""

        return self in {WorkerRole.ALL, WorkerRole.OCR}

    @property
    def needs_enrichment(self) -> bool:
        """Whether the role initializes embed, similarity, and classification providers."""

        return self in {WorkerRole.ALL, WorkerRole.ENRICHMENT}

    @property
    def needs_sync(self) -> bool:
        """Whether the role initializes search-index mutation clients."""

        return self in {WorkerRole.ALL, WorkerRole.SYNC}

    def consumes_stage(self, stage: ContentPipelineStage) -> bool:
        """Return whether a stage subscriber belongs to this role."""

        return stage in self.stages

    def consumer_arguments(self) -> dict[str, str]:
        """Return inspectable RabbitMQ metadata for queue ownership checks."""

        return {RABBITMQ_WORKER_ROLE_ARGUMENT: self.value}


__all__ = ["RABBITMQ_WORKER_ROLE_ARGUMENT", "WorkerRole"]
