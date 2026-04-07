"""Integration tests for the shared API security runtime and degraded-mode behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from memexpert.core.redis import get_async_redis, is_async_redis_initialized, reset_async_redis_state
from memexpert.models.user import User

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
