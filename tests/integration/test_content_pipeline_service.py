"""Integration tests for the operator-facing content-pipeline ingest service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import memexpert.services.content_pipeline as content_pipeline_module
from memexpert.core.classification import ClassificationResult
from memexpert.core.config import Settings
from memexpert.core.media import NormalizedMediaResult, UploadMediaDetails
from memexpert.core.ocr import OCRExtractionResult
from memexpert.core.qdrant import QdrantSimilarityMatch
from memexpert.core.voyage import VoyageEmbeddingResult
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemePopularitySnapshot,
    MemeSource,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineUploadMetadata,
)
from memexpert.services import (
    ContentPipelineService,
    PipelineIngestError,
    PipelineMergeTransactionError,
    PipelinePayloadTooLargeError,
    PipelinePublishError,
    PipelineReplayNotAllowedError,
    PipelineStorageError,
)
from memexpert.services.content_merge import MERGE_REASON_HIGH_SIMILARITY

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class FakeStorageClient:
    """Small sync S3-compatible client used to observe ingest side effects in tests."""

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
class RecordingPublisher:
    """Async publisher double that verifies DB durability before publish is attempted."""

    session_factory: async_sessionmaker[AsyncSession] | None = None
    fail_with: Exception | None = None
    events: list[ContentPipelineDispatchEvent] = field(default_factory=list)
    file_visible_at_publish: list[bool] = field(default_factory=list)
    transcode_visible_at_publish: list[bool] = field(default_factory=list)

    async def __call__(self, event: ContentPipelineDispatchEvent) -> None:
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

        self.events.append(event)


@dataclass(slots=True)
class StartableBroker:
    """FastStream-like broker double that requires start() before publish()."""

    started: bool = False
    start_calls: int = 0
    publish_calls: list[dict[str, object]] = field(default_factory=list)

    async def ping(self) -> bool:
        return self.started

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True

    async def publish(self, payload: object, **kwargs: object) -> None:
        if not self.started:
            raise RuntimeError("publish called before broker.start()")
        self.publish_calls.append({"payload": payload, **kwargs})


@dataclass(slots=True)
class FakeMediaProcessor:
    """Typed media boundary double used to make service tests deterministic."""

    inspect_result: UploadMediaDetails

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        return self.inspect_result

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult:
        raise AssertionError("normalize_for_web should not be called by these service tests")

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        raise AssertionError("extract_preview_frame should not be called by these service tests")


def build_png_bytes(*, color: tuple[int, int, int]) -> bytes:
    """Generate a tiny PNG image payload entirely in memory for ingest tests."""

    image = Image.new("RGB", (8, 8), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def build_normalized_media_result(meme_file_id: uuid.UUID) -> NormalizedMediaResult:
    """Create a stable normalized transcode artifact for service assertions."""

    return NormalizedMediaResult(
        mime_type="video/mp4",
        width=720,
        height=720,
        file_size_bytes=4096,
        quality_score=0.82,
        blur_hash="L4AS~q00~q.8%MRjM{Rj00IU%MRj",
        web_video_object_key=f"pipeline/derived/{meme_file_id}/web.mp4",
        web_video_bytes=b"normalized-web-video",
    )


def build_ocr_result(*, source_object_key: str) -> OCRExtractionResult:
    """Create a stable OCR result for service assertions."""

    return OCRExtractionResult(
        engine="paddleocr",
        fallback_engine="qwen2.5-vl-2b",
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
    """Build unique upload metadata so repeated ingests get distinct perceptual hashes."""

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
    publisher: RecordingPublisher | None = None,
) -> ContentPipelineService:
    """Return a service wired to a fake media processor with a caller-provided phash."""

    return ContentPipelineService(
        session,
        storage_client=FakeStorageClient(),
        publisher=publisher or RecordingPublisher(),
        media_processor=FakeMediaProcessor(inspect_result=_make_distinct_upload_media_details(tag=phash_tag)),
    )


async def _drive_to_embed_pending(
    session: AsyncSession,
    *,
    source_id: str,
    post_id: str,
    phash_tag: str,
    publisher: RecordingPublisher | None = None,
    filename: str = "embed-ready.png",
) -> tuple[uuid.UUID, NormalizedMediaResult, ContentPipelineService]:
    """Create + transcode + OCR a pipeline item up to the embed-pending state."""

    service = _build_service_with_distinct_phash(
        session,
        phash_tag=phash_tag,
        publisher=publisher,
    )
    meme_file_id = await _create_upload(
        service,
        filename=filename,
        content_type="image/png",
        media_bytes=b"fake-upload-bytes",
        source_id=source_id,
        post_id=post_id,
    )
    normalized = build_normalized_media_result(meme_file_id)
    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    await service.complete_ocr_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_ocr_result(source_object_key=normalized.web_video_object_key),
    )
    return meme_file_id, normalized, service


async def _count_pipeline_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int, int]:
    async with session_factory() as session:
        meme_count_result = await session.execute(select(func.count()).select_from(Meme))
        meme_file_count_result = await session.execute(select(func.count()).select_from(MemeFile))
        source_count_result = await session.execute(select(func.count()).select_from(MemeSource))
        journal_count_result = await session.execute(select(func.count()).select_from(PipelineStageJournal))
        return (
            meme_count_result.scalar_one(),
            meme_file_count_result.scalar_one(),
            source_count_result.scalar_one(),
            journal_count_result.scalar_one(),
        )


async def _create_upload(
    service: ContentPipelineService,
    *,
    filename: str,
    content_type: str,
    media_bytes: bytes,
    source_id: str,
    post_id: str,
) -> uuid.UUID:
    item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id=source_id,
            post_id=post_id,
            views=42,
        ),
        filename=filename,
        content_type=content_type,
        media_bytes=media_bytes,
    )
    return item.meme_file_id


async def test_create_upload_persists_before_publish_and_exposes_pending_downstream_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher(session_factory=postgres_session_factory)
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="memexpert_channel",
            post_id="1001",
            views=42,
        ),
        filename="sample.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(255, 0, 0)),
    )

    assert item.current_stage is ContentPipelineStage.TRANSCODE
    assert item.current_status is ContentPipelineStageStatus.PENDING
    assert item.original_object_key.endswith("/original.png")
    assert tuple((stage.stage, stage.status) for stage in item.stages) == (
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.SUCCEEDED),
        (ContentPipelineStage.TRANSCODE, ContentPipelineStageStatus.PENDING),
    )
    assert len(storage_client.put_calls) == 1
    assert storage_client.delete_calls == []
    assert len(publisher.events) == 1
    assert publisher.file_visible_at_publish == [True]
    assert publisher.transcode_visible_at_publish == [True]

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == item.meme_file_id))
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == item.meme_id))

    assert persisted_file is not None
    assert persisted_meme is not None
    assert persisted_file.width == 8
    assert persisted_file.height == 8
    assert persisted_file.mime_type == "image/png"
    assert persisted_meme.media_type is ContentKind.IMAGE


@pytest.mark.parametrize(
    ("media_type", "filename", "content_type"),
    [
        (ContentKind.GIF, "animated.gif", "image/gif"),
        (ContentKind.VIDEO, "clip.mp4", "video/mp4"),
    ],
)
async def test_create_upload_accepts_gif_and_video_contracts(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    media_type: ContentKind,
    filename: str,
    content_type: str,
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
        media_processor=FakeMediaProcessor(
            inspect_result=UploadMediaDetails(
                media_type=media_type,
                mime_type=content_type,
                width=640,
                height=360,
                file_size_bytes=7,
                perceptual_hash="b" * 16,
            )
        ),
    )

    item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id=f"{media_type.value}-source",
            post_id=f"{media_type.value}-post",
            views=3,
        ),
        filename=filename,
        content_type=content_type,
        media_bytes=b"payload!",
    )

    assert item.current_stage is ContentPipelineStage.TRANSCODE
    assert item.current_status is ContentPipelineStageStatus.PENDING
    assert len(publisher.events) == 1

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == item.meme_file_id))
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == item.meme_id))

    assert persisted_file is not None
    assert persisted_meme is not None
    assert persisted_file.mime_type == content_type
    assert persisted_file.width == 640
    assert persisted_file.height == 360
    assert persisted_meme.media_type is media_type


@pytest.mark.parametrize(
    ("media_type", "settings_payload", "content_type", "filename"),
    [
        (ContentKind.IMAGE, {"pipeline_image_upload_max_bytes": 4}, "image/png", "too-large.png"),
        (ContentKind.GIF, {"pipeline_gif_upload_max_bytes": 4}, "image/gif", "too-large.gif"),
        (ContentKind.VIDEO, {"pipeline_video_upload_max_bytes": 4}, "video/mp4", "too-large.mp4"),
    ],
)
async def test_create_upload_enforces_split_upload_size_limits(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    media_type: ContentKind,
    settings_payload: dict[str, int],
    content_type: str,
    filename: str,
) -> None:
    service = ContentPipelineService(
        migrated_db_session,
        settings=Settings.model_validate(settings_payload),
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(),
        media_processor=FakeMediaProcessor(
            inspect_result=UploadMediaDetails(
                media_type=media_type,
                mime_type=content_type,
                width=320,
                height=240,
                file_size_bytes=5,
                perceptual_hash="c" * 16,
            )
        ),
    )

    with pytest.raises(PipelinePayloadTooLargeError, match="4-byte limit"):
        _ = await service.create_upload(
            metadata=ContentPipelineUploadMetadata(
                source_platform=SourcePlatform.TELEGRAM,
                source_id=f"limit-{media_type.value}",
                post_id=f"limit-{media_type.value}",
            ),
            filename=filename,
            content_type=content_type,
            media_bytes=b"12345",
        )

    assert await _count_pipeline_rows(postgres_session_factory) == (0, 0, 0, 0)


async def test_create_upload_starts_lazy_broker_before_real_publish(
    migrated_db_session: AsyncSession,
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    broker = StartableBroker()

    async def fake_ensure_pipeline_broker_started(*_: object, **__: object) -> object:
        await broker.start()
        return broker

    monkeypatch.setattr(content_pipeline_module, "ensure_pipeline_broker_started", fake_ensure_pipeline_broker_started)

    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
    )

    item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="broker-start",
            post_id="1002",
            views=7,
        ),
        filename="broker-start.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(12, 34, 56)),
    )

    assert item.current_stage is ContentPipelineStage.TRANSCODE
    assert item.current_status is ContentPipelineStageStatus.PENDING
    assert broker.start_calls == 1
    assert len(broker.publish_calls) == 1


async def test_complete_transcode_stage_persists_derivative_metadata_and_queues_ocr(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    meme_file_id = await _create_upload(
        service,
        filename="transcode.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(123, 45, 67)),
        source_id="stage-chain",
        post_id="6001",
    )
    normalized = build_normalized_media_result(meme_file_id)

    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )
    after_transcode = await service.get_item(meme_file_id)

    assert after_transcode.current_stage is ContentPipelineStage.OCR
    assert after_transcode.current_status is ContentPipelineStageStatus.PENDING
    assert after_transcode.web_video_object_key == normalized.web_video_object_key
    assert publisher.events[-1].event_type is ContentPipelineEventType.MEME_TRANSCODED
    assert publisher.events[-1].stage is ContentPipelineStage.OCR

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))

    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.PROCESSING
    assert persisted_file.s3_web_video_key == normalized.web_video_object_key
    assert persisted_file.mime_type == "video/mp4"
    assert persisted_file.width == 720
    assert persisted_file.height == 720
    assert persisted_file.file_size_bytes == 4096
    assert persisted_file.quality_score == pytest.approx(0.82)
    assert persisted_file.blur_hash == normalized.blur_hash


async def test_complete_ocr_stage_persists_durable_result_and_keeps_meme_unready(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    meme_file_id = await _create_upload(
        service,
        filename="ocr.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(22, 33, 44)),
        source_id="ocr-source",
        post_id="7001",
    )
    normalized = build_normalized_media_result(meme_file_id)
    await service.complete_transcode_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=normalized,
    )

    await service.complete_ocr_stage(
        meme_file_id=meme_file_id,
        attempt=1,
        event_id=uuid.uuid7(),
        result=build_ocr_result(source_object_key=normalized.web_video_object_key),
    )
    after_ocr = await service.get_item(meme_file_id)

    assert after_ocr.current_stage is ContentPipelineStage.EMBED
    assert after_ocr.current_status is ContentPipelineStageStatus.PENDING
    assert publisher.events[-1].event_type is ContentPipelineEventType.MEME_OCR_DONE
    assert publisher.events[-1].stage is ContentPipelineStage.EMBED

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
    assert persisted_ocr.fallback_engine == "qwen2.5-vl-2b"
    assert persisted_ocr.fallback_used is True
    assert persisted_ocr.low_confidence is True
    assert persisted_ocr.confidence == pytest.approx(0.41)
    assert persisted_ocr.extracted_text == "deadline\nmonday"
    assert persisted_ocr.source_object_key == normalized.web_video_object_key


async def test_storage_failure_prevents_rows_and_publish(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient(fail_put_with=RuntimeError("storage unavailable"))
    publisher = RecordingPublisher()
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    with pytest.raises(PipelineStorageError, match="store the uploaded original"):
        _ = await service.create_upload(
            metadata=ContentPipelineUploadMetadata(
                source_platform=SourcePlatform.TELEGRAM,
                source_id="broken-storage",
                post_id="3001",
            ),
            filename="broken.png",
            content_type="image/png",
            media_bytes=build_png_bytes(color=(0, 0, 255)),
        )

    assert storage_client.delete_calls == []
    assert publisher.events == []
    assert await _count_pipeline_rows(postgres_session_factory) == (0, 0, 0, 0)


async def test_mark_stage_success_publish_failure_marks_next_stage_failed_and_keeps_file_not_ready(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    setup_service = ContentPipelineService(
        migrated_db_session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(),
    )
    meme_file_id = await _create_upload(
        setup_service,
        filename="publish-failure.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(90, 40, 20)),
        source_id="publish-failure",
        post_id="6003",
    )

    failing_service = ContentPipelineService(
        migrated_db_session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(fail_with=RuntimeError("broker unavailable")),
    )

    with pytest.raises(PipelinePublishError, match="downstream dispatch failed"):
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

    sorted_rows = sorted(
        persisted_rows,
        key=lambda row: {
            ContentPipelineStage.INGEST: 0,
            ContentPipelineStage.TRANSCODE: 1,
            ContentPipelineStage.OCR: 2,
        }[row.stage],
    )

    assert persisted_file is not None
    assert persisted_file.status is ContentProcessingStatus.FAILED
    assert tuple((row.stage, row.status, row.normalized_reason) for row in sorted_rows) == (
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.SUCCEEDED, None),
        (ContentPipelineStage.TRANSCODE, ContentPipelineStageStatus.SUCCEEDED, None),
        (ContentPipelineStage.OCR, ContentPipelineStageStatus.FAILED, "publish_failed"),
    )


async def test_replay_item_rejects_stage_that_has_not_been_dispatched_yet(
    migrated_db_session: AsyncSession,
) -> None:
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(),
    )
    meme_file_id = await _create_upload(
        service,
        filename="replay-guard.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(10, 11, 12)),
        source_id="replay-guard",
        post_id="6002",
    )

    with pytest.raises(PipelineReplayNotAllowedError, match="has no durable journal row"):
        _ = await service.replay_item(meme_file_id, stage=ContentPipelineStage.EMBED)


async def test_replay_publish_failure_restores_previous_failed_stage_snapshot(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(),
    )
    meme_file_id = await _create_upload(
        service,
        filename="replay-restore.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(1, 2, 3)),
        source_id="replay-restore",
        post_id="6004",
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

    failing_replay_service = ContentPipelineService(
        migrated_db_session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(fail_with=RuntimeError("republish failed")),
    )

    with pytest.raises(PipelinePublishError, match="Replay was reserved"):
        _ = await failing_replay_service.replay_item(meme_file_id, stage=ContentPipelineStage.TRANSCODE)

    async with postgres_session_factory() as session:
        restored_item = await ContentPipelineService(session).get_item(meme_file_id)

    transcode_stage = next(stage for stage in restored_item.stages if stage.stage is ContentPipelineStage.TRANSCODE)
    assert restored_item.current_stage is ContentPipelineStage.TRANSCODE
    assert restored_item.current_status is ContentPipelineStageStatus.FAILED
    assert transcode_stage.attempt_count == 1
    assert transcode_stage.last_event_id == failed_event_id
    assert transcode_stage.normalized_reason == "forced_failure"


async def test_db_failure_rolls_back_rows_cleans_up_storage_and_skips_publish(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    publisher = RecordingPublisher()
    service = ContentPipelineService(
        migrated_db_session,
        storage_client=storage_client,
        publisher=publisher,
    )

    async def fail_commit() -> None:
        raise SQLAlchemyError("forced commit failure")

    monkeypatch.setattr(migrated_db_session, "commit", fail_commit)

    with pytest.raises(PipelineIngestError, match="persist the upload"):
        _ = await service.create_upload(
            metadata=ContentPipelineUploadMetadata(
                source_platform=SourcePlatform.TELEGRAM,
                source_id="db-failure",
                post_id="5001",
            ),
            filename="rollback.png",
            content_type="image/png",
            media_bytes=build_png_bytes(color=(200, 100, 0)),
        )

    assert len(storage_client.put_calls) == 1
    assert len(storage_client.delete_calls) == 1
    assert publisher.events == []
    assert await _count_pipeline_rows(postgres_session_factory) == (0, 0, 0, 0)


async def test_complete_embed_stage_without_matches_persists_embedding_and_queues_classify(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    publisher = RecordingPublisher()
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="embed-no-match",
        post_id="9001",
        phash_tag="a",
        publisher=publisher,
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
    after_embed = await service.get_item(meme_file_id)
    assert after_embed.current_stage is ContentPipelineStage.CLASSIFY
    assert after_embed.current_status is ContentPipelineStageStatus.PENDING
    assert publisher.events[-1].event_type is ContentPipelineEventType.MEME_EMBEDDED
    assert publisher.events[-1].stage is ContentPipelineStage.CLASSIFY

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
    publisher = RecordingPublisher()
    older_meme_file_id, older_normalized, older_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="merge-older",
        post_id="9100",
        phash_tag="c",
        publisher=publisher,
        filename="older.png",
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
    migrated_db_session.add(
        MemePopularitySnapshot(
            meme_id=older_meme_id,
            source_views=120,
            source_reactions=10,
            source_reposts=1,
            platform_views=60,
            platform_sends=2,
            platform_saves=3,
            platform_likes=4,
            popularity_score=1.5,
        )
    )
    older_meme = await migrated_db_session.scalar(select(Meme).where(Meme.id == older_meme_id))
    assert older_meme is not None
    older_meme.popularity_score = 0.5
    older_meme.like_count = 3
    await migrated_db_session.commit()

    newer_meme_file_id, newer_normalized, newer_service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="merge-newer",
        post_id="9101",
        phash_tag="d",
        publisher=publisher,
        filename="newer.png",
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
        moved_popularity_rows = (
            await session.execute(
                select(MemePopularitySnapshot).where(MemePopularitySnapshot.meme_id == older_meme_id)
            )
        ).scalars().all()
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
    assert len(moved_popularity_rows) == 1
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
    publisher = RecordingPublisher()
    meme_file_id, _, service = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="classify-truth",
        post_id="9300",
        phash_tag="h",
        publisher=publisher,
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

    after_classify = await service.get_item(meme_file_id)
    assert after_classify.current_stage is ContentPipelineStage.SYNC_QDRANT
    assert publisher.events[-1].event_type is ContentPipelineEventType.MEME_READY
    assert publisher.events[-1].stage is ContentPipelineStage.SYNC_QDRANT

    async with postgres_session_factory() as session:
        persisted_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
        assert persisted_file is not None
        persisted_meme = await session.scalar(select(Meme).where(Meme.id == persisted_file.meme_id))

    assert persisted_file.status is ContentProcessingStatus.READY
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


async def test_complete_embed_stage_publish_failure_marks_classify_failed(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_file_id, _, _ = await _drive_to_embed_pending(
        migrated_db_session,
        source_id="embed-publish-fail",
        post_id="9500",
        phash_tag="k",
    )

    failing_service = ContentPipelineService(
        migrated_db_session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(fail_with=RuntimeError("broker unavailable")),
    )
    with pytest.raises(PipelinePublishError, match="downstream dispatch failed"):
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

    stage_rows = {row.stage: row for row in persisted_rows}
    assert stage_rows[ContentPipelineStage.EMBED].status is ContentPipelineStageStatus.SUCCEEDED
    assert stage_rows[ContentPipelineStage.CLASSIFY].status is ContentPipelineStageStatus.FAILED
    assert stage_rows[ContentPipelineStage.CLASSIFY].normalized_reason == "publish_failed"


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
    )

    async with postgres_session_factory() as stash_session:
        newer_file = await stash_session.scalar(select(MemeFile).where(MemeFile.id == newer_meme_file_id))
        assert newer_file is not None
        newer_meme_id = newer_file.meme_id

    from memexpert.services import content_merge as content_merge_module

    async def fake_transfer(self: object, **_: object) -> tuple[uuid.UUID, ...]:
        _ = self
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
) -> tuple[uuid.UUID, ContentPipelineService]:
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


async def test_meili_sync_stub_methods_still_raise_not_implemented_error(
    migrated_db_session: AsyncSession,
) -> None:
    """T02 implemented the Qdrant path, but the Meilisearch stubs are still locked for T03."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="stub-not-impl",
        post_id="9900",
        phash_tag="s",
        input_hash_seed="9",
    )

    with pytest.raises(NotImplementedError, match="T02/T03 implement this method"):
        _ = await service.complete_sync_meili_stage(
            meme_file_id=meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            payload_preview={},
        )
    with pytest.raises(NotImplementedError, match="T02/T03 implement this method"):
        _ = await service.fail_sync_meili_stage(
            meme_file_id=meme_file_id,
            attempt=1,
            event_id=uuid.uuid7(),
            normalized_reason="sync_meili_timeout",
            last_error_text="boom",
        )


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
        _ = await service.replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)
    with pytest.raises(PipelineReplayNotAllowedError, match="cannot replay sync targets"):
        _ = await service.replay_sync_target_batch([meme_file_id], SyncTargetKind.MEILISEARCH)


