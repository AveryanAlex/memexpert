"""Integration tests for the API-safe raw ingest accept service."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.ingest.accept_service import PipelineIngestAcceptService
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptSource
from memexpert.ingest.target_collection_metadata import user_metadata_with_target_collection
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import (
    BlockedPerceptualHash,
    Meme,
    MemeFile,
    MemeSource,
    MemeSourceEngagementSnapshot,
    PipelineIngestRequest,
    RabbitMQOutboxMessage,
)
from memexpert.models.enums import (
    CollectionMembershipRole,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    IngestSourceKind,
    MemeVisibilityMode,
    ModerationReason,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    SourceAttachReason,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
)
from memexpert.models.user import User
from memexpert.services import PipelineIngestError
from memexpert.services.source_engagement import next_source_engagement_schedule_slot

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


def _source(
    *,
    source_id: str = "raw-source",
    post_id: str = "1",
    source_kind: IngestSourceKind = IngestSourceKind.OPERATOR_UPLOAD,
    uploader_user_id: uuid.UUID | None = None,
) -> IngestAcceptSource:
    return IngestAcceptSource(
        source_platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        source_kind=source_kind,
        uploader_user_id=uploader_user_id,
        view_count=7,
        source_metadata={"channel_title": "Raw Source"},
    )


async def _seed_meme_file(
    session: AsyncSession,
    *,
    sha256_hex: str,
    blocked_perceptual_hash_id: uuid.UUID | None = None,
    is_public: bool = True,
    visibility_mode: MemeVisibilityMode | None = None,
    uploader_user_id: uuid.UUID | None = None,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.NONE,
        is_public=is_public,
        visibility_mode=visibility_mode
        or (MemeVisibilityMode.FORCE_PUBLIC if is_public else MemeVisibilityMode.AUTO),
    )
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=(
            ContentProcessingStatus.FAILED
            if blocked_perceptual_hash_id is not None
            else ContentProcessingStatus.PENDING
        ),
        file_size_bytes=128,
        mime_type="image/png",
        s3_original_key=f"pipeline/originals/{meme_file_id}/original.png",
        sha256_hex=sha256_hex,
        blocked_perceptual_hash_id=blocked_perceptual_hash_id,
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    if uploader_user_id is not None:
        session.add(
            MemeSource(
                file_id=meme_file.id,
                platform=SourcePlatform.TELEGRAM,
                source_id=f"seed-user:{meme_file.id}",
                post_id="seed",
                source_kind=IngestSourceKind.USER_UPLOAD,
                uploader_user_id=uploader_user_id,
                source_alive=True,
            )
        )
        await session.flush()
    return meme, meme_file


async def _seed_writable_collection(session: AsyncSession, *, owner: User) -> Collection:
    collection = Collection(owner_id=owner.id, title="Uploads")
    session.add(collection)
    await session.flush()
    session.add(
        CollectionMember(
            collection_id=collection.id,
            user_id=owner.id,
            role=CollectionMembershipRole.OWNER,
        )
    )
    await session.flush()
    return collection


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
        outbox_rows = (await session.execute(select(RabbitMQOutboxMessage))).scalars().all()

    assert meme_count == 0
    assert meme_file_count == 0
    assert source_count == 0
    assert persisted_request is not None
    assert persisted_request.status is PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
    assert len(outbox_rows) == 1
    assert outbox_rows[0].status is RabbitMQOutboxMessageStatus.PENDING
    assert outbox_rows[0].aggregate_id == str(ingest_request.id)
    assert outbox_rows[0].event_type == "media_inspect_requested"
    assert outbox_rows[0].payload["ingest_request_id"] == str(ingest_request.id)


async def test_accept_sha_duplicate_resolves_synchronously_and_does_not_enqueue_inspect(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    media_bytes = b"same-sha-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    _meme, meme_file = await _seed_meme_file(migrated_db_session, sha256_hex=sha256_hex, is_public=True)
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
        outbox_count = await session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage))
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


async def test_accept_sha_duplicate_creates_initial_engagement_snapshot(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    media_bytes = b"same-sha-engagement-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    _meme, meme_file = await _seed_meme_file(migrated_db_session, sha256_hex=sha256_hex, is_public=True)
    published_at = datetime(2020, 1, 15, 10, 30, tzinfo=UTC)
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=IngestAcceptSource(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="sha-engagement-source",
            post_id="engagement-post",
            source_metadata={
                "published_at": published_at.isoformat(),
                "forward_count": 0,
                "comment_count": 3,
                "comments_state": SourceEngagementCommentsState.ENABLED.value,
            },
        ),
        filename="duplicate.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE

    async with postgres_session_factory() as session:
        source = await session.scalar(select(MemeSource).where(MemeSource.source_id == "sha-engagement-source"))
        snapshots = (await session.execute(select(MemeSourceEngagementSnapshot))).scalars().all()

    assert source is not None
    assert source.file_id == meme_file.id
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.meme_source_id == source.id
    assert snapshot.capture_reason is SourceEngagementCaptureReason.INGEST_INITIAL
    assert snapshot.schedule_label is SourceEngagementScheduleLabel.INGEST_INITIAL
    assert snapshot.fetch_status is SourceEngagementFetchStatus.SUCCESS
    assert snapshot.view_count is None
    assert snapshot.reactions is None
    assert snapshot.reaction_count is None
    assert snapshot.forward_count == 0
    assert snapshot.comment_count == 3
    assert snapshot.comments_state is SourceEngagementCommentsState.ENABLED
    assert source.last_engagement_check_at == snapshot.captured_at
    expected_slot = next_source_engagement_schedule_slot(published_at, now=snapshot.captured_at)
    assert expected_slot is not None
    assert expected_slot.label is SourceEngagementScheduleLabel.MONTHLY
    assert source.next_engagement_check_at == expected_slot.scheduled_for


async def test_accept_sha_duplicate_public_match_saves_target_collection(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = User(email="target-sha-owner@example.com")
    migrated_db_session.add(owner)
    await migrated_db_session.flush()
    target_collection = await _seed_writable_collection(migrated_db_session, owner=owner)
    media_bytes = b"same-public-target-sha-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    public_meme, meme_file = await _seed_meme_file(
        migrated_db_session,
        sha256_hex=sha256_hex,
        is_public=True,
    )
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=_source(source_id="sha-target-source", post_id="target-post").model_copy(
            update={
                "source_kind": IngestSourceKind.USER_UPLOAD,
                "uploader_user_id": owner.id,
                "user_metadata": user_metadata_with_target_collection(
                    target_collection_id=target_collection.id,
                ),
            }
        ),
        filename="duplicate.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE
    assert result.ingest_request.materialized_meme_id == public_meme.id
    assert result.ingest_request.user_metadata["target_collection_id"] == str(target_collection.id)
    assert storage_client.put_calls == []

    async with postgres_session_factory() as session:
        saved = await session.get(CollectionMeme, (target_collection.id, public_meme.id))
        request = await session.get(PipelineIngestRequest, result.ingest_request.id)
        source = await session.scalar(select(MemeSource).where(MemeSource.source_id == "sha-target-source"))

    assert saved is not None
    assert saved.added_by_user_id == owner.id
    assert request is not None
    assert request.uploader_user_id == owner.id
    assert request.user_metadata["target_collection_id"] == str(target_collection.id)
    assert source is not None
    assert source.file_id == meme_file.id


async def test_accept_sha_duplicate_reuses_private_file_across_users_without_making_it_public(
    migrated_db_session: AsyncSession,
) -> None:
    owner = User(email="sha-owner@example.com")
    other_owner = User(email="sha-other@example.com")
    migrated_db_session.add_all([owner, other_owner])
    await migrated_db_session.flush()
    target_collection = await _seed_writable_collection(migrated_db_session, owner=owner)
    media_bytes = b"same-other-private-sha-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    other_meme, _other_file = await _seed_meme_file(
        migrated_db_session,
        sha256_hex=sha256_hex,
        is_public=False,
        uploader_user_id=other_owner.id,
    )
    shared_private_collection = await _seed_writable_collection(migrated_db_session, owner=other_owner)
    migrated_db_session.add(
        CollectionMember(
            collection_id=shared_private_collection.id,
            user_id=owner.id,
            role=CollectionMembershipRole.VIEWER,
        )
    )
    migrated_db_session.add(
        CollectionMeme(
            collection_id=shared_private_collection.id,
            meme_id=other_meme.id,
            added_by_user_id=other_owner.id,
        )
    )
    await migrated_db_session.flush()
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=_source(source_id="private-sha-owner", post_id="private-sha-post").model_copy(
            update={
                "source_kind": IngestSourceKind.USER_UPLOAD,
                "uploader_user_id": owner.id,
                "user_metadata": user_metadata_with_target_collection(
                    target_collection_id=target_collection.id,
                ),
            }
        ),
        filename="private-duplicate.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE
    assert result.ingest_request.materialized_meme_id == other_meme.id
    assert len(storage_client.put_calls) == 0
    assert await migrated_db_session.get(CollectionMeme, (target_collection.id, other_meme.id)) is not None
    await migrated_db_session.refresh(other_meme)
    assert other_meme.is_public is False
    uploader_ids = set(
        (
            await migrated_db_session.execute(
                select(MemeSource.uploader_user_id)
                .join(MemeFile, MemeFile.id == MemeSource.file_id)
                .where(MemeFile.meme_id == other_meme.id, MemeSource.uploader_user_id.is_not(None))
            )
        ).scalars()
    )
    assert uploader_ids == {owner.id, other_owner.id}


async def test_crawler_exact_sha_promotes_automatic_private_meme_to_public(
    migrated_db_session: AsyncSession,
) -> None:
    uploader = User(email="sha-promotion-uploader@example.com")
    migrated_db_session.add(uploader)
    await migrated_db_session.flush()
    media_bytes = b"automatic-private-crawler-promotion"
    meme, meme_file = await _seed_meme_file(
        migrated_db_session,
        sha256_hex=hashlib.sha256(media_bytes).hexdigest(),
        is_public=False,
        visibility_mode=MemeVisibilityMode.AUTO,
        uploader_user_id=uploader.id,
    )
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=FakeStorageClient())

    result = await service.accept_bytes(
        source=_source(
            source_id="promotion-crawler",
            post_id="promotion-post",
            source_kind=IngestSourceKind.PUBLIC_CRAWLER,
        ),
        filename="promotion.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE
    assert result.ingest_request.materialized_meme_file_id == meme_file.id
    await migrated_db_session.refresh(meme)
    assert meme.visibility_mode is MemeVisibilityMode.AUTO
    assert meme.is_public is True


async def test_crawler_exact_sha_keeps_forced_private_meme_private(
    migrated_db_session: AsyncSession,
) -> None:
    owner = User(email="sha-editor@example.com")
    other_owner = User(email="sha-editor-source-owner@example.com")
    migrated_db_session.add_all([owner, other_owner])
    await migrated_db_session.flush()
    target_collection = await _seed_writable_collection(migrated_db_session, owner=owner)
    media_bytes = b"same-editor-private-sha-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    other_meme, _other_file = await _seed_meme_file(
        migrated_db_session,
        sha256_hex=sha256_hex,
        is_public=False,
        visibility_mode=MemeVisibilityMode.FORCE_PRIVATE,
        uploader_user_id=other_owner.id,
    )
    shared_private_collection = await _seed_writable_collection(migrated_db_session, owner=other_owner)
    migrated_db_session.add_all(
        [
            CollectionMember(
                collection_id=shared_private_collection.id,
                user_id=owner.id,
                role=CollectionMembershipRole.EDITOR,
            ),
            CollectionMeme(
                collection_id=shared_private_collection.id,
                meme_id=other_meme.id,
                added_by_user_id=other_owner.id,
            ),
        ]
    )
    await migrated_db_session.flush()
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=_source(
            source_id="private-sha-editor",
            post_id="private-sha-editor-post",
            source_kind=IngestSourceKind.PUBLIC_CRAWLER,
        ),
        filename="private-editor-duplicate.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE
    await migrated_db_session.refresh(other_meme)
    assert other_meme.visibility_mode is MemeVisibilityMode.FORCE_PRIVATE
    assert other_meme.is_public is False
    assert await migrated_db_session.get(CollectionMeme, (target_collection.id, other_meme.id)) is None
    assert len(storage_client.put_calls) == 0


async def test_accept_sha_duplicate_of_blocked_file_preserves_blocked_source_reason(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    media_bytes = b"same-blocked-sha-bytes"
    sha256_hex = hashlib.sha256(media_bytes).hexdigest()
    blocked_hash = BlockedPerceptualHash(
        perceptual_hash="b" * 16,
        hash_algorithm="phash",
        hash_size=64,
        max_hamming_distance=0,
        reason=ModerationReason.SPAM,
        is_active=True,
    )
    migrated_db_session.add(blocked_hash)
    blocked_owner = User(email="blocked-sha-owner@example.com")
    migrated_db_session.add(blocked_owner)
    await migrated_db_session.flush()
    _meme, meme_file = await _seed_meme_file(
        migrated_db_session,
        sha256_hex=sha256_hex,
        blocked_perceptual_hash_id=blocked_hash.id,
        is_public=True,
        uploader_user_id=blocked_owner.id,
    )
    storage_client = FakeStorageClient()
    service = PipelineIngestAcceptService(migrated_db_session, storage_client=storage_client)

    result = await service.accept_bytes(
        source=_source(source_id="blocked-sha-source", post_id="blocked-sha-post"),
        filename="blocked-duplicate.png",
        content_type="image/png",
        media_bytes=media_bytes,
    )

    assert result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE
    assert result.ingest_request.materialized_meme_file_id == meme_file.id
    assert result.ingest_request.source_attach_reason is SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE
    assert storage_client.put_calls == []

    async with postgres_session_factory() as session:
        source = await session.scalar(select(MemeSource).where(MemeSource.source_id == "blocked-sha-source"))
        file_count = await session.scalar(select(func.count()).select_from(MemeFile))

    assert file_count == 1
    assert source is not None
    assert source.file_id == meme_file.id
    assert source.attach_reason is SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE
    assert source.matched_meme_file_id == meme_file.id


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
        outbox_count = await session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage))

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
