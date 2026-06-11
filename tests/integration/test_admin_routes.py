# ruff: noqa: TC001,TC002
"""Integration tests for cookie-authenticated browser-admin routes."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.enums import SourcePlatform
from memexpert.models.user import ChannelSuggestion, User
from memexpert.services import AuthService, UserService
from tests.conftest import create_full_user_via_upgrade
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME, build_test_auth_service


async def _issue_user_cookie(
    session_factory: async_sessionmaker[AsyncSession],
    auth_settings_overrides: dict[str, str],
    *,
    email: str,
    is_admin: bool,
) -> str:
    async with session_factory() as session:
        user_service = UserService(session)
        auth_service: AuthService = build_test_auth_service(session, auth_settings_overrides)
        user = await create_full_user_via_upgrade(user_service, email=email)
        persisted_user = await session.get(User, user.id)
        assert persisted_user is not None
        persisted_user.is_admin = is_admin
        await session.commit()
        auth_session = await auth_service.issue_session_for_user(user)
        return auth_session.access_token


async def test_admin_routes_require_session_cookie_admin_flag_and_ignore_operator_header(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        anonymous_response = await anonymous_client.get("/api/v1/admin/session")

    non_admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-denied@example.com",
        is_admin=False,
    )
    async with AsyncClient(transport=transport, base_url="https://testserver") as non_admin_client:
        non_admin_client.cookies.set(ACCESS_COOKIE_NAME, non_admin_token)
        forbidden_response = await non_admin_client.get("/api/v1/admin/session")

    async with AsyncClient(transport=transport, base_url="https://testserver") as operator_header_client:
        operator_response = await operator_header_client.get(
            "/api/v1/admin/session",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )

    assert anonymous_response.status_code == 401
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["code"] == "admin_required"
    assert operator_response.status_code == 401


async def test_admin_can_approve_channel_suggestion_through_cookie_session(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-approve@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        owner = (await session.execute(select(User).where(User.email == "admin-approve@example.com"))).scalar_one()
        suggestion = ChannelSuggestion(
            user_id=owner.id,
            platform=SourcePlatform.TELEGRAM,
            channel_url="https://t.me/memexpert_source",
        )
        session.add(suggestion)
        await session.commit()
        await session.refresh(suggestion)
        suggestion_id = suggestion.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/channel-suggestions/{suggestion_id}/approve",
            json={"admin_note": "Looks relevant"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approved"
    assert payload["admin_note"] == "Looks relevant"
    assert payload["reviewed_at"] is not None

    async with postgres_session_factory() as session:
        persisted = await session.get(ChannelSuggestion, suggestion_id)
        assert persisted is not None
        assert persisted.status.value == "approved"
