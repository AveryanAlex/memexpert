"""Integration tests for collection, membership, invite, and active-save service invariants."""

from __future__ import annotations

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
    CollectionService,
    CollectionVerificationRequiredError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    InvalidPinnedMemeOrderError,
    MemeNotFoundError,
    PinLimitExceededError,
    UserService,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    author_user_id: uuid.UUID | None = None,
    s3_original_key: str | None = None,
    s3_web_video_key: str | None = None,
    mime_type: str = "image/jpeg",
) -> Meme:
    meme = Meme(
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        is_public=is_public,
        author_user_id=author_user_id,
    )
    session.add(meme)
    await session.flush()
    file = MemeFile(
        meme_id=meme.id,
        s3_original_key=s3_original_key or f"pipeline/originals/test/{meme.id}.jpg",
        s3_web_video_key=s3_web_video_key,
        mime_type=mime_type,
        width=640,
        height=480,
        quality_score=0.8,
        is_primary=True,
    )
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
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
        author_user_id=owner.id,
        s3_original_key="pipeline/originals/private/library-upload.gif",
        s3_web_video_key="pipeline/derived/private/library-upload.mp4",
        mime_type="image/gif",
    )
    await migrated_db_session.commit()
    assert private_meme.primary_file_id is not None

    _ = await collection_service.favorite_meme(user_id=owner.id, meme_id=private_meme.id)
    library = await collection_service.get_meme_library(user_id=owner.id)

    assert [meme.id for meme in library.favorites] == [private_meme.id]
    primary_file = library.favorites[0].primary_file
    assert primary_file is not None
    assert primary_file.id == private_meme.primary_file_id
    assert primary_file.render is not None
    assert primary_file.render.thumbnail_url == f"/api/v1/media/files/{private_meme.primary_file_id}/thumbnail"
    assert primary_file.render.preview_url == f"/api/v1/media/files/{private_meme.primary_file_id}/preview"
    assert primary_file.render.web_video_url == f"/api/v1/media/files/{private_meme.primary_file_id}/web-video.mp4"
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
    author = await create_full_user_via_upgrade(user_service, email="private-author@example.com")
    stranger = await create_full_user_via_upgrade(user_service, email="private-stranger@example.com")
    private_meme = await _create_meme(migrated_db_session, is_public=False, author_user_id=author.id)
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
