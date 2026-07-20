"""Unit tests for the user-facing Qdrant semantic search adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

import memexpert.core.qdrant as qdrant_module
from memexpert.api.dependencies.meme import get_meme_search_service
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import (
    PipelineQdrantClient,
    PipelineQdrantRecommendationClient,
    PipelineQdrantSyncClient,
    PipelineQdrantUserSearchClient,
    QdrantNearestSourceQuery,
    QdrantRecommendationMatch,
    QdrantRecommendationSourceResult,
    QdrantRecommendSourceQuery,
    QdrantSimilarityMatch,
    QdrantSyncPayload,
    QdrantUserSearchMatch,
    _build_meme_point,
    _build_sync_preview,
)
from memexpert.core.search_index_prefilter import SearchIndexPrefilter, SearchIndexPrefilterScope
from memexpert.core.voyage import PipelineVoyageClient
from memexpert.ingest.policy import ApproximateMergeScope
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


class FakeQdrantNotFoundError(Exception):
    status_code = 404


class FakeBootstrapQdrantClient:
    def __init__(self, *, collection_exists: bool = False) -> None:
        self.collection_exists_result = collection_exists
        self.collection_exists_calls: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.payload_index_calls: list[dict[str, Any]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.fail_next_upsert_not_found = False

    async def collection_exists(self, collection_name: str) -> bool:
        self.collection_exists_calls.append(collection_name)
        return self.collection_exists_result

    async def upsert(self, **kwargs: Any) -> bool:
        self.upsert_calls.append(kwargs)
        if self.fail_next_upsert_not_found:
            self.fail_next_upsert_not_found = False
            self.collection_exists_result = False
            raise FakeQdrantNotFoundError
        return True

    async def create_collection(self, **kwargs: Any) -> bool:
        self.create_calls.append(kwargs)
        self.collection_exists_result = True
        return True

    async def create_payload_index(self, **kwargs: Any) -> bool:
        self.payload_index_calls.append(kwargs)
        return True


class FakeBatchQdrantClient:
    def __init__(self, responses_by_call: list[object]) -> None:
        self.responses_by_call = responses_by_call
        self.calls: list[dict[str, Any]] = []

    async def query_batch_points(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        response = self.responses_by_call[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


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


def test_private_qdrant_prefilter_excludes_public_collection_saves() -> None:
    prefilter = SearchIndexPrefilter(
        scope=SearchIndexPrefilterScope.PRIVATE,
        search_index_algorithm_version="collection-aware-v1",
        viewer_user_id="viewer-1",
    )

    query_filter = prefilter.to_qdrant_filter()
    assert query_filter is not None
    payload = cast("Any", query_filter).model_dump(exclude_none=True, mode="json")
    private_access = payload["must"][1]
    assert private_access["must"][0] == {"key": "is_public", "match": {"value": False}}
    assert {condition["key"] for condition in private_access["must"][1]["should"]} == {
        "collection_owner_user_ids",
        "collection_member_user_ids",
    }


@pytest.mark.asyncio
async def test_recommendation_client_isolates_nearest_and_best_score_batches_with_primary_filter() -> None:
    nearest_meme_id = uuid.uuid4()
    nearest_file_id = uuid.uuid4()
    recommended_meme_id = uuid.uuid4()
    recommended_file_id = uuid.uuid4()
    positive_file_ids = (uuid.uuid4(), uuid.uuid4())
    explicitly_excluded_file_id = uuid.uuid4()
    raw_client = FakeBatchQdrantClient(
        [
            [
                SimpleNamespace(
                    points=[
                        RawQdrantMatch(
                            payload={"meme_id": str(nearest_meme_id), "meme_file_id": str(nearest_file_id)},
                            score=0.91,
                        )
                    ]
                ),
            ],
            [
                SimpleNamespace(
                    points=[
                        RawQdrantMatch(
                            payload={
                                "meme_id": str(recommended_meme_id),
                                "meme_file_id": str(recommended_file_id),
                            },
                            score=1.4,
                        )
                    ]
                ),
            ],
        ]
    )
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantRecommendationClient(settings=cast("Settings", settings))
    adapter._client = raw_client
    prefilter = SearchIndexPrefilter(
        scope=SearchIndexPrefilterScope.PUBLIC,
        search_index_algorithm_version="collection-aware-v1",
        language="en",
        include_nsfw=False,
    )

    results = await adapter.query_recommendation_sources(
        nearest_queries=(QdrantNearestSourceQuery(source="current_intent", vector=(0.2, 0.8), limit=12),),
        recommend_queries=(
            QdrantRecommendSourceQuery(
                source="recent_positives",
                positive_meme_file_ids=positive_file_ids,
                limit=7,
            ),
        ),
        prefilter=prefilter,
        excluded_meme_file_ids=(explicitly_excluded_file_id,),
    )

    assert results == (
        QdrantRecommendationSourceResult(
            source="current_intent",
            matches=(
                QdrantRecommendationMatch(
                    meme_file_id=nearest_file_id,
                    meme_id=nearest_meme_id,
                    candidate_score=0.91,
                ),
            ),
        ),
        QdrantRecommendationSourceResult(
            source="recent_positives",
            matches=(
                QdrantRecommendationMatch(
                    meme_file_id=recommended_file_id,
                    meme_id=recommended_meme_id,
                    candidate_score=1.4,
                ),
            ),
        ),
    )
    assert len(raw_client.calls) == 2
    nearest_request = raw_client.calls[0]["requests"][0]
    recommend_request = raw_client.calls[1]["requests"][0]
    assert nearest_request.query == [0.2, 0.8]
    assert nearest_request.limit == 12
    assert recommend_request.query.recommend.strategy.value == "best_score"
    assert recommend_request.query.recommend.positive == [str(meme_file_id) for meme_file_id in positive_file_ids]
    assert recommend_request.limit == 7

    filter_payload = nearest_request.filter.model_dump(exclude_none=True, mode="json")
    assert {condition.get("key") for condition in filter_payload["must"] if "key" in condition} == {"is_primary_file"}
    assert filter_payload["must"][-1] == {"key": "is_primary_file", "match": {"value": True}}
    assert set(filter_payload["must_not"][0]["has_id"]) == {
        str(explicitly_excluded_file_id),
        *(str(meme_file_id) for meme_file_id in positive_file_ids),
    }
    assert recommend_request.filter == nearest_request.filter


@pytest.mark.asyncio
async def test_recommendation_client_preserves_nearest_results_when_positive_seed_is_absent() -> None:
    nearest_meme_id = uuid.uuid4()
    nearest_file_id = uuid.uuid4()
    raw_client = FakeBatchQdrantClient(
        [
            [
                SimpleNamespace(
                    points=[
                        RawQdrantMatch(
                            payload={"meme_id": str(nearest_meme_id), "meme_file_id": str(nearest_file_id)},
                            score=0.88,
                        )
                    ]
                )
            ],
            RuntimeError("positive point does not exist"),
        ]
    )
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantRecommendationClient(settings=cast("Settings", settings))
    adapter._client = raw_client

    results = await adapter.query_recommendation_sources(
        nearest_queries=(QdrantNearestSourceQuery(source="short_term", vector=(1.0, 0.0), limit=5),),
        recommend_queries=(
            QdrantRecommendSourceQuery(
                source="recent_positives",
                positive_meme_file_ids=(uuid.uuid4(),),
                limit=5,
            ),
        ),
    )

    assert results == (
        QdrantRecommendationSourceResult(
            source="short_term",
            matches=(
                QdrantRecommendationMatch(
                    meme_file_id=nearest_file_id,
                    meme_id=nearest_meme_id,
                    candidate_score=0.88,
                ),
            ),
        ),
        QdrantRecommendationSourceResult(source="recent_positives", matches=()),
    )
    assert len(raw_client.calls) == 2


@pytest.mark.asyncio
async def test_qdrant_adapters_share_and_close_the_process_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class ClosableQdrantClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    raw_client = ClosableQdrantClient()
    monkeypatch.setattr(qdrant_module, "_async_qdrant_client", raw_client)
    first = PipelineQdrantUserSearchClient()
    second = PipelineQdrantRecommendationClient()

    assert await first._ensure_client() is raw_client
    assert await second._ensure_client() is raw_client
    assert qdrant_module.is_async_qdrant_initialized() is True

    await qdrant_module.reset_async_qdrant_state()

    assert raw_client.close_calls == 1
    assert qdrant_module.is_async_qdrant_initialized() is False


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
            "query_filter": None,
            "limit": 7,
            "with_payload": True,
            "with_vectors": False,
        },
    ]


@pytest.mark.asyncio
async def test_similarity_client_treats_missing_first_collection_as_no_matches() -> None:
    class MissingCollectionClient:
        async def query_points(self, **kwargs: Any) -> object:
            _ = kwargs
            raise FakeQdrantNotFoundError("collection missing")

    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_search_top_k=7,
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantClient(settings=cast("Settings", settings))
    adapter._client = MissingCollectionClient()

    assert await adapter.find_similar_memes(vector=(0.3, 0.4), current_meme_file_id=uuid.uuid4()) == ()


@pytest.mark.asyncio
async def test_similarity_client_filters_private_candidates_to_one_matching_uploader() -> None:
    fake_client = FakeQdrantClient([])
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_search_top_k=7,
        pipeline_qdrant_timeout_seconds=1,
        qdrant_url="http://qdrant.test",
    )
    adapter = PipelineQdrantClient(settings=cast("Settings", settings))
    adapter._client = fake_client
    uploader_user_id = uuid.uuid4()

    _ = await adapter.find_similar_memes(
        vector=(0.3, 0.4),
        current_meme_file_id=uuid.uuid4(),
        scope=ApproximateMergeScope(is_public=False, uploader_user_id=uploader_user_id),
    )

    query_filter = fake_client.calls[0]["query_filter"]
    payload = query_filter.model_dump(exclude_none=True, mode="json")
    assert payload == {
        "must": [
            {"key": "is_public", "match": {"value": False}},
            {"key": "uploader_user_ids", "match": {"value": str(uploader_user_id)}},
            {"key": "uploader_user_ids", "values_count": {"gte": 1, "lte": 1}},
        ]
    }


@pytest.mark.asyncio
async def test_sync_client_creates_missing_collection_and_provisions_filter_indexes_once() -> None:
    now = datetime.now(UTC)
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        pipeline_voyage_output_dimensions=4,
        qdrant_url="http://qdrant.test",
    )
    raw_client = FakeBootstrapQdrantClient()
    adapter = PipelineQdrantSyncClient(settings=cast("Settings", settings))
    adapter._client = raw_client
    payload = QdrantSyncPayload(
        meme_id=uuid.uuid4(),
        meme_file_id=uuid.uuid4(),
        search_index_algorithm_version="test-v1",
        is_public=True,
        is_primary_file=True,
        media_type="image",
        language="en",
        is_nsfw=False,
        seo_page_slug=None,
        template_id=None,
        template_slug=None,
        popularity_score=0.0,
        like_count=0,
        created_at=now,
        updated_at=now,
    )

    await adapter.upsert_meme_point(payload, (0.1, 0.2, 0.3, 0.4))
    await adapter.upsert_meme_point(payload, (0.1, 0.2, 0.3, 0.4))

    assert raw_client.collection_exists_calls == ["memes-test"]
    assert len(raw_client.upsert_calls) == 2
    assert len(raw_client.create_calls) == 1
    assert raw_client.create_calls[0]["collection_name"] == "memes-test"
    vectors_config = raw_client.create_calls[0]["vectors_config"]
    assert vectors_config.size == 4
    assert {(call["field_name"], call["field_schema"].value) for call in raw_client.payload_index_calls} == {
        ("search_index_algorithm_version", "keyword"),
        ("is_public", "bool"),
        ("is_primary_file", "bool"),
        ("uploader_user_ids", "keyword"),
        ("media_type", "keyword"),
        ("language", "keyword"),
        ("is_nsfw", "bool"),
        ("tags", "keyword"),
        ("collection_ids", "keyword"),
        ("collection_owner_user_ids", "keyword"),
        ("collection_member_user_ids", "keyword"),
    }


@pytest.mark.asyncio
async def test_sync_client_provisions_indexes_for_an_existing_collection() -> None:
    now = datetime.now(UTC)
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        pipeline_voyage_output_dimensions=2,
        qdrant_url="http://qdrant.test",
    )
    raw_client = FakeBootstrapQdrantClient(collection_exists=True)
    adapter = PipelineQdrantSyncClient(settings=cast("Settings", settings))
    adapter._client = raw_client
    payload = QdrantSyncPayload(
        meme_id=uuid.uuid4(),
        meme_file_id=uuid.uuid4(),
        search_index_algorithm_version="test-v1",
        is_public=True,
        is_primary_file=True,
        media_type="image",
        language="en",
        is_nsfw=False,
        seo_page_slug=None,
        template_id=None,
        template_slug=None,
        popularity_score=0.0,
        like_count=0,
        created_at=now,
        updated_at=now,
    )

    await adapter.upsert_meme_point(payload, (0.1, 0.2))

    assert raw_client.collection_exists_calls == ["memes-test"]
    assert raw_client.create_calls == []
    assert len(raw_client.payload_index_calls) == 11
    assert len(raw_client.upsert_calls) == 1


@pytest.mark.asyncio
async def test_sync_client_reprovisions_once_when_cached_collection_was_replaced() -> None:
    now = datetime.now(UTC)
    settings = SimpleNamespace(
        pipeline_qdrant_collection_name="memes-test",
        pipeline_qdrant_timeout_seconds=1,
        pipeline_voyage_output_dimensions=2,
        qdrant_url="http://qdrant.test",
    )
    raw_client = FakeBootstrapQdrantClient()
    adapter = PipelineQdrantSyncClient(settings=cast("Settings", settings))
    adapter._client = raw_client
    payload = QdrantSyncPayload(
        meme_id=uuid.uuid4(),
        meme_file_id=uuid.uuid4(),
        search_index_algorithm_version="test-v1",
        is_public=True,
        is_primary_file=True,
        media_type="image",
        language="en",
        is_nsfw=False,
        seo_page_slug=None,
        template_id=None,
        template_slug=None,
        popularity_score=0.0,
        like_count=0,
        created_at=now,
        updated_at=now,
    )

    await adapter.upsert_meme_point(payload, (0.1, 0.2))
    raw_client.fail_next_upsert_not_found = True
    await adapter.upsert_meme_point(payload, (0.1, 0.2))

    assert raw_client.collection_exists_calls == ["memes-test", "memes-test"]
    assert len(raw_client.create_calls) == 2
    assert len(raw_client.payload_index_calls) == 22
    assert len(raw_client.upsert_calls) == 3


def test_meme_search_dependency_wires_lazy_text_semantic_and_embedding_clients() -> None:
    service = get_meme_search_service(cast("Any", object()))

    assert isinstance(service, MemeSearchService)
    assert isinstance(service._text_client, PipelineMeilisearchSyncClient)
    assert isinstance(service._semantic_client, PipelineQdrantUserSearchClient)
    assert isinstance(service._similarity_client, PipelineQdrantClient)
    assert isinstance(service._query_embedding_client, CachedTextQueryEmbeddingService)
    assert isinstance(service._query_embedding_client._provider, PipelineVoyageClient)
    assert service._query_embedding_client._cache_session_factory is not None
    assert service._text_client._client is None
    assert service._semantic_client._client is None
    assert service._similarity_client._client is None


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
        is_primary_file=True,
        uploader_user_ids=[str(uuid.uuid4())],
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
    assert point.payload["is_primary_file"] is True
    assert point.payload["uploader_user_ids"] == payload.uploader_user_ids
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
    assert preview.preview_fields["is_primary_file"] is True
    assert preview.preview_fields["collection_ids"] == payload.collection_ids
    assert preview.preview_fields["collection_owner_user_ids"] == payload.collection_owner_user_ids
    assert "ignored" not in preview.preview_fields