async def test_replay_sync_target_meili_stub_still_raises_not_implemented_after_classify_success(
    migrated_db_session: AsyncSession,
) -> None:
    """T02 wired the Qdrant replay path; Meilisearch replay is still stubbed for T03."""

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="replay-stub",
        post_id="9920",
        phash_tag="t",
        input_hash_seed="a",
    )

    with pytest.raises(NotImplementedError, match="T03 implements Meilisearch sync replay"):
        _ = await service.replay_sync_target(meme_file_id, SyncTargetKind.MEILISEARCH)
    with pytest.raises(NotImplementedError, match="T03 implements Meilisearch sync replay"):
        _ = await service.replay_sync_target_batch(
            [meme_file_id],
            SyncTargetKind.MEILISEARCH,
        )


async def test_replay_sync_target_batch_refuses_batches_beyond_the_max(
    migrated_db_session: AsyncSession,
) -> None:
    from memexpert.services.content_pipeline import SYNC_REPLAY_BATCH_MAX

    meme_file_id, service = await _drive_to_classify_succeeded(
        migrated_db_session,
        source_id="batch-max",
        post_id="9940",
        phash_tag="v",
        input_hash_seed="c",
    )
    oversized_batch = [meme_file_id] * (SYNC_REPLAY_BATCH_MAX + 1)

    with pytest.raises(PipelineReplayNotAllowedError, match="exceeds the configured maximum"):
        _ = await service.replay_sync_target_batch(oversized_batch, SyncTargetKind.QDRANT)


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
        replay_service = ContentPipelineService(
            replay_session,
            storage_client=FakeStorageClient(),
            publisher=RecordingPublisher(),
            media_processor=FakeMediaProcessor(
                inspect_result=_make_distinct_upload_media_details(tag="q"),
            ),
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
        replay_service = ContentPipelineService(
            replay_session,
            storage_client=FakeStorageClient(),
            publisher=RecordingPublisher(),
            media_processor=FakeMediaProcessor(
                inspect_result=_make_distinct_upload_media_details(tag="p"),
            ),
        )
        accepted = await replay_service.replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)

    async with postgres_session_factory() as fail_session:
        fail_service = ContentPipelineService(
            fail_session,
            storage_client=FakeStorageClient(),
            publisher=RecordingPublisher(),
            media_processor=FakeMediaProcessor(
                inspect_result=_make_distinct_upload_media_details(tag="p"),
            ),
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

    accepted = await service.replay_sync_target(meme_file_id, SyncTargetKind.QDRANT)
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
    empty_detail = await service.get_item_detail(meme_file_id)
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
        read_service = ContentPipelineService(
            read_session,
            storage_client=FakeStorageClient(),
            publisher=RecordingPublisher(),
            media_processor=FakeMediaProcessor(
                inspect_result=_make_distinct_upload_media_details(tag="w"),
            ),
        )
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


