"""Integration tests for the PostgreSQL-backed auth service invariants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override

import jwt
import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountStatus, AccountType, CollectionKind, UserLanguage
from memexpert.models.user import LoginEvent, User
from memexpert.schemas import GuestBootstrapRequest, UserRead
from memexpert.services import (
    AccountUnavailableError,
    AuthConfigurationError,
    AuthenticatedUserNotFoundError,
    AuthService,
    ExpiredTokenError,
    InvalidTokenError,
    UpgradeRequiredError,
    UserService,
    UserStateMismatchError,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

JWT_SECRET = "test-auth-service-secret-with-32-byte-minimum"
ACCESS_TOKEN_TTL = timedelta(days=30)


class SpyUserService(UserService):
    """Track whether AuthService composes the existing guest bootstrap primitive."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.create_guest_user_calls: int = 0

    @override
    async def create_guest_user(
        self,
        *,
        language: UserLanguage = UserLanguage.ANY,
        nsfw_enabled: bool = False,
        commit: bool = True,
    ) -> UserRead:
        self.create_guest_user_calls += 1
        return await super().create_guest_user(
            language=language, nsfw_enabled=nsfw_enabled, commit=commit,
        )


def build_auth_service(
    session: AsyncSession,
    *,
    jwt_secret: str = JWT_SECRET,
    access_token_ttl: timedelta = ACCESS_TOKEN_TTL,
    user_service: UserService | None = None,
) -> AuthService:
    return AuthService(
        session,
        jwt_secret=jwt_secret,
        access_token_ttl=access_token_ttl,
        user_service=user_service,
    )


