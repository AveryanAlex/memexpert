"""Reusable FastAPI auth dependencies and auth-error translation helpers."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Final, cast

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.api.cookies import set_access_cookie
from memexpert.core.config import get_settings
from memexpert.core.database import get_db_session
from memexpert.models.enums import AccountType
from memexpert.schemas.auth import AuthErrorCode, AuthErrorResponse
from memexpert.schemas.user import UserRead
from memexpert.services import (
    AccountLinkAlreadyCompletedError,
    AccountLinkService,
    AuthConfigurationError,
    AuthService,
    AuthServiceError,
    GuestAccountRequiredError,
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
    AuthErrorCode.ADMIN_REQUIRED: int(HTTPStatus.FORBIDDEN),
    AuthErrorCode.UPGRADE_REQUIRED: int(HTTPStatus.FORBIDDEN),
    AuthErrorCode.GUEST_ACCOUNT_REQUIRED: int(HTTPStatus.FORBIDDEN),
    AuthErrorCode.ACCOUNT_LINK_ALREADY_COMPLETED: int(HTTPStatus.CONFLICT),
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


def to_auth_http_error(error: AuthServiceError) -> AuthHTTPError:
    """Convert a service-layer auth error into an API-facing JSON error response."""

    try:
        error_code = AuthErrorCode(error.error_code)
    except ValueError:
        if isinstance(error, AuthConfigurationError):
            error_code = AuthErrorCode.AUTH_CONFIGURATION_ERROR
        elif isinstance(error, AccountLinkAlreadyCompletedError):
            error_code = AuthErrorCode.ACCOUNT_LINK_ALREADY_COMPLETED
        else:
            error_code = AuthErrorCode.INVALID_TOKEN

    status_code = AUTH_ERROR_STATUS_CODES[error_code]

    return AuthHTTPError(
        status_code=status_code,
        payload=AuthErrorResponse(code=error_code, detail=str(error)),
    )


def get_optional_access_token(request: Request) -> str | None:
    """Read the access token exclusively from the configured access cookie.

    The cookie is the sole transport for the access token — no
    ``Authorization: Bearer`` fallback exists. Routes that want
    authenticated callers consume the token string downstream via
    :func:`get_optional_current_user`, which doesn't care how it
    arrived.
    """

    access_cookie_name = get_settings().auth_access_cookie_name
    access_cookie = request.cookies.get(access_cookie_name)

    if access_cookie is None:
        return None

    trimmed = access_cookie.strip()
    return trimmed or None


OptionalAccessTokenDep = Annotated[str | None, Depends(get_optional_access_token)]


async def get_optional_current_user(
    request: Request,
    response: Response,
    auth_service: AuthServiceDep,
    access_token: OptionalAccessTokenDep,
) -> UserRead | None:
    """Return the canonical authenticated user when a session cookie is provided.

    A valid token for a guest account that was merged into another account
    is repaired here for every auth-aware endpoint. The same response gets
    a replacement cookie for the canonical target account, so callers do
    not need a dedicated refresh route.
    """

    if access_token is None:
        return None

    client_ip = request.client.host if request.client is not None else None
    try:
        resolution = await auth_service.resolve_access_token(
            access_token,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    if resolution.replacement_session is not None:
        set_access_cookie(response, resolution.replacement_session.access_token)

    return resolution.user


OptionalCurrentUserDep = Annotated[UserRead | None, Depends(get_optional_current_user)]


async def get_current_user(current_user: OptionalCurrentUserDep) -> UserRead:
    """Require an authenticated caller and return the current DB-backed user."""

    if current_user is None:
        raise to_auth_http_error(MissingTokenError("Access session cookie is required."))

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


async def get_admin_user(current_user: CurrentUserDep) -> UserRead:
    """Require a durable, full account with the admin flag."""

    if current_user.account_type is not AccountType.FULL or not current_user.is_admin:
        raise AuthHTTPError(
            status_code=int(HTTPStatus.FORBIDDEN),
            payload=AuthErrorResponse(
                code=AuthErrorCode.ADMIN_REQUIRED,
                detail="An admin account is required for this operation.",
            ),
        )

    return current_user


async def get_or_bootstrap_guest_user(
    request: Request,
    response: Response,
    optional_current_user: OptionalCurrentUserDep,
    auth_service: AuthServiceDep,
) -> UserRead:
    """Return the current user or transparently bootstrap a guest one.

    Use this on routes where the caller MUST be attributed to a user
    (writes, personalized reads, analytics-carrying endpoints) and the
    frontend should not need to explicitly call ``/auth/guest`` first.
    On cache miss the dep creates a guest via
    :meth:`AuthService.create_guest_session`, sets the access cookie on
    the outgoing response, and returns the freshly-minted ``UserRead``.

    NOTE: this dependency writes to the database on cache miss — it is
    NOT a safe read. Prefer :class:`OptionalCurrentUserDep` for pure
    anonymous-friendly reads that don't need attribution. Callers that
    present a structurally-invalid or forged cookie hit
    :func:`get_optional_current_user` first and get the usual 401 —
    auto-bootstrap only fires for *absent* sessions, not broken ones.
    """

    if optional_current_user is not None:
        return optional_current_user

    client_ip = request.client.host if request.client is not None else None
    try:
        auth_session = await auth_service.create_guest_session(
            None,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    set_access_cookie(response, auth_session.access_token)
    return auth_session.user


async def get_optional_guest_user(
    current_user: OptionalCurrentUserDep,
) -> UserRead | None:
    """Return the caller's guest user when one is present; ``None`` otherwise.

    Used by the direct ``/auth/*`` routes under the unified writer path:
    a guest caller forwards their existing guest id into
    ``AccountLinkService.link_guest_with_*``, an anonymous caller forwards
    ``None`` so the service bootstraps a throwaway guest inside the same
    transaction. Full-account callers are filtered to ``None`` here and
    rejected by :func:`forbid_full_account_caller` before the route runs.
    """

    if current_user is None:
        return None
    if current_user.account_type is not AccountType.GUEST:
        return None
    return current_user


async def forbid_full_account_caller(
    current_user: OptionalCurrentUserDep,
) -> None:
    """Reject authenticated full-account callers on direct auth entry points.

    The unified writer path bootstraps a guest when the caller has no
    session, or reuses the caller's guest when they do. A full-account
    caller falls into neither category and would silently create a
    second unrelated account; reject with the same
    ``GUEST_ACCOUNT_REQUIRED`` / 403 mapping the departing ``/auth/link/*``
    routes used for the symmetric "full caller on guest-only endpoint"
    case.
    """

    if current_user is None:
        return
    if current_user.account_type is not AccountType.GUEST:
        raise to_auth_http_error(
            GuestAccountRequiredError(
                "This endpoint rejects authenticated full-account callers; sign out first.",
            ),
        )


FullAccountUserDep = Annotated[UserRead, Depends(get_full_account_user)]
AdminUserDep = Annotated[UserRead, Depends(get_admin_user)]
GuestUserDep = Annotated[UserRead, Depends(get_guest_user)]
OptionalGuestUserDep = Annotated["UserRead | None", Depends(get_optional_guest_user)]
ForbidFullAccountCallerDep = Annotated[None, Depends(forbid_full_account_caller)]
AutoGuestUserDep = Annotated[UserRead, Depends(get_or_bootstrap_guest_user)]


__all__ = [
    "AUTH_ERROR_RESPONSES",
    "AUTH_ERROR_STATUS_CODES",
    "AccountLinkServiceDep",
    "AdminUserDep",
    "AuthHTTPError",
    "AuthServiceDep",
    "AutoGuestUserDep",
    "CurrentUserDep",
    "DbSessionDep",
    "ForbidFullAccountCallerDep",
    "FullAccountUserDep",
    "GuestUserDep",
    "OptionalAccessTokenDep",
    "OptionalCurrentUserDep",
    "OptionalGuestUserDep",
    "ProviderAuthServiceDep",
    "auth_http_exception_handler",
    "forbid_full_account_caller",
    "get_account_link_service",
    "get_auth_service",
    "get_current_user",
    "get_full_account_user",
    "get_admin_user",
    "get_guest_user",
    "get_optional_current_user",
    "get_optional_guest_user",
    "get_or_bootstrap_guest_user",
    "get_provider_auth_service",
    "to_auth_http_error",
]
