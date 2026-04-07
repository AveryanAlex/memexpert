# ruff: noqa: TC002,TC003
"""Shared request-security classification, degraded-mode guards, and API error plumbing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from http import HTTPStatus
from typing import Final, cast

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import RedisConfigurationError, RedisConnectionError, get_async_redis, verify_async_redis
from memexpert.schemas.api_errors import ApiErrorCode, ApiErrorResponse

SAFE_HTTP_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
V1_AUTH_PATH_PREFIX: Final = "/api/v1/auth"

SECURITY_ERROR_STATUS_CODES: Final[dict[ApiErrorCode, int]] = {
    ApiErrorCode.RATE_LIMITER_UNAVAILABLE: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ApiErrorCode.RATE_LIMIT_EXCEEDED: int(HTTPStatus.TOO_MANY_REQUESTS),
    ApiErrorCode.ORIGIN_NOT_ALLOWED: int(HTTPStatus.FORBIDDEN),
    ApiErrorCode.CSRF_HEADER_REQUIRED: int(HTTPStatus.FORBIDDEN),
}

SECURITY_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    int(HTTPStatus.FORBIDDEN): {
        "description": "The request was rejected by shared API security controls.",
        "model": ApiErrorResponse,
    },
    int(HTTPStatus.TOO_MANY_REQUESTS): {
        "description": "The request exceeded a configured shared API security limit.",
        "model": ApiErrorResponse,
    },
    int(HTTPStatus.SERVICE_UNAVAILABLE): {
        "description": "A required shared API security dependency is temporarily unavailable.",
        "model": ApiErrorResponse,
    },
}


class SecurityRouteTier(StrEnum):
    """High-level request classification used by shared API security middleware."""

    SAFE = "safe"
    AUTH_WRITE = "auth_write"


class SecurityHTTPError(Exception):
    """Internal API-layer security exception rendered as a stable JSON payload."""

    status_code: int
    payload: ApiErrorResponse

    def __init__(self, *, status_code: int, payload: ApiErrorResponse) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload.detail)


def classify_security_route(request: Request) -> SecurityRouteTier:
    """Classify versioned API requests into safe or protected security tiers."""

    normalized_path = request.url.path.rstrip("/") or "/"
    normalized_method = request.method.upper()

    if not normalized_path.startswith(V1_AUTH_PATH_PREFIX):
        return SecurityRouteTier.SAFE
    if normalized_method in SAFE_HTTP_METHODS:
        return SecurityRouteTier.SAFE
    return SecurityRouteTier.AUTH_WRITE


async def security_http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render shared security failures as the documented machine-readable schema."""

    security_error = cast("SecurityHTTPError", exc)
    return JSONResponse(
        status_code=security_error.status_code,
        content=security_error.payload.model_dump(mode="json"),
    )


def build_security_http_error(
    code: ApiErrorCode,
    detail: str,
    *,
    status_code: int | None = None,
) -> SecurityHTTPError:
    """Construct a typed shared-security HTTP error."""

    return SecurityHTTPError(
        status_code=status_code or SECURITY_ERROR_STATUS_CODES[code],
        payload=ApiErrorResponse(code=code, detail=detail),
    )


async def ensure_security_runtime_available(
    request: Request,
    *,
    settings: Settings | None = None,
) -> SecurityRouteTier:
    """Require the shared security runtime only for unsafe auth routes."""

    resolved_settings = settings or get_settings()
    route_tier = classify_security_route(request)
    request.state.security_route_tier = route_tier

    if route_tier is SecurityRouteTier.SAFE or not resolved_settings.security_rate_limit_enabled:
        return route_tier

    try:
        await verify_async_redis(
            get_async_redis(),
            timeout=resolved_settings.security_rate_limit_redis_timeout_seconds,
        )
    except RedisConfigurationError as exc:
        if not resolved_settings.security_rate_limit_fail_closed:
            return route_tier

        raise build_security_http_error(
            ApiErrorCode.RATE_LIMITER_UNAVAILABLE,
            "Rate limiter is unavailable because the Redis security backend is misconfigured.",
        ) from exc
    except RedisConnectionError as exc:
        if not resolved_settings.security_rate_limit_fail_closed:
            return route_tier

        raise build_security_http_error(
            ApiErrorCode.RATE_LIMITER_UNAVAILABLE,
            "Rate limiter is temporarily unavailable; retry later.",
        ) from exc

    return route_tier


async def security_http_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Guard risky auth routes with lazy shared-security runtime checks."""

    try:
        _ = await ensure_security_runtime_available(request)
    except SecurityHTTPError as exc:
        return await security_http_exception_handler(request, exc)

    return await call_next(request)


__all__ = [
    "SECURITY_ERROR_RESPONSES",
    "SECURITY_ERROR_STATUS_CODES",
    "SAFE_HTTP_METHODS",
    "SecurityHTTPError",
    "SecurityRouteTier",
    "V1_AUTH_PATH_PREFIX",
    "build_security_http_error",
    "classify_security_route",
    "ensure_security_runtime_available",
    "security_http_exception_handler",
    "security_http_middleware",
]
