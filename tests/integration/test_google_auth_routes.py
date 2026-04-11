"""Integration tests for FastAPI Google auth routes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import func, select

from memexpert.api.dependencies import DbSessionDep, get_provider_auth_service
from memexpert.core.config import get_settings
from memexpert.models.enums import AccountType
from memexpert.models.user import AccountMergeLog, RefreshToken, User
from memexpert.services import ProviderAuthService, UserService
from tests.conftest import create_full_user_via_upgrade

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
        existing_user = await create_full_user_via_upgrade(
            UserService(session), email="route-existing@example.com",
        )

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


async def test_google_route_anonymous_caller_upgrades_bootstrapped_guest_without_merge_log(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fresh Google sign-in from an anonymous caller upgrades a throwaway guest in place.

    Under Variant C a previously-unknown google_id takes the
    ``_upgrade_guest_in_place`` branch (bootstrap a guest, upgrade it,
    return session). No merge happens because there's no existing
    account to merge into, so ``account_merge_logs`` must stay empty.
    """

    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "anon-google-subject",
                        "email": "anon-google@example.com",
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
            json={"code": "anon-oauth-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert payload["user"]["account_type"] == "full"
    assert payload["user"]["google_id"] == "anon-google-subject"
    assert payload["user"]["email"] == "anon-google@example.com"

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        full_count_result = await session.execute(
            select(func.count()).select_from(User).where(User.account_type == AccountType.FULL)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        # Exactly one user row: the bootstrapped guest, upgraded in place
        # to a full account. No leftover transient guest, no merge log.
        assert user_count_result.scalar_one() == 1
        assert full_count_result.scalar_one() == 1
        assert merge_log_count_result.scalar_one() == 0


async def test_google_route_rejects_full_account_caller_with_guest_required(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2: a full-account caller on /auth/google gets 403 guest_account_required."""

    # Seed a full user + bearer session via an earlier Google signup.
    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "seed-token"}),
                google_response(
                    payload={
                        "sub": "seed-sub",
                        "email": "seed@example.com",
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
        seed_response = await auth_client.post(
            "/api/v1/auth/google",
            json={"code": "seed-oauth-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()
    assert seed_response.status_code == 200
    access_token = seed_response.json()["access_token"]

    second_flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "second-token"}),
                google_response(
                    payload={
                        "sub": "second-sub",
                        "email": "second@example.com",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        second_flow.build_transport()
    )
    try:
        conflict_response = await auth_client.post(
            "/api/v1/auth/google",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"code": "second-oauth-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    assert conflict_response.status_code == 403
    assert conflict_response.json()["code"] == "guest_account_required"

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        # Only the seeded account exists; the second call did not touch
        # the Google transport (guard fires before the service runs).
        assert user_count_result.scalar_one() == 1
    assert len(second_flow.history) == 0


async def test_google_route_returns_typed_conflict_for_telegram_email_collision(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        _ = await create_full_user_via_upgrade(
            UserService(session),
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
