# ruff: noqa: TC002,TC003
"""Shared request-security classification, CORS config, CSRF enforcement, rate limiting, and API error plumbing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Final, TypedDict, cast

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from jwt import ExpiredSignatureError
from jwt.exceptions import InvalidTokenError as PyJWTInvalidTokenError
from redis.exceptions import ConnectionError as RedisBackendConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisBackendTimeoutError

from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import RedisConfigurationError, RedisConnectionError, get_async_redis
from memexpert.schemas.api_errors import ApiErrorCode, ApiErrorResponse

SAFE_HTTP_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
READ_HTTP_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})
API_PATH_SEGMENT: Final = "api"
AUTH_PATH_SEGMENT: Final = "auth"
ADMIN_PATH_SEGMENT: Final = "admin"
ANALYTICS_PATH_SEGMENT: Final = "analytics"
MEMES_PATH_SEGMENT: Final = "memes"
PIPELINE_PATH_SEGMENT: Final = "pipeline"
UPLOADS_PATH_SEGMENT: Final = "uploads"
PAGE_VIEWS_PATH_SEGMENT: Final = "page-views"
REPORT_PATH_SEGMENT: Final = "report"
VERSION_PATH_PREFIX: Final = "v"
V1_AUTH_PATH_PREFIX: Final = "/api/v1/auth"
RATE_LIMIT_KEY_PREFIX: Final = "security:rate_limit"
RETRY_AFTER_HEADER: Final = "Retry-After"
RATE_LIMIT_LIMIT_HEADER: Final = "X-RateLimit-Limit"
RATE_LIMIT_REMAINING_HEADER: Final = "X-RateLimit-Remaining"
RATE_LIMIT_RESET_HEADER: Final = "X-RateLimit-Reset"
RATE_LIMIT_TIER_HEADER: Final = "X-RateLimit-Tier"

logger = logging.getLogger(__name__)

SECURITY_ERROR_STATUS_CODES: Final[dict[ApiErrorCode, int]] = {
    ApiErrorCode.RATE_LIMITER_UNAVAILABLE: int(HTTPStatus.SERVICE_UNAVAILABLE),
    ApiErrorCode.RATE_LIMIT_EXCEEDED: int(HTTPStatus.TOO_MANY_REQUESTS),
    ApiErrorCode.ORIGIN_NOT_ALLOWED: int(HTTPStatus.FORBIDDEN),
    ApiErrorCode.CSRF_FAILED: int(HTTPStatus.FORBIDDEN),
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
    SEARCH_FEED = "search_feed"
    ANALYTICS_WRITE = "analytics_write"
    WRITE = "write"
    UPLOAD = "upload"
    ADMIN = "admin"


class SecurityRateLimitTier(StrEnum):
    """Rate-limit buckets enforced by the shared API security middleware."""

    AUTH_WRITE = "auth_write"
    SEARCH_FEED = "search_feed"
    ANALYTICS_WRITE = "analytics_write"
    WRITE = "write"
    UPLOAD = "upload"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class SecurityRateLimitPolicy:
    """Resolved runtime config for one active rate-limit bucket."""

    tier: SecurityRateLimitTier
    max_requests: int
    window_seconds: int


@dataclass(frozen=True, slots=True)
class SecurityRateLimitResult:
    """Outcome of consuming one request from a Redis-backed security tier."""

    policy: SecurityRateLimitPolicy
    subject: str
    current_count: int
    retry_after_seconds: int

    @property
    def remaining_requests(self) -> int:
        return max(self.policy.max_requests - self.current_count, 0)

    def build_headers(self, *, include_retry_after: bool = False) -> dict[str, str]:
        headers = {
            RATE_LIMIT_TIER_HEADER: self.policy.tier.value,
            RATE_LIMIT_LIMIT_HEADER: str(self.policy.max_requests),
            RATE_LIMIT_REMAINING_HEADER: str(self.remaining_requests),
            RATE_LIMIT_RESET_HEADER: str(self.retry_after_seconds),
        }
        if include_retry_after:
            headers[RETRY_AFTER_HEADER] = str(self.retry_after_seconds)
        return headers


class SecurityHTTPError(Exception):
    """Internal API-layer security exception rendered as a stable JSON payload."""

    status_code: int
    payload: ApiErrorResponse
    headers: dict[str, str]

    def __init__(
        self,
        *,
        status_code: int,
        payload: ApiErrorResponse,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers = dict(headers or {})
        super().__init__(payload.detail)


class SecuritySubjectResolutionError(RuntimeError):
    """Raised when a protected request cannot be mapped to a limiter subject."""


class SecurityCORSMiddlewareKwargs(TypedDict):
    """Typed subset of CORSMiddleware kwargs used by the shared API app."""

    allow_origins: list[str]
    allow_origin_regex: str | None
    allow_credentials: bool
    allow_methods: list[str]
    allow_headers: list[str]


def build_security_cors_middleware_kwargs(settings: Settings | None = None) -> SecurityCORSMiddlewareKwargs:
    """Build the explicit credentialed CORS configuration for the shared API app."""

    resolved_settings = settings or get_settings()
    return {
        "allow_origins": list(resolved_settings.security_cors_allowed_origins),
        "allow_origin_regex": resolved_settings.security_cors_allowed_origin_regex,
        "allow_credentials": True,
        "allow_methods": list(resolved_settings.security_cors_allowed_methods),
        "allow_headers": list(resolved_settings.security_cors_allowed_headers),
    }


def _normalize_path_segments(path: str) -> tuple[str, ...]:
    normalized_path = path.rstrip("/") or "/"
    return tuple(segment for segment in normalized_path.split("/") if segment)


def _is_version_segment(segment: str) -> bool:
    return len(segment) > 1 and segment.startswith(VERSION_PATH_PREFIX) and segment[1:].isdigit()


def _is_versioned_api_path_segments(segments: tuple[str, ...]) -> bool:
    return len(segments) >= 3 and segments[0] == API_PATH_SEGMENT and _is_version_segment(segments[1])


def _is_search_feed_path(segments: tuple[str, ...]) -> bool:
    if len(segments) < 4 or segments[2] != MEMES_PATH_SEGMENT:
        return False

    memes_path = segments[3:]
    if not memes_path:
        return False

    first_segment = memes_path[0]
    if first_segment in {"search", "browse", "home-feed", "trending"} and len(memes_path) == 1:
        return True
    return first_segment == "trends"


def _is_analytics_write_path(segments: tuple[str, ...]) -> bool:
    """Return whether this is the privacy-bounded page-view ingestion endpoint."""

    return (
        len(segments) == 4
        and segments[2] == ANALYTICS_PATH_SEGMENT
        and segments[3] == PAGE_VIEWS_PATH_SEGMENT
    )


def classify_security_route(request: Request) -> SecurityRouteTier:
    """Classify versioned API requests into the shared CSRF and rate-limit tiers."""

    normalized_method = request.method.upper()
    if normalized_method == "OPTIONS":
        return SecurityRouteTier.SAFE

    segments = _normalize_path_segments(request.url.path)
    if not _is_versioned_api_path_segments(segments):
        return SecurityRouteTier.SAFE

    if segments[2] == AUTH_PATH_SEGMENT:
        return SecurityRouteTier.AUTH_WRITE if normalized_method not in SAFE_HTTP_METHODS else SecurityRouteTier.SAFE
    if segments[2] == ADMIN_PATH_SEGMENT:
        return SecurityRouteTier.ADMIN

    if normalized_method not in SAFE_HTTP_METHODS and _is_analytics_write_path(segments):
        return SecurityRouteTier.ANALYTICS_WRITE

    if normalized_method in READ_HTTP_METHODS:
        return SecurityRouteTier.SEARCH_FEED if _is_search_feed_path(segments) else SecurityRouteTier.SAFE
    if normalized_method in SAFE_HTTP_METHODS:
        return SecurityRouteTier.SAFE

    if segments[2] == PIPELINE_PATH_SEGMENT and len(segments) == 4 and segments[3] == UPLOADS_PATH_SEGMENT:
        return SecurityRouteTier.UPLOAD

    if segments[2] == MEMES_PATH_SEGMENT and len(segments) == 5 and segments[4] == REPORT_PATH_SEGMENT:
        return SecurityRouteTier.WRITE

    if _is_versioned_api_path_segments(segments):
        return SecurityRouteTier.WRITE

    return SecurityRouteTier.SAFE


async def security_http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render shared security failures as the documented machine-readable schema."""

    security_error = cast("SecurityHTTPError", exc)
    return JSONResponse(
        status_code=security_error.status_code,
        content=security_error.payload.model_dump(mode="json"),
        headers=security_error.headers,
    )


