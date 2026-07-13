"""Integration tests for collection, membership, invite, and active-save service invariants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from memexpert.models.collection import Collection, CollectionInvite, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
)
from memexpert.models.user import User
from memexpert.services import (
    CollectionNotFoundError,
    CollectionService,
    CollectionVerificationRequiredError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    InvalidCollectionMembershipError,
    InvalidCollectionTitleError,
    InvalidPinnedMemeOrderError,
    MemeNotFoundError,
    PinLimitExceededError,
    UserService,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.transactional_db


class FakeStorageClient:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, object]] = []
        self.fail_deletes = False

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        if self.fail_deletes:
            raise RuntimeError("forced delete failure")
        return {"DeleteMarker": True}


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    s3_original_key: str | None = None,
    s3_web_video_key: str | None = None,
    mime_type: str = "image/jpeg",
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


async def test_guest_cannot_create_custom_collection(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()

    with pytest.raises(GuestCollectionAccessError, match="cannot create custom collections"):
        _ = await collection_service.create_custom_collection(owner_user_id=guest.id, title="Guest board")

    # Under lazy Favorites the guest owns zero collections until first save.
    collection_count_result = await migrated_db_session.execute(select(func.count()).select_from(Collection))
    assert collection_count_result.scalar_one() == 0


async def test_ensure_favorites_collection_lazily_bootstraps_and_is_idempotent(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()

    assert guest.active_save_collection_id is None

    first_call = await collection_service.ensure_favorites_collection(guest.id)
    second_call = await collection_service.ensure_favorites_collection(guest.id)

    assert first_call.id == second_call.id
    assert first_call.kind is CollectionKind.FAVORITES
    assert first_call.title == "Favorites"
    assert [member.role for member in first_call.memberships] == [CollectionMembershipRole.OWNER]
    assert [member.user_id for member in first_call.memberships] == [guest.id]

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(Collection.kind == CollectionKind.FAVORITES)
    )
    assert favorites_count_result.scalar_one() == 1

    persisted_user_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    persisted_user = persisted_user_result.scalar_one()
    assert persisted_user.active_save_collection_id == first_call.id


async def test_full_user_can_create_custom_collection_with_owner_membership(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="owner@example.com")

    created_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="  Work Reactions  ",
        description="  Shared board for the team  ",
        visibility=CollectionVisibility.UNLISTED,
    )

    assert created_collection.kind is CollectionKind.CUSTOM
    assert created_collection.title == "Work Reactions"
    assert created_collection.description == "Shared board for the team"
    assert created_collection.visibility is CollectionVisibility.UNLISTED
    assert len(created_collection.memberships) == 1
    assert created_collection.memberships[0].user_id == owner.id
    assert created_collection.memberships[0].role is CollectionMembershipRole.OWNER

    result = await migrated_db_session.execute(
        select(Collection)
        .options(selectinload(Collection.memberships))
        .where(Collection.id == created_collection.id)
    )
    persisted_collection = result.scalar_one()
    assert persisted_collection.owner_id == owner.id
    assert [membership.role for membership in persisted_collection.memberships] == [CollectionMembershipRole.OWNER]


async def test_collection_list_orders_by_most_recent_meme_addition(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="recent-collections@example.com")
    favorites = await collection_service.ensure_favorites_collection(owner.id)
    older_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Older saves",
    )
    newest_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Newest saves",
    )
    empty_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Empty collection",
    )
    older_meme = await _create_meme(migrated_db_session)
    favorite_meme = await _create_meme(migrated_db_session)
    newest_meme = await _create_meme(migrated_db_session)
    migrated_db_session.add_all(
        [
            CollectionMeme(
                collection_id=older_collection.id,
                meme_id=older_meme.id,
                added_by_user_id=owner.id,
                added_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            CollectionMeme(
                collection_id=favorites.id,
                meme_id=favorite_meme.id,
                added_by_user_id=owner.id,
                added_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            CollectionMeme(
                collection_id=newest_collection.id,
                meme_id=newest_meme.id,
                added_by_user_id=owner.id,
                added_at=datetime(2026, 1, 3, tzinfo=UTC),
            ),
        ]
    )
    await migrated_db_session.commit()

    collections = await collection_service.list_collections_for_user(user_id=owner.id)

    assert [collection.id for collection in collections] == [
        newest_collection.id,
        favorites.id,
        older_collection.id,
        empty_collection.id,
    ]


async def test_custom_collections_do_not_allow_public_visibility_at_launch(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="visibility-owner@example.com")

    with pytest.raises(InvalidCollectionTitleError, match="Public collections"):
        _ = await collection_service.create_custom_collection(
            owner_user_id=owner.id,
            title="Public board",
            visibility=CollectionVisibility.PUBLIC,
        )

    private_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Private board",
    )
    with pytest.raises(InvalidCollectionTitleError, match="Public collections"):
        _ = await collection_service.update_custom_collection(
            collection_id=private_collection.id,
            user_id=owner.id,
            title="Still private",
            visibility=CollectionVisibility.PUBLIC,
        )


async def test_active_save_collection_switching_persists_across_transactions(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="switcher@example.com")

    first_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="First",
    )
    second_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Second",
    )

    first_switch = await collection_service.update_active_save_collection(
        user_id=owner.id,
        collection_id=first_collection.id,
    )
    second_switch = await collection_service.update_active_save_collection(
        user_id=owner.id,
        collection_id=second_collection.id,
    )

    assert first_switch.active_save_collection_id == first_collection.id
    assert second_switch.active_save_collection_id == second_collection.id

    result = await migrated_db_session.execute(select(User).where(User.id == owner.id))
    persisted_user = result.scalar_one()
    assert persisted_user.active_save_collection_id == second_collection.id


async def test_active_save_collection_rejects_non_member_and_viewer_targets(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)

    owner = await create_full_user_via_upgrade(user_service, email="owner@example.com")
    viewer = await create_full_user_via_upgrade(user_service, email="viewer@example.com")
    outsider = await create_full_user_via_upgrade(user_service, email="outsider@example.com")
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Shared",
    )

    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=viewer.id,
        role=CollectionMembershipRole.VIEWER,
    )

    with pytest.raises(CollectionWriteAccessError, match=str(shared_collection.id)):
        _ = await collection_service.update_active_save_collection(
            user_id=viewer.id,
            collection_id=shared_collection.id,
        )

    with pytest.raises(CollectionWriteAccessError, match=str(shared_collection.id)):
        _ = await collection_service.update_active_save_collection(
            user_id=outsider.id,
            collection_id=shared_collection.id,
        )


async def test_favorite_unfavorite_is_idempotent_and_updates_like_count(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()
    meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    first = await collection_service.favorite_meme(user_id=guest.id, meme_id=meme.id)
    second = await collection_service.favorite_meme(user_id=guest.id, meme_id=meme.id)
    favorites = await collection_service.list_favorite_memes(user_id=guest.id)

    assert first.collection_id == second.collection_id
    assert [favorite.meme_id for favorite in favorites] == [meme.id]
    assert await migrated_db_session.scalar(select(func.count()).select_from(CollectionMeme)) == 1
    assert await migrated_db_session.scalar(select(Meme.like_count).where(Meme.id == meme.id)) == 1

    assert await collection_service.unfavorite_meme(user_id=guest.id, meme_id=meme.id) is True
    assert await collection_service.unfavorite_meme(user_id=guest.id, meme_id=meme.id) is False
    assert await migrated_db_session.scalar(select(Meme.like_count).where(Meme.id == meme.id)) == 0


async def test_meme_library_bootstraps_guest_active_favorites_without_cards(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()

    library = await collection_service.get_meme_library(user_id=guest.id)

    assert library.favorites == []
    assert library.pinned_memes == []
    assert library.active_save_collection is not None
    assert library.active_save_collection.kind is CollectionKind.FAVORITES
    assert library.active_save_collection.can_write is True
    assert [(collection.title, collection.saved_meme_count) for collection in library.collections] == [("Favorites", 0)]


async def test_meme_library_returns_renderable_cards_collections_and_viewer_flags(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    full_user = await create_full_user_via_upgrade(user_service, email="library@example.com")
    favorite_meme = await _create_meme(migrated_db_session)
    pinned_meme = await _create_meme(migrated_db_session)
    custom_meme = await _create_meme(migrated_db_session)
    _ = await collection_service.favorite_meme(user_id=full_user.id, meme_id=favorite_meme.id)
    custom = await collection_service.create_custom_collection(owner_user_id=full_user.id, title="Work saves")
    _ = await collection_service.update_active_save_collection(user_id=full_user.id, collection_id=custom.id)
    _ = await collection_service.pin_meme(user_id=full_user.id, meme_id=pinned_meme.id)
    _ = await collection_service.save_meme_to_active_collection(user_id=full_user.id, meme_id=custom_meme.id)

    library = await collection_service.get_meme_library(user_id=full_user.id)

    assert [meme.id for meme in library.favorites] == [favorite_meme.id]
    assert [meme.id for meme in library.pinned_memes] == [pinned_meme.id]
    assert library.favorites[0].viewer_has_favorited is True
    assert library.favorites[0].viewer_has_saved is False
    assert library.favorites[0].viewer_has_pinned is False
    assert library.pinned_memes[0].viewer_has_favorited is False
    assert library.pinned_memes[0].viewer_has_saved is False
    assert library.pinned_memes[0].viewer_has_pinned is True
    assert library.active_save_collection is not None
    assert library.active_save_collection.id == custom.id
    collection_rows = [
        (collection.title, collection.saved_meme_count, collection.can_write)
        for collection in library.collections
    ]
    assert collection_rows == [
        ("Favorites", 1, True),
        ("Work saves", 1, True),
    ]


async def test_meme_library_returns_private_authenticated_render_urls_for_owner(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="private-library@example.com")
    private_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/library-upload.gif",
        s3_web_video_key="pipeline/derived/private/library-upload.mp4",
        mime_type="image/gif",
    )
    favorites = await collection_service.ensure_favorites_collection(owner.id)
    migrated_db_session.add(
        CollectionMeme(collection_id=favorites.id, meme_id=private_meme.id, added_by_user_id=owner.id)
    )
    await migrated_db_session.commit()
    assert private_meme.primary_file_id is not None

    library = await collection_service.get_meme_library(user_id=owner.id)

    assert [meme.id for meme in library.favorites] == [private_meme.id]
    primary_file = library.favorites[0].primary_file
    assert primary_file is not None
    assert primary_file.id == private_meme.primary_file_id
    assert primary_file.render is not None
    assert primary_file.render.thumbnail_url is None
    assert primary_file.render.preview_url is None
    assert primary_file.render.web_video_url == f"/api/v1/media/files/{private_meme.primary_file_id}/web-video.mp4"
    assert primary_file.render.download_url == primary_file.render.web_video_url
    serialized = library.model_dump_json()
    assert "pipeline/originals/private/library-upload.gif" not in serialized
    assert "pipeline/derived/private/library-upload.mp4" not in serialized


async def test_save_uses_guest_favorites_or_full_active_custom_collection(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()
    full_user = await create_full_user_via_upgrade(user_service, email="save@example.com")
    guest_meme = await _create_meme(migrated_db_session)
    full_meme = await _create_meme(migrated_db_session)
    custom = await collection_service.create_custom_collection(owner_user_id=full_user.id, title="Keepers")
    _ = await collection_service.update_active_save_collection(user_id=full_user.id, collection_id=custom.id)

    guest_save = await collection_service.save_meme_to_active_collection(user_id=guest.id, meme_id=guest_meme.id)
    full_save = await collection_service.save_meme_to_active_collection(user_id=full_user.id, meme_id=full_meme.id)

    guest_favorites = await collection_service.ensure_favorites_collection(guest.id)
    assert guest_save.collection_id == guest_favorites.id
    assert full_save.collection_id == custom.id
    assert await migrated_db_session.scalar(select(Meme.like_count).where(Meme.id == guest_meme.id)) == 1
    assert await collection_service.remove_meme_from_active_collection(user_id=guest.id, meme_id=guest_meme.id) is True
    assert await migrated_db_session.scalar(select(Meme.like_count).where(Meme.id == guest_meme.id)) == 0

    with pytest.raises(GuestCollectionAccessError, match="Guest accounts can only use Favorites"):
        _ = await collection_service.update_active_save_collection(user_id=guest.id, collection_id=custom.id)


async def test_private_cleanup_deletes_only_unreferenced_private_storage_and_rows(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    storage_client = FakeStorageClient()
    collection_service = CollectionService(migrated_db_session, storage_client=storage_client)
    owner = await create_full_user_via_upgrade(user_service, email="cleanup-owner@example.com")
    primary_collection = await collection_service.create_custom_collection(owner_user_id=owner.id, title="Primary")
    shared_collection = await collection_service.create_custom_collection(owner_user_id=owner.id, title="Shared ref")
    private_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/cleanup-original.png",
        s3_web_video_key="pipeline/derived/private/cleanup-web.mp4",
        mime_type="image/png",
    )
    public_meme = await _create_meme(
        migrated_db_session,
        is_public=True,
        s3_original_key="pipeline/originals/public/keep-original.png",
        s3_web_video_key="pipeline/derived/public/keep-web.mp4",
    )
    migrated_db_session.add(
        CollectionMeme(
            collection_id=primary_collection.id,
            meme_id=private_meme.id,
            added_by_user_id=owner.id,
        )
    )
    await migrated_db_session.commit()

    _ = await collection_service.save_meme_to_collection(
        collection_id=shared_collection.id,
        user_id=owner.id,
        meme_id=private_meme.id,
    )
    _ = await collection_service.save_meme_to_collection(
        collection_id=primary_collection.id,
        user_id=owner.id,
        meme_id=public_meme.id,
    )

    assert await collection_service.remove_meme_from_collection(
        collection_id=primary_collection.id,
        user_id=owner.id,
        meme_id=public_meme.id,
    ) is True
    assert await migrated_db_session.get(Meme, public_meme.id) is not None
    assert storage_client.delete_calls == []

    assert await collection_service.remove_meme_from_collection(
        collection_id=primary_collection.id,
        user_id=owner.id,
        meme_id=private_meme.id,
    ) is True
    assert await migrated_db_session.get(Meme, private_meme.id) is not None
    assert storage_client.delete_calls == []

    assert await collection_service.remove_meme_from_collection(
        collection_id=shared_collection.id,
        user_id=owner.id,
        meme_id=private_meme.id,
    ) is True
    assert await migrated_db_session.get(Meme, private_meme.id) is None
    assert await migrated_db_session.scalar(
        select(func.count()).select_from(MemeFile).where(MemeFile.meme_id == private_meme.id)
    ) == 0
    assert {call["Key"] for call in storage_client.delete_calls} == {
        "pipeline/originals/private/cleanup-original.png",
        "pipeline/derived/private/cleanup-web.mp4",
    }
    delete_call_count = len(storage_client.delete_calls)

    assert await collection_service.remove_meme_from_collection(
        collection_id=shared_collection.id,
        user_id=owner.id,
        meme_id=private_meme.id,
    ) is False
    assert len(storage_client.delete_calls) == delete_call_count


async def test_private_cleanup_runs_for_active_collection_and_unfavorite_paths(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    storage_client = FakeStorageClient()
    storage_client.fail_deletes = True
    collection_service = CollectionService(migrated_db_session, storage_client=storage_client)
    owner = await create_full_user_via_upgrade(user_service, email="cleanup-active-owner@example.com")
    _ = await collection_service.ensure_favorites_collection(owner.id)
    custom = await collection_service.create_custom_collection(owner_user_id=owner.id, title="Active cleanup")
    _ = await collection_service.update_active_save_collection(user_id=owner.id, collection_id=custom.id)
    active_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/active-cleanup.png",
        s3_web_video_key="pipeline/derived/private/active-cleanup.mp4",
    )
    favorite_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/favorite-cleanup.png",
    )
    active_meme_id = active_meme.id
    favorite_meme_id = favorite_meme.id
    favorites = await collection_service.ensure_favorites_collection(owner.id)
    migrated_db_session.add_all(
        [
            CollectionMeme(collection_id=custom.id, meme_id=active_meme_id, added_by_user_id=owner.id),
            CollectionMeme(collection_id=favorites.id, meme_id=favorite_meme_id, added_by_user_id=owner.id),
        ]
    )
    await migrated_db_session.commit()

    assert await collection_service.remove_meme_from_active_collection(user_id=owner.id, meme_id=active_meme_id) is True
    assert await collection_service.unfavorite_meme(user_id=owner.id, meme_id=favorite_meme_id) is True

    assert await migrated_db_session.get(Meme, active_meme_id) is None
    assert await migrated_db_session.get(Meme, favorite_meme_id) is None
    assert {call["Key"] for call in storage_client.delete_calls} == {
        "pipeline/originals/private/active-cleanup.png",
        "pipeline/derived/private/active-cleanup.mp4",
        "pipeline/originals/private/favorite-cleanup.png",
    }


async def test_custom_collection_delete_cleans_orphans_and_pinned_private_memes(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    storage_client = FakeStorageClient()
    collection_service = CollectionService(migrated_db_session, storage_client=storage_client)
    owner = await create_full_user_via_upgrade(user_service, email="cleanup-delete-owner@example.com")
    custom = await collection_service.create_custom_collection(owner_user_id=owner.id, title="Delete cleanup")
    orphan_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/delete-orphan.png",
        s3_web_video_key="pipeline/derived/private/delete-orphan.mp4",
    )
    pinned_meme = await _create_meme(
        migrated_db_session,
        is_public=False,
        s3_original_key="pipeline/originals/private/delete-pinned.png",
        s3_web_video_key="pipeline/derived/private/delete-pinned.mp4",
    )
    orphan_meme_id = orphan_meme.id
    pinned_meme_id = pinned_meme.id
    migrated_db_session.add_all(
        [
            CollectionMeme(collection_id=custom.id, meme_id=orphan_meme_id, added_by_user_id=owner.id),
            CollectionMeme(collection_id=custom.id, meme_id=pinned_meme_id, added_by_user_id=owner.id),
        ]
    )
    await migrated_db_session.commit()
    _ = await collection_service.pin_meme(user_id=owner.id, meme_id=pinned_meme_id)

    assert await collection_service.delete_custom_collection(collection_id=custom.id, user_id=owner.id) is True

    assert await migrated_db_session.get(Meme, orphan_meme_id) is None
    assert await migrated_db_session.get(Meme, pinned_meme_id) is None
    assert await migrated_db_session.get(PinnedMeme, (owner.id, pinned_meme_id)) is None
    assert {call["Key"] for call in storage_client.delete_calls} == {
        "pipeline/originals/private/delete-orphan.png",
        "pipeline/derived/private/delete-orphan.mp4",
        "pipeline/originals/private/delete-pinned.png",
        "pipeline/derived/private/delete-pinned.mp4",
    }


async def test_pins_require_full_account_enforce_limit_and_reorder(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()
    full_user = await create_full_user_via_upgrade(user_service, email="pins@example.com")
    memes = [await _create_meme(migrated_db_session) for _ in range(21)]
    await migrated_db_session.commit()

    with pytest.raises(GuestCollectionAccessError, match="full account"):
        _ = await collection_service.pin_meme(user_id=guest.id, meme_id=memes[0].id)

    first = await collection_service.pin_meme(user_id=full_user.id, meme_id=memes[0].id)
    duplicate = await collection_service.pin_meme(user_id=full_user.id, meme_id=memes[0].id)
    assert first.position == duplicate.position == 1

    for meme in memes[1:20]:
        _ = await collection_service.pin_meme(user_id=full_user.id, meme_id=meme.id)

    with pytest.raises(PinLimitExceededError, match="at most 20"):
        _ = await collection_service.pin_meme(user_id=full_user.id, meme_id=memes[20].id)

    reordered = await collection_service.reorder_pins(
        user_id=full_user.id,
        meme_ids=[memes[2].id, memes[1].id, *[meme.id for meme in memes[3:20]], memes[0].id],
    )
    assert [pin.meme_id for pin in reordered[:2]] == [memes[2].id, memes[1].id]
    assert [pin.position for pin in reordered] == list(range(1, 21))

    with pytest.raises(InvalidPinnedMemeOrderError, match="duplicate"):
        _ = await collection_service.reorder_pins(user_id=full_user.id, meme_ids=[memes[0].id, memes[0].id])

    assert await collection_service.unpin_meme(user_id=full_user.id, meme_id=memes[2].id) is True
    persisted_positions = await migrated_db_session.scalars(
        select(PinnedMeme.position).where(PinnedMeme.user_id == full_user.id).order_by(PinnedMeme.position.asc())
    )
    assert list(persisted_positions) == list(range(1, 20))


async def test_library_writes_reject_private_memes_not_visible_to_user(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    _ = await create_full_user_via_upgrade(user_service, email="private-author@example.com")
    stranger = await create_full_user_via_upgrade(user_service, email="private-stranger@example.com")
    private_meme = await _create_meme(migrated_db_session, is_public=False)
    await migrated_db_session.commit()

    with pytest.raises(MemeNotFoundError):
        _ = await collection_service.favorite_meme(user_id=stranger.id, meme_id=private_meme.id)
    with pytest.raises(MemeNotFoundError):
        _ = await collection_service.save_meme_to_active_collection(user_id=stranger.id, meme_id=private_meme.id)
    with pytest.raises(MemeNotFoundError):
        _ = await collection_service.pin_meme(user_id=stranger.id, meme_id=private_meme.id)

    assert await migrated_db_session.scalar(select(func.count()).select_from(CollectionMeme)) == 0
    assert await migrated_db_session.scalar(select(func.count()).select_from(PinnedMeme)) == 0
    assert await migrated_db_session.scalar(select(Meme.like_count).where(Meme.id == private_meme.id)) == 0


async def test_owner_manages_member_roles_and_write_permissions(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="member-owner@example.com")
    editor = await create_full_user_via_upgrade(user_service, email="member-editor@example.com")
    viewer = await create_full_user_via_upgrade(user_service, email="member-viewer@example.com")
    outsider = await create_full_user_via_upgrade(user_service, email="member-outsider@example.com")
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Member roles",
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=editor.id,
        role=CollectionMembershipRole.EDITOR,
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=viewer.id,
        role=CollectionMembershipRole.VIEWER,
    )
    meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    _ = await collection_service.save_meme_to_collection(
        collection_id=shared_collection.id,
        user_id=editor.id,
        meme_id=meme.id,
    )
    assert await collection_service.remove_meme_from_collection(
        collection_id=shared_collection.id,
        user_id=editor.id,
        meme_id=meme.id,
    ) is True
    with pytest.raises(CollectionWriteAccessError):
        _ = await collection_service.save_meme_to_collection(
            collection_id=shared_collection.id,
            user_id=viewer.id,
            meme_id=meme.id,
        )
    with pytest.raises(CollectionNotFoundError):
        _ = await collection_service.save_meme_to_collection(
            collection_id=shared_collection.id,
            user_id=outsider.id,
            meme_id=meme.id,
        )

    promoted = await collection_service.update_member_role(
        collection_id=shared_collection.id,
        acting_user_id=owner.id,
        member_user_id=viewer.id,
        role=CollectionMembershipRole.EDITOR,
    )
    assert promoted.role is CollectionMembershipRole.EDITOR
    _ = await collection_service.save_meme_to_collection(
        collection_id=shared_collection.id,
        user_id=viewer.id,
        meme_id=meme.id,
    )
    demoted = await collection_service.update_member_role(
        collection_id=shared_collection.id,
        acting_user_id=owner.id,
        member_user_id=viewer.id,
        role=CollectionMembershipRole.VIEWER,
    )
    assert demoted.role is CollectionMembershipRole.VIEWER

    with pytest.raises(CollectionWriteAccessError, match="Only the owner"):
        _ = await collection_service.update_member_role(
            collection_id=shared_collection.id,
            acting_user_id=editor.id,
            member_user_id=viewer.id,
            role=CollectionMembershipRole.EDITOR,
        )
    with pytest.raises(CollectionWriteAccessError, match="Only the owner"):
        _ = await collection_service.remove_member(
            collection_id=shared_collection.id,
            acting_user_id=viewer.id,
            member_user_id=editor.id,
        )
    with pytest.raises(CollectionNotFoundError):
        _ = await collection_service.update_member_role(
            collection_id=shared_collection.id,
            acting_user_id=outsider.id,
            member_user_id=viewer.id,
            role=CollectionMembershipRole.EDITOR,
        )
    with pytest.raises(InvalidCollectionMembershipError, match="ownership cannot be transferred"):
        _ = await collection_service.update_member_role(
            collection_id=shared_collection.id,
            acting_user_id=owner.id,
            member_user_id=viewer.id,
            role=CollectionMembershipRole.OWNER,
        )
    with pytest.raises(InvalidCollectionMembershipError, match="owner role cannot be changed"):
        _ = await collection_service.update_member_role(
            collection_id=shared_collection.id,
            acting_user_id=owner.id,
            member_user_id=owner.id,
            role=CollectionMembershipRole.VIEWER,
        )
    with pytest.raises(InvalidCollectionMembershipError, match="owner cannot be removed"):
        _ = await collection_service.remove_member(
            collection_id=shared_collection.id,
            acting_user_id=owner.id,
            member_user_id=owner.id,
        )

    assert await collection_service.remove_member(
        collection_id=shared_collection.id,
        acting_user_id=owner.id,
        member_user_id=viewer.id,
    ) is True
    assert await collection_service.remove_member(
        collection_id=shared_collection.id,
        acting_user_id=owner.id,
        member_user_id=viewer.id,
    ) is False
    with pytest.raises(CollectionNotFoundError):
        _ = await collection_service.get_collection_for_user(collection_id=shared_collection.id, user_id=viewer.id)


async def test_create_invite_rejects_unverified_email_only_accounts_without_persisting_invites(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service, email="owner@example.com")
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Shared board",
    )

    with pytest.raises(CollectionVerificationRequiredError, match="verified email"):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=owner.id,
            token_hash="e" * 64,
        )

    invite_count_result = await migrated_db_session.execute(select(func.count()).select_from(CollectionInvite))
    assert invite_count_result.scalar_one() == 0


async def test_create_invite_preserves_guest_and_write_access_errors_without_persisting_invites(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service,
        email="owner@example.com",
        email_verified_at=datetime.now(UTC),
    )
    viewer = await create_full_user_via_upgrade(user_service,
        email="viewer@example.com",
        email_verified_at=datetime.now(UTC),
    )
    guest = await user_service.create_guest_user()
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Shared board",
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=viewer.id,
        role=CollectionMembershipRole.VIEWER,
    )

    with pytest.raises(GuestCollectionAccessError, match="cannot create collection invites"):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=guest.id,
            token_hash="f" * 64,
        )

    with pytest.raises(CollectionWriteAccessError, match=str(shared_collection.id)):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=viewer.id,
            token_hash="g" * 64,
        )

    invite_count_result = await migrated_db_session.execute(select(func.count()).select_from(CollectionInvite))
    assert invite_count_result.scalar_one() == 0


async def test_create_invite_allows_telegram_and_google_backed_editors_with_write_access(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service,
        email="owner@example.com",
        email_verified_at=datetime.now(UTC),
    )
    telegram_editor = await create_full_user_via_upgrade(user_service, telegram_id=123456789)
    google_editor = await create_full_user_via_upgrade(user_service,
        google_id="google-subject-123",
        email="google-editor@example.com",
        email_verified_at=datetime.now(UTC),
    )
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Shared board",
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=telegram_editor.id,
        role=CollectionMembershipRole.EDITOR,
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=google_editor.id,
        role=CollectionMembershipRole.EDITOR,
    )

    telegram_invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=telegram_editor.id,
        token_hash="h" * 64,
    )
    google_invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=google_editor.id,
        token_hash="i" * 64,
    )

    assert telegram_invite.created_by_user_id == telegram_editor.id
    assert google_invite.created_by_user_id == google_editor.id

    invite_count_result = await migrated_db_session.execute(select(func.count()).select_from(CollectionInvite))
    assert invite_count_result.scalar_one() == 2


async def test_create_invite_persists_valid_payload_and_rejects_malformed_inputs(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(user_service,
        email="owner@example.com",
        email_verified_at=datetime.now(UTC),
    )
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Shared board",
    )
    expires_at = datetime.now(UTC) + timedelta(days=3)

    invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=owner.id,
        token_hash="a" * 64,
        role=CollectionMembershipRole.EDITOR,
        channel=CollectionInviteChannel.EMAIL,
        label="  Team editors  ",
        max_uses=2,
        expires_at=expires_at,
        recipient_email=" Viewer@Example.COM ",
    )

    assert invite.collection_id == shared_collection.id
    assert invite.created_by_user_id == owner.id
    assert invite.role is CollectionMembershipRole.EDITOR
    assert invite.channel is CollectionInviteChannel.EMAIL
    assert invite.label == "Team editors"
    assert invite.status is CollectionInviteStatus.PENDING
    assert invite.max_uses == 2
    assert invite.expires_at == expires_at
    assert invite.recipient_email == "viewer@example.com"

    with pytest.raises(InvalidCollectionInviteError, match="owner role"):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=owner.id,
            token_hash="b" * 64,
            role=CollectionMembershipRole.OWNER,
        )

    with pytest.raises(InvalidCollectionInviteError, match="greater than zero"):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=owner.id,
            token_hash="c" * 64,
            max_uses=0,
        )

    with pytest.raises(InvalidCollectionInviteError, match="recipient_email"):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=owner.id,
            token_hash="d" * 64,
            channel=CollectionInviteChannel.EMAIL,
        )

    with pytest.raises(DuplicateCollectionInviteError, match="already exists"):
        _ = await collection_service.create_invite(
            collection_id=shared_collection.id,
            created_by_user_id=owner.id,
            token_hash="a" * 64,
        )

    invite_count_result = await migrated_db_session.execute(select(func.count()).select_from(CollectionInvite))
    assert invite_count_result.scalar_one() == 1


async def test_invite_join_terminal_statuses_persist_and_revoked_invites_cannot_join(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    owner = await create_full_user_via_upgrade(
        user_service,
        email="invite-owner@example.com",
        email_verified_at=datetime.now(UTC),
    )
    editor = await create_full_user_via_upgrade(user_service, telegram_id=9001)
    viewer = await create_full_user_via_upgrade(user_service, telegram_id=9002)
    joiner = await create_full_user_via_upgrade(user_service, email="invite-joiner@example.com")
    second_joiner = await create_full_user_via_upgrade(user_service, email="invite-second@example.com")
    expired_joiner = await create_full_user_via_upgrade(user_service, email="invite-expired@example.com")
    revoked_joiner = await create_full_user_via_upgrade(user_service, email="invite-revoked@example.com")
    shared_collection = await collection_service.create_custom_collection(
        owner_user_id=owner.id,
        title="Invite lifecycle",
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=editor.id,
        role=CollectionMembershipRole.EDITOR,
    )
    _ = await collection_service.ensure_member(
        collection_id=shared_collection.id,
        user_id=viewer.id,
        role=CollectionMembershipRole.VIEWER,
    )

    one_use_invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=owner.id,
        token_hash="j" * 64,
        max_uses=1,
    )
    joined_collection = await collection_service.join_invite(token_hash="j" * 64, user_id=joiner.id)
    assert any(member.user_id == joiner.id for member in joined_collection.memberships)
    persisted_one_use = await migrated_db_session.scalar(
        select(CollectionInvite).where(CollectionInvite.id == one_use_invite.id)
    )
    assert persisted_one_use is not None
    assert persisted_one_use.use_count == 1
    assert persisted_one_use.status is CollectionInviteStatus.ACCEPTED
    with pytest.raises(InvalidCollectionInviteError):
        _ = await collection_service.join_invite(token_hash="j" * 64, user_id=second_joiner.id)

    expiring_invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=owner.id,
        token_hash="k" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    expiring_row = await migrated_db_session.scalar(
        select(CollectionInvite).where(CollectionInvite.id == expiring_invite.id)
    )
    assert expiring_row is not None
    expiring_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await migrated_db_session.commit()
    with pytest.raises(InvalidCollectionInviteError):
        _ = await collection_service.join_invite(token_hash="k" * 64, user_id=expired_joiner.id)
    persisted_expired = await migrated_db_session.scalar(
        select(CollectionInvite).where(CollectionInvite.id == expiring_invite.id)
    )
    assert persisted_expired is not None
    assert persisted_expired.status is CollectionInviteStatus.EXPIRED

    revoked_invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=editor.id,
        token_hash="l" * 64,
        max_uses=2,
    )
    revoked = await collection_service.revoke_invite(
        collection_id=shared_collection.id,
        invite_id=revoked_invite.id,
        user_id=editor.id,
    )
    assert revoked.status is CollectionInviteStatus.REVOKED
    assert revoked.revoked_at is not None
    with pytest.raises(InvalidCollectionInviteError):
        _ = await collection_service.join_invite(token_hash="l" * 64, user_id=revoked_joiner.id)

    viewer_invite = await collection_service.create_invite(
        collection_id=shared_collection.id,
        created_by_user_id=owner.id,
        token_hash="m" * 64,
    )
    with pytest.raises(CollectionWriteAccessError):
        _ = await collection_service.revoke_invite(
            collection_id=shared_collection.id,
            invite_id=viewer_invite.id,
            user_id=viewer.id,
        )

    detail = await collection_service.get_collection_for_user(collection_id=shared_collection.id, user_id=owner.id)
    invites_by_id = {invite.id: invite for invite in detail.invites}
    assert invites_by_id[one_use_invite.id].status is CollectionInviteStatus.ACCEPTED
    assert invites_by_id[expiring_invite.id].status is CollectionInviteStatus.EXPIRED
    assert invites_by_id[revoked_invite.id].status is CollectionInviteStatus.REVOKED
    assert invites_by_id[revoked_invite.id].revoked_at is not None
