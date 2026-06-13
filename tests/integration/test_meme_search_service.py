# ruff: noqa: TC002,TC003
"""Focused tests for the shared hybrid meme search service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from memexpert.api.dependencies import PIPELINE_OPERATOR_TOKEN_HEADER_NAME
from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_analytics_service, get_meme_search_service, get_public_trends_service
from memexpert.core.config import Settings, get_settings
from memexpert.core.qdrant import QdrantUserSearchMatch
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile, MemePopularitySnapshot, MemeSeoPage, MemeTemplate
from memexpert.models.enums import (
    AccountType,
    AnalyticsEventType,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
)
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, User
from memexpert.schemas.user import UserRead
from memexpert.services.analytics import AnalyticsService, InteractionEventRefs, InteractionEventWrite
from memexpert.services.media_render_urls import MediaRenderUrlService
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters, MemeSearchScope, MemeSearchService
from memexpert.services.meme_seo import MemeSeoGenerationService, MemeSeoProviderResult
from memexpert.services.public_trends import PublicTrendsService, refresh_public_trend_materialized_views

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from memexpert.schemas.meme import PublicMemeSearchPageRead


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


class FakeSeoProvider:
    model_id = "fake-seo-model"
    prompt_version = "seo-test-v1"

    def __init__(self, payloads: list[MemeSeoProviderResult]) -> None:
        self._payloads = payloads
        self.calls: list[uuid.UUID] = []

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        self.calls.append(meme.id)
        return self._payloads.pop(0)


class FakeSeoObjectProvider:
    model_id = "fake-seo-model"
    prompt_version = "seo-test-v1"

    def __init__(self, payloads: list[object]) -> None:
        self._payloads = payloads
        self.calls: list[uuid.UUID] = []

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        self.calls.append(meme.id)
        return cast("MemeSeoProviderResult", self._payloads.pop(0))


class FailingSeoProvider:
    model_id = "fake-failing-model"
    prompt_version = "seo-test-v1"

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        self.calls.append(meme.id)
        raise RuntimeError("provider-secret-seo-failure")


class FlakySeoProvider:
    model_id = "fake-flaky-model"
    prompt_version = "seo-test-v1"

    def __init__(self, *, failures_before_success: int, payload: MemeSeoProviderResult) -> None:
        self._remaining_failures = failures_before_success
        self._payload = payload
        self.calls: list[uuid.UUID] = []

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        self.calls.append(meme.id)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("provider-secret-transient-seo-failure")
        return self._payload


async def _create_meme(
    session: AsyncSession,
    *,
    media_type: ContentKind = ContentKind.IMAGE,
    language: ContentLanguage = ContentLanguage.EN,
    tags: list[str] | None = None,
    is_nsfw: bool = False,
    popularity_score: float = 0.0,
    like_count: int = 0,
    is_public: bool = True,
    author_user_id: uuid.UUID | None = None,
    ocr_text: str | None = None,
    s3_original_key: str | None = None,
    s3_web_video_key: str | None = None,
    mime_type: str = "image/jpeg",
    width: int | None = None,
    height: int | None = None,
    blur_hash: str | None = None,
) -> Meme:
    meme = Meme(
        media_type=media_type,
        language=language,
        tags=tags or [],
        is_nsfw=is_nsfw,
        popularity_score=popularity_score,
        like_count=like_count,
        is_public=is_public,
        author_user_id=author_user_id,
        ocr_text=ocr_text,
    )
    session.add(meme)
    await session.flush()
    file = MemeFile(
        meme_id=meme.id,
        s3_original_key=s3_original_key or f"memes/{meme.id}.jpg",
        s3_web_video_key=s3_web_video_key,
        mime_type=mime_type,
        width=width,
        height=height,
        blur_hash=blur_hash,
        quality_score=0.8,
        is_primary=True,
    )
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
    await session.flush()
    return meme


async def _create_collection(
    session: AsyncSession,
    *,
    owner: User,
    title: str,
    kind: CollectionKind = CollectionKind.CUSTOM,
    visibility: CollectionVisibility = CollectionVisibility.PRIVATE,
    memberships: list[tuple[User, CollectionMembershipRole]] | None = None,
    memes: list[Meme] | None = None,
) -> Collection:
    collection = Collection(
        owner_id=owner.id,
        title=title,
        kind=kind,
        visibility=visibility,
    )
    session.add(collection)
    await session.flush()
    session.add_all(
        [CollectionMember(collection_id=collection.id, user_id=user.id, role=role) for user, role in memberships or []]
    )
    session.add_all(
        [
            CollectionMeme(collection_id=collection.id, meme_id=meme.id, added_by_user_id=owner.id)
            for meme in memes or []
        ]
    )
    await session.flush()
    return collection


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

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(session)

    def override_public_trends_service() -> PublicTrendsService:
        return PublicTrendsService(session)

    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_public_trends_service] = override_public_trends_service
    app.dependency_overrides[get_optional_current_user] = override_current_user


async def _refresh_trend_views(session: AsyncSession) -> None:
    await session.execute(text("REFRESH MATERIALIZED VIEW public_meme_trends_mv"))
    await session.execute(text("REFRESH MATERIALIZED VIEW public_tag_trends_mv"))
    await session.execute(text("REFRESH MATERIALIZED VIEW public_template_trends_mv"))


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

    event = await migrated_db_session.scalar(
        select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.SEARCH_QUERY)
    )
    assert event is not None
    assert event.user_id is None
    assert event.payload == {
        "surface": "public_api",
        "query": "frog wizard",
        "language": None,
        "media_type": None,
        "scope": "public",
        "collection_ids": [],
        "include_nsfw": False,
        "tags": [],
        "limit": 10,
        "offset": 0,
        "result_count": 2,
        "has_more": False,
    }


async def test_public_openapi_registers_catalog_routes_without_internal_surface(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    components = schema["components"]["schemas"]

    assert "/api/v1/memes/search" in paths
    assert "/api/v1/memes/browse" in paths
    assert "/api/v1/memes/trending" in paths
    assert "/api/v1/memes/trends" in paths
    assert "/api/v1/memes/trends/tags" in paths
    assert "/api/v1/memes/trends/templates" in paths
    assert "/api/v1/memes/slug/{slug}" in paths
    assert "/api/v1/memes/tags/{tag_slug}" in paths
    assert "/api/v1/memes/templates/{template_slug}" in paths
    assert "/api/v1/memes/{meme_id}/canonical" in paths
    assert "/api/v1/memes/{meme_id}/popularity" in paths
    assert "/api/v1/memes/{meme_id}" in paths
    search_parameters = {parameter["name"] for parameter in paths["/api/v1/memes/search"]["get"]["parameters"]}
    browse_parameters = {parameter["name"] for parameter in paths["/api/v1/memes/browse"]["get"]["parameters"]}
    trending_parameters = {
        parameter["name"]: parameter for parameter in paths["/api/v1/memes/trending"]["get"]["parameters"]
    }
    assert "query" in search_parameters
    assert "scope" in search_parameters
    assert "collection_ids" in search_parameters
    assert "scope" in browse_parameters
    assert "collection_ids" in browse_parameters
    assert "query_vector" not in search_parameters
    assert "lookback_hours" in trending_parameters
    assert "ignores this value" in trending_parameters["lookback_hours"]["description"]
    assert set(components["PublicMemeSearchResultRead"]["properties"]) == {"meme"}
    assert "viewer_has_favorited" in components["PublicMemeCardRead"]["properties"]
    assert "viewer_has_saved" in components["PublicMemeCardRead"]["properties"]
    assert "viewer_has_pinned" in components["PublicMemeCardRead"]["properties"]
    assert "MemeSearchScoreRead" not in components
    assert set(components["PublicMemeTrendRead"]["properties"]) == {"meme", "trend"}
    assert set(components["PublicMemePopularitySummaryRead"]["properties"]) == {"meme_id", "trend", "sparkline"}
    assert "s3_original_key" not in components["PublicMemeFileRead"]["properties"]
    assert "s3_web_video_key" not in components["PublicMemeFileRead"]["properties"]
    assert "author_user_id" not in components["PublicMemeDetailRead"]["properties"]
    assert "is_public" not in components["PublicMemeDetailRead"]["properties"]


async def test_public_route_json_includes_render_contract_without_storage_leakage(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(
        migrated_db_session,
        s3_original_key="pipeline/originals/private/source.jpg",
        width=640,
        height=480,
        blur_hash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
    )
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=meme.id,
            slug="frog-wizard",
            page_title="Frog Wizard",
            meta_description="A frog wizard meme.",
            alt_text="Frog wizard image",
            caption="Frog wizard",
            tags=["frog"],
            model_id="test",
            prompt_version="test-v1",
            generated_at=datetime.now(UTC),
        )
    )
    private_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/hidden.jpg",
    )
    await migrated_db_session.commit()

    settings = Settings.model_validate(
        {
            "imgproxy_base_url": "https://img.memexpert.test",
            "imgproxy_key": "00112233445566778899aabbccddeeff",
            "imgproxy_salt": "ffeeddccbbaa99887766554433221100",
            "s3_bucket": "private-media-bucket",
        }
    )
    service = MemeSearchService(
        migrated_db_session,
        media_render_service=MediaRenderUrlService(settings),
    )
    _install_meme_route_overrides(app, migrated_db_session, service=service)

    try:
        response = await client.get("/api/v1/memes/browse", params={"limit": 10})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [item["meme"]["id"] for item in payload["items"]] == [str(meme.id)]
    assert payload["items"][0]["meme"]["viewer_has_favorited"] is False
    assert payload["items"][0]["meme"]["viewer_has_saved"] is False
    assert payload["items"][0]["meme"]["viewer_has_pinned"] is False
    assert str(private_meme.id) not in response.text
    primary_file = payload["items"][0]["meme"]["primary_file"]
    assert "s3_original_key" not in primary_file
    assert "s3_web_video_key" not in primary_file
    render = primary_file["render"]
    assert render["thumbnail_url"].startswith("https://img.memexpert.test/")
    assert "/unsafe/" not in render["thumbnail_url"]
    assert render["thumbnail_url"].endswith(".webp")
    assert "@webp" not in render["thumbnail_url"]
    assert render["preview_url"] == render["display_url"]
    assert render["download_url"] is not None
    assert "fn:ZnJvZy13aXphcmQuanBn:1" in render["download_url"]
    assert render["download_url"].endswith(".jpg")
    assert "@jpg" not in render["download_url"]
    assert render["width"] == 640
    assert render["height"] == 480
    assert render["blur_hash"] == "LEHV6nWB2yk8pyo0adR*.7kCMdnj"
    assert "private-media-bucket" not in response.text
    assert "pipeline/originals/private/source.jpg" not in response.text
    assert "pipeline/originals/private/hidden.jpg" not in response.text


async def test_search_route_scope_collections_returns_only_authorized_requested_collection_memes(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    viewer = User(account_type=AccountType.FULL)
    stranger = User(account_type=AccountType.FULL)
    migrated_db_session.add_all([viewer, stranger])
    await migrated_db_session.flush()
    authorized_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=viewer.id,
        s3_original_key="pipeline/originals/private/authorized-search-route.jpg",
    )
    unauthorized_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=stranger.id,
        s3_original_key="pipeline/originals/private/unauthorized-search-route.jpg",
    )
    authorized_collection = await _create_collection(
        migrated_db_session,
        owner=viewer,
        title="Authorized collection",
        memes=[authorized_private],
    )
    unauthorized_collection = await _create_collection(
        migrated_db_session,
        owner=stranger,
        title="Unauthorized collection",
        memes=[unauthorized_private],
    )
    await migrated_db_session.commit()

    service = MemeSearchService(
        migrated_db_session,
        media_render_service=MediaRenderUrlService(
            Settings.model_validate({"imgproxy_base_url": "https://img.memexpert.test"})
        ),
    )
    _install_meme_route_overrides(app, migrated_db_session, current_user=_user_read(viewer), service=service)

    try:
        response = await client.get(
            "/api/v1/memes/search",
            params=[
                ("scope", "collections"),
                ("collection_ids", str(authorized_collection.id)),
                ("collection_ids", str(unauthorized_collection.id)),
                ("collection_ids", str(authorized_collection.id)),
                ("limit", "10"),
            ],
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [item["meme"]["id"] for item in payload["items"]] == [str(authorized_private.id)]
    assert payload["items"][0]["meme"]["primary_file"]["render"]["thumbnail_url"] == (
        f"/api/v1/media/files/{authorized_private.primary_file_id}/thumbnail"
    )
    assert str(unauthorized_private.id) not in response.text
    assert "authorized-search-route.jpg" not in response.text
    assert "unauthorized-search-route.jpg" not in response.text

    event = await migrated_db_session.scalar(
        select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.SEARCH_QUERY)
    )
    assert event is not None
    assert event.payload["scope"] == "collections"
    assert event.payload["collection_ids"] == [str(authorized_collection.id), str(unauthorized_collection.id)]


async def test_public_search_and_detail_do_not_emit_authenticated_private_media(
    migrated_db_session: AsyncSession,
) -> None:
    owner = User(account_type=AccountType.FULL)
    migrated_db_session.add(owner)
    await migrated_db_session.flush()
    public_meme = await _create_meme(migrated_db_session, popularity_score=20.0)
    private_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=owner.id,
        popularity_score=100.0,
        s3_original_key="pipeline/originals/private/authenticated-owner.jpg",
    )
    await migrated_db_session.commit()
    assert private_meme.primary_file_id is not None
    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {
                    "id": str(private_meme.primary_file_id),
                    "meme_id": str(private_meme.id),
                    "_rankingScore": 1.0,
                },
                {"id": str(public_meme.primary_file_id), "meme_id": str(public_meme.id), "_rankingScore": 0.5},
            ]
        ),
        media_render_service=MediaRenderUrlService(
            Settings.model_validate({"imgproxy_base_url": "https://img.memexpert.test"})
        ),
    )

    search_page = await service.search_public_memes("owner private", viewer_user_id=owner.id, limit=10)
    browse_page = await service.browse_public_memes(viewer_user_id=owner.id, limit=10)
    public_detail = await service.get_public_meme_detail(public_meme.id, viewer_user_id=owner.id)

    assert [item.meme.id for item in search_page.items] == [public_meme.id]
    assert private_meme.id not in {item.meme.id for item in browse_page.items}
    assert public_detail.primary_file is not None
    assert public_detail.primary_file.id == public_meme.primary_file_id
    with pytest.raises(MemeNotFoundError):
        _ = await service.get_public_meme_detail(private_meme.id, viewer_user_id=owner.id)

    serialized = search_page.model_dump_json() + browse_page.model_dump_json()
    assert str(private_meme.id) not in serialized
    assert str(private_meme.primary_file_id) not in serialized
    assert "authenticated-owner.jpg" not in serialized


async def test_search_scopes_filter_db_candidates_memberships_and_nsfw(
    migrated_db_session: AsyncSession,
) -> None:
    viewer = User(account_type=AccountType.FULL)
    editor_owner = User(account_type=AccountType.FULL)
    viewer_owner = User(account_type=AccountType.FULL)
    stranger = User(account_type=AccountType.FULL)
    migrated_db_session.add_all([viewer, editor_owner, viewer_owner, stranger])
    await migrated_db_session.flush()

    public_catalog = await _create_meme(migrated_db_session, popularity_score=1.0)
    viewer_private_upload = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=viewer.id,
        popularity_score=1.0,
    )
    public_in_favorites = await _create_meme(migrated_db_session, popularity_score=1.0)
    owned_collection_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=viewer.id,
        popularity_score=1.0,
    )
    editor_collection_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=editor_owner.id,
        popularity_score=1.0,
    )
    viewer_collection_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=viewer_owner.id,
        popularity_score=1.0,
    )
    nsfw_editor_collection_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        is_nsfw=True,
        author_user_id=editor_owner.id,
        popularity_score=1.0,
    )
    unauthorized_collection_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=stranger.id,
        popularity_score=1.0,
    )
    stale_private_candidate = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=stranger.id,
        popularity_score=1.0,
    )

    favorites = await _create_collection(
        migrated_db_session,
        owner=viewer,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        memes=[public_in_favorites],
    )
    owned_collection = await _create_collection(
        migrated_db_session,
        owner=viewer,
        title="Owned collection",
        memes=[owned_collection_private],
    )
    editor_shared = await _create_collection(
        migrated_db_session,
        owner=editor_owner,
        title="Editor shared",
        memberships=[(viewer, CollectionMembershipRole.EDITOR)],
        memes=[editor_collection_private, nsfw_editor_collection_private],
    )
    await _create_collection(
        migrated_db_session,
        owner=viewer_owner,
        title="Viewer shared",
        memberships=[(viewer, CollectionMembershipRole.VIEWER)],
        memes=[viewer_collection_private],
    )
    unauthorized_collection = await _create_collection(
        migrated_db_session,
        owner=stranger,
        title="Unauthorized",
        memes=[unauthorized_collection_private],
    )
    await migrated_db_session.commit()

    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {
                    "id": str(viewer_private_upload.primary_file_id),
                    "meme_id": str(viewer_private_upload.id),
                    "_rankingScore": 1.0,
                },
                {
                    "id": str(public_in_favorites.primary_file_id),
                    "meme_id": str(public_in_favorites.id),
                    "_rankingScore": 0.95,
                },
                {
                    "id": str(owned_collection_private.primary_file_id),
                    "meme_id": str(owned_collection_private.id),
                    "_rankingScore": 0.9,
                },
                {
                    "id": str(editor_collection_private.primary_file_id),
                    "meme_id": str(editor_collection_private.id),
                    "_rankingScore": 0.85,
                },
                {
                    "id": str(viewer_collection_private.primary_file_id),
                    "meme_id": str(viewer_collection_private.id),
                    "_rankingScore": 0.8,
                },
                {
                    "id": str(nsfw_editor_collection_private.primary_file_id),
                    "meme_id": str(nsfw_editor_collection_private.id),
                    "_rankingScore": 0.75,
                },
                {
                    "id": str(unauthorized_collection_private.primary_file_id),
                    "meme_id": str(unauthorized_collection_private.id),
                    "_rankingScore": 0.7,
                },
                {
                    "id": str(stale_private_candidate.primary_file_id),
                    "meme_id": str(stale_private_candidate.id),
                    "_rankingScore": 0.65,
                },
                {
                    "id": str(public_catalog.primary_file_id),
                    "meme_id": str(public_catalog.id),
                    "_rankingScore": 0.6,
                },
            ]
        ),
        semantic_client=FakeSemanticSearchClient(
            (
                QdrantUserSearchMatch(
                    meme_file_id=_primary_file_id(stale_private_candidate),
                    meme_id=stale_private_candidate.id,
                    semantic_score=0.99,
                ),
                QdrantUserSearchMatch(
                    meme_file_id=_primary_file_id(viewer_collection_private),
                    meme_id=viewer_collection_private.id,
                    semantic_score=0.9,
                ),
            )
        ),
    )

    all_page = await service.search_memes(
        "scope",
        viewer_user_id=viewer.id,
        query_vector=(0.1, 0.2),
        filters=MemeSearchFilters(scope=MemeSearchScope.ALL),
        limit=20,
    )
    private_page = await service.search_memes(
        "scope",
        viewer_user_id=viewer.id,
        query_vector=(0.1, 0.2),
        filters=MemeSearchFilters(scope=MemeSearchScope.PRIVATE),
        limit=20,
    )
    collections_page = await service.search_memes(
        "scope",
        viewer_user_id=viewer.id,
        query_vector=(0.1, 0.2),
        filters=MemeSearchFilters(
            scope=MemeSearchScope.COLLECTIONS,
            collection_ids=(editor_shared.id, unauthorized_collection.id),
        ),
        limit=20,
    )
    collections_with_nsfw_page = await service.search_memes(
        "scope",
        viewer_user_id=viewer.id,
        query_vector=(0.1, 0.2),
        filters=MemeSearchFilters(
            scope=MemeSearchScope.COLLECTIONS,
            collection_ids=(editor_shared.id,),
            include_nsfw=True,
        ),
        limit=20,
    )
    empty_collections_page = await service.search_memes(
        "scope",
        viewer_user_id=viewer.id,
        query_vector=(0.1, 0.2),
        filters=MemeSearchFilters(scope=MemeSearchScope.COLLECTIONS),
        limit=20,
    )
    anonymous_private_page = await service.search_memes(
        "scope",
        query_vector=(0.1, 0.2),
        filters=MemeSearchFilters(scope=MemeSearchScope.PRIVATE),
        limit=20,
    )

    assert {item.meme.id for item in all_page.items} == {
        public_catalog.id,
        viewer_private_upload.id,
        public_in_favorites.id,
        owned_collection_private.id,
        editor_collection_private.id,
        viewer_collection_private.id,
    }
    assert {item.meme.id for item in private_page.items} == {
        viewer_private_upload.id,
        public_in_favorites.id,
        owned_collection_private.id,
        editor_collection_private.id,
        viewer_collection_private.id,
    }
    assert public_catalog.id not in {item.meme.id for item in private_page.items}
    assert {item.meme.id for item in collections_page.items} == {editor_collection_private.id}
    assert {item.meme.id for item in collections_with_nsfw_page.items} == {
        editor_collection_private.id,
        nsfw_editor_collection_private.id,
    }
    assert empty_collections_page.items == []
    assert anonymous_private_page.items == []
    assert unauthorized_collection_private.id not in {item.meme.id for item in all_page.items}
    assert stale_private_candidate.id not in {item.meme.id for item in all_page.items}
    assert favorites.id != owned_collection.id


async def test_guest_search_scope_includes_guest_uploads_and_favorites(
    migrated_db_session: AsyncSession,
) -> None:
    guest = User(account_type=AccountType.GUEST)
    stranger = User(account_type=AccountType.FULL)
    migrated_db_session.add_all([guest, stranger])
    await migrated_db_session.flush()

    public_catalog = await _create_meme(migrated_db_session, popularity_score=1.0)
    guest_private_upload = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=guest.id,
        popularity_score=1.0,
    )
    guest_favorite_public = await _create_meme(migrated_db_session, popularity_score=1.0)
    stranger_private = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=stranger.id,
        popularity_score=1.0,
    )
    favorites = await _create_collection(
        migrated_db_session,
        owner=guest,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        memes=[guest_favorite_public],
    )
    await migrated_db_session.commit()

    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {
                    "id": str(guest_private_upload.primary_file_id),
                    "meme_id": str(guest_private_upload.id),
                    "_rankingScore": 1.0,
                },
                {
                    "id": str(guest_favorite_public.primary_file_id),
                    "meme_id": str(guest_favorite_public.id),
                    "_rankingScore": 0.9,
                },
                {
                    "id": str(stranger_private.primary_file_id),
                    "meme_id": str(stranger_private.id),
                    "_rankingScore": 0.8,
                },
                {
                    "id": str(public_catalog.primary_file_id),
                    "meme_id": str(public_catalog.id),
                    "_rankingScore": 0.7,
                },
            ]
        ),
    )

    private_page = await service.search_memes(
        "guest",
        viewer_user_id=guest.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.PRIVATE),
        limit=20,
    )
    all_page = await service.search_memes("guest", viewer_user_id=guest.id, limit=20)
    collections_page = await service.search_memes(
        "guest",
        viewer_user_id=guest.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.COLLECTIONS, collection_ids=(favorites.id,)),
        limit=20,
    )

    assert {item.meme.id for item in private_page.items} == {guest_private_upload.id, guest_favorite_public.id}
    assert {item.meme.id for item in all_page.items} == {
        public_catalog.id,
        guest_private_upload.id,
        guest_favorite_public.id,
    }
    assert [item.meme.id for item in collections_page.items] == [guest_favorite_public.id]
    assert stranger_private.id not in {item.meme.id for item in all_page.items}


async def test_public_wrappers_stay_public_by_default_and_allow_authorized_scope_expansion(
    migrated_db_session: AsyncSession,
) -> None:
    owner = User(account_type=AccountType.FULL)
    migrated_db_session.add(owner)
    await migrated_db_session.flush()
    public_meme = await _create_meme(migrated_db_session, popularity_score=1.0)
    private_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        author_user_id=owner.id,
        popularity_score=1.0,
        s3_original_key="pipeline/originals/private/scope-owner.jpg",
    )
    await migrated_db_session.commit()

    service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {"id": str(private_meme.primary_file_id), "meme_id": str(private_meme.id), "_rankingScore": 1.0},
                {"id": str(public_meme.primary_file_id), "meme_id": str(public_meme.id), "_rankingScore": 0.9},
            ]
        ),
        media_render_service=MediaRenderUrlService(
            Settings.model_validate({"imgproxy_base_url": "https://img.memexpert.test"})
        ),
    )

    default_search_page = await service.search_public_memes("owner", viewer_user_id=owner.id, limit=10)
    expanded_search_page = await service.search_public_memes(
        "owner",
        viewer_user_id=owner.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.ALL),
        limit=10,
    )
    default_browse_page = await service.browse_public_memes(viewer_user_id=owner.id, limit=10)
    expanded_browse_page = await service.browse_public_memes(
        viewer_user_id=owner.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.ALL),
        limit=10,
    )

    assert [item.meme.id for item in default_search_page.items] == [public_meme.id]
    assert private_meme.id not in {item.meme.id for item in default_browse_page.items}
    assert {item.meme.id for item in expanded_search_page.items} == {public_meme.id, private_meme.id}
    assert {item.meme.id for item in expanded_browse_page.items} == {public_meme.id, private_meme.id}

    private_card = next(item.meme for item in expanded_search_page.items if item.meme.id == private_meme.id)
    assert private_card.primary_file is not None
    assert private_card.primary_file.render is not None
    assert private_card.primary_file.render.thumbnail_url == (
        f"/api/v1/media/files/{private_meme.primary_file_id}/thumbnail"
    )
    assert "scope-owner.jpg" not in expanded_search_page.model_dump_json()


async def test_public_meme_dtos_include_viewer_action_state_for_anonymous_guest_and_full_accounts(
    migrated_db_session: AsyncSession,
) -> None:
    tag = "action-state"
    template = MemeTemplate(slug="action-template", name="Action Template")
    migrated_db_session.add(template)
    await migrated_db_session.flush()
    favorite_meme = await _create_meme(migrated_db_session, tags=[tag], popularity_score=30.0)
    saved_meme = await _create_meme(migrated_db_session, tags=[tag], popularity_score=20.0)
    pinned_meme = await _create_meme(migrated_db_session, tags=[tag], popularity_score=10.0)
    favorite_meme.template_id = template.id
    saved_meme.template_id = template.id
    pinned_meme.template_id = template.id
    guest_user = User(account_type=AccountType.GUEST)
    full_user = User(account_type=AccountType.FULL)
    migrated_db_session.add_all([guest_user, full_user])
    await migrated_db_session.flush()

    guest_favorites = Collection(
        owner_id=guest_user.id,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        visibility=CollectionVisibility.PRIVATE,
    )
    full_favorites = Collection(
        owner_id=full_user.id,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        visibility=CollectionVisibility.PRIVATE,
    )
    full_active_collection = Collection(
        owner_id=full_user.id,
        title="Saved reactions",
        kind=CollectionKind.CUSTOM,
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add_all([guest_favorites, full_favorites, full_active_collection])
    await migrated_db_session.flush()
    guest_user.active_save_collection_id = guest_favorites.id
    full_user.active_save_collection_id = full_active_collection.id
    migrated_db_session.add_all(
        [
            CollectionMeme(collection_id=guest_favorites.id, meme_id=favorite_meme.id, added_by_user_id=guest_user.id),
            CollectionMeme(collection_id=full_favorites.id, meme_id=favorite_meme.id, added_by_user_id=full_user.id),
            CollectionMeme(
                collection_id=full_active_collection.id,
                meme_id=saved_meme.id,
                added_by_user_id=full_user.id,
            ),
            PinnedMeme(user_id=full_user.id, meme_id=pinned_meme.id, position=1),
        ]
    )
    await migrated_db_session.flush()
    service = MemeSearchService(migrated_db_session)
    search_service = MemeSearchService(
        migrated_db_session,
        text_client=FakeTextSearchClient(
            [
                {"id": str(favorite_meme.primary_file_id), "meme_id": str(favorite_meme.id), "_rankingScore": 1.0},
                {"id": str(saved_meme.primary_file_id), "meme_id": str(saved_meme.id), "_rankingScore": 0.9},
                {"id": str(pinned_meme.primary_file_id), "meme_id": str(pinned_meme.id), "_rankingScore": 0.8},
            ]
        ),
    )

    def page_states(page: PublicMemeSearchPageRead) -> dict[uuid.UUID, tuple[bool, bool, bool]]:
        return {
            item.meme.id: (item.meme.viewer_has_favorited, item.meme.viewer_has_saved, item.meme.viewer_has_pinned)
            for item in page.items
        }

    anonymous_page = await service.browse_public_memes(limit=10)
    guest_page = await service.browse_public_memes(viewer_user_id=guest_user.id, limit=10)
    full_page = await service.browse_public_memes(viewer_user_id=full_user.id, limit=10)
    full_search_page = await search_service.search_public_memes("action", viewer_user_id=full_user.id, limit=10)
    full_tag_page = await service.browse_public_tag(tag, viewer_user_id=full_user.id, limit=10)
    matched_template, full_template_page = await service.browse_public_template(
        template.slug,
        viewer_user_id=full_user.id,
        limit=10,
    )
    full_saved_detail = await service.get_public_meme_detail(saved_meme.id, viewer_user_id=full_user.id)

    anonymous_states = page_states(anonymous_page)
    guest_states = page_states(guest_page)
    full_states = page_states(full_page)
    expected_full_states = {
        favorite_meme.id: (True, False, False),
        saved_meme.id: (False, True, False),
        pinned_meme.id: (False, False, True),
    }

    assert set(anonymous_states) == {favorite_meme.id, saved_meme.id, pinned_meme.id}
    assert all(state == (False, False, False) for state in anonymous_states.values())
    assert guest_states[favorite_meme.id] == (True, True, False)
    assert guest_states[saved_meme.id] == (False, False, False)
    assert full_states == expected_full_states
    assert page_states(full_search_page) == expected_full_states
    assert page_states(full_tag_page) == expected_full_states
    assert matched_template is not None
    assert page_states(full_template_page) == expected_full_states
    assert full_saved_detail.viewer_has_favorited is False
    assert full_saved_detail.viewer_has_saved is True
    assert full_saved_detail.viewer_has_pinned is False


async def test_public_page_viewer_action_state_uses_fixed_query_count(
    migrated_db_session: AsyncSession,
    postgres_async_engine: AsyncEngine,
) -> None:
    memes = [await _create_meme(migrated_db_session, popularity_score=float(score)) for score in (30, 20, 10)]
    user = User(account_type=AccountType.FULL)
    migrated_db_session.add(user)
    await migrated_db_session.flush()
    user_id = user.id
    favorites = Collection(
        owner_id=user.id,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        visibility=CollectionVisibility.PRIVATE,
    )
    active_collection = Collection(
        owner_id=user.id,
        title="Saved reactions",
        kind=CollectionKind.CUSTOM,
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add_all([favorites, active_collection])
    await migrated_db_session.flush()
    user.active_save_collection_id = active_collection.id
    migrated_db_session.add_all(
        [
            CollectionMeme(collection_id=favorites.id, meme_id=memes[0].id, added_by_user_id=user.id),
            CollectionMeme(collection_id=active_collection.id, meme_id=memes[1].id, added_by_user_id=user.id),
            PinnedMeme(user_id=user.id, meme_id=memes[2].id, position=1),
        ]
    )
    await migrated_db_session.flush()
    service = MemeSearchService(migrated_db_session)

    async def count_selects(limit: int) -> int:
        migrated_db_session.expire_all()
        select_count = 0

        def count_select(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(postgres_async_engine.sync_engine, "before_cursor_execute", count_select)
        try:
            page = await service.browse_public_memes(viewer_user_id=user_id, limit=limit)
        finally:
            event.remove(postgres_async_engine.sync_engine, "before_cursor_execute", count_select)
        assert len(page.items) == limit
        return select_count

    one_item_selects = await count_selects(1)
    three_item_selects = await count_selects(3)

    assert one_item_selects > 0
    assert three_item_selects == one_item_selects


async def test_public_video_detail_uses_direct_media_base_without_imgproxy(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(
        migrated_db_session,
        media_type=ContentKind.VIDEO,
        s3_original_key="pipeline/originals/private/video.mov",
        s3_web_video_key="pipeline/derived/private/web.mp4",
        mime_type="video/mp4",
    )
    await migrated_db_session.commit()
    settings = Settings.model_validate(
        {
            "imgproxy_base_url": "https://img.memexpert.test",
            "media_public_base_url": "https://media.memexpert.test/files",
        }
    )
    service = MemeSearchService(
        migrated_db_session,
        media_render_service=MediaRenderUrlService(settings),
    )

    detail = await service.get_public_meme_detail(meme.id)

    assert detail.primary_file is not None
    assert detail.primary_file.render is not None
    assert detail.primary_file.render.web_video_url is not None
    assert detail.primary_file.render.web_video_url.startswith("https://media.memexpert.test/files/")
    assert "img.memexpert.test" not in detail.primary_file.render.web_video_url


async def test_seo_generation_stores_prompt_evidence_unique_slugs_tags_and_template(
    migrated_db_session: AsyncSession,
) -> None:
    first = await _create_meme(migrated_db_session, tags=["old"], ocr_text="frog wizard text")
    second = await _create_meme(migrated_db_session, tags=["old"], ocr_text="frog wizard text")
    provider = FakeSeoProvider(
        [
            MemeSeoProviderResult(
                slug="Frog Wizard",
                page_title="Frog Wizard Reaction",
                meta_description="A frog wizard reaction meme.",
                alt_text="Frog wizard reaction image",
                caption="Frog wizard",
                body_text="A wizard frog casts a spell.",
                tags=(" Frog ", "wizard", "frog"),
                template_slug="wizard-frog",
                template_name="Wizard Frog",
                template_description="Frog wizard image macro template.",
            ),
            MemeSeoProviderResult(
                slug="frog wizard",
                page_title="Another Frog Wizard",
                meta_description="Another frog wizard meme.",
                alt_text="Another frog wizard image",
                tags=("frog", "magic"),
                template_slug="wizard-frog",
                template_name="Wizard Frog",
            ),
        ],
    )
    service = MemeSeoGenerationService(migrated_db_session, provider=provider)

    results = await service.generate_for_meme_ids((first.id, second.id), commit=False)

    assert [result.status for result in results] == ["generated", "generated"]
    assert [result.slug for result in results] == ["frog-wizard", "frog-wizard-2"]
    assert [result.model_id for result in results] == ["fake-seo-model", "fake-seo-model"]
    assert [result.prompt_version for result in results] == ["seo-test-v1", "seo-test-v1"]
    assert provider.calls == [first.id, second.id]

    first_page = await migrated_db_session.get(MemeSeoPage, first.id)
    second_page = await migrated_db_session.get(MemeSeoPage, second.id)
    assert first_page is not None
    assert second_page is not None
    assert first_page.slug == "frog-wizard"
    assert second_page.slug == "frog-wizard-2"
    assert first_page.model_id == "fake-seo-model"
    assert first_page.prompt_version == "seo-test-v1"
    assert first_page.tags == ["frog", "wizard"]
    assert first.tags == ["frog", "wizard"]
    assert first.template_id is not None
    assert second.template_id == first.template_id


async def test_seo_generation_preserves_manual_pages_without_force(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["original"])
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=meme.id,
            slug="manual-slug",
            page_title="Manual title",
            meta_description="Manual description",
            alt_text="Manual alt",
            tags=["manual"],
            model_id="admin",
            prompt_version="manual-v1",
            generated_at=datetime.now(UTC),
            edited_at=datetime.now(UTC),
        ),
    )
    await migrated_db_session.flush()
    provider = FakeSeoProvider(
        [
            MemeSeoProviderResult(
                slug="provider-slug",
                page_title="Provider title",
                meta_description="Provider description",
                alt_text="Provider alt",
                tags=("provider",),
            ),
        ],
    )
    service = MemeSeoGenerationService(migrated_db_session, provider=provider)

    skipped = await service.generate_for_meme_id(meme.id, commit=False)
    forced = await service.generate_for_meme_id(meme.id, force=True, commit=False)

    assert skipped.status == "skipped"
    assert skipped.reason == "manual_edit_present"
    assert provider.calls == [meme.id]
    assert forced.status == "generated"
    page = await migrated_db_session.get(MemeSeoPage, meme.id)
    assert page is not None
    assert page.slug == "provider-slug"
    assert page.page_title == "Provider title"


async def test_seo_provider_failure_returns_failed_without_raw_error_or_page(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session)
    provider = FailingSeoProvider()
    service = MemeSeoGenerationService(migrated_db_session, provider=provider, provider_max_attempts=2)

    result = await service.generate_for_meme_id(meme.id, commit=False)

    assert result.status == "failed"
    assert result.reason == "provider_error"
    assert "provider-secret" not in repr(result)
    assert provider.calls == [meme.id, meme.id]
    assert await migrated_db_session.get(MemeSeoPage, meme.id) is None


async def test_seo_generation_accepts_v0_alias_payload_safely(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["frog"], ocr_text="wizard frog")
    provider = FakeSeoObjectProvider(
        [
            {
                "title": "Wizard Frog",
                "description": "A wizard frog meme.",
                "subtitle": "Wizard frog reaction image",
                "text_on_meme": "THIS SHOULD NOT PERSIST",
                "slug": "wizard frog",
                "tags": ["frog", "magic"],
            }
        ],
    )
    service = MemeSeoGenerationService(migrated_db_session, provider=provider)

    result = await service.generate_for_meme_id(meme.id, commit=False)

    assert result.status == "generated"
    page = await migrated_db_session.get(MemeSeoPage, meme.id)
    assert page is not None
    assert page.page_title == "Wizard Frog"
    assert page.meta_description == "A wizard frog meme."
    assert page.alt_text == "Wizard frog reaction image"
    assert page.caption == "Wizard frog reaction image"
    assert page.body_text is None
    assert page.slug == "wizard-frog"
    assert page.tags == ["frog", "magic"]


async def test_seo_generation_rejects_invalid_tag_payload_without_creating_page(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["frog"])
    service = MemeSeoGenerationService(
        migrated_db_session,
        provider=FakeSeoObjectProvider(
            [
                {
                    "page_title": "Wizard Frog",
                    "meta_description": "A wizard frog meme.",
                    "alt_text": "Wizard frog reaction image",
                    "tags": {"frog": "magic"},
                }
            ],
        ),
    )

    result = await service.generate_for_meme_id(meme.id, commit=False)

    assert result.status == "failed"
    assert result.reason == "invalid_output"
    assert await migrated_db_session.get(MemeSeoPage, meme.id) is None


async def test_seo_generation_retries_transient_provider_failures_before_success(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["frog"], ocr_text="wizard frog")
    provider = FlakySeoProvider(
        failures_before_success=2,
        payload=MemeSeoProviderResult(
            slug="wizard frog",
            page_title="Wizard Frog",
            meta_description="A wizard frog meme.",
            alt_text="Wizard frog meme image",
            tags=("frog", "magic"),
        ),
    )
    service = MemeSeoGenerationService(migrated_db_session, provider=provider, provider_max_attempts=3)

    result = await service.generate_for_meme_id(meme.id, commit=False)

    assert result.status == "generated"
    assert result.slug == "wizard-frog"
    assert provider.calls == [meme.id, meme.id, meme.id]
    page = await migrated_db_session.get(MemeSeoPage, meme.id)
    assert page is not None
    assert page.page_title == "Wizard Frog"


async def test_seo_generation_rejects_invalid_provider_output_without_updating_existing_page(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["frog"])
    existing_generated_at = datetime.now(UTC) - timedelta(days=1)
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=meme.id,
            slug="existing-slug",
            page_title="Existing title",
            meta_description="Existing description",
            alt_text="Existing alt text",
            caption="Existing caption",
            body_text="Existing body text",
            tags=["existing"],
            model_id="existing-model",
            prompt_version="existing-v1",
            generated_at=existing_generated_at,
        ),
    )
    await migrated_db_session.flush()
    service = MemeSeoGenerationService(
        migrated_db_session,
        provider=FakeSeoObjectProvider(
            [
                {
                    "page_title": "Replacement title",
                    "meta_description": "Replacement description",
                    "alt_text": "Replacement alt text",
                    "template_slug": "replacement-template",
                    "template_description": "Replacement template description",
                },
            ],
        ),
    )

    result = await service.generate_for_meme_id(meme.id, force=True, commit=False)

    assert result.status == "failed"
    assert result.reason == "invalid_output"
    page = await migrated_db_session.get(MemeSeoPage, meme.id)
    assert page is not None
    assert page.slug == "existing-slug"
    assert page.page_title == "Existing title"
    assert page.meta_description == "Existing description"
    assert page.alt_text == "Existing alt text"
    assert page.caption == "Existing caption"
    assert page.body_text == "Existing body text"
    assert page.tags == ["existing"]
    assert page.model_id == "existing-model"
    assert page.prompt_version == "existing-v1"
    assert page.generated_at == existing_generated_at


async def test_slug_route_uuid_route_and_id_to_slug_metadata_return_seo_fields(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["frog"])
    service = MemeSeoGenerationService(
        migrated_db_session,
        provider=FakeSeoProvider(
            [
                MemeSeoProviderResult(
                    slug="frog wizard",
                    page_title="Frog Wizard Reaction",
                    meta_description="A frog wizard reaction meme.",
                    alt_text="Frog wizard reaction image",
                    body_text="Generated body text.",
                    tags=("frog", "wizard"),
                ),
            ],
        ),
    )
    await service.generate_for_meme_id(meme.id, commit=False)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        by_id = await client.get(f"/api/v1/memes/{meme.id}")
        canonical = await client.get(f"/api/v1/memes/{meme.id}/canonical")
        by_slug = await client.get("/api/v1/memes/slug/frog-wizard")
    finally:
        app.dependency_overrides.clear()

    assert by_id.status_code == 200
    by_id_payload = by_id.json()
    assert by_id_payload["id"] == str(meme.id)
    assert by_id_payload["seo_page_slug"] == "frog-wizard"
    assert by_id_payload["seo_title"] == "Frog Wizard Reaction"
    assert by_id_payload["seo_description"] == "A frog wizard reaction meme."
    assert by_id_payload["seo_alt_text"] == "Frog wizard reaction image"
    assert by_id_payload["seo_body_text"] == "Generated body text."
    assert by_id_payload["seo_model_id"] == "fake-seo-model"
    assert by_id_payload["seo_prompt_version"] == "seo-test-v1"
    assert by_id_payload["seo_generated_at"] is not None
    assert canonical.status_code == 200
    assert canonical.json() == {
        "meme_id": str(meme.id),
        "slug": "frog-wizard",
        "path": "/memes/frog-wizard",
        "should_redirect": True,
    }
    assert by_slug.status_code == 200
    assert by_slug.json()["id"] == str(meme.id)


async def test_tag_and_template_landing_routes_return_public_pages(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    template = MemeTemplate(slug="drake-template", name="Drake Template", description="Drake choices memes")
    migrated_db_session.add(template)
    await migrated_db_session.flush()
    first = await _create_meme(migrated_db_session, tags=["reaction"], popularity_score=10.0)
    second = await _create_meme(migrated_db_session, tags=["reaction"], popularity_score=5.0)
    unrelated = await _create_meme(migrated_db_session, tags=["cat"], popularity_score=100.0)
    first.template_id = template.id
    second.template_id = template.id
    await migrated_db_session.flush()
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        tag_response = await client.get("/api/v1/memes/tags/reaction", params={"limit": 10})
        template_response = await client.get("/api/v1/memes/templates/drake-template", params={"limit": 10})
        missing_template_response = await client.get("/api/v1/memes/templates/missing-template")
    finally:
        app.dependency_overrides.clear()

    assert tag_response.status_code == 200
    tag_payload = tag_response.json()
    assert tag_payload["kind"] == "tag"
    assert tag_payload["slug"] == "reaction"
    assert tag_payload["page"]["total"] == 2
    assert [item["meme"]["id"] for item in tag_payload["page"]["items"]] == [str(first.id), str(second.id)]
    assert str(unrelated.id) not in tag_response.text

    assert template_response.status_code == 200
    template_payload = template_response.json()
    assert template_payload["kind"] == "template"
    assert template_payload["title"] == "Drake Template memes"
    assert template_payload["page"]["total"] == 2
    assert [item["meme"]["id"] for item in template_payload["page"]["items"]] == [str(first.id), str(second.id)]
    assert missing_template_response.status_code == 404


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


async def test_trending_route_ranks_recent_events_snapshots_and_popularity_without_private_ids(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    event_meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=1.0, like_count=1)
    snapshot_meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=2.0, like_count=1)
    popularity_meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=20.0, like_count=10)
    private_meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=100.0, is_public=False)
    migrated_db_session.add_all(
        [
            *[
                AnalyticsEvent(
                    event_type=AnalyticsEventType.MEME_LIKE,
                    payload={"meme_id": str(event_meme.id), "telegram_user_hash": "hashed-user"},
                )
                for _ in range(10)
            ],
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(private_meme.id), "telegram_user_id": 12345},
            ),
            AnalyticsEvent(event_type=AnalyticsEventType.MEME_VIEW, payload={"meme_id": "not-a-uuid"}),
            AnalyticsEvent(event_type=AnalyticsEventType.MEME_VIEW, payload={"meme_id": {"bad": "json"}}),
            MemePopularitySnapshot(meme_id=snapshot_meme.id, popularity_score=100.0, source_views=50),
        ]
    )
    await migrated_db_session.flush()
    await _refresh_trend_views(migrated_db_session)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        first_response = await client.get("/api/v1/memes/trending", params={"tags": "trend", "limit": 10})
        second_response = await client.get("/api/v1/memes/trending", params={"tags": "trend", "limit": 10})
    finally:
        app.dependency_overrides.clear()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json() == second_response.json()
    payload = first_response.json()
    assert [item["meme"]["id"] for item in payload["items"]] == [
        str(event_meme.id),
        str(snapshot_meme.id),
        str(popularity_meme.id),
    ]
    assert str(private_meme.id) not in first_response.text
    assert "telegram_user_id" not in first_response.text
    assert "telegram_user_hash" not in first_response.text


async def test_trending_memes_uses_strict_interaction_event_refs_for_recent_scores(
    migrated_db_session: AsyncSession,
) -> None:
    strict_event_meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=1.0, like_count=1)
    no_event_meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=1.0, like_count=1)
    analytics_service = AnalyticsService(migrated_db_session)
    await analytics_service.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_SAVE,
            surface="public_api",
            refs=InteractionEventRefs(meme_id=strict_event_meme.id),
            properties={"chat_hash": "hashed-chat"},
        )
    )

    page = await MemeSearchService(migrated_db_session).trending_memes(
        filters=MemeSearchFilters(tags=("trend",)),
        limit=10,
        lookback_hours=24,
    )

    assert [item.meme.id for item in page.items] == [strict_event_meme.id, no_event_meme.id]


async def test_public_trend_endpoints_rank_from_materialized_views_and_return_aggregates(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    template = MemeTemplate(slug="frog-template", name="Frog Template", description="Frog format")
    migrated_db_session.add(template)
    await migrated_db_session.flush()
    snapshot_meme = await _create_meme(migrated_db_session, tags=["reaction"], popularity_score=1.0)
    snapshot_meme.template_id = template.id
    liked_meme = await _create_meme(migrated_db_session, tags=["reaction"], popularity_score=1.0)
    rising_meme = await _create_meme(migrated_db_session, tags=["reaction"], popularity_score=1.0)
    analytics_service = AnalyticsService(migrated_db_session)
    await analytics_service.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_DOWNLOAD,
            surface="public_api",
            refs=InteractionEventRefs(meme_id=liked_meme.id),
            properties={"chat_hash": "hashed-chat-1"},
        )
    )
    await analytics_service.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_DOWNLOAD,
            surface="public_api",
            refs=InteractionEventRefs(meme_id=liked_meme.id),
            properties={"chat_hash": "hashed-chat-2"},
        )
    )
    now = datetime.now(UTC)
    migrated_db_session.add_all(
        [
            *[
                AnalyticsEvent(event_type=AnalyticsEventType.MEME_LIKE, payload={"meme_id": str(liked_meme.id)})
                for _ in range(3)
            ],
            *[
                AnalyticsEvent(event_type=AnalyticsEventType.MEME_SEND, payload={"meme_id": str(rising_meme.id)})
                for _ in range(7)
            ],
            MemePopularitySnapshot(
                meme_id=snapshot_meme.id,
                captured_at=now - timedelta(hours=2),
                source_views=10,
                popularity_score=100.0,
            ),
            MemePopularitySnapshot(
                meme_id=snapshot_meme.id,
                captured_at=now,
                source_views=20,
                source_reactions=4,
                source_reposts=1,
                popularity_score=200.0,
            ),
        ]
    )
    await migrated_db_session.flush()
    await _refresh_trend_views(migrated_db_session)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        trending_response = await client.get("/api/v1/memes/trends", params={"ranking": "trending", "limit": 10})
        rising_response = await client.get(
            "/api/v1/memes/trends",
            params={"ranking": "fastest_rising", "limit": 10},
        )
        liked_response = await client.get("/api/v1/memes/trends", params={"ranking": "most_liked", "limit": 10})
        tag_response = await client.get("/api/v1/memes/trends/tags", params={"limit": 10})
        template_response = await client.get("/api/v1/memes/trends/templates", params={"limit": 10})
        popularity_response = await client.get(f"/api/v1/memes/{snapshot_meme.id}/popularity")
    finally:
        app.dependency_overrides.clear()

    assert trending_response.status_code == 200
    assert rising_response.status_code == 200
    assert liked_response.status_code == 200
    assert tag_response.status_code == 200
    assert template_response.status_code == 200
    assert popularity_response.status_code == 200
    assert trending_response.json()["items"][0]["meme"]["id"] == str(snapshot_meme.id)
    assert rising_response.json()["items"][0]["meme"]["id"] == str(rising_meme.id)
    assert liked_response.json()["items"][0]["meme"]["id"] == str(liked_meme.id)
    assert liked_response.json()["items"][0]["trend"]["recent"]["likes"] == 3
    assert liked_response.json()["items"][0]["trend"]["recent"]["downloads"] == 2
    assert liked_response.json()["items"][0]["trend"]["previous"]["downloads"] == 0
    assert "payload" not in trending_response.text
    assert "query" not in trending_response.text

    tag_payload = tag_response.json()
    reaction_summary = next(summary for summary in tag_payload if summary["slug"] == "reaction")
    assert reaction_summary["meme_count"] == 3
    assert reaction_summary["trend"]["recent"]["sends"] == 7
    assert reaction_summary["trend"]["recent"]["likes"] == 3
    assert reaction_summary["trend"]["recent"]["downloads"] == 2

    template_payload = template_response.json()
    assert template_payload[0]["slug"] == "frog-template"
    assert template_payload[0]["meme_count"] == 1
    assert template_payload[0]["trend"]["latest_source_views"] == 20
    assert template_payload[0]["trend"]["recent"]["downloads"] == 0

    popularity_payload = popularity_response.json()
    assert popularity_payload["meme_id"] == str(snapshot_meme.id)
    assert [point["popularity_score"] for point in popularity_payload["sparkline"]] == [100.0, 200.0]
    assert popularity_payload["trend"]["latest_source_views"] == 20


async def test_public_trend_views_count_strict_writer_nested_meme_refs(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, tags=["trend"], popularity_score=1.0)
    analytics_service = AnalyticsService(migrated_db_session)
    await analytics_service.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_DOWNLOAD,
            surface="public_api",
            refs=InteractionEventRefs(meme_id=meme.id),
            properties={"chat_hash": "hashed-chat"},
        )
    )
    await _refresh_trend_views(migrated_db_session)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        response = await client.get(f"/api/v1/memes/{meme.id}/popularity")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["trend"]["recent"]["downloads"] == 1


async def test_public_trend_empty_states_are_honest(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _refresh_trend_views(migrated_db_session)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        trends_response = await client.get("/api/v1/memes/trends")
        tags_response = await client.get("/api/v1/memes/trends/tags")
        templates_response = await client.get("/api/v1/memes/trends/templates")
    finally:
        app.dependency_overrides.clear()

    assert trends_response.status_code == 200
    assert trends_response.json() == {"items": [], "limit": 20, "offset": 0, "total": 0, "has_more": False}
    assert tags_response.status_code == 200
    assert tags_response.json() == []
    # Static trend routes must be matched before dynamic /{meme_id} detail routes.
    assert tags_response.headers["content-type"].startswith("application/json")
    assert templates_response.status_code == 200
    assert templates_response.json() == []
    assert templates_response.headers["content-type"].startswith("application/json")

    meme = await _create_meme(migrated_db_session)
    await _refresh_trend_views(migrated_db_session)
    _install_meme_route_overrides(app, migrated_db_session)
    try:
        popularity_response = await client.get(f"/api/v1/memes/{meme.id}/popularity")
    finally:
        app.dependency_overrides.clear()

    assert popularity_response.status_code == 200
    popularity_payload = popularity_response.json()
    assert popularity_payload["trend"] is not None
    assert popularity_payload["sparkline"] == []


async def test_public_trend_compare_returns_real_series_and_insufficient_history_states(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    template = MemeTemplate(slug="frog-template", name="Frog Template", description="Frog format")
    migrated_db_session.add(template)
    await migrated_db_session.flush()
    meme = await _create_meme(migrated_db_session, tags=["reaction"], popularity_score=1.0)
    meme.template_id = template.id
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=meme.id,
            slug="launch-reaction",
            page_title="Launch reaction meme",
            meta_description="Launch reaction",
            alt_text="Launch reaction",
            model_id="test",
            prompt_version="v1",
        )
    )
    migrated_db_session.add_all(
        [
            MemePopularitySnapshot(
                meme_id=meme.id,
                captured_at=datetime(2026, 1, 1, tzinfo=UTC),
                popularity_score=10.0,
                source_views=10,
            ),
            MemePopularitySnapshot(
                meme_id=meme.id,
                captured_at=datetime(2026, 1, 2, tzinfo=UTC),
                popularity_score=15.0,
                source_views=15,
            ),
        ]
    )
    await migrated_db_session.flush()
    await _refresh_trend_views(migrated_db_session)
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        response = await client.get(
            "/api/v1/memes/trends/compare",
            params=[
                ("item", "meme:launch-reaction"),
                ("item", "tag:reaction"),
                ("item", "template:frog-template"),
                ("item", "bad-spec"),
            ],
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["max_items"] == 6
    assert payload["requested_items"] == ["meme:launch-reaction", "tag:reaction", "template:frog-template", "bad-spec"]

    meme_series = payload["items"][0]
    assert meme_series["kind"] == "meme"
    assert meme_series["title"] == "Launch reaction meme"
    assert meme_series["insufficient_history"] is False
    assert [point["value"] for point in meme_series["points"]] == [10.0, 15.0]
    assert {point["metric"] for point in meme_series["points"]} == {"popularity_score"}

    tag_series = payload["items"][1]
    assert tag_series["kind"] == "tag"
    assert tag_series["insufficient_history"] is True
    assert tag_series["points"][0]["metric"] == "trending_score"
    assert tag_series["points"][0]["value"] > 0

    template_series = payload["items"][2]
    assert template_series["kind"] == "template"
    assert template_series["insufficient_history"] is True
    assert template_series["points"][0]["metric"] == "trending_score"

    invalid_series = payload["items"][3]
    assert invalid_series["kind"] == "unknown"
    assert invalid_series["points"] == []
    assert invalid_series["no_data_reason"].startswith("Use item specs")


async def test_public_trend_timeline_groups_real_snapshot_periods_without_private_memes(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    january_meme = await _create_meme(migrated_db_session, popularity_score=1.0)
    february_meme = await _create_meme(migrated_db_session, popularity_score=1.0)
    private_meme = await _create_meme(migrated_db_session, popularity_score=100.0, is_public=False)
    migrated_db_session.add_all(
        [
            MemePopularitySnapshot(
                meme_id=january_meme.id,
                captured_at=datetime(2026, 1, 15, tzinfo=UTC),
                popularity_score=20.0,
                source_views=200,
            ),
            MemePopularitySnapshot(
                meme_id=february_meme.id,
                captured_at=datetime(2026, 2, 10, tzinfo=UTC),
                popularity_score=30.0,
                source_views=100,
                platform_views=40,
            ),
            MemePopularitySnapshot(
                meme_id=private_meme.id,
                captured_at=datetime(2026, 2, 11, tzinfo=UTC),
                popularity_score=999.0,
                source_views=999,
            ),
        ]
    )
    await migrated_db_session.flush()
    _install_meme_route_overrides(app, migrated_db_session)

    try:
        month_response = await client.get("/api/v1/memes/trends/timeline", params={"granularity": "month"})
        year_response = await client.get("/api/v1/memes/trends/timeline", params={"granularity": "year"})
    finally:
        app.dependency_overrides.clear()

    assert month_response.status_code == 200
    month_payload = month_response.json()
    assert month_payload["granularity"] == "month"
    assert [period["period"] for period in month_payload["periods"]] == ["2026-02", "2026-01"]
    assert month_payload["periods"][0]["top_memes"][0]["meme"]["id"] == str(february_meme.id)
    assert month_payload["periods"][0]["top_memes"][0]["popularity_score"] == 30.0
    assert str(private_meme.id) not in month_response.text

    assert year_response.status_code == 200
    year_payload = year_response.json()
    assert year_payload["granularity"] == "year"
    assert year_payload["periods"][0]["period"] == "2026"
    assert [item["meme"]["id"] for item in year_payload["periods"][0]["top_memes"]] == [
        str(february_meme.id),
        str(january_meme.id),
    ]


async def test_public_trend_refresh_command_path_uses_concurrent_refresh_with_fallback(
    postgres_async_engine: AsyncEngine,
    migrated_db_session: AsyncSession,
) -> None:
    _ = migrated_db_session
    await refresh_public_trend_materialized_views(postgres_async_engine, concurrently=True)


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
    assert await detail_status(private_meme.id, _user_read(author)) == 404
    assert await detail_status(nsfw_meme.id, _user_read(author)) == 404
    assert await detail_status(nsfw_meme.id, _user_read(author), include_nsfw=True) == 200

    event = await migrated_db_session.scalar(
        select(AnalyticsEvent)
        .where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_VIEW)
        .order_by(AnalyticsEvent.occurred_at.desc())
    )
    assert event is not None
    assert event.payload["surface"] == "public_api"
    assert event.payload["meme_id"] == str(nsfw_meme.id)


async def test_operator_launch_kpis_count_events_source_metrics_and_conversions(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user = User(account_type=AccountType.FULL)
    guest = User(account_type=AccountType.GUEST)
    target = User(account_type=AccountType.FULL)
    migrated_db_session.add_all([user, guest, target])
    await migrated_db_session.flush()
    meme = await _create_meme(migrated_db_session)
    migrated_db_session.add_all(
        [
            AnalyticsEvent(user_id=user.id, event_type=AnalyticsEventType.SEARCH_QUERY, payload={"query": "cats"}),
            AnalyticsEvent(user_id=user.id, event_type=AnalyticsEventType.MEME_VIEW, payload={"meme_id": str(meme.id)}),
            AnalyticsEvent(user_id=user.id, event_type=AnalyticsEventType.MEME_SEND, payload={"meme_id": str(meme.id)}),
            AnalyticsEvent(user_id=user.id, event_type=AnalyticsEventType.MEME_LIKE, payload={"meme_id": str(meme.id)}),
            AnalyticsEvent(user_id=None, event_type=AnalyticsEventType.MEME_SAVE, payload={"meme_id": str(meme.id)}),
            AccountMergeLog(
                guest_account_id=guest.id,
                target_account_id=target.id,
                favorites_transferred=1,
                views_transferred=2,
            ),
            MemePopularitySnapshot(
                meme_id=meme.id,
                source_views=100,
                source_reactions=7,
                source_reposts=3,
                popularity_score=10.0,
            ),
        ]
    )
    await migrated_db_session.flush()
    operator_token = get_settings().pipeline_operator_token.get_secret_value()

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    app.dependency_overrides[get_analytics_service] = override_analytics_service
    try:
        response = await client.get(
            "/api/v1/pipeline/launch-kpis",
            headers={PIPELINE_OPERATOR_TOKEN_HEADER_NAME: operator_token},
            params={"lookback_hours": 24},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["lookback_hours"] == 24
    assert payload["searches"] == 1
    assert payload["views"] == 1
    assert payload["sends"] == 1
    assert payload["active_users"] == 1
    assert payload["likes"] == 1
    assert payload["saves"] == 1
    assert payload["guest_to_full_conversions"] == 1
    assert payload["source_views"] == 100
    assert payload["source_reactions"] == 7
    assert payload["source_reposts"] == 3


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
