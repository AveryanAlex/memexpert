"""Integration tests for the operator-facing content-pipeline ingest service."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.models.content import Meme, MemeFile, MemeSource, PipelineStageJournal
from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus, SourcePlatform
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent, ContentPipelineUploadMetadata
from memexpert.services import (
    ContentPipelineService,
    PipelineIngestError,
    PipelineSourceConflictError,
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


def build_png_bytes(*, color: tuple[int, int, int]) -> bytes:
    """Generate a tiny PNG image payload entirely in memory for ingest tests."""

    image = Image.new("RGB", (8, 8), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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
    assert publisher.events[0].meme_file_id == item.meme_file_id
    assert publisher.events[0].meme_id == item.meme_id
    assert publisher.file_visible_at_publish == [True]
    assert publisher.transcode_visible_at_publish == [True]

    async with postgres_session_factory() as session:
        persisted_file_result = await session.execute(
            select(MemeFile).where(MemeFile.id == item.meme_file_id)
        )
        persisted_source_result = await session.execute(
            select(MemeSource).where(MemeSource.file_id == item.meme_file_id)
        )
        persisted_journal_result = await session.execute(
            select(PipelineStageJournal)
            .where(PipelineStageJournal.meme_file_id == item.meme_file_id)
            .order_by(PipelineStageJournal.stage.asc())
        )

        persisted_file = persisted_file_result.scalar_one()
        persisted_source = persisted_source_result.scalar_one()
        persisted_journal_rows = persisted_journal_result.scalars().all()

        stored_body = storage_client.put_calls[0]["Body"]
        assert isinstance(stored_body, bytes)
        assert persisted_file.s3_original_key == item.original_object_key
        assert persisted_file.width == 8
        assert persisted_file.height == 8
        assert persisted_file.file_size_bytes == len(stored_body)
        assert persisted_source.source_id == "memexpert_channel"
        assert persisted_source.post_id == "1001"
        assert len(persisted_journal_rows) == 2


async def test_duplicate_upload_short_circuits_with_terminal_journal_state_and_no_second_publish(
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
    payload = build_png_bytes(color=(0, 255, 0))

    first_item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="channel-alpha",
            post_id="2001",
            views=12,
        ),
        filename="alpha.png",
        content_type="image/png",
        media_bytes=payload,
    )
    duplicate_item = await service.create_upload(
        metadata=ContentPipelineUploadMetadata(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="channel-beta",
            post_id="2002",
            views=8,
        ),
        filename="beta.png",
        content_type="image/png",
        media_bytes=payload,
    )

    assert len(storage_client.put_calls) == 2
    assert len(publisher.events) == 1
    assert first_item.meme_id == duplicate_item.meme_id
    assert duplicate_item.meme_file_id != first_item.meme_file_id
    assert duplicate_item.current_stage is ContentPipelineStage.INGEST
    assert duplicate_item.current_status is ContentPipelineStageStatus.DUPLICATE
    assert duplicate_item.normalized_reason == "duplicate_perceptual_hash"
    assert duplicate_item.last_error_text is not None
    assert str(first_item.meme_file_id) in duplicate_item.last_error_text
    assert tuple((stage.stage, stage.status) for stage in duplicate_item.stages) == (
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.DUPLICATE),
    )

    assert await _count_pipeline_rows(postgres_session_factory) == (1, 2, 2, 3)


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


async def test_source_conflict_rejects_reused_provenance_and_cleans_up_uploaded_object(
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
    metadata = ContentPipelineUploadMetadata(
        source_platform=SourcePlatform.TELEGRAM,
        source_id="channel-collision",
        post_id="4001",
        views=1,
    )

    _ = await service.create_upload(
        metadata=metadata,
        filename="first.png",
        content_type="image/png",
        media_bytes=build_png_bytes(color=(10, 20, 30)),
    )

    with pytest.raises(PipelineSourceConflictError, match="already attached"):
        _ = await service.create_upload(
            metadata=metadata,
            filename="second.png",
            content_type="image/png",
            media_bytes=build_png_bytes(color=(30, 20, 10)),
        )

    assert len(storage_client.put_calls) == 2
    assert len(storage_client.delete_calls) == 1
    assert len(publisher.events) == 1
    assert await _count_pipeline_rows(postgres_session_factory) == (1, 1, 1, 2)


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
