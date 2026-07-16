"""Unit tests for published-synonym settings reconciliation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from memexpert.core.meilisearch_settings import MeilisearchSettingsProviderUnavailableError
from memexpert.models.enums import SearchSynonymLocale, SearchSynonymSyncStatus
from memexpert.services.meilisearch_settings_reconcile import (
    MeilisearchSettingsReconciler,
    PublishedSynonymSnapshot,
    hash_canonical_synonym_map,
    run_meilisearch_settings_reconcile,
)
from memexpert.services.search_synonym_compiler import (
    SEARCH_SYNONYM_COMPILER_VERSION,
    compile_search_synonyms,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class FakeSyncRepository:
    def __init__(self, snapshots: tuple[PublishedSynonymSnapshot, ...]) -> None:
        self.snapshots = snapshots
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.no_published_status = SearchSynonymSyncStatus.IDLE
        self.supersede_on_next_task_uid_with: tuple[PublishedSynonymSnapshot, ...] | None = None

    async def load_published_snapshots(self) -> tuple[PublishedSynonymSnapshot, ...]:
        return self.snapshots

    async def record_no_published_revisions(self, **values: Any) -> SearchSynonymSyncStatus:
        self.records.append(("no_published", values))
        return self.no_published_status

    async def record_syncing(self, **values: Any) -> bool:
        self.records.append(("syncing", values))
        return True

    async def record_task_uid(self, task_uid: int, **values: Any) -> bool:
        self.records.append(("task_uid", {"task_uid": task_uid, **values}))
        if self.supersede_on_next_task_uid_with is not None:
            self.snapshots = self.supersede_on_next_task_uid_with
            self.supersede_on_next_task_uid_with = None
            return False
        return True

    async def record_success(self, **values: Any) -> bool:
        self.records.append(("success", values))
        return True

    async def record_failure(self, **values: Any) -> bool:
        self.records.append(("failure", values))
        return True


class FakeSettingsClient:
    def __init__(self, reads: list[object]) -> None:
        self.reads = list(reads)
        self.submissions: list[dict[str, list[str]]] = []
        self.waits: list[int] = []
        self.task_uid = 73

    async def get_synonyms(self) -> dict[str, list[str]]:
        result = self.reads.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, dict)
        return cast("dict[str, list[str]]", result)

    async def submit_synonyms(self, synonyms: Mapping[str, list[str]]) -> int:
        self.submissions.append({key: list(values) for key, values in synonyms.items()})
        return self.task_uid

    async def wait_for_task(self, task_uid: int) -> None:
        self.waits.append(task_uid)


def _snapshot(
    locale: str,
    revision_number: int,
    source_text: str,
    *,
    compiled_synonyms: object | None = None,
    compiler_version: str = SEARCH_SYNONYM_COMPILER_VERSION,
    compiled_hash: str | None = None,
) -> PublishedSynonymSnapshot:
    suffix = 1 if locale == "en" else 2
    compile_result = compile_search_synonyms(source_text, locale=SearchSynonymLocale(locale))
    return PublishedSynonymSnapshot(
        revision_id=uuid.UUID(int=revision_number * 10 + suffix),
        locale=locale,
        revision_number=revision_number,
        source_text=source_text,
        compiled_synonyms=(
            compile_result.compiled_synonyms
            if compiled_synonyms is None
            else compiled_synonyms
        ),
        compiler_version=compiler_version,
        compiled_hash=compiled_hash or compile_result.compiled_hash,
    )


def _compiled(locale: str, source_text: str) -> dict[str, list[str]]:
    return compile_search_synonyms(
        source_text,
        locale=SearchSynonymLocale(locale),
    ).compiled_synonyms


@pytest.mark.asyncio
async def test_reconcile_no_published_revisions_is_idle_and_does_not_clear_provider() -> None:
    repository = FakeSyncRepository(())
    client = FakeSettingsClient([])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.IDLE
    assert result.reason == "no_published_revisions"
    assert result.changed is False
    assert client.submissions == []
    assert client.reads == []
    assert repository.records == [
        (
            "no_published",
            {"expected_snapshot_ids": (), "attempted_at": NOW},
        )
    ]


@pytest.mark.asyncio
async def test_reconcile_canonical_noop_records_verified_in_sync_state() -> None:
    snapshots = (
        _snapshot("ru", 2, "лягушка,жаба"),
        _snapshot("en", 4, "frog,toad,frog meme"),
    )
    expected_map = {
        "frog": ["frog meme", "toad"],
        "frog meme": ["frog", "toad"],
        "toad": ["frog", "frog meme"],
        "жаба": ["лягушка"],
        "лягушка": ["жаба"],
    }
    repository = FakeSyncRepository(snapshots)
    client = FakeSettingsClient([expected_map])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.SYNCED
    assert result.reason == "in_sync"
    assert result.changed is False
    assert result.desired_hash == hash_canonical_synonym_map(expected_map)
    assert client.submissions == []
    assert [name for name, _ in repository.records] == ["success"]
    success = repository.records[0][1]
    assert success["desired_hash"] == success["actual_hash"]
    assert success["task_uid"] is None
    assert success["provider_applied"] is False
    assert success["desired_revision_ids"] == {
        "en": str(uuid.UUID(int=41)),
        "ru": str(uuid.UUID(int=22)),
    }


@pytest.mark.asyncio
async def test_reconcile_applies_full_replacement_waits_and_verifies() -> None:
    desired = {"frog": ["toad"], "toad": ["frog"]}
    repository = FakeSyncRepository((_snapshot("en", 1, "frog,toad"),))
    client = FakeSettingsClient([{"old": ["value"]}, desired])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.SYNCED
    assert result.reason == "applied"
    assert result.changed is True
    assert result.provider_task_uid == 73
    assert client.submissions == [desired]
    assert client.waits == [73]
    assert [name for name, _ in repository.records] == ["syncing", "task_uid", "success"]
    assert repository.records[-1][1]["actual_hash"] == hash_canonical_synonym_map(desired)
    assert repository.records[-1][1]["task_uid"] == 73
    assert repository.records[-1][1]["provider_applied"] is True


@pytest.mark.asyncio
async def test_reconcile_provider_failure_records_safe_failure_without_replacement() -> None:
    repository = FakeSyncRepository((_snapshot("en", 1, "frog,toad"),))
    client = FakeSettingsClient(
        [MeilisearchSettingsProviderUnavailableError("Meilisearch get_synonyms failed.")],
    )

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "provider_read_failed"
    assert client.submissions == []
    assert [name for name, _ in repository.records] == ["failure"]
    failure = repository.records[0][1]
    assert failure["safe_error"] == "Meilisearch get_synonyms failed."
    assert failure["actual_hash"] is None


@pytest.mark.asyncio
async def test_reconcile_records_failure_when_post_task_verification_does_not_match() -> None:
    desired = {"frog": ["toad"], "toad": ["frog"]}
    verified = {"frog": ["newt"]}
    repository = FakeSyncRepository((_snapshot("en", 1, "frog,toad"),))
    client = FakeSettingsClient([{"old": ["value"]}, verified])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "provider_apply_failed"
    assert result.provider_task_uid == 73
    assert client.submissions == [desired]
    assert client.waits == [73]
    assert [name for name, _ in repository.records] == ["syncing", "task_uid", "failure"]
    failure = repository.records[-1][1]
    assert failure["actual_hash"] == hash_canonical_synonym_map(verified)
    assert failure["task_uid"] == 73
    assert failure["safe_error"] == (
        "Meilisearch synonym verification did not match the published snapshot."
    )


@pytest.mark.asyncio
async def test_reconcile_rejects_cross_locale_conflicting_keys_before_provider_call() -> None:
    repository = FakeSyncRepository(
        (
            _snapshot("en", 1, "123,456"),
            _snapshot("ru", 1, "123,789"),
        )
    )
    client = FakeSettingsClient([])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "invalid_published_snapshots"
    assert client.submissions == []
    assert client.reads == []
    assert [name for name, _ in repository.records] == ["failure"]
    failure = repository.records[0][1]
    assert failure["safe_error"] == "Published synonym snapshots contain a conflicting duplicate key."
    assert "123" not in failure["safe_error"]


@pytest.mark.asyncio
async def test_reconcile_rejects_empty_published_map_instead_of_clearing_provider() -> None:
    repository = FakeSyncRepository((_snapshot("en", 1, ""),))
    client = FakeSettingsClient([])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "invalid_published_snapshots"
    assert client.submissions == []
    assert client.reads == []
    assert repository.records[0][0] == "failure"
    assert "refusing to clear provider settings" in repository.records[0][1]["safe_error"]


@pytest.mark.asyncio
async def test_reconcile_rejects_one_empty_locale_even_when_another_locale_is_nonempty() -> None:
    repository = FakeSyncRepository(
        (
            _snapshot("en", 1, ""),
            _snapshot("ru", 1, "жаба,лягушка"),
        )
    )
    client = FakeSettingsClient([])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "invalid_published_snapshots"
    assert client.submissions == []
    assert client.reads == []
    assert "published locale synonym snapshot is empty" in repository.records[0][1][
        "safe_error"
    ].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot("en", 1, "frog,toad", compiled_hash="0" * 64),
        _snapshot("en", 1, "frog,toad", compiler_version="unknown_compiler"),
        _snapshot("en", 1, "frog,toad", compiled_synonyms={"frog": ["newt"]}),
    ],
)
async def test_reconcile_rejects_published_snapshot_integrity_failures(
    snapshot: PublishedSynonymSnapshot,
) -> None:
    repository = FakeSyncRepository((snapshot,))
    client = FakeSettingsClient([])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "invalid_published_snapshots"
    assert client.submissions == []
    assert client.reads == []


@pytest.mark.asyncio
async def test_reconcile_reports_missing_publications_after_a_prior_application() -> None:
    repository = FakeSyncRepository(())
    repository.no_published_status = SearchSynonymSyncStatus.FAILED
    client = FakeSettingsClient([])

    result = await MeilisearchSettingsReconciler(repository, client=client).run(now=NOW)

    assert result.status is SearchSynonymSyncStatus.FAILED
    assert result.reason == "published_revisions_missing_after_apply"
    assert client.reads == []
    assert client.submissions == []


@pytest.mark.asyncio
async def test_reconcile_immediately_applies_a_publication_that_supersedes_an_inflight_task() -> None:
    first = _snapshot("en", 1, "frog,toad")
    second = _snapshot("en", 2, "frog,newt")
    first_map = _compiled("en", first.source_text)
    second_map = _compiled("en", second.source_text)
    repository = FakeSyncRepository((first,))
    repository.supersede_on_next_task_uid_with = (second,)
    client = FakeSettingsClient(
        [
            {"old": ["value"]},
            first_map,
            first_map,
            second_map,
        ]
    )

    result = await MeilisearchSettingsReconciler(repository, client=client).run_until_current(
        now=NOW
    )

    assert result.status is SearchSynonymSyncStatus.SYNCED
    assert result.reason == "applied"
    assert result.desired_hash == hash_canonical_synonym_map(second_map)
    assert client.submissions == [first_map, second_map]
    assert client.waits == [73, 73]
    successes = [values for name, values in repository.records if name == "success"]
    assert len(successes) == 1
    assert successes[0]["desired_revision_ids"] == {"en": str(second.revision_id)}


@pytest.mark.asyncio
async def test_production_runner_closes_its_owned_meilisearch_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeSyncRepository(())
    created_clients: list[OwnedFakeSettingsClient] = []

    class OwnedFakeSettingsClient(FakeSettingsClient):
        def __init__(self, *, settings: object) -> None:
            del settings
            super().__init__([])
            self.closed = False
            created_clients.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "memexpert.services.meilisearch_settings_reconcile.SqlAlchemySearchSynonymSyncRepository",
        lambda _session_factory: repository,
    )
    monkeypatch.setattr(
        "memexpert.services.meilisearch_settings_reconcile.MeilisearchSettingsClient",
        OwnedFakeSettingsClient,
    )

    result = await run_meilisearch_settings_reconcile(
        cast("Any", object()),
        settings=cast("Any", object()),
        now=NOW,
    )

    assert result.status is SearchSynonymSyncStatus.IDLE
    assert len(created_clients) == 1
    assert created_clients[0].closed is True
