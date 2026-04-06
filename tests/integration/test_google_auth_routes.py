"""Integration tests for FastAPI Google auth routes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from memexpert.api.dependencies import DbSessionDep, get_provider_auth_service
from memexpert.core.config import get_settings
from memexpert.models.user import RefreshToken, User
from memexpert.services import ProviderAuthService, UserService

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class MockGoogleFlow:
    """Ordered Google HTTP exchange stub with request-history capture."""

    steps: deque[httpx.Response | Exception]
    history: list[httpx.Request] = field(default_factory=list)

    def build_transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.history.append(request)
            if not self.steps:
                raise AssertionError(f"Unexpected Google request: {request.method} {request.url}")

            step = self.steps.popleft()
            if isinstance(step, Exception):
                raise step
            return step

        return httpx.MockTransport(handler)


def google_response(
    *,
    status_code: int = 200,
    payload: dict[str, object] | None = None,
    text: str | None = None,
) -> httpx.Response:
    """Build a deterministic HTTPX response for the mocked Google exchange."""

    if payload is not None:
        return httpx.Response(status_code=status_code, json=payload)
    if text is not None:
        return httpx.Response(status_code=status_code, text=text)
    return httpx.Response(status_code=status_code)


def build_google_provider_override(
    transport: httpx.AsyncBaseTransport,
) -> Callable[[DbSessionDep], ProviderAuthService]:
    """Build a dependency override that injects a mock Google transport."""

    def override_provider_auth_service(session: DbSessionDep) -> ProviderAuthService:
        return ProviderAuthService.from_settings(
            session,
            settings=get_settings(),
            google_http_transport=transport,
        )

    return override_provider_auth_service


async def test_google_route_sets_refresh_cookie_and_reuses_verified_email_account(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        existing_user = await UserService(session).create_full_user(email="route-existing@example.com")

    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "route-google-subject",
                        "email": "route-existing@example.com",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        flow.build_transport()
    )
    try:
        response = await auth_client.post(
            "/api/v1/auth/google",
            headers={"User-Agent": "Google Chrome"},
            json={"code": "route-oauth-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    payload = response.json()

    assert response.status_code == 200
    assert payload["user"]["id"] == str(existing_user.id)
    assert payload["user"]["google_id"] == "route-google-subject"
    assert payload["user"]["email"] == "route-existing@example.com"
    assert payload["refresh_cookie"]["name"] == cookie_name
    assert auth_client.cookies.get(cookie_name) is not None
    assert len(flow.history) == 2
    assert flow.history[0].method == "POST"
    assert flow.history[1].headers["Authorization"] == "Bearer google-access-token"

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == existing_user.id))
        persisted_user = persisted_user_result.scalar_one()
        refresh_token_result = await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == existing_user.id)
        )
        refresh_token_row = refresh_token_result.scalar_one()

        assert persisted_user.google_id == "route-google-subject"
        assert persisted_user.email_verified_at is not None
        assert refresh_token_row.device_info == "Google Chrome"
        assert refresh_token_row.token_hash != auth_client.cookies.get(cookie_name)


async def test_google_route_returns_typed_conflict_for_telegram_email_collision(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        _ = await UserService(session).create_full_user(
            telegram_id=999888777,
            email="telegram-collision@example.com",
        )

    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "route-google-conflict",
                        "email": "telegram-collision@example.com",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        flow.build_transport()
    )
    try:
        response = await auth_client.post(
            "/api/v1/auth/google",
            json={"code": "route-conflict-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["code"] == "email_already_in_use"
    assert "telegram-backed" in response.json()["detail"].lower()


async def test_google_route_returns_typed_provider_access_denied_for_upstream_401(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    flow = MockGoogleFlow(
        steps=deque([google_response(status_code=401, payload={"error": "invalid_grant"})])
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        flow.build_transport()
    )
    try:
        response = await auth_client.post(
            "/api/v1/auth/google",
            json={"code": "route-denied-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json()["code"] == "provider_access_denied"
    assert "denied" in response.json()["detail"].lower()


async def test_google_route_rejects_blank_codes_and_malformed_userinfo_before_side_effects(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    blank_code_response = await auth_client.post(
        "/api/v1/auth/google",
        json={"code": "   "},
    )

    malformed_flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(text="not-json"),
            ]
        )
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        malformed_flow.build_transport()
    )
    try:
        malformed_response = await auth_client.post(
            "/api/v1/auth/google",
            json={"code": "route-malformed-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    assert blank_code_response.status_code == 401
    assert blank_code_response.json()["code"] == "provider_payload_invalid"
    assert "authorization code is required" in blank_code_response.json()["detail"].lower()

    assert malformed_response.status_code == 401
    assert malformed_response.json()["code"] == "provider_payload_invalid"
    assert "malformed" in malformed_response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(User).where(User.google_id.is_not(None)))
        refresh_token_result = await session.execute(select(RefreshToken))

        assert user_count_result.scalars().all() == []
        assert refresh_token_result.scalars().all() == []
