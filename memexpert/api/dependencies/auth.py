"""Reusable FastAPI auth dependencies and auth-error translation helpers."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Final, cast

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.core.database import get_db_session
from memexpert.models.enums import AccountType
from memexpert.schemas.auth import AuthErrorCode, AuthErrorResponse
from memexpert.schemas.user import UserRead
from memexpert.services import (
    AccountLinkService,
    AuthConfigurationError,
    AuthService,
    AuthServiceError,
    GuestAccountRequiredError,
    InvalidTokenError,
    MissingTokenError,
    ProviderAuthService,
    UpgradeRequiredError,
)

AUTH_ERROR_STATUS_CODES: Final[dict[AuthErrorCode, int]] = {
    AuthErrorCode.AUTH_CONFIGURATION_ERROR: int(HTTPStatus.SERVICE_UNAVAILABLE),
    AuthErrorCode.PROVIDER_NOT_CONFIGURED: int(HTTPStatus.SERVICE_UNAVAILABLE),
    AuthErrorCode.EXPIRED_TOKEN: int(HTTPStatus.UNAUTHORIZED),
    AuthErrorCode.INVALID_TOKEN: int(HTTPStatus.UNAUTHORIZED),
    AuthErrorCode.PROVIDER_PAYLOAD_INVALID: int(HTTPStatus.UNAUTHORIZED),
    AuthErrorCode.PROVIDER_PAYLOAD_EXPIRED: int(HTTPStatus.UNAUTHORIZED),
    AuthErrorCode.PROVIDER_ACCESS_DENIED: int(HTTPStatus.UNAUTHORIZED),
    AuthErrorCode.INVALID_CREDENTIALS: int(HTTPStatus.UNAUTHORIZED),
    AuthErrorCode.ACCOUNT_UNAVAILABLE: int(HTTPStatus.FORBIDDEN),
    AuthErrorCode.UPGRADE_REQUIRED: int(HTTPStatus.FORBIDDEN),
    AuthErrorCode.GUEST_ACCOUNT_REQUIRED: int(HTTPStatus.FORBIDDEN),
    AuthErrorCode.EMAIL_ALREADY_IN_USE: int(HTTPStatus.CONFLICT),
    AuthErrorCode.ACCOUNT_LINK_INVARIANT_ERROR: int(HTTPStatus.CONFLICT),
}

AUTH_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    int(HTTPStatus.UNAUTHORIZED): {
        "description": "Authentication failed or provided credentials were not accepted.",
        "model": AuthErrorResponse,
    },
    int(HTTPStatus.FORBIDDEN): {
        "description": "The identified account cannot perform this authenticated operation.",
        "model": AuthErrorResponse,
    },
    int(HTTPStatus.CONFLICT): {
        "description": "The authentication or link request conflicts with existing account state.",
        "model": AuthErrorResponse,
    },
    int(HTTPStatus.SERVICE_UNAVAILABLE): {
        "description": "Authentication is temporarily unavailable due to configuration.",
        "model": AuthErrorResponse,
    },
}


class AuthHTTPError(Exception):
    """Internal API-layer auth exception rendered as a stable JSON payload."""

    status_code: int
    payload: AuthErrorResponse

    def __init__(self, *, status_code: int, payload: AuthErrorResponse) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload.detail)


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


async def auth_http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render auth dependency failures as the documented machine-readable schema."""

    auth_error = cast("AuthHTTPError", exc)
    return JSONResponse(
        status_code=auth_error.status_code,
        content=auth_error.payload.model_dump(mode="json"),
    )


def get_auth_service(session: DbSessionDep) -> AuthService:
    """Build an auth service from the current request session and cached settings."""

    return AuthService.from_settings(session)


def get_provider_auth_service(session: DbSessionDep) -> ProviderAuthService:
    """Build a provider-auth service from the current request session and cached settings."""

    return ProviderAuthService.from_settings(session)


