"""Integration tests for the email/password provider-auth service."""

from __future__ import annotations

from typing import TYPE_CHECKING

import bcrypt
import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountStatus, AccountType, CollectionKind
from memexpert.models.user import RefreshToken, User
from memexpert.services import (
    AccountUnavailableError,
    AuthConfigurationError,
    EmailAlreadyInUseError,
    InvalidCredentialsError,
    ProviderAuthService,
    UserService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"


def build_provider_auth_service(
    migrated_db_session: AsyncSession,
    *,
    password_hash_rounds: int = 12,
) -> ProviderAuthService:
    return ProviderAuthService(
        migrated_db_session,
        password_hash_rounds=password_hash_rounds,
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


async def test_email_signup_hashes_password_bootstraps_favorites_and_issues_session(
    migrated_db_session: AsyncSession,
) -> None:
    provider_auth_service = build_provider_auth_service(migrated_db_session)

    auth_session = await provider_auth_service.signup_with_email(
        email="  NewUser@Example.COM ",
        password=PASSWORD,
        device_info="  Firefox on macOS  ",
    )
    public_session = auth_session.to_read()

    assert auth_session.user.account_type is AccountType.FULL
    assert auth_session.user.email == "newuser@example.com"
    assert public_session.user.email == "newuser@example.com"
    assert "password_hash" not in public_session.model_dump_json()
    assert auth_session.refresh_cookie.path == "/api/v1/auth/refresh"

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

    refresh_token_result = await migrated_db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == auth_session.user.id)
    )
    refresh_token_row = refresh_token_result.scalar_one()
    assert refresh_token_row.token_hash != auth_session.refresh_token
    assert refresh_token_row.device_info == "Firefox on macOS"
    assert refresh_token_row.revoked_at is None


async def test_email_signup_rejects_duplicate_email_and_invalid_hashing_config(
    migrated_db_session: AsyncSession,
) -> None:
    provider_auth_service = build_provider_auth_service(migrated_db_session)

    _ = await provider_auth_service.signup_with_email(email="owner@example.com", password=PASSWORD)

    with pytest.raises(EmailAlreadyInUseError, match="already in use"):
        _ = await provider_auth_service.signup_with_email(email=" OWNER@example.com ", password=PASSWORD)

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    refresh_token_count_result = await migrated_db_session.execute(select(func.count()).select_from(RefreshToken))
    assert user_count_result.scalar_one() == 1
    assert refresh_token_count_result.scalar_one() == 1

    with pytest.raises(AuthConfigurationError, match="between 4 and 31"):
        _ = build_provider_auth_service(migrated_db_session, password_hash_rounds=0)


async def test_email_login_normalizes_email_and_uses_bcrypt_comparison(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    full_user = await user_service.create_full_user(
        email="login@example.com",
        password_hash=hash_password(PASSWORD),
    )

    auth_session = await provider_auth_service.login_with_email(
        email="  LOGIN@example.com  ",
        password=PASSWORD,
        device_info="Safari",
    )

    assert auth_session.user.id == full_user.id
    assert auth_session.user.account_type is AccountType.FULL

    refresh_token_result = await migrated_db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == full_user.id)
    )
    refresh_token_row = refresh_token_result.scalar_one()
    assert refresh_token_row.device_info == "Safari"


async def test_email_login_rejects_wrong_password_and_missing_stored_hash_without_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    password_user = await user_service.create_full_user(
        email="password-user@example.com",
        password_hash=hash_password(PASSWORD),
    )
    missing_hash_user = await user_service.create_full_user(email="missing-hash@example.com")

    with pytest.raises(InvalidCredentialsError, match="invalid"):
        _ = await provider_auth_service.login_with_email(
            email="password-user@example.com",
            password="wrong-password",
        )

    with pytest.raises(InvalidCredentialsError, match="not available"):
        _ = await provider_auth_service.login_with_email(
            email="missing-hash@example.com",
            password=PASSWORD,
        )

    refresh_token_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id.in_([password_user.id, missing_hash_user.id]))
    )
    assert refresh_token_count_result.scalar_one() == 0


async def test_email_login_rejects_non_active_accounts(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    unavailable_user = await user_service.create_full_user(
        email="inactive@example.com",
        password_hash=hash_password(PASSWORD),
    )

    unavailable_user_result = await migrated_db_session.execute(select(User).where(User.id == unavailable_user.id))
    persisted_user = unavailable_user_result.scalar_one()
    persisted_user.status = AccountStatus.DELETED
    await migrated_db_session.commit()

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await provider_auth_service.login_with_email(
            email="inactive@example.com",
            password=PASSWORD,
        )

    refresh_token_count_result = await migrated_db_session.execute(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == unavailable_user.id)
    )
    assert refresh_token_count_result.scalar_one() == 0
