"""Integration tests for guest-only account-link auth routes and provider-state reads."""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

import bcrypt
import httpx
import pytest
from sqlalchemy import func, select

from memexpert.api.dependencies import DbSessionDep, get_provider_auth_service
from memexpert.core.config import get_settings
from memexpert.models.enums import AccountType
from memexpert.models.user import AccountMergeLog, RefreshToken, User
from memexpert.services import AuthService, ProviderAuthService, UserService

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PASSWORD = "correct-horse-battery"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)


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


def build_auth_service(
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


def hash_password(password: str) -> str:
    """Build a bcrypt hash for route tests that seed password accounts directly."""

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


async def test_email_signup_link_upgrades_guest_in_place_issues_canonical_session_and_updates_read_surface(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post(
        "/api/v1/auth/guest",
        json={"language": "ru", "nsfw_enabled": True},
    )
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = uuid.UUID(guest_payload["user"]["id"])
    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    original_refresh_cookie = auth_client.cookies.get(cookie_name)

    before_link_response = await auth_client.get(
        "/api/v1/auth/linked-providers",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )
    link_response = await auth_client.post(
        "/api/v1/auth/link/email/signup",
        headers={
            "Authorization": f"Bearer {guest_access_token}",
            "User-Agent": "Link Safari",
        },
        json={
            "email": "  LinkUser@Example.com ",
            "password": PASSWORD,
        },
    )
    link_payload = link_response.json()
    canonical_access_token = link_payload["session"]["access_token"]
    linked_providers_response = await auth_client.get(
        "/api/v1/auth/linked-providers",
        headers={"Authorization": f"Bearer {canonical_access_token}"},
    )
    stale_guest_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )

    assert guest_response.status_code == 201
    assert before_link_response.status_code == 200
    assert before_link_response.json() == {
        "email": None,
        "email_verified_at": None,
        "has_password": False,
        "google_linked": False,
        "telegram_linked": False,
    }

    assert link_response.status_code == 200
    assert link_payload["session"]["user"]["id"] == str(guest_user_id)
    assert link_payload["session"]["user"]["account_type"] == "full"
    assert link_payload["session"]["user"]["email"] == "linkuser@example.com"
    assert link_payload["session"]["refresh_cookie"]["name"] == cookie_name
    assert auth_client.cookies.get(cookie_name) is not None
    assert auth_client.cookies.get(cookie_name) != original_refresh_cookie
    assert "password_hash" not in link_response.text
    assert link_payload["linked_providers"] == {
        "email": "linkuser@example.com",
        "email_verified_at": None,
        "has_password": True,
        "google_linked": False,
        "telegram_linked": False,
    }
    assert link_payload["merge_summary"] == {
        "merge_performed": False,
        "merge_log_id": None,
        "guest_user_id": str(guest_user_id),
        "canonical_user_id": str(guest_user_id),
        "deleted_guest_user_id": None,
        "favorites_transferred": 0,
        "duplicate_favorites_skipped": 0,
        "analytics_events_transferred": 0,
        "inline_usage_events_transferred": 0,
        "views_transferred": 0,
    }

    assert linked_providers_response.status_code == 200
    assert linked_providers_response.json() == link_payload["linked_providers"]

    assert stale_guest_response.status_code == 401
    assert stale_guest_response.json()["code"] == "invalid_token"

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user_id))
        persisted_user = persisted_user_result.scalar_one()
        refresh_token_rows_result = await session.execute(
            select(RefreshToken)
            .where(RefreshToken.user_id == guest_user_id)
            .order_by(RefreshToken.created_at.asc())
        )
        refresh_token_rows = refresh_token_rows_result.scalars().all()

        assert persisted_user.account_type is AccountType.FULL
        assert persisted_user.email == "linkuser@example.com"
        assert persisted_user.password_hash is not None
        assert len(refresh_token_rows) == 2
        assert refresh_token_rows[-1].device_info == "Link Safari"
        assert refresh_token_rows[-1].token_hash != auth_client.cookies.get(cookie_name)


