"""Shared FastAPI dependency exports."""

from memexpert.api.dependencies.auth import (
    AUTH_ERROR_RESPONSES,
    AuthHTTPError,
    AuthServiceDep,
    CurrentUserDep,
    DbSessionDep,
    FullAccountUserDep,
    OptionalCurrentUserDep,
    auth_http_exception_handler,
    get_auth_service,
    get_current_user,
    get_full_account_user,
    get_optional_current_user,
    to_auth_http_error,
)

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
