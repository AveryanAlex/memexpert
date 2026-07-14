"""Integration tests for content-pipeline stage lifecycle, read, replay, and sync behavior."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from memexpert.core.classification import ClassificationResult
from memexpert.core.config import Settings
from memexpert.core.ocr import OCRExtractionResult
from memexpert.core.qdrant import (
    QdrantSimilarityMatch,
)
from memexpert.core.voyage import VoyageEmbeddingResult
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.media.contracts import NormalizedMediaResult, UploadMediaDetails
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemeSource,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    SourceChannel,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    IngestSourceKind,
    RabbitMQOutboxMessageStatus,
    SourceAttachReason,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.pipeline.constants import SYNC_REPLAY_BATCH_MAX
from memexpert.pipeline.items import PipelineItemReadService
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.pipeline.stage_completion import PipelineStageCompletionService
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
)
from memexpert.services import (
    PipelineIngestError,
    PipelineMergeTransactionError,
    PipelineReplayNotAllowedError,
)
from memexpert.services.content_merge import MERGE_REASON_HIGH_SIMILARITY

if TYPE_CHECKING:
    from datetime import datetime

    from aio_pika.abc import HeadersType
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

@dataclass(slots=True)
class FakeStorageClient:
    """Small sync S3-compatible client used by crawler service setup tests."""

    fail_put_with: Exception | None = None
    put_calls: list[dict[str, object]] = field(default_factory=list)
    delete_calls: list[dict[str, object]] = field(default_factory=list)

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object:
        if self.fail_put_with is not None:
            raise self.fail_put_with

        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
                "ContentLength": ContentLength,
            }
        )
        return {"ETag": "fake"}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        return {"DeleteMarker": True}


@dataclass(slots=True)
class RecordingBroker:
    """Broker double that verifies DB durability before publish is attempted."""

    session_factory: async_sessionmaker[AsyncSession] | None = None
    fail_with: Exception | None = None
    events: list[ContentPipelineDispatchEvent] = field(default_factory=list)
    publish_calls: list[dict[str, object]] = field(default_factory=list)
    file_visible_at_publish: list[bool] = field(default_factory=list)
    transcode_visible_at_publish: list[bool] = field(default_factory=list)

    async def publish(
        self,
        message: object,
        /,
        queue: str = "",
        exchange: str | None = None,
        *,
        routing_key: str = "",
        mandatory: bool = True,
        persist: bool = False,
        content_type: str | None = None,
        headers: HeadersType | None = None,
        message_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> object:
        event = ContentPipelineDispatchEvent.model_validate(message)
        if self.session_factory is not None:
            async with self.session_factory() as session:
                file_result = await session.execute(select(MemeFile).where(MemeFile.id == event.meme_file_id))
                transcode_result = await session.execute(
                    select(PipelineStageJournal).where(
                        PipelineStageJournal.meme_file_id == event.meme_file_id,
                        PipelineStageJournal.stage == ContentPipelineStage.TRANSCODE,
                    )
                )
                self.file_visible_at_publish.append(file_result.scalar_one_or_none() is not None)
                self.transcode_visible_at_publish.append(transcode_result.scalar_one_or_none() is not None)

        if self.fail_with is not None:
            raise self.fail_with

        self.publish_calls.append(
            {
                "payload": message,
                "queue": queue,
                "exchange": exchange,
                "routing_key": routing_key,
                "mandatory": mandatory,
                "persist": persist,
                "content_type": content_type,
                "headers": headers,
                "message_id": message_id,
                "timestamp": timestamp,
            }
        )
        self.events.append(event)
        return None


def build_normalized_media_result(meme_file_id: uuid.UUID, *, web_video: bool = True) -> NormalizedMediaResult:
    """Create a stable normalized artifact for service assertions."""

    return NormalizedMediaResult(
        quality_score=0.82,
        blur_hash="L4AS~q00~q.8%MRjM{Rj00IU%MRj",
        preview_image_object_key=f"pipeline/derived/{meme_file_id}/preview.png" if web_video else None,
        preview_image_bytes=b"normalized-preview-image" if web_video else None,
        web_video_object_key=f"pipeline/derived/{meme_file_id}/web.mp4" if web_video else None,
        web_video_bytes=b"normalized-web-video" if web_video else None,
    )


def build_ocr_result(*, source_object_key: str) -> OCRExtractionResult:
    """Create a stable OCR result for service assertions."""

    return OCRExtractionResult(
        engine="paddleocr",
        fallback_engine="ocr-command",
        fallback_used=True,
        low_confidence=True,
        confidence=0.41,
        language=ContentLanguage.EN,
        extracted_text="deadline\nmonday",
        source_object_key=source_object_key,
    )


def build_voyage_embedding_result(
    *,
    vector: tuple[float, ...] | None = None,
    dimensions: int = 1024,
    model: str = "voyage-multimodal-3.5",
    input_hash: str | None = None,
) -> VoyageEmbeddingResult:
    """Create a stable embedding result matching the 1024-dim Voyage contract."""

    resolved_vector = vector if vector is not None else tuple(0.01 * index for index in range(dimensions))
    resolved_hash = input_hash if input_hash is not None else ("a" * 64)
    return VoyageEmbeddingResult(
        model=model,
        dimensions=dimensions,
        vector=resolved_vector,
        input_hash=resolved_hash,
    )


def build_classification_result(*, is_nsfw: bool = False, nsfw_score: float = 0.1) -> ClassificationResult:
    """Create a stable classification result for service assertions."""

    return ClassificationResult(
        model="memexpert-nsfw-v1",
        is_nsfw=is_nsfw,
        nsfw_score=nsfw_score,
    )


def _make_distinct_upload_media_details(*, tag: str) -> UploadMediaDetails:
    """Build unique media metadata so repeated seed rows get distinct perceptual hashes."""

    perceptual_hash = (tag * 16)[:16]
    return UploadMediaDetails(
        media_type=ContentKind.IMAGE,
        mime_type="image/png",
        width=128,
        height=128,
        file_size_bytes=64,
        perceptual_hash=perceptual_hash,
    )


def _build_service_with_distinct_phash(
    session: AsyncSession,
    *,
    phash_tag: str,
    broker: RecordingBroker | None = None,
) -> PipelineStageCompletionService:
    """Return a stage completion service while seed rows own their pHash."""

    _ = phash_tag
    return PipelineStageCompletionService(
        session,
        broker=broker or RecordingBroker(),
    )


async def _drive_to_embed_pending(
    session: AsyncSession,
    *,
    source_id: str,
    post_id: str,
    phash_tag: str,
    broker: RecordingBroker | None = None,
    filename: str = "embed-ready.png",
    source_kind: IngestSourceKind = IngestSourceKind.OPERATOR_UPLOAD,
) -> tuple[uuid.UUID, NormalizedMediaResult, PipelineStageCompletionService]:
    """Create + transcode + OCR a pipeline item up to the embed-pending state."""

    meme_file_id = await _seed_pending_pipeline_item(
        session,
        filename=filename,
        content_type="image/png",
        media_bytes=f"fake-upload-bytes:{source_id}:{post_id}:{phash_tag}".encode(),
        source_id=source_id,
        post_id=post_id,
        phash_tag=phash_tag,
        source_kind=source_kind,
    )
    service = _build_service_with_distinct_phash(
        session,
        phash_tag=phash_tag,
        broker=broker,
    )
    normalized = build_normalized_media_result(meme_file_id)
    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    web_video_object_key = normalized.web_video_object_key
    assert web_video_object_key is not None
    await service.complete_ocr_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_ocr_result(source_object_key=web_video_object_key),
    )
    return meme_file_id, normalized, service


async def _seed_pending_pipeline_item(
    session: AsyncSession,
    *,
    filename: str,
    content_type: str,
    media_bytes: bytes,
    source_id: str,
    post_id: str,
    phash_tag: str = "a",
    source_kind: IngestSourceKind = IngestSourceKind.OPERATOR_UPLOAD,
) -> uuid.UUID:
    details = _make_distinct_upload_media_details(tag=phash_tag)
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    event_id = uuid.uuid7()
    now = utcnow_for_tests()
    session.add(
        Meme(
            id=meme_id,
            media_type=details.media_type,
            primary_file_id=meme_file_id,
            language=ContentLanguage.NONE,
            is_public=source_kind is IngestSourceKind.PUBLIC_CRAWLER,
        )
    )
    await session.flush()
    session.add_all(
        [
            MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.PENDING,
                width=details.width,
                height=details.height,
                file_size_bytes=len(media_bytes),
                mime_type=content_type,
                s3_original_key=f"pipeline/originals/{meme_file_id}/original.{filename.rsplit('.', 1)[-1]}",
                perceptual_hash=details.perceptual_hash,
                sha256_hex=hashlib.sha256(media_bytes).hexdigest(),
                ingest_origin=IngestFileOrigin.NEW_MEME,
            ),
            MemeSource(
                file_id=meme_file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id=source_id,
                post_id=post_id,
                source_kind=source_kind,
                is_first_source=True,
                source_alive=True,
                attach_reason=SourceAttachReason.NEW_FILE,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=event_id,
                is_retryable=False,
                started_at=now,
                finished_at=now,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.PENDING,
                attempt_count=0,
                last_event_id=event_id,
                is_retryable=True,
            ),
        ]
    )
    await session.commit()
    return meme_file_id


async def test_complete_transcode_stage_persists_derivative_metadata_and_queues_ocr(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broker = RecordingBroker()
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )

    meme_file_id = await _seed_pending_pipeline_item(
        migrated_db_session,
        filename="transcode.gif",
        content_type="image/gif",
        media_bytes=b"transcode-bytes",
        source_id="stage-chain",
        post_id="6001",
        phash_tag="t",
    )
    normalized = build_normalized_media_result(meme_file_id)

    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    after_transcode = await PipelineItemReadService(migrated_db_session).get_item(meme_file_id)

    assert after_transcode.current_stage is ContentPipelineStage.OCR
    assert after_transcode.current_status is ContentPipelineStageStatus.PENDING
    assert after_transcode.web_video_object_key == normalized.web_video_object_key
    assert broker.events[-1].event_type is ContentPipelineEventType.MEME_TRANSCODED
    assert broker.events[-1].stage is ContentPipelineStage.OCR

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))

    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.PROCESSING
    assert persisted_file.s3_web_video_key == normalized.web_video_object_key
    assert persisted_file.mime_type == "image/gif"
    assert persisted_file.width == 128
    assert persisted_file.height == 128
    assert persisted_file.file_size_bytes == len(b"transcode-bytes")
    assert persisted_file.quality_score == pytest.approx(0.82)
    assert persisted_file.blur_hash == normalized.blur_hash


async def test_complete_transcode_stage_preserves_original_metadata_without_static_web_video(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broker = RecordingBroker()
    service = PipelineStageCompletionService(migrated_db_session, broker=broker)
    media_bytes = b"static-image-original-bytes"
    meme_file_id = await _seed_pending_pipeline_item(
        migrated_db_session,
        filename="static.jpg",
        content_type="image/jpeg",
        media_bytes=media_bytes,
        source_id="static-stage-chain",
        post_id="static-6001",
        phash_tag="s",
    )

    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_normalized_media_result(meme_file_id, web_video=False),
    )

    after_transcode = await PipelineItemReadService(migrated_db_session).get_item(meme_file_id)
    assert after_transcode.current_stage is ContentPipelineStage.OCR
    assert after_transcode.current_status is ContentPipelineStageStatus.PENDING
    assert after_transcode.web_video_object_key is None
    assert broker.events[-1].event_type is ContentPipelineEventType.MEME_TRANSCODED
    assert broker.events[-1].stage is ContentPipelineStage.OCR

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))

    assert persisted_file is not None
    assert persisted_file.mime_type == "image/jpeg"
    assert persisted_file.file_size_bytes == len(media_bytes)
    assert persisted_file.width == 128
    assert persisted_file.height == 128
    assert persisted_file.s3_web_video_key is None
    assert persisted_file.quality_score == pytest.approx(0.82)
    assert persisted_file.blur_hash == "L4AS~q00~q.8%MRjM{Rj00IU%MRj"


async def test_complete_ocr_stage_persists_durable_result_and_keeps_meme_unready(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broker = RecordingBroker()
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=broker,
    )

    meme_file_id = await _seed_pending_pipeline_item(
        migrated_db_session,
        filename="ocr.png",
        content_type="image/png",
        media_bytes=b"ocr-bytes",
        source_id="ocr-source",
        post_id="7001",
        phash_tag="o",
    )
    normalized = build_normalized_media_result(meme_file_id)
    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    web_video_object_key = normalized.web_video_object_key
    assert web_video_object_key is not None

    await service.complete_ocr_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_ocr_result(source_object_key=web_video_object_key),
    )
    after_ocr = await PipelineItemReadService(migrated_db_session).get_item(meme_file_id)

    assert after_ocr.current_stage is ContentPipelineStage.EMBED
    assert after_ocr.current_status is ContentPipelineStageStatus.PENDING
    assert broker.events[-1].event_type is ContentPipelineEventType.MEME_OCR_DONE
    assert broker.events[-1].stage is ContentPipelineStage.EMBED

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        assert persisted_file is not None
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == persisted_file.meme_id))
        persisted_ocr = await session.scalar(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == meme_file_id)
        )

    assert persisted_meme is not None
    assert persisted_ocr is not None
    assert persisted_file.status is ContentProcessingStatus.PROCESSING
    assert persisted_meme.ocr_text is None
    assert persisted_meme.language is ContentLanguage.NONE
    assert persisted_ocr.engine == "paddleocr"
    assert persisted_ocr.fallback_engine == "ocr-command"
    assert persisted_ocr.fallback_used is True
    assert persisted_ocr.low_confidence is True
    assert persisted_ocr.confidence == pytest.approx(0.41)
    assert persisted_ocr.extracted_text == "deadline\nmonday"
    assert persisted_ocr.source_object_key == normalized.web_video_object_key


async def test_mark_stage_success_publish_failure_leaves_retryable_outbox_and_keeps_business_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file_id = await _seed_pending_pipeline_item(
        migrated_db_session,
        filename="publish-failure.png",
        content_type="image/png",
        media_bytes=b"publish-failure-bytes",
        source_id="publish-failure",
        post_id="6003",
        phash_tag="p",
    )

    failing_service = PipelineStageCompletionService(
        migrated_db_session,
        broker=RecordingBroker(fail_with=RuntimeError("broker unavailable")),
    )

    await failing_service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_normalized_media_result(meme_file_id),
    )

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        persisted_rows = (
            await session.execute(
                select(PipelineStageJournal).where(PipelineStageJournal.meme_file_id == meme_file_id)
            )
        ).scalars().all()
        outbox_message = await session.scalar(
            select(RabbitMQOutboxMessage).where(
                RabbitMQOutboxMessage.aggregate_id == str(meme_file_id),
                RabbitMQOutboxMessage.event_type == ContentPipelineEventType.MEME_TRANSCODED.value,
            )
        )

    sorted_rows = sorted(
        persisted_rows,
        key=lambda row: {
            ContentPipelineStage.INGEST: 0,
            ContentPipelineStage.TRANSCODE: 1,
            ContentPipelineStage.OCR: 2,
        }[row.stage],
    )

    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.PROCESSING
    assert tuple((row.stage, row.status, row.normalized_reason) for row in sorted_rows) == (
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.SUCCEEDED, None),
        (ContentPipelineStage.TRANSCODE, ContentPipelineStageStatus.SUCCEEDED, None),
        (ContentPipelineStage.OCR, ContentPipelineStageStatus.PENDING, None),
    )
    assert outbox_message is not None
    assert outbox_message.status is RabbitMQOutboxMessageStatus.FAILED
    assert outbox_message.next_retry_at is not None
    assert "broker unavailable" in (outbox_message.last_error_text or "")


async def test_replay_item_rejects_stage_that_has_not_been_dispatched_yet(
    migrated_db_session: AsyncSession,
) -> None:
    service = PipelineReplayService(
        migrated_db_session,
        broker=RecordingBroker(),
    )
    meme_file_id = await _seed_pending_pipeline_item(
        migrated_db_session,
        filename="replay-guard.png",
        content_type="image/png",
        media_bytes=b"replay-guard-bytes",
        source_id="replay-guard",
        post_id="6002",
        phash_tag="r",
    )

    with pytest.raises(PipelineReplayNotAllowedError, match="has no durable journal row"):
        _ = await service.replay_item(meme_file_id, stage=ContentPipelineStage.EMBED)


async def test_replay_publish_failure_keeps_reservation_and_retryable_outbox(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PipelineStageCompletionService(
        migrated_db_session,
        broker=RecordingBroker(),
    )
    meme_file_id = await _seed_pending_pipeline_item(
        migrated_db_session,
        filename="replay-restore.png",
        content_type="image/png",
        media_bytes=b"replay-restore-bytes",
        source_id="replay-restore",
        post_id="6004",
        phash_tag="s",
    )
    failed_event_id = uuid.uuid7()
    await service.mark_stage_failed(
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        attempt=1,
        event_id=failed_event_id,
        normalized_reason="forced_failure",
        last_error_text="transcode failed the first time",
        retryable=True,
    )

    failing_replay_service = PipelineReplayService(
        migrated_db_session,
        broker=RecordingBroker(fail_with=RuntimeError("republish failed")),
    )

    replay = await failing_replay_service.replay_item(meme_file_id, stage=ContentPipelineStage.TRANSCODE)

    async with postgres_session_factory() as session:
        reserved_item = await PipelineItemReadService(session).get_item(meme_file_id)
        outbox_message = await session.scalar(
            select(RabbitMQOutboxMessage).where(
                RabbitMQOutboxMessage.aggregate_id == str(meme_file_id),
                RabbitMQOutboxMessage.event_type == ContentPipelineEventType.STAGE_REPLAY_REQUESTED.value,
            )
        )

    transcode_stage = next(stage for stage in reserved_item.stages if stage.stage is ContentPipelineStage.TRANSCODE)
    assert reserved_item.current_stage is ContentPipelineStage.TRANSCODE
    assert reserved_item.current_status is ContentPipelineStageStatus.PENDING
    assert transcode_stage.attempt_count == 2
    assert transcode_stage.last_event_id == replay.replay_event_id
    assert transcode_stage.last_event_id != failed_event_id
    assert transcode_stage.normalized_reason == "replay_requested"
    assert outbox_message is not None
    assert outbox_message.status is RabbitMQOutboxMessageStatus.FAILED
    assert outbox_message.next_retry_at is not None
    assert "republish failed" in (outbox_message.last_error_text or "")


async def test_complete_embed_stage_without_matches_persists_embedding_and_queues_classify(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broker = RecordingBroker()
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="embed-no-match",
        post_id="9001",
        phash_tag="a",
        broker=broker,
    )
    embedding_result = build_voyage_embedding_result(input_hash="1" * 64)

    merge_outcome = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=embedding_result,
        similarity_matches=(),
    )

    assert merge_outcome.merged is False
    after_embed = await PipelineItemReadService(migrated_db_session).get_item(meme_file_id)
    assert after_embed.current_stage is ContentPipelineStage.CLASSIFY
    assert after_embed.current_status is ContentPipelineStageStatus.PENDING
    assert broker.events[-1].event_type is ContentPipelineEventType.MEME_EMBEDDED
    assert broker.events[-1].stage is ContentPipelineStage.CLASSIFY

    async with postgres_session_factory() as session:
        persisted_cache_row = await session.scalar(
            select(EmbeddingCache).where(EmbeddingCache.source_file_id == meme_file_id)
        )
        persisted_meme = await session.scalar(
            select(Meme).where(Meme.id == after_embed.meme_id)
        )

    assert persisted_cache_row is not None
    assert persisted_cache_row.input_hash == embedding_result.input_hash
    assert persisted_cache_row.model_version == embedding_result.model
    assert persisted_cache_row.embedding == embedding_result.embedding_bytes
    assert persisted_meme is not None
    # Classification has not run yet, so the canonical meme must not be truthfully NSFW.
    assert persisted_meme.is_nsfw is False
    assert persisted_meme.ocr_text is None


async def test_complete_embed_stage_rejects_malformed_vector_dimensions(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="malformed-vector",
        post_id="9002",
        phash_tag="b",
    )
    malformed_result = build_voyage_embedding_result(
        vector=tuple(0.1 for _ in range(512)),
        dimensions=512,
        input_hash="2" * 64,
    )

    with pytest.raises(PipelineIngestError, match="unexpected dimensionality"):
        _ = await service.complete_embed_stage(
            meme_file_id=meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            embedding_result=malformed_result,
            similarity_matches=(),
        )

    async with postgres_session_factory() as session:
        cache_rows = (
            await session.execute(select(EmbeddingCache).where(EmbeddingCache.source_file_id == meme_file_id))
        ).scalars().all()

    assert cache_rows == []


async def test_complete_embed_stage_auto_merges_high_similarity_into_older_meme(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broker = RecordingBroker()
    older_meme_file_id, older_normalized, older_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="merge-older",
        post_id="9100",
        phash_tag="c",
        broker=broker,
        filename="older.png",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )
    _ = await older_service.complete_embed_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="3" * 64),
        similarity_matches=(),
    )

    older_file = await migrated_db_session.scalar(select(MemeFile).where(MemeFile.id == older_meme_file_id))
    assert older_file is not None
    older_meme_id = older_file.meme_id
    older_file.quality_score = 0.99
    older_meme = await migrated_db_session.scalar(select(Meme).where(Meme.id == older_meme_id))
    assert older_meme is not None
    older_meme.like_count = 3
    await migrated_db_session.commit()

    newer_meme_file_id, newer_normalized, newer_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="merge-newer",
        post_id="9101",
        phash_tag="d",
        broker=broker,
        filename="newer.png",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )

    async with postgres_session_factory() as pre_merge_session:
        newer_file_before = await pre_merge_session.scalar(
            select(MemeFile).where(MemeFile.id == newer_meme_file_id)
        )
        assert newer_file_before is not None
        newer_meme_id = newer_file_before.meme_id

    newer_embedding = build_voyage_embedding_result(input_hash="4" * 64)
    similarity_match = QdrantSimilarityMatch(
        meme_file_id=older_meme_file_id,
        meme_id=older_meme_id,
        similarity_score=0.97,
    )

    merge_outcome = await newer_service.complete_embed_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=newer_embedding,
        similarity_matches=(similarity_match,),
    )

    assert merge_outcome.merged is True
    assert merge_outcome.target_meme_id == older_meme_id
    assert merge_outcome.source_meme_id == newer_meme_id
    assert merge_outcome.similarity_score == pytest.approx(0.97)

    async with postgres_session_factory() as session:
        surviving_memes = (
            await session.execute(select(Meme.id))
        ).scalars().all()
        target_meme = await session.scalar(select(Meme).where(Meme.id == older_meme_id))
        source_meme = await session.scalar(select(Meme).where(Meme.id == newer_meme_id))
        moved_file = await session.scalar(select(MemeFile).where(MemeFile.id == newer_meme_file_id))
        merge_log = await session.scalar(
            select(MemeMergeLog).where(MemeMergeLog.source_meme_file_id == newer_meme_file_id)
        )

    assert newer_meme_id not in set(surviving_memes)
    assert source_meme is None
    assert target_meme is not None
    assert target_meme.like_count == 3  # newer meme contributes 0 likes so sum stays at 3.
    assert moved_file is not None
    assert moved_file.meme_id == older_meme_id
    assert target_meme.primary_file_id == older_meme_file_id
    assert merge_log is not None
    assert merge_log.merge_reason == MERGE_REASON_HIGH_SIMILARITY
    assert merge_log.similarity_score == pytest.approx(0.97)


async def test_complete_embed_stage_ignores_self_matches_and_rejects_newer_targets(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    older_meme_file_id, _, older_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="self-older",
        post_id="9200",
        phash_tag="e",
    )
    _ = await older_service.complete_embed_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="5" * 64),
        similarity_matches=(),
    )

    middle_meme_file_id, _, middle_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="self-middle",
        post_id="9201",
        phash_tag="f",
    )
    _ = await middle_service.complete_embed_stage(
        meme_file_id=middle_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="6" * 64),
        similarity_matches=(),
    )

    newer_meme_file_id, _, newer_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="self-newer",
        post_id="9202",
        phash_tag="g",
    )

    async with postgres_session_factory() as session:
        middle_file = await session.scalar(select(MemeFile).where(MemeFile.id == middle_meme_file_id))
        newer_file = await session.scalar(select(MemeFile).where(MemeFile.id == newer_meme_file_id))
        older_file = await session.scalar(select(MemeFile).where(MemeFile.id == older_meme_file_id))
        assert middle_file is not None and newer_file is not None and older_file is not None
        middle_meme_id = middle_file.meme_id
        newer_meme_id = newer_file.meme_id
        older_meme_id = older_file.meme_id

    # Self-match (same meme_file_id) must be ignored, and the only other candidate
    # (middle meme) is older than the newer meme but we also include a pseudo-match
    # pointing at the newer meme's own meme_id to confirm meme-id self-match is dropped.
    self_file_match = QdrantSimilarityMatch(
        meme_file_id=newer_meme_file_id,
        meme_id=newer_meme_id,
        similarity_score=0.99,
    )
    self_meme_match = QdrantSimilarityMatch(
        meme_file_id=older_meme_file_id,
        meme_id=newer_meme_id,
        similarity_score=0.98,
    )

    merge_outcome = await newer_service.complete_embed_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="7" * 64),
        similarity_matches=(self_file_match, self_meme_match),
    )

    assert merge_outcome.merged is False
    assert merge_outcome.target_meme_id == newer_meme_id

    async with postgres_session_factory() as session:
        surviving = (await session.execute(select(Meme.id))).scalars().all()
    assert older_meme_id in set(surviving)
    assert middle_meme_id in set(surviving)
    assert newer_meme_id in set(surviving)


async def test_complete_classify_stage_makes_meme_ready_with_truthful_ocr_and_nsfw(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broker = RecordingBroker()
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="classify-truth",
        post_id="9300",
        phash_tag="h",
        broker=broker,
    )
    _ = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="7" * 64),
        similarity_matches=(),
    )

    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(is_nsfw=True, nsfw_score=0.88),
    )

    after_classify = await PipelineItemReadService(migrated_db_session).get_item(meme_file_id)
    # T03: classify fans out to both sync stages in one atomic commit.
    # ``current_stage`` reflects whichever sync row the stage-ordering helper
    # picks first, but both must exist and both MEME_READY publishes must
    # have fired.
    assert after_classify.current_stage in {
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    }
    meme_ready_events = [
        event for event in broker.events if event.event_type is ContentPipelineEventType.MEME_READY
    ]
    assert {event.stage for event in meme_ready_events} == {
        ContentPipelineStage.SYNC_QDRANT,
        ContentPipelineStage.SYNC_MEILI,
    }

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        assert persisted_file is not None
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == persisted_file.meme_id))
        stage_rows = (
            await session.execute(
                select(PipelineStageJournal).where(PipelineStageJournal.meme_file_id == meme_file_id)
            )
        ).scalars().all()
    stage_values = {row.stage for row in stage_rows}
    assert ContentPipelineStage.SYNC_QDRANT in stage_values
    assert ContentPipelineStage.SYNC_MEILI in stage_values

    assert persisted_file.status is ContentProcessingStatus.READY
    assert persisted_meme is not None
    assert persisted_meme.is_nsfw is True
    assert persisted_meme.ocr_text == "deadline\nmonday"
    assert persisted_meme.language is ContentLanguage.EN


@pytest.mark.parametrize(
    "stage",
    [ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI],
)
async def test_starting_sync_stage_preserves_classified_file_readiness(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    stage: ContentPipelineStage,
) -> None:
    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id=f"{stage.value}-ready",
        post_id=f"ready-{stage.value}",
        phash_tag="r",
        input_hash_seed="r",
    )

    await service.mark_stage_processing(
        meme_file_id=meme_file_id,
        stage=stage,
        attempt=1,
        event_id=uuid.uuid7(),
    )

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))

    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.READY


async def test_complete_classify_stage_does_not_clear_existing_nsfw_true(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="classify-conservative",
        post_id="9301",
        phash_tag="z",
    )
    _ = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="c" * 64),
        similarity_matches=(),
    )
    file_row = await migrated_db_session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
    assert file_row is not None
    meme_row = await migrated_db_session.scalar(select(Meme).where(Meme.id == file_row.meme_id))
    assert meme_row is not None
    meme_row.is_nsfw = True
    await migrated_db_session.commit()

    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(is_nsfw=False, nsfw_score=0.01),
    )

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        assert persisted_file is not None
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == persisted_file.meme_id))

    assert persisted_meme is not None
    assert persisted_meme.is_nsfw is True
    assert persisted_meme.ocr_text == "deadline\nmonday"
    assert persisted_meme.language is ContentLanguage.EN


async def test_complete_classify_stage_follows_primary_file_change_after_merge(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    older_meme_file_id, _, older_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="primary-older",
        post_id="9400",
        phash_tag="i",
        filename="primary-older.png",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )
    _ = await older_service.complete_embed_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="8" * 64),
        similarity_matches=(),
    )

    # Mutate the older file + OCR row through the same session so the primary session
    # sees the changes without cache-expiry surprises.
    older_file_row = await migrated_db_session.scalar(
        select(MemeFile).where(MemeFile.id == older_meme_file_id)
    )
    assert older_file_row is not None
    older_meme_id = older_file_row.meme_id
    older_file_row.quality_score = 0.3  # low so the newer file wins primary reselection.
    older_ocr = await migrated_db_session.scalar(
        select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == older_meme_file_id)
    )
    assert older_ocr is not None
    older_ocr.extracted_text = "older-primary-text"
    older_ocr.language = ContentLanguage.EN
    await migrated_db_session.commit()

    newer_meme_file_id, newer_normalized, newer_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="primary-newer",
        post_id="9401",
        phash_tag="j",
        filename="primary-newer.png",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )

    newer_file_row = await migrated_db_session.scalar(
        select(MemeFile).where(MemeFile.id == newer_meme_file_id)
    )
    assert newer_file_row is not None
    newer_meme_id = newer_file_row.meme_id
    newer_file_row.quality_score = 0.95
    newer_ocr = await migrated_db_session.scalar(
        select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == newer_meme_file_id)
    )
    assert newer_ocr is not None
    newer_ocr.extracted_text = "newer-primary-text"
    newer_ocr.language = ContentLanguage.RU
    await migrated_db_session.commit()

    merge_outcome = await newer_service.complete_embed_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="9" * 64),
        similarity_matches=(
            QdrantSimilarityMatch(
                meme_file_id=older_meme_file_id,
                meme_id=older_meme_id,
                similarity_score=0.97,
            ),
        ),
    )

    assert merge_outcome.merged is True
    assert merge_outcome.target_meme_id == older_meme_id
    assert merge_outcome.primary_file_id == newer_meme_file_id

    await newer_service.complete_classify_stage(
        meme_file_id=newer_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(is_nsfw=False, nsfw_score=0.2),
    )

    async with postgres_session_factory() as session:
        target_meme = await session.scalar(select(Meme).where(Meme.id == older_meme_id))

    assert target_meme is not None
    assert target_meme.primary_file_id == newer_meme_file_id
    # Canonical OCR truth must follow the new primary file's OCR row.
    assert target_meme.ocr_text == "newer-primary-text"
    assert target_meme.language is ContentLanguage.RU
    assert target_meme.is_nsfw is False
    # Pre-existing newer meme row must be gone.
    async with postgres_session_factory() as session:
        stale_meme = await session.scalar(select(Meme).where(Meme.id == newer_meme_id))
    assert stale_meme is None


async def test_complete_embed_stage_publish_failure_leaves_classify_pending_and_retryable_outbox(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file_id, _, _ = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="embed-publish-fail",
        post_id="9500",
        phash_tag="k",
    )

    failing_service = PipelineStageCompletionService(
        migrated_db_session,
        broker=RecordingBroker(fail_with=RuntimeError("broker unavailable")),
    )
    _ = await failing_service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="a" * 64),
        similarity_matches=(),
    )

    async with postgres_session_factory() as session:
        persisted_rows = (
            await session.execute(
                select(PipelineStageJournal).where(PipelineStageJournal.meme_file_id == meme_file_id)
            )
        ).scalars().all()
        outbox_message = await session.scalar(
            select(RabbitMQOutboxMessage).where(
                RabbitMQOutboxMessage.aggregate_id == str(meme_file_id),
                RabbitMQOutboxMessage.event_type == ContentPipelineEventType.MEME_EMBEDDED.value,
            )
        )

    stage_rows = {row.stage: row for row in persisted_rows}
    assert stage_rows[ContentPipelineStage.EMBED].status is ContentPipelineStageStatus.SUCCEEDED
    assert stage_rows[ContentPipelineStage.CLASSIFY].status is ContentPipelineStageStatus.PENDING
    assert stage_rows[ContentPipelineStage.CLASSIFY].normalized_reason is None
    assert outbox_message is not None
    assert outbox_message.status is RabbitMQOutboxMessageStatus.FAILED
    assert outbox_message.next_retry_at is not None
    assert "broker unavailable" in (outbox_message.last_error_text or "")


async def test_complete_embed_stage_rolls_back_partial_merge_and_keeps_embed_replayable(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    older_meme_file_id, _, older_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="merge-rollback-older",
        post_id="9600",
        phash_tag="l",
        filename="rollback-older.png",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )
    _ = await older_service.complete_embed_stage(
        meme_file_id=older_meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash="c" * 64),
        similarity_matches=(),
    )

    async with postgres_session_factory() as stash_session:
        older_file = await stash_session.scalar(select(MemeFile).where(MemeFile.id == older_meme_file_id))
        assert older_file is not None
        older_meme_id = older_file.meme_id

    newer_meme_file_id, _, newer_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="merge-rollback-newer",
        post_id="9601",
        phash_tag="m",
        filename="rollback-newer.png",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )

    async with postgres_session_factory() as stash_session:
        newer_file = await stash_session.scalar(select(MemeFile).where(MemeFile.id == newer_meme_file_id))
        assert newer_file is not None
        newer_meme_id = newer_file.meme_id

    from memexpert.services import content_merge as content_merge_module

    async def fake_transfer(_self: object, **_kwargs: object) -> tuple[uuid.UUID, ...]:
        raise RuntimeError("forced merge transfer failure")

    monkeypatch.setattr(
        content_merge_module.ContentMergeService,
        "_transfer_meme_files",
        fake_transfer,
    )

    similarity_match = QdrantSimilarityMatch(
        meme_file_id=older_meme_file_id,
        meme_id=older_meme_id,
        similarity_score=0.95,
    )

    with pytest.raises(PipelineMergeTransactionError, match="post-embed auto-merge transaction"):
        _ = await newer_service.complete_embed_stage(
            meme_file_id=newer_meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            embedding_result=build_voyage_embedding_result(input_hash="d" * 64),
            similarity_matches=(similarity_match,),
        )

    async with postgres_session_factory() as verify_session:
        # Both memes must still be intact because the merge transaction rolled back.
        surviving_meme_ids = (
            await verify_session.execute(select(Meme.id))
        ).scalars().all()
        embed_stage_row = await verify_session.scalar(
            select(PipelineStageJournal).where(
                PipelineStageJournal.meme_file_id == newer_meme_file_id,
                PipelineStageJournal.stage == ContentPipelineStage.EMBED,
            )
        )
        newer_file_after = await verify_session.scalar(
            select(MemeFile).where(MemeFile.id == newer_meme_file_id)
        )
        # The single merge transaction must also roll back the embedding-cache
        # row so the replay re-runs end-to-end against a clean durable state.
        newer_cache_rows = (
            await verify_session.execute(
                select(EmbeddingCache).where(EmbeddingCache.source_file_id == newer_meme_file_id)
            )
        ).scalars().all()

    assert newer_meme_id in set(surviving_meme_ids)
    assert older_meme_id in set(surviving_meme_ids)
    assert embed_stage_row is not None
    # Embed stage must remain replayable after the rollback.
    assert embed_stage_row.status in {
        ContentPipelineStageStatus.PENDING,
        ContentPipelineStageStatus.PROCESSING,
    }
    # Locking in the single-transaction invariant at the service boundary: a
    # merge failure must leave ``is_retryable=True`` so the runtime replay can
    # take over without operator intervention.
    assert embed_stage_row.is_retryable is True
    assert newer_file_after is not None
    assert newer_cache_rows == []
    assert newer_file_after.meme_id == newer_meme_id  # not moved


async def _drive_to_classify_succeeded(
    session: AsyncSession,
    *,
    source_id: str,
    post_id: str,
    phash_tag: str,
    input_hash_seed: str,
) -> tuple[uuid.UUID, PipelineStageCompletionService]:
    """Drive a pipeline item end-to-end through classify success for sync replay tests."""

    meme_file_id, _, service = await _drive_to_embed_pending(
        session,
        source_id=source_id,
        post_id=post_id,
        phash_tag=phash_tag,
    )
    _ = await service.complete_embed_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        embedding_result=build_voyage_embedding_result(input_hash=input_hash_seed * 64),
        similarity_matches=(),
    )
    await service.complete_classify_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        classification_result=build_classification_result(),
    )
    return meme_file_id, service


async def test_meili_sync_methods_persist_snapshot_and_emit_synced_event(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T03 shipped the real Meilisearch path — the stubs must be fully functional now."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="meili-happy",
        post_id="9900",
        phash_tag="s",
        input_hash_seed="9",
    )
    event_id = uuid.uuid7()
    preview: dict[str, object] = {"id": meme_file_id.hex, "language": "en", "tags": ["a"]}

    first = await service.complete_sync_meili_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=event_id,
        payload_preview=preview,
    )
    assert first.status is SyncTargetStatus.SYNCED
    assert first.attempt_count == 1
    assert first.last_success_at is not None
    assert first.last_preview is not None
    assert first.last_preview.preview_fields["id"] == meme_file_id.hex

    # Idempotent re-run keeps the attempt count stable and reuses the event id.
    async with postgres_session_factory() as replay_session:
        replay_service = PipelineStageCompletionService(
            replay_session,
            broker=RecordingBroker(),
        )
        second = await replay_service.complete_sync_meili_stage(
            meme_file_id=meme_file_id,
            attempt=1,
            event_id=event_id,
            payload_preview=preview,
        )
    assert second.attempt_count == first.attempt_count
    assert second.last_event_id == event_id


async def test_replay_sync_target_rejects_items_without_classify_success(
    migrated_db_session: AsyncSession,
) -> None:
    # Stop before classify so the replay guard must fire.
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="replay-guard",
        post_id="9910",
        phash_tag="r",
    )

    with pytest.raises(PipelineReplayNotAllowedError, match="cannot replay sync targets"):
        replay_service = PipelineReplayService(migrated_db_session, broker=RecordingBroker())
        _ = await replay_service.replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)
    with pytest.raises(PipelineReplayNotAllowedError, match="cannot replay sync targets"):
        _ = await replay_service.replay_sync_target_batch([meme_file_id], SyncTargetKind.MEILISEARCH)


async def test_replay_sync_target_meili_reserves_independent_dispatch(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T03 shipped Meilisearch replay — the route must reserve a dispatch independently."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="replay-meili",
        post_id="9920",
        phash_tag="t",
        input_hash_seed="a",
    )

    accepted = await PipelineReplayService(
        migrated_db_session,
        broker=RecordingBroker(),
    ).replay_sync_target(meme_file_id, SyncTargetKind.MEILISEARCH)
    assert accepted.stage is ContentPipelineStage.SYNC_MEILI

    async with postgres_session_factory() as read_session:
        stage_row = await read_session.scalar(
            select(PipelineStageJournal).where(
                PipelineStageJournal.meme_file_id == meme_file_id,
                PipelineStageJournal.stage == ContentPipelineStage.SYNC_MEILI,
            )
        )
    assert stage_row is not None
    assert stage_row.last_event_id == accepted.replay_event_id


async def test_replay_sync_target_batch_refuses_batches_beyond_the_max(
    migrated_db_session: AsyncSession,
) -> None:
    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="batch-max",
        post_id="9940",
        phash_tag="v",
        input_hash_seed="c",
    )
    oversized_batch = [meme_file_id] * (SYNC_REPLAY_BATCH_MAX + 1)

    with pytest.raises(PipelineReplayNotAllowedError, match="exceeds the configured maximum"):
        _ = await PipelineReplayService(
            migrated_db_session,
            broker=RecordingBroker(),
        ).replay_sync_target_batch(oversized_batch, SyncTargetKind.QDRANT)


async def test_complete_sync_qdrant_stage_persists_snapshot_and_is_idempotent(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A successful Qdrant sync must upsert the snapshot row AND re-run as a no-op."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="sync-qdrant-happy",
        post_id="9960",
        phash_tag="q",
        input_hash_seed="q",
    )
    event_id = uuid.uuid7()
    preview = {"point_id": str(meme_file_id), "is_nsfw": False, "tags": []}

    first = await service.complete_sync_qdrant_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=event_id,
        payload_preview=preview,
    )
    assert first.status is SyncTargetStatus.SYNCED
    assert first.attempt_count == 1
    assert first.last_success_at is not None
    assert first.last_preview is not None
    assert first.last_preview.preview_fields["point_id"] == str(meme_file_id)

    # Re-running with the same event id must not bump attempts or reset
    # last_success_at to a new timestamp — the publish path is idempotent.
    async with postgres_session_factory() as replay_session:
        replay_service = PipelineStageCompletionService(
            replay_session,
            broker=RecordingBroker(),
        )
        second = await replay_service.complete_sync_qdrant_stage(
            meme_file_id=meme_file_id,
            attempt=1,
            event_id=event_id,
            payload_preview=preview,
        )
    assert second.status is SyncTargetStatus.SYNCED
    assert second.attempt_count == first.attempt_count  # no bump on idempotent replay.
    assert second.last_event_id == event_id


async def test_fail_sync_qdrant_stage_preserves_prior_success_timestamps(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transient failure must not erase the last known good sync timestamp."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="sync-qdrant-preserve",
        post_id="9961",
        phash_tag="p",
        input_hash_seed="p",
    )
    first_success_event = uuid.uuid7()
    first = await service.complete_sync_qdrant_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=first_success_event,
        payload_preview={"marker": "first"},
    )
    assert first.last_success_at is not None
    first_success_at = first.last_success_at
    first_preview = first.last_preview
    assert first_preview is not None

    # A replay is required so the stage row becomes failable again; reserve it
    # through replay_sync_target so both the snapshot row and the journal row
    # are in the right state for a transient failure.
    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            broker=RecordingBroker(),
        )
        accepted = await replay_service.replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)

    async with postgres_session_factory() as fail_session:
        fail_service = PipelineStageCompletionService(
            fail_session,
            broker=RecordingBroker(),
        )
        failure = await fail_service.fail_sync_qdrant_stage(
            meme_file_id=meme_file_id,
            attempt=accepted.attempt,
            event_id=accepted.replay_event_id,
            normalized_reason="sync_qdrant_timeout",
            last_error_text="transient timeout",
        )
    assert failure.status is SyncTargetStatus.FAILED
    assert failure.normalized_reason == "sync_qdrant_timeout"
    # The last known good sync timestamp and preview must still be intact.
    assert failure.last_success_at == first_success_at
    assert failure.last_preview is not None
    assert failure.last_preview.preview_fields["marker"] == "first"
    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.READY


async def test_replay_sync_target_qdrant_leaves_meili_row_untouched(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Qdrant-only sync replay must not touch the Meilisearch snapshot row."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="replay-isolation",
        post_id="9962",
        phash_tag="i",
        input_hash_seed="i",
    )

    # Seed a meilisearch snapshot row so we can prove the replay does not
    # mutate it. Populating it directly via the ORM is safe because T03 has
    # not wired the real Meilisearch consumer yet.
    from memexpert.models.base import utcnow as _utcnow

    async with postgres_session_factory() as seed_session:
        meili_row = MemeFileSyncTargetSnapshot(
            meme_file_id=meme_file_id,
            sync_target=SyncTargetKind.MEILISEARCH,
            status=SyncTargetStatus.FAILED,
            last_event_id=uuid.uuid7(),
            normalized_reason="sync_meili_timeout",
            last_error_text="pre-existing",
            last_payload_preview={},
            last_success_at=None,
            last_attempt_at=_utcnow(),
            attempt_count=2,
        )
        seed_session.add(meili_row)
        await seed_session.commit()
        meili_updated_at = meili_row.updated_at

    accepted = await PipelineReplayService(
        migrated_db_session,
        broker=RecordingBroker(),
    ).replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)
    assert accepted.stage is ContentPipelineStage.SYNC_QDRANT

    async with postgres_session_factory() as verify_session:
        surviving = await verify_session.scalar(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == SyncTargetKind.MEILISEARCH,
            )
        )
    assert surviving is not None
    assert surviving.normalized_reason == "sync_meili_timeout"
    assert surviving.last_error_text == "pre-existing"
    assert surviving.attempt_count == 2
    assert surviving.updated_at == meili_updated_at


async def test_item_detail_projects_per_target_sync_status_from_snapshot_rows(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="detail-sync",
        post_id="9950",
        phash_tag="w",
        input_hash_seed="d",
    )

    # First, the empty default: the item detail must already expose an empty
    # mapping before any snapshot rows exist.
    empty_detail = await PipelineItemReadService(migrated_db_session).get_item_detail(meme_file_id)
    assert empty_detail.sync_targets == {}

    from memexpert.models.base import utcnow as _utcnow

    now = _utcnow()
    async with postgres_session_factory() as write_session:
        write_session.add(
            MemeFileSyncTargetSnapshot(
                meme_file_id=meme_file_id,
                sync_target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.SYNCED,
                last_event_id=uuid.uuid7(),
                normalized_reason=None,
                last_error_text=None,
                last_payload_preview={"point_id": str(meme_file_id)},
                last_success_at=now,
                last_attempt_at=now,
                attempt_count=1,
            ),
        )
        write_session.add(
            MemeFileSyncTargetSnapshot(
                meme_file_id=meme_file_id,
                sync_target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.FAILED,
                last_event_id=uuid.uuid7(),
                normalized_reason="sync_meili_timeout",
                last_error_text="deadline exceeded",
                last_payload_preview={},
                last_success_at=None,
                last_attempt_at=now,
                attempt_count=3,
            ),
        )
        await write_session.commit()

    async with postgres_session_factory() as read_session:
        read_service = PipelineItemReadService(read_session)
        detail = await read_service.get_item_detail(meme_file_id)

    assert set(detail.sync_targets) == {SyncTargetKind.QDRANT, SyncTargetKind.MEILISEARCH}
    qdrant_status = detail.sync_targets[SyncTargetKind.QDRANT]
    meili_status = detail.sync_targets[SyncTargetKind.MEILISEARCH]
    assert qdrant_status.status is SyncTargetStatus.SYNCED
    assert qdrant_status.attempt_count == 1
    assert qdrant_status.last_success_at is not None
    assert meili_status.status is SyncTargetStatus.FAILED
    assert meili_status.normalized_reason == "sync_meili_timeout"
    assert meili_status.last_error_text == "deadline exceeded"
    assert meili_status.attempt_count == 3


async def test_fail_sync_meili_stage_preserves_prior_success_timestamps(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transient Meilisearch failure must not erase the last known good timestamp."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="sync-meili-preserve",
        post_id="9970",
        phash_tag="m",
        input_hash_seed="m",
    )
    first_success_event = uuid.uuid7()
    first = await service.complete_sync_meili_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=first_success_event,
        payload_preview={"marker": "first-meili"},
    )
    assert first.last_success_at is not None
    first_success_at = first.last_success_at
    first_preview = first.last_preview
    assert first_preview is not None

    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            broker=RecordingBroker(),
        )
        accepted = await replay_service.replay_sync_target(meme_file_id, SyncTargetKind.MEILISEARCH)

    async with postgres_session_factory() as fail_session:
        fail_service = PipelineStageCompletionService(
            fail_session,
            broker=RecordingBroker(),
        )
        failure = await fail_service.fail_sync_meili_stage(
            meme_file_id=meme_file_id,
            attempt=accepted.attempt,
            event_id=accepted.replay_event_id,
            normalized_reason="sync_meili_timeout",
            last_error_text="transient timeout",
        )
    assert failure.status is SyncTargetStatus.FAILED
    assert failure.normalized_reason == "sync_meili_timeout"
    # Prior ``last_success_at`` and preview are intact across the failure.
    assert failure.last_success_at == first_success_at
    assert failure.last_preview is not None
    assert failure.last_preview.preview_fields["marker"] == "first-meili"
    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.READY


async def test_replay_sync_target_meili_leaves_qdrant_row_byte_identical(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T03: replaying Meilisearch alone must not touch the Qdrant snapshot row.

    The test captures the full Qdrant row dict before and after the replay
    so any mutation — even an ``updated_at`` bump — would fail the
    comparison.
    """

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="replay-meili-isolation",
        post_id="9971",
        phash_tag="o",
        input_hash_seed="o",
    )
    _ = await service.complete_sync_qdrant_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        payload_preview={"qdrant_marker": str(meme_file_id)},
    )

    async with postgres_session_factory() as before_session:
        qdrant_before = await before_session.scalar(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == SyncTargetKind.QDRANT,
            )
        )
    assert qdrant_before is not None
    qdrant_before_snapshot = _snapshot_row_to_dict(qdrant_before)

    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            broker=RecordingBroker(),
        )
        accepted = await replay_service.replay_sync_target(meme_file_id, SyncTargetKind.MEILISEARCH)
    assert accepted.stage is ContentPipelineStage.SYNC_MEILI

    async with postgres_session_factory() as after_session:
        qdrant_after = await after_session.scalar(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == SyncTargetKind.QDRANT,
            )
        )
    assert qdrant_after is not None
    qdrant_after_snapshot = _snapshot_row_to_dict(qdrant_after)

    assert qdrant_before_snapshot == qdrant_after_snapshot


