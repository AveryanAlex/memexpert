"""Focused tests for scheduler-owned bounded backend batch jobs."""

from __future__ import annotations

import array
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from memexpert.core.config import Settings
from memexpert.core.qdrant import QdrantSyncPayload, QdrantSyncTimeoutError
from memexpert.models.base import utcnow
from memexpert.models.content import EmbeddingCache, Meme, MemeFile, MemeFileSyncTargetSnapshot, MemeSeoPage
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    EmbeddingInputType,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.services.content_pipeline_helpers import build_sync_preview_model
from memexpert.services.content_pipeline_reporting import decode_sync_preview
from memexpert.services.meme_seo import MemeSeoProviderResult
from memexpert.services.scheduler_batch_jobs import (
    SearchIndexBatchJobService,
    SeoBacklogBatchJobService,
    run_scheduler_search_index_sync_batch,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from memexpert.core.meilisearch import PipelineMeilisearchDocument


class FakeQdrantSyncClient:
    def __init__(self, *, fail_for: set[uuid.UUID] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.upserts: list[tuple[QdrantSyncPayload, tuple[float, ...]]] = []

    async def upsert_meme_point(self, payload: QdrantSyncPayload, vector: tuple[float, ...]) -> None:
        self.upserts.append((payload, vector))
        if payload.meme_file_id in self.fail_for:
            raise QdrantSyncTimeoutError("qdrant timeout for test")

    async def fetch_meme_point(self, meme_file_id: uuid.UUID) -> None:
        return None

    async def delete_meme_point(self, meme_file_id: uuid.UUID) -> None:
        return None


class FakeMeilisearchSyncClient:
    def __init__(self) -> None:
        self.upserts: list[str] = []

    async def upsert_document(self, document: PipelineMeilisearchDocument) -> None:
        self.upserts.append(document.meme_file_id)

    async def fetch_document(self, meme_file_id: uuid.UUID) -> None:
        return None

    async def delete_document(self, meme_file_id: uuid.UUID) -> None:
        return None

    async def ensure_index(self) -> None:
        return None

    async def search(self, query: str, *, limit: int = 20, prefilter: object | None = None) -> list[dict[str, object]]:
        return []


class FakeSeoProvider:
    model_id = "fake-seo-model"

    def __init__(self, *, prompt_version: str, fail_for: set[uuid.UUID] | None = None) -> None:
        self.prompt_version = prompt_version
        self.fail_for = fail_for or set()
        self.calls: list[uuid.UUID] = []

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        self.calls.append(meme.id)
        if meme.id in self.fail_for:
            raise RuntimeError("seo provider failed for test")
        slug_seed = str(meme.id)[:8]
        return MemeSeoProviderResult(
            page_title=f"SEO {slug_seed}",
            meta_description=f"SEO description {slug_seed}",
            alt_text=f"SEO alt {slug_seed}",
            slug=f"seo-{slug_seed}",
            tags=("seo", slug_seed),
        )


@dataclass(frozen=True, slots=True)
class ReadyMemeFixture:
    meme: Meme
    meme_file: MemeFile


async def test_search_index_batch_processes_both_targets_with_per_target_limit(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _create_ready_meme_file(migrated_db_session, popularity_score=10.0)
    _ = await _create_ready_meme_file(migrated_db_session, popularity_score=5.0)
    await migrated_db_session.commit()
    settings = Settings.model_validate(
        {
            "pipeline_voyage_output_dimensions": 2,
            "scheduler_search_index_sync_batch_size": 1,
        }
    )
    qdrant_client = FakeQdrantSyncClient()
    meili_client = FakeMeilisearchSyncClient()

    result = await run_scheduler_search_index_sync_batch(
        postgres_session_factory,
        settings=settings,
        qdrant_client=qdrant_client,
        meilisearch_client=meili_client,
    )

    assert result.scanned == 2
    assert result.updated == 2
    assert result.failed == 0
    assert len(qdrant_client.upserts) == 1
    assert len(meili_client.upserts) == 1
    qdrant_payload, vector = qdrant_client.upserts[0]
    assert qdrant_payload.meme_file_id == first.meme_file.id
    assert vector == pytest.approx((0.25, 0.75))

    snapshots = await _load_snapshots(migrated_db_session, first.meme_file.id)
    assert {snapshot.sync_target for snapshot in snapshots} == {SyncTargetKind.QDRANT, SyncTargetKind.MEILISEARCH}
    assert {snapshot.status for snapshot in snapshots} == {SyncTargetStatus.SYNCED}
    for snapshot in snapshots:
        assert snapshot.attempt_count == 1
        decoded_preview = decode_sync_preview(snapshot.last_payload_preview, target=snapshot.sync_target)
        assert decoded_preview is not None
        assert decoded_preview.target is snapshot.sync_target


async def test_search_index_batch_reprocesses_stale_synced_snapshot(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _create_ready_meme_file(migrated_db_session, popularity_score=3.0)
    old_time = utcnow() - timedelta(days=30)
    future_time = utcnow() + timedelta(days=1)
    migrated_db_session.add_all(
        [
            MemeFileSyncTargetSnapshot(
                meme_file_id=fixture.meme_file.id,
                sync_target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.SYNCED,
                last_success_at=future_time,
                last_attempt_at=future_time,
                attempt_count=1,
            ),
            MemeFileSyncTargetSnapshot(
                meme_file_id=fixture.meme_file.id,
                sync_target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.SYNCED,
                last_success_at=old_time,
                last_attempt_at=old_time,
                attempt_count=2,
            ),
        ]
    )
    await migrated_db_session.commit()
    meili_client = FakeMeilisearchSyncClient()

    result = await run_scheduler_search_index_sync_batch(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "pipeline_voyage_output_dimensions": 2,
                "scheduler_search_index_sync_batch_size": 10,
            }
        ),
        qdrant_client=FakeQdrantSyncClient(),
        meilisearch_client=meili_client,
    )

    assert result.scanned == 1
    assert result.updated == 1
    assert meili_client.upserts == [str(fixture.meme_file.id)]
    snapshots = await _load_snapshots(migrated_db_session, fixture.meme_file.id)
    meili_snapshot = next(snapshot for snapshot in snapshots if snapshot.sync_target is SyncTargetKind.MEILISEARCH)
    assert meili_snapshot.status is SyncTargetStatus.SYNCED
    assert meili_snapshot.attempt_count == 3
    assert meili_snapshot.last_success_at is not None
    assert meili_snapshot.last_success_at > old_time


async def test_search_index_batch_records_failure_and_preserves_last_good_preview(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    retry_fixture = await _create_ready_meme_file(migrated_db_session)
    processing_fixture = await _create_ready_meme_file(migrated_db_session)
    last_good_time = utcnow() - timedelta(days=1)
    last_good_preview = build_sync_preview_model(
        {"meme_file_id": str(retry_fixture.meme_file.id), "is_public": True},
        target=SyncTargetKind.QDRANT,
    ).model_dump(mode="json")
    migrated_db_session.add_all(
        [
            MemeFileSyncTargetSnapshot(
                meme_file_id=retry_fixture.meme_file.id,
                sync_target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.FAILED,
                last_payload_preview=last_good_preview,
                last_success_at=last_good_time,
                last_attempt_at=last_good_time,
                attempt_count=3,
            ),
            MemeFileSyncTargetSnapshot(
                meme_file_id=retry_fixture.meme_file.id,
                sync_target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.SYNCED,
                last_success_at=utcnow() + timedelta(days=1),
                attempt_count=1,
            ),
            MemeFileSyncTargetSnapshot(
                meme_file_id=processing_fixture.meme_file.id,
                sync_target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.PROCESSING,
                last_attempt_at=utcnow(),
                attempt_count=4,
            ),
            MemeFileSyncTargetSnapshot(
                meme_file_id=processing_fixture.meme_file.id,
                sync_target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.SYNCED,
                last_success_at=utcnow() + timedelta(days=1),
                attempt_count=1,
            ),
        ]
    )
    await migrated_db_session.commit()
    qdrant_client = FakeQdrantSyncClient(fail_for={retry_fixture.meme_file.id})

    result = await SearchIndexBatchJobService(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "pipeline_voyage_output_dimensions": 2,
                "scheduler_search_index_sync_batch_size": 10,
                "scheduler_search_index_sync_processing_timeout_seconds": 60.0,
            }
        ),
        qdrant_client=qdrant_client,
        meilisearch_client=FakeMeilisearchSyncClient(),
    ).run()

    assert result.scanned == 1
    assert result.failed == 1
    assert [payload.meme_file_id for payload, _ in qdrant_client.upserts] == [retry_fixture.meme_file.id]
    snapshots = await _load_snapshots(migrated_db_session, retry_fixture.meme_file.id)
    retry_snapshot = next(snapshot for snapshot in snapshots if snapshot.sync_target is SyncTargetKind.QDRANT)
    assert retry_snapshot.status is SyncTargetStatus.FAILED
    assert retry_snapshot.attempt_count == 4
    assert retry_snapshot.normalized_reason == "sync_qdrant_timeout"
    assert retry_snapshot.last_success_at == last_good_time
    assert retry_snapshot.last_payload_preview == last_good_preview


async def test_search_index_batch_reclaims_stale_processing_snapshot(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _create_ready_meme_file(migrated_db_session)
    stale_attempt_time = utcnow() - timedelta(minutes=30)
    migrated_db_session.add_all(
        [
            MemeFileSyncTargetSnapshot(
                meme_file_id=fixture.meme_file.id,
                sync_target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.PROCESSING,
                last_attempt_at=stale_attempt_time,
                attempt_count=2,
            ),
            MemeFileSyncTargetSnapshot(
                meme_file_id=fixture.meme_file.id,
                sync_target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.SYNCED,
                last_success_at=utcnow() + timedelta(days=1),
                attempt_count=1,
            ),
        ]
    )
    await migrated_db_session.commit()
    qdrant_client = FakeQdrantSyncClient()

    result = await SearchIndexBatchJobService(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "pipeline_voyage_output_dimensions": 2,
                "scheduler_search_index_sync_batch_size": 10,
                "scheduler_search_index_sync_processing_timeout_seconds": 60.0,
            }
        ),
        qdrant_client=qdrant_client,
        meilisearch_client=FakeMeilisearchSyncClient(),
    ).run()

    assert result.scanned == 1
    assert result.updated == 1
    assert [payload.meme_file_id for payload, _ in qdrant_client.upserts] == [fixture.meme_file.id]
    snapshots = await _load_snapshots(migrated_db_session, fixture.meme_file.id)
    qdrant_snapshot = next(snapshot for snapshot in snapshots if snapshot.sync_target is SyncTargetKind.QDRANT)
    assert qdrant_snapshot.status is SyncTargetStatus.SYNCED
    assert qdrant_snapshot.attempt_count == 3


