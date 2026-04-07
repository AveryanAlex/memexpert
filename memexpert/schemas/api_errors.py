"""Shared non-auth API error schemas for security and infrastructure boundaries."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ApiErrorCode(StrEnum):
    """Machine-readable non-auth error codes surfaced by shared API guards."""

    RATE_LIMITER_UNAVAILABLE = "rate_limiter_unavailable"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    CSRF_HEADER_REQUIRED = "csrf_header_required"


class ApiErrorResponse(BaseModel):
    """Machine-readable error payload used by shared API security boundaries."""

    code: ApiErrorCode
    detail: str


__all__ = ["ApiErrorCode", "ApiErrorResponse"]
