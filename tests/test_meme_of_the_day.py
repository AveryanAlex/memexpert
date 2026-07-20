# ruff: noqa: TC002,TC003
"""Focused tests for Meme of the Day selection, cache, and routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text

import memexpert.services.meme_of_the_day as meme_of_the_day_module
from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_meme_of_the_day_service
from memexpert.core.config import Settings
from memexpert.models.content import Meme, MemeFile, MemeOfTheDaySelection
from memexpert.models.enums import AccountStatus, AnalyticsEventType, ContentKind, ContentLanguage
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.user import UserRead
from memexpert.services.collection_service import CollectionService
from memexpert.services.meme_of_the_day import MemeOfTheDayService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.transactional_db]


async def test_motd_filters_public_safe_recent_quality_candidates(migrated_db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    selected = await _create_meme(migrated_db_session, created_at=now - timedelta(days=1), quality_score=0.8)
    _ = await _create_meme(migrated_db_session, created_at=now - timedelta(days=1), quality_score=0.9, is_public=False)
    _ = await _create_meme(migrated_db_session, created_at=now - timedelta(days=1), quality_score=0.95, is_nsfw=True)
    _ = await _create_meme(migrated_db_session, created_at=now - timedelta(days=1), quality_score=0.4)
    _ = await _create_meme(migrated_db_session, created_at=now - timedelta(days=45), quality_score=0.99)

    result = await MemeOfTheDayService(
        migrated_db_session,
        settings=Settings.model_validate(
            {
                "motd_min_quality_score": 0.5,
                "motd_candidate_lookback_days": 30,
                "motd_quality_weight": 1.0,
                "motd_popularity_weight": 0.0,
                "motd_trending_growth_weight": 0.0,
                "motd_novelty_weight": 0.0,
            }
        ),
    ).refresh()

    assert result.meme is not None
    assert result.meme.id == selected.id
    assert result.meme.is_nsfw is False
    assert result.candidate_count == 1
    assert result.reason == "selected"
    assert result.attribution is not None
    assert result.attribution.source_algorithm == "motd"
    assert result.attribution.surface == "web_home"
    assert result.attribution.rank == 1


async def test_motd_uses_trend_mv_metrics_when_present_and_allows_missing_mv_rows(
    migrated_db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    trended = await _create_meme(migrated_db_session, created_at=now - timedelta(days=2), quality_score=0.6)
    for _ in range(8):
        migrated_db_session.add(
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_LIKE,
                payload={"meme_id": str(trended.id)},
                occurred_at=now - timedelta(hours=1),
            )
        )
    await migrated_db_session.flush()
    await migrated_db_session.execute(text("REFRESH MATERIALIZED VIEW public_meme_trends_mv"))
    missing_mv = await _create_meme(migrated_db_session, created_at=now - timedelta(days=1), quality_score=1.0)

    result = await MemeOfTheDayService(
        migrated_db_session,
        settings=Settings.model_validate(
            {
                "motd_popularity_weight": 0.0,
                "motd_trending_growth_weight": 1.0,
                "motd_novelty_weight": 0.0,
                "motd_quality_weight": 0.0,
            }
        ),
    ).refresh()

    assert result.meme is not None
    assert result.meme.id == trended.id
    assert result.candidate_count == 2
    assert result.score_components["trending_growth_raw"] > 0.0
    assert missing_mv.id != trended.id


async def test_motd_keeps_download_metrics_but_excludes_them_from_trending_growth(
    migrated_db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    download_heavy = await _create_meme(
        migrated_db_session,
        created_at=now - timedelta(days=1),
        quality_score=0.8,
    )
    viewed = await _create_meme(
        migrated_db_session,
        created_at=now - timedelta(days=2),
        quality_score=0.8,
    )
    migrated_db_session.add_all(
        [
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                payload={"meme_id": str(download_heavy.id)},
                occurred_at=now - timedelta(hours=1),
            )
            for _ in range(8)
        ]
        + [
            AnalyticsEvent(
                event_type=AnalyticsEventType.MEME_VIEW,
                payload={"meme_id": str(viewed.id)},
                occurred_at=now - timedelta(hours=1),
            )
        ]
    )
    await migrated_db_session.flush()
    await migrated_db_session.execute(text("REFRESH MATERIALIZED VIEW public_meme_trends_mv"))

    trend_result = await migrated_db_session.execute(
        text(
            """
            SELECT meme_id, recent_view_count, recent_download_count
            FROM public_meme_trends_mv
            WHERE meme_id IN (:download_meme_id, :viewed_meme_id)
            """
        ),
        {"download_meme_id": download_heavy.id, "viewed_meme_id": viewed.id},
    )
    trend_rows = {row["meme_id"]: row for row in trend_result.mappings()}

    result = await MemeOfTheDayService(
        migrated_db_session,
        settings=Settings.model_validate(
            {
                "motd_popularity_weight": 0.0,
                "motd_trending_growth_weight": 1.0,
                "motd_novelty_weight": 0.0,
                "motd_quality_weight": 0.0,
            }
        ),
    ).refresh()

    assert trend_rows[download_heavy.id]["recent_download_count"] == 8
    assert trend_rows[download_heavy.id]["recent_view_count"] == 0
    assert result.meme is not None
    assert result.meme.id == viewed.id
    assert result.score_components["trending_growth_raw"] == pytest.approx(1.0)


async def test_motd_deterministic_tiebreakers_use_newest_then_meme_id(migrated_db_session: AsyncSession) -> None:
    older = await _create_meme(
        migrated_db_session,
        meme_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        created_at=datetime.now(UTC) - timedelta(days=2),
        quality_score=0.8,
    )
    newer_low_id = await _create_meme(
        migrated_db_session,
        meme_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        created_at=datetime.now(UTC) - timedelta(days=1),
        quality_score=0.8,
    )
    newer_high_id = await _create_meme(
        migrated_db_session,
        meme_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        created_at=newer_low_id.created_at,
        quality_score=0.8,
    )

    result = await MemeOfTheDayService(
        migrated_db_session,
        settings=Settings.model_validate(
            {
                "motd_popularity_weight": 0.0,
                "motd_trending_growth_weight": 0.0,
                "motd_novelty_weight": 0.0,
                "motd_quality_weight": 0.0,
            }
        ),
    ).refresh()

    assert result.meme is not None
    assert result.meme.id == newer_high_id.id
    assert result.meme.id not in {older.id, newer_low_id.id}


async def test_motd_excludes_future_created_candidate_for_today(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(meme_of_the_day_module, "utcnow", lambda: fixed_now)
    current = await _create_meme(migrated_db_session, created_at=fixed_now - timedelta(hours=1), quality_score=0.6)
    future = await _create_meme(migrated_db_session, created_at=fixed_now + timedelta(hours=1), quality_score=1.0)

    result = await MemeOfTheDayService(
        migrated_db_session,
        settings=Settings.model_validate(
            {
                "motd_quality_weight": 1.0,
                "motd_popularity_weight": 0.0,
                "motd_trending_growth_weight": 0.0,
                "motd_novelty_weight": 0.0,
            }
        ),
    ).refresh()

    assert result.meme is not None
    assert result.meme.id == current.id
    assert result.meme.id != future.id
    assert result.candidate_count == 1


async def test_motd_no_candidate_fallback_and_cache_update_behavior(migrated_db_session: AsyncSession) -> None:
    settings = Settings.model_validate(
        {
            "motd_quality_weight": 1.0,
            "motd_popularity_weight": 0.0,
            "motd_trending_growth_weight": 0.0,
            "motd_novelty_weight": 0.0,
        }
    )
    service = MemeOfTheDayService(migrated_db_session, settings=settings)

    fallback = await service.refresh()

    assert fallback.meme is None
    assert fallback.candidate_count == 0
    assert fallback.reason == "no_candidates"
    row = await migrated_db_session.scalar(select(MemeOfTheDaySelection))
    assert row is not None
    assert row.meme_id is None

    low = await _create_meme(migrated_db_session, created_at=datetime.now(UTC) - timedelta(days=1), quality_score=0.6)
    cached = await service.get_today()
    better = await _create_meme(
        migrated_db_session,
        created_at=datetime.now(UTC) - timedelta(days=1),
        quality_score=0.9,
    )
    refreshed = await service.refresh()

    assert cached.meme is None
    assert refreshed.meme is not None
    assert refreshed.meme.id == better.id
    assert refreshed.meme.id != low.id
    rows = (await migrated_db_session.execute(select(MemeOfTheDaySelection))).scalars().all()
    assert len(rows) == 1


async def test_motd_route_public_read_and_admin_refresh_auth_shape(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session, created_at=datetime.now(UTC) - timedelta(days=1), quality_score=0.8)
    admin = User(
        email="motd-admin@example.com",
        email_verified_at=datetime.now(UTC),
        is_admin=True,
        status=AccountStatus.ACTIVE,
    )
    migrated_db_session.add(admin)
    await migrated_db_session.flush()

    def override_motd_service() -> MemeOfTheDayService:
        return MemeOfTheDayService(migrated_db_session)

    async def anonymous_user() -> UserRead | None:
        return None

    async def admin_user() -> UserRead | None:
        return UserRead.model_validate(admin)

    app.dependency_overrides[get_meme_of_the_day_service] = override_motd_service
    app.dependency_overrides[get_optional_current_user] = anonymous_user
    try:
        public_response = await client.get("/api/v1/memes/meme-of-the-day")
        unauthorized_refresh = await client.post("/api/v1/memes/meme-of-the-day/refresh")
        app.dependency_overrides[get_optional_current_user] = admin_user
        admin_refresh = await client.post("/api/v1/memes/meme-of-the-day/refresh")
    finally:
        app.dependency_overrides.clear()

    assert public_response.status_code == 200
    payload = public_response.json()
    assert payload["meme"]["id"] == str(meme.id)
    assert payload["attribution"]["source_algorithm"] == "motd"
    assert payload["attribution"]["surface"] == "web_home"
    assert unauthorized_refresh.status_code == 401
    assert admin_refresh.status_code == 200
    assert admin_refresh.json()["meme"]["id"] == str(meme.id)


async def test_motd_route_overlays_viewer_favorite_state_after_reload(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(
        migrated_db_session,
        created_at=datetime.now(UTC) - timedelta(days=1),
        quality_score=0.8,
    )
    viewer = User(
        email="motd-viewer@example.com",
        email_verified_at=datetime.now(UTC),
        status=AccountStatus.ACTIVE,
    )
    other_viewer = User(
        email="motd-other-viewer@example.com",
        email_verified_at=datetime.now(UTC),
        status=AccountStatus.ACTIVE,
    )
    migrated_db_session.add_all([viewer, other_viewer])
    await migrated_db_session.flush()
    _ = await CollectionService(migrated_db_session).favorite_meme(user_id=viewer.id, meme_id=meme.id)

    current_user = UserRead.model_validate(viewer)

    def override_motd_service() -> MemeOfTheDayService:
        return MemeOfTheDayService(migrated_db_session)

    async def override_current_user() -> UserRead | None:
        return current_user

    app.dependency_overrides[get_meme_of_the_day_service] = override_motd_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        viewer_response = await client.get("/api/v1/memes/meme-of-the-day")
        current_user = UserRead.model_validate(other_viewer)
        other_response = await client.get("/api/v1/memes/meme-of-the-day")
    finally:
        app.dependency_overrides.clear()

    assert viewer_response.status_code == 200
    assert viewer_response.json()["meme"]["viewer_has_favorited"] is True
    assert viewer_response.json()["meme"]["like_count"] == 1
    assert other_response.status_code == 200
    assert other_response.json()["meme"]["viewer_has_favorited"] is False
    assert other_response.json()["meme"]["like_count"] == 1


async def _create_meme(
    session: AsyncSession,
    *,
    meme_id: uuid.UUID | None = None,
    created_at: datetime,
    quality_score: float,
    is_public: bool = True,
    is_nsfw: bool = False,
) -> Meme:
    resolved_meme_id = meme_id or uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=resolved_meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=["motd"],
        is_public=is_public,
        is_nsfw=is_nsfw,
        like_count=0,
        created_at=created_at,
        updated_at=created_at,
    )
    file = MemeFile(
        id=file_id,
        meme_id=resolved_meme_id,
        s3_original_key=f"motd/{file_id}.jpg",
        mime_type="image/jpeg",
        quality_score=quality_score,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()
    return meme
