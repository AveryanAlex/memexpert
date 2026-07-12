"""Integration tests for guest-only account-link auth routes and provider-state reads."""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

import bcrypt
import httpx
import pytest
from sqlalchemy import func, select

from memexpert.api.dependencies import (
    DbSessionDep,
    ProviderAuthServiceDep,
    get_account_link_service,
    get_provider_auth_service,
)
from memexpert.core.config import get_settings
from memexpert.models.enums import AccountType
from memexpert.models.user import AccountMergeLog, LoginEvent, User
from memexpert.services import AccountLinkService, AuthService, ProviderAuthService, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PASSWORD = "correct-horse-battery"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)
ACCESS_COOKIE_NAME = "memexpert_access_token"


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
    )


def hash_password(password: str) -> str:
    """Build a bcrypt hash for route tests that seed password accounts directly."""

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


class CoordinatedRouteMergeAccountLinkService(AccountLinkService):
    """Pause the first route-level merge after locking rows so the loser resumes stale."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        first_lock_acquired: asyncio.Event,
        second_attempt_started: asyncio.Event,
        release_first: asyncio.Event,
        provider_auth_service: ProviderAuthService | None = None,
    ) -> None:
        super().__init__(session, provider_auth_service=provider_auth_service)
        self._first_lock_acquired: asyncio.Event = first_lock_acquired
        self._second_attempt_started: asyncio.Event = second_attempt_started
        self._release_first: asyncio.Event = release_first

    async def _lock_users_in_order(self, *user_ids: uuid.UUID) -> dict[uuid.UUID, User | None]:
        if not self._first_lock_acquired.is_set():
            locked_users = await super()._lock_users_in_order(*user_ids)
            self._first_lock_acquired.set()
            await self._release_first.wait()
            return locked_users

        self._second_attempt_started.set()
        return await super()._lock_users_in_order(*user_ids)


async def test_email_signup_link_upgrades_guest_in_place_issues_canonical_session_and_updates_read_surface(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post(
        "/api/v1/auth/guest",
        json={"language": "ru", "nsfw_enabled": True},
    )
    guest_payload = guest_response.json()
    guest_user_id = uuid.UUID(guest_payload["user"]["id"])
    # Capture the pre-upgrade guest token so we can later re-attach it
    # and prove verification reloads the row and derives account type
    # from the newly linked identity instead of trusting a stale claim.
    stale_guest_access_token = auth_client.cookies.get(ACCESS_COOKIE_NAME)
    assert stale_guest_access_token is not None

    before_link_response = await auth_client.get("/api/v1/auth/linked-providers")
    link_response = await auth_client.post(
        "/api/v1/auth/email/signup",
        headers={"User-Agent": "Link Safari"},
        json={
            "email": "  LinkUser@Example.com ",
            "password": PASSWORD,
        },
    )
    link_payload = link_response.json()
    # The signup response rotated the cookie to the canonical session.
    linked_providers_response = await auth_client.get("/api/v1/auth/linked-providers")

    # Forge the old guest cookie back onto the client; without an account-type
    # token claim, verification should reload the upgraded row successfully.
    auth_client.cookies.set(ACCESS_COOKIE_NAME, stale_guest_access_token)
    stale_guest_response = await auth_client.get("/api/v1/auth/me")

    assert guest_response.status_code == 201
    assert before_link_response.status_code == 200
    assert before_link_response.json() == {
        "email": None,
        "email_verified_at": None,
        "has_password": False,
        "google_linked": False,
        "telegram_linked": False,
    }

    assert link_response.status_code == 201
    assert link_payload["user"]["id"] == str(guest_user_id)
    assert link_payload["user"]["account_type"] == "full"
    assert link_payload["user"]["email"] == "linkuser@example.com"
    assert "access_token" not in link_payload
    assert "password_hash" not in link_response.text

    assert linked_providers_response.status_code == 200
    assert linked_providers_response.json() == {
        "email": "linkuser@example.com",
        "email_verified_at": None,
        "has_password": True,
        "google_linked": False,
        "telegram_linked": False,
    }

    assert stale_guest_response.status_code == 200
    assert stale_guest_response.json()["account_type"] == "full"
    assert stale_guest_response.json()["email"] == "linkuser@example.com"

    async with postgres_session_factory() as session:
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))
        assert merge_log_count_result.scalar_one() == 0

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.id == guest_user_id))
        persisted_user = persisted_user_result.scalar_one()
        login_event_rows_result = await session.execute(
            select(LoginEvent)
            .where(LoginEvent.user_id == guest_user_id)
            .order_by(LoginEvent.created_at.asc(), LoginEvent.id.asc())
        )
        login_event_rows = login_event_rows_result.scalars().all()

        assert persisted_user.account_type is AccountType.FULL
        assert persisted_user.email == "linkuser@example.com"
        assert persisted_user.password_hash is not None
        assert len(login_event_rows) == 2
        assert login_event_rows[-1].user_agent == "Link Safari"


async def test_email_login_link_wrong_password_leaves_guest_session_intact(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        _ = await create_full_user_via_upgrade(
            user_service,
            email="owner@example.com",
            password_hash=hash_password(PASSWORD),
        )

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]

    failed_link_response = await auth_client.post(
        "/api/v1/auth/email/login",
        json={
            "email": "owner@example.com",
            "password": "wrong-password",
        },
    )
    me_response = await auth_client.get("/api/v1/auth/me")

    assert failed_link_response.status_code == 401
    assert failed_link_response.json()["code"] == "invalid_credentials"

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_user_id
    assert me_response.json()["account_type"] == "guest"

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == uuid.UUID(guest_user_id)))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert persisted_guest_result.scalar_one().account_type is AccountType.GUEST
        assert merge_log_count_result.scalar_one() == 0


async def test_email_login_link_route_concurrent_loser_gets_already_completed_conflict(
    auth_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await create_full_user_via_upgrade(
            user_service,
            email="race-owner@example.com",
            password_hash=hash_password(PASSWORD),
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app),
        base_url="https://testserver",
    ) as bootstrap_client:
        guest_response = await bootstrap_client.post("/api/v1/auth/guest")
        guest_payload = guest_response.json()
        guest_user_id = uuid.UUID(guest_payload["user"]["id"])
        guest_access_token = bootstrap_client.cookies.get(ACCESS_COOKIE_NAME)
        assert guest_access_token is not None

    assert guest_response.status_code == 201

    first_lock_acquired = asyncio.Event()
    second_attempt_started = asyncio.Event()
    release_first = asyncio.Event()

    async def override_account_link_service(
        session: DbSessionDep,
        provider_auth_service: ProviderAuthServiceDep,
    ) -> AccountLinkService:
        return CoordinatedRouteMergeAccountLinkService(
            session,
            first_lock_acquired=first_lock_acquired,
            second_attempt_started=second_attempt_started,
            release_first=release_first,
            provider_auth_service=provider_auth_service,
        )

    async def post_link(client: AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/v1/auth/email/login",
            headers={"User-Agent": "Race Link Browser"},
            json={
                "email": "race-owner@example.com",
                "password": PASSWORD,
            },
        )

    auth_app.dependency_overrides[get_account_link_service] = override_account_link_service
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=auth_app),
                base_url="https://testserver",
            ) as first_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=auth_app),
                base_url="https://testserver",
            ) as second_client,
        ):
            first_client.cookies.set(ACCESS_COOKIE_NAME, guest_access_token)
            second_client.cookies.set(ACCESS_COOKIE_NAME, guest_access_token)
            first_task = asyncio.create_task(post_link(first_client))
            await first_lock_acquired.wait()

            second_task = asyncio.create_task(post_link(second_client))
            await second_attempt_started.wait()
            release_first.set()

            first_response, second_response = await asyncio.gather(first_task, second_task)
            race_results = [(first_client, first_response), (second_client, second_response)]

            successful_results = [
                (client, response)
                for client, response in race_results
                if response.status_code == 200
            ]
            failed_results = [
                (client, response)
                for client, response in race_results
                if response.status_code == 409
            ]

            unexpected_statuses = [
                response.status_code
                for _, response in race_results
                if response.status_code not in {200, 409}
            ]

            assert unexpected_statuses == []
            assert len(successful_results) == 1
            assert len(failed_results) == 1

            _winner_client, winner_response = successful_results[0]
            _loser_client, loser_response = failed_results[0]
            winner_payload = winner_response.json()
            loser_payload = loser_response.json()

            assert winner_payload["user"]["id"] == str(full_user.id)
            assert winner_payload["user"]["account_type"] == "full"
            assert "access_token" not in winner_payload

            assert loser_payload["code"] == "account_link_already_completed"
            assert "already completed elsewhere" in loser_payload["detail"].lower()
            assert "refresh or reload memexpert" in loser_payload["detail"].lower()

    finally:
        auth_app.dependency_overrides.pop(get_account_link_service, None)

    async with postgres_session_factory() as session:
        merge_logs_result = await session.execute(select(AccountMergeLog))
        deleted_guest_result = await session.execute(select(User).where(User.id == guest_user_id))
        login_event_rows_result = await session.execute(
            select(LoginEvent)
            .where(LoginEvent.user_id == full_user.id)
            .order_by(LoginEvent.created_at.asc(), LoginEvent.id.asc())
        )

        merge_logs = merge_logs_result.scalars().all()
        login_event_rows = login_event_rows_result.scalars().all()

        assert len(merge_logs) == 1
        assert merge_logs[0].guest_account_id == guest_user_id
        assert merge_logs[0].target_account_id == full_user.id
        assert deleted_guest_result.scalar_one_or_none() is None
        assert len(login_event_rows) == 1
        assert login_event_rows[0].user_agent == "Race Link Browser"


async def test_google_link_merges_guest_into_existing_full_and_exposes_linked_provider_read_surface(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await create_full_user_via_upgrade(user_service, email="google-owner@example.com")

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = uuid.UUID(guest_response.json()["user"]["id"])
    stale_guest_access_token = auth_client.cookies.get(ACCESS_COOKIE_NAME)
    assert stale_guest_access_token is not None

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
            "/api/v1/auth/google",
            headers={"User-Agent": "Google Link Browser"},
            json={"code": "route-link-google-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    link_payload = link_response.json()
    # Cookie was rotated to the canonical session by the link response.
    linked_providers_response = await auth_client.get("/api/v1/auth/linked-providers")

    auth_client.cookies.set(ACCESS_COOKIE_NAME, stale_guest_access_token)
    stale_guest_response = await auth_client.get("/api/v1/auth/me")

    assert link_response.status_code == 200
    assert link_payload["user"]["id"] == str(full_user.id)
    assert link_payload["user"]["account_type"] == "full"
    assert link_payload["user"]["email"] == "google-owner@example.com"
    assert "access_token" not in link_payload
    assert len(flow.history) == 2

    assert linked_providers_response.status_code == 200
    linked_providers_payload = linked_providers_response.json()
    assert linked_providers_payload == {
        "email": "google-owner@example.com",
        "email_verified_at": linked_providers_payload["email_verified_at"],
        "has_password": False,
        "google_linked": True,
        "telegram_linked": False,
    }
    assert linked_providers_payload["email_verified_at"] is not None

    # The guest was merged into the canonical account and deleted; the stale
    # guest cookie self-heals through the merge log on the next auth-aware read.
    assert stale_guest_response.status_code == 200
    assert stale_guest_response.json()["id"] == str(full_user.id)
    assert stale_guest_response.json()["account_type"] == "full"
    assert f"{ACCESS_COOKIE_NAME}=" in stale_guest_response.headers["set-cookie"]

    async with postgres_session_factory() as session:
        persisted_full_result = await session.execute(select(User).where(User.id == full_user.id))
        deleted_guest_result = await session.execute(select(User).where(User.id == guest_user_id))
        login_event_result = await session.execute(
            select(LoginEvent)
            .where(LoginEvent.user_id == full_user.id)
            .order_by(LoginEvent.occurred_at.asc())
        )
        login_event_rows = login_event_result.scalars().all()
        persisted_full = persisted_full_result.scalar_one()
        merge_log_count_result = await session.execute(
            select(func.count()).select_from(AccountMergeLog).where(AccountMergeLog.guest_account_id == guest_user_id)
        )

        assert persisted_full.google_id == "google-link-subject"
        assert persisted_full.email_verified_at is not None
        assert deleted_guest_result.scalar_one_or_none() is None
        assert len(login_event_rows) == 2
        assert "Google Link Browser" in {row.user_agent for row in login_event_rows}
        assert merge_log_count_result.scalar_one() == 1


async def test_google_link_provider_denial_leaves_guest_session_usable(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]

    flow = MockGoogleFlow(
        steps=deque([google_response(status_code=401, payload={"error": "invalid_grant"})])
    )
    auth_app.dependency_overrides[get_provider_auth_service] = build_google_provider_override(
        flow.build_transport()
    )
    try:
        failed_link_response = await auth_client.post(
            "/api/v1/auth/google",
            json={"code": "denied-google-code"},
        )
    finally:
        auth_app.dependency_overrides.clear()

    me_response = await auth_client.get("/api/v1/auth/me")
    linked_providers_response = await auth_client.get("/api/v1/auth/linked-providers")

    assert failed_link_response.status_code == 401
    assert failed_link_response.json()["code"] == "provider_access_denied"

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
        login_event_count_result = await session.execute(select(func.count()).select_from(LoginEvent))

        assert user_count_result.scalar_one() == 1
        assert login_event_count_result.scalar_one() == 1


async def test_link_routes_reject_full_account_callers_with_guest_only_error(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        auth_service = build_auth_service(session, auth_settings_overrides)
        full_user = await create_full_user_via_upgrade(user_service, email="already-full@example.com")
        full_session = await auth_service.issue_session_for_user(full_user)

    auth_client.cookies.set(ACCESS_COOKIE_NAME, full_session.access_token)
    response = await auth_client.post(
        "/api/v1/auth/email/signup",
        json={
            "email": "replacement@example.com",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "guest_account_required"
    assert "full-account" in response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert user_count_result.scalar_one() == 1
        assert merge_log_count_result.scalar_one() == 0


async def test_email_login_route_merges_guest_when_guest_bearer_is_present(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Under the unified writer path /auth/email/login merges the guest into the target.

    The previous direct-auth contract left the guest intact and issued a
    session on a different account. Variant C collapses signup and login
    onto the same ``link_guest_with_email_*`` primitives, so a guest bearer
    present on ``/auth/email/login`` now rolls the guest's state (favorites,
    analytics, inline usage) into the authenticated target account and the
    transient guest row is deleted. A stale guest bearer is no longer
    usable after the merge.
    """

    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await create_full_user_via_upgrade(
            user_service,
            email="plain-login@example.com",
            password_hash=hash_password(PASSWORD),
        )

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = uuid.UUID(guest_response.json()["user"]["id"])

    login_response = await auth_client.post(
        "/api/v1/auth/email/login",
        json={
            "email": "plain-login@example.com",
            "password": PASSWORD,
        },
    )

    assert login_response.status_code == 200
    assert login_response.json()["user"]["id"] == str(full_user.id)
    assert login_response.json()["user"]["account_type"] == "full"

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == guest_user_id))
        persisted_target_result = await session.execute(select(User).where(User.id == full_user.id))
        merge_log_count_result = await session.execute(
            select(func.count()).select_from(AccountMergeLog).where(AccountMergeLog.guest_account_id == guest_user_id)
        )

        # The guest was merged into the target and deleted; the merge
        # log row records the operation even though the transient guest
        # had no favorites/analytics to transfer.
        assert persisted_guest_result.scalar_one_or_none() is None
        assert persisted_target_result.scalar_one() is not None
        assert merge_log_count_result.scalar_one() == 1


@pytest.mark.parametrize(
    ("endpoint", "payload", "expected_status", "expected_code"),
    [
        (
            "/api/v1/auth/email/signup",
            {"email": "   ", "password": PASSWORD},
            422,
            None,
        ),
        (
            "/api/v1/auth/email/login",
            {"email": "owner@example.com", "password": "short"},
            422,
            None,
        ),
        (
            "/api/v1/auth/google",
            {"code": "   "},
            401,
            "provider_payload_invalid",
        ),
    ],
)
async def test_link_routes_reject_malformed_payloads_without_mutating_guest_state(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    endpoint: str,
    payload: dict[str, str],
    expected_status: int,
    expected_code: str | None,
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]

    response = await auth_client.post(endpoint, json=payload)
    me_response = await auth_client.get("/api/v1/auth/me")

    assert response.status_code == expected_status
    if expected_code is not None:
        assert response.json()["code"] == expected_code

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_user_id
    assert me_response.json()["account_type"] == "guest"

    async with postgres_session_factory() as session:
        persisted_guest_result = await session.execute(select(User).where(User.id == uuid.UUID(guest_user_id)))
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))

        assert persisted_guest_result.scalar_one().account_type is AccountType.GUEST
        assert merge_log_count_result.scalar_one() == 0
