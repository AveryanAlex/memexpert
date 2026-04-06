"""Integration tests for the PostgreSQL-backed auth service invariants."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override

import jwt
import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountStatus, AccountType, CollectionKind, UserLanguage
from memexpert.models.user import RefreshToken, User
from memexpert.schemas import GuestBootstrapRequest, UserRead
from memexpert.services import (
    AccountUnavailableError,
    AuthConfigurationError,
    AuthenticatedUserNotFoundError,
    AuthService,
    ExpiredTokenError,
    InvalidTokenError,
    RefreshTokenReuseError,
    UpgradeRequiredError,
    UserService,
    UserStateMismatchError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

JWT_SECRET = "test-auth-service-secret-with-32-byte-minimum"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)


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
    ) -> UserRead:
        self.create_guest_user_calls += 1
        return await super().create_guest_user(language=language, nsfw_enabled=nsfw_enabled)


def build_auth_service(
    session: AsyncSession,
    *,
    jwt_secret: str = JWT_SECRET,
    access_token_ttl: timedelta = ACCESS_TOKEN_TTL,
    refresh_token_ttl: timedelta = REFRESH_TOKEN_TTL,
    refresh_cookie_secure: bool = False,
    user_service: UserService | None = None,
) -> AuthService:
    return AuthService(
        session,
        jwt_secret=jwt_secret,
        access_token_ttl=access_token_ttl,
        refresh_token_ttl=refresh_token_ttl,
        refresh_cookie_secure=refresh_cookie_secure,
        user_service=user_service,
    )


def build_access_token(
    *,
    user_id: uuid.UUID,
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
                "account_type": account_type.value,
            },
            JWT_SECRET,
            algorithm="HS256",
        )
    )


async def test_create_guest_session_composes_guest_bootstrap_and_persists_hashed_refresh_token(
    migrated_db_session: AsyncSession,
) -> None:
    spy_user_service = SpyUserService(migrated_db_session)
    auth_service = build_auth_service(migrated_db_session, user_service=spy_user_service)

    session = await auth_service.create_guest_session(
        GuestBootstrapRequest(
            language=UserLanguage.RU,
            nsfw_enabled=True,
            device_info="  Safari on macOS  ",
        )
    )
    public_session = session.to_read()

    assert spy_user_service.create_guest_user_calls == 1
    assert session.user.account_type is AccountType.GUEST
    assert session.user.language is UserLanguage.RU
    assert session.user.nsfw_enabled is True
    assert session.user.active_save_collection_id is not None
    assert "refresh_token" not in public_session.model_dump()
    assert public_session.refresh_cookie.name == "memexpert_refresh_token"
    assert public_session.refresh_cookie.path == "/api/v1/auth/refresh"
    assert public_session.refresh_cookie.max_age == int(REFRESH_TOKEN_TTL.total_seconds())
    assert public_session.refresh_cookie.secure is False
    assert public_session.refresh_cookie.http_only is True
    assert public_session.refresh_cookie.same_site == "lax"

    access_token_claims = jwt.decode(
        session.access_token,
        JWT_SECRET,
        algorithms=["HS256"],
        options={"require": ["sub", "type", "exp", "iat"]},
    )
    assert access_token_claims["sub"] == str(session.user.id)
    assert access_token_claims["type"] == "access"
    assert access_token_claims["account_type"] == AccountType.GUEST.value
    assert access_token_claims["exp"] - access_token_claims["iat"] == int(ACCESS_TOKEN_TTL.total_seconds())

    refresh_token_result = await migrated_db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == session.user.id)
    )
    persisted_refresh_token = refresh_token_result.scalar_one()
    assert persisted_refresh_token.token_hash == hashlib.sha256(session.refresh_token.encode("utf-8")).hexdigest()
    assert persisted_refresh_token.token_hash != session.refresh_token
    assert persisted_refresh_token.device_info == "Safari on macOS"
    assert persisted_refresh_token.revoked_at is None
    assert persisted_refresh_token.last_used_at is None

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(
            Collection.owner_id == session.user.id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    assert favorites_count_result.scalar_one() == 1


async def test_auth_service_rejects_blank_secret_and_nonpositive_ttls(
    migrated_db_session: AsyncSession,
) -> None:
    with pytest.raises(AuthConfigurationError, match="jwt_secret"):
        _ = build_auth_service(migrated_db_session, jwt_secret="  ")

    with pytest.raises(AuthConfigurationError, match="32 bytes"):
        _ = build_auth_service(migrated_db_session, jwt_secret="short-secret")

    with pytest.raises(AuthConfigurationError, match="access_token_ttl"):
        _ = build_auth_service(migrated_db_session, access_token_ttl=timedelta())

    with pytest.raises(AuthConfigurationError, match="refresh_token_ttl"):
        _ = build_auth_service(migrated_db_session, refresh_token_ttl=timedelta())


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

    full_user = await user_service.create_full_user(email="full@example.com")
    full_session = await auth_service.issue_session_for_user(full_user)
    verified_full_user = await auth_service.verify_access_token(
        full_session.access_token,
        require_full_account=True,
    )

    assert verified_full_user.id == full_user.id
    assert verified_full_user.account_type is AccountType.FULL


async def test_issue_session_and_refresh_rotation_reject_non_active_users(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    user_service = UserService(migrated_db_session)
    unavailable_user = await user_service.create_full_user(email="unavailable@example.com")

    unavailable_user_result = await migrated_db_session.execute(select(User).where(User.id == unavailable_user.id))
    persisted_unavailable_user = unavailable_user_result.scalar_one()
    persisted_unavailable_user.status = AccountStatus.DELETION_PENDING
    await migrated_db_session.commit()

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await auth_service.issue_session_for_user(unavailable_user)

    unavailable_refresh_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == unavailable_user.id)
    )
    assert unavailable_refresh_count_result.scalar_one() == 0

    active_user = await user_service.create_full_user(email="rotation-check@example.com")
    active_session = await auth_service.issue_session_for_user(active_user)

    active_user_result = await migrated_db_session.execute(select(User).where(User.id == active_user.id))
    persisted_active_user = active_user_result.scalar_one()
    persisted_active_user.status = AccountStatus.DELETED
    await migrated_db_session.commit()

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await auth_service.rotate_refresh_token(active_session.refresh_token)

    await migrated_db_session.rollback()
    refresh_token_result = await migrated_db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == active_user.id)
    )
    refresh_token_row = refresh_token_result.scalar_one()
    assert refresh_token_row.last_used_at is not None
    assert refresh_token_row.revoked_at is None


async def test_refresh_rotation_revokes_predecessor_and_rejected_reuse_persists_last_used_at(
    migrated_db_session: AsyncSession,
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    original_session = await auth_service.create_guest_session(
        GuestBootstrapRequest(device_info="Firefox")
    )

    rotated_session = await auth_service.rotate_refresh_token(
        original_session.refresh_token,
        device_info="Safari",
    )

    assert rotated_session.refresh_token != original_session.refresh_token
    assert rotated_session.user.id == original_session.user.id

    refresh_token_rows_result = await migrated_db_session.execute(
        select(RefreshToken)
        .where(RefreshToken.user_id == original_session.user.id)
        .order_by(RefreshToken.created_at.asc())
    )
    refresh_token_rows = refresh_token_rows_result.scalars().all()
    assert len(refresh_token_rows) == 2

    original_row = next(
        row
        for row in refresh_token_rows
        if row.token_hash == hashlib.sha256(original_session.refresh_token.encode("utf-8")).hexdigest()
    )
    rotated_row = next(
        row
        for row in refresh_token_rows
        if row.token_hash == hashlib.sha256(rotated_session.refresh_token.encode("utf-8")).hexdigest()
    )

    assert original_row.revoked_at is not None
    assert original_row.last_used_at is not None
    assert rotated_row.revoked_at is None
    assert rotated_row.last_used_at is None
    assert rotated_row.device_info == "Safari"

    with pytest.raises(RefreshTokenReuseError, match="already been revoked"):
        _ = await auth_service.rotate_refresh_token(original_session.refresh_token)

    await migrated_db_session.rollback()
    replayed_row_result = await migrated_db_session.execute(
        select(RefreshToken).where(RefreshToken.id == original_row.id)
    )
    replayed_row = replayed_row_result.scalar_one()
    assert replayed_row.last_used_at is not None

    active_refresh_token_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(RefreshToken)
        .where(
            RefreshToken.user_id == original_session.user.id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    assert active_refresh_token_count_result.scalar_one() == 1


async def test_concurrent_refresh_rotation_allows_only_one_active_successor(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    auth_service = build_auth_service(migrated_db_session)
    original_session = await auth_service.create_guest_session()

    async def rotate_once() -> str | Exception:
        async with postgres_session_factory() as session:
            concurrent_auth_service = build_auth_service(session)
            try:
                rotated_session = await concurrent_auth_service.rotate_refresh_token(original_session.refresh_token)
            except Exception as exc:  # pragma: no cover - assertion below validates the concrete type
                return exc
            return rotated_session.refresh_token

    first_result, second_result = await asyncio.gather(rotate_once(), rotate_once())
    results = [first_result, second_result]
    successful_rotations = [result for result in results if isinstance(result, str)]
    failed_rotations = [result for result in results if isinstance(result, Exception)]

    assert len(successful_rotations) == 1
    assert len(failed_rotations) == 1
    assert isinstance(failed_rotations[0], RefreshTokenReuseError)

    async with postgres_session_factory() as verification_session:
        refresh_token_rows_result = await verification_session.execute(
            select(RefreshToken).where(RefreshToken.user_id == original_session.user.id)
        )
        refresh_token_rows = refresh_token_rows_result.scalars().all()

    active_refresh_token_rows = [row for row in refresh_token_rows if row.revoked_at is None]
    assert len(active_refresh_token_rows) == 1
    assert active_refresh_token_rows[0].token_hash == hashlib.sha256(
        successful_rotations[0].encode("utf-8")
    ).hexdigest()
