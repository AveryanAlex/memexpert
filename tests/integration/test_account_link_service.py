# ruff: noqa: TC003
"""Integration tests for guest account linking and merge orchestration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, override

import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.content import Meme
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionKind,
    ContentKind,
    UserLanguage,
)
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, InlineUsageEvent, User
from memexpert.services import (
    AccountLinkInvariantError,
    AccountLinkService,
    AccountUnavailableError,
    GuestAccountRequiredError,
    InvalidCredentialsError,
    ProviderAuthService,
    ProviderPayloadInvalidError,
    UserService,
)
from memexpert.services.provider_auth_service import GoogleIdentity, TelegramIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
GOOGLE_AUTH_DATE = datetime.now(UTC)


def build_provider_auth_service(session: AsyncSession) -> ProviderAuthService:
    return ProviderAuthService(
        session,
        password_hash_rounds=12,
        telegram_bot_token="123456:account-link-test-bot-token",
        google_client_id="account-link-google-client-id",
        google_client_secret="account-link-google-client-secret",
        google_redirect_uri="https://memexpert.test/auth/google/callback",
    )


def build_account_link_service(
    session: AsyncSession,
    *,
    provider_auth_service: ProviderAuthService | None = None,
) -> AccountLinkService:
    return AccountLinkService(
        session,
        provider_auth_service=provider_auth_service or build_provider_auth_service(session),
    )


class ExplodingAccountLinkService(AccountLinkService):
    """Inject a deterministic failure after partial merge-side updates begin."""

    @override
    async def _reassign_inline_usage_events(
        self,
        *,
        source_user_id: uuid.UUID,
        target_user_id: uuid.UUID,
    ) -> int:
        _ = await super()._reassign_inline_usage_events(
            source_user_id=source_user_id,
            target_user_id=target_user_id,
        )
        raise AccountLinkInvariantError("forced rollback after partial transfer")


async def create_password_user(
    session: AsyncSession,
    *,
    email: str,
    language: UserLanguage = UserLanguage.ANY,
    nsfw_enabled: bool = False,
):
    provider_auth_service = build_provider_auth_service(session)
    user_service = UserService(session)
    signup_identity = provider_auth_service.prepare_email_signup_identity(
        email=email,
        password=PASSWORD,
    )
    return await user_service.create_full_user(
        email=signup_identity.email,
        password_hash=signup_identity.password_hash,
        language=language,
        nsfw_enabled=nsfw_enabled,
    )


async def create_meme(session: AsyncSession) -> Meme:
    meme = Meme(media_type=ContentKind.IMAGE)
    session.add(meme)
    await session.flush()
    return meme


async def add_saved_meme(
    session: AsyncSession,
    *,
    collection_id: uuid.UUID,
    meme_id: uuid.UUID,
    added_by_user_id: uuid.UUID,
) -> None:
    session.add(
        CollectionMeme(
            collection_id=collection_id,
            meme_id=meme_id,
            added_by_user_id=added_by_user_id,
        )
    )
    await session.flush()


async def add_guest_history(
    session: AsyncSession,
    *,
    guest_user_id: uuid.UUID,
    meme_id: str,
) -> None:
    session.add_all(
        [
            AnalyticsEvent(
                user_id=guest_user_id,
                event_type=AnalyticsEventType.MEME_VIEW,
                payload={"meme_id": meme_id},
            ),
            AnalyticsEvent(
                user_id=guest_user_id,
                event_type=AnalyticsEventType.CLICK,
                payload={"surface": "favorites"},
            ),
            InlineUsageEvent(user_id=guest_user_id, group_hash="feedbeef1234"),
        ]
    )
    await session.flush()


async def count_favorites_rows(session: AsyncSession, *, owner_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .where(
            Collection.owner_id == owner_id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    return int(result.scalar_one())


async def get_favorites_collection_id(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
) -> uuid.UUID | None:
    result = await session.execute(
        select(Collection.id).where(
            Collection.owner_id == owner_id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    return result.scalar_one_or_none()


async def test_email_signup_upgrades_guest_in_place_and_keeps_favorites_collection(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    guest = await user_service.create_guest_user(language=UserLanguage.RU, nsfw_enabled=True)
    favorites_collection_id = await get_favorites_collection_id(migrated_db_session, owner_id=guest.id)

    result = await link_service.link_guest_with_email_signup(
        guest_user_id=guest.id,
        email="  NewGuest@Example.COM ",
        password=PASSWORD,
    )

    assert result.merge_performed is False
    assert result.user.id == guest.id
    assert result.user.account_type is AccountType.FULL
    assert result.user.email == "newguest@example.com"
    assert result.user.guest_expires_at is None
    assert result.user.language is UserLanguage.RU
    assert result.user.nsfw_enabled is True
    assert result.linked_providers.email == "newguest@example.com"
    assert result.linked_providers.has_password is True
    assert result.linked_providers.google_linked is False
    assert result.linked_providers.telegram_linked is False
    assert result.favorites_transferred == 0
    assert result.merge_log_id is None

    persisted_user_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    persisted_user = persisted_user_result.scalar_one()
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))

    assert persisted_user.active_save_collection_id == favorites_collection_id
    assert persisted_user.password_hash is not None
    assert merge_log_count_result.scalar_one() == 0


async def test_email_login_merges_guest_into_existing_full_with_deduped_favorites_history_and_audit(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)

    guest = await user_service.create_guest_user(language=UserLanguage.EN, nsfw_enabled=True)
    full_user = await create_password_user(
        migrated_db_session,
        email="owner@example.com",
        language=UserLanguage.RU,
        nsfw_enabled=False,
    )

    guest_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=guest.id)
    full_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=full_user.id)
    assert guest_favorites_id is not None
    assert full_favorites_id is not None

    duplicate_meme = await create_meme(migrated_db_session)
    transferred_meme = await create_meme(migrated_db_session)
    await add_saved_meme(
        migrated_db_session,
        collection_id=full_favorites_id,
        meme_id=duplicate_meme.id,
        added_by_user_id=full_user.id,
    )
    await add_saved_meme(
        migrated_db_session,
        collection_id=guest_favorites_id,
        meme_id=duplicate_meme.id,
        added_by_user_id=guest.id,
    )
    await add_saved_meme(
        migrated_db_session,
        collection_id=guest_favorites_id,
        meme_id=transferred_meme.id,
        added_by_user_id=guest.id,
    )
    await add_guest_history(
        migrated_db_session,
        guest_user_id=guest.id,
        meme_id=str(transferred_meme.id),
    )
    await migrated_db_session.commit()

    result = await link_service.link_guest_with_email_login(
        guest_user_id=guest.id,
        email=" OWNER@example.com ",
        password=PASSWORD,
    )

    assert result.merge_performed is True
    assert result.user.id == full_user.id
    assert result.deleted_guest_user_id == guest.id
    assert result.favorites_transferred == 1
    assert result.duplicate_favorites_skipped == 1
    assert result.analytics_events_transferred == 2
    assert result.inline_usage_events_transferred == 1
    assert result.views_transferred == 1
    assert result.linked_providers.email == "owner@example.com"
    assert result.linked_providers.has_password is True
    assert result.linked_providers.google_linked is False
    assert result.linked_providers.telegram_linked is False

    deleted_guest_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    persisted_full_result = await migrated_db_session.execute(select(User).where(User.id == full_user.id))
    merge_log_result = await migrated_db_session.execute(
        select(AccountMergeLog).where(AccountMergeLog.id == result.merge_log_id)
    )
    analytics_user_ids_result = await migrated_db_session.execute(
        select(AnalyticsEvent.user_id).order_by(AnalyticsEvent.occurred_at.asc())
    )
    inline_user_ids_result = await migrated_db_session.execute(select(InlineUsageEvent.user_id))
    favorites_meme_ids_result = await migrated_db_session.execute(
        select(CollectionMeme.meme_id)
        .where(CollectionMeme.collection_id == full_favorites_id)
        .order_by(CollectionMeme.meme_id.asc())
    )
    guest_favorites_result = await migrated_db_session.execute(
        select(Collection).where(Collection.id == guest_favorites_id)
    )

    assert deleted_guest_result.scalar_one_or_none() is None
    persisted_full = persisted_full_result.scalar_one()
    assert persisted_full.language is UserLanguage.RU
    assert persisted_full.nsfw_enabled is False
    assert persisted_full.email == "owner@example.com"
    assert guest_favorites_result.scalar_one_or_none() is None
    assert set(favorites_meme_ids_result.scalars().all()) == {duplicate_meme.id, transferred_meme.id}
    assert analytics_user_ids_result.scalars().all() == [full_user.id, full_user.id]
    assert inline_user_ids_result.scalars().all() == [full_user.id]

    merge_log = merge_log_result.scalar_one()
    assert merge_log.favorites_transferred == 1
    assert merge_log.views_transferred == 1
    assert merge_log.details == {
        "mode": "merge_into_existing_full",
        "guest_favorites_collection_id": str(guest_favorites_id),
        "target_favorites_collection_id": str(full_favorites_id),
        "guest_favorite_rows": 2,
        "favorites_transferred": 1,
        "favorite_duplicates_skipped": 1,
        "analytics_events_transferred": 2,
        "views_transferred": 1,
        "inline_usage_events_transferred": 1,
    }


async def test_google_verified_email_link_merges_guest_into_existing_full_and_attaches_google_id(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    guest = await user_service.create_guest_user()
    full_user = await create_password_user(migrated_db_session, email="google-owner@example.com")

    result = await link_service.link_guest_with_google_identity(
        guest_user_id=guest.id,
        identity=GoogleIdentity(
            google_id="google-subject-123",
            email=" GOOGLE-OWNER@example.com ".strip().lower(),
            email_verified_at=GOOGLE_AUTH_DATE,
        ),
    )

    assert result.merge_performed is True
    assert result.user.id == full_user.id
    assert result.linked_providers.google_linked is True
    assert result.linked_providers.has_password is True

    persisted_full_result = await migrated_db_session.execute(select(User).where(User.id == full_user.id))
    persisted_guest_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    persisted_full = persisted_full_result.scalar_one()

    assert persisted_full.google_id == "google-subject-123"
    assert persisted_full.email_verified_at == GOOGLE_AUTH_DATE
    assert persisted_guest_result.scalar_one_or_none() is None


async def test_telegram_link_reuses_existing_full_strictly_by_telegram_id(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    first_guest = await user_service.create_guest_user()
    second_guest = await user_service.create_guest_user()
    second_guest_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=second_guest.id)
    assert second_guest_favorites_id is not None

    guest_only_meme = await create_meme(migrated_db_session)
    await add_saved_meme(
        migrated_db_session,
        collection_id=second_guest_favorites_id,
        meme_id=guest_only_meme.id,
        added_by_user_id=second_guest.id,
    )
    await migrated_db_session.commit()

    first_result = await link_service.link_guest_with_telegram_identity(
        guest_user_id=first_guest.id,
        identity=TelegramIdentity(telegram_id=777_888_999, auth_date=GOOGLE_AUTH_DATE),
    )
    second_result = await link_service.link_guest_with_telegram_identity(
        guest_user_id=second_guest.id,
        identity=TelegramIdentity(telegram_id=777_888_999, auth_date=GOOGLE_AUTH_DATE),
    )

    assert first_result.merge_performed is False
    assert second_result.merge_performed is True
    assert second_result.user.id == first_result.user.id
    assert second_result.linked_providers.telegram_linked is True
    assert second_result.favorites_transferred == 1

    canonical_user_result = await migrated_db_session.execute(select(User).where(User.id == first_guest.id))
    deleted_second_guest_result = await migrated_db_session.execute(select(User).where(User.id == second_guest.id))
    canonical_user = canonical_user_result.scalar_one()

    assert canonical_user.account_type is AccountType.FULL
    assert canonical_user.telegram_id == 777_888_999
    assert deleted_second_guest_result.scalar_one_or_none() is None
    assert await count_favorites_rows(migrated_db_session, owner_id=canonical_user.id) == 1


async def test_link_service_rejects_malformed_inputs_without_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    guest = await user_service.create_guest_user()
    guest_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=guest.id)
    assert guest_favorites_id is not None

    with pytest.raises(InvalidCredentialsError, match="invalid"):
        _ = await link_service.link_guest_with_email_signup(
            guest_user_id=guest.id,
            email="   ",
            password=PASSWORD,
        )

    with pytest.raises(InvalidCredentialsError, match="invalid"):
        _ = await link_service.link_guest_with_email_login(
            guest_user_id=guest.id,
            email="guest@example.com",
            password="   ",
        )

    with pytest.raises(ProviderPayloadInvalidError, match="authorization code is required"):
        _ = await link_service.link_guest_with_google_code(
            guest_user_id=guest.id,
            code="   ",
        )

    persisted_guest_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))

    assert persisted_guest_result.scalar_one().account_type is AccountType.GUEST
    assert await count_favorites_rows(migrated_db_session, owner_id=guest.id) == 0
    assert await get_favorites_collection_id(migrated_db_session, owner_id=guest.id) == guest_favorites_id
    assert merge_log_count_result.scalar_one() == 0


async def test_link_service_rejects_non_guest_callers_without_mutation(
    migrated_db_session: AsyncSession,
) -> None:
    full_user = await create_password_user(migrated_db_session, email="already-full@example.com")
    link_service = build_account_link_service(migrated_db_session)

    with pytest.raises(GuestAccountRequiredError, match="Only guest accounts can be linked"):
        _ = await link_service.link_guest_with_email_signup(
            guest_user_id=full_user.id,
            email="replacement@example.com",
            password=PASSWORD,
        )

    persisted_full_result = await migrated_db_session.execute(select(User).where(User.id == full_user.id))
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))

    assert persisted_full_result.scalar_one().account_type is AccountType.FULL
    assert merge_log_count_result.scalar_one() == 0


async def test_google_link_rejects_unavailable_verified_email_targets_without_guest_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    guest = await user_service.create_guest_user()
    full_user = await create_password_user(migrated_db_session, email="inactive-google@example.com")

    persisted_full_result = await migrated_db_session.execute(select(User).where(User.id == full_user.id))
    persisted_full = persisted_full_result.scalar_one()
    persisted_full.status = AccountStatus.DELETED
    await migrated_db_session.commit()

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await link_service.link_guest_with_google_identity(
            guest_user_id=guest.id,
            identity=GoogleIdentity(
                google_id="google-inactive-target",
                email="inactive-google@example.com",
                email_verified_at=GOOGLE_AUTH_DATE,
            ),
        )

    persisted_guest_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    reloaded_full_result = await migrated_db_session.execute(select(User).where(User.id == full_user.id))

    assert persisted_guest_result.scalar_one().account_type is AccountType.GUEST
    assert reloaded_full_result.scalar_one().google_id is None


async def test_transaction_failures_roll_back_partial_favorites_and_history_transfers(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    link_service = ExplodingAccountLinkService(
        migrated_db_session,
        provider_auth_service=provider_auth_service,
    )

    guest = await user_service.create_guest_user()
    full_user = await create_password_user(migrated_db_session, email="rollback-owner@example.com")
    guest_favorites_id = await get_favorites_collection_id(migrated_db_session, owner_id=guest.id)
    assert guest_favorites_id is not None

    guest_meme = await create_meme(migrated_db_session)
    await add_saved_meme(
        migrated_db_session,
        collection_id=guest_favorites_id,
        meme_id=guest_meme.id,
        added_by_user_id=guest.id,
    )
    await add_guest_history(migrated_db_session, guest_user_id=guest.id, meme_id=str(guest_meme.id))
    await migrated_db_session.commit()

    with pytest.raises(AccountLinkInvariantError, match="forced rollback"):
        _ = await link_service.link_guest_with_email_login(
            guest_user_id=guest.id,
            email="rollback-owner@example.com",
            password=PASSWORD,
        )

    guest_result = await migrated_db_session.execute(select(User).where(User.id == guest.id))
    full_result = await migrated_db_session.execute(select(User).where(User.id == full_user.id))
    analytics_user_ids_result = await migrated_db_session.execute(select(AnalyticsEvent.user_id))
    inline_user_ids_result = await migrated_db_session.execute(select(InlineUsageEvent.user_id))
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))

    assert guest_result.scalar_one().account_type is AccountType.GUEST
    assert full_result.scalar_one().account_type is AccountType.FULL
    assert await count_favorites_rows(migrated_db_session, owner_id=guest.id) == 1
    assert await count_favorites_rows(migrated_db_session, owner_id=full_user.id) == 0
    assert analytics_user_ids_result.scalars().all() == [guest.id, guest.id]
    assert inline_user_ids_result.scalars().all() == [guest.id]
    assert merge_log_count_result.scalar_one() == 0
