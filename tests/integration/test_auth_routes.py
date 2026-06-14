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

from memexpert.api.app import create_app
from memexpert.api.dependencies import FullAccountUserDep
from memexpert.models.user import LoginEvent, User
from memexpert.schemas.user import UserRead
from memexpert.services import AuthService, UserService
from tests.conftest import create_full_user_via_upgrade, reset_test_runtime_state

FULL_ONLY_PROBE_PATH = "/api/v1/test-auth/full-only"
ACCESS_TOKEN_TTL = timedelta(days=30)
ACCESS_COOKIE_NAME = "memexpert_access_token"

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


async def test_me_route_rejects_missing_and_malformed_cookies(
    auth_client: AsyncClient,
) -> None:
    """Both absent and structurally-invalid access cookies must fail with 401."""

    no_cookie_response = await auth_client.get("/api/v1/auth/me")
    assert no_cookie_response.status_code == 401
    assert no_cookie_response.json()["code"] == "invalid_token"
    assert "access session cookie is required" in no_cookie_response.json()["detail"].lower()

    auth_client.cookies.set(ACCESS_COOKIE_NAME, "not-a-jwt")
    try:
        malformed_response = await auth_client.get("/api/v1/auth/me")
    finally:
        auth_client.cookies.delete(ACCESS_COOKIE_NAME)

    assert malformed_response.status_code == 401
    assert malformed_response.json()["code"] == "invalid_token"
    assert "access token is invalid" in malformed_response.json()["detail"].lower()


async def test_guest_route_sets_access_cookie_and_persists_login_event(
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
    # Cookie-only transport: no token fields in the body.
    assert "access_token" not in payload
    assert "token_type" not in payload
    assert "expires_in" not in payload

    set_cookie_header = response.headers["set-cookie"]
    assert f"{ACCESS_COOKIE_NAME}=" in set_cookie_header
    assert "HttpOnly" in set_cookie_header
    assert "Secure" in set_cookie_header
    assert "SameSite=lax".lower() in set_cookie_header.lower()
    assert "Path=/" in set_cookie_header
    # The httpx cookie jar picked up the token for subsequent calls.
    assert auth_client.cookies.get(ACCESS_COOKIE_NAME)

    async with postgres_session_factory() as session:
        login_event_result = await session.execute(
            select(LoginEvent).where(LoginEvent.user_id == payload["user"]["id"])
        )
        login_event = login_event_result.scalar_one()
        # The route captures the HTTP User-Agent header; under the httpx
        # test driver that header is the httpx default user-agent.
        assert login_event.user_agent is not None
        assert "httpx" in login_event.user_agent


async def test_auth_routes_honor_configured_access_cookie_name_end_to_end(
    migrated_db_session: AsyncSession,
    auth_settings_overrides: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = migrated_db_session
    custom_access_cookie_name = "memexpert_custom_session_cookie"

    for key, value in {
        **auth_settings_overrides,
        "AUTH_ACCESS_COOKIE_NAME": custom_access_cookie_name,
    }.items():
        monkeypatch.setenv(key, value)

    await reset_test_runtime_state(flush_redis=True)
    app = create_app()
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="https://testserver") as custom_cookie_client:
            guest_response = await custom_cookie_client.post("/api/v1/auth/guest")
            guest_user_id = guest_response.json()["user"]["id"]

            assert guest_response.status_code == 201
            assert f"{custom_access_cookie_name}=" in guest_response.headers["set-cookie"]
            assert "memexpert_access_token=" not in guest_response.headers["set-cookie"]
            assert custom_cookie_client.cookies.get(custom_access_cookie_name) is not None
            assert custom_cookie_client.cookies.get(ACCESS_COOKIE_NAME) is None

            me_response = await custom_cookie_client.get("/api/v1/auth/me")

            assert me_response.status_code == 200
            assert me_response.json()["id"] == guest_user_id
            assert me_response.json()["account_type"] == "guest"
    finally:
        await reset_test_runtime_state(flush_redis=True)


async def test_current_session_auto_bootstraps_guest_and_returns_linked_providers(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await auth_client.get("/api/v1/auth/session")
    payload = response.json()

    assert response.status_code == 200
    assert f"{ACCESS_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert payload["user"]["account_type"] == "guest"
    assert payload["linked_providers"] == {
        "email": None,
        "email_verified_at": None,
        "has_password": False,
        "google_linked": False,
        "telegram_linked": False,
    }
    assert "access_token" not in payload

    second_response = await auth_client.get("/api/v1/auth/session")
    assert second_response.status_code == 200
    assert second_response.json()["user"]["id"] == payload["user"]["id"]
    assert "set-cookie" not in second_response.headers

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(User).where(User.id == payload["user"]["id"]))
        assert user_count_result.scalar_one().account_type.value == "guest"


async def test_me_route_accepts_cookie_only_caller_and_rejects_expired_tokens(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]

    me_response = await auth_client.get("/api/v1/auth/me")

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
            },
            auth_settings_overrides["AUTH_JWT_SECRET"],
            algorithm="HS256",
        )
    )
    auth_client.cookies.set(ACCESS_COOKIE_NAME, expired_access_token)
    expired_response = await auth_client.get("/api/v1/auth/me")

    assert expired_response.status_code == 401
    assert expired_response.json()["code"] == "expired_token"
    assert "expired" in expired_response.json()["detail"].lower()