async def test_replay_sync_target_qdrant_leaves_meili_row_byte_identical(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The opposite direction of independence — qdrant replay must not touch meili row."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="replay-qdrant-isolation",
        post_id="9972",
        phash_tag="u",
        input_hash_seed="u",
    )
    _ = await service.complete_sync_meili_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        payload_preview={"meili_marker": meme_file_id.hex},
    )

    async with postgres_session_factory() as before_session:
        meili_before = await before_session.scalar(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == SyncTargetKind.MEILISEARCH,
            )
        )
    assert meili_before is not None
    meili_before_snapshot = _snapshot_row_to_dict(meili_before)

    async with postgres_session_factory() as replay_session:
        replay_service = PipelineReplayService(
            replay_session,
            broker=RecordingBroker(),
        )
        accepted = await replay_service.replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)
    assert accepted.stage is ContentPipelineStage.SYNC_QDRANT

    async with postgres_session_factory() as after_session:
        meili_after = await after_session.scalar(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == SyncTargetKind.MEILISEARCH,
            )
        )
    assert meili_after is not None
    meili_after_snapshot = _snapshot_row_to_dict(meili_after)

    assert meili_before_snapshot == meili_after_snapshot


async def test_replay_sync_target_batch_meili_cap_enforced(
    migrated_db_session: AsyncSession,
) -> None:
    """T03: the Meilisearch batch-replay path honors the same cap as the Qdrant batch."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="meili-batch-cap",
        post_id="9973",
        phash_tag="x",
        input_hash_seed="x",
    )
    oversized_batch = [meme_file_id] * (SYNC_REPLAY_BATCH_MAX + 1)
    with pytest.raises(PipelineReplayNotAllowedError, match="exceeds the configured maximum"):
        _ = await PipelineReplayService(
            migrated_db_session,
            broker=RecordingBroker(),
        ).replay_sync_target_batch(oversized_batch, SyncTargetKind.MEILISEARCH)


def _snapshot_row_to_dict(row: MemeFileSyncTargetSnapshot) -> dict[str, object]:
    """Freeze every inspect-visible attribute of a snapshot row into a dict.

    Used by the per-target isolation tests to prove that replaying one
    target does NOT mutate the other target's row down to the
    ``updated_at`` timestamp.
    """

    return {
        "id": row.id,
        "meme_file_id": row.meme_file_id,
        "sync_target": row.sync_target,
        "status": row.status,
        "last_event_id": row.last_event_id,
        "normalized_reason": row.normalized_reason,
        "last_error_text": row.last_error_text,
        "last_payload_preview": dict(row.last_payload_preview)
        if isinstance(row.last_payload_preview, dict)
        else row.last_payload_preview,
        "last_success_at": row.last_success_at,
        "last_attempt_at": row.last_attempt_at,
        "attempt_count": row.attempt_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def utcnow_for_tests() -> datetime:
    from datetime import UTC
    from datetime import datetime as _datetime

    return _datetime.now(tz=UTC)

# ---------------------------------------------------------------------------
# S04 crawler operations support
# ---------------------------------------------------------------------------


async def _seed_tracked_telegram_channel(
    session: AsyncSession,
    *,
    platform_id: str,
    title: str,
    username: str | None = None,
    is_paused: bool = False,
    last_read_post_id: str | None = None,
) -> SourceChannel:
    """Helper that creates a tracked Telegram channel the crawler is allowed to ingest from."""

    channel = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id=platform_id,
        username=username,
        title=title,
        is_active=True,
        is_paused=is_paused,
        last_read_post_id=last_read_post_id,
    )
    session.add(channel)
    await session.commit()
    await session.refresh(channel)
    return channel


async def test_crawler_operations_list_sessions_populates_owned_channel_count(
    migrated_db_session: AsyncSession,
) -> None:
    """T03: ``CrawlerOperationsService.list_sessions`` must compute owned channel counts."""

    from memexpert.crawlers.telegram.client import FakeTelegramClient
    from memexpert.crawlers.telegram.runtime import TelegramCrawlerRuntime
    from memexpert.models.content import TelegramSession
    from memexpert.models.enums import TelegramSessionStatus
    from memexpert.services.crawler_operations import CrawlerOperationsService

    migrated_db_session.add(
        TelegramSession(
            name="primary",
            display_name="Primary",
            status=TelegramSessionStatus.ACTIVE,
        )
    )
    migrated_db_session.add(
        TelegramSession(
            name="empty",
            display_name="Empty",
            status=TelegramSessionStatus.STOPPED,
        )
    )
    await migrated_db_session.commit()

    await _seed_tracked_telegram_channel(
        migrated_db_session,
        platform_id="owned_one",
        title="Owned One",
    )
    await _seed_tracked_telegram_channel(
        migrated_db_session,
        platform_id="owned_two",
        title="Owned Two",
    )
    # Bind both channels to the primary session so the operations surface
    # can count them.
    primary_session_id = await migrated_db_session.scalar(
        select(TelegramSession.id).where(TelegramSession.name == "primary"),
    )
    assert primary_session_id is not None
    primary_channels = (
        await migrated_db_session.execute(
            select(SourceChannel).where(SourceChannel.platform_id.in_(["owned_one", "owned_two"])),
        )
    ).scalars().all()
    for channel in primary_channels:
        channel.telegram_session_id = primary_session_id
    await migrated_db_session.commit()

    ingest_service = PipelineCrawlerIngestService.from_settings(
        migrated_db_session,
        settings=Settings(),
        storage_client=FakeStorageClient(),
    )
    runtime = TelegramCrawlerRuntime(
        ingest_service=ingest_service,
        telegram_client=FakeTelegramClient(),
        session=migrated_db_session,
        settings=Settings(),
    )
    service = CrawlerOperationsService(session=migrated_db_session, runtime=runtime)

    sessions_by_name = {row.name: row for row in await service.list_sessions()}
    assert sessions_by_name["primary"].owned_channel_count == 2
    assert sessions_by_name["empty"].owned_channel_count == 0
