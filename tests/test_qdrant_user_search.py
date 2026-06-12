"""Unit tests for the user-facing Qdrant semantic search adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from memexpert.api.dependencies.meme import get_meme_search_service
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import (
    PipelineQdrantClient,
    PipelineQdrantUserSearchClient,
    QdrantSimilarityMatch,
    QdrantUserSearchMatch,
)
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
            "limit": 5,
            "with_payload": True,
            "with_vectors": False,
        },
    ]


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
