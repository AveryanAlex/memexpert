"""Shared FastAPI dependency exports."""

from memexpert.api.dependencies.auth import (
    AUTH_ERROR_RESPONSES,
    AUTH_ERROR_STATUS_CODES,
    AuthHTTPError,
    AuthServiceDep,
    CurrentUserDep,
    DbSessionDep,
    FullAccountUserDep,
    OptionalCurrentUserDep,
    ProviderAuthServiceDep,
    auth_http_exception_handler,
    get_auth_service,
    get_current_user,
    get_full_account_user,
    get_optional_current_user,
    get_provider_auth_service,
    to_auth_http_error,
)

__all__ = [
    "AUTH_ERROR_RESPONSES",
    "AUTH_ERROR_STATUS_CODES",
    "AuthHTTPError",
    "AuthServiceDep",
    "CurrentUserDep",
    "DbSessionDep",
    "FullAccountUserDep",
    "OptionalCurrentUserDep",
    "ProviderAuthServiceDep",
    "auth_http_exception_handler",
    "get_auth_service",
    "get_current_user",
    "get_full_account_user",
    "get_optional_current_user",
    "get_provider_auth_service",
    "to_auth_http_error",
]
