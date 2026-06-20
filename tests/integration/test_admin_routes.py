# ruff: noqa: TC001,TC002
"""Integration tests for cookie-authenticated browser-admin routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid7

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import memexpert.services.admin as admin_service_module
from memexpert.models.collection import Collection, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    AdminMemeDestructiveAuditLog,
    BlockedPerceptualHash,
    BlockedPerceptualHashAuditLog,
    Meme,
    MemeFile,
    MemeMergeLog,
    MemeSeoPage,
    MemeTemplate,
    ModerationDecision,
    ModerationReport,
    SourceChannel,
    TelegramAdminAuditLog,
    TelegramSession,
)
from memexpert.models.enums import (
    ContentKind,
    ContentProcessingStatus,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.models.user import ChannelSuggestion, User
from memexpert.services import AuthService, UserService
from tests.conftest import create_full_user_via_upgrade
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME, build_test_auth_service

if TYPE_CHECKING:
    from fastapi import FastAPI


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


def _canonical_meme(
    *,
    file_key: str | None = None,
    file_quality: float = 0.9,
    **meme_kwargs: object,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid7()
    file_id = uuid7()
    meme = Meme(id=meme_id, primary_file_id=file_id, **meme_kwargs)
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=file_key or f"admin/{meme_id}/primary.jpg",
        mime_type="image/jpeg",
        quality_score=file_quality,
    )
    return meme, file


async def _persist_canonical_meme(session: AsyncSession, meme: Meme, file: MemeFile) -> None:
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()


async def test_admin_routes_require_session_cookie_admin_flag_and_ignore_operator_header(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_id = "11111111-1111-4111-8111-111111111111"
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        anonymous_session_response = await anonymous_client.get("/api/v1/admin/session")
        anonymous_telegram_sessions_response = await anonymous_client.get("/api/v1/admin/telegram/sessions")
        anonymous_detail_response = await anonymous_client.get(f"/api/v1/admin/memes/{meme_id}")
        anonymous_override_response = await anonymous_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={"is_nsfw": True},
        )
        anonymous_seo_pages_response = await anonymous_client.get("/api/v1/admin/seo-pages")
        anonymous_seo_edit_response = await anonymous_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"slug": "permission", "title": "Permission", "meta": "Permission", "alt": "Permission"},
        )
        anonymous_seo_regenerate_response = await anonymous_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": meme_id},
        )

    non_admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-denied@example.com",
        is_admin=False,
    )
    async with AsyncClient(transport=transport, base_url="https://testserver") as non_admin_client:
        non_admin_client.cookies.set(ACCESS_COOKIE_NAME, non_admin_token)
        forbidden_session_response = await non_admin_client.get("/api/v1/admin/session")
        forbidden_detail_response = await non_admin_client.get(f"/api/v1/admin/memes/{meme_id}")
        forbidden_override_response = await non_admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={"is_nsfw": True},
        )
        forbidden_delete_response = await non_admin_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            json={"confirmation": meme_id, "note": "test"},
        )
        forbidden_merge_response = await non_admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/merge",
            json={"target_meme_id": meme_id, "confirmation": meme_id, "note": "test"},
        )
        forbidden_template_create_response = await non_admin_client.post(
            "/api/v1/admin/meme-templates",
            json={"slug": "permission-test", "name": "Permission Test"},
        )
        forbidden_source_mark_dead_response = await non_admin_client.post(
            f"/api/v1/admin/source-channels/{meme_id}/mark-dead",
            json={"confirmation": meme_id},
        )
        forbidden_seo_pages_response = await non_admin_client.get("/api/v1/admin/seo-pages")
        forbidden_seo_edit_response = await non_admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"slug": "permission", "title": "Permission", "meta": "Permission", "alt": "Permission"},
        )
        forbidden_seo_regenerate_response = await non_admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": meme_id},
        )
        forbidden_telegram_session_create_response = await non_admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={"name": "permission-test", "display_name": "Permission Test"},
        )
        forbidden_telegram_channels_response = await non_admin_client.get("/api/v1/admin/telegram/channels")
        forbidden_reports_response = await non_admin_client.get("/api/v1/admin/moderation-reports")
        forbidden_blocked_hashes_response = await non_admin_client.get("/api/v1/admin/blocked-perceptual-hashes")

    async with AsyncClient(transport=transport, base_url="https://testserver") as operator_header_client:
        operator_session_response = await operator_header_client.get(
            "/api/v1/admin/session",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )
        operator_detail_response = await operator_header_client.get(
            f"/api/v1/admin/memes/{meme_id}",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )
        operator_delete_response = await operator_header_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            headers={"X-Pipeline-Operator-Token": "anything"},
            json={"confirmation": meme_id, "note": "test"},
        )
        operator_seo_pages_response = await operator_header_client.get(
            "/api/v1/admin/seo-pages",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )
        operator_telegram_sessions_response = await operator_header_client.get(
            "/api/v1/admin/telegram/sessions",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )

    assert anonymous_session_response.status_code == 401
    assert anonymous_telegram_sessions_response.status_code == 401
    assert anonymous_detail_response.status_code == 401
    assert anonymous_override_response.status_code == 401
    assert anonymous_seo_pages_response.status_code == 401
    assert anonymous_seo_edit_response.status_code == 401
    assert anonymous_seo_regenerate_response.status_code == 401
    assert forbidden_session_response.status_code == 403
    assert forbidden_detail_response.status_code == 403
    assert forbidden_override_response.status_code == 403
    assert forbidden_delete_response.status_code == 403
    assert forbidden_merge_response.status_code == 403
    assert forbidden_template_create_response.status_code == 403
    assert forbidden_source_mark_dead_response.status_code == 403
    assert forbidden_seo_pages_response.status_code == 403
    assert forbidden_seo_edit_response.status_code == 403
    assert forbidden_seo_regenerate_response.status_code == 403
    assert forbidden_telegram_session_create_response.status_code == 403
    assert forbidden_telegram_channels_response.status_code == 403
    assert forbidden_reports_response.status_code == 403
    assert forbidden_blocked_hashes_response.status_code == 403
    assert forbidden_session_response.json()["code"] == "admin_required"
    assert forbidden_reports_response.json()["code"] == "admin_required"
    assert forbidden_blocked_hashes_response.json()["code"] == "admin_required"
    assert operator_session_response.status_code == 401
    assert operator_detail_response.status_code == 401
    assert operator_delete_response.status_code == 401
    assert operator_seo_pages_response.status_code == 401
    assert operator_telegram_sessions_response.status_code == 401


async def test_admin_can_approve_channel_suggestion_through_cookie_session(
    auth_app: FastAPI,
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


async def test_admin_can_list_read_and_resolve_moderation_report_with_audited_decision(
    auth_app: FastAPI,
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
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        report = ModerationReport(
            meme=meme,
            reporter_user_id=reporter.id,
            reason=ModerationReason.NSFW,
            note="This should be marked nsfw",
        )
        await _persist_canonical_meme(session, meme, meme_file)
        session.add(report)
        await session.commit()
        await session.refresh(report)
        report_id = report.id
        meme_id = meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        list_response = await admin_client.get("/api/v1/admin/moderation-reports")
        detail_response = await admin_client.get(f"/api/v1/admin/memes/{meme_id}")
        resolve_response = await admin_client.post(
            f"/api/v1/admin/moderation-reports/{report_id}/resolve",
            json={"action": "mark_nsfw", "reason": "nsfw", "note": "Confirmed by moderator"},
        )
        history_response = await admin_client.get(f"/api/v1/admin/moderation-decisions?meme_id={meme_id}")

    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [str(report_id)]
    assert list_response.json()[0]["meme"]["id"] == str(meme_id)

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["meme"]["id"] == str(meme_id)
    assert [item["id"] for item in detail_payload["reports"]] == [str(report_id)]
    assert detail_payload["decisions"] == []

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
    assert history_payload[0]["previous_template_id"] is None
    assert history_payload[0]["new_template_id"] is None

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


async def test_admin_direct_meme_override_persists_template_and_decision_audit_records(
    auth_app: FastAPI,
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
        template = MemeTemplate(slug="new-template", name="New Template")
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        session.add(template)
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        await session.refresh(template)
        await session.refresh(meme)
        template_id = template.id
        meme_id = meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        override_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={
                "is_public": False,
                "is_nsfw": True,
                "template_id": str(template_id),
                "reason": "spam",
                "note": "Manual override from admin screen",
            },
        )
        detail_response = await admin_client.get(f"/api/v1/admin/memes/{meme_id}")

    assert override_response.status_code == 200
    payload = override_response.json()
    assert payload["is_public"] is False
    assert payload["is_nsfw"] is True
    assert payload["template_id"] == str(template_id)

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    decision_actions = {decision["action"] for decision in detail_payload["decisions"]}
    assert decision_actions == {"template_override", "override_flags"}

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        persisted_decisions = (
            await session.execute(
                select(ModerationDecision)
                .where(ModerationDecision.meme_id == meme_id)
                .order_by(ModerationDecision.created_at.asc()),
            )
        ).scalars().all()

        assert persisted_meme is not None
        assert persisted_meme.is_public is False
        assert persisted_meme.is_nsfw is True
        assert persisted_meme.template_id == template_id
        decisions_by_action = {decision.action: decision for decision in persisted_decisions}
        assert set(decisions_by_action) == {ModerationAction.OVERRIDE_FLAGS, ModerationAction.TEMPLATE_OVERRIDE}
        flag_decision = decisions_by_action[ModerationAction.OVERRIDE_FLAGS]
        template_decision = decisions_by_action[ModerationAction.TEMPLATE_OVERRIDE]
        assert flag_decision.report_id is None
        assert flag_decision.admin_user_id == admin_id
        assert flag_decision.reason is ModerationReason.SPAM
        assert flag_decision.previous_is_public is True
        assert flag_decision.previous_is_nsfw is False
        assert flag_decision.new_is_public is False
        assert flag_decision.new_is_nsfw is True
        assert flag_decision.previous_template_id is None
        assert flag_decision.new_template_id == template_id
        assert template_decision.previous_template_id is None
        assert template_decision.new_template_id == template_id


async def test_admin_template_create_rejects_duplicate_slug(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-create-template@example.com",
        is_admin=True,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post(
            "/api/v1/admin/meme-templates",
            json={"slug": "launch-template", "name": "Launch Template", "is_curated": True},
        )
        duplicate_response = await admin_client.post(
            "/api/v1/admin/meme-templates",
            json={"slug": "launch-template", "name": "Duplicate Launch Template"},
        )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["slug"] == "launch-template"
    assert payload["name"] == "Launch Template"
    assert payload["is_curated"] is True
    assert duplicate_response.status_code == 409
    assert "slug" in duplicate_response.json()["detail"]


async def test_admin_can_manage_blocked_perceptual_hashes_with_audit_and_safe_delete(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-blocked-phash@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (
            await session.execute(select(User).where(User.email == "admin-blocked-phash@example.com"))
        ).scalar_one()
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        invalid_response = await admin_client.post(
            "/api/v1/admin/blocked-perceptual-hashes",
            json={"perceptual_hash": "not-hex", "reason": "spam"},
        )
        create_response = await admin_client.post(
            "/api/v1/admin/blocked-perceptual-hashes",
            json={
                "perceptual_hash": "ABCDEF1234567890",
                "max_hamming_distance": 2,
                "reason": "spam",
                "note": "seed ban",
            },
        )
        duplicate_response = await admin_client.post(
            "/api/v1/admin/blocked-perceptual-hashes",
            json={"perceptual_hash": "abcdef1234567890", "reason": "spam"},
        )
        blocked_hash_id = create_response.json()["id"]
        update_response = await admin_client.patch(
            f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}",
            json={
                "perceptual_hash": "abcdef1234567891",
                "max_hamming_distance": 3,
                "reason": "copyright",
                "note": "tightened pattern",
                "is_active": True,
            },
        )
        list_response = await admin_client.get("/api/v1/admin/blocked-perceptual-hashes?is_active=true")
        deactivate_response = await admin_client.post(
            f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}/deactivate",
            json={"note": "temporary pause"},
        )
        audit_response = await admin_client.get(
            f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}/audit-log",
        )
        delete_response = await admin_client.delete(f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}")

    assert invalid_response.status_code == 422
    assert create_response.status_code == 201
    create_payload = create_response.json()
    assert create_payload["perceptual_hash"] == "abcdef1234567890"
    assert create_payload["hash_algorithm"] == "phash"
    assert create_payload["hash_size"] == 64
    assert create_payload["created_by_admin_user_id"] == str(admin_id)
    assert duplicate_response.status_code == 409
    assert update_response.status_code == 200
    assert update_response.json()["perceptual_hash"] == "abcdef1234567891"
    assert update_response.json()["max_hamming_distance"] == 3
    assert [item["id"] for item in list_response.json()] == [blocked_hash_id]
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["action"] == "deactivate"
    assert audit_response.status_code == 200
    assert [item["action"] for item in audit_response.json()] == ["deactivate", "update", "create"]
    assert delete_response.status_code == 200
    assert delete_response.json()["action"] == "delete"

    async with postgres_session_factory() as session:
        blocked_hash_uuid = UUID(blocked_hash_id)
        deleted = await session.get(BlockedPerceptualHash, blocked_hash_uuid)
        audit_rows = (
            await session.execute(
                select(BlockedPerceptualHashAuditLog)
                .where(BlockedPerceptualHashAuditLog.blocked_perceptual_hash_id == blocked_hash_uuid)
                .order_by(BlockedPerceptualHashAuditLog.created_at.asc()),
            )
        ).scalars().all()

    assert deleted is None
    assert [row.action for row in audit_rows] == ["create", "update", "deactivate", "delete"]
    assert {row.admin_user_id for row in audit_rows} == {admin_id}


async def test_admin_template_merge_reassigns_memes_and_writes_template_override_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-merge-template@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (
            await session.execute(select(User).where(User.email == "admin-merge-template@example.com"))
        ).scalar_one()
        source_template = MemeTemplate(slug="duplicate-template", name="Duplicate Template")
        target_template = MemeTemplate(slug="canonical-template", name="Canonical Template")
        first_meme, first_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            template=source_template,
        )
        second_meme, second_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=False,
            is_nsfw=True,
            template=source_template,
        )
        session.add_all([source_template, target_template])
        await _persist_canonical_meme(session, first_meme, first_file)
        await _persist_canonical_meme(session, second_meme, second_file)
        await session.commit()
        source_template_id = source_template.id
        target_template_id = target_template.id
        first_meme_id = first_meme.id
        second_meme_id = second_meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/meme-templates/{source_template_id}/merge",
            json={
                "target_template_id": str(target_template_id),
                "confirmation": str(source_template_id),
                "note": "Canonical template selected by content ops",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "merge"
    assert payload["source_template_id"] == str(source_template_id)
    assert payload["target_template_id"] == str(target_template_id)
    assert payload["affected_meme_count"] == 2

    async with postgres_session_factory() as session:
        deleted_source = await session.get(MemeTemplate, source_template_id)
        persisted_first_meme = await session.get(Meme, first_meme_id)
        persisted_second_meme = await session.get(Meme, second_meme_id)
        decisions = (
            await session.execute(
                select(ModerationDecision)
                .where(ModerationDecision.meme_id.in_([first_meme_id, second_meme_id]))
                .order_by(ModerationDecision.created_at.asc()),
            )
        ).scalars().all()

        assert deleted_source is None
        assert persisted_first_meme is not None
        assert persisted_second_meme is not None
        assert persisted_first_meme.template_id == target_template_id
        assert persisted_second_meme.template_id == target_template_id
        assert len(decisions) == 2
        assert {decision.action for decision in decisions} == {ModerationAction.TEMPLATE_OVERRIDE}
        assert {decision.admin_user_id for decision in decisions} == {admin_id}
        assert {decision.new_template_id for decision in decisions} == {target_template_id}
        assert all("Canonical template selected" in (decision.note or "") for decision in decisions)


async def test_admin_template_delete_only_allows_unreferenced_templates(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-template@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        referenced_template = MemeTemplate(slug="referenced-template", name="Referenced Template")
        unreferenced_template = MemeTemplate(slug="unreferenced-template", name="Unreferenced Template")
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            template=referenced_template,
        )
        session.add_all([referenced_template, unreferenced_template])
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        referenced_template_id = referenced_template.id
        unreferenced_template_id = unreferenced_template.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        blocked_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/meme-templates/{referenced_template_id}",
            json={"confirmation": str(referenced_template_id), "note": "try referenced delete"},
        )
        deleted_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/meme-templates/{unreferenced_template_id}",
            json={"confirmation": str(unreferenced_template_id), "note": "safe cleanup"},
        )

    assert blocked_response.status_code == 409
    assert "referenced by memes" in blocked_response.json()["detail"]
    assert deleted_response.status_code == 200
    assert deleted_response.json()["action"] == "delete"

    async with postgres_session_factory() as session:
        persisted_referenced_template = await session.get(MemeTemplate, referenced_template_id)
        deleted_unreferenced_template = await session.get(MemeTemplate, unreferenced_template_id)
        assert persisted_referenced_template is not None
        assert deleted_unreferenced_template is None


async def test_admin_manual_seo_edit_creates_updates_and_rejects_slug_conflict(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-manual-seo@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            tags=["original"],
        )
        conflict_meme, conflict_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
        )
        await _persist_canonical_meme(session, meme, meme_file)
        await _persist_canonical_meme(session, conflict_meme, conflict_file)
        session.add(
            MemeSeoPage(
                meme=conflict_meme,
                slug="taken-slug",
                page_title="Taken slug",
                meta_description="Taken slug",
                alt_text="Taken slug",
                tags=["taken"],
                model_id="test-model",
                prompt_version="test-version",
            ),
        )
        await session.commit()
        meme_id = meme.id
        conflict_meme_id = conflict_meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={
                "slug": " Launch Slug! ",
                "title": " Launch Title ",
                "meta": " Launch meta description ",
                "alt": " Launch alt text ",
                "caption": " Launch caption ",
                "body": " Launch body text ",
                "tags": "Funny, FUNNY, Reaction Tag",
            },
        )
        list_response = await admin_client.get("/api/v1/admin/seo-pages?limit=10")
        update_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"caption": "Updated caption", "tags": ["Reaction Tag", "new tag", "reaction tag"]},
        )
        conflict_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"slug": "taken-slug"},
        )

    assert create_response.status_code == 200
    create_payload = create_response.json()
    assert create_payload["slug"] == "launch-slug"
    assert create_payload["page_title"] == "Launch Title"
    assert create_payload["meta_description"] == "Launch meta description"
    assert create_payload["alt_text"] == "Launch alt text"
    assert create_payload["caption"] == "Launch caption"
    assert create_payload["body_text"] == "Launch body text"
    assert create_payload["tags"] == ["funny", "reaction-tag"]
    assert create_payload["model_id"] == "admin-manual"
    assert create_payload["prompt_version"] == "admin-manual"
    assert create_payload["generated_at"] is not None
    assert create_payload["edited_at"] is not None

    assert list_response.status_code == 200
    review_rows = {item["meme"]["id"]: item for item in list_response.json()}
    assert review_rows[str(meme_id)]["status"] == "edited"
    assert review_rows[str(meme_id)]["seo_page"]["slug"] == "launch-slug"
    assert review_rows[str(meme_id)]["meme"]["popularity_score"] == 0.0
    assert review_rows[str(conflict_meme_id)]["status"] == "generated"

    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["slug"] == "launch-slug"
    assert update_payload["caption"] == "Updated caption"
    assert update_payload["tags"] == ["reaction-tag", "new-tag"]
    assert update_payload["edited_at"] is not None
    assert conflict_response.status_code == 409
    assert "slug" in conflict_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_page = await session.get(MemeSeoPage, meme_id)
        persisted_meme = await session.get(Meme, meme_id)
        assert persisted_page is not None
        assert persisted_page.slug == "launch-slug"
        assert persisted_page.caption == "Updated caption"
        assert persisted_page.tags == ["reaction-tag", "new-tag"]
        assert persisted_page.edited_at is not None
        assert persisted_meme is not None
        assert persisted_meme.tags == ["reaction-tag", "new-tag"]


async def test_admin_seo_regenerate_uses_static_provider_and_clears_edited_at(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-regenerate-seo@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            tags=["regen tag"],
            ocr_text="Regenerate this meme text",
        )
        await _persist_canonical_meme(session, meme, meme_file)
        session.add(
            MemeSeoPage(
                meme=meme,
                slug="manual-regenerate",
                page_title="Manual title",
                meta_description="Manual meta",
                alt_text="Manual alt",
                caption="Manual caption",
                body_text="Manual body",
                tags=["manual"],
                model_id="admin-manual",
                prompt_version="admin-manual",
                edited_at=datetime.now(UTC) - timedelta(days=1),
            ),
        )
        await session.commit()
        meme_id = meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        wrong_confirmation_response = await admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": "wrong-id"},
        )
        regenerate_response = await admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": str(meme_id)},
        )

    assert wrong_confirmation_response.status_code == 409
    assert "confirmation" in wrong_confirmation_response.json()["detail"]
    assert regenerate_response.status_code == 200
    payload = regenerate_response.json()
    assert payload["slug"] == "regen-tag"
    assert payload["page_title"] == "Regen Tag meme"
    assert payload["model_id"] == "static-local"
    assert payload["prompt_version"] == "meme-seo-v1"
    assert payload["edited_at"] is None

    async with postgres_session_factory() as session:
        persisted_page = await session.get(MemeSeoPage, meme_id)
        persisted_meme = await session.get(Meme, meme_id)
        assert persisted_page is not None
        assert persisted_page.model_id == "static-local"
        assert persisted_page.edited_at is None
        assert persisted_page.tags == ["regen-tag"]
        assert persisted_meme is not None
        assert persisted_meme.tags == ["regen-tag"]


async def test_admin_source_channel_mark_dead_requires_exact_confirmation_without_mutation(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-source-confirmation@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-confirmation",
            username="source_confirmation",
            title="Source Confirmation",
        )
        session.add(channel)
        await session.commit()
        channel_id = channel.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        missing_body_response = await admin_client.post(f"/api/v1/admin/source-channels/{channel_id}/mark-dead")
        wrong_confirmation_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{channel_id}/mark-dead",
            json={"confirmation": "wrong-id"},
        )

    assert missing_body_response.status_code == 422
    assert wrong_confirmation_response.status_code == 409
    assert "confirmation" in wrong_confirmation_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_channel = await session.get(SourceChannel, channel_id)
        assert persisted_channel is not None
        assert persisted_channel.is_active is True
        assert persisted_channel.is_paused is False


async def test_admin_source_channel_health_and_mark_dead_conflicts(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_email = "admin-source-health@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        stale_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-health-stale",
            username="source_health_stale",
            title="Source Health Stale",
            last_read_post_id="42",
            last_fetched_at=datetime.now(UTC) - timedelta(days=2),
        )
        checkpoint_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-health-checkpoint",
            username="source_health_checkpoint",
            title="Source Health Checkpoint",
            last_read_post_id="43",
        )
        session.add_all([stale_channel, checkpoint_channel])
        await session.commit()
        admin_user_id = admin_user.id
        stale_channel_id = stale_channel.id
        checkpoint_channel_id = checkpoint_channel.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        list_response = await admin_client.get("/api/v1/admin/source-channels")
        mark_dead_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{stale_channel_id}/mark-dead",
            json={"confirmation": str(stale_channel_id)},
        )
        resume_dead_response = await admin_client.post(f"/api/v1/admin/source-channels/{stale_channel_id}/resume")
        mark_dead_again_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{stale_channel_id}/mark-dead",
            json={"confirmation": str(stale_channel_id)},
        )

    assert list_response.status_code == 200
    channels = {item["id"]: item for item in list_response.json()}
    assert channels[str(stale_channel_id)]["operational_status"] == "active"
    assert channels[str(stale_channel_id)]["freshness_status"] == "stale"
    assert channels[str(stale_channel_id)]["seconds_since_last_fetch"] >= 2 * 24 * 60 * 60 - 10
    assert channels[str(checkpoint_channel_id)]["freshness_status"] == "checkpoint_only"

    assert mark_dead_response.status_code == 200
    mark_dead_payload = mark_dead_response.json()
    assert mark_dead_payload["is_active"] is False
    assert mark_dead_payload["is_paused"] is True
    assert mark_dead_payload["operational_status"] == "inactive"
    assert resume_dead_response.status_code == 409
    assert "marked dead" in resume_dead_response.json()["detail"]
    assert mark_dead_again_response.status_code == 409
    assert "already marked dead" in mark_dead_again_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_stale_channel = await session.get(SourceChannel, stale_channel_id)
        audit_row = (
            await session.execute(
                select(TelegramAdminAuditLog).where(
                    TelegramAdminAuditLog.source_channel_id == stale_channel_id,
                    TelegramAdminAuditLog.action == "channel_mark_dead",
                ),
            )
        ).scalar_one()
        assert persisted_stale_channel is not None
        assert persisted_stale_channel.is_active is False
        assert persisted_stale_channel.is_paused is True
        assert audit_row.admin_user_id == admin_user_id
        assert audit_row.previous_values["is_active"] is True
        assert audit_row.new_values["is_active"] is False


async def test_admin_create_source_channel_uses_telegram_session_id_and_rejects_unknown_target(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-source-create@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        telegram_session = TelegramSession(
            name="session-a",
            display_name="Session A",
            status=TelegramSessionStatus.ACTIVE,
        )
        session.add(telegram_session)
        await session.commit()
        telegram_session_id = telegram_session.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        created_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "admin-created-source",
                "title": "Admin Created Source",
                "telegram_session_id": str(telegram_session_id),
                "live_enabled": False,
                "engagement_enabled": False,
            },
        )
        unknown_session_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "admin-created-source-unknown",
                "title": "Unknown Session Source",
                "telegram_session_id": str(uuid7()),
            },
        )

    assert created_response.status_code == 201
    payload = created_response.json()
    assert payload["telegram_session_id"] == str(telegram_session_id)
    assert payload["telegram_session_name"] == "session-a"
    assert payload["is_orphaned"] is False
    assert payload["live_enabled"] is False
    assert payload["engagement_enabled"] is False
    assert unknown_session_response.status_code == 404
    assert "Telegram session" in unknown_session_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted = await session.scalar(
            select(SourceChannel).where(SourceChannel.platform_id == "admin-created-source"),
        )
        assert persisted is not None
        assert persisted.telegram_session_id == telegram_session_id


async def test_admin_telegram_session_lifecycle_validates_without_leaking_string_session(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-session@example.com",
        is_admin=True,
    )
    raw_string_session = "raw-string-session-secret-for-admin-test"
    checked_channel_references: list[str | None] = []

    async def fake_validate_admin_telegram_string_session(
        *,
        settings: object,
        string_session: SecretStr,
        channel_reference: str | None = None,
    ) -> admin_service_module.AdminTelegramValidationResult:
        _ = settings
        assert hasattr(string_session, "get_secret_value")
        assert string_session.get_secret_value() == raw_string_session
        checked_channel_references.append(channel_reference)
        return admin_service_module.AdminTelegramValidationResult(
            account=admin_service_module.AdminTelegramAccountProjection(
                user_id=777000,
                username="validated_admin_session",
                phone_hint="ending-7000",
            ),
            channel_reference=channel_reference,
        )

    monkeypatch.setattr(
        admin_service_module,
        "validate_admin_telegram_string_session",
        fake_validate_admin_telegram_string_session,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={
                "name": "primary-admin-session",
                "display_name": "Primary Admin Session",
                "string_session": raw_string_session,
                "account_user_id": 12345,
                "account_username": "seeded_username",
                "account_phone_hint": "ending-2345",
                "max_requests_per_second": 2.5,
            },
        )
        create_auth_required_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={"name": "auth-required-session", "display_name": "Auth Required Session"},
        )
        session_id = create_response.json()["id"]
        patch_response = await admin_client.patch(
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={
                "status": "quarantined",
                "enabled": False,
                "last_error_class": "ManualPark",
                "last_error_text": "Parked by test admin",
                "note": "park for maintenance",
            },
        )
        clear_response = await admin_client.patch(
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={"status": "active", "enabled": True, "clear_error": True},
        )
        channel_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={
                "platform": "telegram",
                "platform_id": "validated-channel-id",
                "username": "validated_channel",
                "title": "Validated Channel",
                "telegram_session_id": session_id,
            },
        )
        validate_response = await admin_client.post(
            f"/api/v1/admin/telegram/sessions/{session_id}/validate",
            json={"source_channel_id": channel_response.json()["id"]},
        )
        list_response = await admin_client.get("/api/v1/admin/telegram/sessions")

    assert create_response.status_code == 201
    create_payload = create_response.json()
    assert create_payload["status"] == "active"
    assert create_payload["has_string_session"] is True
    assert create_payload["account_user_id"] == 12345
    assert create_payload["owned_channel_count"] == 0
    assert raw_string_session not in create_response.text
    assert "encrypted_string_session" not in create_response.text

    assert create_auth_required_response.status_code == 201
    auth_required_payload = create_auth_required_response.json()
    assert auth_required_payload["status"] == "auth_required"
    assert auth_required_payload["has_string_session"] is False

    assert patch_response.status_code == 200
    patch_payload = patch_response.json()
    assert patch_payload["status"] == "quarantined"
    assert patch_payload["enabled"] is False
    assert patch_payload["last_error_class"] == "ManualPark"
    assert patch_payload["quarantined_at"] is not None
    assert raw_string_session not in patch_response.text

    assert clear_response.status_code == 200
    assert clear_response.json()["status"] == "active"
    assert clear_response.json()["last_error_class"] is None
    assert clear_response.json()["last_error_text"] is None

    assert channel_response.status_code == 201
    assert channel_response.json()["is_indexable"] is True
    assert validate_response.status_code == 200
    validate_payload = validate_response.json()
    assert validate_payload["channel_checked"] is True
    assert validate_payload["channel_reference"] == "@validated_channel"
    assert validate_payload["telegram_session"]["account_user_id"] == 777000
    assert validate_payload["telegram_session"]["account_username"] == "validated_admin_session"
    assert raw_string_session not in validate_response.text
    assert checked_channel_references == ["@validated_channel"]

    assert list_response.status_code == 200
    list_payload = {item["name"]: item for item in list_response.json()}
    assert list_payload["primary-admin-session"]["owned_channel_count"] == 1
    assert list_payload["primary-admin-session"]["has_string_session"] is True
    assert list_payload["auth-required-session"]["has_string_session"] is False
    assert raw_string_session not in list_response.text
    assert "encrypted_string_session" not in list_response.text

    async with postgres_session_factory() as session:
        persisted = await session.get(TelegramSession, UUID(session_id))
        audit_rows = (
            await session.execute(
                select(TelegramAdminAuditLog).order_by(TelegramAdminAuditLog.created_at.asc()),
            )
        ).scalars().all()

    assert persisted is not None
    assert persisted.encrypted_string_session is not None
    assert persisted.encrypted_string_session != raw_string_session
    assert persisted.account_user_id == 777000
    assert persisted.account_username == "validated_admin_session"
    assert [row.action for row in audit_rows if row.telegram_session_id == persisted.id] == [
        "session_create",
        "session_patch",
        "session_patch",
        "channel_create",
    ]
    for row in audit_rows:
        audit_text = f"{row.previous_values} {row.new_values}"
        assert raw_string_session not in audit_text
        assert "encrypted_string_session" not in audit_text


async def test_admin_telegram_channel_assignment_orphan_filters_and_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-channel@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        telegram_session = TelegramSession(
            name="assignment-session",
            display_name="Assignment Session",
            status=TelegramSessionStatus.ACTIVE,
        )
        session.add(telegram_session)
        await session.commit()
        telegram_session_id = telegram_session.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        missing_target_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={"platform": "telegram", "platform_id": "missing-target", "title": "Missing Target"},
        )
        unknown_target_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={
                "platform": "telegram",
                "platform_id": "unknown-target",
                "title": "Unknown Target",
                "telegram_session_id": str(uuid7()),
            },
        )
        orphan_create_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={
                "platform": "telegram",
                "platform_id": "orphan-create",
                "title": "Orphan Create",
                "orphaned": True,
                "catchup_enabled": True,
                "live_enabled": True,
                "engagement_enabled": True,
            },
        )
        channel_id = orphan_create_response.json()["id"]
        enable_orphan_response = await admin_client.patch(
            f"/api/v1/admin/telegram/channels/{channel_id}",
            json={"catchup_enabled": True},
        )
        assign_unknown_response = await admin_client.post(
            f"/api/v1/admin/telegram/channels/{channel_id}/assign",
            json={"telegram_session_id": str(uuid7())},
        )
        assign_response = await admin_client.post(
            f"/api/v1/admin/telegram/channels/{channel_id}/assign",
            json={"telegram_session_id": str(telegram_session_id), "note": "move to live session"},
        )
        update_response = await admin_client.patch(
            f"/api/v1/admin/telegram/channels/{channel_id}",
            json={
                "catchup_enabled": True,
                "live_enabled": True,
                "engagement_enabled": True,
                "catchup_message_limit": 123,
            },
        )
        by_session_response = await admin_client.get(
            f"/api/v1/admin/telegram/channels?telegram_session_id={telegram_session_id}",
        )
        grouped_response = await admin_client.get("/api/v1/admin/telegram/channels/grouped")
        orphan_response = await admin_client.post(
            f"/api/v1/admin/telegram/channels/{channel_id}/orphan",
            json={"note": "explicit orphan"},
        )
        orphaned_list_response = await admin_client.get("/api/v1/admin/telegram/channels?orphaned=true")

    assert missing_target_response.status_code == 409
    assert "telegram_session_id or orphaned=true" in missing_target_response.json()["detail"]
    assert unknown_target_response.status_code == 404

    assert orphan_create_response.status_code == 201
    orphan_payload = orphan_create_response.json()
    assert orphan_payload["telegram_session_id"] is None
    assert orphan_payload["is_orphaned"] is True
    assert orphan_payload["is_indexable"] is False
    assert orphan_payload["catchup_enabled"] is False
    assert orphan_payload["live_enabled"] is False
    assert orphan_payload["engagement_enabled"] is False

    assert enable_orphan_response.status_code == 409
    assert "Orphaned source channels" in enable_orphan_response.json()["detail"]
    assert assign_unknown_response.status_code == 404

    assert assign_response.status_code == 200
    assert assign_response.json()["telegram_session_id"] == str(telegram_session_id)
    assert assign_response.json()["is_orphaned"] is False
    assert assign_response.json()["is_indexable"] is False

    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["catchup_message_limit"] == 123
    assert update_payload["is_indexable"] is True

    assert by_session_response.status_code == 200
    assert [item["id"] for item in by_session_response.json()] == [channel_id]

    assert grouped_response.status_code == 200
    session_groups = {group["telegram_session"]["id"] for group in grouped_response.json() if group["telegram_session"]}
    assert str(telegram_session_id) in session_groups

    assert orphan_response.status_code == 200
    orphan_after_assign_payload = orphan_response.json()
    assert orphan_after_assign_payload["telegram_session_id"] is None
    assert orphan_after_assign_payload["catchup_enabled"] is False
    assert orphan_after_assign_payload["live_enabled"] is False
    assert orphan_after_assign_payload["engagement_enabled"] is False
    assert orphan_after_assign_payload["is_indexable"] is False

    assert orphaned_list_response.status_code == 200
    assert channel_id in {item["id"] for item in orphaned_list_response.json()}

    async with postgres_session_factory() as session:
        persisted = await session.get(SourceChannel, UUID(channel_id))
        audit_actions = (
            await session.execute(
                select(TelegramAdminAuditLog.action)
                .where(TelegramAdminAuditLog.source_channel_id == UUID(channel_id))
                .order_by(TelegramAdminAuditLog.created_at.asc()),
            )
        ).scalars().all()

    assert persisted is not None
    assert persisted.telegram_session_id is None
    assert persisted.catchup_enabled is False
    assert persisted.live_enabled is False
    assert persisted.engagement_enabled is False
    assert audit_actions == ["channel_create", "channel_assign", "channel_update", "channel_orphan"]


async def test_admin_delete_telegram_session_orphans_channels_and_audits_delete(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-telegram-session@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        telegram_session = TelegramSession(
            name="delete-session",
            display_name="Delete Session",
            status=TelegramSessionStatus.ACTIVE,
            encrypted_string_session="encrypted-not-raw",
        )
        keep_session = TelegramSession(
            name="keep-session",
            display_name="Keep Session",
            status=TelegramSessionStatus.ACTIVE,
        )
        first_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="delete-session-first",
            title="Delete Session First",
            telegram_session=telegram_session,
            catchup_enabled=True,
            live_enabled=True,
            engagement_enabled=True,
        )
        second_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="delete-session-second",
            title="Delete Session Second",
            telegram_session=telegram_session,
            catchup_enabled=True,
            live_enabled=True,
            engagement_enabled=True,
        )
        keep_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="keep-session-channel",
            title="Keep Session Channel",
            telegram_session=keep_session,
        )
        session.add_all([telegram_session, keep_session, first_channel, second_channel, keep_channel])
        await session.commit()
        session_id = telegram_session.id
        first_channel_id = first_channel.id
        second_channel_id = second_channel.id
        keep_channel_id = keep_channel.id
        keep_session_id = keep_session.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        bad_confirmation_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={"confirmation": str(uuid7()), "note": "try wrong delete"},
        )
        delete_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={"confirmation": str(session_id), "note": "retire account"},
        )

    assert bad_confirmation_response.status_code == 409
    assert delete_response.status_code == 200
    assert delete_response.json()["orphaned_source_channel_count"] == 2

    async with postgres_session_factory() as session:
        deleted_session = await session.get(TelegramSession, session_id)
        first_channel = await session.get(SourceChannel, first_channel_id)
        second_channel = await session.get(SourceChannel, second_channel_id)
        keep_channel = await session.get(SourceChannel, keep_channel_id)
        audit_rows = (
            await session.execute(
                select(TelegramAdminAuditLog)
                .where(TelegramAdminAuditLog.telegram_session_id == session_id)
                .order_by(TelegramAdminAuditLog.created_at.asc()),
            )
        ).scalars().all()

    assert deleted_session is None
    assert first_channel is not None
    assert second_channel is not None
    for channel in (first_channel, second_channel):
        assert channel.telegram_session_id is None
        assert channel.catchup_enabled is False
        assert channel.live_enabled is False
        assert channel.engagement_enabled is False
    assert keep_channel is not None
    assert keep_channel.telegram_session_id == keep_session_id
    assert [row.action for row in audit_rows] == ["channel_orphan", "channel_orphan", "session_delete"]
    delete_audit = audit_rows[-1]
    assert delete_audit.note == "retire account"
    assert "encrypted_string_session" not in str(delete_audit.previous_values)


async def test_admin_can_delete_meme_with_durable_destructive_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-meme@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (await session.execute(select(User).where(User.email == "admin-delete-meme@example.com"))).scalar_one()
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            like_count=7,
            file_key="admin/delete/original.jpg",
        )
        await _persist_canonical_meme(session, meme, meme_file)
        collection = Collection(owner_id=admin.id, title="Admin saved memes")
        report = ModerationReport(meme=meme, reporter_user_id=admin.id, reason=ModerationReason.SPAM)
        decision = ModerationDecision(
            meme=meme,
            report=report,
            admin_user_id=admin.id,
            action=ModerationAction.HIDE,
            reason=ModerationReason.SPAM,
            note="Prior hide",
            previous_is_public=True,
            previous_is_nsfw=False,
            new_is_public=False,
            new_is_nsfw=False,
        )
        seo_page = MemeSeoPage(
            meme=meme,
            slug="admin-delete-test",
            page_title="Delete test",
            meta_description="Delete test",
            alt_text="Delete test",
            tags=["delete"],
            model_id="test-model",
            prompt_version="v1",
        )
        session.add_all([admin, collection, report, decision, seo_page])
        await session.flush()
        session.add_all(
            [
                CollectionMeme(collection=collection, meme=meme, added_by_user_id=admin.id),
                PinnedMeme(user_id=admin.id, meme=meme, position=1),
            ]
        )
        await session.commit()
        meme_id = meme.id
        file_id = meme_file.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            json={"confirmation": str(meme_id), "note": "Unsafe duplicate should be removed"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "delete"
    assert payload["source_meme_id"] == str(meme_id)
    assert payload["target_meme_id"] is None
    assert payload["affected_snapshot"]["meme_files"]["count"] == 1
    assert payload["affected_snapshot"]["seo_page"]["count"] == 1
    assert payload["affected_snapshot"]["collection_saves"]["count"] == 1
    assert payload["affected_snapshot"]["pins"]["count"] == 1
    assert payload["affected_snapshot"]["moderation_reports"]["count"] == 1
    assert payload["affected_snapshot"]["moderation_decisions"]["count"] == 1

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        persisted_file = await session.get(MemeFile, file_id)
        audit_log = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == meme_id),
        )

        assert persisted_meme is None
        assert persisted_file is None
        assert audit_log is not None
        assert audit_log.admin_user_id == admin_id
        assert audit_log.action == "delete"
        assert audit_log.note == "Unsafe duplicate should be removed"
        affected_snapshot = cast("dict[str, dict[str, object]]", audit_log.affected_snapshot)
        assert affected_snapshot["meme_files"]["ids"] == [str(file_id)]


async def test_admin_delete_requires_exact_confirmation_without_partial_delete(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-blocked@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        meme_id = meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            json={"confirmation": "wrong-id", "note": "try delete"},
        )

    assert response.status_code == 409
    assert "confirmation" in response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        audit_count = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == meme_id),
        )

        assert persisted_meme is not None
        assert audit_count is None


async def test_admin_can_merge_meme_with_shared_lineage_transfer_and_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-merge-meme@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (await session.execute(select(User).where(User.email == "admin-merge-meme@example.com"))).scalar_one()
        source_meme, source_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            like_count=2,
            file_key="admin/merge/source.jpg",
            file_quality=0.5,
        )
        target_meme, target_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            like_count=5,
            file_key="admin/merge/target.jpg",
            file_quality=1.0,
        )
        collection = Collection(owner_id=admin.id, title="Merge collection")
        await _persist_canonical_meme(session, source_meme, source_file)
        await _persist_canonical_meme(session, target_meme, target_file)
        session.add(collection)
        await session.flush()
        session.add_all(
            [
                CollectionMeme(collection=collection, meme=source_meme, added_by_user_id=admin.id),
                PinnedMeme(user_id=admin.id, meme=source_meme, position=1),
            ]
        )
        await session.commit()
        source_meme_id = source_meme.id
        target_meme_id = target_meme.id
        source_file_id = source_file.id
        target_file_id = target_file.id
        collection_id = collection.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/memes/{source_meme_id}/merge",
            json={
                "target_meme_id": str(target_meme_id),
                "confirmation": str(source_meme_id),
                "note": "Confirmed duplicate canonical merge",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "merge"
    assert payload["source_meme_id"] == str(source_meme_id)
    assert payload["target_meme_id"] == str(target_meme_id)
    assert payload["affected_snapshot"]["meme_files"]["ids"] == [str(source_file_id)]

    async with postgres_session_factory() as session:
        deleted_source = await session.get(Meme, source_meme_id)
        target = await session.get(Meme, target_meme_id)
        moved_file = await session.get(MemeFile, source_file_id)
        collection_link = await session.get(CollectionMeme, (collection_id, target_meme_id))
        pin_link = await session.get(PinnedMeme, (admin_id, target_meme_id))
        audit_log = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == source_meme_id),
        )
        merge_log = await session.scalar(select(MemeMergeLog).where(MemeMergeLog.source_meme_id == source_meme_id))

        assert deleted_source is None
        assert target is not None
        assert target.like_count == 7
        assert target.primary_file_id == target_file_id
        assert moved_file is not None
        assert moved_file.meme_id == target_meme_id
        assert collection_link is not None
        assert pin_link is not None
        assert audit_log is not None
        assert audit_log.admin_user_id == admin_id
        assert audit_log.action == "merge"
        assert audit_log.note == "Confirmed duplicate canonical merge"
        assert merge_log is not None
        assert merge_log.merge_reason == "admin_destructive_merge"
        assert merge_log.details["admin_user_id"] == str(admin_id)
        assert merge_log.details["admin_note"] == "Confirmed duplicate canonical merge"


async def test_admin_merge_self_is_blocked_without_partial_delete_or_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-merge-blocked@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        meme_id = meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/merge",
            json={"target_meme_id": str(meme_id), "confirmation": str(meme_id), "note": "try self merge"},
        )

    assert response.status_code == 409
    assert "cannot be merged into itself" in response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        audit_log = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == meme_id),
        )

        assert persisted_meme is not None
        assert audit_log is None
