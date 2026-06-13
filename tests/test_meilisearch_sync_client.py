"""Unit tests for the Meilisearch sync adapter."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from memexpert.core.meilisearch import (
    MEILISEARCH_FILTERABLE_ATTRIBUTES,
    PipelineMeilisearchDocument,
    PipelineMeilisearchSyncClient,
    _build_document_payload,
    _build_sync_preview,
)
from memexpert.core.search_index_prefilter import SearchIndexPrefilter, SearchIndexPrefilterScope

if TYPE_CHECKING:
    from memexpert.core.config import Settings


class FakeMeiliIndex:
    def __init__(self) -> None:
        self.get_document_calls: list[str] = []
        self.delete_document_calls: list[str] = []
        self.search_calls: list[dict[str, object]] = []
        self.filterable_attribute_updates: list[list[str]] = []

    async def get_document(self, document_id: str) -> dict[str, object]:
        self.get_document_calls.append(document_id)
        return {"id": document_id, "meme_id": str(uuid.uuid4()), "tags": ["e2e-prd"]}

    async def delete_document(self, document_id: str) -> None:
        self.delete_document_calls.append(document_id)

    async def search(self, query: str, *, limit: int = 20, filter: str | None = None) -> dict[str, object]:
        self.search_calls.append({"query": query, "limit": limit, "filter": filter})
        return {"hits": []}

    async def update_filterable_attributes(self, body: list[str], *, compress: bool = False) -> dict[str, object]:
        _ = compress
        self.filterable_attribute_updates.append(list(body))
        return {"taskUid": 1}


class FakeMeiliClient:
    def __init__(self, index: FakeMeiliIndex) -> None:
        self.index = index
        self.get_or_create_calls: list[dict[str, object]] = []

    async def get_or_create_index(self, uid: str, *, primary_key: str) -> FakeMeiliIndex:
        self.get_or_create_calls.append({"uid": uid, "primary_key": primary_key})
        return self.index


def build_client(index: FakeMeiliIndex) -> PipelineMeilisearchSyncClient:
    settings = SimpleNamespace(
        meilisearch_master_key="test-key",
        meilisearch_url="http://meili.test",
        pipeline_meilisearch_index_name="memes-test",
        pipeline_meilisearch_timeout_seconds=1,
    )
    client = PipelineMeilisearchSyncClient(settings=cast("Settings", settings))
    client._index = index
    return client


@pytest.mark.asyncio
async def test_fetch_document_uses_meme_file_uuid_hex_document_id() -> None:
    meme_file_id = uuid.uuid4()
    index = FakeMeiliIndex()
    client = build_client(index)

    preview = await client.fetch_document(meme_file_id)

    assert index.get_document_calls == [meme_file_id.hex]
    assert preview is not None
    assert preview.preview_fields["id"] == meme_file_id.hex


@pytest.mark.asyncio
async def test_delete_document_uses_meme_file_uuid_hex_document_id() -> None:
    meme_file_id = uuid.uuid4()
    index = FakeMeiliIndex()
    client = build_client(index)

    await client.delete_document(meme_file_id)

    assert index.delete_document_calls == [meme_file_id.hex]


@pytest.mark.asyncio
async def test_search_passes_conservative_meilisearch_prefilter_expression() -> None:
    index = FakeMeiliIndex()
    client = build_client(index)
    prefilter = SearchIndexPrefilter(
        scope=SearchIndexPrefilterScope.ALL,
        search_index_algorithm_version="collection-aware-v1",
        viewer_user_id="viewer-1",
        media_type="image",
        language="en",
        tags=("frog",),
    )

    hits = await client.search("frog", limit=3, prefilter=prefilter)

    assert hits == []
    assert index.search_calls == [
        {
            "query": "frog",
            "limit": 3,
            "filter": 'search_index_algorithm_version = "collection-aware-v1" '
            'AND (is_public = true OR (author_user_id = "viewer-1" '
            'OR collection_owner_user_ids = "viewer-1" '
            'OR collection_member_user_ids = "viewer-1")) '
            'AND media_type = "image" AND language = "en" AND is_nsfw = false AND tags = "frog"',
        }
    ]


@pytest.mark.asyncio
async def test_ensure_index_configures_filterable_attributes() -> None:
    index = FakeMeiliIndex()
    client = build_client(index)
    fake_client = FakeMeiliClient(index)
    client._client = fake_client
    client._index = None

    await client.ensure_index()

    assert fake_client.get_or_create_calls == [{"uid": "memes-test", "primary_key": "id"}]
    assert index.filterable_attribute_updates == [list(MEILISEARCH_FILTERABLE_ATTRIBUTES)]


def test_meilisearch_document_serializer_and_preview_include_collection_metadata() -> None:
    meme_id = uuid.uuid4()
    meme_file_id = uuid.uuid4()
    created_at = datetime(2026, 6, 13, 9, 0, tzinfo=UTC)
    updated_at = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)
    document = PipelineMeilisearchDocument(
        id=meme_file_id.hex,
        meme_id=str(meme_id),
        meme_file_id=str(meme_file_id),
        search_index_algorithm_version="collection-aware-v1",
        is_public=True,
        author_user_id=str(uuid.uuid4()),
        media_type="image",
        language="en",
        is_nsfw=False,
        created_at=created_at,
        updated_at=updated_at,
        tags=["frog", "wizard"],
        seo_page_slug="frog-wizard",
        template_id=str(uuid.uuid4()),
        template_slug="frog-template",
        popularity_score=42.0,
        like_count=7,
        collection_ids=[str(uuid.uuid4())],
        public_collection_ids=[str(uuid.uuid4())],
        unlisted_collection_ids=[],
        private_collection_ids=[],
        shared_collection_ids=[str(uuid.uuid4())],
        collection_owner_user_ids=[str(uuid.uuid4())],
        collection_member_user_ids=[str(uuid.uuid4())],
        ocr_text="frog wizard caption",
        quality_score=0.88,
    )

    payload = _build_document_payload(document)

    assert payload["meme_file_id"] == str(meme_file_id)
    assert payload["search_index_algorithm_version"] == "collection-aware-v1"
    assert payload["is_public"] is True
    assert payload["media_type"] == "image"
    assert payload["seo_page_slug"] == "frog-wizard"
    assert payload["template_slug"] == "frog-template"
    assert payload["popularity_score"] == 42.0
    assert payload["like_count"] == 7
    assert payload["created_at"] == created_at.isoformat()
    assert payload["updated_at"] == updated_at.isoformat()
    assert payload["collection_ids"] == document.collection_ids
    assert payload["collection_member_user_ids"] == document.collection_member_user_ids

    preview = _build_sync_preview(
        {**payload, "ignored": "drop-me"},
        fetched_at=updated_at,
    )

    assert preview is not None
    assert preview.preview_fields["search_index_algorithm_version"] == "collection-aware-v1"
    assert preview.preview_fields["collection_ids"] == document.collection_ids
    assert preview.preview_fields["collection_owner_user_ids"] == document.collection_owner_user_ids
    assert "ignored" not in preview.preview_fields


def test_meilisearch_prefilter_escapes_quoted_values() -> None:
    prefilter = SearchIndexPrefilter(
        scope=SearchIndexPrefilterScope.PUBLIC,
        search_index_algorithm_version='collection-"aware"-v1',
        tags=('frog\\wizard"tag',),
    )

    assert prefilter.to_meilisearch_filter() == (
        'search_index_algorithm_version = "collection-\\"aware\\"-v1" '
        'AND is_public = true AND is_nsfw = false AND tags = "frog\\\\wizard\\"tag"'
    )
