"""Integration tests for scheduled popularity snapshot capture."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_public_trends_service
from memexpert.core.config import Settings
from memexpert.models.content import Meme, MemeFile, MemeSource
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage, SourcePlatform
from memexpert.models.user import AnalyticsEvent
from memexpert.services.popularity_snapshots import (
    PopularitySnapshotWeights,
    calculate_popularity_score,
    capture_popularity_snapshots,
)
from memexpert.services.public_trends import PublicTrendsService, refresh_public_trend_materialized_views

if TYPE_CHECKING:
    import uuid

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from memexpert.schemas.user import UserRead


def test_calculate_popularity_score_uses_log1p_weighted_metrics() -> None:
    weights = PopularitySnapshotWeights(
        source_view=1.5,
        source_reaction=2.5,
        source_repost=3.5,
        platform_view=4.5,
        platform_send=5.5,
        platform_save=6.5,
        platform_like=7.5,
    )

    score = calculate_popularity_score(
        source_views=10,
        source_reactions=20,
        source_reposts=3,
        platform_views=11,
        platform_sends=5,
        platform_saves=7,
        platform_likes=13,
        weights=weights,
    )

    assert score == pytest.approx(
        math.log1p(10) * 1.5
        + math.log1p(20) * 2.5
        + math.log1p(3) * 3.5
        + math.log1p(11) * 4.5
        + math.log1p(5) * 5.5
        + math.log1p(7) * 6.5
        + math.log1p(13) * 7.5
    )


@pytest.mark.asyncio
async def test_capture_popularity_snapshots_computes_metrics_upserts_and_updates_memes(
    migrated_db_session: AsyncSession,
) -> None:
    captured_at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    meme = await _create_meme(migrated_db_session, tags=["cats"])
    private_meme = await _create_meme(migrated_db_session, is_public=False)
    await _add_sources(migrated_db_session, meme)
    migrated_db_session.add(
        MemeSource(
            file_id=_primary_file_id(private_meme),
            platform=SourcePlatform.TELEGRAM,
            source_id=f"private-{private_meme.id}",
            post_id="1",
            views=999,
            reactions={"hidden": 999},
        )
    )
    migrated_db_session.add_all(
        [
            _analytics_event(AnalyticsEventType.MEME_VIEW, meme_id=meme.id),
            _analytics_event(AnalyticsEventType.VIEW, meme_id=meme.id, use_refs=True),
            _analytics_event(AnalyticsEventType.MEME_SEND, meme_id=meme.id),
            _analytics_event(AnalyticsEventType.SHARE, meme_id=meme.id, use_refs=True),
            _analytics_event(AnalyticsEventType.MEME_SAVE, meme_id=meme.id),
            _analytics_event(AnalyticsEventType.SAVE, meme_id=meme.id, use_refs=True),
            _analytics_event(AnalyticsEventType.MEME_LIKE, meme_id=meme.id),
            _analytics_event(AnalyticsEventType.FAVORITE, meme_id=meme.id, use_refs=True),
            _analytics_event(AnalyticsEventType.MEME_VIEW, meme_id=private_meme.id),
            AnalyticsEvent(event_type=AnalyticsEventType.MEME_VIEW, payload={"refs": {"meme_id": "not-a-uuid"}}),
        ]
    )
    await migrated_db_session.flush()

    result = await capture_popularity_snapshots(
        migrated_db_session,
        settings=Settings(),
        captured_at=captured_at,
    )

    assert result.public_meme_count == 1
    assert result.snapshot_count == 1
    assert result.updated_meme_count == 1
    row = await _snapshot_row(migrated_db_session, meme.id, captured_at)
    assert row == {
        "source_views": 125,
        "source_reactions": 9,
        "source_reposts": 1,
        "platform_views": 2,
        "platform_sends": 2,
        "platform_saves": 2,
        "platform_likes": 2,
        "popularity_score": pytest.approx(
            calculate_popularity_score(
                source_views=125,
                source_reactions=9,
                source_reposts=1,
                platform_views=2,
                platform_sends=2,
                platform_saves=2,
                platform_likes=2,
                weights=PopularitySnapshotWeights.from_settings(Settings()),
            )
        ),
    }
    await migrated_db_session.refresh(meme)
    assert meme.popularity_score == pytest.approx(row["popularity_score"])
    private_snapshot_count = await migrated_db_session.scalar(
        text("SELECT count(*) FROM meme_popularity_snapshots WHERE meme_id = :meme_id"),
        {"meme_id": private_meme.id},
    )
    assert private_snapshot_count == 0

    migrated_db_session.add(_analytics_event(AnalyticsEventType.MEME_VIEW, meme_id=meme.id))
    await migrated_db_session.flush()
    result = await capture_popularity_snapshots(
        migrated_db_session,
        settings=Settings(),
        captured_at=captured_at,
    )

    assert result.snapshot_count == 1
    snapshot_count = await migrated_db_session.scalar(
        text(
            """
            SELECT count(*)
            FROM meme_popularity_snapshots
            WHERE meme_id = :meme_id AND captured_at = :captured_at
            """
        ),
        {"meme_id": meme.id, "captured_at": captured_at},
    )
    assert snapshot_count == 1
    row = await _snapshot_row(migrated_db_session, meme.id, captured_at)
    assert row["platform_views"] == 3
    await migrated_db_session.refresh(meme)
    assert meme.popularity_score == pytest.approx(row["popularity_score"])


@pytest.mark.asyncio
async def test_public_trend_routes_read_snapshot_data_after_capture_and_mv_refresh(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
    postgres_async_engine: AsyncEngine,
) -> None:
    captured_at = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    meme = await _create_meme(migrated_db_session, tags=["public-route"])
    await _add_sources(migrated_db_session, meme)
    migrated_db_session.add(_analytics_event(AnalyticsEventType.MEME_LIKE, meme_id=meme.id, use_refs=True))
    await migrated_db_session.flush()

    _ = await capture_popularity_snapshots(migrated_db_session, settings=Settings(), captured_at=captured_at)
    await refresh_public_trend_materialized_views(postgres_async_engine, concurrently=True)
    _install_public_trends_route_overrides(app, migrated_db_session)
    try:
        trends_response = await client.get("/api/v1/memes/trends")
        popularity_response = await client.get(f"/api/v1/memes/{meme.id}/popularity")
    finally:
        app.dependency_overrides.clear()

    assert trends_response.status_code == 200
    trends_payload = trends_response.json()
    trend_item = next(item for item in trends_payload["items"] if item["meme"]["id"] == str(meme.id))
    assert trend_item["trend"]["latest_source_views"] == 125
    assert trend_item["trend"]["latest_source_reactions"] == 9
    assert trend_item["trend"]["latest_platform_likes"] == 1

    assert popularity_response.status_code == 200
    popularity_payload = popularity_response.json()
    assert popularity_payload["meme_id"] == str(meme.id)
    assert popularity_payload["sparkline"] == [
        {
            "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "source_views": 125,
            "source_reactions": 9,
            "source_reposts": 1,
            "platform_views": 0,
            "platform_sends": 0,
            "platform_saves": 0,
            "platform_likes": 1,
            "popularity_score": pytest.approx(trend_item["trend"]["latest_popularity_score"]),
        }
    ]


async def _create_meme(
    session: AsyncSession,
    *,
    tags: list[str] | None = None,
    is_public: bool = True,
) -> Meme:
    meme = Meme(
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        tags=tags or [],
        is_public=is_public,
        popularity_score=0.0,
    )
    session.add(meme)
    await session.flush()
    file = MemeFile(
        meme_id=meme.id,
        s3_original_key=f"memes/{meme.id}.jpg",
        mime_type="image/jpeg",
        quality_score=1.0,
        is_primary=True,
    )
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
    await session.flush()
    return meme


async def _add_sources(session: AsyncSession, meme: Meme) -> None:
    file_id = _primary_file_id(meme)
    session.add_all(
        [
            MemeSource(
                file_id=file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id=f"source-{meme.id}",
                post_id="1",
                views=100,
                reactions={"like": 2, "fire": 3},
            ),
            MemeSource(
                file_id=file_id,
                platform=SourcePlatform.TELEGRAM,
                source_id=f"repost-{meme.id}",
                post_id="2",
                views=25,
                reactions={"heart": 4},
                forwarded_from_source_id=f"source-{meme.id}",
                forwarded_from_post_id="1",
            ),
        ]
    )


def _primary_file_id(meme: Meme) -> uuid.UUID:
    assert meme.primary_file_id is not None
    return meme.primary_file_id


def _analytics_event(
    event_type: AnalyticsEventType,
    *,
    meme_id: uuid.UUID,
    use_refs: bool = False,
) -> AnalyticsEvent:
    payload: dict[str, object] = {"refs": {"meme_id": str(meme_id)}} if use_refs else {"meme_id": str(meme_id)}
    return AnalyticsEvent(event_type=event_type, payload=payload)


async def _snapshot_row(session: AsyncSession, meme_id: uuid.UUID, captured_at: datetime) -> dict[str, object]:
    result = await session.execute(
        text(
            """
            SELECT
                source_views,
                source_reactions,
                source_reposts,
                platform_views,
                platform_sends,
                platform_saves,
                platform_likes,
                popularity_score
            FROM meme_popularity_snapshots
            WHERE meme_id = :meme_id AND captured_at = :captured_at
            """
        ),
        {"meme_id": meme_id, "captured_at": captured_at},
    )
    row = result.mappings().one()
    return dict(row)


def _install_public_trends_route_overrides(app: FastAPI, session: AsyncSession) -> None:
    def override_public_trends_service() -> PublicTrendsService:
        return PublicTrendsService(session)

    async def override_current_user() -> UserRead | None:
        return None

    app.dependency_overrides[get_public_trends_service] = override_public_trends_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
