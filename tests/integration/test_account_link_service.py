# ruff: noqa: TC003
"""Integration tests for guest account linking and merge orchestration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionKind,
    ContentKind,
    ContentProcessingStatus,
    UserLanguage,
)
from memexpert.models.recommendation import (
    UserMemeRecommendationState,
    UserRecommendationProfile,
    UserRecommendationProfileStatus,
)
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, InlineUsageEvent, User
from memexpert.services import (
    AccountLinkAlreadyCompletedError,
    AccountLinkInvariantError,
    AccountLinkResult,
    AccountLinkService,
    AccountUnavailableError,
    CollectionService,
    EmailAlreadyInUseError,
    GuestAccountRequiredError,
    InvalidCredentialsError,
    ProviderAuthService,
    ProviderPayloadInvalidError,
    UserNotFoundError,
    UserService,
)
from memexpert.services.provider_auth_service import GoogleIdentity, TelegramIdentity
from memexpert.services.recommendations.feed_sessions import FeedSessionStore
from memexpert.services.recommendations.intent import RecommendationIntentStore
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from memexpert.schemas.user import UserRead

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


class CoordinatedMergeAccountLinkService(AccountLinkService):
    """Pause the first merge after locking users so a second caller must resume stale."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        first_lock_acquired: asyncio.Event,
        release_first: asyncio.Event,
        provider_auth_service: ProviderAuthService | None = None,
    ) -> None:
        super().__init__(session, provider_auth_service=provider_auth_service)
        self._first_lock_acquired = first_lock_acquired
        self._release_first = release_first

    @override
    async def _lock_users_in_order(self, *user_ids: uuid.UUID) -> dict[uuid.UUID, User | None]:
        locked_users = await super()._lock_users_in_order(*user_ids)
        if not self._first_lock_acquired.is_set():
            self._first_lock_acquired.set()
            await self._release_first.wait()
        return locked_users


