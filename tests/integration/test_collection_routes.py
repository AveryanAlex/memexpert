"""Integration tests for collection management API routes."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from sqlalchemy import select

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.collection import get_collection_service
from memexpert.api.dependencies.meme import get_analytics_service, get_meme_search_service
from memexpert.api.routes.v1 import media as media_routes
from memexpert.core.database import get_db_session
from memexpert.core.storage import media_object_version_token
from memexpert.models.collection import CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.user import UserRead
from memexpert.services import CollectionService, MemeSearchService, UserService
from memexpert.services.analytics import AnalyticsService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession


class FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_presigned_url(self, operation: str, *, Params: dict[str, str], ExpiresIn: int) -> str:
        self.calls.append({"operation": operation, "params": Params, "expires_in": ExpiresIn})
        return f"https://s3.memexpert.test/{Params['Key']}"


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    mime_type: str = "image/jpeg",
    s3_original_key: str | None = None,
    s3_web_video_key: str | None = None,
) -> Meme:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        is_public=is_public,
    )
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        s3_original_key=s3_original_key or f"pipeline/originals/test/{meme_id}.jpg",
        s3_web_video_key=s3_web_video_key,
        mime_type=mime_type,
        width=640,
        height=480,
        quality_score=0.8,
    )
    session.add(meme)
    await session.flush()
    session.add(file)
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
    meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    current_user = UserRead.model_validate(owner)

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_analytics_service] = override_analytics_service
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
        save_response = await client.post(
            f"/api/v1/collections/{collection_id}/memes/{meme.id}",
            json={
                "attribution": {
                    "request_id": "req_collection_save",
                    "impression_id": "imp_collection_save",
                    "surface": "collection_chooser",
                    "source_algorithm": "hybrid_search",
                    "rank": 2,
                    "collection_scope": "public",
                }
            },
        )
        repeated_save_response = await client.post(f"/api/v1/collections/{collection_id}/memes/{meme.id}")
        owner_choices_response = await client.get(f"/api/v1/collections/meme-choices/{meme.id}")
        detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        invite_response = await client.post(
            f"/api/v1/collections/{collection_id}/invites",
            json={"role": "viewer", "label": "QA", "max_uses": 2, "expires_in_hours": 24},
        )

        current_user = UserRead.model_validate(viewer)
        join_response = await client.post(f"/api/v1/collections/invites/{invite_response.json()['token']}/join")
        viewer_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        viewer_choices_response = await client.get(f"/api/v1/collections/meme-choices/{meme.id}")
        viewer_remove_response = await client.delete(f"/api/v1/collections/{collection_id}/memes/{meme.id}")
        forbidden_delete_response = await client.delete(f"/api/v1/collections/{collection_id}")

        current_user = UserRead.model_validate(guest)
        guest_create_response = await client.post("/api/v1/collections", json={"title": "Guest board"})

        current_user = UserRead.model_validate(owner)
        remove_response = await client.delete(f"/api/v1/collections/{collection_id}/memes/{meme.id}")
        delete_response = await client.delete(f"/api/v1/collections/{collection_id}")
    finally:
        app.dependency_overrides.clear()

    save_events = list(
        await migrated_db_session.scalars(
            select(AnalyticsEvent)
            .where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_SAVE)
            .order_by(AnalyticsEvent.occurred_at, AnalyticsEvent.id)
        )
    )
    save_event = next(
        (
            event
            for event in save_events
            if cast("dict[str, object]", event.payload["properties"])["action"] == "add"
        ),
        None,
    )
    remove_event = next(
        (
            event
            for event in save_events
            if cast("dict[str, object]", event.payload["properties"])["action"] == "remove"
        ),
        None,
    )
    save_event_refs = cast("dict[str, object]", save_event.payload["refs"]) if save_event else {}
    save_event_properties = cast("dict[str, object]", save_event.payload["properties"]) if save_event else {}

    assert create_response.status_code == 201
    assert update_response.status_code == 200
    assert update_response.json()["collection"]["title"] == "Launch Saves"
    assert active_response.status_code == 200
    assert active_response.json()["active_save_collection_id"] == collection_id
    assert save_response.status_code == 200
    assert repeated_save_response.status_code == 200
    assert save_event is not None
    assert save_event.user_id == owner.id
    assert save_event.payload["surface"] == "collection_chooser"
    assert save_event.payload["request_id"] == "req_collection_save"
    assert save_event.payload["impression_id"] == "imp_collection_save"
    assert save_event.payload["source_algorithm"] == "hybrid_search"
    assert save_event.payload["rank"] == 2
    assert save_event_refs["collection_id"] == collection_id
    assert save_event_refs["meme_id"] == str(meme.id)
    assert save_event_properties["action"] == "add"
    assert save_event_properties["preference_kind"] == "save"
    assert save_event_properties["attribution_trusted"] is False
    assert save_event_properties["collection_scope"] == "public"
    assert remove_event is not None
    remove_event_properties = cast("dict[str, object]", remove_event.payload["properties"])
    assert remove_event.user_id == owner.id
    assert remove_event_properties["preference_kind"] == "save"
    assert remove_event_properties["attribution_trusted"] is False
    assert len(save_events) == 2
    assert owner_choices_response.status_code == 200
    assert owner_choices_response.json()["collections"] == [
        {
            "collection_id": collection_id,
            "title": "Launch Saves",
            "contains_meme": True,
            "can_add_memes": True,
            "can_remove_memes": True,
        }
    ]
    assert detail_response.status_code == 200
    assert [item["meme"]["id"] for item in detail_response.json()["saved_memes"]] == [str(meme.id)]
    assert detail_response.json()["saved_memes"][0]["meme"]["viewer_has_saved"] is True
    assert invite_response.status_code == 200
    assert invite_response.json()["join_path"].startswith("/collection/invite/")
    assert forbidden_delete_response.status_code == 403
    assert join_response.status_code == 200
    assert viewer_detail_response.status_code == 200
    assert viewer_detail_response.json()["viewer_role"] == "viewer"
    assert viewer_detail_response.json()["saved_memes"][0]["meme"]["viewer_has_saved"] is True
    assert viewer_choices_response.status_code == 200
    assert viewer_choices_response.json()["collections"] == [
        {
            "collection_id": collection_id,
            "title": "Launch Saves",
            "contains_meme": True,
            "can_add_memes": False,
            "can_remove_memes": False,
        }
    ]
    assert viewer_detail_response.json()["capabilities"]["can_remove_memes"] is False
    assert viewer_remove_response.status_code == 403
    assert guest_create_response.status_code == 403
    assert remove_response.status_code == 200
    assert remove_response.json()["removed"] is True
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] is True


async def test_collection_routes_member_management_invite_revoke_and_capabilities(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, telegram_id=3001, email="route-owner@example.com")
    member = await create_full_user_via_upgrade(user_service, telegram_id=3002, email="route-member@example.com")
    outsider = await create_full_user_via_upgrade(user_service, telegram_id=3003, email="route-outsider@example.com")
    await migrated_db_session.commit()

    current_user = UserRead.model_validate(owner)

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        create_response = await client.post("/api/v1/collections", json={"title": "Managed"})
        collection_id = create_response.json()["collection"]["id"]
        owner_invite_response = await client.post(
            f"/api/v1/collections/{collection_id}/invites",
            json={"role": "viewer", "max_uses": 5, "expires_in_hours": 24},
        )

        current_user = UserRead.model_validate(member)
        join_response = await client.post(
            f"/api/v1/collections/invites/{owner_invite_response.json()['token']}/join"
        )
        viewer_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        viewer_revoke_response = await client.delete(
            f"/api/v1/collections/{collection_id}/invites/{owner_invite_response.json()['invite']['id']}"
        )
        viewer_manage_response = await client.patch(
            f"/api/v1/collections/{collection_id}/members/{member.id}",
            json={"role": "editor"},
        )

        current_user = UserRead.model_validate(owner)
        promote_response = await client.patch(
            f"/api/v1/collections/{collection_id}/members/{member.id}",
            json={"role": "editor"},
        )
        owner_transfer_response = await client.patch(
            f"/api/v1/collections/{collection_id}/members/{owner.id}",
            json={"role": "viewer"},
        )
        owner_remove_response = await client.delete(f"/api/v1/collections/{collection_id}/members/{owner.id}")

        current_user = UserRead.model_validate(member)
        editor_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        editor_invite_response = await client.post(
            f"/api/v1/collections/{collection_id}/invites",
            json={"role": "viewer", "max_uses": 1, "expires_in_hours": 24},
        )
        editor_revoke_response = await client.delete(
            f"/api/v1/collections/{collection_id}/invites/{editor_invite_response.json()['invite']['id']}"
        )

        current_user = UserRead.model_validate(outsider)
        revoked_join_response = await client.post(
            f"/api/v1/collections/invites/{editor_invite_response.json()['token']}/join"
        )
        outsider_manage_response = await client.patch(
            f"/api/v1/collections/{collection_id}/members/{member.id}",
            json={"role": "viewer"},
        )

        current_user = UserRead.model_validate(owner)
        owner_list_response = await client.get("/api/v1/collections")
        owner_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        demote_response = await client.patch(
            f"/api/v1/collections/{collection_id}/members/{member.id}",
            json={"role": "viewer"},
        )
        remove_response = await client.delete(f"/api/v1/collections/{collection_id}/members/{member.id}")

        current_user = UserRead.model_validate(member)
        removed_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 201
    assert create_response.json()["capabilities"]["can_manage_members"] is True
    assert create_response.json()["capabilities"]["can_revoke_invites"] is True
    assert owner_invite_response.status_code == 200
    assert join_response.status_code == 200
    assert viewer_detail_response.status_code == 200
    assert viewer_detail_response.json()["viewer_role"] == "viewer"
    assert viewer_detail_response.json()["capabilities"]["can_manage_members"] is False
    assert viewer_detail_response.json()["capabilities"]["can_revoke_invites"] is False
    assert viewer_revoke_response.status_code == 403
    assert viewer_manage_response.status_code == 403

    assert promote_response.status_code == 200
    assert promote_response.json()["role"] == "editor"
    assert owner_transfer_response.status_code == 409
    assert owner_remove_response.status_code == 409
    assert editor_detail_response.status_code == 200
    assert editor_detail_response.json()["viewer_role"] == "editor"
    assert editor_detail_response.json()["capabilities"]["can_revoke_invites"] is True
    assert editor_detail_response.json()["capabilities"]["can_manage_members"] is False
    assert editor_invite_response.status_code == 200
    assert editor_revoke_response.status_code == 200
    assert editor_revoke_response.json()["status"] == "revoked"
    assert editor_revoke_response.json()["revoked_at"] is not None
    assert revoked_join_response.status_code == 400
    assert outsider_manage_response.status_code == 404

    assert owner_list_response.status_code == 200
    listed_collections = owner_list_response.json()["collections"]
    listed_collection = next(item for item in listed_collections if item["collection"]["id"] == collection_id)
    listed_invites = {invite["id"]: invite for invite in listed_collection["collection"]["invites"]}
    assert listed_invites[editor_invite_response.json()["invite"]["id"]]["status"] == "revoked"
    assert listed_invites[editor_invite_response.json()["invite"]["id"]]["revoked_at"] is not None
    assert owner_detail_response.status_code == 200
    detail_invites = {invite["id"]: invite for invite in owner_detail_response.json()["collection"]["invites"]}
    assert detail_invites[editor_invite_response.json()["invite"]["id"]]["status"] == "revoked"
    assert demote_response.status_code == 200
    assert demote_response.json()["role"] == "viewer"
    assert remove_response.status_code == 200
    assert remove_response.json()["removed"] is True
    assert removed_detail_response.status_code == 404


async def test_meme_pin_reorder_route_persists_display_order(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    full_user = await create_full_user_via_upgrade(user_service, email="pin-route@example.com")
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
        first_pin_response = await client.post(f"/api/v1/memes/{first_meme.id}/pin")
        second_pin_response = await client.post(f"/api/v1/memes/{second_meme.id}/pin")
        reorder_response = await client.put(
            "/api/v1/memes/pins/reorder",
            json={"meme_ids": [str(second_meme.id), str(first_meme.id)]},
        )
    finally:
        app.dependency_overrides.clear()

    assert first_pin_response.status_code == 200
    assert second_pin_response.status_code == 200
    assert reorder_response.status_code == 200
    assert [pin["meme_id"] for pin in reorder_response.json()] == [str(second_meme.id), str(first_meme.id)]
    persisted_positions = await migrated_db_session.scalars(
        select(PinnedMeme.meme_id).where(PinnedMeme.user_id == full_user.id).order_by(PinnedMeme.position.asc())
    )
    assert list(persisted_positions) == [second_meme.id, first_meme.id]


async def test_collection_detail_and_media_route_authorize_private_saved_media(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
    monkeypatch: MonkeyPatch,
) -> None:
    user_service = UserService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, telegram_id=2001, email="private-owner@example.com")
    member = await create_full_user_via_upgrade(user_service, telegram_id=2002, email="private-member@example.com")
    outsider = await create_full_user_via_upgrade(user_service, telegram_id=2003, email="private-outsider@example.com")
    admin = await create_full_user_via_upgrade(user_service, telegram_id=2004, email="private-admin@example.com")
    persisted_admin = await migrated_db_session.get(User, admin.id)
    assert persisted_admin is not None
    persisted_admin.is_admin = True
    owner_favorites = await CollectionService(migrated_db_session).ensure_favorites_collection(owner.id)
    private_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/owner-upload.mov",
        s3_web_video_key="pipeline/derived/private/owner-upload.mp4",
        mime_type="video/quicktime",
    )
    migrated_db_session.add(
        CollectionMeme(
            collection_id=owner_favorites.id,
            meme_id=private_meme.id,
            added_by_user_id=owner.id,
        )
    )
    assert private_meme.primary_file_id is not None
    private_file_id = private_meme.primary_file_id
    active_generation_id = uuid.UUID("22222222-2222-7222-8222-222222222222")
    active_video_key = f"pipeline/derived/{private_file_id}/generations/{active_generation_id}/web.mp4"
    private_file = await migrated_db_session.get(MemeFile, private_file_id)
    assert private_file is not None
    private_file.s3_web_video_key = active_video_key
    await migrated_db_session.commit()

    current_user: UserRead | None = UserRead.model_validate(owner)
    fake_s3_client = FakeS3Client()
    monkeypatch.setattr(media_routes, "get_s3_client", lambda: fake_s3_client)

    def override_collection_service() -> CollectionService:
        return CollectionService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    async def override_db_session() -> AsyncSession:
        return migrated_db_session

    app.dependency_overrides[get_collection_service] = override_collection_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    app.dependency_overrides[get_db_session] = override_db_session
    try:
        create_response = await client.post("/api/v1/collections", json={"title": "Private saves"})
        collection_id = create_response.json()["collection"]["id"]
        save_response = await client.post(f"/api/v1/collections/{collection_id}/memes/{private_meme.id}")
        owner_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        owner_media_response = await client.get(
            f"/api/v1/media/files/{private_file_id}/web-video.mp4",
            follow_redirects=False,
        )

        invite_response = await client.post(
            f"/api/v1/collections/{collection_id}/invites",
            json={"role": "viewer", "max_uses": 1, "expires_in_hours": 24},
        )
        current_user = UserRead.model_validate(member)
        join_response = await client.post(f"/api/v1/collections/invites/{invite_response.json()['token']}/join")
        member_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        member_media_response = await client.get(
            f"/api/v1/media/files/{private_file_id}/preview",
            follow_redirects=False,
        )

        current_user = UserRead.model_validate(outsider)
        outsider_detail_response = await client.get(f"/api/v1/collections/{collection_id}")
        outsider_media_response = await client.get(
            f"/api/v1/media/files/{private_file_id}/preview",
            follow_redirects=False,
        )

        current_user = UserRead.model_validate(persisted_admin)
        admin_media_response = await client.get(
            f"/api/v1/media/files/{private_file_id}/preview",
            follow_redirects=False,
        )

        current_user = None
        anonymous_media_response = await client.get(
            f"/api/v1/media/files/{private_file_id}/preview",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 201
    assert save_response.status_code == 200
    assert owner_detail_response.status_code == 200
    owner_render = owner_detail_response.json()["saved_memes"][0]["meme"]["primary_file"]["render"]
    version = media_object_version_token(active_video_key)
    assert owner_render["thumbnail_url"] == f"/api/v1/media/files/{private_file_id}/thumbnail?v={version}"
    assert owner_render["preview_url"] == f"/api/v1/media/files/{private_file_id}/preview?v={version}"
    assert owner_render["web_video_url"] == f"/api/v1/media/files/{private_file_id}/web-video.mp4?v={version}"
    assert "pipeline/originals/private/owner-upload.mov" not in owner_detail_response.text
    assert active_video_key not in owner_detail_response.text
    assert owner_media_response.status_code == 307
    assert owner_media_response.headers["location"] == f"https://s3.memexpert.test/{active_video_key}"
    assert owner_media_response.headers["cache-control"] == "private, no-store"
    assert owner_media_response.headers["pragma"] == "no-cache"

    assert join_response.status_code == 200
    assert member_detail_response.status_code == 200
    member_render = member_detail_response.json()["saved_memes"][0]["meme"]["primary_file"]["render"]
    assert member_render["preview_url"] == f"/api/v1/media/files/{private_file_id}/preview?v={version}"
    assert member_media_response.status_code == 307
    assert member_media_response.headers["cache-control"] == "private, no-store"
    expected_preview_key = f"pipeline/derived/{private_file_id}/generations/{active_generation_id}/preview.png"
    assert member_media_response.headers["location"] == f"https://s3.memexpert.test/{expected_preview_key}"

    assert outsider_detail_response.status_code == 404
    assert outsider_media_response.status_code == 404
    assert admin_media_response.status_code == 307
    assert admin_media_response.headers["location"] == f"https://s3.memexpert.test/{expected_preview_key}"
    assert admin_media_response.headers["cache-control"] == "private, no-store"
    assert anonymous_media_response.status_code == 401
    first_params = cast("dict[str, str]", fake_s3_client.calls[0]["params"])
    second_params = cast("dict[str, str]", fake_s3_client.calls[1]["params"])
    third_params = cast("dict[str, str]", fake_s3_client.calls[2]["params"])
    assert first_params["Key"] == active_video_key
    assert second_params["Key"] == expected_preview_key
    assert second_params["ResponseContentType"] == "image/png"
    assert third_params["Key"] == expected_preview_key
