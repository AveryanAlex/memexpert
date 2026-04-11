"""Guest-session and provider-auth routes for session issuance, self lookup, and linking."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, status
from fastapi.responses import Response

from memexpert.api.dependencies import (
    AUTH_ERROR_RESPONSES,
    AccountLinkServiceDep,
    AuthServiceDep,
    CurrentUserDep,
    DbSessionDep,
    ForbidFullAccountCallerDep,
    GuestUserDep,
    OptionalGuestUserDep,
    to_auth_http_error,
)
from memexpert.schemas.auth import (
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
    AuthenticatedUserNotFoundError,
    AuthServiceError,
    LinkedProvidersProjection,
    UserNotFoundError,
    UserService,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _extract_client_ip(request: Request) -> str | None:
    """Return the raw request client host, or None for non-HTTP test drivers."""

    if request.client is None:
        return None
    return request.client.host


@router.post(
    "/guest",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Bootstrap a guest session",
)
async def create_guest_session(
    request: Request,
    auth_service: AuthServiceDep,
    guest_request: Annotated[GuestBootstrapRequest | None, Body()] = None,
) -> AuthSessionRead:
    """Create a guest account and immediately issue a long-lived access token."""

    try:
        auth_session = await auth_service.create_guest_session(
            guest_request,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

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
    _guard: ForbidFullAccountCallerDep,
    optional_guest: OptionalGuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[EmailSignupRequest, Body()],
) -> AuthSessionRead:
    """Create a full account via the unified guest-upgrade writer path."""

    try:
        link_result = await account_link_service.link_guest_with_email_signup(
            guest_user_id=optional_guest.id if optional_guest else None,
            email=credentials.email,
            password=credentials.password,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    return auth_session.to_read()


@router.post(
    "/email/login",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with email and password",
)
async def login_with_email(
    request: Request,
    _guard: ForbidFullAccountCallerDep,
    optional_guest: OptionalGuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[EmailLoginRequest, Body()],
) -> AuthSessionRead:
    """Authenticate an existing email/password account via the unified merge path."""

    try:
        link_result = await account_link_service.link_guest_with_email_login(
            guest_user_id=optional_guest.id if optional_guest else None,
            email=credentials.email,
            password=credentials.password,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    return auth_session.to_read()


@router.post(
    "/google",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Google OAuth",
)
async def login_with_google(
    request: Request,
    _guard: ForbidFullAccountCallerDep,
    optional_guest: OptionalGuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[GoogleAuthRequest, Body()],
) -> AuthSessionRead:
    """Exchange a Google auth code through the unified guest-upgrade writer path."""

    try:
        link_result = await account_link_service.link_guest_with_google_code(
            guest_user_id=optional_guest.id if optional_guest else None,
            code=credentials.code,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    return auth_session.to_read()


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
    _guard: ForbidFullAccountCallerDep,
    optional_guest: OptionalGuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[TelegramWidgetAuthRequest, Body()],
) -> AuthSessionRead:
    """Validate a Telegram Login Widget payload via the unified writer path."""

    try:
        link_result = await account_link_service.link_guest_with_telegram_widget(
            guest_user_id=optional_guest.id if optional_guest else None,
            payload=credentials,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    return auth_session.to_read()


@router.post(
    "/telegram-miniapp",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Telegram Mini App initData",
)
async def login_with_telegram_miniapp(
    request: Request,
    _guard: ForbidFullAccountCallerDep,
    optional_guest: OptionalGuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    credentials: Annotated[TelegramMiniAppAuthRequest, Body()],
) -> AuthSessionRead:
    """Validate Telegram Mini App initData via the unified writer path."""

    try:
        link_result = await account_link_service.link_guest_with_telegram_miniapp(
            guest_user_id=optional_guest.id if optional_guest else None,
            init_data=credentials.init_data,
        )
        auth_session = await auth_service.issue_session_for_user(
            link_result.user,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            reload_user=False,
        )
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc

    return auth_session.to_read()


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=AUTH_ERROR_RESPONSES,
    summary="Soft logout for the current client",
)
async def logout(_current_user: CurrentUserDep) -> Response:
    """Return a 204 so the caller can drop its locally-held access token.

    This is a "single device" soft logout: the server performs no
    state mutation. Clients that hold the access token in memory simply
    discard it; nothing else needs to happen on the server because the
    JWT is stateless. Call ``/auth/logout-all`` to invalidate every
    outstanding token for the account.
    """

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=AUTH_ERROR_RESPONSES,
    summary="Invalidate every outstanding session for the current account",
)
async def logout_all(
    current_user: CurrentUserDep,
    session: DbSessionDep,
) -> Response:
    """Bump the user's token nonce so every live JWT fails verification.

    This is the nuclear revocation primitive: the caller stays
    authenticated for exactly this one response, and every other
    outstanding access token (browser, mobile, stale tab) is invalid on
    its next request because its ``nonce`` claim no longer matches the
    user row.
    """

    user_service = UserService(session)
    try:
        await user_service.bump_token_nonce(user_id=current_user.id)
    except UserNotFoundError as exc:
        raise to_auth_http_error(
            AuthenticatedUserNotFoundError(
                f"Authenticated user {current_user.id} no longer exists.",
            )
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


def _build_linked_providers_read(linked_providers: LinkedProvidersProjection) -> LinkedProvidersRead:
    return LinkedProvidersRead(
        email=linked_providers.email,
        email_verified_at=linked_providers.email_verified_at,
        has_password=linked_providers.has_password,
        google_linked=linked_providers.google_linked,
        telegram_linked=linked_providers.telegram_linked,
    )


__all__ = ["router"]
