"""Integration tests for the email/password provider-auth service."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import bcrypt
import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountStatus, AccountType, CollectionKind
from memexpert.models.user import LoginEvent, User
from memexpert.services import (
    AccountLinkService,
    AccountUnavailableError,
    AuthConfigurationError,
    AuthService,
    EmailAlreadyInUseError,
    InvalidCredentialsError,
    ProviderAuthService,
    UserService,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
JWT_SECRET = "email-service-test-jwt-secret-with-32-byte-minimum"
ACCESS_TOKEN_TTL = timedelta(days=30)


def build_provider_auth_service(
    migrated_db_session: AsyncSession,
    *,
    password_hash_rounds: int = 12,
) -> ProviderAuthService:
    return ProviderAuthService(
        migrated_db_session,
        password_hash_rounds=password_hash_rounds,
    )


def build_auth_service(session: AsyncSession) -> AuthService:
    return AuthService(
        session,
        jwt_secret=JWT_SECRET,
        access_token_ttl=ACCESS_TOKEN_TTL,
    )


def build_account_link_service(session: AsyncSession) -> AccountLinkService:
    return AccountLinkService(
        session,
        provider_auth_service=ProviderAuthService(session, password_hash_rounds=12),
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


async def test_email_signup_hashes_password_bootstraps_favorites_and_issues_session(
    migrated_db_session: AsyncSession,
) -> None:
    link_service = build_account_link_service(migrated_db_session)
    auth_service = build_auth_service(migrated_db_session)

    link_result = await link_service.link_guest_with_email_signup(
        guest_user_id=None,
        email="  NewUser@Example.COM ",
        password=PASSWORD,
    )
    auth_session = await auth_service.issue_session_for_user(
        link_result.user,
        user_agent="  Firefox on macOS  ",
        reload_user=False,
    )
    public_session = auth_session.to_read()

    assert auth_session.user.account_type is AccountType.FULL
    assert auth_session.user.email == "newuser@example.com"
    assert public_session.user.email == "newuser@example.com"
    assert "password_hash" not in public_session.model_dump_json()
    assert public_session.token_type == "bearer"

    persisted_user_result = await migrated_db_session.execute(select(User).where(User.id == auth_session.user.id))
    persisted_user = persisted_user_result.scalar_one()
    assert persisted_user.password_hash is not None
    assert persisted_user.password_hash != PASSWORD
    assert bcrypt.checkpw(PASSWORD.encode("utf-8"), persisted_user.password_hash.encode("utf-8")) is True

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(
            Collection.owner_id == auth_session.user.id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    assert favorites_count_result.scalar_one() == 1

    login_event_result = await migrated_db_session.execute(
        select(LoginEvent).where(LoginEvent.user_id == auth_session.user.id)
    )
    login_event_row = login_event_result.scalar_one()
    assert login_event_row.user_agent == "Firefox on macOS"


async def test_email_signup_rejects_duplicate_email_and_invalid_hashing_config(
    migrated_db_session: AsyncSession,
) -> None:
    link_service = build_account_link_service(migrated_db_session)
    auth_service = build_auth_service(migrated_db_session)

    first_result = await link_service.link_guest_with_email_signup(
        guest_user_id=None,
        email="owner@example.com",
        password=PASSWORD,
    )
    _ = await auth_service.issue_session_for_user(first_result.user, reload_user=False)

    with pytest.raises(EmailAlreadyInUseError, match="already in use"):
        _ = await link_service.link_guest_with_email_signup(
            guest_user_id=None,
            email=" OWNER@example.com ",
            password=PASSWORD,
        )

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))
    assert user_count_result.scalar_one() == 1
    assert login_event_count_result.scalar_one() == 1

    with pytest.raises(AuthConfigurationError, match="between 4 and 31"):
        _ = build_provider_auth_service(migrated_db_session, password_hash_rounds=0)


async def test_email_login_normalizes_email_and_uses_bcrypt_comparison(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    auth_service = build_auth_service(migrated_db_session)
    full_user = await create_full_user_via_upgrade(
        user_service,
        email="login@example.com",
        password_hash=hash_password(PASSWORD),
    )

    link_result = await link_service.link_guest_with_email_login(
        guest_user_id=None,
        email="  LOGIN@example.com  ",
        password=PASSWORD,
    )
    auth_session = await auth_service.issue_session_for_user(
        link_result.user, user_agent="Safari", reload_user=False,
    )

    assert auth_session.user.id == full_user.id
    assert auth_session.user.account_type is AccountType.FULL

    login_event_result = await migrated_db_session.execute(
        select(LoginEvent).where(LoginEvent.user_id == full_user.id)
    )
    login_event_row = login_event_result.scalar_one()
    assert login_event_row.user_agent == "Safari"


async def test_email_login_rejects_wrong_password_and_missing_stored_hash_without_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    password_user = await create_full_user_via_upgrade(
        user_service,
        email="password-user@example.com",
        password_hash=hash_password(PASSWORD),
    )
    missing_hash_user = await create_full_user_via_upgrade(
        user_service, email="missing-hash@example.com",
    )

    with pytest.raises(InvalidCredentialsError, match="invalid"):
        _ = await link_service.link_guest_with_email_login(
            guest_user_id=None,
            email="password-user@example.com",
            password="wrong-password",
        )

    with pytest.raises(InvalidCredentialsError, match="not available"):
        _ = await link_service.link_guest_with_email_login(
            guest_user_id=None,
            email="missing-hash@example.com",
            password=PASSWORD,
        )

    login_event_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(LoginEvent)
        .where(LoginEvent.user_id.in_([password_user.id, missing_hash_user.id]))
    )
    assert login_event_count_result.scalar_one() == 0


async def test_email_login_rejects_non_active_accounts(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    unavailable_user = await create_full_user_via_upgrade(
        user_service,
        email="inactive@example.com",
        password_hash=hash_password(PASSWORD),
    )

    unavailable_user_result = await migrated_db_session.execute(select(User).where(User.id == unavailable_user.id))
    persisted_user = unavailable_user_result.scalar_one()
    persisted_user.status = AccountStatus.DELETED
    await migrated_db_session.commit()

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await link_service.link_guest_with_email_login(
            guest_user_id=None,
            email="inactive@example.com",
            password=PASSWORD,
        )

    login_event_count_result = await migrated_db_session.execute(
        select(func.count()).select_from(LoginEvent).where(LoginEvent.user_id == unavailable_user.id)
    )
    assert login_event_count_result.scalar_one() == 0
