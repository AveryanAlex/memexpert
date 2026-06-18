"""Integration tests for worker-side ingest-request materialization and outbox publishing."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.config import Settings
from memexpert.core.storage import build_temp_original_object_key
from memexpert.ingest.materializer import PipelineIngestMaterializer
from memexpert.media.contracts import MediaValidationError, NormalizedMediaResult, UploadMediaDetails
from memexpert.models.base import utcnow
from memexpert.models.content import (
    BlockedPerceptualHash,
    Meme,
    MemeFile,
    MemeSource,
    PipelineIngestRequest,
    PipelineOutboxEvent,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    ModerationReason,
    PipelineIngestRequestStatus,
    PipelineOutboxEventStatus,
    SourceAttachReason,
    SourcePlatform,
)
from memexpert.pipeline.outbox import PipelineOutboxPublisher
from memexpert.schemas.content_pipeline import ContentPipelineEventType
from memexpert.services import PipelineIngestError

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class StoredObject:
    body: bytes
    content_type: str


@dataclass(slots=True)
class FakeStorageBody:
    payload: bytes

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        return None


@dataclass(slots=True)
class FakeStorageClient:
    objects: dict[str, StoredObject] = field(default_factory=dict)
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
        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
                "ContentLength": ContentLength,
            }
        )
        self.objects[Key] = StoredObject(body=Body, content_type=ContentType)
        return {"ETag": "fake"}

    def get_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        stored = self.objects[Key]
        return {"Body": FakeStorageBody(stored.body)}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        _ = Bucket
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop(Key, None)
        return {"DeleteMarker": True}


@dataclass(slots=True)
class FakeMediaProcessor:
    inspect_result: UploadMediaDetails | None = None
    inspect_error: Exception | None = None
    inspect_calls: int = 0

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        self.inspect_calls += 1
        if self.inspect_error is not None:
            raise self.inspect_error
        assert self.inspect_result is not None
        return self.inspect_result

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult:
        _ = (meme_file_id, filename, content_type, media_bytes)
        raise AssertionError("normalize_for_web should not be called by materializer tests")

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("extract_preview_frame should not be called by materializer tests")


@dataclass(slots=True)
class FakeBroker:
    fail_routing_keys: set[str] = field(default_factory=set)
    publish_calls: list[dict[str, object]] = field(default_factory=list)

    async def publish(self, payload: object, **kwargs: object) -> object:
        self.publish_calls.append({"payload": payload, **kwargs})
        routing_key = kwargs.get("routing_key")
        if isinstance(routing_key, str) and routing_key in self.fail_routing_keys:
            raise RuntimeError(f"forced publish failure for {routing_key}")
        return None


def _upload_details(*, perceptual_hash: str = "a" * 16) -> UploadMediaDetails:
    return UploadMediaDetails(
        media_type=ContentKind.IMAGE,
        mime_type="image/png",
        width=8,
        height=8,
        file_size_bytes=13,
        perceptual_hash=perceptual_hash,
    )


async def _seed_raw_request(
    session: AsyncSession,
    storage_client: FakeStorageClient,
    *,
    media_bytes: bytes = b"raw-materializer-bytes",
    source_id: str = "materializer-source",
    post_id: str = "1",
    views: int = 12,
) -> PipelineIngestRequest:
    ingest_request_id = uuid.uuid7()
    temp_key = build_temp_original_object_key(ingest_request_id, "raw.png", settings=Settings())
    storage_client.objects[temp_key] = StoredObject(body=media_bytes, content_type="image/png")
    ingest_request = PipelineIngestRequest(
        id=ingest_request_id,
        source_platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        source_metadata={"views": views},
        declared_filename="raw.png",
        declared_content_type="image/png",
        temp_original_object_key=temp_key,
        sha256_hex=hashlib.sha256(media_bytes).hexdigest(),
        file_size_bytes=len(media_bytes),
        status=PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
        attempt_count=0,
    )
    session.add(ingest_request)
    await session.commit()
    return ingest_request


async def _seed_existing_meme_file(
    session: AsyncSession,
    *,
    perceptual_hash: str,
    sha256_hex: str = "0" * 64,
) -> MemeFile:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.NONE,
        is_public=False,
    )
    session.add(meme)
    await session.flush()
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.PENDING,
        width=8,
        height=8,
        file_size_bytes=13,
        mime_type="image/png",
        s3_original_key=f"pipeline/originals/{meme_file_id}/original.png",
        perceptual_hash=perceptual_hash,
        sha256_hex=sha256_hex,
        ingest_origin=IngestFileOrigin.NEW_MEME,
    )
    session.add(meme_file)
    await session.commit()
    return meme_file


async def test_materializer_new_content_creates_content_rows_outbox_and_cleans_temp(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(migrated_db_session, storage_client, views=15)
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="1" * 16))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert result.materialized_meme_file_id is not None
    assert result.outbox_event_id is not None
    assert media_processor.inspect_calls == 1
    assert ingest_request.temp_original_object_key not in storage_client.objects
    assert any(call["Key"] == ingest_request.temp_original_object_key for call in storage_client.delete_calls)

    async with postgres_session_factory() as session:
        request = await session.get(PipelineIngestRequest, ingest_request.id)
        meme_file = await session.get(MemeFile, result.materialized_meme_file_id)
        sources = (await session.execute(select(MemeSource))).scalars().all()
        stage_rows = (await session.execute(select(PipelineStageJournal))).scalars().all()
        outbox_rows = (await session.execute(select(PipelineOutboxEvent))).scalars().all()

    assert request is not None
    assert request.status is PipelineIngestRequestStatus.MATERIALIZED
    assert meme_file is not None
    assert meme_file.ingest_origin is IngestFileOrigin.NEW_MEME
    assert meme_file.s3_original_key in storage_client.objects
    assert sources[0].file_id == meme_file.id
    assert sources[0].views == 15
    assert sources[0].attach_reason is SourceAttachReason.NEW_FILE
    assert {(row.stage, row.status) for row in stage_rows} == {
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.SUCCEEDED),
        (ContentPipelineStage.TRANSCODE, ContentPipelineStageStatus.PENDING),
    }
    assert len(outbox_rows) == 1
    assert outbox_rows[0].event_type == ContentPipelineEventType.MEME_CREATED.value
    assert outbox_rows[0].routing_key == "pipeline.transcode"
    assert outbox_rows[0].payload["meme_file_id"] == str(meme_file.id)


async def test_materializer_phash_duplicate_creates_new_file_under_existing_meme(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    perceptual_hash = "2" * 16
    existing_file = await _seed_existing_meme_file(migrated_db_session, perceptual_hash=perceptual_hash)
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="phash-source",
        post_id="2",
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert result.materialized_meme_id == existing_file.meme_id
    assert result.matched_meme_file_id == existing_file.id

    async with postgres_session_factory() as session:
        new_file = await session.get(MemeFile, result.materialized_meme_file_id)
        new_source = (
            await session.execute(select(MemeSource).where(MemeSource.file_id == result.materialized_meme_file_id))
        ).scalar_one()
        outbox_count = await session.scalar(select(func.count()).select_from(PipelineOutboxEvent))

    assert new_file is not None
    assert new_file.meme_id == existing_file.meme_id
    assert new_file.ingest_origin is IngestFileOrigin.PHASH_EXACT_EXISTING_MEME
    assert new_file.matched_meme_file_id == existing_file.id
    assert new_source.attach_reason is SourceAttachReason.PHASH_EXACT_NEW_FILE
    assert new_source.matched_meme_file_id == existing_file.id
    assert outbox_count == 1


async def test_materializer_blocked_phash_creates_failed_audit_rows_and_no_transcode_outbox(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    perceptual_hash = "3" * 16
    blocked_hash = BlockedPerceptualHash(
        perceptual_hash=perceptual_hash,
        hash_algorithm="phash",
        hash_size=len(perceptual_hash) * 4,
        max_hamming_distance=0,
        reason=ModerationReason.SPAM,
        is_active=True,
    )
    migrated_db_session.add(blocked_hash)
    await migrated_db_session.commit()
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="blocked-source",
        post_id="3",
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH
    assert result.materialized_meme_file_id is not None
    assert ingest_request.temp_original_object_key not in storage_client.objects

    async with postgres_session_factory() as session:
        request = await session.get(PipelineIngestRequest, ingest_request.id)
        meme_file = await session.get(MemeFile, result.materialized_meme_file_id)
        outbox_count = await session.scalar(select(func.count()).select_from(PipelineOutboxEvent))

    assert request is not None
    assert request.status is PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH
    assert request.source_attach_reason is SourceAttachReason.BLOCKED_PERCEPTUAL_HASH_NEW_FILE
    assert meme_file is not None
    assert meme_file.status is ContentProcessingStatus.FAILED
    assert meme_file.blocked_perceptual_hash_id == blocked_hash.id
    assert meme_file.s3_original_key in storage_client.objects
    assert outbox_count == 0


async def test_materializer_invalid_media_marks_request_and_retains_temp_object(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="invalid-source",
        post_id="4",
    )
    media_processor = FakeMediaProcessor(inspect_error=MediaValidationError("not an image"))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.FAILED_INVALID_MEDIA
    assert ingest_request.temp_original_object_key in storage_client.objects
    assert storage_client.delete_calls == []

    async with postgres_session_factory() as session:
        request = await session.get(PipelineIngestRequest, ingest_request.id)
        meme_file_count = await session.scalar(select(func.count()).select_from(MemeFile))
        outbox_count = await session.scalar(select(func.count()).select_from(PipelineOutboxEvent))

    assert request is not None
    assert request.status is PipelineIngestRequestStatus.FAILED_INVALID_MEDIA
    assert request.failure_code == "invalid_media"
    assert request.failure_detail == "not an image"
    assert meme_file_count == 0
    assert outbox_count == 0


async def test_materializer_deletes_canonical_object_on_db_failure_after_promotion(
    migrated_db_session: AsyncSession,
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="db-failure-source",
        post_id="5",
    )
    temp_object_key = ingest_request.temp_original_object_key
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="5" * 16))

    async def fail_commit() -> None:
        raise SQLAlchemyError("forced commit failure")

    monkeypatch.setattr(migrated_db_session, "commit", fail_commit)

    with pytest.raises(PipelineIngestError):
        _ = await PipelineIngestMaterializer(
            migrated_db_session,
            settings=Settings(),
            storage_client=storage_client,
            media_processor=media_processor,
        ).materialize(ingest_request.id)

    deleted_keys = [str(call["Key"]) for call in storage_client.delete_calls]
    assert any(key.startswith("pipeline/originals/") for key in deleted_keys)
    assert temp_object_key in storage_client.objects


async def test_outbox_publisher_claims_publishes_and_records_failed_retry(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = utcnow()
    publishable = PipelineOutboxEvent(
        aggregate_type="test",
        aggregate_id=uuid.uuid7(),
        event_type="test_event",
        routing_key="pipeline.ok",
        payload={"ok": True},
        status=PipelineOutboxEventStatus.PENDING,
        next_retry_at=now,
    )
    failing = PipelineOutboxEvent(
        aggregate_type="test",
        aggregate_id=uuid.uuid7(),
        event_type="test_event",
        routing_key="pipeline.fail",
        payload={"ok": False},
        status=PipelineOutboxEventStatus.PENDING,
        next_retry_at=now,
    )
    stale = PipelineOutboxEvent(
        aggregate_type="test",
        aggregate_id=uuid.uuid7(),
        event_type="test_event",
        routing_key="pipeline.stale",
        payload={},
        status=PipelineOutboxEventStatus.PUBLISHING,
        updated_at=now - timedelta(hours=1),
    )
    migrated_db_session.add_all([publishable, failing, stale])
    await migrated_db_session.commit()

    broker = FakeBroker(fail_routing_keys={"pipeline.fail"})
    publisher = PipelineOutboxPublisher(migrated_db_session, broker=broker, settings=Settings())

    result = await publisher.publish_batch(limit=10)
    recovered = await publisher.recover_stale_publishing(stale_before=now - timedelta(minutes=10))

    assert result.claimed == 2
    assert result.published == 1
    assert result.failed == 1
    assert recovered == 1
    assert [call["routing_key"] for call in broker.publish_calls] == ["pipeline.ok", "pipeline.fail"]

    async with postgres_session_factory() as session:
        published_row = await session.get(PipelineOutboxEvent, publishable.id)
        failed_row = await session.get(PipelineOutboxEvent, failing.id)
        recovered_row = await session.get(PipelineOutboxEvent, stale.id)

    assert published_row is not None
    assert published_row.status is PipelineOutboxEventStatus.PUBLISHED
    assert published_row.attempt_count == 1
    assert published_row.published_at is not None
    assert failed_row is not None
    assert failed_row.status is PipelineOutboxEventStatus.FAILED
    assert failed_row.attempt_count == 1
    assert failed_row.next_retry_at is not None
    assert recovered_row is not None
    assert recovered_row.status is PipelineOutboxEventStatus.FAILED
