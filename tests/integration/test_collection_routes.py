"""Integration tests for collection management API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.collection import get_collection_service
from memexpert.api.dependencies.meme import get_meme_search_service
from memexpert.models.content import Meme
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.user import UserRead
from memexpert.services import CollectionService, MemeSearchService, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    import uuid

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_meme(session: AsyncSession, *, author_user_id: uuid.UUID | None = None) -> Meme:
    meme = Meme(
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        is_public=True,
        author_user_id=author_user_id,
    )
    session.add(meme)
    await session.flush()
    return meme


async def test_collection_routes_crud_detail_remove_active_and_invites(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, telegram_id=1001, email="owner@example.com")
    viewer = await create_full_user_via_upgrade(user_service, telegram_id=1002, email="viewer@example.com")
    guest = await user_service.create_guest_user()
    meme = await _create_meme(migrated_db_session, author_user_id=owner.id)
    await migrated_db_session.commit()

    current_user = UserRead.model_validate(owner)

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        create_response = await client.post(
            "/api/v1/collections",
            json={"title": "Team Saves", "description": "For launch", "visibility": "private"},
        )
        collection_id = create_response.json()["collection"]["id"]

        update_response = await client.patch(
            f"/api/v1/collections/{collection_id}",
            json={"title": "Launch Saves", "description": "Renamed", "visibility": "unlisted"},
        )
        active_response = await client.put(f"/api/v1/collections/{collection_id}/active-save")
        save_response = await client.post(f"/api/v1/collections/{collection_id}/memes/{meme.id}")
        detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        invite_response = await client.post(
            f"/api/v1/collections/{collection_id}/invites",
            json={"role": "viewer", "label": "QA", "max_uses": 2, "expires_in_hours": 24},
        )

        current_user = UserRead.model_validate(viewer)
        join_response = await client.post(f"/api/v1/collections/invites/{invite_response.json()['token']}/join")
        viewer_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        viewer_remove_response = await client.delete(f"/api/v1/collections/{collection_id}/memes/{meme.id}")
        forbidden_delete_response = await client.delete(f"/api/v1/collections/{collection_id}")

        current_user = UserRead.model_validate(guest)
        guest_create_response = await client.post("/api/v1/collections", json={"title": "Guest board"})

        current_user = UserRead.model_validate(owner)
        remove_response = await client.delete(f"/api/v1/collections/{collection_id}/memes/{meme.id}")
        delete_response = await client.delete(f"/api/v1/collections/{collection_id}")
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 201
    assert update_response.status_code == 200
    assert update_response.json()["collection"]["title"] == "Launch Saves"
    assert active_response.status_code == 200
    assert active_response.json()["active_save_collection_id"] == collection_id
    assert save_response.status_code == 200
    assert detail_response.status_code == 200
    assert [item["meme"]["id"] for item in detail_response.json()["saved_memes"]] == [str(meme.id)]
    assert detail_response.json()["saved_memes"][0]["meme"]["viewer_has_saved"] is True
    assert invite_response.status_code == 200
    assert invite_response.json()["join_path"].startswith("/collection/invite/")
    assert forbidden_delete_response.status_code == 403
    assert join_response.status_code == 200
    assert viewer_detail_response.status_code == 200
    assert viewer_detail_response.json()["viewer_role"] == "viewer"
    assert viewer_detail_response.json()["capabilities"]["can_remove_memes"] is False
    assert viewer_remove_response.status_code == 403
    assert guest_create_response.status_code == 403
    assert remove_response.status_code == 200
    assert remove_response.json()["removed"] is True
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] is True
