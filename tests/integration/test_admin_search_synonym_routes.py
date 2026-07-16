# ruff: noqa: TC001,TC002,TC003
"""Admin route coverage for versioned synonym drafts and publishing."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.base import utcnow
from memexpert.models.enums import SearchSynonymLocale, SearchSynonymRevisionStatus
from memexpert.models.operations import OperationalAuditLog
from memexpert.models.search_synonyms import (
    SearchSynonymCatalog,
    SearchSynonymRevision,
    SearchSynonymSyncState,
)
from memexpert.services.admin_search_synonyms import SEARCH_SYNONYM_SYNC_STATE_ID
from memexpert.services.meilisearch_settings_reconcile import (
    SqlAlchemySearchSynonymSyncRepository,
    hash_canonical_synonym_map,
)
from memexpert.services.search_synonym_compiler import compile_search_synonyms
from tests.integration.test_admin_routes import _issue_user_cookie
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME

if TYPE_CHECKING:
    from fastapi import FastAPI


async def _seed_synonym_control_plane(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        for locale in SearchSynonymLocale:
            result = compile_search_synonyms("", locale=locale)
            catalog = SearchSynonymCatalog(locale=locale)
            session.add(catalog)
            await session.flush()
            session.add(
                SearchSynonymRevision(
                    catalog_id=catalog.id,
                    revision_number=1,
                    status=SearchSynonymRevisionStatus.DRAFT,
                    source_text="",
                    compiled_synonyms=result.compiled_synonyms,
                    compiler_version=result.compiler_version,
                    compiled_hash=result.compiled_hash,
                    validation=result.validation,
                    stats={key: value for key, value in result.stats.items()},
                    change_note=f"Initial empty {locale.value.upper()} draft.",
                    version=1,
                )
            )
        session.add(SearchSynonymSyncState(id=SEARCH_SYNONYM_SYNC_STATE_ID))
        await session.commit()


async def _admin_client(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    settings: dict[str, str],
    *,
    email: str,
) -> AsyncClient:
    token = await _issue_user_cookie(
        session_factory,
        settings,
        email=email,
        is_admin=True,
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
    client.cookies.set(ACCESS_COOKIE_NAME, token)
    return client


async def test_admin_can_save_publish_retry_restore_and_confirm_destructive_reduction(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_synonym_control_plane(postgres_session_factory)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous:
        assert (await anonymous.get("/api/v1/admin/search-synonyms/ru")).status_code == 401

    async with await _admin_client(
        auth_app,
        postgres_session_factory,
        auth_settings_overrides,
        email="synonym-lifecycle-admin@example.com",
    ) as client:
        initial = await client.get("/api/v1/admin/search-synonyms/ru")
        assert initial.status_code == 200
        assert initial.json()["draft"]["validation"]["valid"] is False
        assert initial.json()["draft"]["version"] == "1"

        first_source = "жаба,лягушка\nкот,кошка\n"
        saved = await client.put(
            "/api/v1/admin/search-synonyms/ru/draft",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "1",
                "source_text": first_source,
                "reason": "Add high-value Russian lexical aliases.",
            },
        )
        assert saved.status_code == 200
        saved_body = saved.json()
        assert saved_body["draft"]["validation"] == {
            "valid": True,
            "group_count": 2,
            "compiled_key_count": 4,
            "edge_count": 4,
            "payload_bytes": saved_body["draft"]["validation"]["payload_bytes"],
            "issues": [],
        }
        assert saved_body["draft"]["compiler_version"] == "meili_synonyms_v1"
        assert saved_body["draft"]["version"] == "2"

        stale = await client.put(
            "/api/v1/admin/search-synonyms/ru/draft",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "1",
                "source_text": first_source,
                "reason": "Attempt a stale overwrite.",
            },
        )
        assert stale.status_code == 409

        published = await client.post(
            "/api/v1/admin/search-synonyms/ru/draft/publish",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "2",
                "reason": "Publish the reviewed Russian lexical aliases.",
                "confirm_destructive": False,
            },
        )
        assert published.status_code == 200
        published_body = published.json()
        assert published_body["published"]["revision_number"] == 1
        assert published_body["published"]["source_text"] == first_source
        assert published_body["draft"]["revision_number"] == 2
        assert published_body["draft"]["version"] == "1"
        assert published_body["history"] == []

        sync = await client.get("/api/v1/admin/search-synonyms/sync")
        assert sync.status_code == 200
        sync_body = sync.json()
        assert sync_body["index_name"] == "memexpert-memes"
        assert sync_body["status"] == "pending"
        assert sync_body["desired_revisions"] == {"ru": 1}
        assert sync_body["version"] == "2"

        async with postgres_session_factory() as session:
            sync_state = await session.get(SearchSynonymSyncState, SEARCH_SYNONYM_SYNC_STATE_ID)
            assert sync_state is not None
            sync_state.version += 5
            await session.commit()

        retried = await client.post(
            "/api/v1/admin/search-synonyms/sync/retry",
            json={
                "request_id": str(uuid.uuid7()),
                "version": sync_body["version"],
                "reason": "Retry after checking Meilisearch availability.",
            },
        )
        assert retried.status_code == 202
        assert retried.json()["status"] == "pending"
        assert retried.json()["version"] == "8"

        reduced = await client.put(
            "/api/v1/admin/search-synonyms/ru/draft",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "1",
                "source_text": "жаба,лягушка\n",
                "reason": "Remove the lower-value cat alias group.",
            },
        )
        assert reduced.status_code == 200
        reduced_version = reduced.json()["draft"]["version"]

        unconfirmed = await client.post(
            "/api/v1/admin/search-synonyms/ru/draft/publish",
            json={
                "request_id": str(uuid.uuid7()),
                "version": reduced_version,
                "reason": "Publish the intentionally smaller Russian catalog.",
                "confirm_destructive": False,
            },
        )
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["detail"]["previous_key_count"] == 4
        assert unconfirmed.json()["detail"]["new_key_count"] == 2

        confirmed = await client.post(
            "/api/v1/admin/search-synonyms/ru/draft/publish",
            json={
                "request_id": str(uuid.uuid7()),
                "version": reduced_version,
                "reason": "Publish the intentionally smaller Russian catalog.",
                "confirm_destructive": True,
            },
        )
        assert confirmed.status_code == 200
        confirmed_body = confirmed.json()
        assert confirmed_body["published"]["revision_number"] == 2
        assert [revision["revision_number"] for revision in confirmed_body["history"]] == [1]

        restored = await client.post(
            "/api/v1/admin/search-synonyms/ru/draft/reset",
            json={
                "request_id": str(uuid.uuid7()),
                "version": confirmed_body["draft"]["version"],
                "reason": "Restore the first published revision for review.",
                "revision_id": confirmed_body["history"][0]["id"],
            },
        )
        assert restored.status_code == 200
        assert restored.json()["draft"]["source_text"] == first_source

    async with postgres_session_factory() as session:
        actions = set((await session.execute(select(OperationalAuditLog.action))).scalars())
    assert {
        "publish_search_synonym_revision",
        "reset_search_synonym_draft",
        "retry_search_synonym_sync",
        "save_search_synonym_draft",
    }.issubset(actions)


async def test_admin_seed_import_keeps_inactive_long_groups_as_warnings(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_synonym_control_plane(postgres_session_factory)
    async with await _admin_client(
        auth_app,
        postgres_session_factory,
        auth_settings_overrides,
        email="synonym-seed-admin@example.com",
    ) as client:
        imported = await client.post(
            "/api/v1/admin/search-synonyms/en/draft/import-seed",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "1",
                "reason": "Load the bundled English research seed for review.",
            },
        )

    assert imported.status_code == 200
    validation = imported.json()["draft"]["validation"]
    assert validation["valid"] is True
    assert validation["group_count"] == 280
    assert validation["compiled_key_count"] > 0
    assert sum(issue["code"] == "inactive_group_no_eligible_key" for issue in validation["issues"]) == 27


async def test_publish_rejects_validation_errors_and_cross_locale_key_collisions(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_synonym_control_plane(postgres_session_factory)
    async with await _admin_client(
        auth_app,
        postgres_session_factory,
        auth_settings_overrides,
        email="synonym-validation-admin@example.com",
    ) as client:
        invalid = await client.put(
            "/api/v1/admin/search-synonyms/ru/draft",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "1",
                "source_text": "frog,toad\n",
                "reason": "Check script validation before publishing.",
            },
        )
        assert invalid.status_code == 200
        assert invalid.json()["draft"]["validation"]["valid"] is False
        blocked = await client.post(
            "/api/v1/admin/search-synonyms/ru/draft/publish",
            json={
                "request_id": str(uuid.uuid7()),
                "version": invalid.json()["draft"]["version"],
                "reason": "Attempt to publish the invalid draft.",
                "confirm_destructive": False,
            },
        )
        assert blocked.status_code == 422
        assert blocked.json()["detail"]["validation"]["valid"] is False
        assert any(
            issue["code"] == "catalog_requires_compiled_key"
            for issue in blocked.json()["detail"]["validation"]["issues"]
        )

        for locale, source in (("en", "123,456\n"), ("ru", "123,789\n")):
            catalog = await client.get(f"/api/v1/admin/search-synonyms/{locale}")
            saved = await client.put(
                f"/api/v1/admin/search-synonyms/{locale}/draft",
                json={
                    "request_id": str(uuid.uuid7()),
                    "version": catalog.json()["draft"]["version"],
                    "source_text": source,
                    "reason": f"Prepare the {locale.upper()} numeric alias collision case.",
                },
            )
            assert saved.status_code == 200
            published = await client.post(
                f"/api/v1/admin/search-synonyms/{locale}/draft/publish",
                json={
                    "request_id": str(uuid.uuid7()),
                    "version": saved.json()["draft"]["version"],
                    "reason": f"Publish the {locale.upper()} numeric alias collision case.",
                    "confirm_destructive": False,
                },
            )
            if locale == "en":
                assert published.status_code == 200
            else:
                assert published.status_code == 422
                issues = published.json()["detail"]["validation"]["issues"]
                assert any(
                    issue["code"] == "cross_locale_key_collision" and issue["term"] == "123"
                    for issue in issues
                )

        ru_catalog = await client.get("/api/v1/admin/search-synonyms/ru")
        assert ru_catalog.status_code == 200
        assert ru_catalog.json()["published"] is None


async def test_stale_reconcile_completion_cannot_overwrite_a_newer_publication(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_synonym_control_plane(postgres_session_factory)
    async with await _admin_client(
        auth_app,
        postgres_session_factory,
        auth_settings_overrides,
        email="synonym-concurrency-admin@example.com",
    ) as client:
        first_saved = await client.put(
            "/api/v1/admin/search-synonyms/en/draft",
            json={
                "request_id": str(uuid.uuid7()),
                "version": "1",
                "source_text": "frog,toad\n",
                "reason": "Prepare the first English synonym revision.",
            },
        )
        first_published = await client.post(
            "/api/v1/admin/search-synonyms/en/draft/publish",
            json={
                "request_id": str(uuid.uuid7()),
                "version": first_saved.json()["draft"]["version"],
                "reason": "Publish the first English synonym revision.",
                "confirm_destructive": False,
            },
        )
        assert first_published.status_code == 200

        repository = SqlAlchemySearchSynonymSyncRepository(postgres_session_factory)
        stale_snapshots = await repository.load_published_snapshots()
        assert len(stale_snapshots) == 1
        stale_snapshot = stale_snapshots[0]
        stale_map = compile_search_synonyms(
            stale_snapshot.source_text,
            locale=SearchSynonymLocale.EN,
        ).compiled_synonyms

        second_saved = await client.put(
            "/api/v1/admin/search-synonyms/en/draft",
            json={
                "request_id": str(uuid.uuid7()),
                "version": first_published.json()["draft"]["version"],
                "source_text": "frog,newt\n",
                "reason": "Prepare a superseding English synonym revision.",
            },
        )
        second_published = await client.post(
            "/api/v1/admin/search-synonyms/en/draft/publish",
            json={
                "request_id": str(uuid.uuid7()),
                "version": second_saved.json()["draft"]["version"],
                "reason": "Publish the superseding English synonym revision.",
                "confirm_destructive": False,
            },
        )
        assert second_published.status_code == 200
        current_sync = (await client.get("/api/v1/admin/search-synonyms/sync")).json()

    recorded = await repository.record_success(
        desired_hash=hash_canonical_synonym_map(stale_map),
        desired_revision_ids={"en": str(stale_snapshot.revision_id)},
        expected_snapshot_ids=(str(stale_snapshot.revision_id),),
        actual_hash=hash_canonical_synonym_map(stale_map),
        task_uid=73,
        provider_applied=True,
        succeeded_at=utcnow(),
    )

    assert recorded is False
    async with postgres_session_factory() as session:
        state = await session.get(SearchSynonymSyncState, SEARCH_SYNONYM_SYNC_STATE_ID)
        assert state is not None
        assert state.status.value == "pending"
        assert state.desired_hash == current_sync["desired_hash"]
        assert state.desired_revision_ids != {"en": str(stale_snapshot.revision_id)}
        assert state.provider_task_uid is None
        assert state.last_success_at is None
