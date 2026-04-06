"""Guest-session and provider-auth routes for session issuance, rotation, self lookup, and linking."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response, status

from memexpert.api.dependencies import (
    AUTH_ERROR_RESPONSES,
    AccountLinkServiceDep,
    AuthServiceDep,
    CurrentUserDep,
    GuestUserDep,
    ProviderAuthServiceDep,
    to_auth_http_error,
)
from memexpert.core.config import get_settings
from memexpert.schemas.auth import (
    AccountLinkMergeSummaryRead,
    AccountLinkResponseRead,
    AuthSessionRead,
    EmailLoginRequest,
    EmailSignupRequest,
    GoogleAuthRequest,
    GuestBootstrapRequest,
    LinkedProvidersRead,
    TelegramLinkStartRead,
    TelegramMiniAppAuthRequest,
    TelegramWidgetAuthRequest,
)
from memexpert.schemas.user import UserRead
from memexpert.services import (
    AccountLinkResult,
    AuthenticatedUserNotFoundError,
    AuthServiceError,
    AuthSession,
    LinkedProvidersProjection,
    MissingTokenError,
    UserNotFoundError,
)

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
    "/google",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Google OAuth",
)
async def login_with_google(
    request: Request,
    response: Response,
    provider_auth_service: ProviderAuthServiceDep,
    credentials: Annotated[GoogleAuthRequest, Body()],
) -> AuthSessionRead:
    """Exchange a Google auth code, issue a session, and keep the refresh token cookie-only."""

    try:
        auth_session = await provider_auth_service.authenticate_with_google_code(
            code=credentials.code,
            device_info=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return auth_session.to_read()


@router.post(
    "/link/email/signup",
    response_model=AccountLinkResponseRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Link a guest account by creating email/password credentials",
)
async def link_guest_with_email_signup(
    request: Request,
    response: Response,
    guest_user: GuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[EmailSignupRequest, Body()],
) -> AccountLinkResponseRead:
    """Upgrade the current guest in place with email/password credentials and issue a canonical session."""

    try:
        link_result = await account_link_service.link_guest_with_email_signup(
            guest_user_id=guest_user.id,
            email=credentials.email,
            password=credentials.password,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            device_info=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return _build_account_link_response(auth_session=auth_session, link_result=link_result)


@router.post(
    "/link/email/login",
    response_model=AccountLinkResponseRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Link a guest account to an existing email/password account",
)
async def link_guest_with_email_login(
    request: Request,
    response: Response,
    guest_user: GuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[EmailLoginRequest, Body()],
) -> AccountLinkResponseRead:
    """Merge the current guest into the canonical email/password account and issue a fresh session."""

    try:
        link_result = await account_link_service.link_guest_with_email_login(
            guest_user_id=guest_user.id,
            email=credentials.email,
            password=credentials.password,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            device_info=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return _build_account_link_response(auth_session=auth_session, link_result=link_result)


@router.post(
    "/link/google",
    response_model=AccountLinkResponseRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Link a guest account with Google OAuth",
)
async def link_guest_with_google(
    request: Request,
    response: Response,
    guest_user: GuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[GoogleAuthRequest, Body()],
) -> AccountLinkResponseRead:
    """Link the current guest with Google and issue a fresh canonical session on success."""

    try:
        link_result = await account_link_service.link_guest_with_google_code(
            guest_user_id=guest_user.id,
            code=credentials.code,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            device_info=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    _set_refresh_cookie(response, auth_session)
    return _build_account_link_response(auth_session=auth_session, link_result=link_result)


@router.post(
    "/link/telegram",
    response_model=TelegramLinkStartRead,
    responses=AUTH_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Start the Telegram guest-link flow with a deep link",
)
async def start_telegram_link(
    guest_user: GuestUserDep,
    account_link_service: AccountLinkServiceDep,
) -> TelegramLinkStartRead:
    """Issue a short-lived Telegram deep link for the current guest without exposing internal identifiers."""

    try:
        link_result = await account_link_service.issue_telegram_link_code(guest_user_id=guest_user.id)
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    return TelegramLinkStartRead(
        code=link_result.code,
        deep_link_url=link_result.deep_link_url,
        expires_at=link_result.expires_at,
        expires_in_seconds=link_result.expires_in_seconds,
        return_url=link_result.return_url,
    )


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


@router.get(
    "/linked-providers",
    response_model=LinkedProvidersRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Read the authenticated caller's linked provider state",
)
async def read_linked_providers(
    current_user: CurrentUserDep,
    account_link_service: AccountLinkServiceDep,
) -> LinkedProvidersRead:
    """Return a read-only linked-provider projection without exposing internal password state."""

    try:
        linked_providers = await account_link_service.get_linked_providers(user_id=current_user.id)
    except UserNotFoundError as exc:
        raise to_auth_http_error(
            AuthenticatedUserNotFoundError(
                f"Authenticated user {current_user.id} no longer exists.",
            )
        ) from exc

    return _build_linked_providers_read(linked_providers)


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


def _build_linked_providers_read(linked_providers: LinkedProvidersProjection) -> LinkedProvidersRead:
    return LinkedProvidersRead(
        email=linked_providers.email,
        email_verified_at=linked_providers.email_verified_at,
        has_password=linked_providers.has_password,
        google_linked=linked_providers.google_linked,
        telegram_linked=linked_providers.telegram_linked,
    )


def _build_account_link_response(
    *,
    auth_session: AuthSession,
    link_result: AccountLinkResult,
) -> AccountLinkResponseRead:
    return AccountLinkResponseRead(
        session=auth_session.to_read(),
        linked_providers=_build_linked_providers_read(link_result.linked_providers),
        merge_summary=AccountLinkMergeSummaryRead(
            merge_performed=link_result.merge_performed,
            merge_log_id=link_result.merge_log_id,
            guest_user_id=link_result.guest_user_id,
            canonical_user_id=link_result.canonical_user_id,
            deleted_guest_user_id=link_result.deleted_guest_user_id,
            favorites_transferred=link_result.favorites_transferred,
            duplicate_favorites_skipped=link_result.duplicate_favorites_skipped,
            analytics_events_transferred=link_result.analytics_events_transferred,
            inline_usage_events_transferred=link_result.inline_usage_events_transferred,
            views_transferred=link_result.views_transferred,
        ),
    )


__all__ = ["router"]
