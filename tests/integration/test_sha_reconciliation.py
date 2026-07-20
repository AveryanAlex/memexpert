"""Integration coverage for resumable pre-0032 SHA duplicate reconciliation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest_asyncio
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from memexpert.ingest.sha_dedupe import sha_match_attach_reason
from memexpert.models.collection import Collection, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    BlockedPerceptualHash,
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemeOfTheDaySelection,
    MemeSeoPage,
    MemeSource,
    MemeSourceEngagementSnapshot,
    ModerationDecision,
    ModerationReport,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    TelegramFileIdCache,
)
from memexpert.models.enums import (
    AnalyticsEventType,
    CollectionKind,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    EmbeddingInputType,
    IngestSourceKind,
    MemeVisibilityMode,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    SourceAttachReason,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramMediaFormat,
)
from memexpert.models.user import AnalyticsEvent, User
from memexpert.services.sha_reconciliation import (
    MERGE_REASON_SHA256_RECONCILIATION,
    ShaDuplicateReconciliationService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"

type ReconciliationDatabase = tuple[async_sessionmaker[AsyncSession], Config, AsyncEngine]


def _build_alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


async def _run_upgrade(config: Config, revision: str) -> None:
    async with asyncio.timeout(30):
        await asyncio.to_thread(command.upgrade, config, revision)


async def _reset_public_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))


async def _add_current_meme_file_orm_compatibility_columns(engine: AsyncEngine) -> None:
    """Keep the current ORM usable while exercising the physical 0031 schema."""

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                ALTER TABLE meme_files
                    ADD COLUMN active_media_generation_id uuid,
                    ADD COLUMN source_has_audio boolean,
                    ADD COLUMN web_video_has_audio boolean,
                    ADD COLUMN web_video_profile varchar(128),
                    ADD COLUMN web_video_verified_at timestamptz
                """
            )
        )


@pytest_asyncio.fixture(loop_scope="session")
async def reconciliation_database(
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
) -> AsyncIterator[ReconciliationDatabase]:
    await _reset_public_schema(postgres_async_engine)
    config = _build_alembic_config(postgres_async_url)
    await _run_upgrade(config, "0031")
    await _add_current_meme_file_orm_compatibility_columns(postgres_async_engine)
    session_factory = async_sessionmaker(postgres_async_engine, expire_on_commit=False)
    try:
        yield session_factory, config, postgres_async_engine
    finally:
        await _reset_public_schema(postgres_async_engine)


async def _add_meme_file(
    session: AsyncSession,
    *,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID,
    sha256_hex: str,
    created_at: datetime,
    visibility_mode: MemeVisibilityMode = MemeVisibilityMode.AUTO,
    is_public: bool = False,
    is_nsfw: bool = False,
    like_count: int = 0,
    tags: list[str] | None = None,
    ocr_text: str | None = None,
    quality_score: float = 1.0,
    s3_original_key: str | None = None,
    s3_web_video_key: str | None = None,
    status: ContentProcessingStatus = ContentProcessingStatus.READY,
    blocked_perceptual_hash_id: uuid.UUID | None = None,
) -> tuple[Meme, MemeFile]:
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.EN,
        visibility_mode=visibility_mode,
        is_public=is_public,
        is_nsfw=is_nsfw,
        like_count=like_count,
        tags=tags or [],
        ocr_text=ocr_text,
        created_at=created_at,
        updated_at=created_at,
    )
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=status,
        width=640,
        height=480,
        file_size_bytes=1024,
        mime_type="image/jpeg",
        s3_original_key=s3_original_key or f"pipeline/originals/{meme_file_id}.jpg",
        s3_web_video_key=s3_web_video_key,
        sha256_hex=sha256_hex,
        blocked_perceptual_hash_id=blocked_perceptual_hash_id,
        quality_score=quality_score,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    return meme, meme_file


