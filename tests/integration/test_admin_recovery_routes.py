# ruff: noqa: TC001,TC002
"""Cookie-admin route coverage for failed-work visibility and audited recovery scheduling."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.content import Meme, MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
)
from memexpert.models.operations import RecoveryJob, RecoveryJobItem
from tests.integration.test_admin_routes import _issue_user_cookie
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME

if TYPE_CHECKING:
    from fastapi import FastAPI


async def test_admin_can_list_retryable_work_and_schedule_idempotent_recovery(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="recovery-admin@example.com",
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        meme_id = uuid.uuid7()
        file_id = uuid.uuid7()
        meme = Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.IMAGE)
        file = MemeFile(
            id=file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.FAILED,
            s3_original_key=f"tests/admin-recovery/{file_id}.jpg",
            mime_type="image/jpeg",
        )
        session.add(meme)
        await session.flush()
        session.add(file)
        await session.flush()
        prerequisite = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.TRANSCODE,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
        )
        stage = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.OCR,
            status=ContentPipelineStageStatus.FAILED,
            attempt_count=5,
            last_event_id=uuid.uuid7(),
            normalized_reason="ocr_provider_blocked",
            last_error_text="OCR provider was unavailable.",
            is_retryable=True,
        )
        blocked_stage = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.CLASSIFY,
            status=ContentPipelineStageStatus.FAILED,
            attempt_count=1,
            normalized_reason="classify_terminal_failure",
            is_retryable=False,
        )
        session.add_all((prerequisite, stage, blocked_stage))
        await session.commit()
        stage_id = stage.id
        blocked_stage_id = blocked_stage.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous:
        assert (await anonymous.get("/api/v1/admin/recovery/summary")).status_code == 401

    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(ACCESS_COOKIE_NAME, token)
        summary = await client.get("/api/v1/admin/recovery/summary")
        assert summary.status_code == 200
        assert summary.json()["retryable_count"] >= 1

        work_page = await client.get(
            "/api/v1/admin/recovery/work",
            params={"kind": "pipeline_stage", "bucket": "retryable"},
        )
        assert work_page.status_code == 200
        work = next(item for item in work_page.json()["items"] if item["id"] == str(stage_id))
        assert work["capabilities"] == ["retry_stage"]
        assert work["error_code"] == "ocr_provider_blocked"
        actions = {action["capability"]: action for action in work["actions"]}
        assert set(actions) == {"retry_stage", "replay_stage"}
        assert actions["retry_stage"]["available"] is True
        assert actions["replay_stage"]["available"] is True

        blocked_page = await client.get(
            "/api/v1/admin/recovery/work",
            params={"kind": "pipeline_stage", "bucket": "blocked"},
        )
        assert blocked_page.status_code == 200
        blocked_work = next(
            item for item in blocked_page.json()["items"] if item["id"] == str(blocked_stage_id)
        )
        blocked_actions = {action["capability"]: action for action in blocked_work["actions"]}
        assert set(blocked_actions) == {"retry_stage", "replay_stage"}
        assert blocked_actions["retry_stage"]["available"] is False
        assert blocked_actions["replay_stage"]["available"] is False
        assert blocked_actions["replay_stage"]["blocked_prerequisites"] == [
            "classify requires a successful embed prerequisite."
        ]

        request_id = str(uuid.uuid7())
        mutation = {
            "request_id": request_id,
            "version": work["version"],
            "reason": "OCR provider connectivity has been restored.",
            "capability": "retry_stage",
        }
        scheduled = await client.post(
            f"/api/v1/admin/recovery/work/pipeline_stage/{stage_id}/retry",
            json=mutation,
        )
        assert scheduled.status_code == 202
        scheduled_body = scheduled.json()
        assert scheduled_body["status"] == "queued"
        assert scheduled_body["total_count"] == 1
        assert scheduled_body["items"][0]["status"] == "queued"

        repeated = await client.post(
            f"/api/v1/admin/recovery/work/pipeline_stage/{stage_id}/retry",
            json=mutation,
        )
        assert repeated.status_code == 202
        assert repeated.json()["id"] == scheduled_body["id"]

        stale = await client.post(
            f"/api/v1/admin/recovery/work/pipeline_stage/{stage_id}/retry",
            json={
                **mutation,
                "request_id": str(uuid.uuid7()),
                "version": "stale-version",
            },
        )
        assert stale.status_code == 409

        job = await client.get(f"/api/v1/admin/recovery/batches/{scheduled_body['id']}")
        assert job.status_code == 200
        assert job.json()["request_id"] == request_id


async def test_meme_processing_media_action_uses_shared_version_and_fences_stale_state(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="processing-media-version-admin@example.com",
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        meme_id = uuid.uuid7()
        file_id = uuid.uuid7()
        session.add(Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.VIDEO))
        await session.flush()
        meme_file = MemeFile(
            id=file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.READY,
            s3_original_key=f"tests/admin-recovery/{file_id}/original-a.webm",
            s3_web_video_key=f"pipeline/derived/{file_id}/generations/active-a/web.mp4",
            mime_type="video/webm",
            web_video_profile="legacy-profile-a",
            source_has_audio=True,
            web_video_has_audio=True,
        )
        session.add(meme_file)
        await session.flush()
        transcode = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.TRANSCODE,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
        )
        terminal_descendant = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.CLASSIFY,
            status=ContentPipelineStageStatus.FAILED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
        )
        session.add_all((transcode, terminal_descendant))
        await session.commit()
        transcode_id = transcode.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(ACCESS_COOKIE_NAME, token)
        detail_response = await client.get(f"/api/v1/admin/memes/{meme_id}")
        assert detail_response.status_code == 200
        processing_file = detail_response.json()["processing_files"][0]
        processing_stage = next(
            stage for stage in processing_file["stages"] if stage["stage"] == "transcode"
        )
        candidate_response = await client.get(
            f"/api/v1/admin/recovery/work/pipeline_stage/{transcode_id}/candidate"
        )
        assert candidate_response.status_code == 200
        candidate = candidate_response.json()
        assert processing_stage["version"] == candidate["work"]["version"]
        assert processing_file["version"] == candidate["work"]["version"]
        assert processing_stage["version"].startswith("media-v1:")
        replay = next(
            action
            for action in processing_stage["actions"]
            if action["capability"] == "replay_stage"
        )
        assert replay["required_acknowledgements"] == []
        assert replay["scope_requirements"]["stage_only"]["required_acknowledgements"] == []
        assert replay["scope_requirements"]["stage_and_dependents"][
            "required_acknowledgements"
        ] == ["terminal_override"]

        scheduled = await client.post(
            f"/api/v1/admin/recovery/work/pipeline_stage/{transcode_id}/actions",
            json={
                "request_id": str(uuid.uuid7()),
                "version": processing_file["version"],
                "reason": "Regenerate the reviewed moving-media derivatives.",
                "action": "regenerate_derivatives",
                "scope": "stage_only",
                "retry_limit": 3,
                "acknowledgements": [],
            },
        )
        assert scheduled.status_code == 202

        async with postgres_session_factory() as session:
            job = await session.get(RecoveryJob, uuid.UUID(scheduled.json()["id"]))
            assert job is not None
            job.status = RecoveryJobStatus.COMPLETED
            items = (
                (
                    await session.execute(
                        select(RecoveryJobItem).where(RecoveryJobItem.recovery_job_id == job.id)
                    )
                )
                .scalars()
                .all()
            )
            for item in items:
                item.status = RecoveryJobItemStatus.SUCCEEDED
                item.reservation_active = False
            persisted_file = await session.get(MemeFile, file_id)
            assert persisted_file is not None
            persisted_file.s3_original_key = f"tests/admin-recovery/{file_id}/original-b.webm"
            persisted_file.s3_web_video_key = (
                f"pipeline/derived/{file_id}/generations/active-b/web.mp4"
            )
            persisted_file.web_video_profile = "legacy-profile-b"
            persisted_file.source_has_audio = False
            await session.commit()

        stale = await client.post(
            f"/api/v1/admin/recovery/work/pipeline_stage/{transcode_id}/actions",
            json={
                "request_id": str(uuid.uuid7()),
                "version": processing_file["version"],
                "reason": "Reject the stale reviewed media pointer and source state.",
                "action": "regenerate_derivatives",
                "scope": "stage_only",
                "retry_limit": 3,
                "acknowledgements": [],
            },
        )
        assert stale.status_code == 409
        assert "changed" in stale.json()["detail"].lower()


async def test_admin_can_start_uncapped_successful_stage_preview(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="successful-stage-preview-admin@example.com",
        is_admin=True,
    )
    request_id = uuid.uuid7()
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(ACCESS_COOKIE_NAME, token)
        response = await client.post(
            "/api/v1/admin/recovery/batches/preview",
            json={
                "request_id": str(request_id),
                "action": "replay_stage",
                "scope": "stage_and_dependents",
                "retry_limit": 5,
                "reason": "Replay the exact successful OCR cohort after review.",
                "selector": {
                    "type": "query",
                    "filters": {"successful_stage": True, "stage": "ocr"},
                },
                "acknowledgements": ["terminal_override"],
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["request_id"] == str(request_id)
    assert body["status"] == "preparing"
    assert body["action"] == "replay_stage"
    assert body["scope"] == "stage_and_dependents"
    assert body["retry_limit"] == 5
    assert body["selection_snapshot_at"] is None
    async with postgres_session_factory() as session:
        job = await session.get(RecoveryJob, uuid.UUID(body["id"]))
        assert job is not None
        selector = cast("dict[str, object]", job.selection["selector"])
        assert selector["filters"] == {
            "bucket": None,
            "kind": None,
            "source_channel_id": None,
            "stage": "ocr",
            "reason": None,
            "query": None,
            "outdated_web_video": False,
            "successful_stage": True,
        }
        assert job.selection["acknowledgements"] == ["terminal_override"]


async def test_admin_can_preview_schedule_and_cancel_bounded_recovery_batch(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="recovery-batch-admin@example.com",
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        meme_id = uuid.uuid7()
        file_id = uuid.uuid7()
        session.add(Meme(id=meme_id, primary_file_id=file_id, media_type=ContentKind.IMAGE))
        await session.flush()
        session.add(
            MemeFile(
                id=file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.FAILED,
                s3_original_key=f"tests/admin-recovery/{file_id}.jpg",
                mime_type="image/jpeg",
            )
        )
        await session.flush()
        prerequisite = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.EMBED,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=uuid.uuid7(),
            is_retryable=False,
        )
        stage = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.CLASSIFY,
            status=ContentPipelineStageStatus.FAILED,
            attempt_count=5,
            normalized_reason="classify_provider_blocked",
            is_retryable=True,
        )
        session.add_all((prerequisite, stage))
        await session.commit()
        stage_id = stage.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(ACCESS_COOKIE_NAME, token)
        detail = await client.get(f"/api/v1/admin/recovery/work/pipeline_stage/{stage_id}")
        assert detail.status_code == 200
        work = detail.json()
        preview = await client.post(
            "/api/v1/admin/recovery/batches/preview",
            json={
                "request_id": str(uuid.uuid7()),
                "capability": "retry_stage",
                "reason": "Preview a bounded classification replay.",
                "items": [{"kind": "pipeline_stage", "id": str(stage_id), "version": work["version"]}],
            },
        )
        assert preview.status_code == 201
        assert preview.json()["status"] == "preview"

        scheduled = await client.post(
            f"/api/v1/admin/recovery/batches/{preview.json()['id']}/schedule",
            json={
                "version": preview.json()["version"],
                "reason": "Proceed after reviewing the one-item preview.",
            },
        )
        assert scheduled.status_code == 202
        assert scheduled.json()["status"] == "queued"

        repeated_schedule = await client.post(
            f"/api/v1/admin/recovery/batches/{preview.json()['id']}/schedule",
            json={
                "version": preview.json()["version"],
                "reason": "Proceed after reviewing the one-item preview.",
            },
        )
        assert repeated_schedule.status_code == 202
        assert repeated_schedule.json()["id"] == scheduled.json()["id"]

        cancelled = await client.post(
            f"/api/v1/admin/recovery/batches/{preview.json()['id']}/cancel",
            json={
                "version": scheduled.json()["version"],
                "reason": "Cancel before the scheduler dispatches it.",
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["items"][0]["status"] == "cancelled"

        repeated_cancel = await client.post(
            f"/api/v1/admin/recovery/batches/{preview.json()['id']}/cancel",
            json={
                "version": scheduled.json()["version"],
                "reason": "Cancel before the scheduler dispatches it.",
            },
        )
        assert repeated_cancel.status_code == 200
        assert repeated_cancel.json()["id"] == cancelled.json()["id"]
