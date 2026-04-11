"""Integration tests for collection, membership, invite, and active-save service invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from memexpert.models.collection import Collection, CollectionInvite
from memexpert.models.enums import (
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
)
from memexpert.models.user import User
from memexpert.services import (
    CollectionService,
    CollectionVerificationRequiredError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    UserService,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_guest_cannot_create_custom_collection(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    collection_service = CollectionService(migrated_db_session)
    guest = await user_service.create_guest_user()

    with pytest.raises(GuestCollectionAccessError, match="cannot create custom collections"):
        _ = await collection_service.create_custom_collection(owner_user_id=guest.id, title="Guest board")

    collection_count_result = await migrated_db_session.execute(select(func.count()).select_from(Collection))
    assert collection_count_result.scalar_one() == 1


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
