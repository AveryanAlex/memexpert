"""Cookie-based access token transport shared by auth routes and dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memexpert.core.config import get_settings

if TYPE_CHECKING:
    from fastapi import Response


def set_access_cookie(response: Response, access_token: str) -> None:
    """Attach the HttpOnly access-token cookie to an outgoing response.

    Mutates ``response`` in place with the configured cookie name,
    attributes, and max-age. Called from every auth-write route so
    the browser (or any cookie-jar-backed client) carries the token
    on subsequent requests without ever exposing it to JS.
    """

    settings = get_settings()
    response.set_cookie(
        key=settings.auth_access_cookie_name,
        value=access_token,
        max_age=settings.auth_access_token_ttl_seconds,
        path=settings.auth_access_cookie_path,
        domain=settings.auth_access_cookie_domain,
        secure=settings.auth_access_cookie_secure,
        httponly=settings.auth_access_cookie_httponly,
        samesite=settings.auth_access_cookie_samesite,
    )


def delete_access_cookie(response: Response) -> None:
    """Clear the access-token cookie on logout.

    Uses the same path/domain as ``set_access_cookie`` so the browser
    actually drops the stored value; omitting them would leave the
    cookie intact under the original scope.
    """

    settings = get_settings()
    response.delete_cookie(
        key=settings.auth_access_cookie_name,
        path=settings.auth_access_cookie_path,
        domain=settings.auth_access_cookie_domain,
    )


__all__ = ["delete_access_cookie", "set_access_cookie"]
