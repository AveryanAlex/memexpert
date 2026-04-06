"""Guest-session and provider-auth routes for session issuance, rotation, and self lookup."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response, status

from memexpert.api.dependencies import (
    AUTH_ERROR_RESPONSES,
    AuthServiceDep,
    CurrentUserDep,
    ProviderAuthServiceDep,
    to_auth_http_error,
)
from memexpert.core.config import get_settings
from memexpert.schemas.auth import (
    AuthSessionRead,
    EmailLoginRequest,
    EmailSignupRequest,
    GuestBootstrapRequest,
    TelegramMiniAppAuthRequest,
    TelegramWidgetAuthRequest,
)
from memexpert.schemas.user import UserRead
from memexpert.services import AuthServiceError, AuthSession, MissingTokenError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/guest",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Bootstrap a guest session",
)
async def create_guest_session(
    response: Response,
    auth_service: AuthServiceDep,
    guest_request: Annotated[GuestBootstrapRequest | None, Body()] = None,
) -> AuthSessionRead:
    """Create a guest account, issue tokens, and set the opaque refresh token as a cookie."""

    try:
        auth_session = await auth_service.create_guest_session(guest_request)
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.post(
    "/email/signup",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Create a full account with email and password",
)
async def signup_with_email(
    request: Request,
    response: Response,
    provider_auth_service: ProviderAuthServiceDep,
    credentials: Annotated[EmailSignupRequest, Body()],
) -> AuthSessionRead:
    """Create a full account, issue a session immediately, and keep the refresh token cookie-only."""

    try:
        auth_session = await provider_auth_service.signup_with_email(
            email=credentials.email,
            password=credentials.password,
            device_info=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.post(
    "/email/login",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with email and password",
)
async def login_with_email(
    request: Request,
    response: Response,
    provider_auth_service: ProviderAuthServiceDep,
    credentials: Annotated[EmailLoginRequest, Body()],
) -> AuthSessionRead:
    """Authenticate an existing email/password account and keep the refresh token cookie-only."""

    try:
        auth_session = await provider_auth_service.login_with_email(
            email=credentials.email,
            password=credentials.password,
            device_info=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.post(
    "/telegram",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Telegram Login Widget",
)
async def login_with_telegram_widget(
    request: Request,
    response: Response,
    provider_auth_service: ProviderAuthServiceDep,
    credentials: Annotated[TelegramWidgetAuthRequest, Body()],
) -> AuthSessionRead:
    """Validate a Telegram Login Widget payload and keep the refresh token cookie-only."""

    try:
        auth_session = await provider_auth_service.authenticate_with_telegram_widget(
            payload=credentials,
            device_info=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.post(
    "/telegram-miniapp",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Telegram Mini App initData",
)
async def login_with_telegram_miniapp(
    request: Request,
    response: Response,
    provider_auth_service: ProviderAuthServiceDep,
    credentials: Annotated[TelegramMiniAppAuthRequest, Body()],
) -> AuthSessionRead:
    """Validate Telegram Mini App initData and keep the refresh token cookie-only."""

    try:
        auth_session = await provider_auth_service.authenticate_with_telegram_miniapp(
            init_data=credentials.init_data,
            device_info=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.post(
    "/refresh",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Rotate the refresh token and mint a fresh access token",
)
async def refresh_session(
    request: Request,
    response: Response,
    auth_service: AuthServiceDep,
) -> AuthSessionRead:
    """Rotate the caller's refresh token using the configured cookie name and metadata."""

    refresh_token = _get_refresh_token_from_request(request)

    try:
        auth_session = await auth_service.rotate_refresh_token(
            refresh_token,
            device_info=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.get(
    "/me",
    response_model=UserRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Get the authenticated caller",
)
async def read_current_user(current_user: CurrentUserDep) -> UserRead:
    """Return the current DB-backed user for the supplied bearer token."""

    return current_user


def _get_refresh_token_from_request(request: Request) -> str:
    settings = get_settings()
    refresh_token = request.cookies.get(settings.auth_refresh_cookie_name)
    if refresh_token is None or not refresh_token.strip():
        raise to_auth_http_error(MissingTokenError("Refresh token cookie is required."))
    return refresh_token


def _set_refresh_cookie(response: Response, auth_session: AuthSession) -> None:
    refresh_cookie = auth_session.refresh_cookie
    response.set_cookie(
        key=refresh_cookie.name,
        value=auth_session.refresh_token,
        max_age=refresh_cookie.max_age,
        expires=auth_session.refresh_expires_at,
        path=refresh_cookie.path,
        domain=refresh_cookie.domain,
        secure=refresh_cookie.secure,
        httponly=refresh_cookie.http_only,
        samesite=refresh_cookie.same_site,
    )


__all__ = ["router"]
