"""Guest-session and provider-auth routes for session issuance, self lookup, and linking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Body, Request, status
from fastapi.responses import Response

from memexpert.api.cookies import delete_access_cookie, set_access_cookie
from memexpert.api.dependencies import (
    AUTH_ERROR_RESPONSES,
    AccountLinkServiceDep,
    AnalyticsServiceDep,
    AuthServiceDep,
    AutoGuestUserDep,
    CurrentUserDep,
    DbSessionDep,
    ForbidFullAccountCallerDep,
    GuestUserDep,
    OptionalAccessTokenDep,
    OptionalGuestUserDep,
    to_auth_http_error,
)
from memexpert.models.enums import AnalyticsEventType
from memexpert.schemas.auth import (
    AuthSessionRead,
    CurrentSessionRead,
    EmailLoginRequest,
    EmailSignupRequest,
    GoogleAuthRequest,
    GuestBootstrapRequest,
    LinkedProvidersRead,
    ProfileStatsRead,
    TelegramLinkStartRead,
    TelegramMiniAppAuthRequest,
    TelegramWidgetAuthRequest,
    UserPreferencesUpdateRequest,
)
from memexpert.schemas.user import UserRead
from memexpert.services import (
    AuthenticatedUserNotFoundError,
    AuthServiceError,
    AuthSession,
    LinkedProvidersProjection,
    UserNotFoundError,
    UserService,
)
from memexpert.services.analytics import AnalyticsService, hash_external_identifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _extract_client_ip(request: Request) -> str | None:
    """Return the raw request client host, or None for non-HTTP test drivers."""

    if request.client is None:
        return None
    return request.client.host


def _issue_session_response(response: Response, auth_session: AuthSession) -> AuthSessionRead:
    """Attach the access cookie and return the cookie-less public session schema."""

    set_access_cookie(response, auth_session.access_token)
    return auth_session.to_read()


def _build_current_session_read(user: UserRead, linked_providers: LinkedProvidersProjection) -> CurrentSessionRead:
    """Return the web session envelope without any token-bearing fields."""

    return CurrentSessionRead(
        user=user,
        linked_providers=_build_linked_providers_read(linked_providers),
    )


@router.post(
    "/guest",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
    summary="Bootstrap a guest session",
)
async def create_guest_session(
    request: Request,
    response: Response,
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

    return _issue_session_response(response, auth_session)


@router.get(
    "/session",
    response_model=CurrentSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Get or bootstrap the current web session",
)
async def read_current_session(
    current_user: AutoGuestUserDep,
    account_link_service: AccountLinkServiceDep,
) -> CurrentSessionRead:
    """Return current user and provider state, bootstrapping a guest on no-cookie web hits."""

    try:
        linked_providers = await account_link_service.get_linked_providers(user_id=current_user.id)
    except UserNotFoundError as exc:
        raise to_auth_http_error(
            AuthenticatedUserNotFoundError(
                f"Authenticated user {current_user.id} no longer exists.",
            )
        ) from exc

    return _build_current_session_read(current_user, linked_providers)


@router.get(
    "/profile-stats",
    response_model=ProfileStatsRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Read profile interaction stats",
)
async def read_profile_stats(
    current_user: AutoGuestUserDep,
    analytics_service: AnalyticsServiceDep,
) -> ProfileStatsRead:
    """Return profile stats derived from the caller's persisted analytics events."""

    return await analytics_service.profile_stats(user_id=current_user.id)


