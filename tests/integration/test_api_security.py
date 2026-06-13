"""Integration tests for shared API security rate limiting, CORS, and browser-targeted CSRF behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from starlette.requests import Request

from memexpert.api.security import SecurityRouteTier, classify_security_route
from memexpert.core.redis import get_async_redis, is_async_redis_initialized, reset_async_redis_state
from memexpert.models.user import LoginEvent, User
from memexpert.services import AccountLinkService, AuthService, UserService
from tests.conftest import create_full_user_via_upgrade
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME, build_test_auth_service

if TYPE_CHECKING:
    import uuid

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ALLOWED_BROWSER_ORIGINS = (
    "https://app.memexpert.net",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://web.telegram.org",
    "https://oauth.telegram.org",
)
DISALLOWED_BROWSER_ORIGIN = "https://evil.example"
BROWSER_REQUESTED_WITH_VALUE = "XMLHttpRequest"


@pytest.fixture
def security_settings_overrides(
    auth_settings_overrides: dict[str, str],
    redis_container_url: str,
) -> dict[str, str]:
    """Tighten every shared security tier to a tiny budget for rate-limit tests."""

    return {
        **auth_settings_overrides,
        "REDIS_URL": redis_container_url,
        "SECURITY_RATE_LIMIT_REDIS_TIMEOUT_SECONDS": "0.1",
        "SECURITY_RATE_LIMIT_AUTH_WRITE_MAX_REQUESTS": "2",
        "SECURITY_RATE_LIMIT_AUTH_WRITE_WINDOW_SECONDS": "60",
        "SECURITY_RATE_LIMIT_SEARCH_FEED_MAX_REQUESTS": "2",
        "SECURITY_RATE_LIMIT_SEARCH_FEED_WINDOW_SECONDS": "60",
        "SECURITY_RATE_LIMIT_WRITE_MAX_REQUESTS": "2",
        "SECURITY_RATE_LIMIT_WRITE_WINDOW_SECONDS": "60",
        "SECURITY_RATE_LIMIT_UPLOAD_MAX_REQUESTS": "2",
        "SECURITY_RATE_LIMIT_UPLOAD_WINDOW_SECONDS": "60",
        "SECURITY_RATE_LIMIT_ADMIN_MAX_REQUESTS": "2",
        "SECURITY_RATE_LIMIT_ADMIN_WINDOW_SECONDS": "60",
    }


def _build_cors_preflight_headers(
    origin: str,
    *,
    requested_headers: str = "Content-Type, X-Requested-With",
) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": requested_headers,
    }


async def _count_auth_side_effects(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        login_event_count_result = await session.execute(select(func.count()).select_from(LoginEvent))
        return user_count_result.scalar_one(), login_event_count_result.scalar_one()


def _build_request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
        }
    )


async def _post_operator_upload(client: AsyncClient) -> Response:
    return await client.post(
        "/api/v1/pipeline/uploads",
        data={
            "source_platform": "telegram",
            "source_id": "channel-1",
            "post_id": "post-1",
            "views": "0",
        },
        files={"file": ("upload.png", b"fake-image-bytes", "image/png")},
    )


async def _issue_session_cookie(
    session_factory: async_sessionmaker[AsyncSession],
    auth_settings_overrides: dict[str, str],
    *,
    email: str,
    is_admin: bool = False,
) -> str:
    async with session_factory() as session:
        user_service = UserService(session)
        auth_service: AuthService = build_test_auth_service(session, auth_settings_overrides)
        user = await create_full_user_via_upgrade(user_service, email=email)
        persisted_user = await session.get(User, user.id)
        assert persisted_user is not None
        persisted_user.is_admin = is_admin
        await session.commit()
        auth_session = await auth_service.issue_session_for_user(user)
        return auth_session.access_token


async def test_safe_read_routes_keep_redis_runtime_lazy_until_first_protected_request(
    security_client: AsyncClient,
) -> None:
    assert is_async_redis_initialized() is False

    health_response = await security_client.get("/health")
    version_response = await security_client.get("/api/v1/")

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert version_response.status_code == 200
    assert version_response.json() == {"version": "v1", "status": "available"}
    assert is_async_redis_initialized() is False

    guest_response = await security_client.post("/api/v1/auth/guest")

    assert guest_response.status_code == 201
    assert guest_response.headers["X-RateLimit-Limit"] == "2"
    assert guest_response.headers["X-RateLimit-Remaining"] == "1"
    assert guest_response.headers["X-RateLimit-Tier"] == "auth_write"
    assert is_async_redis_initialized() is True


async def test_safe_read_redis_reset_helper_flushes_cached_state(
    security_client: AsyncClient,
) -> None:
    bootstrap_response = await security_client.post("/api/v1/auth/guest")

    assert bootstrap_response.status_code == 201

    redis_client = get_async_redis()
    await redis_client.set("security:test:key", "1")
    assert await redis_client.get("security:test:key") == "1"

    await reset_async_redis_state(flushdb=True)

    assert is_async_redis_initialized() is False

    fresh_client = get_async_redis()
    assert await fresh_client.get("security:test:key") is None


async def test_redis_unavailable_blocks_unsafe_auth_guest_bootstrap_with_typed_503(
    unavailable_security_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await unavailable_security_client.post(
        "/api/v1/auth/guest",
        json={"language": "ru", "nsfw_enabled": True},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "rate_limiter_unavailable"
    assert "rate limiter" in response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        assert user_count_result.scalar_one() == 0


async def test_safe_read_routes_stay_available_when_redis_unavailable(
    unavailable_security_client: AsyncClient,
) -> None:
    assert is_async_redis_initialized() is False

    health_response = await unavailable_security_client.get("/health")
    version_response = await unavailable_security_client.get("/api/v1/")

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert version_response.status_code == 200
    assert version_response.json() == {"version": "v1", "status": "available"}
    assert is_async_redis_initialized() is False


@pytest.mark.parametrize(
    ("method", "path", "expected_tier"),
    [
        ("GET", "/api/v1/memes/search", SecurityRouteTier.SEARCH_FEED),
        ("GET", "/api/v1/memes/browse", SecurityRouteTier.SEARCH_FEED),
        ("GET", "/api/v1/memes/trending", SecurityRouteTier.SEARCH_FEED),
        ("GET", "/api/v1/memes/trends", SecurityRouteTier.SEARCH_FEED),
        ("GET", "/api/v1/memes/trends/tags", SecurityRouteTier.SEARCH_FEED),
        ("POST", "/api/v1/collections", SecurityRouteTier.WRITE),
        ("POST", "/api/v1/pipeline/uploads", SecurityRouteTier.UPLOAD),
        ("GET", "/api/v1/admin/session", SecurityRouteTier.ADMIN),
        ("POST", "/api/v1/auth/guest", SecurityRouteTier.AUTH_WRITE),
    ],
)
def test_classify_security_route_matches_documented_tiers(
    method: str,
    path: str,
    expected_tier: SecurityRouteTier,
) -> None:
    request = _build_request(method, path)

    assert classify_security_route(request) is expected_tier


async def test_auth_write_rate_limit_returns_retry_metadata_and_skips_safe_reads(
    security_client: AsyncClient,
) -> None:
    guest_response = await security_client.post(
        "/api/v1/auth/guest",
        json={"language": "ru", "nsfw_enabled": True},
    )
    guest_payload = guest_response.json()

    me_response = await security_client.get("/api/v1/auth/me")
    linked_providers_response = await security_client.get("/api/v1/auth/linked-providers")
    logout_all_response = await security_client.post("/api/v1/auth/logout-all")
    # Second logout-all is rate-limited at the middleware layer (IP
    # bucket), which fires before the auth dep would ever look at the
    # now-cleared cookie.
    limited_response = await security_client.post("/api/v1/auth/logout-all")
    limited_payload = limited_response.json()

    assert guest_response.status_code == 201
    assert guest_response.headers["X-RateLimit-Limit"] == "2"
    assert guest_response.headers["X-RateLimit-Remaining"] == "1"

    assert me_response.status_code == 200
    assert me_response.json()["id"] == guest_payload["user"]["id"]
    assert "X-RateLimit-Limit" not in me_response.headers

    assert linked_providers_response.status_code == 200
    assert linked_providers_response.json() == {
        "email": None,
        "email_verified_at": None,
        "has_password": False,
        "google_linked": False,
        "telegram_linked": False,
    }
    assert "X-RateLimit-Limit" not in linked_providers_response.headers

    assert logout_all_response.status_code == 204
    assert logout_all_response.headers["X-RateLimit-Limit"] == "2"
    assert logout_all_response.headers["X-RateLimit-Remaining"] == "0"
    assert logout_all_response.headers["X-RateLimit-Tier"] == "auth_write"

    assert limited_response.status_code == 429
    assert limited_payload["code"] == "rate_limit_exceeded"
    assert "retry after" in limited_payload["detail"].lower()
    assert isinstance(limited_payload["retry_after_seconds"], int)
    assert limited_payload["retry_after_seconds"] > 0
    assert limited_response.headers["Retry-After"] == str(limited_payload["retry_after_seconds"])
    assert limited_response.headers["X-RateLimit-Limit"] == "2"
    assert limited_response.headers["X-RateLimit-Remaining"] == "0"
    assert limited_response.headers["X-RateLimit-Reset"] == str(limited_payload["retry_after_seconds"])
    assert limited_response.headers["X-RateLimit-Tier"] == "auth_write"


async def test_auth_write_rate_limit_state_isolated_between_tests(
    security_client: AsyncClient,
) -> None:
    guest_response = await security_client.post("/api/v1/auth/guest")
    logout_all_response = await security_client.post("/api/v1/auth/logout-all")

    assert guest_response.status_code == 201
    assert guest_response.headers["X-RateLimit-Remaining"] == "1"
    assert logout_all_response.status_code == 204
    assert logout_all_response.headers["X-RateLimit-Remaining"] == "0"


async def test_search_feed_rate_limit_returns_retry_metadata_on_trends_subpaths(
    security_client: AsyncClient,
) -> None:
    first_response = await security_client.get("/api/v1/memes/trends/not-a-route")
    second_response = await security_client.get("/api/v1/memes/trends/not-a-route")
    limited_response = await security_client.get("/api/v1/memes/trends/not-a-route")
    limited_payload = limited_response.json()

    assert first_response.status_code == 404
    assert first_response.headers["X-RateLimit-Limit"] == "2"
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert first_response.headers["X-RateLimit-Tier"] == "search_feed"

    assert second_response.status_code == 404
    assert second_response.headers["X-RateLimit-Remaining"] == "0"
    assert second_response.headers["X-RateLimit-Tier"] == "search_feed"

    assert limited_response.status_code == 429
    assert limited_payload["code"] == "rate_limit_exceeded"
    assert limited_response.headers["Retry-After"] == str(limited_payload["retry_after_seconds"])
    assert limited_response.headers["X-RateLimit-Limit"] == "2"
    assert limited_response.headers["X-RateLimit-Remaining"] == "0"
    assert limited_response.headers["X-RateLimit-Tier"] == "search_feed"


async def test_write_rate_limit_returns_retry_metadata_for_generic_versioned_writes(
    security_client: AsyncClient,
) -> None:
    request_payload = {"title": "Writer route", "visibility": "private"}
    first_response = await security_client.post("/api/v1/collections", json=request_payload)
    second_response = await security_client.post("/api/v1/collections", json=request_payload)
    limited_response = await security_client.post("/api/v1/collections", json=request_payload)
    limited_payload = limited_response.json()

    assert first_response.status_code == 401
    assert first_response.json()["code"] == "invalid_token"
    assert first_response.headers["X-RateLimit-Limit"] == "2"
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert first_response.headers["X-RateLimit-Tier"] == "write"

    assert second_response.status_code == 401
    assert second_response.headers["X-RateLimit-Remaining"] == "0"
    assert second_response.headers["X-RateLimit-Tier"] == "write"

    assert limited_response.status_code == 429
    assert limited_payload["code"] == "rate_limit_exceeded"
    assert limited_response.headers["Retry-After"] == str(limited_payload["retry_after_seconds"])
    assert limited_response.headers["X-RateLimit-Limit"] == "2"
    assert limited_response.headers["X-RateLimit-Remaining"] == "0"
    assert limited_response.headers["X-RateLimit-Tier"] == "write"


async def test_upload_rate_limit_returns_retry_metadata_before_operator_auth(
    security_client: AsyncClient,
) -> None:
    first_response = await _post_operator_upload(security_client)
    second_response = await _post_operator_upload(security_client)
    limited_response = await _post_operator_upload(security_client)
    limited_payload = limited_response.json()

    assert first_response.status_code == 401
    assert first_response.headers["X-RateLimit-Limit"] == "2"
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert first_response.headers["X-RateLimit-Tier"] == "upload"

    assert second_response.status_code == 401
    assert second_response.headers["X-RateLimit-Remaining"] == "0"
    assert second_response.headers["X-RateLimit-Tier"] == "upload"

    assert limited_response.status_code == 429
    assert limited_payload["code"] == "rate_limit_exceeded"
    assert limited_response.headers["Retry-After"] == str(limited_payload["retry_after_seconds"])
    assert limited_response.headers["X-RateLimit-Limit"] == "2"
    assert limited_response.headers["X-RateLimit-Remaining"] == "0"
    assert limited_response.headers["X-RateLimit-Tier"] == "upload"


async def test_admin_rate_limit_applies_to_safe_reads_and_returns_retry_metadata(
    security_client: AsyncClient,
) -> None:
    first_response = await security_client.get("/api/v1/admin/session")
    second_response = await security_client.get("/api/v1/admin/session")
    limited_response = await security_client.get("/api/v1/admin/session")
    limited_payload = limited_response.json()

    assert first_response.status_code == 401
    assert first_response.json()["code"] == "invalid_token"
    assert first_response.headers["X-RateLimit-Limit"] == "2"
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert first_response.headers["X-RateLimit-Tier"] == "admin"

    assert second_response.status_code == 401
    assert second_response.headers["X-RateLimit-Remaining"] == "0"
    assert second_response.headers["X-RateLimit-Tier"] == "admin"

    assert limited_response.status_code == 429
    assert limited_payload["code"] == "rate_limit_exceeded"
    assert limited_response.headers["Retry-After"] == str(limited_payload["retry_after_seconds"])
    assert limited_response.headers["X-RateLimit-Limit"] == "2"
    assert limited_response.headers["X-RateLimit-Remaining"] == "0"
    assert limited_response.headers["X-RateLimit-Tier"] == "admin"


async def test_search_feed_rate_limit_uses_signed_user_subject_instead_of_shared_ip_bucket(
    security_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    security_settings_overrides: dict[str, str],
) -> None:
    first_cookie = await _issue_session_cookie(
        postgres_session_factory,
        security_settings_overrides,
        email="security-search-1@example.com",
    )
    second_cookie = await _issue_session_cookie(
        postgres_session_factory,
        security_settings_overrides,
        email="security-search-2@example.com",
    )
    transport = ASGITransport(app=security_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as first_client:
        first_client.cookies.set(ACCESS_COOKIE_NAME, first_cookie)
        first_response = await first_client.get("/api/v1/memes/trends/not-a-route")
        second_first_response = await first_client.get("/api/v1/memes/trends/not-a-route")

    async with AsyncClient(transport=transport, base_url="https://testserver") as second_client:
        second_client.cookies.set(ACCESS_COOKIE_NAME, second_cookie)
        second_response = await second_client.get("/api/v1/memes/trends/not-a-route")

    assert first_response.status_code == 404
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert second_response.status_code == 404
    assert second_response.headers["X-RateLimit-Remaining"] == "1"
    assert second_first_response.status_code == 404
    assert second_first_response.headers["X-RateLimit-Remaining"] == "0"


async def test_write_rate_limit_uses_signed_user_subject_instead_of_shared_ip_bucket(
    security_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    security_settings_overrides: dict[str, str],
) -> None:
    first_cookie = await _issue_session_cookie(
        postgres_session_factory,
        security_settings_overrides,
        email="security-write-1@example.com",
    )
    second_cookie = await _issue_session_cookie(
        postgres_session_factory,
        security_settings_overrides,
        email="security-write-2@example.com",
    )
    transport = ASGITransport(app=security_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as first_client:
        first_client.cookies.set(ACCESS_COOKIE_NAME, first_cookie)
        first_response = await first_client.post(
            "/api/v1/collections",
            json={"title": "Security write one", "visibility": "private"},
        )
        second_first_response = await first_client.post(
            "/api/v1/collections",
            json={"title": "Security write two", "visibility": "private"},
        )

    async with AsyncClient(transport=transport, base_url="https://testserver") as second_client:
        second_client.cookies.set(ACCESS_COOKIE_NAME, second_cookie)
        second_response = await second_client.post(
            "/api/v1/collections",
            json={"title": "Security write three", "visibility": "private"},
        )

    assert first_response.status_code == 201
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert second_response.status_code == 201
    assert second_response.headers["X-RateLimit-Remaining"] == "1"
    assert second_first_response.status_code == 201
    assert second_first_response.headers["X-RateLimit-Remaining"] == "0"


async def test_admin_rate_limit_uses_signed_user_subject_instead_of_shared_ip_bucket(
    security_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    security_settings_overrides: dict[str, str],
) -> None:
    first_cookie = await _issue_session_cookie(
        postgres_session_factory,
        security_settings_overrides,
        email="security-admin-1@example.com",
        is_admin=True,
    )
    second_cookie = await _issue_session_cookie(
        postgres_session_factory,
        security_settings_overrides,
        email="security-admin-2@example.com",
        is_admin=True,
    )
    transport = ASGITransport(app=security_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as first_client:
        first_client.cookies.set(ACCESS_COOKIE_NAME, first_cookie)
        first_response = await first_client.get("/api/v1/admin/session")
        second_first_response = await first_client.get("/api/v1/admin/session")

    async with AsyncClient(transport=transport, base_url="https://testserver") as second_client:
        second_client.cookies.set(ACCESS_COOKIE_NAME, second_cookie)
        second_response = await second_client.get("/api/v1/admin/session")

    assert first_response.status_code == 200
    assert first_response.headers["X-RateLimit-Remaining"] == "1"
    assert second_response.status_code == 200
    assert second_response.headers["X-RateLimit-Remaining"] == "1"
    assert second_first_response.status_code == 200
    assert second_first_response.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.parametrize(
    ("payload", "expected_loc"),
    [
        ({"unexpected": "boom"}, ["body", "unexpected"]),
        ({"nsfw_enabled": "true"}, ["body", "nsfw_enabled"]),
    ],
)
async def test_guest_bootstrap_validation_rejects_extra_fields_and_bool_coercion_before_side_effects(
    security_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    expected_loc: list[str],
) -> None:
    response = await security_client.post("/api/v1/auth/guest", json=payload)
    validation_errors = response.json()["detail"]

    assert response.status_code == 422
    assert expected_loc in [error["loc"] for error in validation_errors]
    assert await _count_auth_side_effects(postgres_session_factory) == (0, 0)


async def test_email_signup_validation_rejects_extra_fields_before_side_effects(
    security_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await security_client.post(
        "/api/v1/auth/email/signup",
        json={
            "email": "routeuser@example.com",
            "password": "correct-horse-battery",
            "unexpected": "boom",
        },
    )
    validation_errors = response.json()["detail"]

    assert response.status_code == 422
    assert ["body", "unexpected"] in [error["loc"] for error in validation_errors]
    assert await _count_auth_side_effects(postgres_session_factory) == (0, 0)


async def test_google_auth_validation_rejects_extra_fields_before_provider_calls(
    security_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[uuid.UUID | None, str]] = []

    async def fake_link_guest_with_google_code(
        self: AccountLinkService,
        *,
        guest_user_id: uuid.UUID | None,
        code: str,
    ) -> object:
        calls.append((guest_user_id, code))
        raise AssertionError("Google account-link service should not run when request validation fails.")

    monkeypatch.setattr(
        AccountLinkService,
        "link_guest_with_google_code",
        fake_link_guest_with_google_code,
    )

    response = await security_client.post(
        "/api/v1/auth/google",
        json={
            "code": "route-google-code",
            "unexpected": "boom",
        },
    )
    validation_errors = response.json()["detail"]

    assert response.status_code == 422
    assert ["body", "unexpected"] in [error["loc"] for error in validation_errors]
    assert calls == []
    assert await _count_auth_side_effects(postgres_session_factory) == (0, 0)


@pytest.mark.parametrize(
    "origin",
    ALLOWED_BROWSER_ORIGINS,
)
async def test_cors_preflight_allows_memexpert_local_and_telegram_browser_origins(
    browser_security_client: AsyncClient,
    origin: str,
) -> None:
    response = await browser_security_client.options(
        "/api/v1/auth/guest",
        headers=_build_cors_preflight_headers(origin),
    )

    allow_headers = response.headers["Access-Control-Allow-Headers"].lower()
    allow_methods = response.headers["Access-Control-Allow-Methods"]

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.headers["Vary"] == "Origin"
    assert "content-type" in allow_headers
    assert "x-requested-with" in allow_headers
    assert "POST" in allow_methods


async def test_cors_preflight_rejects_unknown_origin_without_wildcard_fallback(
    browser_security_client: AsyncClient,
) -> None:
    response = await browser_security_client.options(
        "/api/v1/auth/guest",
        headers=_build_cors_preflight_headers(DISALLOWED_BROWSER_ORIGIN),
    )

    assert response.status_code == 400
    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert "disallowed cors origin" in response.text.lower()


async def test_cors_preflight_rejects_unexpected_request_headers(
    browser_security_client: AsyncClient,
) -> None:
    response = await browser_security_client.options(
        "/api/v1/auth/guest",
        headers=_build_cors_preflight_headers(
            "https://app.memexpert.net",
            requested_headers="Content-Type, X-Requested-With, X-Not-Allowed",
        ),
    )

    assert response.status_code == 400
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert "disallowed cors headers" in response.text.lower()


async def test_csrf_rejects_browser_guest_bootstrap_without_required_header(
    browser_security_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={"Origin": "https://app.memexpert.net"},
        json={"language": "ru", "nsfw_enabled": True},
    )

    assert response.status_code == 403
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.json()["code"] == "csrf_failed"
    assert "x-requested-with" in response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        assert user_count_result.scalar_one() == 0


async def test_csrf_allows_browser_guest_bootstrap_with_required_header_and_sets_lax_cookie(
    browser_security_client: AsyncClient,
) -> None:
    response = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
        json={"language": "ru", "nsfw_enabled": True},
    )

    assert response.status_code == 201
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert "access_token" not in response.json()
    # Cookie-only transport: the access token lives in Set-Cookie only.
    assert "memexpert_access_token=" in response.headers["set-cookie"]


async def test_csrf_rejects_generic_browser_write_without_required_header(
    browser_security_client: AsyncClient,
) -> None:
    response = await browser_security_client.post(
        "/api/v1/collections",
        headers={"Origin": "https://app.memexpert.net"},
        json={"title": "csrf probe", "visibility": "private"},
    )

    assert response.status_code == 403
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.json()["code"] == "csrf_failed"
    assert "x-requested-with" in response.json()["detail"].lower()


async def test_csrf_generic_browser_write_proceeds_past_csrf_with_required_header(
    browser_security_client: AsyncClient,
) -> None:
    response = await browser_security_client.post(
        "/api/v1/collections",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
        json={"title": "csrf probe", "visibility": "private"},
    )

    assert response.status_code == 401
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.json()["code"] == "invalid_token"


async def test_csrf_safe_get_remains_exempt_when_origin_is_present(
    browser_security_client: AsyncClient,
) -> None:
    _ = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )

    me_response = await browser_security_client.get(
        "/api/v1/auth/me",
        headers={"Origin": "https://app.memexpert.net"},
    )

    assert me_response.status_code == 200
    assert me_response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert me_response.json()["account_type"] == "guest"


async def test_csrf_does_not_apply_to_non_browser_logout_all_post_without_origin(
    browser_security_client: AsyncClient,
) -> None:
    """Non-browser clients (no Origin header) are exempt from the CSRF guard."""

    _ = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )

    logout_all_response = await browser_security_client.post("/api/v1/auth/logout-all")

    assert logout_all_response.status_code == 204


async def test_csrf_logout_all_route_requires_header_when_browser_origin_present(
    browser_security_client: AsyncClient,
) -> None:
    """With an Origin header the CSRF guard requires X-Requested-With on mutations."""

    _ = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )

    rejected_response = await browser_security_client.post(
        "/api/v1/auth/logout-all",
        headers={"Origin": "https://app.memexpert.net"},
    )
    accepted_response = await browser_security_client.post(
        "/api/v1/auth/logout-all",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )

    assert rejected_response.status_code == 403
    assert rejected_response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert rejected_response.json()["code"] == "csrf_failed"

    assert accepted_response.status_code == 204
    assert accepted_response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert accepted_response.headers["Access-Control-Allow-Credentials"] == "true"


async def test_csrf_admin_browser_writes_require_header_before_auth_dependency(
    browser_security_client: AsyncClient,
) -> None:
    report_id = "11111111-1111-4111-8111-111111111111"
    rejected_response = await browser_security_client.post(
        f"/api/v1/admin/moderation-reports/{report_id}/resolve",
        headers={"Origin": "https://app.memexpert.net"},
        json={"action": "no_action", "reason": "other", "note": "csrf probe"},
    )
    accepted_response = await browser_security_client.post(
        f"/api/v1/admin/moderation-reports/{report_id}/resolve",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
        json={"action": "no_action", "reason": "other", "note": "csrf probe"},
    )

    assert rejected_response.status_code == 403
    assert rejected_response.json()["code"] == "csrf_failed"
    assert accepted_response.status_code == 401
    assert accepted_response.json()["code"] == "invalid_token"