class CoordinatedUpgradeAccountLinkService(AccountLinkService):
    """Pause the first in-place upgrade after locking the guest so a second caller resumes stale."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        first_lock_acquired: asyncio.Event,
        release_first: asyncio.Event,
        provider_auth_service: ProviderAuthService | None = None,
    ) -> None:
        super().__init__(session, provider_auth_service=provider_auth_service)
        self._first_lock_acquired = first_lock_acquired
        self._release_first = release_first

    @override
    async def _lock_required_user(self, user_id: uuid.UUID) -> User:
        user = await super()._lock_required_user(user_id)
        if not self._first_lock_acquired.is_set():
            self._first_lock_acquired.set()
            await self._release_first.wait()
        return user


async def create_password_user(
    session: AsyncSession,
    *,
    email: str,
    language: UserLanguage = UserLanguage.ANY,
    nsfw_enabled: bool = False,
) -> UserRead:
    provider_auth_service = build_provider_auth_service(session)
    user_service = UserService(session)
    signup_identity = provider_auth_service.prepare_email_signup_identity(
        email=email,
        password=PASSWORD,
    )
    return await create_full_user_via_upgrade(user_service,
        email=signup_identity.email,
        password_hash=signup_identity.password_hash,
        language=language,
        nsfw_enabled=nsfw_enabled,
    )


async def create_meme(session: AsyncSession) -> Meme:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=meme_file_id)
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


async def bootstrap_favorites(session: AsyncSession, *, user_id: uuid.UUID) -> uuid.UUID:
    """Lazily materialize Favorites for a user in tests that need pre-existing rows.

    Under the lazy-Favorites cold path ``create_guest_user`` /
    ``create_full_user_via_upgrade`` no longer bootstrap Favorites.
    Tests that exercise the merge-with-transfer branch of
    ``AccountLinkService`` seed the rows explicitly through this helper.
    """

    collection_service = CollectionService(session)
    favorites = await collection_service.ensure_favorites_collection(user_id)
    return favorites.id


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


async def test_link_guest_with_email_signup_bootstraps_a_fresh_guest_when_id_is_none(
    migrated_db_session: AsyncSession,
) -> None:
    """When guest_user_id=None, the link method bootstraps a throwaway guest and upgrades it in place."""

    link_service = build_account_link_service(migrated_db_session)

    result = await link_service.link_guest_with_email_signup(
        guest_user_id=None,
        email="bootstrap@example.com",
        password=PASSWORD,
    )

    assert result.merge_performed is False
    assert result.user.account_type is AccountType.FULL
    assert result.user.email == "bootstrap@example.com"
    assert result.merge_log_id is None

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))

    assert user_count_result.scalar_one() == 1
    assert merge_log_count_result.scalar_one() == 0


async def test_link_guest_with_email_signup_rolls_back_bootstrap_on_duplicate_email(
    migrated_db_session: AsyncSession,
) -> None:
    """A duplicate-email failure with guest_user_id=None must leave zero bootstrapped rows."""

    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    _ = await create_full_user_via_upgrade(user_service, email="taken@example.com")
    baseline_user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    baseline_user_count = baseline_user_count_result.scalar_one()

    with pytest.raises(EmailAlreadyInUseError):
        _ = await link_service.link_guest_with_email_signup(
            guest_user_id=None,
            email="taken@example.com",
            password=PASSWORD,
        )

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    assert user_count_result.scalar_one() == baseline_user_count


async def test_email_signup_upgrades_guest_in_place_and_preserves_lazy_favorites_state(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    guest = await user_service.create_guest_user(language=UserLanguage.RU, nsfw_enabled=True)

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
    collection_count_result = await migrated_db_session.execute(select(func.count()).select_from(Collection))

    # Upgrade-in-place must not materialize Favorites; the lazy bootstrap
    # only fires when the user actually interacts with a save surface.
    assert persisted_user.active_save_collection_id is None
    assert collection_count_result.scalar_one() == 0
    assert persisted_user.password_hash is not None
    assert merge_log_count_result.scalar_one() == 0


async def test_email_login_merges_guest_into_existing_full_with_deduped_favorites_history_and_audit(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    feed_sessions = AsyncMock(spec=FeedSessionStore)
    intent_store = AsyncMock(spec=RecommendationIntentStore)
    feed_sessions.invalidate_viewer.side_effect = RuntimeError("redis feed cache unavailable")
    intent_store.invalidate.side_effect = RuntimeError("redis intent cache unavailable")
    link_service = AccountLinkService(
        migrated_db_session,
        provider_auth_service=build_provider_auth_service(migrated_db_session),
        recommendation_feed_sessions=feed_sessions,
        recommendation_intent_store=intent_store,
    )

    guest = await user_service.create_guest_user(language=UserLanguage.EN, nsfw_enabled=True)
    full_user = await create_password_user(
        migrated_db_session,
        email="owner@example.com",
        language=UserLanguage.RU,
        nsfw_enabled=False,
    )

    # Lazy Favorites: materialize both sides up front because this test
    # exercises the transfer branch and needs pre-existing rows to save
    # memes into.
    guest_favorites_id = await bootstrap_favorites(migrated_db_session, user_id=guest.id)
    full_favorites_id = await bootstrap_favorites(migrated_db_session, user_id=full_user.id)

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
    first_seen = datetime(2026, 1, 1, tzinfo=UTC)
    migrated_db_session.add_all(
        [
            UserMemeRecommendationState(
                user_id=guest.id,
                meme_id=duplicate_meme.id,
                first_seen_at=first_seen,
                latest_impression_at=first_seen,
                latest_engaged_view_at=first_seen,
                latest_strong_action_at=first_seen,
                impression_count=2,
            ),
            UserMemeRecommendationState(
                user_id=full_user.id,
                meme_id=duplicate_meme.id,
                first_seen_at=first_seen + timedelta(days=1),
                latest_impression_at=first_seen + timedelta(days=2),
                latest_engaged_view_at=first_seen + timedelta(days=2),
                latest_strong_action_at=first_seen + timedelta(days=2),
                impression_count=3,
            ),
            UserMemeRecommendationState(
                user_id=guest.id,
                meme_id=transferred_meme.id,
                first_seen_at=first_seen + timedelta(hours=1),
                latest_impression_at=first_seen + timedelta(hours=1),
                impression_count=1,
            ),
            UserRecommendationProfileStatus(user_id=full_user.id, dirty_since=None),
            UserRecommendationProfile(
                user_id=full_user.id,
                profile_slot=0,
                model_version="test-model",
                profile_version="test-profile",
                signal_count=1,
                total_weight=1.0,
                vector=b"stale",
                generated_at=first_seen,
            ),
        ]
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
    recommendation_states = (
        await migrated_db_session.scalars(
            select(UserMemeRecommendationState)
            .where(UserMemeRecommendationState.user_id == full_user.id)
            .order_by(UserMemeRecommendationState.meme_id)
        )
    ).all()
    recommendation_profile_count = await migrated_db_session.scalar(
        select(func.count())
        .select_from(UserRecommendationProfile)
        .where(UserRecommendationProfile.user_id == full_user.id)
    )
    recommendation_profile_status = await migrated_db_session.get(UserRecommendationProfileStatus, full_user.id)

    assert deleted_guest_result.scalar_one_or_none() is None
    persisted_full = persisted_full_result.scalar_one()
    assert persisted_full.language is UserLanguage.RU
    assert persisted_full.nsfw_enabled is False
    assert persisted_full.email == "owner@example.com"
    assert guest_favorites_result.scalar_one_or_none() is None
    assert set(favorites_meme_ids_result.scalars().all()) == {duplicate_meme.id, transferred_meme.id}
    assert analytics_user_ids_result.scalars().all() == [full_user.id, full_user.id]
    assert inline_user_ids_result.scalars().all() == [full_user.id]
    states_by_meme_id = {state.meme_id: state for state in recommendation_states}
    assert states_by_meme_id[duplicate_meme.id].first_seen_at == first_seen
    assert states_by_meme_id[duplicate_meme.id].latest_impression_at == first_seen + timedelta(days=2)
    assert states_by_meme_id[duplicate_meme.id].latest_engaged_view_at == first_seen + timedelta(days=2)
    assert states_by_meme_id[duplicate_meme.id].latest_strong_action_at == first_seen + timedelta(days=2)
    assert states_by_meme_id[duplicate_meme.id].impression_count == 5
    assert states_by_meme_id[transferred_meme.id].impression_count == 1
    assert recommendation_profile_count == 0
    assert recommendation_profile_status is not None
    assert recommendation_profile_status.dirty_since is not None
    assert feed_sessions.invalidate_viewer.await_args_list == [call(guest.id), call(full_user.id)]
    assert intent_store.invalidate.await_args_list == [
        call(user_id=guest.id),
        call(user_id=full_user.id),
    ]

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


async def test_concurrent_email_login_merge_loser_raises_completed_elsewhere_after_guest_deletion(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_service = UserService(migrated_db_session)
    guest = await user_service.create_guest_user(language=UserLanguage.EN, nsfw_enabled=True)
    full_user = await create_password_user(
        migrated_db_session,
        email="race-owner@example.com",
        language=UserLanguage.RU,
        nsfw_enabled=False,
    )

    guest_favorites_id = await bootstrap_favorites(migrated_db_session, user_id=guest.id)

    guest_meme = await create_meme(migrated_db_session)
    await add_saved_meme(
        migrated_db_session,
        collection_id=guest_favorites_id,
        meme_id=guest_meme.id,
        added_by_user_id=guest.id,
    )
    await add_guest_history(migrated_db_session, guest_user_id=guest.id, meme_id=str(guest_meme.id))
    await migrated_db_session.commit()

    first_lock_acquired = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def merge_once(*, started_event: asyncio.Event | None = None) -> AccountLinkResult | Exception:
        if started_event is not None:
            started_event.set()

        async with postgres_session_factory() as session:
            service = CoordinatedMergeAccountLinkService(
                session,
                first_lock_acquired=first_lock_acquired,
                release_first=release_first,
                provider_auth_service=build_provider_auth_service(session),
            )
            try:
                return await service.link_guest_with_email_login(
                    guest_user_id=guest.id,
                    email="race-owner@example.com",
                    password=PASSWORD,
                )
            except Exception as exc:  # pragma: no cover - assertions below validate the concrete type
                return exc

    first_task = asyncio.create_task(merge_once())
    await first_lock_acquired.wait()

    second_task = asyncio.create_task(merge_once(started_event=second_started))
    await second_started.wait()
    await asyncio.sleep(0)
    release_first.set()

    first_result, second_result = await asyncio.gather(first_task, second_task)
    results = [first_result, second_result]
    successful_results = [result for result in results if isinstance(result, AccountLinkResult)]
    failed_results = [result for result in results if isinstance(result, Exception)]

    assert len(successful_results) == 1
    assert len(failed_results) == 1
    assert isinstance(failed_results[0], AccountLinkAlreadyCompletedError)

    winner = successful_results[0]
    assert winner.merge_performed is True
    assert winner.user.id == full_user.id
    assert winner.deleted_guest_user_id == guest.id
    assert winner.favorites_transferred == 1
    assert winner.duplicate_favorites_skipped == 0
    assert winner.analytics_events_transferred == 2
    assert winner.inline_usage_events_transferred == 1
    assert winner.views_transferred == 1

    async with postgres_session_factory() as verification_session:
        merge_logs_result = await verification_session.execute(select(AccountMergeLog))
        persisted_guest_result = await verification_session.execute(select(User).where(User.id == guest.id))
        analytics_user_ids_result = await verification_session.execute(select(AnalyticsEvent.user_id))
        inline_user_ids_result = await verification_session.execute(select(InlineUsageEvent.user_id))

        merge_logs = merge_logs_result.scalars().all()
        assert len(merge_logs) == 1
        merge_log = merge_logs[0]
        assert merge_log.guest_account_id == guest.id
        assert merge_log.target_account_id == full_user.id
        assert merge_log.favorites_transferred == 1
        assert merge_log.views_transferred == 1
        assert merge_log.details["guest_favorite_rows"] == 1
        assert merge_log.details["favorites_transferred"] == 1
        assert merge_log.details["favorite_duplicates_skipped"] == 0
        assert merge_log.details["analytics_events_transferred"] == 2
        assert merge_log.details["inline_usage_events_transferred"] == 1
        assert persisted_guest_result.scalar_one_or_none() is None
        assert analytics_user_ids_result.scalars().all() == [full_user.id, full_user.id]
        assert inline_user_ids_result.scalars().all() == [full_user.id]
        assert await count_favorites_rows(verification_session, owner_id=full_user.id) == 1


async def test_concurrent_email_signup_upgrade_loser_raises_completed_elsewhere_for_stale_full_guest(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_service = UserService(migrated_db_session)
    guest = await user_service.create_guest_user(language=UserLanguage.RU, nsfw_enabled=True)
    await migrated_db_session.commit()

    first_lock_acquired = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def upgrade_once(*, started_event: asyncio.Event | None = None) -> AccountLinkResult | Exception:
        if started_event is not None:
            started_event.set()

        async with postgres_session_factory() as session:
            service = CoordinatedUpgradeAccountLinkService(
                session,
                first_lock_acquired=first_lock_acquired,
                release_first=release_first,
                provider_auth_service=build_provider_auth_service(session),
            )
            try:
                return await service.link_guest_with_email_signup(
                    guest_user_id=guest.id,
                    email="stale-upgrade@example.com",
                    password=PASSWORD,
                )
            except Exception as exc:  # pragma: no cover - assertions below validate the concrete type
                return exc

    first_task = asyncio.create_task(upgrade_once())
    await first_lock_acquired.wait()

    second_task = asyncio.create_task(upgrade_once(started_event=second_started))
    await second_started.wait()
    await asyncio.sleep(0)
    release_first.set()

    first_result, second_result = await asyncio.gather(first_task, second_task)
    results = [first_result, second_result]
    successful_results = [result for result in results if isinstance(result, AccountLinkResult)]
    failed_results = [result for result in results if isinstance(result, Exception)]

    assert len(successful_results) == 1
    assert len(failed_results) == 1
    assert isinstance(failed_results[0], AccountLinkAlreadyCompletedError)

    winner = successful_results[0]
    assert winner.merge_performed is False
    assert winner.user.id == guest.id
    assert winner.user.account_type is AccountType.FULL
    assert winner.user.email == "stale-upgrade@example.com"
    assert winner.merge_log_id is None

    async with postgres_session_factory() as verification_session:
        persisted_guest_result = await verification_session.execute(select(User).where(User.id == guest.id))
        merge_log_count_result = await verification_session.execute(select(func.count()).select_from(AccountMergeLog))

        persisted_guest = persisted_guest_result.scalar_one()
        assert persisted_guest.account_type is AccountType.FULL
        assert persisted_guest.email == "stale-upgrade@example.com"
        # Lazy Favorites: upgrade does not touch collections.
        assert persisted_guest.active_save_collection_id is None
        assert merge_log_count_result.scalar_one() == 0
        assert await count_favorites_rows(verification_session, owner_id=guest.id) == 0


async def test_link_service_keeps_missing_guest_errors_loud(
    migrated_db_session: AsyncSession,
) -> None:
    link_service = build_account_link_service(migrated_db_session)
    missing_guest_id = uuid.uuid7()

    with pytest.raises(UserNotFoundError, match=str(missing_guest_id)):
        _ = await link_service.link_guest_with_email_signup(
            guest_user_id=missing_guest_id,
            email="missing-guest@example.com",
            password=PASSWORD,
        )


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
    # Lazy Favorites: materialize second guest's Favorites row up front
    # because the test exercises a transfer branch and needs rows to save
    # the meme into.
    second_guest_favorites_id = await bootstrap_favorites(migrated_db_session, user_id=second_guest.id)

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
    # Lazy Favorites invariant: rejection path never touches collections.
    assert await count_favorites_rows(migrated_db_session, owner_id=guest.id) == 0
    assert await get_favorites_collection_id(migrated_db_session, owner_id=guest.id) is None
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
    # Lazy Favorites: materialize explicitly for the transfer-branch test.
    guest_favorites_id = await bootstrap_favorites(migrated_db_session, user_id=guest.id)

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
