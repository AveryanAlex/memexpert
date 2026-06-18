"""Integration tests for the API-safe raw ingest accept service."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.ingest.accept_service import PipelineIngestAcceptService
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptSource
from memexpert.models.content import Meme, MemeFile, MemeSource, PipelineIngestRequest, PipelineOutboxEvent
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    PipelineIngestRequestStatus,
    PipelineOutboxEventStatus,
    SourceAttachReason,
    SourcePlatform,
)
from memexpert.services import PipelineIngestError

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class FakeStorageClient:
    """Small sync S3-compatible client used to observe raw ingest side effects."""

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
        return {"ETag": "fake"}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        return {"DeleteMarker": True}


def _source(*, source_id: str = "raw-source", post_id: str = "1") -> IngestAcceptSource:
    return IngestAcceptSource(
        source_platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        views=7,
        source_metadata={"channel_title": "Raw Source"},
    )


async def _seed_meme_file(
    session: AsyncSession,
    *,
    sha256_hex: str,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.NONE,
        is_public=False,
    )
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.PENDING,
        file_size_bytes=128,
        mime_type="image/png",
        s3_original_key=f"pipeline/originals/{meme_file_id}/original.png",
        sha256_hex=sha256_hex,
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    return meme, meme_file


async def test_accept_new_upload_creates_raw_ingest_request_and_pending_outbox_only(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    media_bytes = b"raw-upload-bytes"
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=_source(),
        filename="raw.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.ACCEPTED_ASYNC
    ingest_request = result.ingest_request
    assert ingest_request.status is PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
    assert ingest_request.sha256_hex == hashlib.sha256(media_bytes).hexdigest()
    assert ingest_request.temp_original_object_key is not None
    assert ingest_request.temp_original_object_key.startswith("pipeline/temp-originals/")
    assert ingest_request.materialized_meme_file_id is None
    assert ingest_request.matched_meme_file_id is None
    assert len(storage_client.put_calls) == 1
    assert storage_client.put_calls[0]["Key"] == ingest_request.temp_original_object_key
    assert storage_client.put_calls[0]["Body"] == media_bytes
    assert storage_client.delete_calls == []

    async with postgres_session_factory() as session:
        meme_count = await session.scalar(select(func.count()).select_from(Meme))
        meme_file_count = await session.scalar(select(func.count()).select_from(MemeFile))
        source_count = await session.scalar(select(func.count()).select_from(MemeSource))
        persisted_request = await session.get(PipelineIngestRequest, ingest_request.id)
        outbox_rows = (await session.execute(select(PipelineOutboxEvent))).scalars().all()

    assert meme_count == 0
    assert meme_file_count == 0
    assert source_count == 0
    assert persisted_request is not None
    assert persisted_request.status is PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
    assert len(outbox_rows) == 1
    assert outbox_rows[0].status is PipelineOutboxEventStatus.PENDING
    assert outbox_rows[0].aggregate_id == ingest_request.id
    assert outbox_rows[0].event_type == "media_inspect_requested"
    assert outbox_rows[0].payload["ingest_request_id"] == str(ingest_request.id)


async def test_accept_sha_duplicate_resolves_synchronously_and_does_not_enqueue_inspect(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    media_bytes = b"same-sha-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    _meme, meme_file = await _seed_meme_file(migrated_db_session, sha256_hex=sha256_hex)
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=_source(source_id="sha-source", post_id="2"),
        filename="duplicate.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE
    assert result.ingest_request.status is PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE
    assert result.ingest_request.materialized_meme_id == meme_file.meme_id
    assert result.ingest_request.materialized_meme_file_id == meme_file.id
    assert result.ingest_request.matched_meme_file_id == meme_file.id
    assert result.ingest_request.source_attach_reason is SourceAttachReason.SHA256_EXACT_EXISTING_FILE
    assert result.ingest_request.temp_original_object_key is None
    assert storage_client.put_calls == []
    assert storage_client.delete_calls == []

    async with postgres_session_factory() as session:
        outbox_count = await session.scalar(select(func.count()).select_from(PipelineOutboxEvent))
        sources = (await session.execute(select(MemeSource))).scalars().all()
        request_count = await session.scalar(select(func.count()).select_from(PipelineIngestRequest))
        file_count = await session.scalar(select(func.count()).select_from(MemeFile))

    assert outbox_count == 0
    assert request_count == 1
    assert file_count == 1
    assert len(sources) == 1
    assert sources[0].file_id == meme_file.id
    assert sources[0].attach_reason is SourceAttachReason.SHA256_EXACT_EXISTING_FILE
    assert sources[0].matched_meme_file_id == meme_file.id


async def test_accept_source_replay_returns_existing_request_without_duplicate_work(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)
    source = _source(source_id="replay-source", post_id="3")

    first = await service.accept_bytes(
        source=source,
        filename="first.png",
        content_type="image/png",
        media_bytes=b"first-raw-bytes",
    )
    replay = await service.accept_bytes(
        source=source,
        filename=None,
        content_type=None,
        media_bytes=b"different-bytes-do-not-matter",
    )

    assert first.outcome is IngestAcceptOutcome.ACCEPTED_ASYNC
    assert replay.outcome is IngestAcceptOutcome.SOURCE_REPLAY
    assert replay.ingest_request.id == first.ingest_request.id
    assert len(storage_client.put_calls) == 1
    assert storage_client.delete_calls == []

    async with postgres_session_factory() as session:
        request_count = await session.scalar(select(func.count()).select_from(PipelineIngestRequest))
        outbox_count = await session.scalar(select(func.count()).select_from(PipelineOutboxEvent))

    assert request_count == 1
    assert outbox_count == 1


async def test_accept_db_failure_after_temp_upload_deletes_temp_object(
    migrated_db_session: AsyncSession,
    monkeypatch: MonkeyPatch,
) -> None:
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    async def fail_commit() -> None:
        raise SQLAlchemyError("forced commit failure")

    monkeypatch.setattr(migrated_db_session, "commit", fail_commit)

    with pytest.raises(PipelineIngestError):
        _ = await service.accept_bytes(
            source=_source(source_id="failure-source", post_id="4"),
            filename="failure.png",
            content_type="image/png",
            media_bytes=b"failure-bytes",
        )

    assert len(storage_client.put_calls) == 1
    assert len(storage_client.delete_calls) == 1
    assert storage_client.delete_calls[0]["Bucket"] == storage_client.put_calls[0]["Bucket"]
    assert storage_client.delete_calls[0]["Key"] == storage_client.put_calls[0]["Key"]
