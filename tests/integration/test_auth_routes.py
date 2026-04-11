# ruff: noqa: TC001,TC002
"""Integration tests for FastAPI guest-session auth routes and reusable auth guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.api.dependencies import FullAccountUserDep
from memexpert.models.user import LoginEvent, User
from memexpert.schemas.user import UserRead
from memexpert.services import AuthService, UserService
from tests.conftest import create_full_user_via_upgrade

FULL_ONLY_PROBE_PATH = "/api/v1/test-auth/full-only"
ACCESS_TOKEN_TTL = timedelta(days=30)

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


async def test_guest_route_returns_public_session_data_and_persists_login_event(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/guest",
        json={
            "language": "ru",
            "nsfw_enabled": True,
            "device_info": "  Safari on macOS  ",
        },
    )

    payload = response.json()

    assert response.status_code == 201
    assert payload["user"]["account_type"] == "guest"
    assert payload["user"]["language"] == "ru"
    assert payload["user"]["nsfw_enabled"] is True
    assert payload["user"]["token_nonce"] == 0
    assert payload["token_type"] == "bearer"
    assert payload["access_token"]

    async with postgres_session_factory() as session:
        login_event_result = await session.execute(
            select(LoginEvent).where(LoginEvent.user_id == payload["user"]["id"])
        )
        login_event = login_event_result.scalar_one()
        # The route captures the HTTP User-Agent header; under the httpx
        # test driver that header is the httpx default user-agent.
        assert login_event.user_agent is not None
        assert "httpx" in login_event.user_agent


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
                "nonce": 0,
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


async def test_logout_route_is_noop_server_side(auth_client: AsyncClient) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    access_token = guest_response.json()["access_token"]

    logout_response = await auth_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert logout_response.status_code == 204

    # Bearer token still valid after /auth/logout — this is a soft logout.
    reverify_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert reverify_response.status_code == 200


async def test_logout_all_route_bumps_nonce_and_kills_existing_tokens(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    access_token = guest_response.json()["access_token"]
    guest_user_id = guest_response.json()["user"]["id"]

    logout_all_response = await auth_client.post(
        "/api/v1/auth/logout-all",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout_all_response.status_code == 204

    # The previously-valid bearer is now rejected on the next call.
    reverify_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert reverify_response.status_code == 401
    assert reverify_response.json()["code"] == "invalid_token"

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user_id))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.token_nonce == 1


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
