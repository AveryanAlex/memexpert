"""Unit tests for the Meilisearch synonym-settings adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from memexpert.core.meilisearch_settings import (
    MeilisearchSettingsClient,
    MeilisearchSettingsMalformedResponseError,
    MeilisearchSettingsProviderUnavailableError,
    MeilisearchSettingsRejectedError,
)

if TYPE_CHECKING:
    from memexpert.core.config import Settings


class FakeSettingsIndex:
    def __init__(self) -> None:
        self.synonyms: object = {"frog": ["toad"]}
        self.get_error: BaseException | None = None
        self.update_error: BaseException | None = None
        self.updates: list[dict[str, list[str]]] = []

    async def get_synonyms(self) -> object:
        if self.get_error is not None:
            raise self.get_error
        return self.synonyms

    async def update_synonyms(self, body: dict[str, list[str]]) -> dict[str, int]:
        if self.update_error is not None:
            raise self.update_error
        self.updates.append(body)
        return {"taskUid": 42}


class FakeSettingsSdkClient:
    def __init__(self, index: FakeSettingsIndex) -> None:
        self._index = index
        self.get_or_create_calls: list[dict[str, str]] = []
        self.wait_calls: list[dict[str, object]] = []
        self.wait_result: object = {"status": "succeeded"}
        self.closed = False

    async def get_or_create_index(self, uid: str, *, primary_key: str) -> FakeSettingsIndex:
        self.get_or_create_calls.append({"uid": uid, "primary_key": primary_key})
        return self._index

    async def wait_for_task(
        self,
        task_uid: int,
        *,
        timeout_in_ms: int,
        raise_for_status: bool,
    ) -> object:
        self.wait_calls.append(
            {
                "task_uid": task_uid,
                "timeout_in_ms": timeout_in_ms,
                "raise_for_status": raise_for_status,
            }
        )
        return self.wait_result

    async def aclose(self) -> None:
        self.closed = True


def _build_client(index: FakeSettingsIndex) -> tuple[MeilisearchSettingsClient, FakeSettingsSdkClient]:
    settings = SimpleNamespace(
        meilisearch_master_key="test-key",
        meilisearch_url="http://meili.test",
        pipeline_meilisearch_index_name="memes-test",
        pipeline_meilisearch_timeout_seconds=1.0,
        meilisearch_settings_task_timeout_seconds=600.0,
    )
    client = MeilisearchSettingsClient(settings=cast("Settings", settings))
    sdk_client = FakeSettingsSdkClient(index)
    client._client = sdk_client
    return client, sdk_client


@pytest.mark.asyncio
async def test_settings_adapter_reads_submits_and_waits_with_settings_timeout() -> None:
    index = FakeSettingsIndex()
    client, sdk_client = _build_client(index)

    current = await client.get_synonyms()
    task_uid = await client.submit_synonyms({"toad": ["frog"], "frog": ["toad"]})
    await client.wait_for_task(task_uid)

    assert current == {"frog": ["toad"]}
    assert sdk_client.get_or_create_calls == [{"uid": "memes-test", "primary_key": "id"}]
    assert index.updates == [{"toad": ["frog"], "frog": ["toad"]}]
    assert sdk_client.wait_calls == [
        {"task_uid": 42, "timeout_in_ms": 600_000, "raise_for_status": False}
    ]


@pytest.mark.asyncio
async def test_settings_adapter_rejects_malformed_synonym_payload() -> None:
    index = FakeSettingsIndex()
    index.synonyms = {"frog": "toad"}
    client, _ = _build_client(index)

    with pytest.raises(MeilisearchSettingsMalformedResponseError, match="malformed"):
        await client.get_synonyms()


@pytest.mark.asyncio
async def test_settings_adapter_treats_empty_provider_payload_as_empty_map() -> None:
    index = FakeSettingsIndex()
    index.synonyms = None
    client, _ = _build_client(index)

    assert await client.get_synonyms() == {}


@pytest.mark.asyncio
async def test_settings_adapter_refuses_to_submit_an_empty_map() -> None:
    index = FakeSettingsIndex()
    client, sdk_client = _build_client(index)

    with pytest.raises(MeilisearchSettingsRejectedError, match="empty"):
        await client.submit_synonyms({})

    assert sdk_client.get_or_create_calls == []
    assert index.updates == []


@pytest.mark.asyncio
async def test_settings_adapter_closes_and_releases_cached_sdk_state() -> None:
    index = FakeSettingsIndex()
    client, sdk_client = _build_client(index)

    _ = await client.get_synonyms()
    await client.aclose()

    assert sdk_client.closed is True
    assert client._client is None
    assert client._index is None


@pytest.mark.asyncio
async def test_settings_adapter_reports_failed_provider_task_without_provider_error_details() -> None:
    index = FakeSettingsIndex()
    client, sdk_client = _build_client(index)
    sdk_client.wait_result = {
        "status": "failed",
        "error": {"message": "payload contained private-synonym-value"},
    }

    with pytest.raises(MeilisearchSettingsRejectedError) as exc_info:
        await client.wait_for_task(42)

    assert "private-synonym-value" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_settings_adapter_sanitizes_unexpected_provider_exception() -> None:
    index = FakeSettingsIndex()
    index.get_error = RuntimeError("private-synonym-value")
    client, _ = _build_client(index)

    with pytest.raises(MeilisearchSettingsProviderUnavailableError) as exc_info:
        await client.get_synonyms()

    assert "private-synonym-value" not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)