async def test_seo_backlog_prioritizes_missing_then_stale_and_skips_unsafe_or_manual_pages(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    missing = await _create_ready_meme_file(migrated_db_session, popularity_score=1.0)
    stale = await _create_ready_meme_file(migrated_db_session, popularity_score=100.0)
    manual = await _create_ready_meme_file(migrated_db_session, popularity_score=200.0)
    private = await _create_ready_meme_file(migrated_db_session, popularity_score=300.0, is_public=False)
    nsfw = await _create_ready_meme_file(migrated_db_session, popularity_score=400.0, is_nsfw=True)
    missing_meme_id = missing.meme.id
    stale_meme_id = stale.meme.id
    manual_meme_id = manual.meme.id
    private_meme_id = private.meme.id
    nsfw_meme_id = nsfw.meme.id
    migrated_db_session.add_all(
        [
            _seo_page(stale_meme_id, slug="stale", prompt_version="old-version"),
            _seo_page(manual_meme_id, slug="manual", prompt_version="old-version", edited=True),
        ]
    )
    await migrated_db_session.commit()
    provider = FakeSeoProvider(prompt_version="new-version")

    result = await SeoBacklogBatchJobService(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "pipeline_seo_prompt_version": "new-version",
                "scheduler_seo_backlog_batch_size": 2,
            }
        ),
        provider=provider,
    ).run()

    assert result.scanned == 2
    assert result.updated == 2
    assert provider.calls == [missing_meme_id, stale_meme_id]
    pages = await _load_seo_pages(migrated_db_session)
    assert pages[missing_meme_id].prompt_version == "new-version"
    assert pages[stale_meme_id].prompt_version == "new-version"
    assert pages[manual_meme_id].slug == "manual"
    assert pages[manual_meme_id].prompt_version == "old-version"
    assert private_meme_id not in pages
    assert nsfw_meme_id not in pages


