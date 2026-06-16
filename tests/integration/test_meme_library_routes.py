"""Integration tests for favorites, active-save, and pin API hooks."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.collection import get_collection_service
from memexpert.models.collection import CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import ContentKind, ContentLanguage, ContentProcessingStatus
from memexpert.schemas.user import UserRead
from memexpert.services import CollectionService, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    author_user_id: uuid.UUID | None = None,
) -> Meme:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.EN,
        is_public=is_public,
        author_user_id=author_user_id,
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


async def test_favorite_save_routes_auto_bootstrap_guest_session(
    auth_client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    first_meme = await _create_meme(migrated_db_session)
    second_meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    favorite_response = await auth_client.post(f"/api/v1/memes/{first_meme.id}/favorite")
    save_response = await auth_client.post(f"/api/v1/memes/{second_meme.id}/save")
    favorites_response = await auth_client.get("/api/v1/memes/favorites")

    assert favorite_response.status_code == 200
    assert "memexpert_access_token" in favorite_response.headers["set-cookie"]
    assert save_response.status_code == 200
    assert favorites_response.status_code == 200
    assert {item["meme_id"] for item in favorites_response.json()} == {str(first_meme.id), str(second_meme.id)}
    assert await migrated_db_session.scalar(select(func.count()).select_from(CollectionMeme)) == 2


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

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_collection_service] = override_collection_service
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
    author = await create_full_user_via_upgrade(user_service, email="route-private-author@example.com")
    stranger = await create_full_user_via_upgrade(user_service, email="route-private-stranger@example.com")
    private_meme = await _create_meme(migrated_db_session, is_public=False, author_user_id=author.id)
    await migrated_db_session.commit()

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return UserRead.model_validate(stranger)

    app.dependency_overrides[get_collection_service] = override_collection_service
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
