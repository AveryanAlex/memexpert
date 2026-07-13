"""Integration tests for worker-side ingest-request materialization and outbox publishing."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.config import Settings
from memexpert.core.storage import build_temp_original_object_key
from memexpert.ingest.materializer import PipelineIngestMaterializer
from memexpert.ingest.target_collection_metadata import user_metadata_with_target_collection
from memexpert.media.contracts import MediaValidationError, NormalizedMediaResult, UploadMediaDetails
from memexpert.messaging.rabbitmq_outbox_runtime import run_rabbitmq_outbox_publisher_batch
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import (
    BlockedPerceptualHash,
    Meme,
    MemeFile,
    MemeSource,
    MemeSourceEngagementSnapshot,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
)
from memexpert.models.enums import (
    CollectionMembershipRole,
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
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
from memexpert.schemas.content_pipeline import ContentPipelineEventType
from memexpert.services import PipelineIngestError
from memexpert.services.search_index_sync import build_qdrant_sync_payload, load_search_index_state
from memexpert.services.source_engagement import next_source_engagement_schedule_slot

if TYPE_CHECKING:
    from aio_pika.abc import HeadersType
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

    async def publish(
        self,
        message: object,
        /,
        queue: object = "",
        exchange: object | None = None,
        *,
        routing_key: str = "",
        mandatory: bool = True,
        persist: bool = False,
        content_type: str | None = None,
        headers: HeadersType | None = None,
        message_id: str | None = None,
        timestamp: object | None = None,
    ) -> object:
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
        if routing_key in self.fail_routing_keys:
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
    view_count: int = 12,
    source_kind: IngestSourceKind | None = None,
    uploader_user_id: uuid.UUID | None = None,
    user_metadata: dict[str, object] | None = None,
    source_metadata: dict[str, object] | None = None,
) -> PipelineIngestRequest:
    ingest_request_id = uuid.uuid7()
    temp_key = build_temp_original_object_key(ingest_request_id, "raw.png", settings=Settings())
    storage_client.objects[temp_key] = StoredObject(body=media_bytes, content_type="image/png")
    ingest_request = PipelineIngestRequest(
        id=ingest_request_id,
        source_platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        source_kind=source_kind
        or (IngestSourceKind.USER_UPLOAD if uploader_user_id is not None else IngestSourceKind.OPERATOR_UPLOAD),
        uploader_user_id=uploader_user_id,
        user_metadata=user_metadata or {},
        source_metadata=source_metadata or {"view_count": view_count},
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
    is_public: bool = True,
    visibility_mode: MemeVisibilityMode | None = None,
    uploader_user_ids: tuple[uuid.UUID, ...] = (),
) -> MemeFile:
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
    for uploader_user_id in uploader_user_ids:
        session.add(
            MemeSource(
                file_id=meme_file.id,
                platform=SourcePlatform.TELEGRAM,
                source_id=f"seed-uploader:{uploader_user_id}:{meme_file.id}",
                post_id="seed",
                source_kind=IngestSourceKind.USER_UPLOAD,
                uploader_user_id=uploader_user_id,
                source_alive=True,
            )
        )
    await session.commit()
    return meme_file


async def _seed_collection(
    session: AsyncSession,
    *,
    owner: User,
    member: User | None = None,
    member_role: CollectionMembershipRole = CollectionMembershipRole.VIEWER,
) -> Collection:
    collection = Collection(owner_id=owner.id, title="Upload target")
    session.add(collection)
    await session.flush()
    session.add(
        CollectionMember(
            collection_id=collection.id,
            user_id=owner.id,
            role=CollectionMembershipRole.OWNER,
        )
    )
    if member is not None:
        session.add(
            CollectionMember(
                collection_id=collection.id,
                user_id=member.id,
                role=member_role,
            )
        )
    await session.flush()
    return collection


async def test_materializer_new_content_creates_content_rows_outbox_and_cleans_temp(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    published_at = datetime(2020, 1, 15, 10, 30, tzinfo=UTC)
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_metadata={
            "view_count": 15,
            "published_at": published_at.isoformat(),
            "reactions": {},
            "forward_count": 2,
            "comment_count": 0,
            "comments_state": SourceEngagementCommentsState.DISABLED.value,
        },
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="1" * 16))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert result.materialized_meme_file_id is not None
    assert result.outbox_message_id is not None
    assert media_processor.inspect_calls == 1
    assert ingest_request.temp_original_object_key not in storage_client.objects
    assert any(call["Key"] == ingest_request.temp_original_object_key for call in storage_client.delete_calls)

    async with postgres_session_factory() as session:
        request = await session.get(PipelineIngestRequest, ingest_request.id)
        meme_file = await session.get(MemeFile, result.materialized_meme_file_id)
        meme = await session.get(Meme, result.materialized_meme_id)
        sources = (await session.execute(select(MemeSource))).scalars().all()
        stage_rows = (await session.execute(select(PipelineStageJournal))).scalars().all()
        snapshots = (await session.execute(select(MemeSourceEngagementSnapshot))).scalars().all()
        outbox_rows = (await session.execute(select(RabbitMQOutboxMessage))).scalars().all()

    assert request is not None
    assert request.status is PipelineIngestRequestStatus.MATERIALIZED
    assert meme_file is not None
    assert meme is not None
    assert meme.visibility_mode is MemeVisibilityMode.AUTO
    assert meme.is_public is False
    assert meme_file.ingest_origin is IngestFileOrigin.NEW_MEME
    assert meme_file.s3_original_key in storage_client.objects
    assert sources[0].file_id == meme_file.id
    assert sources[0].source_kind is IngestSourceKind.OPERATOR_UPLOAD
    assert sources[0].attach_reason is SourceAttachReason.NEW_FILE
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.meme_source_id == sources[0].id
    assert snapshot.capture_reason is SourceEngagementCaptureReason.INGEST_INITIAL
    assert snapshot.schedule_label is SourceEngagementScheduleLabel.INGEST_INITIAL
    assert snapshot.fetch_status is SourceEngagementFetchStatus.SUCCESS
    ingest_started_at = next(row.started_at for row in stage_rows if row.stage is ContentPipelineStage.INGEST)
    assert snapshot.captured_at == ingest_started_at
    assert snapshot.view_count == 15
    assert snapshot.reactions == {}
    assert snapshot.reaction_count == 0
    assert snapshot.forward_count == 2
    assert snapshot.comment_count == 0
    assert snapshot.comments_state is SourceEngagementCommentsState.DISABLED
    assert sources[0].last_engagement_check_at == snapshot.captured_at
    expected_slot = next_source_engagement_schedule_slot(published_at, now=snapshot.captured_at)
    assert expected_slot is not None
    assert expected_slot.label is SourceEngagementScheduleLabel.MONTHLY
    assert sources[0].next_engagement_check_at == expected_slot.scheduled_for
    assert {(row.stage, row.status) for row in stage_rows} == {
        (ContentPipelineStage.INGEST, ContentPipelineStageStatus.SUCCEEDED),
        (ContentPipelineStage.TRANSCODE, ContentPipelineStageStatus.PENDING),
    }
    assert len(outbox_rows) == 1
    assert outbox_rows[0].event_type == ContentPipelineEventType.MEME_CREATED.value
    assert outbox_rows[0].routing_key == "pipeline.transcode"
    assert outbox_rows[0].payload["meme_file_id"] == str(meme_file.id)


async def test_materializer_new_crawler_discovery_is_public_in_auto_mode(
    migrated_db_session: AsyncSession,
) -> None:
    storage_client = FakeStorageClient()
    request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="new-public-crawler",
        post_id="new-public-crawler-post",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="f" * 16)),
    ).materialize(request.id)

    meme = await migrated_db_session.get(Meme, result.materialized_meme_id)
    assert meme is not None
    assert meme.visibility_mode is MemeVisibilityMode.AUTO
    assert meme.is_public is True


async def test_materializer_new_private_upload_attaches_target_before_index_payload(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = User(email="materialized-owner@example.com")
    member = User(email="materialized-member@example.com")
    migrated_db_session.add_all([owner, member])
    await migrated_db_session.flush()
    target_collection = await _seed_collection(migrated_db_session, owner=owner, member=member)
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="target-materializer-source",
        post_id="target-materializer-post",
        uploader_user_id=owner.id,
        user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection.id),
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="9" * 16))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert result.materialized_meme_id is not None
    assert result.materialized_meme_file_id is not None

    async with postgres_session_factory() as session:
        saved = await session.get(CollectionMeme, (target_collection.id, result.materialized_meme_id))
        meme = await session.get(Meme, result.materialized_meme_id)
        loaded_state = await load_search_index_state(session, result.materialized_meme_file_id)
        qdrant_payload = build_qdrant_sync_payload(loaded_state.canonical)

    assert saved is not None
    assert saved.added_by_user_id == owner.id
    assert meme is not None
    assert meme.is_public is False
    assert qdrant_payload.is_public is False
    assert qdrant_payload.uploader_user_ids == [str(owner.id)]
    assert qdrant_payload.collection_ids == [str(target_collection.id)]
    assert qdrant_payload.private_collection_ids == [str(target_collection.id)]
    assert qdrant_payload.collection_owner_user_ids == [str(owner.id)]
    assert set(qdrant_payload.collection_member_user_ids) == {str(owner.id), str(member.id)}


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
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
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
        snapshots = (await session.execute(select(MemeSourceEngagementSnapshot))).scalars().all()
        outbox_count = await session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage))

    assert new_file is not None
    assert new_file.meme_id == existing_file.meme_id
    assert new_file.ingest_origin is IngestFileOrigin.PHASH_EXACT_EXISTING_MEME
    assert new_file.matched_meme_file_id == existing_file.id
    assert new_source.attach_reason is SourceAttachReason.PHASH_EXACT_NEW_FILE
    assert new_source.matched_meme_file_id == existing_file.id
    assert len(snapshots) == 1
    assert snapshots[0].meme_source_id == new_source.id
    assert snapshots[0].capture_reason is SourceEngagementCaptureReason.INGEST_INITIAL
    assert snapshots[0].schedule_label is SourceEngagementScheduleLabel.INGEST_INITIAL
    assert snapshots[0].fetch_status is SourceEngagementFetchStatus.SUCCESS
    assert snapshots[0].view_count == 12
    assert snapshots[0].reactions is None
    assert snapshots[0].reaction_count is None
    assert new_source.last_engagement_check_at == snapshots[0].captured_at
    assert outbox_count == 1


async def test_materializer_phash_duplicate_merges_same_single_uploader_private_meme(
    migrated_db_session: AsyncSession,
) -> None:
    uploader = User(email="same-private-phash@example.com")
    migrated_db_session.add(uploader)
    await migrated_db_session.flush()
    target_collection = await _seed_collection(migrated_db_session, owner=uploader)
    perceptual_hash = "6" * 16
    existing_file = await _seed_existing_meme_file(
        migrated_db_session,
        perceptual_hash=perceptual_hash,
        is_public=False,
        uploader_user_ids=(uploader.id,),
    )
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="same-private-phash-source",
        post_id="same-private-phash-post",
        uploader_user_id=uploader.id,
        user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection.id),
    )

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash)),
    ).materialize(ingest_request.id)

    assert result.materialized_meme_id == existing_file.meme_id
    assert result.matched_meme_file_id == existing_file.id
    assert await migrated_db_session.get(CollectionMeme, (target_collection.id, existing_file.meme_id)) is not None


async def test_user_phash_match_to_public_meme_creates_separate_private_meme(
    migrated_db_session: AsyncSession,
) -> None:
    uploader = User(email="public-near-duplicate-user@example.com")
    migrated_db_session.add(uploader)
    await migrated_db_session.flush()
    target_collection = await _seed_collection(migrated_db_session, owner=uploader)
    perceptual_hash = "8" * 16
    public_file = await _seed_existing_meme_file(migrated_db_session, perceptual_hash=perceptual_hash)
    storage_client = FakeStorageClient()
    request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="public-near-user",
        post_id="public-near-user-post",
        uploader_user_id=uploader.id,
        user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection.id),
    )

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash)),
    ).materialize(request.id)

    assert result.matched_meme_file_id is None
    assert result.materialized_meme_id != public_file.meme_id
    private_meme = await migrated_db_session.get(Meme, result.materialized_meme_id)
    assert private_meme is not None
    assert private_meme.is_public is False


async def test_crawler_phash_match_to_private_meme_creates_separate_public_meme(
    migrated_db_session: AsyncSession,
) -> None:
    uploader = User(email="private-near-crawler-user@example.com")
    migrated_db_session.add(uploader)
    await migrated_db_session.flush()
    perceptual_hash = "c" * 16
    private_file = await _seed_existing_meme_file(
        migrated_db_session,
        perceptual_hash=perceptual_hash,
        is_public=False,
        uploader_user_ids=(uploader.id,),
    )
    storage_client = FakeStorageClient()
    request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="private-near-crawler",
        post_id="private-near-crawler-post",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
    )

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash)),
    ).materialize(request.id)

    assert result.matched_meme_file_id is None
    assert result.materialized_meme_id != private_file.meme_id
    public_meme = await migrated_db_session.get(Meme, result.materialized_meme_id)
    assert public_meme is not None
    assert public_meme.is_public is True


async def test_materializer_phash_duplicate_does_not_cross_user_dedupe_private_files(
    migrated_db_session: AsyncSession,
) -> None:
    owner = User(email="phash-owner@example.com")
    other_owner = User(email="phash-other@example.com")
    migrated_db_session.add_all([owner, other_owner])
    await migrated_db_session.flush()
    target_collection = await _seed_collection(migrated_db_session, owner=owner)
    perceptual_hash = "4" * 16
    existing_file = await _seed_existing_meme_file(
        migrated_db_session,
        perceptual_hash=perceptual_hash,
        is_public=False,
        uploader_user_ids=(other_owner.id,),
    )
    shared_private_collection = await _seed_collection(migrated_db_session, owner=other_owner, member=owner)
    migrated_db_session.add(
        CollectionMeme(
            collection_id=shared_private_collection.id,
            meme_id=existing_file.meme_id,
            added_by_user_id=other_owner.id,
        )
    )
    await migrated_db_session.flush()
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="private-phash-source",
        post_id="private-phash-post",
        uploader_user_id=owner.id,
        user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection.id),
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert result.matched_meme_file_id is None
    assert result.materialized_meme_id != existing_file.meme_id

    new_file = await migrated_db_session.get(MemeFile, result.materialized_meme_file_id)
    assert new_file is not None
    assert new_file.ingest_origin is IngestFileOrigin.NEW_MEME
    assert new_file.meme_id == result.materialized_meme_id
    assert (
        await migrated_db_session.get(CollectionMeme, (target_collection.id, result.materialized_meme_id))
        is not None
    )


async def test_materializer_rechecks_global_sha_and_collapses_pending_accept_race(
    migrated_db_session: AsyncSession,
) -> None:
    media_bytes = b"pending-accept-sha-race"
    existing_file = await _seed_existing_meme_file(
        migrated_db_session,
        perceptual_hash="d" * 16,
        sha256_hex=hashlib.sha256(media_bytes).hexdigest(),
    )
    storage_client = FakeStorageClient()
    request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        media_bytes=media_bytes,
        source_id="sha-race-source",
        post_id="sha-race-post",
    )

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="e" * 16)),
    ).materialize(request.id)

    assert result.status is PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE
    assert result.materialized_meme_file_id == existing_file.id
    assert await migrated_db_session.scalar(select(func.count()).select_from(MemeFile)) == 1
    assert await migrated_db_session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage)) == 0


async def test_concurrent_exact_materializations_produce_one_meme_and_one_file(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _ = migrated_db_session
    storage_client = FakeStorageClient()
    media_bytes = b"concurrent-global-sha"
    async with postgres_session_factory() as first_session:
        first_request = await _seed_raw_request(
            first_session,
            storage_client,
            media_bytes=media_bytes,
            source_id="concurrent-source-one",
            post_id="one",
        )
    async with postgres_session_factory() as second_session:
        second_request = await _seed_raw_request(
            second_session,
            storage_client,
            media_bytes=media_bytes,
            source_id="concurrent-source-two",
            post_id="two",
        )

    async def materialize(request_id: uuid.UUID):
        async with postgres_session_factory() as session:
            return await PipelineIngestMaterializer(
                session,
                settings=Settings(),
                storage_client=storage_client,
                media_processor=FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="1" * 16)),
            ).materialize(request_id)

    results = await asyncio.gather(materialize(first_request.id), materialize(second_request.id))

    async with postgres_session_factory() as session:
        meme_count = await session.scalar(select(func.count()).select_from(Meme))
        file_count = await session.scalar(select(func.count()).select_from(MemeFile))
        source_count = await session.scalar(select(func.count()).select_from(MemeSource))
    assert meme_count == 1
    assert file_count == 1
    assert source_count == 2
    assert {result.status for result in results} == {
        PipelineIngestRequestStatus.MATERIALIZED,
        PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE,
    }


async def test_materializer_phash_duplicate_does_not_cross_user_dedupe_private_files_for_editor(
    migrated_db_session: AsyncSession,
) -> None:
    owner = User(email="phash-editor@example.com")
    other_owner = User(email="phash-editor-source-owner@example.com")
    migrated_db_session.add_all([owner, other_owner])
    await migrated_db_session.flush()
    target_collection = await _seed_collection(migrated_db_session, owner=owner)
    perceptual_hash = "5" * 16
    existing_file = await _seed_existing_meme_file(
        migrated_db_session,
        perceptual_hash=perceptual_hash,
        is_public=False,
        uploader_user_ids=(other_owner.id, owner.id),
    )
    shared_private_collection = await _seed_collection(
        migrated_db_session,
        owner=other_owner,
        member=owner,
        member_role=CollectionMembershipRole.EDITOR,
    )
    migrated_db_session.add(
        CollectionMeme(
            collection_id=shared_private_collection.id,
            meme_id=existing_file.meme_id,
            added_by_user_id=other_owner.id,
        )
    )
    await migrated_db_session.flush()
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="private-phash-editor-source",
        post_id="private-phash-editor-post",
        uploader_user_id=owner.id,
        user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection.id),
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash=perceptual_hash))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert result.materialized_meme_id != existing_file.meme_id
    assert result.matched_meme_file_id is None

    new_file = await migrated_db_session.get(MemeFile, result.materialized_meme_file_id)
    assert new_file is not None
    assert new_file.ingest_origin is IngestFileOrigin.NEW_MEME
    assert new_file.meme_id == result.materialized_meme_id
    assert await migrated_db_session.get(CollectionMeme, (target_collection.id, existing_file.meme_id)) is None
    assert (
        await migrated_db_session.get(CollectionMeme, (target_collection.id, result.materialized_meme_id))
        is not None
    )


async def test_materializer_persists_source_attribution_from_raw_request_metadata(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage_client = FakeStorageClient()
    owner = User()
    migrated_db_session.add(owner)
    await migrated_db_session.flush()
    owner_user_id = owner.id
    published_at = utcnow()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="attribution-source",
        post_id="source-post",
        uploader_user_id=owner_user_id,
        source_metadata={
            "view_count": 41,
            "published_at": published_at.isoformat(),
            "reactions": {"like": 5, "fire": 2},
            "forward": {
                "source_id": "original-source",
                "post_id": "original-post",
                "channel_username": None,
                "channel_title": "Original",
            },
        },
    )
    media_processor = FakeMediaProcessor(inspect_result=_upload_details(perceptual_hash="7" * 16))

    result = await PipelineIngestMaterializer(
        migrated_db_session,
        settings=Settings(),
        storage_client=storage_client,
        media_processor=media_processor,
    ).materialize(ingest_request.id)

    async with postgres_session_factory() as session:
        meme = await session.get(Meme, result.materialized_meme_id)
        source = await session.scalar(select(MemeSource).where(MemeSource.source_id == "attribution-source"))
        snapshot = await session.scalar(
            select(MemeSourceEngagementSnapshot)
            .join(MemeSource)
            .where(MemeSource.source_id == "attribution-source")
        )

    assert result.status is PipelineIngestRequestStatus.MATERIALIZED
    assert meme is not None
    assert source is not None
    assert source.source_kind is IngestSourceKind.USER_UPLOAD
    assert source.uploader_user_id == owner_user_id
    assert source.file_id == result.materialized_meme_file_id
    assert snapshot is not None
    assert snapshot.view_count == 41
    assert snapshot.reactions == {"like": 5, "fire": 2}
    assert snapshot.reaction_count == 7
    assert source.is_first_source is False
    assert source.published_at == published_at
    assert source.forwarded_from_source_id == "original-source"
    assert source.forwarded_from_post_id == "original-post"
    assert source.attach_reason is SourceAttachReason.NEW_FILE


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
    owner = User(email="blocked-target-owner@example.com")
    migrated_db_session.add(owner)
    await migrated_db_session.flush()
    target_collection = await _seed_collection(migrated_db_session, owner=owner)
    await migrated_db_session.commit()
    storage_client = FakeStorageClient()
    ingest_request = await _seed_raw_request(
        migrated_db_session,
        storage_client,
        source_id="blocked-source",
        post_id="3",
        uploader_user_id=owner.id,
        user_metadata=user_metadata_with_target_collection(target_collection_id=target_collection.id),
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
        outbox_count = await session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage))
        collection_meme_count = await session.scalar(select(func.count()).select_from(CollectionMeme))

    assert request is not None
    assert request.status is PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH
    assert request.source_attach_reason is SourceAttachReason.BLOCKED_PERCEPTUAL_HASH_NEW_FILE
    assert meme_file is not None
    assert meme_file.status is ContentProcessingStatus.FAILED
    assert meme_file.blocked_perceptual_hash_id == blocked_hash.id
    assert meme_file.s3_original_key in storage_client.objects
    assert outbox_count == 0
    assert collection_meme_count == 0


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
        outbox_count = await session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage))

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


async def test_outbox_batch_runner_publishes_generically_and_recovers_stale_claims(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = utcnow()
    media_inspect_id = uuid.uuid7()
    transcode_id = uuid.uuid7()
    stale_id = uuid.uuid7()
    media_inspect = RabbitMQOutboxMessage(
        id=media_inspect_id,
        exchange="memexpert.pipeline",
        aggregate_type="test",
        aggregate_id=str(uuid.uuid7()),
        event_type="media_inspect_requested",
        routing_key="pipeline.media_inspect",
        payload={"event_type": "media_inspect_requested", "ok": True},
        headers={},
        content_type="application/json",
        message_id=str(media_inspect_id),
        status=RabbitMQOutboxMessageStatus.PENDING,
        next_retry_at=now,
        created_at=now - timedelta(minutes=3),
    )
    transcode = RabbitMQOutboxMessage(
        id=transcode_id,
        exchange="memexpert.pipeline",
        aggregate_type="test",
        aggregate_id=str(uuid.uuid7()),
        event_type=ContentPipelineEventType.MEME_CREATED.value,
        routing_key="pipeline.transcode",
        payload={"event_type": ContentPipelineEventType.MEME_CREATED.value, "ok": False},
        headers={},
        content_type="application/json",
        message_id=str(transcode_id),
        status=RabbitMQOutboxMessageStatus.PENDING,
        next_retry_at=now,
        created_at=now - timedelta(minutes=2),
    )
    stale = RabbitMQOutboxMessage(
        id=stale_id,
        exchange="memexpert.pipeline",
        aggregate_type="test",
        aggregate_id=str(uuid.uuid7()),
        event_type="test_event",
        routing_key="pipeline.stale",
        payload={},
        headers={},
        content_type="application/json",
        message_id=str(stale_id),
        status=RabbitMQOutboxMessageStatus.PUBLISHING,
        locked_at=now - timedelta(hours=1),
        lock_owner="stale-test",
        created_at=now - timedelta(minutes=1),
    )
    migrated_db_session.add_all([media_inspect, transcode, stale])
    await migrated_db_session.commit()

    broker = FakeBroker(fail_routing_keys={"pipeline.transcode"})
    result = await run_rabbitmq_outbox_publisher_batch(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "scheduler_rabbitmq_outbox_publisher_batch_size": 2,
                "scheduler_rabbitmq_outbox_publisher_stale_timeout_seconds": 600.0,
            }
        ),
        broker=broker,
    )

    assert result.claimed == 2
    assert result.published == 1
    assert result.failed == 1
    assert result.recovered == 1
    assert [call["routing_key"] for call in broker.publish_calls] == ["pipeline.media_inspect", "pipeline.transcode"]
    assert broker.publish_calls[0]["payload"] == media_inspect.payload
    assert broker.publish_calls[1]["payload"] == transcode.payload

    async with postgres_session_factory() as session:
        published_row = await session.get(RabbitMQOutboxMessage, media_inspect.id)
        failed_row = await session.get(RabbitMQOutboxMessage, transcode.id)
        recovered_row = await session.get(RabbitMQOutboxMessage, stale.id)

    assert published_row is not None
    assert published_row.status is RabbitMQOutboxMessageStatus.PUBLISHED
    assert published_row.attempt_count == 1
    assert published_row.published_at is not None
    assert failed_row is not None
    assert failed_row.status is RabbitMQOutboxMessageStatus.FAILED
    assert failed_row.attempt_count == 1
    assert failed_row.next_retry_at is not None
    assert recovered_row is not None
    assert recovered_row.status is RabbitMQOutboxMessageStatus.FAILED
    assert recovered_row.next_retry_at is not None
    assert recovered_row.last_error_text == "RabbitMQ outbox message was recovered from a stale publishing lease."
