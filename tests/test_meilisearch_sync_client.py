"""Unit tests for the Meilisearch sync adapter."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from memexpert.core.meilisearch import PipelineMeilisearchSyncClient

if TYPE_CHECKING:
    from memexpert.core.config import Settings


class FakeMeiliIndex:
    def __init__(self) -> None:
        self.get_document_calls: list[str] = []
        self.delete_document_calls: list[str] = []

    async def get_document(self, document_id: str) -> dict[str, object]:
        self.get_document_calls.append(document_id)
        return {"id": document_id, "meme_id": str(uuid.uuid4()), "tags": ["e2e-smoke"]}

    async def delete_document(self, document_id: str) -> None:
        self.delete_document_calls.append(document_id)


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