@router.post(
    "/session/refresh",
    response_model=CurrentSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Refresh the current web session after external account linking",
)
async def refresh_current_session(
    request: Request,
    response: Response,
    access_token: OptionalAccessTokenDep,
    auth_service: AuthServiceDep,
    account_link_service: AccountLinkServiceDep,
) -> CurrentSessionRead:
    """Replace the browser cookie after Telegram links or merges complete outside the browser."""

    if access_token is None:
        raise to_auth_http_error(AuthenticatedUserNotFoundError("Access session cookie is required."))

    try:
        auth_session = await auth_service.refresh_session_from_access_token(
            access_token,
            ip_address=_extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        linked_providers = await account_link_service.get_linked_providers(user_id=auth_session.user.id)
    except AuthServiceError as exc:
        raise to_auth_http_error(exc) from exc
    except UserNotFoundError as exc:
        raise to_auth_http_error(
            AuthenticatedUserNotFoundError(
                f"Authenticated user {auth_session.user.id} no longer exists.",
            )
        ) from exc

    set_access_cookie(response, auth_session.access_token)
    return _build_current_session_read(auth_session.user, linked_providers)


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

    return _issue_session_response(response, auth_session)


@router.post(
    "/email/login",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with email and password",
)
async def login_with_email(
    request: Request,
    response: Response,
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

    return _issue_session_response(response, auth_session)


@router.post(
    "/google",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Google OAuth",
)
async def login_with_google(
    request: Request,
    response: Response,
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

    return _issue_session_response(response, auth_session)


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

    return _issue_session_response(response, auth_session)


@router.post(
    "/telegram-miniapp",
    response_model=AuthSessionRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Authenticate a full account with Telegram Mini App initData",
)
async def login_with_telegram_miniapp(
    request: Request,
    response: Response,
    _guard: ForbidFullAccountCallerDep,
    optional_guest: OptionalGuestUserDep,
    account_link_service: AccountLinkServiceDep,
    auth_service: AuthServiceDep,
    analytics_service: AnalyticsServiceDep,
    session: DbSessionDep,
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

    await _record_telegram_miniapp_open(analytics_service, session, auth_session)
    return _issue_session_response(response, auth_session)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=AUTH_ERROR_RESPONSES,
    summary="Soft logout for the current client",
)
async def logout(_current_user: CurrentUserDep) -> Response:
    """Clear the caller's access cookie and return 204.

    Single-device soft logout: the server performs no state mutation;
    the JWT remains cryptographically valid until its TTL or a
    ``logout-all`` nonce bump. Dropping the cookie here guarantees the
    caller's current tab stops sending it immediately. Call
    ``/auth/logout-all`` to invalidate every outstanding token for the
    account server-side.
    """

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    delete_access_cookie(response)
    return response


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
    user row. The caller's own cookie is also cleared so its current
    tab stops sending a token the server will immediately reject.
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
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    delete_access_cookie(response)
    return response


@router.get(
    "/me",
    response_model=UserRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Get the authenticated caller",
)
async def read_current_user(current_user: CurrentUserDep) -> UserRead:
    """Return the current DB-backed user for the supplied bearer token."""

    return current_user


@router.patch(
    "/preferences",
    response_model=UserRead,
    responses=AUTH_ERROR_RESPONSES,
    summary="Update the authenticated caller's preferences",
)
async def update_current_user_preferences(
    current_user: CurrentUserDep,
    session: DbSessionDep,
    preferences: Annotated[UserPreferencesUpdateRequest, Body()],
) -> UserRead:
    """Persist user preferences for the cookie-authenticated caller."""

    user_service = UserService(session)
    try:
        return await user_service.update_preferences(
            user_id=current_user.id,
            nsfw_enabled=preferences.nsfw_enabled,
            language=preferences.language,
        )
    except UserNotFoundError as exc:
        raise to_auth_http_error(
            AuthenticatedUserNotFoundError(
                f"Authenticated user {current_user.id} no longer exists.",
            )
        ) from exc


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


async def _record_telegram_miniapp_open(
    analytics_service: AnalyticsService,
    session: AsyncSession,
    auth_session: AuthSession,
) -> None:
    telegram_id = auth_session.user.telegram_id
    properties: dict[str, object] = {"action": "auth_session_issued"}
    if telegram_id is not None:
        properties["telegram_user_hash"] = hash_external_identifier("telegram_user", telegram_id)
    try:
        await analytics_service.record_interaction_event(
            {
                "event_type": AnalyticsEventType.MINIAPP_OPEN,
                "user_id": auth_session.user.id,
                "surface": "telegram_miniapp_auth",
                "properties": properties,
            }
        )
    except Exception:
        if session.in_transaction():
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "Telegram Mini App auth analytics rollback failed.",
                    extra={
                        "event": "telegram_analytics_rollback_failed",
                        "analytics_event_type": AnalyticsEventType.MINIAPP_OPEN.value,
                        "surface": "telegram_miniapp_auth",
                        "user_id": str(auth_session.user.id),
                    },
                )
        logger.exception(
            "Telegram Mini App auth analytics write failed.",
            extra={
                "event": "telegram_analytics_write_failed",
                "analytics_event_type": AnalyticsEventType.MINIAPP_OPEN.value,
                "surface": "telegram_miniapp_auth",
                "user_id": str(auth_session.user.id),
            },
        )


__all__ = ["router"]
