# ruff: noqa: TC001,TC002
"""Integration tests for cookie-authenticated browser-admin routes."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.content import Meme, ModerationDecision, ModerationReport
from memexpert.models.enums import (
    ContentKind,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    SourcePlatform,
)
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
        forbidden_reports_response = await non_admin_client.get("/api/v1/admin/moderation-reports")

    async with AsyncClient(transport=transport, base_url="https://testserver") as operator_header_client:
        operator_response = await operator_header_client.get(
            "/api/v1/admin/session",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )

    assert anonymous_response.status_code == 401
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["code"] == "admin_required"
    assert forbidden_reports_response.status_code == 403
    assert forbidden_reports_response.json()["code"] == "admin_required"
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


async def test_admin_can_list_and_resolve_moderation_report_with_audited_decision(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-resolve-report@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        user_service = UserService(session)
        reporter = await create_full_user_via_upgrade(user_service, email="reporter@example.com")
        admin = (
            await session.execute(select(User).where(User.email == "admin-resolve-report@example.com"))
        ).scalar_one()
        meme = Meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        report = ModerationReport(
            meme=meme,
            reporter_user_id=reporter.id,
            reason=ModerationReason.NSFW,
            note="This should be marked nsfw",
        )
        session.add_all([meme, report])
        await session.commit()
        await session.refresh(report)
        report_id = report.id
        meme_id = meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        list_response = await admin_client.get("/api/v1/admin/moderation-reports")
        resolve_response = await admin_client.post(
            f"/api/v1/admin/moderation-reports/{report_id}/resolve",
            json={"action": "mark_nsfw", "reason": "nsfw", "note": "Confirmed by moderator"},
        )
        history_response = await admin_client.get(f"/api/v1/admin/moderation-decisions?meme_id={meme_id}")

    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [str(report_id)]
    assert list_response.json()[0]["meme"]["id"] == str(meme_id)

    assert resolve_response.status_code == 200
    resolved_payload = resolve_response.json()
    assert resolved_payload["status"] == "resolved"
    assert resolved_payload["resolved_by_admin_user_id"] == str(admin_id)
    assert resolved_payload["meme"]["is_nsfw"] is True

    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert len(history_payload) == 1
    assert history_payload[0]["report_id"] == str(report_id)
    assert history_payload[0]["action"] == "mark_nsfw"
    assert history_payload[0]["previous_is_nsfw"] is False
    assert history_payload[0]["new_is_nsfw"] is True

    async with postgres_session_factory() as session:
        persisted_report = await session.get(ModerationReport, report_id)
        persisted_meme = await session.get(Meme, meme_id)
        persisted_decision = await session.scalar(
            select(ModerationDecision).where(ModerationDecision.report_id == report_id),
        )

        assert persisted_report is not None
        assert persisted_report.status is ModerationReportStatus.RESOLVED
        assert persisted_report.resolved_by_admin_user_id == admin_id
        assert persisted_meme is not None
        assert persisted_meme.is_nsfw is True
        assert persisted_decision is not None
        assert persisted_decision.admin_user_id == admin_id
        assert persisted_decision.reason is ModerationReason.NSFW


async def test_admin_direct_meme_moderation_override_creates_decision_audit_record(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-direct-override@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (
            await session.execute(select(User).where(User.email == "admin-direct-override@example.com"))
        ).scalar_one()
        meme = Meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        session.add(meme)
        await session.commit()
        await session.refresh(meme)
        meme_id = meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={
                "is_public": False,
                "is_nsfw": True,
                "reason": "spam",
                "note": "Manual override from admin screen",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_public"] is False
    assert payload["is_nsfw"] is True

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        persisted_decision = await session.scalar(
            select(ModerationDecision).where(ModerationDecision.meme_id == meme_id),
        )

        assert persisted_meme is not None
        assert persisted_meme.is_public is False
        assert persisted_meme.is_nsfw is True
        assert persisted_decision is not None
        assert persisted_decision.report_id is None
        assert persisted_decision.admin_user_id == admin_id
        assert persisted_decision.action is ModerationAction.OVERRIDE_FLAGS
        assert persisted_decision.reason is ModerationReason.SPAM
        assert persisted_decision.previous_is_public is True
        assert persisted_decision.previous_is_nsfw is False
        assert persisted_decision.new_is_public is False
        assert persisted_decision.new_is_nsfw is True