async def test_me_route_ignores_authorization_header_without_cookie(
    auth_client: AsyncClient,
) -> None:
    """A bogus bearer header with no cookie must not satisfy the dep.

    Proves the transport is cookie-only: passing
    ``Authorization: Bearer ...`` does not bypass the cookie read.
    """

    response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert "cookie" in response.json()["detail"].lower()


async def test_preferences_route_rejects_missing_cookie(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.patch(
        "/api/v1/auth/preferences",
        headers={"Authorization": "Bearer not-a-jwt"},
        json={"nsfw_enabled": True},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert "cookie" in response.json()["detail"].lower()


async def test_preferences_route_updates_and_persists_current_user_preferences(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]

    response = await auth_client.patch(
        "/api/v1/auth/preferences",
        json={"nsfw_enabled": True, "language": "ru"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == guest_user_id
    assert response.json()["nsfw_enabled"] is True
    assert response.json()["language"] == "ru"
    assert "access_token" not in response.json()

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user_id))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.nsfw_enabled is True
        assert persisted_user.language.value == "ru"


async def test_preferences_route_rejects_extra_empty_and_malformed_bodies(
    auth_client: AsyncClient,
) -> None:
    _ = await auth_client.post("/api/v1/auth/guest")

    invalid_payloads: list[object] = [
        {},
        {"nsfw_enabled": True, "unknown": "value"},
        {"nsfw_enabled": "true"},
        {"nsfw_enabled": None},
        {"language": None},
        {"language": "de"},
    ]
    for payload in invalid_payloads:
        response = await auth_client.patch("/api/v1/auth/preferences", json=payload)

        assert response.status_code == 422


async def test_logout_route_deletes_cookie_without_bumping_nonce(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]
    live_access_token = auth_client.cookies.get(ACCESS_COOKIE_NAME)
    assert live_access_token is not None

    logout_response = await auth_client.post("/api/v1/auth/logout")

    assert logout_response.status_code == 204
    logout_set_cookie = logout_response.headers["set-cookie"]
    assert f'{ACCESS_COOKIE_NAME}=""' in logout_set_cookie or f"{ACCESS_COOKIE_NAME}=;" in logout_set_cookie
    # httpx honors the cookie delete, so the next call has no cookie.
    assert auth_client.cookies.get(ACCESS_COOKIE_NAME) is None

    reverify_response = await auth_client.get("/api/v1/auth/me")
    assert reverify_response.status_code == 401

    # The server-side nonce is unchanged — soft logout — so the
    # pre-logout token still verifies when manually re-attached.
    auth_client.cookies.set(ACCESS_COOKIE_NAME, live_access_token)
    revived_response = await auth_client.get("/api/v1/auth/me")
    assert revived_response.status_code == 200

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user_id))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.token_nonce == 0


async def test_logout_all_route_bumps_nonce_and_kills_existing_tokens(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]
    live_access_token = auth_client.cookies.get(ACCESS_COOKIE_NAME)
    assert live_access_token is not None

    logout_all_response = await auth_client.post("/api/v1/auth/logout-all")
    assert logout_all_response.status_code == 204
    # logout-all clears the caller's cookie too.
    assert auth_client.cookies.get(ACCESS_COOKIE_NAME) is None

    # Re-attach the pre-bump token; the server now rejects it because
    # the nonce claim no longer matches the persisted user row.
    auth_client.cookies.set(ACCESS_COOKIE_NAME, live_access_token)
    reverify_response = await auth_client.get("/api/v1/auth/me")
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
        _ = await guest_client.post("/api/v1/auth/guest")
        guest_probe_response = await guest_client.get(FULL_ONLY_PROBE_PATH)

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
        full_client.cookies.set(ACCESS_COOKIE_NAME, full_session.access_token)
        full_probe_response = await full_client.get(FULL_ONLY_PROBE_PATH)

    assert full_probe_response.status_code == 200
    assert full_probe_response.json()["id"] == str(full_user.id)
    assert full_probe_response.json()["account_type"] == "full"
