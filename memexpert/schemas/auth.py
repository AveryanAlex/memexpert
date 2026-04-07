# ruff: noqa: TC001,TC003
"""Auth request, response, and error schemas for guest and provider-auth flows."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from memexpert.models.enums import UserLanguage
from memexpert.schemas.user import UserRead

MAX_DEVICE_INFO_LENGTH = 1024
MAX_EMAIL_LENGTH = 320
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 72
MAX_TELEGRAM_START_PARAMETER_LENGTH = 64
TELEGRAM_LINK_START_PREFIX = "link_"
MAX_TELEGRAM_LINK_CODE_LENGTH = MAX_TELEGRAM_START_PARAMETER_LENGTH - len(TELEGRAM_LINK_START_PREFIX)


class AuthErrorCode(StrEnum):
    """Machine-readable auth error codes returned by the service and API."""

    AUTH_CONFIGURATION_ERROR = "auth_configuration_error"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    PROVIDER_PAYLOAD_INVALID = "provider_payload_invalid"
    PROVIDER_PAYLOAD_EXPIRED = "provider_payload_expired"
    PROVIDER_ACCESS_DENIED = "provider_access_denied"
    EXPIRED_TOKEN = "expired_token"
    INVALID_TOKEN = "invalid_token"
    INVALID_CREDENTIALS = "invalid_credentials"
    EMAIL_ALREADY_IN_USE = "email_already_in_use"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    UPGRADE_REQUIRED = "upgrade_required"
    GUEST_ACCOUNT_REQUIRED = "guest_account_required"
    ACCOUNT_LINK_INVARIANT_ERROR = "account_link_invariant_error"


class GuestBootstrapRequest(BaseModel):
    """Optional guest-account preferences accepted at session bootstrap time."""

    model_config = ConfigDict(extra="forbid")

    language: UserLanguage = UserLanguage.ANY
    nsfw_enabled: StrictBool = False
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


class LinkedProvidersRead(BaseModel):
    """Read-only provider-link state for an authenticated account."""

    email: str | None
    email_verified_at: datetime | None
    has_password: bool
    google_linked: bool
    telegram_linked: bool


class AccountLinkMergeSummaryRead(BaseModel):
    """Lightweight merge outcome details returned after a successful guest link."""

    merge_performed: bool
    merge_log_id: uuid.UUID | None
    guest_user_id: uuid.UUID
    canonical_user_id: uuid.UUID
    deleted_guest_user_id: uuid.UUID | None
    favorites_transferred: int = Field(ge=0)
    duplicate_favorites_skipped: int = Field(ge=0)
    analytics_events_transferred: int = Field(ge=0)
    inline_usage_events_transferred: int = Field(ge=0)
    views_transferred: int = Field(ge=0)


class AccountLinkResponseRead(BaseModel):
    """Canonical link response wrapping the new session plus provider-state metadata."""

    session: AuthSessionRead
    linked_providers: LinkedProvidersRead
    merge_summary: AccountLinkMergeSummaryRead


class TelegramLinkStartRead(BaseModel):
    """Guest-only Telegram deep-link handoff metadata for the bot merge flow."""

    code: str = Field(min_length=1, max_length=MAX_TELEGRAM_LINK_CODE_LENGTH)
    deep_link_url: str
    expires_at: datetime
    expires_in_seconds: int = Field(gt=0)
    return_url: str


class EmailCredentialsRequest(BaseModel):
    """Shared request body for email/password authentication entrypoints."""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_auth_email(value)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return validate_auth_password(value)


class EmailSignupRequest(EmailCredentialsRequest):
    """Email/password payload accepted by the signup endpoint."""


class EmailLoginRequest(EmailCredentialsRequest):
    """Email/password payload accepted by the login endpoint."""


class GoogleAuthRequest(BaseModel):
    """Payload forwarded from a Google OAuth frontend after authorization-code grant."""

    model_config = ConfigDict(extra="forbid")

    code: str


class TelegramWidgetAuthRequest(BaseModel):
    """Payload forwarded from the Telegram Login Widget to the backend."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, gt=0)
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int | None = None
    hash: str | None = None


class TelegramMiniAppAuthRequest(BaseModel):
    """Payload forwarded from a Telegram Mini App to the backend."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    init_data: str | None = Field(default=None, alias="initData", serialization_alias="initData")


class AuthErrorResponse(BaseModel):
    """Machine-readable auth error payload used by HTTP routes."""

    code: AuthErrorCode
    detail: str


def normalize_auth_email(email: str) -> str:
    """Normalize and validate an auth email address."""

    normalized_email = email.strip().lower()
    if not normalized_email:
        raise ValueError("Email cannot be blank.")
    if len(normalized_email) > MAX_EMAIL_LENGTH:
        raise ValueError(f"Email must be at most {MAX_EMAIL_LENGTH} characters long.")
    if " " in normalized_email:
        raise ValueError("Email cannot contain spaces.")

    local_part, separator, domain = normalized_email.partition("@")
    if separator != "@" or not local_part or not domain or "." not in domain:
        raise ValueError("Email must be a valid address.")
    if domain.startswith(".") or domain.endswith(".") or ".." in normalized_email:
        raise ValueError("Email must be a valid address.")

    return normalized_email


def validate_auth_password(password: str) -> str:
    """Validate a raw password while preserving the caller's exact bytes."""

    if not password.strip():
        raise ValueError("Password cannot be blank.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.")
    return password


__all__ = [
    "AccountLinkMergeSummaryRead",
    "AccountLinkResponseRead",
    "AuthErrorCode",
    "AuthErrorResponse",
    "AuthSessionRead",
    "EmailCredentialsRequest",
    "EmailLoginRequest",
    "EmailSignupRequest",
    "GoogleAuthRequest",
    "GuestBootstrapRequest",
    "LinkedProvidersRead",
    "RefreshCookieMetadata",
    "TelegramLinkStartRead",
    "TelegramMiniAppAuthRequest",
    "TelegramWidgetAuthRequest",
    "normalize_auth_email",
    "validate_auth_password",
]
