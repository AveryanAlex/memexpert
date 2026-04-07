"""Integration tests for shared API security rate limiting and degraded-mode behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.core.redis import get_async_redis, is_async_redis_initialized, reset_async_redis_state
from memexpert.models.user import User

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
