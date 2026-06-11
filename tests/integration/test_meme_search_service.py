# ruff: noqa: TC002,TC003
"""Focused tests for the shared hybrid meme search service."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_meme_search_service
from memexpert.core.qdrant import QdrantUserSearchMatch
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    AccountType,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
)
from memexpert.models.user import User
from memexpert.schemas.user import UserRead
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters, MemeSearchService

pytestmark = pytest.mark.asyncio


class FakeTextSearchClient:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "limit": limit})
        return self._hits[:limit]


class FakeSemanticSearchClient:
    def __init__(self, hits: tuple[QdrantUserSearchMatch, ...]) -> None:
        self._hits = hits
        self.calls: list[dict[str, Any]] = []

    async def search_memes_by_vector(
        self,
        *,
        query_vector: tuple[float, ...],
        limit: int = 20,
    ) -> tuple[QdrantUserSearchMatch, ...]:
        self.calls.append({"query_vector": query_vector, "limit": limit})
        return self._hits[:limit]


class FakeQueryEmbeddingClient:
    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector
        self.calls: list[str] = []

    async def embed_query(self, query: str) -> tuple[float, ...]:
        self.calls.append(query)
        return self._vector


class FailingTextSearchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "limit": limit})
        raise RuntimeError("provider-secret-text-failure")


class FailingQueryEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_query(self, query: str) -> tuple[float, ...]:
        self.calls.append(query)
        raise RuntimeError("provider-secret-embedding-failure")


async def _create_meme(
    session: AsyncSession,
    *,
    media_type: ContentKind = ContentKind.IMAGE,
    language: ContentLanguage = ContentLanguage.EN,
    tags: list[str] | None = None,
    is_nsfw: bool = False,
    popularity_score: float = 0.0,
    is_public: bool = True,
    author_user_id: uuid.UUID | None = None,
) -> Meme:
    meme = Meme(
        media_type=media_type,
        language=language,
        tags=tags or [],
        is_nsfw=is_nsfw,
        popularity_score=popularity_score,
        is_public=is_public,
        author_user_id=author_user_id,
    )
    session.add(meme)
    await session.flush()
    file = MemeFile(
        meme_id=meme.id,
        s3_original_key=f"memes/{meme.id}.jpg",
        mime_type="image/jpeg",
        quality_score=0.8,
        is_primary=True,
    )
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
    await session.flush()
    return meme


def _primary_file_id(meme: Meme) -> uuid.UUID:
    assert meme.primary_file_id is not None
    return meme.primary_file_id


def _user_read(user: User) -> UserRead:
    return UserRead.model_validate(user)


def _install_meme_route_overrides(
    app: FastAPI,
    session: AsyncSession,
    *,
    current_user: UserRead | None = None,
    service: MemeSearchService | None = None,
) -> None:
    def override_meme_search_service() -> MemeSearchService:
        return service or MemeSearchService(session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_optional_current_user] = override_current_user


async def test_hybrid_search_ranks_by_weighted_semantic_text_and_popularity(
    migrated_db_session: AsyncSession,
) -> None:
    semantic_meme = await _create_meme(migrated_db_session, popularity_score=10.0)
    text_meme = await _create_meme(migrated_db_session, popularity_score=100.0)
    popular_meme = await _create_meme(migrated_db_session, popularity_score=200.0)

    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {"id": str(text_meme.primary_file_id), "meme_id": str(text_meme.id), "_rankingScore": 1.0},
                {"id": str(popular_meme.primary_file_id), "meme_id": str(popular_meme.id), "_rankingScore": 0.1},
            ],
        ),
        semantic_client=FakeSemanticSearchClient(
            (
                QdrantUserSearchMatch(
                    meme_file_id=_primary_file_id(semantic_meme),
                    meme_id=semantic_meme.id,
                    semantic_score=0.95,
                ),
                QdrantUserSearchMatch(
                    meme_file_id=_primary_file_id(popular_meme),
                    meme_id=popular_meme.id,
                    semantic_score=0.4,
                ),
            ),
        ),
    )

    page = await service.search_memes("frog", query_vector=(0.1, 0.2), limit=10)

    assert [item.meme.id for item in page.items] == [semantic_meme.id, text_meme.id, popular_meme.id]
    assert page.items[0].score.semantic == 1.0
    assert page.items[1].score.text == 1.0
    assert page.items[0].score.total > page.items[1].score.total


async def test_plain_text_query_embedding_feeds_qdrant_and_hybrid_merge(
    migrated_db_session: AsyncSession,
) -> None:
    semantic_meme = await _create_meme(migrated_db_session, popularity_score=10.0)
    text_meme = await _create_meme(migrated_db_session, popularity_score=100.0)
    text_client = FakeTextSearchClient(
        [
            {"id": str(text_meme.primary_file_id), "meme_id": str(text_meme.id), "_rankingScore": 1.0},
        ],
    )
    semantic_client = FakeSemanticSearchClient(
        (
            QdrantUserSearchMatch(
                meme_file_id=_primary_file_id(semantic_meme),
                meme_id=semantic_meme.id,
                semantic_score=0.95,
            ),
        ),
    )
    embedding_client = FakeQueryEmbeddingClient((0.3, 0.4))
    service = MemeSearchService(
        migrated_db_session,
        text_client=text_client,
        semantic_client=semantic_client,
        query_embedding_client=embedding_client,
    )

    page = await service.search_memes("  frog wizard  ", limit=10)

    assert embedding_client.calls == ["frog wizard"]
    assert semantic_client.calls == [{"query_vector": (0.3, 0.4), "limit": 40}]
    assert text_client.calls == [{"query": "frog wizard", "limit": 40}]
    assert [item.meme.id for item in page.items] == [semantic_meme.id, text_meme.id]
    assert page.items[0].score.semantic == 1.0
    assert page.items[1].score.text == 1.0


async def test_search_route_uses_plain_text_semantic_path_with_overridden_fakes(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    semantic_meme = await _create_meme(migrated_db_session, popularity_score=10.0)
    text_meme = await _create_meme(migrated_db_session, popularity_score=100.0)
    text_client = FakeTextSearchClient(
        [
            {"id": str(text_meme.primary_file_id), "meme_id": str(text_meme.id), "_rankingScore": 1.0},
        ],
    )
    semantic_client = FakeSemanticSearchClient(
        (
            QdrantUserSearchMatch(
                meme_file_id=_primary_file_id(semantic_meme),
                meme_id=semantic_meme.id,
                semantic_score=0.95,
            ),
        ),
    )
    embedding_client = FakeQueryEmbeddingClient((0.7, 0.8))

    service = MemeSearchService(
        migrated_db_session,
        text_client=text_client,
        semantic_client=semantic_client,
        query_embedding_client=embedding_client,
    )
    _install_meme_route_overrides(app, migrated_db_session, service=service)

    try:
        response = await client.get(
            "/api/v1/memes/search",
            params={"query": "frog wizard", "query_vector": [9.0, 9.0], "limit": 10},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [item["meme"]["id"] for item in payload["items"]] == [str(semantic_meme.id), str(text_meme.id)]
    assert "score" not in payload["items"][0]
    assert "s3_original_key" not in payload["items"][0]["meme"]["primary_file"]
    assert embedding_client.calls == ["frog wizard"]
    assert semantic_client.calls == [{"query_vector": (0.7, 0.8), "limit": 40}]


async def test_public_openapi_registers_catalog_routes_without_internal_surface(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    components = schema["components"]["schemas"]

    assert "/api/v1/memes/search" in paths
    assert "/api/v1/memes/browse" in paths
    assert "/api/v1/memes/{meme_id}" in paths
    search_parameters = {parameter["name"] for parameter in paths["/api/v1/memes/search"]["get"]["parameters"]}
    assert "query" in search_parameters
    assert "query_vector" not in search_parameters
    assert set(components["PublicMemeSearchResultRead"]["properties"]) == {"meme"}
    assert "MemeSearchScoreRead" not in components
    assert "s3_original_key" not in components["PublicMemeFileRead"]["properties"]
    assert "s3_web_video_key" not in components["PublicMemeFileRead"]["properties"]
    assert "author_user_id" not in components["PublicMemeDetailRead"]["properties"]
    assert "is_public" not in components["PublicMemeDetailRead"]["properties"]


async def test_search_filters_and_paginates_after_visibility(migrated_db_session: AsyncSession) -> None:
    first = await _create_meme(migrated_db_session, tags=["cat"], popularity_score=3.0)
    second = await _create_meme(migrated_db_session, tags=["cat"], popularity_score=2.0)
    await _create_meme(migrated_db_session, tags=["dog"], popularity_score=100.0)
    await _create_meme(
        migrated_db_session,
        tags=["cat"],
        language=ContentLanguage.RU,
        is_nsfw=True,
        popularity_score=200.0,
    )
    service = MemeSearchService(migrated_db_session)

    page = await service.search_memes(
        "",
        filters=MemeSearchFilters(language=ContentLanguage.EN, tags=("cat",)),
        limit=1,
        offset=1,
    )

    assert page.total == 2
    assert page.has_more is False
    assert [item.meme.id for item in page.items] == [second.id]
    assert first.id != second.id


async def test_browse_route_filters_and_paginates_popular_catalog(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    first = await _create_meme(migrated_db_session, tags=["cat"], popularity_score=3.0)
    second = await _create_meme(migrated_db_session, tags=["cat"], popularity_score=2.0)
    await _create_meme(migrated_db_session, tags=["dog"], popularity_score=100.0)
    await _create_meme(migrated_db_session, tags=["cat"], is_nsfw=True, popularity_score=200.0)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        response = await client.get(
            "/api/v1/memes/browse",
            params={"tags": "cat", "limit": 1, "offset": 1},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["has_more"] is False
    assert [item["meme"]["id"] for item in payload["items"]] == [str(second.id)]
    assert str(first.id) != str(second.id)


async def test_public_routes_apply_nsfw_defaults_and_authenticated_opt_in(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    safe_meme = await _create_meme(migrated_db_session, popularity_score=10.0)
    nsfw_meme = await _create_meme(migrated_db_session, is_nsfw=True, popularity_score=100.0)
    guest_user = User(nsfw_enabled=True)
    full_user = User(account_type=AccountType.FULL, nsfw_enabled=True)
    full_user_without_nsfw = User(account_type=AccountType.FULL, nsfw_enabled=False)
    migrated_db_session.add_all([guest_user, full_user, full_user_without_nsfw])
    await migrated_db_session.flush()

    async def browse_ids(current_user: UserRead | None, include_nsfw: bool) -> list[str]:
        _install_meme_route_overrides(app, migrated_db_session, current_user=current_user)
        try:
            response = await client.get("/api/v1/memes/browse", params={"include_nsfw": include_nsfw})
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        return [item["meme"]["id"] for item in response.json()["items"]]

    anonymous_ids = await browse_ids(None, include_nsfw=True)
    guest_ids = await browse_ids(_user_read(guest_user), include_nsfw=True)
    full_ids = await browse_ids(_user_read(full_user), include_nsfw=True)
    full_default_ids = await browse_ids(_user_read(full_user), include_nsfw=False)
    disabled_full_ids = await browse_ids(_user_read(full_user_without_nsfw), include_nsfw=True)

    assert anonymous_ids == [str(safe_meme.id)]
    assert guest_ids[:2] == [str(nsfw_meme.id), str(safe_meme.id)]
    assert full_ids[:2] == [str(nsfw_meme.id), str(safe_meme.id)]
    assert full_default_ids == [str(safe_meme.id)]
    assert disabled_full_ids == [str(safe_meme.id)]

    _install_meme_route_overrides(app, migrated_db_session, current_user=_user_read(full_user))
    try:
        detail_response = await client.get(f"/api/v1/memes/{nsfw_meme.id}", params={"include_nsfw": True})
    finally:
        app.dependency_overrides.clear()
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["id"] == str(nsfw_meme.id)
    assert "author_user_id" not in detail_payload
    assert "is_public" not in detail_payload


async def test_search_route_applies_nsfw_gate_for_anonymous_guest_and_full_account(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    safe_meme = await _create_meme(migrated_db_session, popularity_score=10.0)
    nsfw_meme = await _create_meme(migrated_db_session, is_nsfw=True, popularity_score=100.0)
    guest_user = User(nsfw_enabled=True)
    full_user = User(account_type=AccountType.FULL, nsfw_enabled=True)
    migrated_db_session.add_all([guest_user, full_user])
    await migrated_db_session.flush()
    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {"id": str(nsfw_meme.primary_file_id), "meme_id": str(nsfw_meme.id), "_rankingScore": 1.0},
                {"id": str(safe_meme.primary_file_id), "meme_id": str(safe_meme.id), "_rankingScore": 0.5},
            ],
        ),
    )

    async def search_ids(current_user: UserRead | None, include_nsfw: bool) -> list[str]:
        _install_meme_route_overrides(app, migrated_db_session, current_user=current_user, service=service)
        try:
            response = await client.get(
                "/api/v1/memes/search",
                params={"query": "frog", "include_nsfw": include_nsfw},
            )
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        return [item["meme"]["id"] for item in response.json()["items"]]

    assert await search_ids(None, include_nsfw=True) == [str(safe_meme.id)]
    assert await search_ids(_user_read(guest_user), include_nsfw=True) == [str(nsfw_meme.id), str(safe_meme.id)]
    assert await search_ids(_user_read(full_user), include_nsfw=True) == [str(nsfw_meme.id), str(safe_meme.id)]
    assert await search_ids(_user_read(full_user), include_nsfw=False) == [str(safe_meme.id)]


async def test_detail_route_returns_not_found_for_missing_private_or_nsfw_without_opt_in(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    author = User(account_type=AccountType.FULL, nsfw_enabled=True)
    stranger = User(account_type=AccountType.FULL, nsfw_enabled=True)
    migrated_db_session.add_all([author, stranger])
    await migrated_db_session.flush()
    private_meme = await _create_meme(migrated_db_session, is_public=False, author_user_id=author.id)
    nsfw_meme = await _create_meme(migrated_db_session, is_nsfw=True)
    missing_id = uuid.uuid4()

    async def detail_status(meme_id: uuid.UUID, current_user: UserRead | None, include_nsfw: bool = False) -> int:
        _install_meme_route_overrides(app, migrated_db_session, current_user=current_user)
        try:
            response = await client.get(f"/api/v1/memes/{meme_id}", params={"include_nsfw": include_nsfw})
        finally:
            app.dependency_overrides.clear()
        return response.status_code

    assert await detail_status(missing_id, None) == 404
    assert await detail_status(private_meme.id, None) == 404
    assert await detail_status(private_meme.id, _user_read(stranger)) == 404
    assert await detail_status(private_meme.id, _user_read(author)) == 200
    assert await detail_status(nsfw_meme.id, _user_read(author)) == 404
    assert await detail_status(nsfw_meme.id, _user_read(author), include_nsfw=True) == 200


async def test_provider_failures_fall_back_to_popular_without_raw_error_payload(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    popular_meme = await _create_meme(migrated_db_session, popularity_score=100.0)
    text_client = FailingTextSearchClient()
    embedding_client = FailingQueryEmbeddingClient()
    service = MemeSearchService(
        migrated_db_session,
        text_client=text_client,
        query_embedding_client=embedding_client,
    )
    _install_meme_route_overrides(app, migrated_db_session, service=service)

    try:
        response = await client.get("/api/v1/memes/search", params={"query": "frog"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "provider-secret" not in response.text
    payload = response.json()
    assert [item["meme"]["id"] for item in payload["items"]] == [str(popular_meme.id)]
    assert text_client.calls == [{"query": "frog", "limit": 80}]
    assert embedding_client.calls == ["frog"]


async def test_private_memes_are_visible_only_to_author_or_collection_member(
    migrated_db_session: AsyncSession,
) -> None:
    author = User()
    member = User()
    stranger = User()
    migrated_db_session.add_all([author, member, stranger])
    await migrated_db_session.flush()

    authored_private = await _create_meme(migrated_db_session, is_public=False, author_user_id=author.id)
    shared_private = await _create_meme(migrated_db_session, is_public=False)
    collection = Collection(
        owner_id=author.id,
        title="Shared",
        kind=CollectionKind.CUSTOM,
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add(collection)
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            CollectionMember(
                collection_id=collection.id,
                user_id=member.id,
                role=CollectionMembershipRole.VIEWER,
            ),
            CollectionMeme(collection_id=collection.id, meme_id=shared_private.id, added_by_user_id=author.id),
        ],
    )
    await migrated_db_session.flush()

    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {
                    "id": str(authored_private.primary_file_id),
                    "meme_id": str(authored_private.id),
                    "_rankingScore": 1.0,
                },
                {"id": str(shared_private.primary_file_id), "meme_id": str(shared_private.id), "_rankingScore": 0.9},
            ],
        ),
    )

    anonymous_page = await service.search_memes("private")
    author_page = await service.search_memes("private", viewer_user_id=author.id)
    member_page = await service.search_memes("private", viewer_user_id=member.id)
    stranger_page = await service.search_memes("private", viewer_user_id=stranger.id)

    assert anonymous_page.items == []
    assert {item.meme.id for item in author_page.items} == {authored_private.id, shared_private.id}
    assert {item.meme.id for item in member_page.items} == {shared_private.id}
    assert stranger_page.items == []


async def test_detail_read_enforces_visibility_and_nsfw(migrated_db_session: AsyncSession) -> None:
    viewer = User()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    private_meme = await _create_meme(migrated_db_session, is_public=False, author_user_id=viewer.id)
    nsfw_meme = await _create_meme(migrated_db_session, is_nsfw=True)
    service = MemeSearchService(migrated_db_session)

    detail = await service.get_meme_detail(private_meme.id, viewer_user_id=viewer.id)

    assert detail.id == private_meme.id
    with pytest.raises(MemeNotFoundError):
        await service.get_meme_detail(private_meme.id)
    with pytest.raises(MemeNotFoundError):
        await service.get_meme_detail(nsfw_meme.id)
    assert (await service.get_meme_detail(nsfw_meme.id, include_nsfw=True)).id == nsfw_meme.id
