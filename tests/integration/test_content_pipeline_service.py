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
from memexpert.core.config import Settings
from memexpert.core.media import NormalizedMediaResult, UploadMediaDetails
from memexpert.core.ocr import OCRExtractionResult
from memexpert.models.content import Meme, MemeFile, MemeFileOCRResult, MemeSource, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    SourcePlatform,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineUploadMetadata,
)
from memexpert.services import (
    ContentPipelineService,
    PipelineIngestError,
    PipelinePayloadTooLargeError,
    PipelinePublishError,
    PipelineReplayNotAllowedError,
    PipelineStorageError,
)

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