async def test_email_login_link_wrong_password_preserves_guest_bearer_and_refresh_cookie(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        _ = await user_service.create_full_user(
            email="owner@example.com",
            password_hash=hash_password(PASSWORD),
        )

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = guest_payload["user"]["id"]
    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    original_refresh_cookie = auth_client.cookies.get(cookie_name)

    failed_link_response = await auth_client.post(
        "/api/v1/auth/link/email/login",
        headers={"Authorization": f"Bearer {guest_access_token}"},
        json={
            "email": "owner@example.com",
            "password": "wrong-password",
        },
    )
    cookie_after_failed_link = auth_client.cookies.get(cookie_name)
    me_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )
    refresh_response = await auth_client.post("/api/v1/auth/refresh")

    assert failed_link_response.status_code == 401
    assert failed_link_response.json()["code"] == "invalid_credentials"
    assert cookie_after_failed_link == original_refresh_cookie

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_user_id
    assert me_response.json()["account_type"] == "guest"

    assert refresh_response.status_code == 200
    assert refresh_response.json()["user"]["id"] == guest_user_id
    assert refresh_response.json()["user"]["account_type"] == "guest"
    assert auth_client.cookies.get(cookie_name) != original_refresh_cookie

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == uuid.UUID(guest_user_id)))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert persisted_guest_result.scalar_one().account_type is AccountType.GUEST
        assert merge_log_count_result.scalar_one() == 0


async def test_google_link_merges_guest_into_existing_full_and_exposes_linked_provider_read_surface(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await user_service.create_full_user(email="google-owner@example.com")

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = uuid.UUID(guest_payload["user"]["id"])
    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]

    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "google-link-subject",
                        "email": "google-owner@example.com",
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
        link_response = await auth_client.post(
            "/api/v1/auth/link/google",
            headers={
                "Authorization": f"Bearer {guest_access_token}",
                "User-Agent": "Google Link Browser",
            },
            json={"code": "route-link-google-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    link_payload = link_response.json()
    canonical_access_token = link_payload["session"]["access_token"]
    linked_providers_response = await auth_client.get(
        "/api/v1/auth/linked-providers",
        headers={"Authorization": f"Bearer {canonical_access_token}"},
    )
    stale_guest_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )

    assert link_response.status_code == 200
    assert link_payload["session"]["user"]["id"] == str(full_user.id)
    assert link_payload["session"]["user"]["account_type"] == "full"
    assert link_payload["session"]["user"]["email"] == "google-owner@example.com"
    assert link_payload["linked_providers"] == {
        "email": "google-owner@example.com",
        "email_verified_at": link_payload["linked_providers"]["email_verified_at"],
        "has_password": False,
        "google_linked": True,
        "telegram_linked": False,
    }
    assert link_payload["merge_summary"]["merge_performed"] is True
    assert link_payload["merge_summary"]["guest_user_id"] == str(guest_user_id)
    assert link_payload["merge_summary"]["canonical_user_id"] == str(full_user.id)
    assert link_payload["merge_summary"]["deleted_guest_user_id"] == str(guest_user_id)
    assert auth_client.cookies.get(cookie_name) is not None
    assert len(flow.history) == 2

    assert linked_providers_response.status_code == 200
    assert linked_providers_response.json() == link_payload["linked_providers"]

    assert stale_guest_response.status_code == 401
    assert stale_guest_response.json()["code"] == "invalid_token"

    async with postgres_session_factory() as session:
        persisted_full_result = await session.execute(select(User).where(User.id == full_user.id))
        deleted_guest_result = await session.execute(select(User).where(User.id == guest_user_id))
        refresh_token_result = await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == full_user.id)
        )
        refresh_token_row = refresh_token_result.scalar_one()
        persisted_full = persisted_full_result.scalar_one()

        assert persisted_full.google_id == "google-link-subject"
        assert persisted_full.email_verified_at is not None
        assert deleted_guest_result.scalar_one_or_none() is None
        assert refresh_token_row.device_info == "Google Link Browser"
        assert refresh_token_row.token_hash != auth_client.cookies.get(cookie_name)


