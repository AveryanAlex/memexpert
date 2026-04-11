"""Integration tests for FastAPI email/password auth routes."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import bcrypt
import pytest
from sqlalchemy import func, select

from memexpert.models.enums import AccountStatus
from memexpert.models.user import RefreshToken, User
from memexpert.services import UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PASSWORD = "correct-horse-battery"
REFRESH_TOKEN_TTL = timedelta(days=30)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


async def test_email_signup_route_sets_refresh_cookie_and_returns_only_public_session_data(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/email/signup",
        headers={"User-Agent": "Mobile Safari"},
        json={
            "email": "  RouteUser@Example.COM ",
            "password": PASSWORD,
        },
    )

    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    payload = response.json()
    set_cookie_header = response.headers["set-cookie"].lower()

    assert response.status_code == 201
    assert payload["user"]["account_type"] == "full"
    assert payload["user"]["email"] == "routeuser@example.com"
    assert "refresh_token" not in payload
    assert "password_hash" not in response.text
    assert payload["refresh_cookie"] == {
        "name": cookie_name,
        "path": "/api/v1/auth/refresh",
        "max_age": int(REFRESH_TOKEN_TTL.total_seconds()),
        "secure": True,
        "http_only": True,
        "same_site": auth_settings_overrides["AUTH_REFRESH_COOKIE_SAMESITE"],
        "domain": None,
    }
    assert auth_client.cookies.get(cookie_name) is not None
    assert f"{cookie_name.lower()}=" in set_cookie_header
    assert "httponly" in set_cookie_header
    assert "secure" in set_cookie_header

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.email == "routeuser@example.com"))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.password_hash is not None
        assert bcrypt.checkpw(PASSWORD.encode("utf-8"), persisted_user.password_hash.encode("utf-8")) is True

        refresh_token_result = await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == persisted_user.id)
        )
        refresh_token_row = refresh_token_result.scalar_one()
        assert refresh_token_row.device_info == "Mobile Safari"
        assert refresh_token_row.token_hash != auth_client.cookies.get(cookie_name)


async def test_email_signup_route_rejects_duplicate_email_with_typed_conflict(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        _ = await create_full_user_via_upgrade(
            user_service,
            email="duplicate@example.com",
            password_hash=hash_password(PASSWORD),
        )

    response = await auth_client.post(
        "/api/v1/auth/email/signup",
        json={
            "email": " DUPLICATE@example.com ",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "email_already_in_use"
    assert "already in use" in response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        total_user_count_result = await session.execute(select(func.count()).select_from(User))
        user_count_result = await session.execute(
            select(func.count()).select_from(User).where(User.email == "duplicate@example.com")
        )
        refresh_token_count_result = await session.execute(select(func.count()).select_from(RefreshToken))
        # R1: bootstrap rollback — the transient guest created inside
        # AccountLinkService for the failing signup must not leak, so the
        # only user row is the seeded duplicate and zero refresh tokens
        # reference a throwaway bootstrap.
        assert total_user_count_result.scalar_one() == 1
        assert user_count_result.scalar_one() == 1
        assert refresh_token_count_result.scalar_one() == 0


async def test_email_signup_route_rejects_full_account_caller_with_guest_required(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2: a caller authenticated as a full account may not bootstrap a second account."""

    # First signup creates a full account and primes auth_client's bearer token.
    signup_response = await auth_client.post(
        "/api/v1/auth/email/signup",
        json={"email": "full-caller@example.com", "password": PASSWORD},
    )
    assert signup_response.status_code == 201
    access_token = signup_response.json()["access_token"]

    conflict_response = await auth_client.post(
        "/api/v1/auth/email/signup",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"email": "second-account@example.com", "password": PASSWORD},
    )

    assert conflict_response.status_code == 403
    assert conflict_response.json()["code"] == "guest_account_required"

    async with postgres_session_factory() as session:
        total_user_count_result = await session.execute(select(func.count()).select_from(User))
        assert total_user_count_result.scalar_one() == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "valid@example.com", "password": "        "},
        {"email": "valid@example.com", "password": "short"},
    ],
)
async def test_email_signup_route_rejects_invalid_payloads_before_side_effects(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, str],
) -> None:
    response = await auth_client.post("/api/v1/auth/email/signup", json=payload)

    assert response.status_code == 422

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        refresh_token_count_result = await session.execute(select(func.count()).select_from(RefreshToken))
        assert user_count_result.scalar_one() == 0
        assert refresh_token_count_result.scalar_one() == 0


async def test_email_login_route_accepts_normalized_email_and_reuses_shared_session_contract(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await create_full_user_via_upgrade(user_service,
            email="login@example.com",
            password_hash=hash_password(PASSWORD),
        )

    response = await auth_client.post(
        "/api/v1/auth/email/login",
        headers={"User-Agent": "Firefox"},
        json={
            "email": "  LOGIN@example.com ",
            "password": PASSWORD,
        },
    )

    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    payload = response.json()

    assert response.status_code == 200
    assert payload["user"]["id"] == str(full_user.id)
    assert payload["user"]["account_type"] == "full"
    assert payload["user"]["email"] == "login@example.com"
    assert payload["refresh_cookie"]["name"] == cookie_name
    assert auth_client.cookies.get(cookie_name) is not None

    async with postgres_session_factory() as session:
        refresh_token_result = await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == full_user.id)
        )
        refresh_token_row = refresh_token_result.scalar_one()
        assert refresh_token_row.device_info == "Firefox"


async def test_email_login_route_rejects_wrong_password_and_inactive_accounts(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        wrong_password_user = await create_full_user_via_upgrade(user_service,
            email="wrong-password@example.com",
            password_hash=hash_password(PASSWORD),
        )
        inactive_user = await create_full_user_via_upgrade(user_service,
            email="inactive@example.com",
            password_hash=hash_password(PASSWORD),
        )

        inactive_user_result = await session.execute(select(User).where(User.id == inactive_user.id))
        persisted_inactive_user = inactive_user_result.scalar_one()
        persisted_inactive_user.status = AccountStatus.DELETION_PENDING
        await session.commit()

    wrong_password_response = await auth_client.post(
        "/api/v1/auth/email/login",
        json={
            "email": "wrong-password@example.com",
            "password": "wrong-password",
        },
    )
    inactive_response = await auth_client.post(
        "/api/v1/auth/email/login",
        json={
            "email": "inactive@example.com",
            "password": PASSWORD,
        },
    )

    assert wrong_password_response.status_code == 401
    assert wrong_password_response.json()["code"] == "invalid_credentials"
    assert "invalid" in wrong_password_response.json()["detail"].lower()

    assert inactive_response.status_code == 403
    assert inactive_response.json()["code"] == "account_unavailable"
    assert "not available" in inactive_response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        refresh_token_count_result = await session.execute(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id.in_([wrong_password_user.id, inactive_user.id]))
        )
        assert refresh_token_count_result.scalar_one() == 0
