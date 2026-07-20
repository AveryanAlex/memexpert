# ruff: noqa: TC002
"""Integration coverage for bounded recommendation dashboard rollups."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage, ContentProcessingStatus
from memexpert.models.recommendation import RecommendationDailyAggregate
from memexpert.models.user import AnalyticsEvent
from memexpert.services.recommendations.analytics import (
    load_recommendation_daily_analytics,
    rollup_recommendation_daily_analytics,
)
from tests.factories import build_full_user

pytestmark = pytest.mark.asyncio


async def test_daily_rollup_is_idempotent_and_groups_trusted_candidate_sources(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = build_full_user()
    migrated_db_session.add(user)
    await migrated_db_session.flush()
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    second_meme_id = uuid.uuid7()
    second_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        primary_file_id=file_id,
        is_public=True,
    )
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        quality_score=0.8,
        s3_original_key=f"recommendation-analytics/{file_id}.jpg",
    )
    second_meme = Meme(
        id=second_meme_id,
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        primary_file_id=second_file_id,
        is_public=True,
    )
    second_meme_file = MemeFile(
        id=second_file_id,
        meme_id=second_meme_id,
        status=ContentProcessingStatus.READY,
        quality_score=0.7,
        s3_original_key=f"recommendation-analytics/{second_file_id}.jpg",
    )
    migrated_db_session.add_all([meme, second_meme])
    await migrated_db_session.flush()
    migrated_db_session.add_all([meme_file, second_meme_file])
    observed_at = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    source_properties = {
        "attribution_trusted": True,
        "algorithm_version": "personalized_v2",
        "profile_version": "taste_v2:test",
        "candidate_sources": [
            {"source": "short_term", "rank": 1, "score": 0.9, "contribution": 0.01},
            # Multiple retrieval representations may expose the same typed
            # source. It remains one keyed impression for source reporting.
            {"source": "short_term", "rank": 2, "score": 0.8, "contribution": 0.005},
        ],
    }
    migrated_db_session.add_all(
        [
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_IMPRESSION,
                occurred_at=observed_at - timedelta(hours=48),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(meme_id)},
                    "impression_id": "imp-prior-1",
                    "properties": source_properties,
                },
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at - timedelta(days=6),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(second_meme_id)},
                    "properties": source_properties,
                },
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_IMPRESSION,
                occurred_at=observed_at,
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(meme_id)},
                    "impression_id": "imp-v2-1",
                    "reason": "quality_exploration",
                    "properties": source_properties,
                },
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_SEND,
                occurred_at=observed_at + timedelta(minutes=1),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(meme_id)},
                    "impression_id": "imp-v2-1",
                    "reason": "quality_exploration",
                    "properties": source_properties,
                },
            ),
            # A second action for the same keyed exposure remains one
            # conversion, while an action without a matching impression is
            # excluded from the funnel numerator entirely.
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at + timedelta(minutes=2),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(meme_id)},
                    "impression_id": "imp-v2-1",
                    "reason": "quality_exploration",
                    "properties": source_properties,
                },
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_SEND,
                occurred_at=observed_at + timedelta(minutes=3),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(meme_id)},
                    "impression_id": "imp-unmatched",
                    "properties": source_properties,
                },
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_IMPRESSION,
                occurred_at=observed_at + timedelta(minutes=30),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(second_meme_id)},
                    "impression_id": "imp-v2-2",
                    "reason": "multi_source_personalized",
                    "properties": source_properties,
                },
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_IMPRESSION,
                occurred_at=observed_at + timedelta(hours=1),
                payload={
                    "surface": "web_home",
                    "refs": {"meme_id": str(meme_id)},
                    "impression_id": "imp-fallback-1",
                    "reason": "redis_or_personalization_fallback",
                    "properties": {
                        "algorithm_version": "public_trending_keyset_v1",
                        "attribution_trusted": True,
                        "candidate_sources": [
                            {
                                "source": "trending",
                                "rank": 1,
                                "score": 0.5,
                                "contribution": 0.01,
                            }
                        ],
                    },
                },
            ),
        ]
    )
    await migrated_db_session.commit()
    await migrated_db_session.execute(text("REFRESH MATERIALIZED VIEW public_meme_recommendation_features_mv"))
    await migrated_db_session.commit()

    first = await rollup_recommendation_daily_analytics(
        postgres_session_factory,
        through_date=date(2026, 7, 20),
        lookback_days=1,
    )
    second = await rollup_recommendation_daily_analytics(
        postgres_session_factory,
        through_date=date(2026, 7, 20),
        lookback_days=1,
    )
    migrated_db_session.expire_all()
    rows = await load_recommendation_daily_analytics(
        migrated_db_session,
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 20),
    )

    assert first.aggregate_rows == second.aggregate_rows == 2
    assert len(rows) == 2
    by_algorithm = {row.algorithm_version: row for row in rows}
    personalized = by_algorithm["personalized_v2"]
    assert personalized.profile_version == "taste_v2:test"
    assert personalized.candidate_source == "short_term"
    assert personalized.impression_count == 2
    assert personalized.strong_action_count == 1
    assert personalized.attributed_send_count == 1
    assert personalized.exploration_count == 1
    assert personalized.fallback_count == 0
    assert personalized.metrics["strong_action_rate"] == pytest.approx(0.5)
    assert personalized.metrics["attributed_send_rate"] == pytest.approx(0.5)
    assert personalized.metrics["repeat_within_cooldown_count"] == 2
    assert personalized.metrics["repeat_within_cooldown_rate"] == pytest.approx(1.0)
    assert personalized.metrics["fallback_rate"] == pytest.approx(0.0)
    assert personalized.metrics["exploration_share"] == pytest.approx(0.5)
    assert personalized.metrics["exploration_conversion"] == pytest.approx(1.0)
    assert personalized.metrics["catalog_coverage"] == pytest.approx(1.0)

    fallback = by_algorithm["public_trending_keyset_v1"]
    assert fallback.candidate_source == "trending"
    assert fallback.impression_count == 1
    assert fallback.strong_action_count == 0
    assert fallback.fallback_count == 1
    assert fallback.metrics["strong_action_rate"] == pytest.approx(0.0)
    assert fallback.metrics["repeat_within_cooldown_count"] == 1
    assert fallback.metrics["repeat_within_cooldown_rate"] == pytest.approx(1.0)
    assert fallback.metrics["fallback_rate"] == pytest.approx(1.0)

    persisted_count = await migrated_db_session.scalar(select(func.count()).select_from(RecommendationDailyAggregate))
    assert persisted_count == 2
