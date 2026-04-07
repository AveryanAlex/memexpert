"""Integration tests for shared API security rate limiting, CORS, and browser-targeted CSRF behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.core.redis import get_async_redis, is_async_redis_initialized, reset_async_redis_state
from memexpert.models.user import RefreshToken, User
from memexpert.services import ProviderAuthService

if TYPE_CHECKING:
    from httpx import AsyncClient
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
    """Tighten the dedicated security fixture to a tiny auth-write budget for rate-limit tests."""

    return {
        **auth_settings_overrides,
        "REDIS_URL": redis_container_url,
        "AUTH_REFRESH_COOKIE_SAMESITE": "lax",
        "SECURITY_RATE_LIMIT_REDIS_TIMEOUT_SECONDS": "0.1",
        "SECURITY_RATE_LIMIT_AUTH_WRITE_MAX_REQUESTS": "2",
        "SECURITY_RATE_LIMIT_AUTH_WRITE_WINDOW_SECONDS": "60",
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
        refresh_token_count_result = await session.execute(select(func.count()).select_from(RefreshToken))
        return user_count_result.scalar_one(), refresh_token_count_result.scalar_one()


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
    assert guest_response.json()["refresh_cookie"]["same_site"] == "lax"
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


async def test_auth_write_rate_limit_returns_retry_metadata_and_skips_safe_reads(
    security_client: AsyncClient,
) -> None:
    guest_response = await security_client.post(
        "/api/v1/auth/guest",
        json={"language": "ru", "nsfw_enabled": True},
    )
    guest_payload = guest_response.json()
    bearer_token = guest_payload["access_token"]
    auth_headers = {"Authorization": f"Bearer {bearer_token}"}

    me_response = await security_client.get("/api/v1/auth/me", headers=auth_headers)
    linked_providers_response = await security_client.get(
        "/api/v1/auth/linked-providers",
        headers=auth_headers,
    )
    refresh_response = await security_client.post("/api/v1/auth/refresh")
    limited_response = await security_client.post("/api/v1/auth/refresh")
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

    assert refresh_response.status_code == 200
    assert refresh_response.headers["X-RateLimit-Limit"] == "2"
    assert refresh_response.headers["X-RateLimit-Remaining"] == "0"
    assert refresh_response.headers["X-RateLimit-Tier"] == "auth_write"

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
    refresh_response = await security_client.post("/api/v1/auth/refresh")

    assert guest_response.status_code == 201
    assert guest_response.headers["X-RateLimit-Remaining"] == "1"
    assert refresh_response.status_code == 200
    assert refresh_response.headers["X-RateLimit-Remaining"] == "0"


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
    calls: list[tuple[str, str | None]] = []

    async def fake_authenticate_with_google_code(
        self: ProviderAuthService,
        *,
        code: str,
        device_info: str | None = None,
    ) -> object:
        calls.append((code, device_info))
        raise AssertionError("Google provider auth should not run when request validation fails.")

    monkeypatch.setattr(
        ProviderAuthService,
        "authenticate_with_google_code",
        fake_authenticate_with_google_code,
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

    set_cookie_header = response.headers["set-cookie"].lower()

    assert response.status_code == 201
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.json()["refresh_cookie"]["path"] == "/api/v1/auth/refresh"
    assert response.json()["refresh_cookie"]["same_site"] == "lax"
    assert "path=/api/v1/auth/refresh" in set_cookie_header
    assert "samesite=lax" in set_cookie_header


async def test_csrf_safe_get_remains_exempt_when_origin_is_present(
    browser_security_client: AsyncClient,
) -> None:
    guest_response = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )
    access_token = guest_response.json()["access_token"]

    me_response = await browser_security_client.get(
        "/api/v1/auth/me",
        headers={
            "Origin": "https://app.memexpert.net",
            "Authorization": f"Bearer {access_token}",
        },
    )

    assert me_response.status_code == 200
    assert me_response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert me_response.json()["account_type"] == "guest"


async def test_csrf_does_not_apply_to_non_browser_refresh_post_without_origin(
    browser_security_client: AsyncClient,
) -> None:
    guest_response = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )
    cookie_name = guest_response.json()["refresh_cookie"]["name"]
    original_refresh_token = browser_security_client.cookies.get(cookie_name)

    refresh_response = await browser_security_client.post("/api/v1/auth/refresh")
    rotated_refresh_token = browser_security_client.cookies.get(cookie_name)

    assert original_refresh_token is not None
    assert refresh_response.status_code == 200
    assert refresh_response.json()["refresh_cookie"]["path"] == "/api/v1/auth/refresh"
    assert refresh_response.json()["refresh_cookie"]["same_site"] == "lax"
    assert rotated_refresh_token is not None
    assert rotated_refresh_token != original_refresh_token


async def test_csrf_refresh_route_requires_header_and_rotates_cookie_when_present(
    browser_security_client: AsyncClient,
) -> None:
    guest_response = await browser_security_client.post(
        "/api/v1/auth/guest",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )
    cookie_name = guest_response.json()["refresh_cookie"]["name"]
    original_refresh_token = browser_security_client.cookies.get(cookie_name)

    rejected_response = await browser_security_client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "https://app.memexpert.net"},
    )
    refresh_token_after_rejection = browser_security_client.cookies.get(cookie_name)
    accepted_response = await browser_security_client.post(
        "/api/v1/auth/refresh",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
    )
    rotated_refresh_token = browser_security_client.cookies.get(cookie_name)
    accepted_set_cookie_header = accepted_response.headers["set-cookie"].lower()

    assert original_refresh_token is not None
    assert rejected_response.status_code == 403
    assert rejected_response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert rejected_response.json()["code"] == "csrf_failed"
    assert refresh_token_after_rejection == original_refresh_token

    assert accepted_response.status_code == 200
    assert accepted_response.headers["Access-Control-Allow-Origin"] == "https://app.memexpert.net"
    assert accepted_response.headers["Access-Control-Allow-Credentials"] == "true"
    assert accepted_response.json()["refresh_cookie"]["path"] == "/api/v1/auth/refresh"
    assert accepted_response.json()["refresh_cookie"]["same_site"] == "lax"
    assert rotated_refresh_token is not None
    assert rotated_refresh_token != original_refresh_token
    assert "path=/api/v1/auth/refresh" in accepted_set_cookie_header
    assert "samesite=lax" in accepted_set_cookie_header


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("security_rate_limit_auth_write_max_requests", 0),
        ("security_rate_limit_auth_write_window_seconds", 0),
    ],
)
def test_auth_rate_limit_settings_reject_non_positive_values(
    field_name: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        _ = Settings.model_validate({field_name: value})