async def test_google_link_provider_denial_leaves_guest_session_and_cookie_usable(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = guest_payload["user"]["id"]
    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    original_refresh_cookie = auth_client.cookies.get(cookie_name)

    flow = MockGoogleFlow(
        steps=deque([google_response(status_code=401, payload={"error": "invalid_grant"})])
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        flow.build_transport()
    )
    try:
        failed_link_response = await auth_client.post(
            "/api/v1/auth/link/google",
            headers={"Authorization": f"Bearer {guest_access_token}"},
            json={"code": "denied-google-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    me_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )
    linked_providers_response = await auth_client.get(
        "/api/v1/auth/linked-providers",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )

    assert failed_link_response.status_code == 401
    assert failed_link_response.json()["code"] == "provider_access_denied"
    assert auth_client.cookies.get(cookie_name) == original_refresh_cookie

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_user_id
    assert me_response.json()["account_type"] == "guest"

    assert linked_providers_response.status_code == 200
    assert linked_providers_response.json() == {
        "email": None,
        "email_verified_at": None,
        "has_password": False,
        "google_linked": False,
        "telegram_linked": False,
    }

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        refresh_token_count_result = await session.execute(select(func.count()).select_from(RefreshToken))

        assert user_count_result.scalar_one() == 1
        assert refresh_token_count_result.scalar_one() == 1


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        (
            "/api/v1/auth/link/email/signup",
            {"email": "fresh-link@example.com", "password": PASSWORD},
        ),
        (
            "/api/v1/auth/link/email/login",
            {"email": "owner@example.com", "password": PASSWORD},
        ),
        (
            "/api/v1/auth/link/google",
            {"code": "google-link-code"},
        ),
    ],
)
async def test_link_routes_require_guest_bearer_tokens_before_any_side_effects(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    endpoint: str,
    payload: dict[str, str],
) -> None:
    response = await auth_client.post(endpoint, json=payload)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        refresh_token_count_result = await session.execute(select(func.count()).select_from(RefreshToken))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert user_count_result.scalar_one() == 0
        assert refresh_token_count_result.scalar_one() == 0
        assert merge_log_count_result.scalar_one() == 0


async def test_link_routes_reject_full_account_callers_with_guest_only_error(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        auth_service = build_auth_service(session, auth_settings_overrides)
        full_user = await user_service.create_full_user(email="already-full@example.com")
        full_session = await auth_service.issue_session_for_user(full_user)

    response = await auth_client.post(
        "/api/v1/auth/link/email/signup",
        headers={"Authorization": f"Bearer {full_session.access_token}"},
        json={
            "email": "replacement@example.com",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "guest_account_required"
    assert "guest" in response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert user_count_result.scalar_one() == 1
        assert merge_log_count_result.scalar_one() == 0


async def test_email_login_route_stays_plain_auth_even_when_guest_bearer_is_present(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await user_service.create_full_user(
            email="plain-login@example.com",
            password_hash=hash_password(PASSWORD),
        )

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = uuid.UUID(guest_payload["user"]["id"])

    login_response = await auth_client.post(
        "/api/v1/auth/email/login",
        headers={"Authorization": f"Bearer {guest_access_token}"},
        json={
            "email": "plain-login@example.com",
            "password": PASSWORD,
        },
    )
    guest_me_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )

    assert login_response.status_code == 200
    assert login_response.json()["user"]["id"] == str(full_user.id)
    assert login_response.json()["user"]["account_type"] == "full"

    assert guest_me_response.status_code == 200
    assert guest_me_response.json()["id"] == str(guest_user_id)
    assert guest_me_response.json()["account_type"] == "guest"

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == guest_user_id))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))
        persisted_guest = persisted_guest_result.scalar_one()

        assert persisted_guest.account_type is AccountType.GUEST
        assert merge_log_count_result.scalar_one() == 0


@pytest.mark.parametrize(
    ("endpoint", "payload", "expected_status", "expected_code"),
    [
        (
            "/api/v1/auth/link/email/signup",
            {"email": "   ", "password": PASSWORD},
            422,
            None,
        ),
        (
            "/api/v1/auth/link/email/login",
            {"email": "owner@example.com", "password": "short"},
            422,
            None,
        ),
        (
            "/api/v1/auth/link/google",
            {"code": "   "},
            401,
            "provider_payload_invalid",
        ),
    ],
)
async def test_link_routes_reject_malformed_payloads_without_mutating_guest_state(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    endpoint: str,
    payload: dict[str, str],
    expected_status: int,
    expected_code: str | None,
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = guest_payload["user"]["id"]
    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    original_refresh_cookie = auth_client.cookies.get(cookie_name)

    response = await auth_client.post(
        endpoint,
        headers={"Authorization": f"Bearer {guest_access_token}"},
        json=payload,
    )
    me_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )

    assert response.status_code == expected_status
    if expected_code is not None:
        assert response.json()["code"] == expected_code
    assert auth_client.cookies.get(cookie_name) == original_refresh_cookie

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_user_id
    assert me_response.json()["account_type"] == "guest"

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == uuid.UUID(guest_user_id)))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert persisted_guest_result.scalar_one().account_type is AccountType.GUEST
        assert merge_log_count_result.scalar_one() == 0