def build_security_http_error(
    code: ApiErrorCode,
    detail: str,
    *,
    status_code: int | None = None,
    retry_after_seconds: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> SecurityHTTPError:
    """Construct a typed shared-security HTTP error."""

    return SecurityHTTPError(
        status_code=status_code or SECURITY_ERROR_STATUS_CODES[code],
        payload=ApiErrorResponse(
            code=code,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
        ),
        headers=headers,
    )


def _get_request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is None:
        return None

    normalized_origin = origin.strip()
    return normalized_origin or None


def require_browser_csrf_header(
    request: Request,
    route_tier: SecurityRouteTier,
    *,
    settings: Settings,
) -> None:
    """Reject unsafe versioned API browser writes that omit the configured CSRF request header."""

    if request.method.upper() in SAFE_HTTP_METHODS:
        return

    if route_tier is SecurityRouteTier.SAFE:
        segments = _normalize_path_segments(request.url.path)
        if not _is_versioned_api_path_segments(segments):
            return

    request_origin = _get_request_origin(request)
    if request_origin is None:
        return

    csrf_header_name = settings.security_csrf_header_name
    csrf_header_value = request.headers.get(csrf_header_name)
    if csrf_header_value is not None and csrf_header_value.strip():
        request.state.security_request_origin = request_origin
        request.state.security_csrf_header_name = csrf_header_name
        return

    raise build_security_http_error(
        ApiErrorCode.CSRF_FAILED,
        f"Browser-style versioned API writes from {request_origin} must include the {csrf_header_name} header.",
    )


def get_security_rate_limit_policy(
    route_tier: SecurityRouteTier,
    *,
    settings: Settings,
) -> SecurityRateLimitPolicy | None:
    """Resolve the active Redis-backed rate-limit policy for the classified route."""

    if not settings.security_rate_limit_enabled:
        return None

    if route_tier is SecurityRouteTier.AUTH_WRITE:
        return SecurityRateLimitPolicy(
            tier=SecurityRateLimitTier.AUTH_WRITE,
            max_requests=settings.security_rate_limit_auth_write_max_requests,
            window_seconds=settings.security_rate_limit_auth_write_window_seconds,
        )
    if route_tier is SecurityRouteTier.SEARCH_FEED:
        return SecurityRateLimitPolicy(
            tier=SecurityRateLimitTier.SEARCH_FEED,
            max_requests=settings.security_rate_limit_search_feed_max_requests,
            window_seconds=settings.security_rate_limit_search_feed_window_seconds,
        )
    if route_tier is SecurityRouteTier.ANALYTICS_WRITE:
        return SecurityRateLimitPolicy(
            tier=SecurityRateLimitTier.ANALYTICS_WRITE,
            max_requests=settings.security_rate_limit_analytics_write_max_requests,
            window_seconds=settings.security_rate_limit_analytics_write_window_seconds,
        )
    if route_tier is SecurityRouteTier.WRITE:
        return SecurityRateLimitPolicy(
            tier=SecurityRateLimitTier.WRITE,
            max_requests=settings.security_rate_limit_write_max_requests,
            window_seconds=settings.security_rate_limit_write_window_seconds,
        )
    if route_tier is SecurityRouteTier.UPLOAD:
        return SecurityRateLimitPolicy(
            tier=SecurityRateLimitTier.UPLOAD,
            max_requests=settings.security_rate_limit_upload_max_requests,
            window_seconds=settings.security_rate_limit_upload_window_seconds,
        )
    if route_tier is SecurityRouteTier.ADMIN:
        return SecurityRateLimitPolicy(
            tier=SecurityRateLimitTier.ADMIN,
            max_requests=settings.security_rate_limit_admin_max_requests,
            window_seconds=settings.security_rate_limit_admin_window_seconds,
        )
    return None


def _resolve_security_ip_subject(request: Request) -> str:
    client = request.client
    if client is None or not client.host.strip():
        raise SecuritySubjectResolutionError("Protected security rate limiting requires request.client.host.")
    return f"ip:{client.host.strip()}"


def _resolve_signed_access_cookie_subject(request: Request, *, settings: Settings) -> str | None:
    access_cookie = request.cookies.get(settings.auth_access_cookie_name)
    if access_cookie is None:
        return None

    normalized_access_cookie = access_cookie.strip()
    if not normalized_access_cookie:
        return None

    try:
        payload = cast(
            "dict[str, object]",
            jwt.decode(
                normalized_access_cookie,
                settings.auth_jwt_secret.get_secret_value(),
                algorithms=[settings.auth_access_token_algorithm],
                options={"require": ["sub", "exp", "iat"]},
            ),
        )
    except (ExpiredSignatureError, PyJWTInvalidTokenError):
        return None

    raw_subject = payload.get("sub")
    if not isinstance(raw_subject, str):
        return None

    normalized_subject = raw_subject.strip()
    if not normalized_subject:
        return None

    try:
        subject = uuid.UUID(normalized_subject)
    except ValueError:
        return None

    return f"user:{subject}"


def _resolve_signed_user_or_ip_subject(request: Request, *, settings: Settings) -> str:
    return _resolve_signed_access_cookie_subject(request, settings=settings) or _resolve_security_ip_subject(request)


def resolve_security_rate_limit_subject(
    request: Request,
    policy: SecurityRateLimitPolicy,
    *,
    settings: Settings,
) -> str:
    """Resolve the subject key used for the current request's active limit tier."""

    if policy.tier is SecurityRateLimitTier.AUTH_WRITE:
        return _resolve_security_ip_subject(request)
    if policy.tier in {
        SecurityRateLimitTier.SEARCH_FEED,
        SecurityRateLimitTier.ANALYTICS_WRITE,
        SecurityRateLimitTier.WRITE,
        SecurityRateLimitTier.ADMIN,
    }:
        return _resolve_signed_user_or_ip_subject(request, settings=settings)
    if policy.tier is SecurityRateLimitTier.UPLOAD:
        return _resolve_security_ip_subject(request)

    raise SecuritySubjectResolutionError(
        f"Unsupported security rate-limit tier: {policy.tier.value}",
    )


def _build_rate_limit_exceeded_detail(policy: SecurityRateLimitPolicy, *, retry_after_seconds: int) -> str:
    if policy.tier is SecurityRateLimitTier.AUTH_WRITE:
        return f"Too many auth requests from this client IP; retry after {retry_after_seconds} seconds."
    if policy.tier is SecurityRateLimitTier.SEARCH_FEED:
        return f"Too many search feed requests; retry after {retry_after_seconds} seconds."
    if policy.tier is SecurityRateLimitTier.ANALYTICS_WRITE:
        return f"Too many analytics requests; retry after {retry_after_seconds} seconds."
    if policy.tier is SecurityRateLimitTier.WRITE:
        return f"Too many write requests; retry after {retry_after_seconds} seconds."
    if policy.tier is SecurityRateLimitTier.UPLOAD:
        return f"Too many upload requests; retry after {retry_after_seconds} seconds."
    if policy.tier is SecurityRateLimitTier.ADMIN:
        return f"Too many admin requests; retry after {retry_after_seconds} seconds."
    return f"Too many requests; retry after {retry_after_seconds} seconds."


def build_security_rate_limit_key(policy: SecurityRateLimitPolicy, *, subject: str) -> str:
    """Build the Redis key used for the active security limiter bucket."""

    return f"{RATE_LIMIT_KEY_PREFIX}:{policy.tier.value}:{subject}"


async def _consume_security_rate_limit(
    *,
    settings: Settings,
    policy: SecurityRateLimitPolicy,
    subject: str,
) -> SecurityRateLimitResult:
    """Consume one Redis-backed token from the active security rate-limit bucket."""

    rate_limit_key = build_security_rate_limit_key(policy, subject=subject)
    redis_client = get_async_redis()

    try:
        async with asyncio.timeout(settings.security_rate_limit_redis_timeout_seconds):
            raw_current_count = cast("object", await redis_client.incr(rate_limit_key))
            raw_ttl_seconds = cast("object", await redis_client.ttl(rate_limit_key))

            if not isinstance(raw_current_count, int) or raw_current_count <= 0:
                raise RedisConnectionError("Redis rate limiter counter returned an invalid value.")
            if not isinstance(raw_ttl_seconds, int):
                raise RedisConnectionError("Redis rate limiter TTL returned an invalid value.")

            current_count = raw_current_count
            ttl_seconds = raw_ttl_seconds

            if current_count == 1 or ttl_seconds < 0:
                raw_expiry_set = cast("object", await redis_client.expire(rate_limit_key, policy.window_seconds))
                if raw_expiry_set is not True:
                    raise RedisConnectionError(
                        "Redis rate limiter failed to apply the request window expiry.",
                    )
                ttl_seconds = policy.window_seconds
    except TimeoutError as exc:
        timeout_seconds = settings.security_rate_limit_redis_timeout_seconds
        raise RedisConnectionError(
            f"Timed out after {timeout_seconds:.2f}s while updating the Redis rate limiter.",
        ) from exc
    except (RedisBackendConnectionError, RedisBackendTimeoutError, RedisError) as exc:
        raise RedisConnectionError(f"Unable to update the Redis rate limiter: {exc}") from exc

    return SecurityRateLimitResult(
        policy=policy,
        subject=subject,
        current_count=current_count,
        retry_after_seconds=max(ttl_seconds, 1),
    )


async def ensure_security_runtime_available(
    request: Request,
    *,
    settings: Settings | None = None,
) -> SecurityRouteTier:
    """Apply shared CSRF and rate-limit guards for classified versioned API requests."""

    resolved_settings = settings or get_settings()
    route_tier = classify_security_route(request)
    request.state.security_route_tier = route_tier
    request.state.security_rate_limit_headers = {}

    require_browser_csrf_header(request, route_tier, settings=resolved_settings)

    policy = get_security_rate_limit_policy(route_tier, settings=resolved_settings)
    if policy is None:
        return route_tier

    try:
        subject = resolve_security_rate_limit_subject(request, policy, settings=resolved_settings)
        rate_limit_result = await _consume_security_rate_limit(
            settings=resolved_settings,
            policy=policy,
            subject=subject,
        )
    except (RedisConfigurationError, RedisConnectionError, SecuritySubjectResolutionError) as exc:
        if not resolved_settings.security_rate_limit_fail_closed:
            logger.warning(
                "security_rate_limit_degraded",
                extra={
                    "event": "security_rate_limit_degraded",
                    "route_tier": route_tier.value,
                    "rate_limit_tier": policy.tier.value,
                    "degraded_mode": True,
                    "reason": type(exc).__name__,
                },
            )
            return route_tier

        raise build_security_http_error(
            ApiErrorCode.RATE_LIMITER_UNAVAILABLE,
            "Rate limiter is temporarily unavailable; retry later.",
        ) from exc

    request.state.security_rate_limit_tier = policy.tier
    request.state.security_rate_limit_subject = subject
    request.state.security_rate_limit_key = build_security_rate_limit_key(policy, subject=subject)
    request.state.security_rate_limit_headers = rate_limit_result.build_headers()

    if rate_limit_result.current_count > policy.max_requests:
        retry_after_seconds = rate_limit_result.retry_after_seconds
        raise build_security_http_error(
            ApiErrorCode.RATE_LIMIT_EXCEEDED,
            _build_rate_limit_exceeded_detail(policy, retry_after_seconds=retry_after_seconds),
            retry_after_seconds=retry_after_seconds,
            headers=rate_limit_result.build_headers(include_retry_after=True),
        )

    return route_tier


async def security_http_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Guard risky auth routes with CSRF checks, lazy shared-security runtime checks, and rate limits."""

    try:
        _ = await ensure_security_runtime_available(request)
    except SecurityHTTPError as exc:
        return await security_http_exception_handler(request, exc)

    response = await call_next(request)

    rate_limit_headers = getattr(request.state, "security_rate_limit_headers", None)
    if isinstance(rate_limit_headers, dict):
        response.headers.update(rate_limit_headers)

    return response


__all__ = [
    "RATE_LIMIT_KEY_PREFIX",
    "RETRY_AFTER_HEADER",
    "RATE_LIMIT_LIMIT_HEADER",
    "RATE_LIMIT_REMAINING_HEADER",
    "RATE_LIMIT_RESET_HEADER",
    "RATE_LIMIT_TIER_HEADER",
    "SECURITY_ERROR_RESPONSES",
    "SECURITY_ERROR_STATUS_CODES",
    "SAFE_HTTP_METHODS",
    "SecurityHTTPError",
    "SecurityRateLimitPolicy",
    "SecurityRateLimitResult",
    "SecurityRateLimitTier",
    "SecurityRouteTier",
    "V1_AUTH_PATH_PREFIX",
    "build_security_cors_middleware_kwargs",
    "build_security_http_error",
    "build_security_rate_limit_key",
    "classify_security_route",
    "ensure_security_runtime_available",
    "get_security_rate_limit_policy",
    "require_browser_csrf_header",
    "resolve_security_rate_limit_subject",
    "security_http_exception_handler",
    "security_http_middleware",
]
