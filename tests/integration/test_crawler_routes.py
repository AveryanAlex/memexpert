"""Integration tests for the T03 operator-facing crawler HTTP routes.

The tests wire the crawler router against a migrated PostgreSQL session
(via :func:`migrated_db_session`), the shared :class:`FakeTelegramClient`
from the crawler contract, and a real
:class:`CrawlerOperationsService` so the HTTP boundary and the service
layer are exercised together. No test in this module imports
:mod:`telethon` — the FakeTelegramClient is the only adapter that
needs to answer the runtime protocol.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from memexpert.api.dependencies.pipeline import (
    PIPELINE_OPERATOR_TOKEN_HEADER_NAME,
    get_content_pipeline_service,
    get_crawler_operations_service,
    get_crawler_runtime,
    get_pipeline_telegram_client,
)
from memexpert.core.config import Settings, get_settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    RawTelegramChannel,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.runtime import TelegramCrawlerRuntime
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeFileSyncTargetSnapshot,
    MemeSource,
    PipelineStageJournal,
    SourceChannel,
    TelegramSessionState,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramSessionStatus,
)
from memexpert.services.content_pipeline import ContentPipelineService
from memexpert.services.crawler_operations import CrawlerOperationsService
from tests.integration.test_content_pipeline_service import (
    FakeMediaProcessor,
    FakeStorageClient,
    RecordingPublisher,
    _make_distinct_upload_media_details,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _build_real_operations_service(
    session: AsyncSession,
    *,
    telegram_client: FakeTelegramClient,
    phash_tag: str = "R",
) -> CrawlerOperationsService:
    pipeline_service = ContentPipelineService(
        session,
        storage_client=FakeStorageClient(),
        publisher=RecordingPublisher(),
        media_processor=FakeMediaProcessor(
            inspect_result=_make_distinct_upload_media_details(tag=phash_tag),
        ),
    )
    runtime = TelegramCrawlerRuntime(
        pipeline_service=pipeline_service,
        telegram_client=telegram_client,
        session=session,
        settings=Settings(),
    )
    return CrawlerOperationsService(session=session, runtime=runtime)


def _install_operations_service_override(
    app: FastAPI,
    service: CrawlerOperationsService,
) -> None:
    """Wire dependency_overrides so the crawler router uses ``service``.

    The router's three DI providers (operations service, runtime, db
    session, pipeline service) all collapse onto the single test-owned
    ``CrawlerOperationsService`` instance, so we short-circuit every
    upstream provider to return the exact collaborator the test
    configured. That keeps the session objects consistent across the
    request and lets us seed rows directly on ``migrated_db_session``.
    """

    app.dependency_overrides[get_crawler_operations_service] = lambda: service
    app.dependency_overrides[get_crawler_runtime] = lambda: service.runtime
    app.dependency_overrides[get_pipeline_telegram_client] = lambda: service.runtime.telegram_client
    app.dependency_overrides[get_content_pipeline_service] = lambda: service.runtime.pipeline_service


async def _seed_session(
    session: AsyncSession,
    *,
    session_name: str,
    status: TelegramSessionStatus = TelegramSessionStatus.ACTIVE,
) -> TelegramSessionState:
    row = TelegramSessionState(
        session_name=session_name,
        status=status,
        last_heartbeat_at=_now(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _seed_channel(
    session: AsyncSession,
    *,
    platform_id: str,
    title: str = "Curated Channel",
    username: str | None = None,
    session_id: str | None = None,
    is_paused: bool = False,
    catchup_enabled: bool = True,
    catchup_message_limit: int = 500,
    last_read_post_id: str | None = None,
) -> SourceChannel:
    row = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id=platform_id,
        username=username,
        title=title,
        is_active=True,
        is_paused=is_paused,
        catchup_enabled=catchup_enabled,
        catchup_message_limit=catchup_message_limit,
        session_id=session_id,
        last_read_post_id=last_read_post_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _operator_headers() -> dict[str, str]:
    return {PIPELINE_OPERATOR_TOKEN_HEADER_NAME: get_settings().pipeline_operator_token.get_secret_value()}


# ---------------------------------------------------------------------------
# Operator-token guard: every route must reject blank/missing tokens.
# ---------------------------------------------------------------------------


async def test_crawler_routes_reject_requests_without_operator_token(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    fake = FakeTelegramClient()
    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=fake,
    )
    _install_operations_service_override(app, operations_service)

    try:
        channel_id = uuid.uuid7()
        unauthorized_routes = [
            ("GET", "/api/v1/crawler/sessions", None),
            ("GET", "/api/v1/crawler/channels", None),
            ("POST", f"/api/v1/crawler/channels/{channel_id}/pause", None),
            ("POST", f"/api/v1/crawler/channels/{channel_id}/resume", None),
            (
                "POST",
                f"/api/v1/crawler/channels/{channel_id}/reassign",
                {"new_session_name": "primary"},
            ),
            (
                "POST",
                f"/api/v1/crawler/channels/{channel_id}/replay-post",
                {"post_id": "42"},
            ),
            ("GET", "/api/v1/crawler/freshness", None),
        ]
        for method, path, body in unauthorized_routes:
            if method == "GET":
                response = await client.get(path)
            else:
                response = await client.post(path, json=body)
            assert response.status_code == 401, path
            assert response.json()["code"] == "invalid_operator_token", path
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Sessions + channels list routes
# ---------------------------------------------------------------------------


async def test_list_sessions_returns_owned_channel_counts(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_session(migrated_db_session, session_name="secondary")
    await _seed_channel(
        migrated_db_session,
        platform_id="chan_a",
        title="Channel A",
        session_id="primary",
    )
    await _seed_channel(
        migrated_db_session,
        platform_id="chan_b",
        title="Channel B",
        session_id="primary",
    )
    await _seed_channel(
        migrated_db_session,
        platform_id="chan_c",
        title="Channel C",
        session_id="secondary",
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.get("/api/v1/crawler/sessions", headers=_operator_headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    sessions = {row["session_name"]: row for row in response.json()}
    assert sessions["primary"]["owned_channel_count"] == 2
    assert sessions["secondary"]["owned_channel_count"] == 1
    assert sessions["primary"]["status"] == "active"


async def test_list_channels_supports_platform_session_and_paused_filters(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_channel(
        migrated_db_session,
        platform_id="active_chan",
        title="Active",
        session_id="primary",
    )
    await _seed_channel(
        migrated_db_session,
        platform_id="paused_chan",
        title="Paused",
        session_id="primary",
        is_paused=True,
    )
    await _seed_channel(
        migrated_db_session,
        platform_id="secondary_chan",
        title="Secondary",
        session_id="secondary",
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        unfiltered = await client.get(
            "/api/v1/crawler/channels",
            headers=_operator_headers(),
        )
        filtered_session = await client.get(
            "/api/v1/crawler/channels",
            headers=_operator_headers(),
            params={"session_name": "primary"},
        )
        active_only = await client.get(
            "/api/v1/crawler/channels",
            headers=_operator_headers(),
            params={"session_name": "primary", "include_paused": "false"},
        )
        wrong_platform = await client.get(
            "/api/v1/crawler/channels",
            headers=_operator_headers(),
            params={"platform": "reddit"},
        )
    finally:
        app.dependency_overrides.clear()

    assert unfiltered.status_code == 200
    assert {row["platform_id"] for row in unfiltered.json()} == {
        "active_chan",
        "paused_chan",
        "secondary_chan",
    }

    assert filtered_session.status_code == 200
    assert {row["platform_id"] for row in filtered_session.json()} == {
        "active_chan",
        "paused_chan",
    }

    assert active_only.status_code == 200
    assert {row["platform_id"] for row in active_only.json()} == {"active_chan"}

    assert wrong_platform.status_code == 200
    assert wrong_platform.json() == []


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


async def test_pause_and_resume_channel_is_idempotent_and_404s_on_unknown_id(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="toggle_chan",
        session_id="primary",
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        first_pause = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/pause",
            headers=_operator_headers(),
        )
        second_pause = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/pause",
            headers=_operator_headers(),
        )
        first_resume = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/resume",
            headers=_operator_headers(),
        )
        second_resume = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/resume",
            headers=_operator_headers(),
        )
        missing = await client.post(
            f"/api/v1/crawler/channels/{uuid.uuid7()}/pause",
            headers=_operator_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert first_pause.status_code == 200
    assert first_pause.json()["is_paused"] is True
    assert second_pause.status_code == 200
    assert second_pause.json()["is_paused"] is True

    assert first_resume.status_code == 200
    assert first_resume.json()["is_paused"] is False
    assert second_resume.status_code == 200
    assert second_resume.json()["is_paused"] is False

    assert missing.status_code == 404
    assert missing.json()["code"] == "crawler_channel_not_found"


# ---------------------------------------------------------------------------
# Reassign
# ---------------------------------------------------------------------------


async def test_reassign_channel_updates_session_and_rejects_unknown_target(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_session(migrated_db_session, session_name="secondary")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="movable_chan",
        session_id="primary",
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        success = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/reassign",
            headers=_operator_headers(),
            json={"new_session_name": "secondary"},
        )
        unknown_target = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/reassign",
            headers=_operator_headers(),
            json={"new_session_name": "does_not_exist"},
        )
    finally:
        app.dependency_overrides.clear()

    assert success.status_code == 200
    assert success.json()["session_id"] == "secondary"

    assert unknown_target.status_code == 409
    assert unknown_target.json()["code"] == "crawler_invalid_session"

    # Confirm the DB row actually updated to the new session binding.
    refreshed = await migrated_db_session.get(SourceChannel, channel.id)
    assert refreshed is not None
    assert refreshed.session_id == "secondary"


# ---------------------------------------------------------------------------
# Replay post
# ---------------------------------------------------------------------------


def _pin_photo_message(
    *,
    fake: FakeTelegramClient,
    channel_id: str,
    post_id: str,
) -> None:
    fake.pin_single_message(
        channel_id=channel_id,
        post_id=post_id,
        message=RawTelegramMessage(
            message_id=post_id,
            channel_id=channel_id,
            channel_username=None,
            channel_title="Replay Channel",
            published_at=_now(),
            media_type="photo",
            views=7,
            reactions={},
            forward=None,
        ),
        media=b"replayed-bytes-" + post_id.encode(),
    )


async def test_replay_channel_post_ingests_pinned_message_without_advancing_checkpoint(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="replay_chan",
        session_id="primary",
        last_read_post_id="500",
    )
    fake = FakeTelegramClient()
    _pin_photo_message(fake=fake, channel_id="replay_chan", post_id="42")

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=fake,
        phash_tag="R",
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/replay-post",
            headers=_operator_headers(),
            json={"post_id": "42"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "ingested"
    assert body["meme_file_id"] is not None
    assert body["received_at"] is not None

    refreshed = await migrated_db_session.get(SourceChannel, channel.id)
    assert refreshed is not None
    # Replay must NOT advance last_read_post_id, even when a higher post_id
    # is re-ingested out of band.
    assert refreshed.last_read_post_id == "500"


async def test_replay_channel_post_surfaces_malformed_for_unpinned_post_id(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="empty_chan",
        session_id="primary",
    )
    fake = FakeTelegramClient()

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=fake,
        phash_tag="E",
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.post(
            f"/api/v1/crawler/channels/{channel.id}/replay-post",
            headers=_operator_headers(),
            json={"post_id": "99"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["code"] == "telegram_malformed_message"


async def test_replay_channel_post_returns_404_for_unknown_channel_id(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.post(
            f"/api/v1/crawler/channels/{uuid.uuid7()}/replay-post",
            headers=_operator_headers(),
            json={"post_id": "1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["code"] == "crawler_channel_not_found"


# ---------------------------------------------------------------------------
# Freshness snapshot
# ---------------------------------------------------------------------------


def _seed_meme_and_file(
    session: AsyncSession,
    *,
    source_id: str,
) -> MemeFile:
    """Create a minimal Meme + MemeFile pair ready for source + stage inserts."""

    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=file_id, language=ContentLanguage.NONE)
    file_row = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"pipeline/originals/{source_id}.png",
        quality_score=0.5,
    )
    session.add(meme)
    session.add(file_row)
    return file_row


async def _seed_freshness_item(
    session: AsyncSession,
    *,
    source_id: str,
    post_id: str,
    published_at: datetime,
    first_ingested_at: datetime,
    qdrant_finished_at: datetime | None,
    meili_finished_at: datetime | None,
) -> MemeFile:
    file_row = _seed_meme_and_file(session, source_id=f"{source_id}-{post_id}")
    await session.flush()
    meme_source = MemeSource(
        file_id=file_row.id,
        platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        views=1,
        reactions={},
        is_first_source=True,
        source_alive=True,
        published_at=published_at,
    )
    meme_source.created_at = first_ingested_at
    session.add(meme_source)

    if qdrant_finished_at is not None:
        session.add(
            PipelineStageJournal(
                meme_file_id=file_row.id,
                stage=ContentPipelineStage.SYNC_QDRANT,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                started_at=qdrant_finished_at,
                finished_at=qdrant_finished_at,
            )
        )
        session.add(
            MemeFileSyncTargetSnapshot(
                meme_file_id=file_row.id,
                sync_target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.SYNCED,
                last_success_at=qdrant_finished_at,
                last_attempt_at=qdrant_finished_at,
                attempt_count=1,
            )
        )
    if meili_finished_at is not None:
        session.add(
            PipelineStageJournal(
                meme_file_id=file_row.id,
                stage=ContentPipelineStage.SYNC_MEILI,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                started_at=meili_finished_at,
                finished_at=meili_finished_at,
            )
        )
        session.add(
            MemeFileSyncTargetSnapshot(
                meme_file_id=file_row.id,
                sync_target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.SYNCED,
                last_success_at=meili_finished_at,
                last_attempt_at=meili_finished_at,
                attempt_count=1,
            )
        )
    await session.commit()
    await session.refresh(file_row)
    return file_row


async def test_freshness_snapshot_aggregates_per_channel_percentiles_and_slo_flags(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_channel(
        migrated_db_session,
        platform_id="fast_chan",
        title="Fast Channel",
    )
    await _seed_channel(
        migrated_db_session,
        platform_id="mixed_chan",
        title="Mixed Channel",
    )

    base_time = _now() - timedelta(minutes=5)
    # Fast channel: two items each with ~30s freshness (well inside SLO).
    await _seed_freshness_item(
        migrated_db_session,
        source_id="fast_chan",
        post_id="1",
        published_at=base_time,
        first_ingested_at=base_time + timedelta(seconds=1),
        qdrant_finished_at=base_time + timedelta(seconds=20),
        meili_finished_at=base_time + timedelta(seconds=30),
    )
    await _seed_freshness_item(
        migrated_db_session,
        source_id="fast_chan",
        post_id="2",
        published_at=base_time + timedelta(seconds=10),
        first_ingested_at=base_time + timedelta(seconds=11),
        qdrant_finished_at=base_time + timedelta(seconds=35),
        meili_finished_at=base_time + timedelta(seconds=40),
    )
    # Mixed channel: one fully synced fast, one qdrant-only (freshness None),
    # and one not yet synced at all.
    await _seed_freshness_item(
        migrated_db_session,
        source_id="mixed_chan",
        post_id="10",
        published_at=base_time + timedelta(seconds=20),
        first_ingested_at=base_time + timedelta(seconds=21),
        qdrant_finished_at=base_time + timedelta(seconds=40),
        meili_finished_at=base_time + timedelta(seconds=45),
    )
    await _seed_freshness_item(
        migrated_db_session,
        source_id="mixed_chan",
        post_id="11",
        published_at=base_time + timedelta(seconds=30),
        first_ingested_at=base_time + timedelta(seconds=31),
        qdrant_finished_at=base_time + timedelta(seconds=60),
        meili_finished_at=None,
    )
    await _seed_freshness_item(
        migrated_db_session,
        source_id="mixed_chan",
        post_id="12",
        published_at=base_time + timedelta(seconds=40),
        first_ingested_at=base_time + timedelta(seconds=41),
        qdrant_finished_at=None,
        meili_finished_at=None,
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.get(
            "/api/v1/crawler/freshness",
            headers=_operator_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["item_count"] == 5
    assert snapshot["slo_p50_pass"] is True
    assert snapshot["slo_p95_pass"] is True
    # Only three items reached both sync targets, with freshness values
    # 30s (fast_chan:1), 30s (fast_chan:2), and 25s (mixed_chan:10).
    # Linear-interp percentiles on [25, 30, 30]: p50=30, p95≈30.
    assert snapshot["p50_seconds"] == 30.0
    assert snapshot["p95_seconds"] == 30.0
    assert snapshot["slo_p50_seconds"] > 0
    assert snapshot["slo_p95_seconds"] > 0

    per_channel = {row["platform_id"]: row for row in snapshot["per_channel"]}
    assert per_channel["fast_chan"]["item_count"] == 2
    # Both fast_chan items settled at 30s of freshness, so p50 collapses.
    assert per_channel["fast_chan"]["p50_seconds"] == 30.0
    assert per_channel["fast_chan"]["p95_seconds"] == 30.0
    assert per_channel["mixed_chan"]["item_count"] == 3
    # Only the single fully-synced item on the mixed channel contributes
    # to the percentile, so p50 and p95 collapse to 25.0.
    assert per_channel["mixed_chan"]["p50_seconds"] == 25.0
    assert per_channel["mixed_chan"]["p95_seconds"] == 25.0

    # Sample items should be bounded (default max 50) and carry both-synced
    # freshness values when set, nulls otherwise.
    sample_ids = {row["meme_file_id"] for row in snapshot["sample_items"]}
    assert len(sample_ids) == 5

    samples_by_searchability = {row["searchability"]: row for row in snapshot["sample_items"]}
    assert samples_by_searchability["ready"]["qdrant_status"] == "synced"
    assert samples_by_searchability["ready"]["meili_status"] == "synced"
    assert samples_by_searchability["partially_searchable"]["qdrant_status"] == "synced"
    assert samples_by_searchability["partially_searchable"]["meili_status"] is None
    assert samples_by_searchability["in_flight"]["qdrant_status"] is None
    assert samples_by_searchability["in_flight"]["meili_status"] is None


async def test_freshness_snapshot_honors_since_filter(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_channel(
        migrated_db_session,
        platform_id="time_chan",
        title="Time Channel",
    )
    old_time = _now() - timedelta(days=7)
    recent_time = _now() - timedelta(minutes=1)
    await _seed_freshness_item(
        migrated_db_session,
        source_id="time_chan",
        post_id="old",
        published_at=old_time,
        first_ingested_at=old_time + timedelta(seconds=1),
        qdrant_finished_at=old_time + timedelta(seconds=10),
        meili_finished_at=old_time + timedelta(seconds=15),
    )
    await _seed_freshness_item(
        migrated_db_session,
        source_id="time_chan",
        post_id="recent",
        published_at=recent_time,
        first_ingested_at=recent_time + timedelta(seconds=1),
        qdrant_finished_at=recent_time + timedelta(seconds=5),
        meili_finished_at=recent_time + timedelta(seconds=10),
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    cutoff = (_now() - timedelta(hours=1)).isoformat()
    try:
        response = await client.get(
            "/api/v1/crawler/freshness",
            headers=_operator_headers(),
            params={"since": cutoff},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["item_count"] == 1
    assert body["p50_seconds"] == 10.0
    assert body["since"] is not None
    sample_ids = [row["meme_file_id"] for row in body["sample_items"]]
    assert len(sample_ids) == 1


async def test_freshness_snapshot_surfaces_blocked_stage_and_target_reason(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_channel(
        migrated_db_session,
        platform_id="blocked_chan",
        title="Blocked Channel",
    )
    published_at = _now() - timedelta(minutes=2)
    file_row = _seed_meme_and_file(migrated_db_session, source_id="blocked-item")
    await migrated_db_session.flush()
    meme_source = MemeSource(
        file_id=file_row.id,
        platform=SourcePlatform.TELEGRAM,
        source_id="blocked_chan",
        post_id="blocked-1",
        views=1,
        reactions={},
        is_first_source=True,
        source_alive=True,
        published_at=published_at,
    )
    meme_source.created_at = published_at + timedelta(seconds=1)
    migrated_db_session.add(meme_source)
    migrated_db_session.add(
        PipelineStageJournal(
            meme_file_id=file_row.id,
            stage=ContentPipelineStage.SYNC_QDRANT,
            status=ContentPipelineStageStatus.FAILED,
            attempt_count=2,
            normalized_reason="sync_qdrant_timeout",
            last_error_text="qdrant timed out",
            is_retryable=True,
            started_at=published_at + timedelta(seconds=5),
            finished_at=published_at + timedelta(seconds=20),
        )
    )
    migrated_db_session.add(
        MemeFileSyncTargetSnapshot(
            meme_file_id=file_row.id,
            sync_target=SyncTargetKind.QDRANT,
            status=SyncTargetStatus.FAILED,
            normalized_reason="sync_qdrant_timeout",
            last_error_text="qdrant timed out",
            last_attempt_at=published_at + timedelta(seconds=20),
            attempt_count=2,
        )
    )
    await migrated_db_session.commit()

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.get(
            "/api/v1/crawler/freshness",
            headers=_operator_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    sample = response.json()["sample_items"][0]
    assert sample["freshness_seconds"] is None
    assert sample["searchability"] == "blocked"
    assert sample["pipeline_stage"] == "sync_qdrant"
    assert sample["pipeline_status"] == "failed"
    assert sample["failure_reason"] == "sync_qdrant_timeout"
    assert sample["failure_text"] == "qdrant timed out"
    assert sample["qdrant_status"] == "failed"
    assert sample["qdrant_reason"] == "sync_qdrant_timeout"
    assert sample["qdrant_error"] == "qdrant timed out"
    assert sample["meili_status"] is None


async def test_freshness_snapshot_with_no_items_reports_null_percentiles_and_slo_pass(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.get(
            "/api/v1/crawler/freshness",
            headers=_operator_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["item_count"] == 0
    assert body["p50_seconds"] is None
    assert body["p95_seconds"] is None
    # "no data means pass" convention: empty snapshot must not manufacture
    # an SLO breach. T04's proof harness is responsible for insisting on a
    # populated sample set before declaring a pass or fail.
    assert body["slo_p50_pass"] is True
    assert body["slo_p95_pass"] is True
    assert body["per_channel"] == []
    assert body["sample_items"] == []


async def test_freshness_snapshot_skips_memesources_for_untracked_channels(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    """A MemeSource without a matching SourceChannel row must not leak into the snapshot."""

    await _seed_session(migrated_db_session, session_name="primary")
    await _seed_channel(
        migrated_db_session,
        platform_id="tracked_chan",
        title="Tracked",
    )
    base_time = _now() - timedelta(minutes=2)
    await _seed_freshness_item(
        migrated_db_session,
        source_id="tracked_chan",
        post_id="1",
        published_at=base_time,
        first_ingested_at=base_time + timedelta(seconds=1),
        qdrant_finished_at=base_time + timedelta(seconds=10),
        meili_finished_at=base_time + timedelta(seconds=20),
    )
    # Orphaned source: no matching SourceChannel row for source_id="orphan".
    await _seed_freshness_item(
        migrated_db_session,
        source_id="orphan",
        post_id="2",
        published_at=base_time + timedelta(seconds=10),
        first_ingested_at=base_time + timedelta(seconds=11),
        qdrant_finished_at=base_time + timedelta(seconds=20),
        meili_finished_at=base_time + timedelta(seconds=30),
    )

    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.get(
            "/api/v1/crawler/freshness",
            headers=_operator_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert body["item_count"] == 1
    assert len(body["per_channel"]) == 1
    assert body["per_channel"][0]["platform_id"] == "tracked_chan"


# ---------------------------------------------------------------------------
# OpenAPI registration
# ---------------------------------------------------------------------------


async def test_crawler_routes_registered_with_operator_token_header(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    operations_service = _build_real_operations_service(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
    )
    _install_operations_service_override(app, operations_service)

    try:
        response = await client.get("/openapi.json")
    finally:
        app.dependency_overrides.clear()

    paths = response.json()["paths"]
    expected_paths = [
        "/api/v1/crawler/sessions",
        "/api/v1/crawler/channels",
        "/api/v1/crawler/channels/{source_channel_id}/pause",
        "/api/v1/crawler/channels/{source_channel_id}/resume",
        "/api/v1/crawler/channels/{source_channel_id}/reassign",
        "/api/v1/crawler/channels/{source_channel_id}/replay-post",
        "/api/v1/crawler/freshness",
    ]
    for path in expected_paths:
        assert path in paths, path
        # Each route must advertise the operator-token header parameter so
        # operator tooling can auto-detect the auth requirement.
        path_entry = next(iter(paths[path].values()))
        assert any(
            parameter["name"] == PIPELINE_OPERATOR_TOKEN_HEADER_NAME
            and parameter["in"] == "header"
            for parameter in path_entry.get("parameters", [])
        ), f"operator-token header missing for {path}"


# Silence "unused import" from referencing the canned channel class as
# part of the runtime contract imports above.
_ = RawTelegramChannel