def build_access_token(
    *,
    user_id: uuid.UUID,
    nonce: int = 0,
    account_type: AccountType = AccountType.GUEST,
    token_type: str = "access",
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> str:
    resolved_issued_at = issued_at or datetime.now(UTC)
    resolved_expires_at = expires_at or (resolved_issued_at + ACCESS_TOKEN_TTL)
    return str(
        jwt.encode(
            {
                "sub": str(user_id),
                "type": token_type,
                "iat": resolved_issued_at,
                "exp": resolved_expires_at,
                "nonce": nonce,
                "account_type": account_type.value,
            },
            JWT_SECRET,
            algorithm="HS256",
        )
    )


async def test_create_guest_session_composes_guest_bootstrap_and_records_login_event(
    migrated_db_session: AsyncSession,
) -> None:
    spy_user_service = SpyUserService(migrated_db_session)
    auth_service = build_auth_service(migrated_db_session, user_service=spy_user_service)

    session = await auth_service.create_guest_session(
        GuestBootstrapRequest(
            language=UserLanguage.RU,
            nsfw_enabled=True,
            device_info="  Safari on macOS  ",
        ),
        ip_address="198.51.100.17",
    )
    public_session = session.to_read()

    assert spy_user_service.create_guest_user_calls == 1
    assert session.user.account_type is AccountType.GUEST
    assert session.user.language is UserLanguage.RU
    assert session.user.nsfw_enabled is True
    # Lazy Favorites: cold-path guest bootstrap does not allocate collections.
    assert session.user.active_save_collection_id is None
    assert session.user.token_nonce == 0
    assert public_session.access_token == session.access_token
    assert public_session.token_type == "bearer"

    access_token_claims = jwt.decode(
        session.access_token,
        JWT_SECRET,
        algorithms=["HS256"],
        options={"require": ["sub", "type", "exp", "iat", "nonce"]},
    )
    assert access_token_claims["sub"] == str(session.user.id)
    assert access_token_claims["type"] == "access"
    assert access_token_claims["nonce"] == 0
    assert access_token_claims["account_type"] == AccountType.GUEST.value
    assert access_token_claims["exp"] - access_token_claims["iat"] == int(ACCESS_TOKEN_TTL.total_seconds())

    login_event_result = await migrated_db_session.execute(
        select(LoginEvent).where(LoginEvent.user_id == session.user.id)
    )
    persisted_login_event = login_event_result.scalar_one()
    assert persisted_login_event.ip_address == "198.51.100.17"
    assert persisted_login_event.user_agent == "Safari on macOS"

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(
            Collection.owner_id == session.user.id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    assert favorites_count_result.scalar_one() == 0


async def test_auth_service_rejects_blank_secret_and_nonpositive_ttls(
    migrated_db_session: AsyncSession,
) -> None:
    with pytest.raises(AuthConfigurationError, match="jwt_secret"):
        _ = build_auth_service(migrated_db_session, jwt_secret="  ")

    with pytest.raises(AuthConfigurationError, match="32 bytes"):
        _ = build_auth_service(migrated_db_session, jwt_secret="short-secret")

    with pytest.raises(AuthConfigurationError, match="access_token_ttl"):
        _ = build_auth_service(migrated_db_session, access_token_ttl=timedelta())


async def test_verify_access_token_rejects_malformed_missing_claim_and_wrong_type_tokens(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    session = await auth_service.create_guest_session()

    with pytest.raises(InvalidTokenError, match="invalid"):
        _ = await auth_service.verify_access_token("not-a-jwt")

    missing_claim_token = str(
        jwt.encode(
            {
                "sub": str(session.user.id),
                "type": "access",
                "exp": datetime.now(UTC) + ACCESS_TOKEN_TTL,
                "account_type": AccountType.GUEST.value,
            },
            JWT_SECRET,
            algorithm="HS256",
        )
    )
    with pytest.raises(InvalidTokenError, match="invalid"):
        _ = await auth_service.verify_access_token(missing_claim_token)

    wrong_type_token = build_access_token(
        user_id=session.user.id,
        account_type=AccountType.GUEST,
        token_type="refresh",
    )
    with pytest.raises(InvalidTokenError, match="unexpected type"):
        _ = await auth_service.verify_access_token(wrong_type_token)


async def test_verify_access_token_rejects_expired_tokens_and_missing_users(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    session = await auth_service.create_guest_session()
    expired_token = build_access_token(
        user_id=session.user.id,
        issued_at=datetime.now(UTC) - timedelta(minutes=10),
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    missing_user_token = build_access_token(user_id=uuid.uuid4())

    with pytest.raises(ExpiredTokenError, match="expired"):
        _ = await auth_service.verify_access_token(expired_token)

    with pytest.raises(AuthenticatedUserNotFoundError, match="no longer exists"):
        _ = await auth_service.verify_access_token(missing_user_token)


async def test_verify_access_token_reloads_current_user_state_and_enforces_full_account_requirement(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    user_service = UserService(migrated_db_session)
    guest_session = await auth_service.create_guest_session()

    with pytest.raises(UpgradeRequiredError, match="full account"):
        _ = await auth_service.verify_access_token(guest_session.access_token, require_full_account=True)

    guest_result = await migrated_db_session.execute(select(User).where(User.id == guest_session.user.id))
    persisted_guest = guest_result.scalar_one()
    persisted_guest.account_type = AccountType.FULL
    await migrated_db_session.commit()

    with pytest.raises(UserStateMismatchError, match="persisted account state"):
        _ = await auth_service.verify_access_token(guest_session.access_token)

    full_user = await create_full_user_via_upgrade(user_service, email="full@example.com")
    full_session = await auth_service.issue_session_for_user(full_user)
    verified_full_user = await auth_service.verify_access_token(
        full_session.access_token,
        require_full_account=True,
    )

    assert verified_full_user.id == full_user.id
    assert verified_full_user.account_type is AccountType.FULL


async def test_verify_access_token_rejects_stale_nonce_after_logout_all(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    user_service = UserService(migrated_db_session)
    guest_session = await auth_service.create_guest_session()

    # Baseline: the fresh token verifies cleanly.
    verified = await auth_service.verify_access_token(guest_session.access_token)
    assert verified.id == guest_session.user.id

    # Nuke every outstanding token for this user.
    new_nonce = await user_service.bump_token_nonce(user_id=guest_session.user.id)
    assert new_nonce == 1

    # The previously-valid token now fails the nonce check.
    with pytest.raises(InvalidTokenError, match="revoked"):
        _ = await auth_service.verify_access_token(guest_session.access_token)

    # A freshly-issued token against the new nonce verifies again.
    reloaded_guest = await user_service.get_by_id(guest_session.user.id)
    assert reloaded_guest is not None
    assert reloaded_guest.token_nonce == 1
    new_session = await auth_service.issue_session_for_user(reloaded_guest)
    verified_new = await auth_service.verify_access_token(new_session.access_token)
    assert verified_new.token_nonce == 1


async def test_issue_session_rejects_non_active_users_without_recording_login_event(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    user_service = UserService(migrated_db_session)
    unavailable_user = await create_full_user_via_upgrade(user_service, email="unavailable@example.com")

    unavailable_user_result = await migrated_db_session.execute(select(User).where(User.id == unavailable_user.id))
    persisted_unavailable_user = unavailable_user_result.scalar_one()
    persisted_unavailable_user.status = AccountStatus.DELETION_PENDING
    await migrated_db_session.commit()

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await auth_service.issue_session_for_user(unavailable_user)

    login_event_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(LoginEvent)
        .where(LoginEvent.user_id == unavailable_user.id)
    )
    assert login_event_count_result.scalar_one() == 0