async def test_sha_reconciliation_preserves_dependencies_and_resumes_before_0032(
    reconciliation_database: ReconciliationDatabase,
) -> None:
    session_factory, config, engine = reconciliation_database
    now = datetime.now(UTC)
    first_sha = "a" * 64
    second_sha = "b" * 64
    blocked_sha = "e" * 64
    ready_sha = "f" * 64

    canonical_meme_id, canonical_file_id = uuid.uuid7(), uuid.uuid7()
    obsolete_meme_id, obsolete_file_id = uuid.uuid7(), uuid.uuid7()
    moved_file_id = uuid.uuid7()
    second_canonical_meme_id, second_canonical_file_id = uuid.uuid7(), uuid.uuid7()
    second_obsolete_meme_id, second_obsolete_file_id = uuid.uuid7(), uuid.uuid7()
    blocked_oldest_meme_id, blocked_oldest_file_id = uuid.uuid7(), uuid.uuid7()
    blocked_newer_meme_id, blocked_newer_file_id = uuid.uuid7(), uuid.uuid7()
    failed_oldest_meme_id, failed_oldest_file_id = uuid.uuid7(), uuid.uuid7()
    ready_newer_meme_id, ready_newer_file_id = uuid.uuid7(), uuid.uuid7()
    uploader_one, uploader_two, collection_owner = User(email="sha-one@example.com"), User(
        email="sha-two@example.com"
    ), User(email="sha-collection@example.com")

    async with session_factory() as session:
        session.add_all([uploader_one, uploader_two, collection_owner])
        await session.flush()
        blocked_hash = BlockedPerceptualHash(
            perceptual_hash="1" * 16,
            hash_size=64,
            max_hamming_distance=0,
            reason=ModerationReason.ILLEGAL,
        )
        session.add(blocked_hash)
        await session.flush()
        canonical_meme, _ = await _add_meme_file(
            session,
            meme_id=canonical_meme_id,
            meme_file_id=canonical_file_id,
            sha256_hex=first_sha,
            created_at=now - timedelta(hours=5),
            visibility_mode=MemeVisibilityMode.FORCE_PUBLIC,
            is_public=True,
            like_count=100,
            tags=["canonical"],
            quality_score=0.9,
            s3_original_key="pipeline/originals/canonical.jpg",
        )
        obsolete_meme, _ = await _add_meme_file(
            session,
            meme_id=obsolete_meme_id,
            meme_file_id=obsolete_file_id,
            sha256_hex=first_sha,
            created_at=now - timedelta(hours=4),
            visibility_mode=MemeVisibilityMode.FORCE_PRIVATE,
            is_public=False,
            is_nsfw=True,
            like_count=200,
            tags=["obsolete"],
            ocr_text="obsolete OCR",
            quality_score=0.8,
            s3_original_key="pipeline/originals/obsolete.jpg",
            s3_web_video_key="pipeline/derived/obsolete.mp4",
        )
        moved_file = MemeFile(
            id=moved_file_id,
            meme_id=obsolete_meme_id,
            status=ContentProcessingStatus.READY,
            mime_type="image/png",
            s3_original_key="pipeline/originals/moved.png",
            sha256_hex="c" * 64,
            quality_score=0.5,
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=3),
        )
        session.add(moved_file)
        await session.flush()
        await _add_meme_file(
            session,
            meme_id=second_canonical_meme_id,
            meme_file_id=second_canonical_file_id,
            sha256_hex=second_sha,
            created_at=now - timedelta(hours=2),
        )
        await _add_meme_file(
            session,
            meme_id=second_obsolete_meme_id,
            meme_file_id=second_obsolete_file_id,
            sha256_hex=second_sha,
            created_at=now - timedelta(hours=1),
        )
        await _add_meme_file(
            session,
            meme_id=blocked_oldest_meme_id,
            meme_file_id=blocked_oldest_file_id,
            sha256_hex=blocked_sha,
            created_at=now - timedelta(minutes=50),
        )
        await _add_meme_file(
            session,
            meme_id=blocked_newer_meme_id,
            meme_file_id=blocked_newer_file_id,
            sha256_hex=blocked_sha,
            created_at=now - timedelta(minutes=40),
            status=ContentProcessingStatus.FAILED,
            blocked_perceptual_hash_id=blocked_hash.id,
        )
        await _add_meme_file(
            session,
            meme_id=failed_oldest_meme_id,
            meme_file_id=failed_oldest_file_id,
            sha256_hex=ready_sha,
            created_at=now - timedelta(minutes=30),
            status=ContentProcessingStatus.FAILED,
        )
        await _add_meme_file(
            session,
            meme_id=ready_newer_meme_id,
            meme_file_id=ready_newer_file_id,
            sha256_hex=ready_sha,
            created_at=now - timedelta(minutes=20),
        )

        canonical_source = MemeSource(
            file_id=canonical_file_id,
            platform=SourcePlatform.TELEGRAM,
            source_id="sha-canonical",
            post_id="1",
            source_kind=IngestSourceKind.USER_UPLOAD,
            uploader_user_id=uploader_one.id,
            is_first_source=True,
            source_alive=True,
            attach_reason=SourceAttachReason.NEW_FILE,
        )
        obsolete_source = MemeSource(
            file_id=obsolete_file_id,
            platform=SourcePlatform.TELEGRAM,
            source_id="sha-obsolete",
            post_id="2",
            source_kind=IngestSourceKind.USER_UPLOAD,
            uploader_user_id=uploader_two.id,
            is_first_source=True,
            source_alive=True,
            attach_reason=SourceAttachReason.NEW_FILE,
        )
        crawler_source = MemeSource(
            file_id=moved_file_id,
            platform=SourcePlatform.TELEGRAM,
            source_id="sha-crawler",
            post_id="3",
            source_kind=IngestSourceKind.PUBLIC_CRAWLER,
            is_first_source=False,
            source_alive=False,
            attach_reason=SourceAttachReason.PHASH_EXACT_NEW_FILE,
            matched_meme_file_id=obsolete_file_id,
        )
        session.add_all([canonical_source, obsolete_source, crawler_source])
        await session.flush()
        engagement_snapshot = MemeSourceEngagementSnapshot(
            meme_source_id=obsolete_source.id,
            captured_at=now,
            capture_reason=SourceEngagementCaptureReason.INGEST_INITIAL,
            comments_state=SourceEngagementCommentsState.UNKNOWN,
            fetch_status=SourceEngagementFetchStatus.SUCCESS,
            source_alive=True,
            view_count=17,
            raw_metrics={"view_count": 17},
        )

        favorites_one = Collection(
            owner_id=uploader_one.id,
            title="Favorites",
            kind=CollectionKind.FAVORITES,
            visibility=CollectionVisibility.PRIVATE,
        )
        favorites_two = Collection(
            owner_id=uploader_two.id,
            title="Favorites",
            kind=CollectionKind.FAVORITES,
            visibility=CollectionVisibility.PRIVATE,
        )
        custom_collection = Collection(owner_id=collection_owner.id, title="Custom")
        session.add_all([favorites_one, favorites_two, custom_collection])
        await session.flush()
        session.add_all(
            [
                CollectionMeme(
                    collection_id=favorites_one.id,
                    meme_id=canonical_meme_id,
                    added_by_user_id=uploader_one.id,
                ),
                CollectionMeme(
                    collection_id=favorites_one.id,
                    meme_id=obsolete_meme_id,
                    added_by_user_id=uploader_one.id,
                ),
                CollectionMeme(
                    collection_id=favorites_two.id,
                    meme_id=obsolete_meme_id,
                    added_by_user_id=uploader_two.id,
                ),
                CollectionMeme(
                    collection_id=custom_collection.id,
                    meme_id=obsolete_meme_id,
                    added_by_user_id=collection_owner.id,
                ),
                PinnedMeme(user_id=uploader_one.id, meme_id=canonical_meme_id, position=1),
                PinnedMeme(user_id=uploader_one.id, meme_id=obsolete_meme_id, position=2),
                PinnedMeme(user_id=uploader_two.id, meme_id=obsolete_meme_id, position=1),
            ]
        )

        report = ModerationReport(
            meme_id=obsolete_meme_id,
            reporter_user_id=collection_owner.id,
            status=ModerationReportStatus.RESOLVED,
            reason=ModerationReason.SPAM,
        )
        session.add(report)
        await session.flush()
        decision = ModerationDecision(
            meme_id=obsolete_meme_id,
            report_id=report.id,
            action=ModerationAction.HIDE,
            reason=ModerationReason.SPAM,
            previous_is_public=True,
            previous_visibility_mode=MemeVisibilityMode.AUTO,
            previous_is_nsfw=False,
            new_is_public=False,
            new_visibility_mode=MemeVisibilityMode.FORCE_PRIVATE,
            new_is_nsfw=True,
        )
        pipeline_request = PipelineIngestRequest(
            source_platform=SourcePlatform.TELEGRAM,
            source_id="sha-request",
            post_id="4",
            source_kind=IngestSourceKind.USER_UPLOAD,
            uploader_user_id=uploader_two.id,
            status=PipelineIngestRequestStatus.MATERIALIZED,
            materialized_meme_id=obsolete_meme_id,
            materialized_meme_file_id=obsolete_file_id,
            matched_meme_file_id=obsolete_file_id,
            source_attach_reason=SourceAttachReason.SHA256_EXACT_EXISTING_FILE,
        )
        stage = PipelineStageJournal(
            meme_file_id=obsolete_file_id,
            stage=ContentPipelineStage.CLASSIFY,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            is_retryable=False,
        )
        sync_snapshot = MemeFileSyncTargetSnapshot(
            meme_file_id=obsolete_file_id,
            sync_target=SyncTargetKind.QDRANT,
            status=SyncTargetStatus.SYNCED,
            attempt_count=1,
            last_payload_preview={"meme_file_id": str(obsolete_file_id)},
        )
        ocr_result = MemeFileOCRResult(
            meme_file_id=obsolete_file_id,
            engine="test-ocr",
            language=ContentLanguage.EN,
            extracted_text="durable OCR",
        )
        telegram_cache = TelegramFileIdCache(
            meme_file_id=obsolete_file_id,
            bot_scope="test-bot",
            media_format=TelegramMediaFormat.PHOTO,
            telegram_file_id="telegram-file-id",
            telegram_file_unique_id="telegram-unique-id",
        )
        embedding_cache = EmbeddingCache(
            input_hash="d" * 64,
            input_type=EmbeddingInputType.IMAGE,
            embedding=b"embedding",
            model_version="test-model",
            source_file_id=obsolete_file_id,
        )
        motd = MemeOfTheDaySelection(
            selected_for=date.today(),
            algorithm_version="test-v1",
            meme_id=obsolete_meme_id,
            score=1.0,
            score_components={"score": 1.0},
            reason="test",
            candidate_count=2,
            refreshed_at=now,
        )
        canonical_seo = MemeSeoPage(
            meme_id=canonical_meme_id,
            slug="canonical-seo",
            page_title="Canonical",
            meta_description="Canonical description",
            alt_text="Canonical alt",
            tags=["canonical-seo"],
            model_id="test-model",
            prompt_version="v1",
            generated_at=now,
        )
        obsolete_seo = MemeSeoPage(
            meme_id=obsolete_meme_id,
            slug="obsolete-seo",
            page_title="Obsolete",
            meta_description="Obsolete description",
            alt_text="Obsolete alt",
            tags=["obsolete-seo"],
            model_id="test-model",
            prompt_version="v1",
            generated_at=now,
        )
        top_level_event = AnalyticsEvent(
            event_type=AnalyticsEventType.MEME_VIEW,
            payload={"meme_id": str(obsolete_meme_id)},
        )
        nested_event = AnalyticsEvent(
            event_type=AnalyticsEventType.MEME_LIKE,
            payload={"refs": {"meme_id": str(obsolete_meme_id)}},
        )
        outbox = RabbitMQOutboxMessage(
            exchange="memexpert.pipeline",
            routing_key="pipeline.classify",
            payload={
                "meme_id": str(obsolete_meme_id),
                "meme_file_id": str(obsolete_file_id),
                "matched_meme_file_id": str(obsolete_file_id),
            },
            headers={},
            content_type="application/json",
            message_id=str(uuid.uuid7()),
            event_type="meme_ready",
            aggregate_type="meme_file",
            aggregate_id=str(obsolete_file_id),
            ordering_key=str(obsolete_file_id),
            status=RabbitMQOutboxMessageStatus.PENDING,
        )
        session.add_all(
            [
                engagement_snapshot,
                decision,
                pipeline_request,
                stage,
                sync_snapshot,
                ocr_result,
                telegram_cache,
                embedding_cache,
                motd,
                canonical_seo,
                obsolete_seo,
                top_level_event,
                nested_event,
                outbox,
            ]
        )
        await session.commit()

    async with session_factory() as session:
        first_result = await ShaDuplicateReconciliationService(session).reconcile_next()
        assert first_result is not None
        assert first_result.sha256_hex == first_sha
        assert first_result.canonical_meme_id == canonical_meme_id
        assert first_result.canonical_meme_file_id == canonical_file_id
        assert first_result.obsolete_meme_ids == (obsolete_meme_id,)
        assert first_result.obsolete_meme_file_ids == (obsolete_file_id,)
        await session.commit()

    async with session_factory() as session:
        canonical = await session.get(Meme, canonical_meme_id)
        assert canonical is not None
        assert canonical.visibility_mode is MemeVisibilityMode.FORCE_PRIVATE
        assert canonical.is_public is False
        assert canonical.is_nsfw is True
        assert canonical.tags == ["canonical", "obsolete"]
        assert canonical.ocr_text == "obsolete OCR"
        assert canonical.like_count == 2
        assert canonical.primary_file_id == canonical_file_id
        assert await session.get(Meme, obsolete_meme_id) is None
        assert await session.get(MemeFile, obsolete_file_id) is None
        persisted_moved_file = await session.get(MemeFile, moved_file_id)
        assert persisted_moved_file is not None
        assert persisted_moved_file.meme_id == canonical_meme_id

        assert await session.scalar(
            select(func.count()).select_from(MemeFile).where(MemeFile.sha256_hex == first_sha)
        ) == 1
        assert set(
            await session.scalars(
                select(CollectionMeme.collection_id).where(CollectionMeme.meme_id == canonical_meme_id)
            )
        ) == {favorites_one.id, favorites_two.id, custom_collection.id}
        assert set(
            await session.scalars(select(PinnedMeme.user_id).where(PinnedMeme.meme_id == canonical_meme_id))
        ) == {uploader_one.id, uploader_two.id}

        persisted_obsolete_source = await session.get(MemeSource, obsolete_source.id)
        persisted_crawler_source = await session.get(MemeSource, crawler_source.id)
        assert persisted_obsolete_source is not None
        assert persisted_obsolete_source.file_id == canonical_file_id
        assert persisted_crawler_source is not None
        assert persisted_crawler_source.file_id == moved_file_id
        assert persisted_crawler_source.source_alive is False
        assert persisted_crawler_source.matched_meme_file_id == canonical_file_id
        assert await session.get(MemeSourceEngagementSnapshot, engagement_snapshot.id) is not None

        persisted_report = await session.get(ModerationReport, report.id)
        persisted_decision = await session.get(ModerationDecision, decision.id)
        assert persisted_report is not None and persisted_report.meme_id == canonical_meme_id
        assert persisted_decision is not None and persisted_decision.meme_id == canonical_meme_id
        persisted_request = await session.get(PipelineIngestRequest, pipeline_request.id)
        assert persisted_request is not None
        assert persisted_request.materialized_meme_id == canonical_meme_id
        assert persisted_request.materialized_meme_file_id == canonical_file_id
        assert persisted_request.matched_meme_file_id == canonical_file_id
        persisted_stage = await session.get(PipelineStageJournal, stage.id)
        persisted_sync_snapshot = await session.get(MemeFileSyncTargetSnapshot, sync_snapshot.id)
        persisted_ocr_result = await session.get(MemeFileOCRResult, ocr_result.id)
        persisted_telegram_cache = await session.get(TelegramFileIdCache, telegram_cache.id)
        persisted_embedding_cache = await session.get(EmbeddingCache, embedding_cache.id)
        persisted_motd = await session.get(MemeOfTheDaySelection, motd.id)
        assert persisted_stage is not None and persisted_stage.meme_file_id == canonical_file_id
        assert persisted_sync_snapshot is not None and persisted_sync_snapshot.meme_file_id == canonical_file_id
        assert persisted_ocr_result is not None and persisted_ocr_result.meme_file_id == canonical_file_id
        assert persisted_telegram_cache is not None and persisted_telegram_cache.meme_file_id == canonical_file_id
        assert persisted_embedding_cache is not None and persisted_embedding_cache.source_file_id == canonical_file_id
        assert persisted_motd is not None and persisted_motd.meme_id == canonical_meme_id

        persisted_seo = await session.get(MemeSeoPage, canonical_meme_id)
        assert persisted_seo is not None
        assert persisted_seo.slug == "canonical-seo"
        assert persisted_seo.tags == ["canonical-seo", "obsolete-seo"]
        assert await session.get(MemeSeoPage, obsolete_meme_id) is None
        persisted_top_level_event = await session.get(AnalyticsEvent, top_level_event.id)
        persisted_nested_event = await session.get(AnalyticsEvent, nested_event.id)
        assert persisted_top_level_event is not None
        assert persisted_top_level_event.payload == {"meme_id": str(canonical_meme_id)}
        assert persisted_nested_event is not None
        assert persisted_nested_event.payload == {"refs": {"meme_id": str(canonical_meme_id)}}
        persisted_outbox = await session.get(RabbitMQOutboxMessage, outbox.id)
        assert persisted_outbox is not None
        assert persisted_outbox.aggregate_id == str(canonical_file_id)
        assert persisted_outbox.ordering_key == str(canonical_file_id)
        assert persisted_outbox.payload == {
            "meme_id": str(canonical_meme_id),
            "meme_file_id": str(canonical_file_id),
            "matched_meme_file_id": str(canonical_file_id),
        }

        first_merge_log = await session.scalar(
            select(MemeMergeLog).where(
                MemeMergeLog.merge_reason == MERGE_REASON_SHA256_RECONCILIATION,
                MemeMergeLog.source_meme_file_id == obsolete_file_id,
            )
        )
        assert first_merge_log is not None
        assert first_merge_log.details["obsolete_meme_ids"] == [str(obsolete_meme_id)]
        assert first_merge_log.details["obsolete_files"] == [
            {
                "meme_file_id": str(obsolete_file_id),
                "meme_id": str(obsolete_meme_id),
                "s3_original_key": "pipeline/originals/obsolete.jpg",
                "s3_web_video_key": "pipeline/derived/obsolete.mp4",
                "qdrant_point_id": str(obsolete_file_id),
                "meilisearch_document_id": obsolete_file_id.hex,
            }
        ]

    async with session_factory() as session:
        second_result = await ShaDuplicateReconciliationService(session).reconcile_next()
        assert second_result is not None
        assert second_result.sha256_hex == second_sha
        assert second_result.canonical_meme_id == second_canonical_meme_id
        assert second_result.obsolete_meme_ids == (second_obsolete_meme_id,)
        await session.commit()

    async with session_factory() as session:
        blocked_result = await ShaDuplicateReconciliationService(session).reconcile_next()
        assert blocked_result is not None
        assert blocked_result.sha256_hex == blocked_sha
        assert blocked_result.canonical_meme_id == blocked_oldest_meme_id
        assert blocked_result.canonical_meme_file_id == blocked_newer_file_id
        assert blocked_result.obsolete_meme_ids == (blocked_newer_meme_id,)
        assert blocked_result.obsolete_meme_file_ids == (blocked_oldest_file_id,)
        await session.commit()

    async with session_factory() as session:
        blocked_file = await session.get(MemeFile, blocked_newer_file_id)
        assert blocked_file is not None
        assert blocked_file.meme_id == blocked_oldest_meme_id
        assert blocked_file.status is ContentProcessingStatus.FAILED
        assert blocked_file.blocked_perceptual_hash_id == blocked_hash.id
        assert sha_match_attach_reason(blocked_file) is SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE
        assert await session.get(MemeFile, blocked_oldest_file_id) is None
        assert await session.get(Meme, blocked_newer_meme_id) is None

    async with session_factory() as session:
        ready_result = await ShaDuplicateReconciliationService(session).reconcile_next()
        assert ready_result is not None
        assert ready_result.sha256_hex == ready_sha
        assert ready_result.canonical_meme_id == failed_oldest_meme_id
        assert ready_result.canonical_meme_file_id == ready_newer_file_id
        assert ready_result.obsolete_meme_ids == (ready_newer_meme_id,)
        assert ready_result.obsolete_meme_file_ids == (failed_oldest_file_id,)
        await session.commit()

    async with session_factory() as session:
        ready_file = await session.get(MemeFile, ready_newer_file_id)
        assert ready_file is not None
        assert ready_file.meme_id == failed_oldest_meme_id
        assert ready_file.status is ContentProcessingStatus.READY
        assert await session.get(MemeFile, failed_oldest_file_id) is None
        assert await session.get(Meme, ready_newer_meme_id) is None

    async with session_factory() as session:
        service = ShaDuplicateReconciliationService(session)
        assert await service.reconcile_next() is None
        assert await session.scalar(
            select(func.count()).select_from(MemeMergeLog).where(
                MemeMergeLog.merge_reason == MERGE_REASON_SHA256_RECONCILIATION
            )
        ) == 4

    await _run_upgrade(config, "0032")
    async with engine.connect() as connection:
        duplicate_count = await connection.scalar(
            text(
                """
                SELECT count(*)
                FROM (
                    SELECT sha256_hex
                    FROM meme_files
                    WHERE sha256_hex IS NOT NULL
                    GROUP BY sha256_hex
                    HAVING count(*) > 1
                ) AS duplicate_groups
                """
            )
        )
        removed_columns = set(
            await connection.scalars(
                text(
                    """
                    SELECT table_name || '.' || column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND (
                          (table_name = 'memes' AND column_name = 'author_user_id')
                          OR (
                              table_name = 'pipeline_ingest_requests'
                              AND column_name = 'owner_user_id'
                          )
                      )
                    """
                )
            )
        )
        unique_index_exists = await connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = 'uq_meme_files_sha256_hex_not_null'
                )
                """
            )
        )
    assert duplicate_count == 0
    assert removed_columns == set()
    assert unique_index_exists is True