async def test_seo_backlog_failure_does_not_retry_same_meme_within_one_batch(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    failing = await _create_ready_meme_file(migrated_db_session, popularity_score=20.0)
    next_candidate = await _create_ready_meme_file(migrated_db_session, popularity_score=10.0)
    failing_meme_id = failing.meme.id
    next_candidate_meme_id = next_candidate.meme.id
    await migrated_db_session.commit()
    provider = FakeSeoProvider(prompt_version="seo-v2", fail_for={failing_meme_id})

    result = await SeoBacklogBatchJobService(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "pipeline_seo_prompt_version": "seo-v2",
                "scheduler_seo_backlog_batch_size": 2,
            }
        ),
        provider=provider,
    ).run()

    assert result.scanned == 2
    assert result.failed == 1
    assert result.updated == 1
    assert provider.calls == [failing_meme_id, failing_meme_id, next_candidate_meme_id]
    pages = await _load_seo_pages(migrated_db_session)
    assert failing_meme_id not in pages
    assert pages[next_candidate_meme_id].prompt_version == "seo-v2"


async def _create_ready_meme_file(
    session: AsyncSession,
    *,
    popularity_score: float = 0.0,
    is_public: bool = True,
    is_nsfw: bool = False,
) -> ReadyMemeFixture:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=["frog"],
        is_public=is_public,
        is_nsfw=is_nsfw,
        popularity_score=popularity_score,
        like_count=int(popularity_score),
        ocr_text="frog text",
    )
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"pipeline/originals/{file_id}.jpg",
        mime_type="image/jpeg",
        quality_score=0.8,
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    session.add(
        EmbeddingCache(
            input_hash=(file_id.hex * 2)[:64],
            input_type=EmbeddingInputType.IMAGE,
            embedding=_embedding_bytes((0.25, 0.75)),
            model_version="test-model",
            source_file_id=file_id,
        )
    )
    await session.flush()
    return ReadyMemeFixture(meme=meme, meme_file=meme_file)


def _seo_page(meme_id: uuid.UUID, *, slug: str, prompt_version: str, edited: bool = False) -> MemeSeoPage:
    return MemeSeoPage(
        meme_id=meme_id,
        slug=slug,
        page_title=f"{slug} title",
        meta_description=f"{slug} description",
        alt_text=f"{slug} alt",
        model_id="old-model",
        prompt_version=prompt_version,
        edited_at=utcnow() if edited else None,
    )


def _embedding_bytes(values: tuple[float, ...]) -> bytes:
    float_array = array.array("f", values)
    return float_array.tobytes()


async def _load_snapshots(session: AsyncSession, meme_file_id: uuid.UUID) -> list[MemeFileSyncTargetSnapshot]:
    session.expire_all()
    result = await session.execute(
        select(MemeFileSyncTargetSnapshot)
        .where(MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id)
        .order_by(MemeFileSyncTargetSnapshot.sync_target)
    )
    return list(result.scalars().all())


async def _load_seo_pages(session: AsyncSession) -> dict[uuid.UUID, MemeSeoPage]:
    session.expire_all()
    result = await session.execute(select(MemeSeoPage))
    return {page.meme_id: page for page in result.scalars().all()}
