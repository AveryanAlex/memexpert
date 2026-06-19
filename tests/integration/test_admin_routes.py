# ruff: noqa: TC001,TC002
"""Integration tests for cookie-authenticated browser-admin routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid7

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
)
from memexpert.models.enums import (
    ContentKind,
    ContentProcessingStatus,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    SourcePlatform,
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
        anonymous_detail_response = await anonymous_client.get(f"/api/v1/admin/memes/{meme_id}")
        anonymous_override_response = await anonymous_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={"is_nsfw": True},
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
        )
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

    assert anonymous_session_response.status_code == 401
    assert anonymous_detail_response.status_code == 401
    assert anonymous_override_response.status_code == 401
    assert forbidden_session_response.status_code == 403
    assert forbidden_detail_response.status_code == 403
    assert forbidden_override_response.status_code == 403
    assert forbidden_delete_response.status_code == 403
    assert forbidden_merge_response.status_code == 403
    assert forbidden_template_create_response.status_code == 403
    assert forbidden_source_mark_dead_response.status_code == 403
    assert forbidden_reports_response.status_code == 403
    assert forbidden_blocked_hashes_response.status_code == 403
    assert forbidden_session_response.json()["code"] == "admin_required"
    assert forbidden_reports_response.json()["code"] == "admin_required"
    assert forbidden_blocked_hashes_response.json()["code"] == "admin_required"
    assert operator_session_response.status_code == 401
    assert operator_detail_response.status_code == 401
    assert operator_delete_response.status_code == 401


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


async def test_admin_source_channel_health_and_mark_dead_conflicts(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-source-health@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
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
        stale_channel_id = stale_channel.id
        checkpoint_channel_id = checkpoint_channel.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        list_response = await admin_client.get("/api/v1/admin/source-channels")
        mark_dead_response = await admin_client.post(f"/api/v1/admin/source-channels/{stale_channel_id}/mark-dead")
        resume_dead_response = await admin_client.post(f"/api/v1/admin/source-channels/{stale_channel_id}/resume")
        mark_dead_again_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{stale_channel_id}/mark-dead",
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
        assert persisted_stale_channel is not None
        assert persisted_stale_channel.is_active is False
        assert persisted_stale_channel.is_paused is True


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
