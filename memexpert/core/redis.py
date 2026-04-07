# ruff: noqa: TC003
"""Shared async Redis runtime helpers for lazy security-backed features."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any, Final, cast
from urllib.parse import urlparse

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisBackendConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisBackendTimeoutError

from memexpert.core.config import Settings, get_settings

DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS: Final = 0.5
DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS: Final = 0.5
SUPPORTED_REDIS_SCHEMES: Final[frozenset[str]] = frozenset({"redis", "rediss", "unix"})


class RedisConfigurationError(ValueError):
    """Raised when the configured Redis URL cannot produce an async client."""


class RedisConnectionError(RuntimeError):
    """Raised when the async Redis client cannot establish a real connection."""


_redis_client: Redis | None = None


async def _await_redis_result[T](result: T | Awaitable[T]) -> T:
    if inspect.isawaitable(result):
        return await cast("Awaitable[T]", result)
    return result


def normalize_redis_url(redis_url: str) -> str:
    """Normalize and validate Redis URLs before constructing the async client."""

    normalized_input = redis_url.strip()
    if not normalized_input:
        raise RedisConfigurationError(
            "Redis URL is required before constructing the async Redis client.",
        )

    parsed_url = urlparse(normalized_input)
    if parsed_url.scheme not in SUPPORTED_REDIS_SCHEMES:
        raise RedisConfigurationError(
            "Redis URL must use redis://, rediss://, or unix://.",
        )

    if parsed_url.scheme == "unix":
        if not parsed_url.path:
            raise RedisConfigurationError(
                "Unix Redis URLs must include a socket path.",
            )
        return normalized_input

    try:
        port = parsed_url.port
    except ValueError as exc:
        raise RedisConfigurationError(
            "Redis URL contains an invalid port.",
        ) from exc

    if parsed_url.hostname is None:
        raise RedisConfigurationError(
            "Redis URL must include a hostname.",
        )
    if port is not None and port <= 0:
        raise RedisConfigurationError(
            "Redis URL port must be greater than zero.",
        )

    return normalized_input


def get_redis_url(settings: Settings | None = None) -> str:
    """Return the configured Redis URL after normalization."""

    resolved_settings = settings or get_settings()
    return normalize_redis_url(resolved_settings.redis_url)


def build_async_redis_client(
    redis_url: str | None = None,
    *,
    socket_connect_timeout: float = DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS,
    socket_timeout: float = DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS,
    decode_responses: bool = True,
    **client_options: Any,
) -> Redis:
    """Build a lazy async Redis client from the configured URL without connecting yet."""

    resolved_redis_url = normalize_redis_url(redis_url) if redis_url is not None else get_redis_url()

    try:
        return Redis.from_url(
            resolved_redis_url,
            decode_responses=decode_responses,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
            **client_options,
        )
    except (TypeError, ValueError) as exc:
        raise RedisConfigurationError(
            "Unable to construct the async Redis client from the configured Redis URL.",
        ) from exc


def get_async_redis() -> Redis:
    """Return the process-wide async Redis client, creating it lazily."""

    global _redis_client
    if _redis_client is None:
        _redis_client = build_async_redis_client()
    return _redis_client


def is_async_redis_initialized() -> bool:
    """Expose whether the process-wide async Redis client has been created yet."""

    return _redis_client is not None


async def verify_async_redis(
    client: Redis,
    *,
    timeout: float = DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS,
) -> None:
    """Open and validate a real Redis connection with a bounded timeout."""

    try:
        async with asyncio.timeout(timeout):
            is_available = await _await_redis_result(client.ping())
    except TimeoutError as exc:
        raise RedisConnectionError(
            f"Timed out after {timeout:.2f}s while connecting to Redis.",
        ) from exc
    except (RedisBackendConnectionError, RedisBackendTimeoutError, RedisError) as exc:
        raise RedisConnectionError(f"Unable to connect to Redis: {exc}") from exc

    if is_available is not True:
        raise RedisConnectionError("Redis availability probe returned an unexpected response.")


async def reset_async_redis_state(*, flushdb: bool = False) -> None:
    """Flush and close the cached Redis client so test state cannot leak across runs."""

    global _redis_client

    cached_client = _redis_client
    created_for_flush = False

    if flushdb and cached_client is None:
        cached_client = build_async_redis_client()
        created_for_flush = True

    try:
        if cached_client is not None and flushdb:
            _ = await _await_redis_result(cached_client.flushdb())
    finally:
        if cached_client is not None:
            _ = await _await_redis_result(cached_client.aclose())

        if created_for_flush or _redis_client is not None:
            _redis_client = None


__all__ = [
    "DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS",
    "RedisConfigurationError",
    "RedisConnectionError",
    "build_async_redis_client",
    "get_async_redis",
    "get_redis_url",
    "is_async_redis_initialized",
    "normalize_redis_url",
    "reset_async_redis_state",
    "verify_async_redis",
]
