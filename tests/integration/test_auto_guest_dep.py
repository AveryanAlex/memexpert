# ruff: noqa: TC001
"""Integration tests for ``AutoGuestUserDep`` transparent guest bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from memexpert.api.dependencies import AutoGuestUserDep
from memexpert.models.collection import Collection, CollectionMember
from memexpert.models.enums import AccountType
from memexpert.models.user import LoginEvent, User
from memexpert.schemas.user import UserRead

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AUTO_GUEST_PROBE_PATH = "/api/v1/test-auth/auto-guest"
ACCESS_COOKIE_NAME = "memexpert_access_token"

auto_guest_probe_router = APIRouter()


@auto_guest_probe_router.get(AUTO_GUEST_PROBE_PATH, response_model=UserRead, tags=["test-auth"])
async def read_auto_guest_probe(current_user: AutoGuestUserDep) -> UserRead:
    """Expose the auto-bootstrap guest dependency through a route-level probe."""

    return current_user


async def test_auto_guest_dep_bootstraps_fresh_guest_on_cache_miss(
    auth_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A caller with no session triggers a one-shot guest bootstrap.

    The dep must persist exactly one ``users`` row and one
    ``login_events`` row (the nonce-refactor audit entry), attach a
    ``Set-Cookie`` header to the outgoing response, and return the
    freshly-minted user. The lazy-Favorites cold path guarantees zero
    collection rows for one-shot visitors.
    """

    auth_app.include_router(auto_guest_probe_router)
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        first_response = await client.get(AUTO_GUEST_PROBE_PATH)
        second_response = await client.get(AUTO_GUEST_PROBE_PATH)

    assert first_response.status_code == 200
    first_payload = first_response.json()
    assert first_payload["account_type"] == "guest"
    assert f"{ACCESS_COOKIE_NAME}=" in first_response.headers["set-cookie"]

    assert second_response.status_code == 200
    second_payload = second_response.json()
    # Second call carries the cookie, reuses the same guest row, and
    # emits no Set-Cookie header (the dep short-circuits).
    assert second_payload["id"] == first_payload["id"]
    assert "set-cookie" not in second_response.headers

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        login_event_count_result = await session.execute(select(func.count()).select_from(LoginEvent))
        collection_count_result = await session.execute(select(func.count()).select_from(Collection))
        member_count_result = await session.execute(select(func.count()).select_from(CollectionMember))

        assert user_count_result.scalar_one() == 1
        assert login_event_count_result.scalar_one() == 1
        # Lazy Favorites cold-path invariant — crawlers never allocate
        # collection or membership rows.
        assert collection_count_result.scalar_one() == 0
        assert member_count_result.scalar_one() == 0

        persisted_user_result = await session.execute(select(User).where(User.id == first_payload["id"]))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.account_type is AccountType.GUEST
        assert persisted_user.active_save_collection_id is None


async def test_auto_guest_dep_rejects_invalid_cookies_with_401(
    auth_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A structurally-broken cookie fails verification before bootstrap fires.

    Auto-bootstrap only kicks in when the session is *absent*, not when
    the caller presents a forged or tampered token. A broken cookie
    hits ``get_optional_current_user`` first and raises the usual 401.
    """

    auth_app.include_router(auto_guest_probe_router)
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(ACCESS_COOKIE_NAME, "not-a-jwt")
        response = await client.get(AUTO_GUEST_PROBE_PATH)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        login_event_count_result = await session.execute(select(func.count()).select_from(LoginEvent))
        # The rejection happens before any DB write so no throwaway
        # guest leaks out of the failing request.
        assert user_count_result.scalar_one() == 0
        assert login_event_count_result.scalar_one() == 0
