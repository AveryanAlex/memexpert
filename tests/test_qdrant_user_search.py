"""Unit tests for the user-facing Qdrant semantic search adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from memexpert.api.dependencies.meme import get_meme_search_service
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import (
    PipelineQdrantClient,
    PipelineQdrantUserSearchClient,
    QdrantSimilarityMatch,
    QdrantSyncPayload,
    QdrantUserSearchMatch,
    _build_meme_point,
    _build_sync_preview,
)
from memexpert.core.search_index_prefilter import SearchIndexPrefilter, SearchIndexPrefilterScope
from memexpert.core.voyage import PipelineVoyageClient
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService

if TYPE_CHECKING:
    from memexpert.core.config import Settings


@dataclass(slots=True)
class RawQdrantMatch:
    payload: dict[str, object] | None
    score: object


class FakeQdrantClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def query_points(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return self.response


@pytest.mark.asyncio
async def test_user_search_client_parses_valid_matches_and_skips_invalid_payloads() -> None:
    meme_id = uuid.uuid4()
    meme_file_id = uuid.uuid4()
    fake_client = FakeQdrantClient(
        SimpleNamespace(
            points=[
                RawQdrantMatch(
                    payload={"meme_id": str(meme_id), "meme_file_id": str(meme_file_id)},
                    score=0.82,
                ),
                RawQdrantMatch(payload={"meme_id": "not-a-uuid", "meme_file_id": str(uuid.uuid4())}, score=0.9),
                RawQdrantMatch(payload={"meme_id": str(uuid.uuid4()), "meme_file_id": str(uuid.uuid4())}, score=True),
                RawQdrantMatch(payload=None, score=0.5),
            ],
        ),
    )
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantUserSearchClient(settings=cast("Settings", settings))
    adapter._client = fake_client

    matches = await adapter.search_memes_by_vector(query_vector=(0.1, 0.2), limit=5)

    assert matches == (
        QdrantUserSearchMatch(
            meme_file_id=meme_file_id,
            meme_id=meme_id,
            semantic_score=0.82,
        ),
    )
    assert fake_client.calls == [
        {
            "collection_name": "memes-test",
            "query": [0.1, 0.2],
            "query_filter": None,
            "limit": 5,
            "with_payload": True,
            "with_vectors": False,
        },
    ]


@pytest.mark.asyncio
async def test_user_search_client_passes_conservative_qdrant_prefilter() -> None:
    fake_client = FakeQdrantClient([])
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantUserSearchClient(settings=cast("Settings", settings))
    adapter._client = fake_client
    prefilter = SearchIndexPrefilter(
        scope=SearchIndexPrefilterScope.ALL,
        search_index_algorithm_version="collection-aware-v1",
        viewer_user_id="viewer-1",
        media_type="image",
        language="en",
        tags=("frog",),
    )

    _ = await adapter.search_memes_by_vector(query_vector=(0.1, 0.2), limit=5, prefilter=prefilter)

    query_filter = fake_client.calls[0]["query_filter"]
    expected_filter = prefilter.to_qdrant_filter()
    assert query_filter is not None
    assert expected_filter is not None
    actual_filter_payload = cast("Any", query_filter).model_dump(exclude_none=True, mode="json")
    expected_filter_payload = cast("Any", expected_filter).model_dump(exclude_none=True, mode="json")
    assert actual_filter_payload == expected_filter_payload


@pytest.mark.asyncio
async def test_similarity_client_uses_query_points_and_filters_self_matches() -> None:
    current_meme_file_id = uuid.uuid4()
    matched_meme_file_id = uuid.uuid4()
    matched_meme_id = uuid.uuid4()
    fake_client = FakeQdrantClient(
        [
            RawQdrantMatch(
                payload={"meme_id": str(uuid.uuid4()), "meme_file_id": str(current_meme_file_id)},
                score=1.0,
            ),
            RawQdrantMatch(
                payload={"meme_id": str(matched_meme_id), "meme_file_id": str(matched_meme_file_id)},
                score=0.91,
            ),
        ],
    )
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_search_top_k=7,
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantClient(settings=cast("Settings", settings))
    adapter._client = fake_client

    matches = await adapter.find_similar_memes(vector=(0.3, 0.4), current_meme_file_id=current_meme_file_id)

    assert matches == (
        QdrantSimilarityMatch(
            meme_file_id=matched_meme_file_id,
            meme_id=matched_meme_id,
            similarity_score=0.91,
        ),
    )
    assert fake_client.calls == [
        {
            "collection_name": "memes-test",
            "query": [0.3, 0.4],
            "limit": 7,
            "with_payload": True,
            "with_vectors": False,
        },
    ]


def test_meme_search_dependency_wires_lazy_text_semantic_and_embedding_clients() -> None:
    service = get_meme_search_service(cast("Any", object()))

    assert isinstance(service, MemeSearchService)
    assert isinstance(service._text_client, PipelineMeilisearchSyncClient)
    assert isinstance(service._semantic_client, PipelineQdrantUserSearchClient)
    assert isinstance(service._query_embedding_client, CachedTextQueryEmbeddingService)
    assert isinstance(service._query_embedding_client._provider, PipelineVoyageClient)
    assert service._query_embedding_client._cache_session_factory is not None
    assert service._text_client._client is None
    assert service._semantic_client._client is None


def test_qdrant_sync_payload_serializer_and_preview_include_collection_metadata() -> None:
    meme_id = uuid.uuid4()
    meme_file_id = uuid.uuid4()
    created_at = datetime(2026, 6, 13, 9, 0, tzinfo=UTC)
    updated_at = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)
    payload = QdrantSyncPayload(
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        search_index_algorithm_version="collection-aware-v1",
        is_public=False,
        author_user_id=str(uuid.uuid4()),
        media_type="image",
        language="en",
        is_nsfw=False,
        tags=["frog", "wizard"],
        seo_page_slug="frog-wizard",
        template_id=str(uuid.uuid4()),
        template_slug="frog-template",
        popularity_score=42.0,
        like_count=7,
        created_at=created_at,
        updated_at=updated_at,
        collection_ids=[str(uuid.uuid4())],
        public_collection_ids=[],
        unlisted_collection_ids=[str(uuid.uuid4())],
        private_collection_ids=[str(uuid.uuid4())],
        shared_collection_ids=[str(uuid.uuid4())],
        collection_owner_user_ids=[str(uuid.uuid4())],
        collection_member_user_ids=[str(uuid.uuid4())],
        ocr_snippet="frog wizard caption",
        quality_score=0.88,
        source_object_key="pipeline/originals/example.jpg",
    )

    point = _build_meme_point(payload=payload, vector=(0.1, 0.2))

    assert point.id == str(meme_file_id)
    assert point.payload["search_index_algorithm_version"] == "collection-aware-v1"
    assert point.payload["is_public"] is False
    assert point.payload["author_user_id"] == payload.author_user_id
    assert point.payload["media_type"] == "image"
    assert point.payload["template_slug"] == "frog-template"
    assert point.payload["popularity_score"] == 42.0
    assert point.payload["like_count"] == 7
    assert point.payload["created_at"] == created_at.isoformat()
    assert point.payload["updated_at"] == updated_at.isoformat()
    assert point.payload["collection_ids"] == payload.collection_ids
    assert point.payload["collection_member_user_ids"] == payload.collection_member_user_ids

    preview = _build_sync_preview(
        [SimpleNamespace(payload={**point.payload, "ignored": "drop-me"})],
        fetched_at=updated_at,
    )

    assert preview is not None
    assert preview.preview_fields["search_index_algorithm_version"] == "collection-aware-v1"
    assert preview.preview_fields["collection_ids"] == payload.collection_ids
    assert preview.preview_fields["collection_owner_user_ids"] == payload.collection_owner_user_ids
    assert "ignored" not in preview.preview_fields
