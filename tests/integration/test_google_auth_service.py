"""Integration tests for Google provider-auth service flows."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountStatus, AccountType, CollectionKind
from memexpert.models.user import AccountMergeLog, LoginEvent, User
from memexpert.services import (
    AccountLinkResult,
    AccountLinkService,
    AccountUnavailableError,
    AuthService,
    AuthSession,
    EmailAlreadyInUseError,
    ProviderAccessDeniedError,
    ProviderAuthService,
    ProviderPayloadInvalidError,
    UserService,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

GOOGLE_CLIENT_ID = "service-test-google-client-id"
GOOGLE_CLIENT_SECRET = "service-test-google-client-secret"
GOOGLE_REDIRECT_URI = "https://memexpert.test/auth/google/callback"
GOOGLE_TOKEN_URL = "https://google.test/token"
GOOGLE_USERINFO_URL = "https://google.test/userinfo"
GOOGLE_TIMEOUT_SECONDS = 5.0


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


JWT_SECRET = "google-service-test-jwt-secret-with-32-byte-minimum"


def build_provider_auth_service(
    migrated_db_session: AsyncSession,
    *,
    google_http_transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderAuthService:
    return ProviderAuthService(
        migrated_db_session,
        password_hash_rounds=12,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
        google_redirect_uri=GOOGLE_REDIRECT_URI,
        google_token_url=GOOGLE_TOKEN_URL,
        google_userinfo_url=GOOGLE_USERINFO_URL,
        google_timeout_seconds=GOOGLE_TIMEOUT_SECONDS,
        google_http_transport=google_http_transport,
    )


def build_account_link_service(
    migrated_db_session: AsyncSession,
    *,
    google_http_transport: httpx.AsyncBaseTransport | None = None,
) -> AccountLinkService:
    return AccountLinkService(
        migrated_db_session,
        provider_auth_service=build_provider_auth_service(
            migrated_db_session, google_http_transport=google_http_transport,
        ),
    )


def build_auth_service(session: AsyncSession) -> AuthService:
    return AuthService(
        session,
        jwt_secret=JWT_SECRET,
        access_token_ttl=timedelta(minutes=15),
    )


async def _issue_session_for(
    migrated_db_session: AsyncSession,
    link_result: AccountLinkResult,
    *,
    user_agent: str | None = None,
) -> AuthSession:
    auth_service = build_auth_service(migrated_db_session)
    return await auth_service.issue_session_for_user(
        link_result.user,
        user_agent=user_agent,
        reload_user=False,
    )


async def test_google_auth_creates_new_user_by_sub_and_records_mocked_exchange_history(
    migrated_db_session: AsyncSession,
) -> None:
    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "google-subject-123",
                        "email": "  NewUser@Example.COM ",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    link_service = build_account_link_service(
        migrated_db_session,
        google_http_transport=flow.build_transport(),
    )

    link_result = await link_service.link_guest_with_google_code(
        guest_user_id=None,
        code="google-auth-code",
    )
    auth_session = await _issue_session_for(
        migrated_db_session, link_result, user_agent="Chrome on macOS",
    )

    assert auth_session.user.account_type is AccountType.FULL
    assert auth_session.user.google_id == "google-subject-123"
    assert auth_session.user.email == "newuser@example.com"
    assert auth_session.user.email_verified_at is not None
    assert len(flow.history) == 2
    assert flow.history[0].method == "POST"
    assert str(flow.history[0].url) == GOOGLE_TOKEN_URL
    assert "code=google-auth-code" in flow.history[0].content.decode("utf-8")
    assert flow.history[1].method == "GET"
    assert str(flow.history[1].url) == GOOGLE_USERINFO_URL
    assert flow.history[1].headers["Authorization"] == "Bearer google-access-token"

    persisted_user_result = await migrated_db_session.execute(
        select(User).where(User.id == auth_session.user.id)
    )
    persisted_user = persisted_user_result.scalar_one()
    assert persisted_user.google_id == "google-subject-123"
    assert persisted_user.email == "newuser@example.com"
    assert persisted_user.email_verified_at is not None

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(
            Collection.owner_id == auth_session.user.id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    login_event_result = await migrated_db_session.execute(
        select(LoginEvent).where(LoginEvent.user_id == auth_session.user.id)
    )
    login_event_row = login_event_result.scalar_one()
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))

    assert favorites_count_result.scalar_one() == 1
    assert login_event_row.user_agent == "Chrome on macOS"
    # Fresh Google sign-in upgrades a bootstrapped guest in place, no merge
    # log because there was no existing account to merge into.
    assert merge_log_count_result.scalar_one() == 0


async def test_google_auth_reuses_verified_non_telegram_email_account_and_stamps_verification(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    existing_user = await create_full_user_via_upgrade(user_service, email="existing@example.com")
    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "google-subject-456",
                        "email": "EXISTING@example.com",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    link_service = build_account_link_service(
        migrated_db_session,
        google_http_transport=flow.build_transport(),
    )

    link_result = await link_service.link_guest_with_google_code(
        guest_user_id=None, code="reuse-code",
    )
    auth_session = await _issue_session_for(migrated_db_session, link_result)

    assert auth_session.user.id == existing_user.id
    assert auth_session.user.google_id == "google-subject-456"
    assert auth_session.user.email == "existing@example.com"
    assert auth_session.user.email_verified_at is not None

    persisted_user_result = await migrated_db_session.execute(select(User).where(User.id == existing_user.id))
    persisted_user = persisted_user_result.scalar_one()
    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))

    assert persisted_user.google_id == "google-subject-456"
    assert persisted_user.email_verified_at is not None
    # Exactly one full account survives — the transient guest was merged
    # into the existing account and deleted.
    assert user_count_result.scalar_one() == 1


async def test_google_auth_does_not_reuse_unverified_email_matches_and_creates_google_only_account(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    existing_user = await create_full_user_via_upgrade(user_service, email="owner@example.com")
    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "google-subject-unverified",
                        "email": "owner@example.com",
                        "email_verified": False,
                    }
                ),
            ]
        )
    )
    link_service = build_account_link_service(
        migrated_db_session,
        google_http_transport=flow.build_transport(),
    )

    link_result = await link_service.link_guest_with_google_code(
        guest_user_id=None, code="unverified-code",
    )
    auth_session = await _issue_session_for(migrated_db_session, link_result)

    assert auth_session.user.id != existing_user.id
    assert auth_session.user.google_id == "google-subject-unverified"
    assert auth_session.user.email is None
    assert auth_session.user.email_verified_at is None

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    google_only_user_result = await migrated_db_session.execute(
        select(User).where(User.id == auth_session.user.id)
    )
    google_only_user = google_only_user_result.scalar_one()

    # Two users: the original email-only account and the new Google-only
    # account upgraded in place from the bootstrapped guest.
    assert user_count_result.scalar_one() == 2
    assert google_only_user.email is None
    assert google_only_user.google_id == "google-subject-unverified"


async def test_google_auth_rejects_telegram_email_collisions_without_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    telegram_user = await create_full_user_via_upgrade(
        user_service, telegram_id=123456789, email="telegram@example.com",
    )
    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "google-subject-conflict",
                        "email": "telegram@example.com",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    link_service = build_account_link_service(
        migrated_db_session,
        google_http_transport=flow.build_transport(),
    )

    with pytest.raises(EmailAlreadyInUseError, match="Telegram-backed"):
        _ = await link_service.link_guest_with_google_code(
            guest_user_id=None, code="conflict-code",
        )

    persisted_user_result = await migrated_db_session.execute(select(User).where(User.id == telegram_user.id))
    persisted_user = persisted_user_result.scalar_one()
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))
    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))

    assert persisted_user.google_id is None
    assert login_event_count_result.scalar_one() == 0
    # Bootstrapped guest must be rolled back atomically.
    assert user_count_result.scalar_one() == 1


async def test_google_auth_rejects_inactive_verified_email_reuse_without_attaching_google_id(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    existing_user = await create_full_user_via_upgrade(user_service, email="inactive@example.com")

    existing_user_result = await migrated_db_session.execute(select(User).where(User.id == existing_user.id))
    persisted_user = existing_user_result.scalar_one()
    persisted_user.status = AccountStatus.DELETED
    await migrated_db_session.commit()

    flow = MockGoogleFlow(
        steps=deque(
            [
                google_response(payload={"access_token": "google-access-token"}),
                google_response(
                    payload={
                        "sub": "google-subject-inactive",
                        "email": "inactive@example.com",
                        "email_verified": True,
                    }
                ),
            ]
        )
    )
    link_service = build_account_link_service(
        migrated_db_session,
        google_http_transport=flow.build_transport(),
    )

    with pytest.raises(AccountUnavailableError, match="not available"):
        _ = await link_service.link_guest_with_google_code(
            guest_user_id=None, code="inactive-code",
        )

    reloaded_user_result = await migrated_db_session.execute(select(User).where(User.id == existing_user.id))
    reloaded_user = reloaded_user_result.scalar_one()
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))

    assert reloaded_user.google_id is None
    assert login_event_count_result.scalar_one() == 0


@pytest.mark.parametrize(
    ("code", "flow", "message_fragment"),
    [
        (
            "   ",
            None,
            "authorization code is required",
        ),
        (
            "timeout-code",
            MockGoogleFlow(steps=deque([httpx.ReadTimeout("token exchange timed out")])).build_transport(),
            "timed out",
        ),
        (
            "denied-code",
            MockGoogleFlow(
                steps=deque([google_response(status_code=401, payload={"error": "invalid_grant"})])
            ).build_transport(),
            "was denied",
        ),
        (
            "malformed-code",
            MockGoogleFlow(
                steps=deque(
                    [
                        google_response(payload={"access_token": "google-access-token"}),
                        google_response(text="not-json"),
                    ]
                )
            ).build_transport(),
            "userinfo response is malformed",
        ),
        (
            "missing-sub-code",
            MockGoogleFlow(
                steps=deque(
                    [
                        google_response(payload={"access_token": "google-access-token"}),
                        google_response(payload={"email": "user@example.com", "email_verified": True}),
                    ]
                )
            ).build_transport(),
            "missing sub",
        ),
    ],
)
async def test_google_auth_surfaces_typed_upstream_and_payload_failures_without_side_effects(
    migrated_db_session: AsyncSession,
    code: str,
    flow: httpx.AsyncBaseTransport | None,
    message_fragment: str,
) -> None:
    link_service = build_account_link_service(
        migrated_db_session,
        google_http_transport=flow,
    )

    expected_error: type[ProviderPayloadInvalidError] | type[ProviderAccessDeniedError]
    if (
        "authorization code is required" in message_fragment
        or "malformed" in message_fragment
        or "missing sub" in message_fragment
    ):
        expected_error = ProviderPayloadInvalidError
    else:
        expected_error = ProviderAccessDeniedError
    with pytest.raises(expected_error, match=message_fragment):
        _ = await link_service.link_guest_with_google_code(guest_user_id=None, code=code)

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))

    # Upstream/payload failures happen before any bootstrap is attempted,
    # so no user rows should exist.
    assert user_count_result.scalar_one() == 0
    assert login_event_count_result.scalar_one() == 0
