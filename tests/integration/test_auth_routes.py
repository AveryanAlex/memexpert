# ruff: noqa: TC001,TC002
"""Integration tests for FastAPI guest-session auth routes and reusable auth guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.api.dependencies import FullAccountUserDep
from memexpert.schemas.user import UserRead
from memexpert.services import AuthService, UserService
from tests.conftest import create_full_user_via_upgrade

FULL_ONLY_PROBE_PATH = "/api/v1/test-auth/full-only"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)

full_only_probe_router = APIRouter()


@full_only_probe_router.get(FULL_ONLY_PROBE_PATH, response_model=UserRead, tags=["test-auth"])
async def read_full_only_probe(current_user: FullAccountUserDep) -> UserRead:
    """Expose the reusable full-account dependency through a route-level probe."""

    return current_user


def build_test_auth_service(
    session: AsyncSession,
    auth_settings_overrides: dict[str, str],
) -> AuthService:
    """Build an auth service that matches the test app's env-driven token settings."""

    return AuthService(
        session,
        jwt_secret=auth_settings_overrides["AUTH_JWT_SECRET"],
        access_token_ttl=ACCESS_TOKEN_TTL,
        refresh_token_ttl=REFRESH_TOKEN_TTL,
        refresh_cookie_name=auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"],
        refresh_cookie_secure=True,
        refresh_cookie_samesite=auth_settings_overrides["AUTH_REFRESH_COOKIE_SAMESITE"],
    )


@pytest.mark.parametrize(
    ("authorization", "detail_fragment"),
    [
        (None, "bearer token is required"),
        ("Basic abc123", "bearer scheme"),
        ("Bearer not-a-jwt", "access token is invalid"),
    ],
)
async def test_me_route_rejects_missing_wrong_scheme_and_malformed_tokens(
    auth_client: AsyncClient,
    authorization: str | None,
    detail_fragment: str,
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}

    response = await auth_client.get("/api/v1/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert detail_fragment in response.json()["detail"].lower()


async def test_guest_route_sets_secure_refresh_cookie_and_returns_only_public_session_data(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/guest",
        json={
            "language": "ru",
            "nsfw_enabled": True,
            "device_info": "  Safari on macOS  ",
        },
    )

    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    payload = response.json()
    set_cookie_header = response.headers["set-cookie"].lower()

    assert response.status_code == 201
    assert payload["user"]["account_type"] == "guest"
    assert payload["user"]["language"] == "ru"
    assert payload["user"]["nsfw_enabled"] is True
    assert "refresh_token" not in payload
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
    assert "path=/api/v1/auth/refresh" in set_cookie_header
    assert f"samesite={auth_settings_overrides['AUTH_REFRESH_COOKIE_SAMESITE']}" in set_cookie_header


async def test_me_route_accepts_guest_bearer_tokens_and_rejects_expired_tokens(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    access_token = guest_payload["access_token"]
    guest_user_id = guest_payload["user"]["id"]

    me_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_user_id
    assert me_response.json()["account_type"] == "guest"

    now = datetime.now(UTC)
    expired_access_token = str(
        jwt.encode(
            {
                "sub": guest_user_id,
                "type": "access",
                "iat": now - timedelta(minutes=10),
                "exp": now - timedelta(minutes=5),
                "account_type": "guest",
            },
            auth_settings_overrides["AUTH_JWT_SECRET"],
            algorithm="HS256",
        )
    )
    expired_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_access_token}"},
    )

    assert expired_response.status_code == 401
    assert expired_response.json()["code"] == "expired_token"
    assert "expired" in expired_response.json()["detail"].lower()


async def test_refresh_route_rotates_refresh_cookie_and_rejects_revoked_token_reuse(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    original_refresh_token = auth_client.cookies.get(cookie_name)

    refresh_response = await auth_client.post("/api/v1/auth/refresh")
    rotated_refresh_token = auth_client.cookies.get(cookie_name)

    assert guest_response.status_code == 201
    assert original_refresh_token is not None
    assert refresh_response.status_code == 200
    assert refresh_response.json()["user"]["id"] == guest_response.json()["user"]["id"]
    assert refresh_response.json()["refresh_cookie"]["name"] == cookie_name
    assert rotated_refresh_token is not None
    assert rotated_refresh_token != original_refresh_token
    assert "refresh_token" not in refresh_response.json()

    replay_transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=replay_transport, base_url="https://testserver") as replay_client:
        replay_client.cookies.set(cookie_name, original_refresh_token)
        replay_response = await replay_client.post("/api/v1/auth/refresh")

    assert replay_response.status_code == 401
    assert replay_response.json()["code"] == "invalid_token"
    assert "revoked" in replay_response.json()["detail"].lower()


async def test_refresh_route_rejects_missing_refresh_cookie(
    auth_app: FastAPI,
) -> None:
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        response = await anonymous_client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert "refresh token cookie" in response.json()["detail"].lower()


async def test_full_account_dependency_returns_upgrade_required_for_guests_and_allows_full_users(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    auth_app.include_router(full_only_probe_router)
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as guest_client:
        guest_response = await guest_client.post("/api/v1/auth/guest")
        guest_access_token = guest_response.json()["access_token"]
        guest_probe_response = await guest_client.get(
            FULL_ONLY_PROBE_PATH,
            headers={"Authorization": f"Bearer {guest_access_token}"},
        )

    assert guest_probe_response.status_code == 403
    assert guest_probe_response.json()["code"] == "upgrade_required"
    assert "full account" in guest_probe_response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        user_service = UserService(session)
        auth_service = build_test_auth_service(session, auth_settings_overrides)
        full_user = await create_full_user_via_upgrade(
            user_service, email="full-auth-route@example.com",
        )
        full_session = await auth_service.issue_session_for_user(full_user)

    async with AsyncClient(transport=transport, base_url="https://testserver") as full_client:
        full_probe_response = await full_client.get(
            FULL_ONLY_PROBE_PATH,
            headers={"Authorization": f"Bearer {full_session.access_token}"},
        )

    assert full_probe_response.status_code == 200
    assert full_probe_response.json()["id"] == str(full_user.id)
    assert full_probe_response.json()["account_type"] == "full"
