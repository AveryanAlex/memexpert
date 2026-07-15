# ruff: noqa: TC001,TC002
"""Cookie-admin route coverage for failed-work visibility and audited recovery scheduling."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.content import Meme, MemeFile, PipelineStageJournal
from memexpert.models.enums import (
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
)
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
        session.add(stage)
        await session.commit()
        stage_id = stage.id

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
        stage = PipelineStageJournal(
            meme_file_id=file_id,
            stage=ContentPipelineStage.CLASSIFY,
            status=ContentPipelineStageStatus.FAILED,
            attempt_count=5,
            normalized_reason="classify_provider_blocked",
            is_retryable=True,
        )
        session.add(stage)
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
