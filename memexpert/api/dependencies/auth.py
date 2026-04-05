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
    AuthConfigurationError,
    AuthService,
    AuthServiceError,
    InvalidTokenError,
    MissingTokenError,
    UpgradeRequiredError,
)

AUTH_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    int(HTTPStatus.UNAUTHORIZED): {
        "description": "Authentication failed.",
        "model": AuthErrorResponse,
    },
    int(HTTPStatus.FORBIDDEN): {
        "description": "Authenticated user must upgrade to a full account.",
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


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
AuthorizationHeaderDep = Annotated[str | None, Header(alias="Authorization")]


def to_auth_http_error(error: AuthServiceError) -> AuthHTTPError:
    """Convert a service-layer auth error into an API-facing JSON error response."""

    if isinstance(error, UpgradeRequiredError):
        status_code = int(HTTPStatus.FORBIDDEN)
    elif isinstance(error, AuthConfigurationError):
        status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
    else:
        status_code = int(HTTPStatus.UNAUTHORIZED)

    try:
        error_code = AuthErrorCode(error.error_code)
    except ValueError:
        error_code = AuthErrorCode.INVALID_TOKEN

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


FullAccountUserDep = Annotated[UserRead, Depends(get_full_account_user)]


__all__ = [
    "AUTH_ERROR_RESPONSES",
    "AuthHTTPError",
    "AuthServiceDep",
    "CurrentUserDep",
    "DbSessionDep",
    "FullAccountUserDep",
    "OptionalCurrentUserDep",
    "auth_http_exception_handler",
    "get_auth_service",
    "get_current_user",
    "get_full_account_user",
    "get_optional_current_user",
    "to_auth_http_error",
]
