"""Focused regression tests for the container PRD E2E seed helpers."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.core.voyage import VoyageEmbeddingResult
from memexpert.models.base import utcnow
from memexpert.models.content import EmbeddingCache, Meme, MemeFile, MemeFileOCRResult, MemeFileSyncTargetSnapshot
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    EmbeddingInputType,
    SyncTargetKind,
    SyncTargetStatus,
)
from scripts import seed_e2e

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.meilisearch import PipelineMeilisearchDocument
    from memexpert.core.qdrant import QdrantSyncPayload

class RecordingQdrantSyncClient:
    def __init__(self) -> None:
        self.upsert_calls: list[tuple[QdrantSyncPayload, tuple[float, ...]]] = []

    async def upsert_meme_point(self, payload: QdrantSyncPayload, vector: tuple[float, ...]) -> None:
        self.upsert_calls.append((payload, vector))

    async def fetch_meme_point(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id
        return None

    async def delete_meme_point(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id


class RecordingMeilisearchSyncClient:
    def __init__(self) -> None:
        self.upsert_calls: list[PipelineMeilisearchDocument] = []

    async def upsert_document(self, document: PipelineMeilisearchDocument) -> None:
        self.upsert_calls.append(document)

    async def fetch_document(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id
        return None

    async def delete_document(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id

    async def ensure_index(self) -> None:
        return None

    async def search(self, query: str, *, limit: int = 20, prefilter: object | None = None) -> list[dict[str, Any]]:
        _ = query
        _ = limit
        _ = prefilter
        return []


@pytest.mark.asyncio
async def test_publish_created_meme_resync_rebuilds_public_indexes_from_canonical_db_state(
    migrated_db_session: AsyncSession,
) -> None:
    embedding = VoyageEmbeddingResult(
        model="test-model",
        dimensions=3,
        vector=(0.1, 0.2, 0.3),
        input_hash=uuid.uuid4().hex,
    )
    meme = Meme(
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        tags=[],
        is_public=False,
        is_nsfw=True,
        ocr_text="pre-public upload",
    )
    migrated_db_session.add(meme)
    await migrated_db_session.flush()

    meme_file = MemeFile(
        meme_id=meme.id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"pipeline/originals/{meme.id}.png",
        mime_type="image/png",
        quality_score=0.9,
        is_primary=True,
    )
    migrated_db_session.add(meme_file)
    await migrated_db_session.flush()
    meme.primary_file_id = meme_file.id
    migrated_db_session.add_all(
        [
            MemeFileOCRResult(
                meme_file_id=meme_file.id,
                engine="fake-test",
                fallback_engine=None,
                fallback_used=False,
                low_confidence=False,
                confidence=1.0,
                language=ContentLanguage.EN,
                extracted_text="cat generated upload ocr",
                source_object_key=meme_file.s3_original_key,
            ),
            EmbeddingCache(
                input_hash=embedding.input_hash,
                input_type=EmbeddingInputType.IMAGE,
                embedding=embedding.embedding_bytes,
                model_version=embedding.model,
                source_file_id=meme_file.id,
            ),
            seed_e2e._build_sync_snapshot(
                meme_file_id=meme_file.id,
                target=SyncTargetKind.QDRANT,
                preview={"is_public": False, "tags": []},
                now=utcnow(),
            ),
            seed_e2e._build_sync_snapshot(
                meme_file_id=meme_file.id,
                target=SyncTargetKind.MEILISEARCH,
                preview={"is_public": False, "tags": []},
                now=utcnow(),
            ),
        ],
    )
    await migrated_db_session.commit()

    slug = await seed_e2e.publish_created_meme_in_session(migrated_db_session, meme_id=meme.id, query="cat")
    await migrated_db_session.commit()

    qdrant_client = RecordingQdrantSyncClient()
    meili_client = RecordingMeilisearchSyncClient()
    await seed_e2e.resync_created_public_meme_indexes_in_session(
        migrated_db_session,
        settings=Settings.model_validate({"pipeline_voyage_output_dimensions": 3}),
        meme_file_id=meme_file.id,
        qdrant_sync_client=qdrant_client,
        meili_client=meili_client,
    )

    assert len(qdrant_client.upsert_calls) == 1
    qdrant_payload, qdrant_vector = qdrant_client.upsert_calls[0]
    assert qdrant_payload.meme_id == meme.id
    assert qdrant_payload.meme_file_id == meme_file.id
    assert qdrant_payload.is_public is True
    assert qdrant_payload.is_nsfw is False
    assert qdrant_payload.tags == ["cat", "e2e-prd"]
    assert qdrant_payload.seo_page_slug == slug
    assert qdrant_vector == pytest.approx(embedding.vector)

    assert len(meili_client.upsert_calls) == 1
    meili_document = meili_client.upsert_calls[0]
    assert meili_document.id == meme_file.id.hex
    assert meili_document.meme_id == str(meme.id)
    assert meili_document.is_public is True
    assert meili_document.is_nsfw is False
    assert meili_document.tags == ["cat", "e2e-prd"]
    assert meili_document.seo_page_slug == slug
    assert meili_document.ocr_text == "cat generated upload ocr"

    snapshot_count = await migrated_db_session.scalar(
        select(func.count()).select_from(MemeFileSyncTargetSnapshot).where(
            MemeFileSyncTargetSnapshot.meme_file_id == meme_file.id,
        ),
    )
    assert snapshot_count == 2
    snapshots = {
        snapshot.sync_target: snapshot
        for snapshot in (
            await migrated_db_session.execute(
                select(MemeFileSyncTargetSnapshot).where(MemeFileSyncTargetSnapshot.meme_file_id == meme_file.id),
            )
        ).scalars()
    }
    assert snapshots[SyncTargetKind.QDRANT].status is SyncTargetStatus.SYNCED
    assert snapshots[SyncTargetKind.QDRANT].attempt_count == 2
    assert snapshots[SyncTargetKind.QDRANT].last_payload_preview["is_public"] is True
    assert snapshots[SyncTargetKind.MEILISEARCH].status is SyncTargetStatus.SYNCED
    assert snapshots[SyncTargetKind.MEILISEARCH].attempt_count == 2
    assert snapshots[SyncTargetKind.MEILISEARCH].last_payload_preview["is_public"] is True


def test_wait_for_public_search_contains_polls_public_api_until_created_meme_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meme_id = uuid.uuid4()
    payloads: list[dict[str, Any]] = [
        {"items": [{"meme": {"id": str(uuid.uuid4())}}]},
        {"items": [{"meme": {"id": str(meme_id)}}]},
    ]

    class PublicSearchClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def public_search(self, query: str) -> dict[str, Any]:
            self.queries.append(query)
            return payloads.pop(0)

    client = PublicSearchClient()
    monkeypatch.setattr(seed_e2e.time, "sleep", lambda _: None)

    result = seed_e2e.wait_for_public_search_contains(
        cast("seed_e2e.PipelineApiClient", client),
        query="cat",
        meme_id=meme_id,
        timeout_seconds=1.0,
    )

    assert result == {"items": [{"meme": {"id": str(meme_id)}}]}
    assert client.queries == ["cat", "cat"]
