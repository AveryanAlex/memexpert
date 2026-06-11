# ruff: noqa: TC002,TC003
"""Focused tests for the shared hybrid meme search service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from memexpert.api.dependencies import PIPELINE_OPERATOR_TOKEN_HEADER_NAME
from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_analytics_service, get_meme_search_service
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
from memexpert.services.analytics import AnalyticsService
from memexpert.services.media_render_urls import MediaRenderUrlService
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters, MemeSearchService
from memexpert.services.meme_seo import MemeSeoGenerationService, MemeSeoProviderResult

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


class FakeSeoProvider:
    model_id = "fake-seo-model"
    prompt_version = "seo-test-v1"

    def __init__(self, payloads: list[MemeSeoProviderResult]) -> None:
        self._payloads = payloads
        self.calls: list[uuid.UUID] = []

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        self.calls.append(meme.id)
        return self._payloads.pop(0)


class FailingSeoProvider:
    model_id = "fake-failing-model"
    prompt_version = "seo-test-v1"

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        raise RuntimeError("provider-secret-seo-failure")


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

    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
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
    assert "/api/v1/memes/slug/{slug}" in paths
    assert "/api/v1/memes/tags/{tag_slug}" in paths
    assert "/api/v1/memes/templates/{template_slug}" in paths
    assert "/api/v1/memes/{meme_id}/canonical" in paths
    assert "/api/v1/memes/{meme_id}" in paths
    search_parameters = {parameter["name"] for parameter in paths["/api/v1/memes/search"]["get"]["parameters"]}
    assert "query" in search_parameters
    assert "query_vector" not in search_parameters
    assert set(components["PublicMemeSearchResultRead"]["properties"]) == {"meme"}
    assert "viewer_has_favorited" in components["PublicMemeCardRead"]["properties"]
    assert "viewer_has_saved" in components["PublicMemeCardRead"]["properties"]
    assert "viewer_has_pinned" in components["PublicMemeCardRead"]["properties"]
    assert "MemeSearchScoreRead" not in components
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

    def page_states(page: object) -> dict[uuid.UUID, tuple[bool, bool, bool]]:
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
                tags=("frog", "wizard", "frog"),
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
    service = MemeSeoGenerationService(migrated_db_session, provider=FailingSeoProvider())

    result = await service.generate_for_meme_id(meme.id, commit=False)

    assert result.status == "failed"
    assert result.reason == "provider_error"
    assert "provider-secret" not in repr(result)
    assert await migrated_db_session.get(MemeSeoPage, meme.id) is None


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
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(event_meme.id), "telegram_user_hash": "hashed-user"},
            ),
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_LIKE,
                payload={"meme_id": str(event_meme.id), "telegram_user_hash": "hashed-user"},
            ),
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(private_meme.id), "telegram_user_id": 12345},
            ),
            MemePopularitySnapshot(meme_id=snapshot_meme.id, popularity_score=100.0, source_views=50),
        ]
    )
    await migrated_db_session.flush()
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
