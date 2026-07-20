"""Integration tests for favorites, active-save, and pin API hooks."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.collection import get_collection_service
from memexpert.api.dependencies.meme import get_analytics_service, get_meme_report_service, get_meme_search_service
from memexpert.api.routes import _meme_interactions as meme_interaction_routes
from memexpert.models.collection import CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage, ContentProcessingStatus
from memexpert.models.user import AnalyticsEvent
from memexpert.schemas.meme import MemeResultAttributionRead
from memexpert.schemas.user import UserRead
from memexpert.services import CollectionService, UserService
from memexpert.services.analytics import AnalyticsService
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.recommendations.attribution import AttributionTokenService
from memexpert.services.report import MemeReportService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    is_nsfw: bool = False,
) -> Meme:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.EN,
        is_public=is_public,
        is_nsfw=is_nsfw,
    )
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"pipeline/originals/{meme_id}.jpg",
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    return meme


def _attribution_payload(source_meme_id: uuid.UUID) -> dict[str, object]:
    return {
        "request_id": "req-route-attribution",
        "impression_id": "imp-route-attribution",
        "surface": "public_api_meme_similar",
        "source_algorithm": "qdrant_similarity",
        "rank": 4,
        "query": "frog",
        "filters": {
            "language": "en",
            "media_type": "image",
            "include_nsfw": False,
            "tags": ["frog"],
            "scope": "public",
            "collection_ids": [],
        },
        "collection_scope": "public",
        "collection_ids": [],
        "source_meme_id": str(source_meme_id),
        "algorithm_version": "similar-v1",
        "score": 0.88,
        "score_components": {"similarity": 0.88, "total": 0.88},
        "reason": "similarity_match",
    }


def _attribution_query_params(source_meme_id: uuid.UUID) -> dict[str, str]:
    payload = _attribution_payload(source_meme_id)
    return {
        "attribution_request_id": str(payload["request_id"]),
        "attribution_impression_id": str(payload["impression_id"]),
        "attribution_surface": str(payload["surface"]),
        "attribution_source_algorithm": str(payload["source_algorithm"]),
        "attribution_rank": str(payload["rank"]),
        "attribution_query": str(payload["query"]),
        "attribution_filters": json.dumps(payload["filters"]),
        "attribution_collection_scope": str(payload["collection_scope"]),
        "attribution_source_meme_id": str(source_meme_id),
        "attribution_algorithm_version": str(payload["algorithm_version"]),
        "attribution_score": str(payload["score"]),
        "attribution_score_components": json.dumps(payload["score_components"]),
        "attribution_reason": str(payload["reason"]),
    }


async def test_favorite_save_routes_auto_bootstrap_guest_session(
    auth_client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    first_meme = await _create_meme(migrated_db_session)
    second_meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    anonymous_token = AttributionTokenService.from_settings().issue_for_result(
        meme_id=first_meme.id,
        viewer_user_id=None,
        attribution=MemeResultAttributionRead(
            request_id="req-anonymous-search",
            impression_id="imp-anonymous-search",
            surface="web_search",
            source_algorithm="hybrid_search",
            rank=1,
            algorithm_version="hybrid_v1",
        ),
    )
    favorite_response = await auth_client.post(
        f"/api/v1/memes/{first_meme.id}/favorite",
        json={"attribution_token": anonymous_token},
    )
    repeated_favorite_response = await auth_client.post(f"/api/v1/memes/{first_meme.id}/favorite")
    save_response = await auth_client.post(f"/api/v1/memes/{second_meme.id}/save")
    favorites_response = await auth_client.get("/api/v1/memes/favorites")

    assert favorite_response.status_code == 200
    assert "memexpert_access_token" in favorite_response.headers["set-cookie"]
    assert favorite_response.json() == {"favorited": True, "changed": True, "like_count": 1}
    assert repeated_favorite_response.status_code == 200
    assert repeated_favorite_response.json() == {"favorited": True, "changed": False, "like_count": 1}
    assert save_response.status_code == 200
    assert favorites_response.status_code == 200
    assert {item["meme_id"] for item in favorites_response.json()} == {str(first_meme.id), str(second_meme.id)}
    assert await migrated_db_session.scalar(select(func.count()).select_from(CollectionMeme)) == 2
    favorite_event = await migrated_db_session.scalar(
        select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_LIKE)
    )
    assert favorite_event is not None
    assert favorite_event.payload["request_id"] == "req-anonymous-search"
    assert favorite_event.payload["impression_id"] == "imp-anonymous-search"
    assert favorite_event.payload["surface"] == "web_search"
    assert cast("dict[str, object]", favorite_event.payload["properties"])["attribution_trusted"] is True


async def test_library_route_auto_bootstraps_guest_and_returns_empty_profile(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.get("/api/v1/memes/library")

    assert response.status_code == 200
    assert "memexpert_access_token" in response.headers["set-cookie"]
    payload = response.json()
    assert payload["favorites"] == []
    assert payload["pinned_memes"] == []
    assert payload["active_save_collection"]["title"] == "Favorites"
    assert [(collection["title"], collection["saved_meme_count"]) for collection in payload["collections"]] == [
        ("Favorites", 0)
    ]


async def test_full_account_routes_update_active_collection_and_manage_pins(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()
    full_user = await create_full_user_via_upgrade(user_service, email="routes@example.com")
    custom = await collection_service.create_custom_collection(owner_user_id=full_user.id, title="API Saves")
    first_meme = await _create_meme(migrated_db_session)
    second_meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    current_user = UserRead.model_validate(full_user)

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        active_response = await client.put(
            "/api/v1/memes/active-save-collection",
            json={"collection_id": str(custom.id)},
        )
        save_response = await client.post(f"/api/v1/memes/{first_meme.id}/save")
        pin_response = await client.post(f"/api/v1/memes/{first_meme.id}/pin")
        _ = await client.post(f"/api/v1/memes/{second_meme.id}/pin")
        reorder_response = await client.put(
            "/api/v1/memes/pins/reorder",
            json={"meme_ids": [str(second_meme.id), str(first_meme.id)]},
        )

        current_user = UserRead.model_validate(guest)
        guest_pin_response = await client.post(f"/api/v1/memes/{first_meme.id}/pin")
        guest_active_response = await client.put(
            "/api/v1/memes/active-save-collection",
            json={"collection_id": str(custom.id)},
        )
    finally:
        app.dependency_overrides.clear()

    assert active_response.status_code == 200
    assert active_response.json()["active_save_collection_id"] == str(custom.id)
    assert save_response.status_code == 200
    assert save_response.json()["collection_id"] == str(custom.id)
    assert pin_response.status_code == 200
    assert [pin["meme_id"] for pin in reorder_response.json()] == [str(second_meme.id), str(first_meme.id)]
    assert guest_pin_response.status_code == 403
    assert guest_active_response.status_code == 403

    persisted_positions = await migrated_db_session.scalars(
        select(PinnedMeme.meme_id).where(PinnedMeme.user_id == full_user.id).order_by(PinnedMeme.position.asc())
    )
    assert list(persisted_positions) == [second_meme.id, first_meme.id]


async def test_detail_and_successful_actions_persist_strict_attribution_events(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    full_user = await create_full_user_via_upgrade(user_service, email="attribution-actions@example.com")
    collection_service = CollectionService(migrated_db_session)
    active_collection = await collection_service.create_custom_collection(
        owner_user_id=full_user.id,
        title="Attribution saves",
    )
    _ = await collection_service.update_active_save_collection(
        user_id=full_user.id,
        collection_id=active_collection.id,
    )
    source_meme = await _create_meme(migrated_db_session)
    target_meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    current_user = full_user

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    def override_report_service() -> MemeReportService:
        return MemeReportService(session=migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    action_payload = {"attribution": _attribution_payload(source_meme.id)}
    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_meme_report_service] = override_report_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        detail_response = await client.get(
            f"/api/v1/memes/{target_meme.id}",
            params=_attribution_query_params(source_meme.id),
        )
        view_response = await client.post(f"/api/v1/memes/{target_meme.id}/view", json=action_payload)
        impression_response = await client.post(f"/api/v1/memes/{target_meme.id}/impression", json=action_payload)
        detail_click_response = await client.post(f"/api/v1/memes/{target_meme.id}/detail-click", json=action_payload)
        favorite_response = await client.post(f"/api/v1/memes/{target_meme.id}/favorite", json=action_payload)
        repeated_favorite_response = await client.post(f"/api/v1/memes/{target_meme.id}/favorite", json=action_payload)
        save_response = await client.post(f"/api/v1/memes/{target_meme.id}/save", json=action_payload)
        repeated_save_response = await client.post(f"/api/v1/memes/{target_meme.id}/save", json=action_payload)
        pin_response = await client.post(f"/api/v1/memes/{target_meme.id}/pin", json=action_payload)
        repeated_pin_response = await client.post(f"/api/v1/memes/{target_meme.id}/pin", json=action_payload)
        report_response = await client.post(
            f"/api/v1/memes/{target_meme.id}/report",
            json={"reason": "spam", "attribution": action_payload["attribution"]},
        )
        share_response = await client.post(f"/api/v1/memes/{target_meme.id}/share", json=action_payload)
        download_response = await client.post(f"/api/v1/memes/{target_meme.id}/download", json=action_payload)
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    assert view_response.json() == {"ok": True}
    assert impression_response.json() == {"ok": True}
    assert detail_click_response.json() == {"ok": True}
    assert favorite_response.status_code == 200
    assert repeated_favorite_response.json()["changed"] is False
    assert save_response.status_code == 200
    assert save_response.json()["collection_id"] == str(active_collection.id)
    assert repeated_save_response.status_code == 200
    assert pin_response.status_code == 200
    assert repeated_pin_response.status_code == 200
    assert report_response.status_code == 200
    assert share_response.json() == {"ok": True}
    assert download_response.json() == {"ok": True}

    events = list(
        (
            await migrated_db_session.execute(
                select(AnalyticsEvent).order_by(AnalyticsEvent.occurred_at.asc(), AnalyticsEvent.id.asc())
            )
        ).scalars()
    )
    assert [event.event_type for event in events] == [
        AnalyticsEventType.MEME_VIEW,
        AnalyticsEventType.MEME_IMPRESSION,
        AnalyticsEventType.MEME_DETAIL_CLICK,
        AnalyticsEventType.MEME_LIKE,
        AnalyticsEventType.MEME_SAVE,
        AnalyticsEventType.MEME_PIN,
        AnalyticsEventType.MEME_REPORT,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.MEME_DOWNLOAD,
    ]
    for event in events:
        assert event.user_id == full_user.id
        assert event.payload["request_id"] == "req-route-attribution"
        assert event.payload["impression_id"] == "imp-route-attribution"
        assert event.payload["surface"] == "public_api_meme_similar"
        assert event.payload["source_algorithm"] == "qdrant_similarity"
        assert event.payload["rank"] == 4
        assert event.payload["score"] == 0.88
        assert event.payload["score_components"] == {"similarity": 0.88, "total": 0.88}
        assert event.payload["reason"] == "similarity_match"
        refs = cast("dict[str, object]", event.payload["refs"])
        properties = cast("dict[str, object]", event.payload["properties"])
        assert refs["meme_id"] == str(target_meme.id)
        assert refs["source_meme_id"] == str(source_meme.id)
        assert properties["algorithm_version"] == "similar-v1"
        assert properties["filters"] == {
            "language": "en",
            "media_type": "image",
            "include_nsfw": False,
            "tags": ["frog"],
            "scope": "public",
            "collection_ids": [],
        }


async def test_share_download_telemetry_respects_private_and_nsfw_visibility(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    _ = await create_full_user_via_upgrade(user_service, email="telemetry-author@example.com")
    stranger = await create_full_user_via_upgrade(user_service, email="telemetry-stranger@example.com")
    private_meme = await _create_meme(migrated_db_session, is_public=False)
    nsfw_meme = await _create_meme(migrated_db_session, is_nsfw=True)
    await migrated_db_session.commit()

    current_user = stranger

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        private_share_response = await client.post(f"/api/v1/memes/{private_meme.id}/share", json={})
        private_view_response = await client.post(f"/api/v1/memes/{private_meme.id}/view", json={})
        private_impression_response = await client.post(f"/api/v1/memes/{private_meme.id}/impression", json={})
        private_detail_click_response = await client.post(f"/api/v1/memes/{private_meme.id}/detail-click", json={})
        private_download_response = await client.post(f"/api/v1/memes/{private_meme.id}/download", json={})
        nsfw_share_response = await client.post(f"/api/v1/memes/{nsfw_meme.id}/share", json={})
        nsfw_view_response = await client.post(f"/api/v1/memes/{nsfw_meme.id}/view", json={})
        nsfw_impression_response = await client.post(f"/api/v1/memes/{nsfw_meme.id}/impression", json={})
        nsfw_detail_click_response = await client.post(f"/api/v1/memes/{nsfw_meme.id}/detail-click", json={})
        nsfw_download_response = await client.post(f"/api/v1/memes/{nsfw_meme.id}/download", json={})
    finally:
        app.dependency_overrides.clear()

    assert private_share_response.status_code == 404
    assert private_view_response.status_code == 404
    assert private_impression_response.status_code == 404
    assert private_detail_click_response.status_code == 404
    assert private_download_response.status_code == 404
    assert nsfw_share_response.status_code == 404
    assert nsfw_view_response.status_code == 404
    assert nsfw_impression_response.status_code == 404
    assert nsfw_detail_click_response.status_code == 404
    assert nsfw_download_response.status_code == 404
    assert await migrated_db_session.scalar(select(func.count()).select_from(AnalyticsEvent)) == 0


async def test_action_succeeds_when_analytics_writer_fails(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
    monkeypatch,
) -> None:
    user_service = UserService(migrated_db_session)
    full_user = await create_full_user_via_upgrade(user_service, email="analytics-failure-action@example.com")
    meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    class FailingAnalyticsService:
        async def record_interaction_event(self, _event: object) -> None:
            raise RuntimeError("analytics writer unavailable")

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_analytics_service() -> FailingAnalyticsService:
        return FailingAnalyticsService()

    async def override_current_user() -> UserRead | None:
        return full_user

    log_calls: list[dict[str, object]] = []

    def capture_exception(_message: str, *, extra: dict[str, object]) -> None:
        log_calls.append(extra)

    monkeypatch.setattr(meme_interaction_routes.logger, "exception", capture_exception)
    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        response = await client.post(
            f"/api/v1/memes/{meme.id}/save",
            json={"attribution": _attribution_payload(meme.id)},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert await migrated_db_session.scalar(select(func.count()).select_from(CollectionMeme)) == 1
    assert await migrated_db_session.scalar(select(func.count()).select_from(AnalyticsEvent)) == 0

    assert log_calls == [
        {
            "analytics_event_type": AnalyticsEventType.MEME_SAVE.value,
            "meme_id": str(meme.id),
            "user_id": str(full_user.id),
            "request_id": "req-route-attribution",
            "impression_id": "imp-route-attribution",
            "surface": "public_api_meme_similar",
        }
    ]


async def test_library_route_returns_cards_collections_and_active_save_state(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    full_user = await create_full_user_via_upgrade(user_service, email="route-library@example.com")
    favorite_meme = await _create_meme(migrated_db_session)
    pinned_meme = await _create_meme(migrated_db_session)
    _ = await collection_service.favorite_meme(user_id=full_user.id, meme_id=favorite_meme.id)
    custom = await collection_service.create_custom_collection(owner_user_id=full_user.id, title="API Saves")
    _ = await collection_service.update_active_save_collection(user_id=full_user.id, collection_id=custom.id)
    _ = await collection_service.pin_meme(user_id=full_user.id, meme_id=pinned_meme.id)

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return UserRead.model_validate(full_user)

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        response = await client.get("/api/v1/memes/library")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [meme["id"] for meme in payload["favorites"]] == [str(favorite_meme.id)]
    assert [meme["id"] for meme in payload["pinned_memes"]] == [str(pinned_meme.id)]
    assert payload["favorites"][0]["viewer_has_favorited"] is True
    assert payload["favorites"][0]["viewer_has_saved"] is False
    assert payload["pinned_memes"][0]["viewer_has_pinned"] is True
    assert payload["active_save_collection"]["id"] == str(custom.id)
    assert [collection["title"] for collection in payload["collections"]] == ["Favorites", "API Saves"]


async def test_library_routes_reject_private_memes_not_visible_to_user(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    _ = await create_full_user_via_upgrade(user_service, email="route-private-author@example.com")
    stranger = await create_full_user_via_upgrade(user_service, email="route-private-stranger@example.com")
    private_meme = await _create_meme(migrated_db_session, is_public=False)
    await migrated_db_session.commit()

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return UserRead.model_validate(stranger)

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        favorite_response = await client.post(f"/api/v1/memes/{private_meme.id}/favorite")
        save_response = await client.post(f"/api/v1/memes/{private_meme.id}/save")
        pin_response = await client.post(f"/api/v1/memes/{private_meme.id}/pin")
    finally:
        app.dependency_overrides.clear()

    assert favorite_response.status_code == 404
    assert save_response.status_code == 404
    assert pin_response.status_code == 404
    assert await migrated_db_session.scalar(select(func.count()).select_from(CollectionMeme)) == 0
    assert await migrated_db_session.scalar(select(func.count()).select_from(PinnedMeme)) == 0
    assert await migrated_db_session.scalar(select(Meme.like_count).where(Meme.id == private_meme.id)) == 0
