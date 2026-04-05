# ruff: noqa: TC001
"""Auth request, response, and error schemas for guest-session flows."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from memexpert.models.enums import UserLanguage
from memexpert.schemas.user import UserRead

MAX_DEVICE_INFO_LENGTH = 1024


class AuthErrorCode(StrEnum):
    """Machine-readable auth error codes returned by the service and API."""

    AUTH_CONFIGURATION_ERROR = "auth_configuration_error"
    EXPIRED_TOKEN = "expired_token"
    INVALID_TOKEN = "invalid_token"
    UPGRADE_REQUIRED = "upgrade_required"


class GuestBootstrapRequest(BaseModel):
    """Optional guest-account preferences accepted at session bootstrap time."""

    language: UserLanguage = UserLanguage.ANY
    nsfw_enabled: bool = False
    device_info: str | None = Field(default=None, max_length=MAX_DEVICE_INFO_LENGTH)

    @field_validator("device_info")
    @classmethod
    def _normalize_device_info(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized_value = value.strip()
        return normalized_value or None


class RefreshCookieMetadata(BaseModel):
    """Public refresh-cookie metadata without exposing the raw cookie value."""

    name: str
    path: str
    max_age: int
    secure: bool
    http_only: bool
    same_site: Literal["lax", "strict", "none"]
    domain: str | None = None


class AuthSessionRead(BaseModel):
    """Public auth-session payload returned by guest bootstrap and refresh flows."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserRead
    refresh_cookie: RefreshCookieMetadata


class AuthErrorResponse(BaseModel):
    """Machine-readable auth error payload used by HTTP routes."""

    code: AuthErrorCode
    detail: str


__all__ = [
    "AuthErrorCode",
    "AuthErrorResponse",
    "AuthSessionRead",
    "GuestBootstrapRequest",
    "RefreshCookieMetadata",
]