def get_account_link_service(
    session: DbSessionDep,
    provider_auth_service: Annotated[ProviderAuthService, Depends(get_provider_auth_service)],
) -> AccountLinkService:
    """Build the shared guest-link orchestration service for explicit link routes."""

    return AccountLinkService.from_settings(
        session,
        provider_auth_service=provider_auth_service,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
ProviderAuthServiceDep = Annotated[ProviderAuthService, Depends(get_provider_auth_service)]
AccountLinkServiceDep = Annotated[AccountLinkService, Depends(get_account_link_service)]
AuthorizationHeaderDep = Annotated[str | None, Header(alias="Authorization")]


def to_auth_http_error(error: AuthServiceError) -> AuthHTTPError:
    """Convert a service-layer auth error into an API-facing JSON error response."""

    try:
        error_code = AuthErrorCode(error.error_code)
    except ValueError:
        if isinstance(error, AuthConfigurationError):
            error_code = AuthErrorCode.AUTH_CONFIGURATION_ERROR
        else:
            error_code = AuthErrorCode.INVALID_TOKEN

    status_code = AUTH_ERROR_STATUS_CODES[error_code]

    return AuthHTTPError(
        status_code=status_code,
        payload=AuthErrorResponse(code=error_code, detail=str(error)),
    )


def get_optional_bearer_token(authorization: AuthorizationHeaderDep = None) -> str | None:
    """Parse a Bearer token from the Authorization header when present."""

    if authorization is None:
        return None

    header_parts = authorization.strip().split()
    if len(header_parts) != 2 or header_parts[0].lower() != "bearer":
        raise to_auth_http_error(
            InvalidTokenError("Authorization header must use the Bearer scheme."),
        )

    access_token = header_parts[1].strip()
    if not access_token:
        raise to_auth_http_error(MissingTokenError("Bearer token is required."))

    return access_token


OptionalBearerTokenDep = Annotated[str | None, Depends(get_optional_bearer_token)]


async def get_optional_current_user(
    auth_service: AuthServiceDep,
    access_token: OptionalBearerTokenDep,
) -> UserRead | None:
    """Return the current authenticated user when a bearer token is provided."""

    if access_token is None:
        return None

    try:
        return await auth_service.verify_access_token(access_token)
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc


OptionalCurrentUserDep = Annotated[UserRead | None, Depends(get_optional_current_user)]


async def get_current_user(current_user: OptionalCurrentUserDep) -> UserRead:
    """Require an authenticated caller and return the current DB-backed user."""

    if current_user is None:
        raise to_auth_http_error(MissingTokenError("Bearer token is required."))

    return current_user


CurrentUserDep = Annotated[UserRead, Depends(get_current_user)]


async def get_full_account_user(current_user: CurrentUserDep) -> UserRead:
    """Require a full account while keeping the guard read-only for ordinary requests."""

    if current_user.account_type is not AccountType.FULL:
        raise to_auth_http_error(
            UpgradeRequiredError("A full account is required for this operation."),
        )

    return current_user


async def get_guest_user(current_user: CurrentUserDep) -> UserRead:
    """Require a guest account for explicit link routes and reject full callers early."""

    if current_user.account_type is not AccountType.GUEST:
        raise to_auth_http_error(
            GuestAccountRequiredError("Only guest accounts can be linked."),
        )

    return current_user


FullAccountUserDep = Annotated[UserRead, Depends(get_full_account_user)]
GuestUserDep = Annotated[UserRead, Depends(get_guest_user)]


__all__ = [
    "AUTH_ERROR_RESPONSES",
    "AUTH_ERROR_STATUS_CODES",
    "AccountLinkServiceDep",
    "AuthHTTPError",
    "AuthServiceDep",
    "CurrentUserDep",
    "DbSessionDep",
    "FullAccountUserDep",
    "GuestUserDep",
    "OptionalCurrentUserDep",
    "ProviderAuthServiceDep",
    "auth_http_exception_handler",
    "get_account_link_service",
    "get_auth_service",
    "get_current_user",
    "get_full_account_user",
    "get_guest_user",
    "get_optional_current_user",
    "get_provider_auth_service",
    "to_auth_http_error",
]
