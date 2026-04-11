"""Integration tests for the user/account service invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from memexpert.models.collection import Collection, CollectionMember
from memexpert.models.enums import AccountType, AuthProvider, CollectionKind, CollectionMembershipRole
from memexpert.models.user import User
from memexpert.services import DuplicateIdentityError, InvalidIdentityError, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_create_guest_user_bootstraps_favorites_membership_and_active_save_collection(
    migrated_db_session: AsyncSession,
) -> None:
    service = UserService(migrated_db_session)

    created_user = await service.create_guest_user()

    assert created_user.account_type is AccountType.GUEST
    assert created_user.active_save_collection_id is not None
    assert created_user.guest_expires_at is not None
    assert created_user.last_active_at is not None

    result = await migrated_db_session.execute(
        select(User)
        .options(
            selectinload(User.active_save_collection),
            selectinload(User.owned_collections).selectinload(Collection.memberships),
        )
        .where(User.id == created_user.id)
    )
    persisted_user = result.scalar_one()

    assert persisted_user.active_save_collection is not None
    assert persisted_user.active_save_collection.kind is CollectionKind.FAVORITES
    assert persisted_user.active_save_collection_id == persisted_user.active_save_collection.id
    assert len(persisted_user.owned_collections) == 1
    favorites = persisted_user.owned_collections[0]
    assert favorites.kind is CollectionKind.FAVORITES
    assert favorites.title == "Favorites"
    assert [member.role for member in favorites.memberships] == [CollectionMembershipRole.OWNER]
    assert [member.user_id for member in favorites.memberships] == [persisted_user.id]


async def test_upgrade_guest_normalizes_email_supports_provider_lookups_and_preserves_favorites(
    migrated_db_session: AsyncSession,
) -> None:
    service = UserService(migrated_db_session)

    created_user = await create_full_user_via_upgrade(
        service,
        telegram_id=123456789,
        google_id="google-subject-123",
        email="  User@Example.COM ",
    )

    assert created_user.account_type is AccountType.FULL
    assert created_user.email == "user@example.com"
    assert created_user.active_save_collection_id is not None
    assert created_user.guest_expires_at is None

    by_email = await service.get_by_email("USER@example.com")
    by_google = await service.get_by_provider(AuthProvider.GOOGLE, "google-subject-123")
    by_telegram = await service.get_by_provider(AuthProvider.TELEGRAM, 123456789)
    favorites = await service.get_favorites_collection(created_user.id)

    assert by_email is not None and by_email.id == created_user.id
    assert by_google is not None and by_google.id == created_user.id
    assert by_telegram is not None and by_telegram.id == created_user.id
    assert favorites is not None
    assert favorites.id == created_user.active_save_collection_id
    assert favorites.kind is CollectionKind.FAVORITES


async def test_upgrade_guest_rejects_duplicate_identities_across_transactions(
    migrated_db_session: AsyncSession,
) -> None:
    service = UserService(migrated_db_session)

    _ = await create_full_user_via_upgrade(
        service,
        telegram_id=99,
        google_id="google-dup",
        email="owner@example.com",
    )

    with pytest.raises(DuplicateIdentityError, match="google-dup"):
        _ = await create_full_user_via_upgrade(
            service, google_id="google-dup", email="other@example.com",
        )
    await migrated_db_session.rollback()

    with pytest.raises(DuplicateIdentityError, match="owner@example.com"):
        _ = await create_full_user_via_upgrade(service, email="OWNER@EXAMPLE.COM")
    await migrated_db_session.rollback()

    with pytest.raises(DuplicateIdentityError, match="Telegram ID 99"):
        _ = await create_full_user_via_upgrade(service, telegram_id=99)
    await migrated_db_session.rollback()


async def test_repeated_guest_creation_produces_distinct_accounts_with_one_favorites_each(
    migrated_db_session: AsyncSession,
) -> None:
    service = UserService(migrated_db_session)

    first_guest = await service.create_guest_user()
    second_guest = await service.create_guest_user()

    assert first_guest.id != second_guest.id

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(Collection.kind == CollectionKind.FAVORITES)
    )
    membership_count_result = await migrated_db_session.execute(select(func.count()).select_from(CollectionMember))

    assert favorites_count_result.scalar_one() == 2
    assert membership_count_result.scalar_one() == 2


async def test_create_guest_user_commit_false_rolls_back_on_session_rollback(
    migrated_db_session: AsyncSession,
) -> None:
    """Bootstrapped guests must disappear when the outer transaction rolls back.

    ``AccountLinkService`` relies on this: it bootstraps a guest with
    ``commit=False`` so that a validation failure during the subsequent
    in-place upgrade rolls the guest row back atomically instead of leaking
    an orphan user + favorites + membership tuple.
    """

    service = UserService(migrated_db_session)

    created_user = await service.create_guest_user(commit=False)

    in_session_result = await migrated_db_session.execute(select(User).where(User.id == created_user.id))
    assert in_session_result.scalar_one_or_none() is not None

    await migrated_db_session.rollback()

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(Collection.kind == CollectionKind.FAVORITES)
    )
    membership_count_result = await migrated_db_session.execute(select(func.count()).select_from(CollectionMember))

    assert user_count_result.scalar_one() == 0
    assert favorites_count_result.scalar_one() == 0
    assert membership_count_result.scalar_one() == 0


async def test_upgrade_guest_rejects_malformed_email_and_rolls_back_the_bootstrap(
    migrated_db_session: AsyncSession,
) -> None:
    """Seeding via the upgrade helper with a malformed email must leave zero rows.

    The helper bootstraps the guest with ``commit=False``, so when
    ``upgrade_guest_to_full_account`` rejects the malformed email the outer
    ``session.rollback()`` unwinds the bootstrap atomically — the same
    "rejected before commit" invariant the deleted ``create_full_user``
    seed primitive enforced.
    """

    service = UserService(migrated_db_session)

    with pytest.raises(InvalidIdentityError, match="valid address"):
        _ = await create_full_user_via_upgrade(service, email="not-an-email")

    await migrated_db_session.rollback()

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    collection_count_result = await migrated_db_session.execute(select(func.count()).select_from(Collection))

    assert user_count_result.scalar_one() == 0
    assert collection_count_result.scalar_one() == 0


async def test_touch_last_active_updates_lifecycle_timestamp(
    migrated_db_session: AsyncSession,
) -> None:
    service = UserService(migrated_db_session)
    created_user = await service.create_guest_user()
    updated_at = datetime.now(UTC) + timedelta(minutes=5)

    touched_user = await service.touch_last_active(created_user.id, occurred_at=updated_at)

    assert touched_user.last_active_at == updated_at

    result = await migrated_db_session.execute(select(User).where(User.id == created_user.id))
    persisted_user = result.scalar_one()
    assert persisted_user.last_active_at == updated_at
