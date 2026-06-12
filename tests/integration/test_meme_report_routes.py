# ruff: noqa: TC001,TC002,TC003
"""Integration tests for user-facing meme report submission."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.content import Meme, ModerationReport
from memexpert.models.enums import ContentKind, ModerationReason, ModerationReportStatus
from memexpert.models.user import User
from memexpert.services import AuthService, UserService
from tests.conftest import create_full_user_via_upgrade
from tests.integration.test_api_security import BROWSER_REQUESTED_WITH_VALUE
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME, build_test_auth_service


async def _issue_full_user_cookie(
    session_factory: async_sessionmaker[AsyncSession],
    auth_settings_overrides: dict[str, str],
    *,
    email: str,
    nsfw_enabled: bool = False,
    is_admin: bool = False,
) -> tuple[str, uuid.UUID]:
    async with session_factory() as session:
        user_service = UserService(session)
        auth_service: AuthService = build_test_auth_service(session, auth_settings_overrides)
        user_read = await create_full_user_via_upgrade(
            user_service,
            email=email,
            nsfw_enabled=nsfw_enabled,
        )
        user = await session.get(User, user_read.id)
        assert user is not None
        user.is_admin = is_admin
        user_id = user.id
        await session.commit()
        auth_session = await auth_service.issue_session_for_user(user_read)
        return auth_session.access_token, user_id


async def _create_meme(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    is_public: bool = True,
    is_nsfw: bool = False,
) -> uuid.UUID:
    async with session_factory() as session:
        meme = Meme(media_type=ContentKind.IMAGE, is_public=is_public, is_nsfw=is_nsfw)
        session.add(meme)
        await session.flush()
        meme_id = meme.id
        await session.commit()
        return meme_id


async def test_report_meme_requires_authenticated_full_account_and_rejects_guests(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_id = await _create_meme(postgres_session_factory)
    full_token, _ = await _issue_full_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="report-auth-full@example.com",
    )
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        anonymous_response = await anonymous_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "spam"},
        )

    async with AsyncClient(transport=transport, base_url="https://testserver") as guest_client:
        _ = await guest_client.post("/api/v1/auth/guest")
        guest_response = await guest_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "spam"},
        )

    async with AsyncClient(transport=transport, base_url="https://testserver") as full_client:
        full_client.cookies.set(ACCESS_COOKIE_NAME, full_token)
        full_response = await full_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "spam"},
        )

    assert anonymous_response.status_code == 401
    assert anonymous_response.json()["code"] == "invalid_token"
    assert guest_response.status_code == 403
    assert guest_response.json()["code"] == "upgrade_required"
    assert full_response.status_code == 200


async def test_browser_report_write_requires_csrf_header_before_auth_dependency(
    browser_security_client: AsyncClient,
) -> None:
    meme_id = "11111111-1111-4111-8111-111111111111"
    rejected_response = await browser_security_client.post(
        f"/api/v1/memes/{meme_id}/report",
        headers={"Origin": "https://app.memexpert.net"},
        json={"reason": "spam"},
    )
    accepted_response = await browser_security_client.post(
        f"/api/v1/memes/{meme_id}/report",
        headers={
            "Origin": "https://app.memexpert.net",
            "X-Requested-With": BROWSER_REQUESTED_WITH_VALUE,
        },
        json={"reason": "spam"},
    )

    assert rejected_response.status_code == 403
    assert rejected_response.json()["code"] == "csrf_failed"
    assert accepted_response.status_code == 401
    assert accepted_response.json()["code"] == "invalid_token"


async def test_full_user_report_creates_admin_queue_entry_and_normalizes_payload(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_id = await _create_meme(postgres_session_factory)
    reporter_token, reporter_id = await _issue_full_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="reporter-queue@example.com",
    )
    admin_token, _ = await _issue_full_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="report-admin@example.com",
        is_admin=True,
    )
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as reporter_client:
        reporter_client.cookies.set(ACCESS_COOKIE_NAME, reporter_token)
        report_response = await reporter_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "harassment", "note": "  targets a private person  "},
        )

    assert report_response.status_code == 200
    report_payload = report_response.json()
    assert report_payload["meme_id"] == str(meme_id)
    assert report_payload["status"] == "pending"
    assert report_payload["reason"] == "harassment"
    assert report_payload["note"] == "targets a private person"
    assert "reporter_user_id" not in report_payload
    report_id = report_payload["id"]

    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        queue_response = await admin_client.get("/api/v1/admin/moderation-reports")

    assert queue_response.status_code == 200
    queue_payload = queue_response.json()
    assert [item["id"] for item in queue_payload] == [report_id]
    assert queue_payload[0]["meme"]["id"] == str(meme_id)
    assert queue_payload[0]["reporter_user_id"] == str(reporter_id)


async def test_duplicate_open_user_report_reuses_existing_row_without_spam(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_id = await _create_meme(postgres_session_factory)
    reporter_token, reporter_id = await _issue_full_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="reporter-duplicate@example.com",
    )
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as reporter_client:
        reporter_client.cookies.set(ACCESS_COOKIE_NAME, reporter_token)
        first_response = await reporter_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "spam", "note": "first"},
        )
        second_response = await reporter_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "illegal", "note": "second"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["id"] == first_response.json()["id"]
    assert second_response.json()["reason"] == "spam"
    assert second_response.json()["note"] == "first"

    async with postgres_session_factory() as session:
        report_count = await session.scalar(
            select(func.count())
            .select_from(ModerationReport)
            .where(ModerationReport.meme_id == meme_id, ModerationReport.reporter_user_id == reporter_id),
        )
        assert report_count == 1


async def test_report_rejects_hidden_and_nsfw_filtered_memes_without_creating_rows(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    hidden_meme_id = await _create_meme(postgres_session_factory, is_public=False)
    nsfw_meme_id = await _create_meme(postgres_session_factory, is_nsfw=True)
    reporter_token, _ = await _issue_full_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="reporter-filtered@example.com",
        nsfw_enabled=False,
    )
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as reporter_client:
        reporter_client.cookies.set(ACCESS_COOKIE_NAME, reporter_token)
        hidden_response = await reporter_client.post(
            f"/api/v1/memes/{hidden_meme_id}/report",
            json={"reason": "other"},
        )
        nsfw_response = await reporter_client.post(
            f"/api/v1/memes/{nsfw_meme_id}/report",
            json={"reason": "other"},
        )

    assert hidden_response.status_code == 404
    assert nsfw_response.status_code == 404

    async with postgres_session_factory() as session:
        report_count = await session.scalar(
            select(func.count())
            .select_from(ModerationReport)
            .where(ModerationReport.meme_id.in_((hidden_meme_id, nsfw_meme_id))),
        )
        assert report_count == 0


async def test_report_reuses_existing_in_review_but_not_closed_report(
    auth_app,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_id = await _create_meme(postgres_session_factory)
    reporter_token, reporter_id = await _issue_full_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="reporter-in-review@example.com",
    )
    async with postgres_session_factory() as session:
        closed_report = ModerationReport(
            meme_id=meme_id,
            reporter_user_id=reporter_id,
            status=ModerationReportStatus.DISMISSED,
            reason=ModerationReason.SPAM,
        )
        in_review_report = ModerationReport(
            meme_id=meme_id,
            reporter_user_id=reporter_id,
            status=ModerationReportStatus.IN_REVIEW,
            reason=ModerationReason.COPYRIGHT,
        )
        session.add_all([closed_report, in_review_report])
        await session.commit()
        await session.refresh(in_review_report)
        in_review_report_id = in_review_report.id
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as reporter_client:
        reporter_client.cookies.set(ACCESS_COOKIE_NAME, reporter_token)
        response = await reporter_client.post(
            f"/api/v1/memes/{meme_id}/report",
            json={"reason": "illegal"},
        )

    assert response.status_code == 200
    assert response.json()["id"] == str(in_review_report_id)
