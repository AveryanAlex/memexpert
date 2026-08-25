"""Integration coverage for PostgreSQL-authoritative personalized-v2 serving."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, text

from memexpert.api.dependencies.auth import get_or_bootstrap_guest_user
from memexpert.api.dependencies.recommendation import get_recommendation_service
from memexpert.core.config import Settings
from memexpert.core.voyage import VoyageEmbeddingResult
from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeSource,
    MemeSourceEngagementSnapshot,
    PipelineStageJournal,
    SourceChannel,
)
from memexpert.models.enums import (
    AnalyticsEventType,
    CollectionKind,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    EmbeddingInputType,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
)
from memexpert.models.recommendation import (
    UserMemeRecommendationState,
    UserRecommendationProfile,
    UserRecommendationProfileSignal,
    UserRecommendationProfileStatus,
)
from memexpert.models.user import AnalyticsEvent
from memexpert.schemas.user import UserRead
from memexpert.services.analytics import AnalyticsService, InteractionEventRefs, InteractionEventWrite
from memexpert.services.collection_service import CollectionService
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchScope, MemeSearchService
from memexpert.services.recommendations.feed_sessions import CachedFeedCandidate, FeedSessionStore
from memexpert.services.recommendations.intent import RecommendationIntentStore
from memexpert.services.recommendations.math import encode_vector
from memexpert.services.recommendations.profile_store import (
    RecommendationProfileStore,
    rebuild_dirty_recommendation_profiles,
)
from memexpert.services.recommendations.profiles import is_profile_materialization_current
from memexpert.services.recommendations.service import RecommendationService
from memexpert.services.recommendations.taste import TastePersonalizationService
from tests.factories import build_full_user

if TYPE_CHECKING:
    from collections.abc import Sequence, Set

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from memexpert.core.qdrant import (
        QdrantNearestSourceQuery,
        QdrantRecommendationSourceResult,
        QdrantRecommendSourceQuery,
    )
    from memexpert.core.search_index_prefilter import SearchIndexPrefilter

pytestmark = pytest.mark.asyncio


class MemoryRecommendationRedis:
    """Minimal feed-session and intent Redis contract with explicit pool state."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        assert ex > 0
        self.values[key] = value
        return True

    async def sadd(self, key: str, *values: str) -> int:
        bucket = self.sets.setdefault(key, set())
        previous = len(bucket)
        bucket.update(values)
        return len(bucket) - previous

    async def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, ()))

    async def expire(self, key: str, seconds: int) -> bool:
        assert key in self.values or key in self.sets
        return seconds > 0

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.values.pop(key, None) is not None)
            deleted += int(self.sets.pop(key, None) is not None)
            deleted += int(self.sorted_sets.pop(key, None) is not None)
        return deleted

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        assert "ZCARD" in script
        assert numkeys == 2
        pool_key, index_key, payload, ttl, score, pool_id, max_pools, pool_prefix = keys_and_args
        assert int(str(ttl)) > 0
        pool_key = str(pool_key)
        index_key = str(index_key)
        pool_id = str(pool_id)
        pool_prefix = str(pool_prefix)
        self.values[pool_key] = str(payload)
        index = self.sorted_sets.setdefault(index_key, {})
        index[pool_id] = float(str(score))
        ordered = sorted(index, key=lambda value: (index[value], value))
        evicted = ordered[: max(0, len(ordered) - int(str(max_pools)))]
        for evicted_id in evicted:
            index.pop(evicted_id, None)
            self.values.pop(f"{pool_prefix}{evicted_id}", None)
        return evicted

    async def zrange(self, key: str, start: int, end: int) -> object:
        index = self.sorted_sets.get(key, {})
        ordered = sorted(index, key=lambda value: (index[value], value))
        return ordered[start:] if end == -1 else ordered[start : end + 1]


class FailingRecommendationRedis(MemoryRecommendationRedis):
    """Fail every Redis operation to exercise PostgreSQL keyset degradation."""

    async def get(self, key: str) -> str | None:
        raise ConnectionError(f"redis unavailable for {key.split(':', maxsplit=1)[0]}")

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        del key, value, ex
        raise ConnectionError("redis unavailable")

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        del script, numkeys, keys_and_args
        raise ConnectionError("redis unavailable")


class ReadFailingRecommendationRedis(MemoryRecommendationRedis):
    """Freeze pools normally, then fail reads during a continuation."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_reads = False

    async def get(self, key: str) -> str | None:
        if self.fail_reads:
            raise ConnectionError("redis unavailable during continuation")
        return await super().get(key)


class EmptyRecommendationQdrant:
    async def query_recommendation_sources(
        self,
        *,
        nearest_queries: Sequence[QdrantNearestSourceQuery] = (),
        recommend_queries: Sequence[QdrantRecommendSourceQuery] = (),
        prefilter: SearchIndexPrefilter | None = None,
        excluded_meme_file_ids: Sequence[uuid.UUID] = (),
    ) -> tuple[QdrantRecommendationSourceResult, ...]:
        del nearest_queries, recommend_queries, prefilter, excluded_meme_file_ids
        return ()


class FailingRecommendationQdrant(EmptyRecommendationQdrant):
    def __init__(self) -> None:
        self.calls = 0

    async def query_recommendation_sources(
        self,
        *,
        nearest_queries: Sequence[QdrantNearestSourceQuery] = (),
        recommend_queries: Sequence[QdrantRecommendSourceQuery] = (),
        prefilter: SearchIndexPrefilter | None = None,
        excluded_meme_file_ids: Sequence[uuid.UUID] = (),
    ) -> tuple[QdrantRecommendationSourceResult, ...]:
        del nearest_queries, recommend_queries, prefilter, excluded_meme_file_ids
        self.calls += 1
        raise RuntimeError("qdrant unavailable")


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    is_nsfw: bool = False,
) -> Meme:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=[],
        is_nsfw=is_nsfw,
        is_public=is_public,
    )
    session.add(meme)
    await session.flush()
    session.add(
        MemeFile(
            id=file_id,
            meme_id=meme_id,
            s3_original_key=f"recommendation-v2/{meme_id}.jpg",
            mime_type="image/jpeg",
            quality_score=0.8,
        )
    )
    await session.flush()
    return meme


async def _add_embedding(
    session: AsyncSession,
    meme: Meme,
    vector: tuple[float, ...],
) -> None:
    session.add(
        EmbeddingCache(
            input_hash=uuid.uuid4().hex,
            input_type=EmbeddingInputType.IMAGE,
            embedding=VoyageEmbeddingResult(
                model="test-voyage",
                dimensions=len(vector),
                vector=vector,
                input_hash=uuid.uuid4().hex,
            ).embedding_bytes,
            model_version="test-voyage",
            source_file_id=meme.primary_file_id,
        )
    )
    await session.flush()


def _raw_event(
    *,
    user_id: uuid.UUID,
    meme_id: uuid.UUID,
    event_type: AnalyticsEventType,
    occurred_at: datetime,
    action: str | None = None,
) -> AnalyticsEvent:
    properties = {} if action is None else {"action": action}
    return AnalyticsEvent(
        user_id=user_id,
        event_type=event_type,
        payload={"refs": {"meme_id": str(meme_id)}, "properties": properties},
        occurred_at=occurred_at,
    )


def _cached_candidates(memes: Sequence[Meme]) -> list[CachedFeedCandidate]:
    return [
        CachedFeedCandidate(
            meme_id=meme.id,
            score=1.0 - index / 100.0,
            score_components={"total": 1.0 - index / 100.0},
            contributions=(),
            reason="integration_fixture",
        )
        for index, meme in enumerate(memes)
    ]


def _build_service(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    redis: MemoryRecommendationRedis | None = None,
    qdrant: EmptyRecommendationQdrant | None = None,
) -> tuple[RecommendationService, MemoryRecommendationRedis]:
    resolved_settings = settings or Settings(
        recommendation_enabled=True,
        recommendation_shadow_mode=False,
        recommendation_canary_percent=100,
    )
    resolved_redis = redis or MemoryRecommendationRedis()
    meme_search = MemeSearchService(session, settings=resolved_settings)
    return (
        RecommendationService(
            session,
            meme_search_service=meme_search,
            qdrant_client=qdrant or EmptyRecommendationQdrant(),
            feed_sessions=FeedSessionStore(redis=resolved_redis, settings=resolved_settings),
            intent_store=RecommendationIntentStore(redis=resolved_redis, settings=resolved_settings),
            settings=resolved_settings,
        ),
        resolved_redis,
    )


async def _refresh_recommendation_views(session: AsyncSession) -> None:
    await session.execute(text("REFRESH MATERIALIZED VIEW public_meme_trends_mv"))
    await session.execute(text("REFRESH MATERIALIZED VIEW public_meme_recommendation_features_mv"))


async def test_recommendation_features_keep_missing_inputs_neutral_and_percentile_ties_equal(
    migrated_db_session: AsyncSession,
) -> None:
    published_at = datetime.now(UTC) - timedelta(days=3)
    captured_at = published_at + timedelta(hours=1)
    channel = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id="recommendation-feature-channel",
        title="Recommendation feature test channel",
    )
    migrated_db_session.add(channel)
    await migrated_db_session.flush()

    missing_snapshot = await _create_meme(migrated_db_session)
    tied_first = await _create_meme(migrated_db_session)
    tied_second = await _create_meme(migrated_db_session)
    higher_metrics = await _create_meme(migrated_db_session)
    memes = (missing_snapshot, tied_first, tied_second, higher_metrics)
    sources = [
        MemeSource(
            file_id=meme.primary_file_id,
            platform=SourcePlatform.TELEGRAM,
            source_id=channel.platform_id,
            post_id=str(index),
            source_alive=True,
            published_at=published_at,
        )
        for index, meme in enumerate(memes, start=1)
    ]
    migrated_db_session.add_all(sources)
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            MemeSourceEngagementSnapshot(
                meme_source_id=source.id,
                captured_at=captured_at,
                capture_reason=SourceEngagementCaptureReason.SCHEDULED,
                view_count=views,
                reaction_count=reactions,
                comment_count=comments,
                forward_count=forwards,
                comments_state=SourceEngagementCommentsState.ENABLED,
                fetch_status=SourceEngagementFetchStatus.SUCCESS,
                source_alive=True,
            )
            for source, views, reactions, comments, forwards in (
                (sources[1], 100, 10, 4, 2),
                (sources[2], 100, 10, 4, 2),
                (sources[3], 200, 80, 10, 10),
            )
        ]
    )
    migrated_db_session.add(
        PipelineStageJournal(
            meme_file_id=tied_first.primary_file_id,
            stage=ContentPipelineStage.TRANSCODE,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            started_at=captured_at,
            finished_at=captured_at,
        )
    )
    await migrated_db_session.flush()
    await _refresh_recommendation_views(migrated_db_session)

    result = await migrated_db_session.execute(
        text(
            """
            SELECT
                meme_id,
                latest_published_at,
                source_channel_ids,
                representative_source_channel_id,
                source_popularity_quantile,
                source_quality_quantile,
                technical_quality,
                popularity_quantile,
                trend_quantile,
                live_source_count,
                coverage_flags
            FROM public_meme_recommendation_features_mv
            WHERE meme_id IN (:missing, :tied_first, :tied_second, :higher)
            """
        ),
        {
            "missing": missing_snapshot.id,
            "tied_first": tied_first.id,
            "tied_second": tied_second.id,
            "higher": higher_metrics.id,
        },
    )
    rows = {row.meme_id: row for row in result.mappings()}

    missing = rows[missing_snapshot.id]
    assert missing.latest_published_at == published_at
    assert missing.source_channel_ids == [channel.id]
    assert missing.representative_source_channel_id == channel.id
    assert missing.live_source_count == 1
    assert missing.source_popularity_quantile == pytest.approx(0.5)
    assert missing.source_quality_quantile == pytest.approx(0.5)
    assert missing.technical_quality == pytest.approx(0.5)
    assert missing.coverage_flags["provenance"] is True
    assert missing.coverage_flags["source_popularity"] is False
    assert missing.coverage_flags["source_quality"] is False
    assert missing.coverage_flags["technical_quality"] is False

    first = rows[tied_first.id]
    second = rows[tied_second.id]
    higher = rows[higher_metrics.id]
    assert first.source_popularity_quantile == pytest.approx(second.source_popularity_quantile)
    assert first.source_quality_quantile == pytest.approx(second.source_quality_quantile)
    assert first.popularity_quantile == pytest.approx(second.popularity_quantile)
    assert first.trend_quantile == pytest.approx(second.trend_quantile)
    assert higher.source_popularity_quantile > first.source_popularity_quantile
    assert higher.source_quality_quantile > first.source_quality_quantile
    assert first.technical_quality == pytest.approx(0.8)
    assert first.coverage_flags["technical_quality"] is True
    assert second.technical_quality == pytest.approx(0.5)
    assert second.coverage_flags["technical_quality"] is False


async def test_shadow_query_timeout_recovers_session_before_serving_trending_fallback(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    fallback = await _create_meme(migrated_db_session)
    await _refresh_recommendation_views(migrated_db_session)
    await migrated_db_session.commit()
    settings = Settings(
        recommendation_enabled=True,
        recommendation_shadow_mode=True,
        recommendation_canary_percent=0,
        recommendation_shadow_timeout_seconds=0.01,
    )
    service, _redis = _build_service(migrated_db_session, settings=settings)
    cancelled = asyncio.Event()

    async def blocked_shadow_generation(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        try:
            await migrated_db_session.execute(text("SELECT pg_sleep(1)"))
        finally:
            cancelled.set()
        raise AssertionError("unreachable shadow query completion")

    monkeypatch.setattr(service, "_generate_pool_candidates", blocked_shadow_generation)

    page = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.PUBLIC),
        limit=10,
    )

    assert cancelled.is_set()
    assert [item.meme.id for item in page.items] == [fallback.id]
    assert page.feed_session_id.startswith("fallback:")


async def test_profile_rebuild_keeps_low_intent_short_term_and_high_intent_or_durable_long_term(
    migrated_db_session: AsyncSession,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    detail_meme = await _create_meme(migrated_db_session)
    download_meme = await _create_meme(migrated_db_session)
    durable_meme = await _create_meme(migrated_db_session)
    old_download_meme = await _create_meme(migrated_db_session)
    old_durable_meme = await _create_meme(migrated_db_session)
    removed_save_meme = await _create_meme(migrated_db_session)
    await _add_embedding(migrated_db_session, detail_meme, (1.0, 0.0))
    await _add_embedding(migrated_db_session, download_meme, (0.0, 1.0))
    await _add_embedding(migrated_db_session, durable_meme, (0.0, 1.0))
    await _add_embedding(migrated_db_session, old_download_meme, (0.0, 1.0))
    await _add_embedding(migrated_db_session, old_durable_meme, (0.0, 1.0))
    await _add_embedding(migrated_db_session, removed_save_meme, (1.0, 1.0))

    observed_at = datetime.now(UTC) - timedelta(minutes=1)
    saved_collection = Collection(
        owner_id=viewer.id,
        title="Active saved preferences",
        kind=CollectionKind.CUSTOM,
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add(saved_collection)
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            CollectionMeme(
                collection_id=saved_collection.id,
                meme_id=durable_meme.id,
                added_by_user_id=viewer.id,
                added_at=observed_at,
            ),
            CollectionMeme(
                collection_id=saved_collection.id,
                meme_id=old_durable_meme.id,
                added_by_user_id=viewer.id,
                added_at=observed_at - timedelta(days=8),
            ),
        ]
    )
    migrated_db_session.add_all(
        [
            _raw_event(
                user_id=viewer.id,
                meme_id=detail_meme.id,
                event_type=AnalyticsEventType.MEME_DETAIL_CLICK,
                occurred_at=observed_at,
            ),
            _raw_event(
                user_id=viewer.id,
                meme_id=download_meme.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at,
            ),
            _raw_event(
                user_id=viewer.id,
                meme_id=old_download_meme.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at - timedelta(days=8),
            ),
            # Historical add/remove telemetry is not durable preference truth.
            _raw_event(
                user_id=viewer.id,
                meme_id=removed_save_meme.id,
                event_type=AnalyticsEventType.MEME_SAVE,
                occurred_at=observed_at - timedelta(seconds=1),
                action="add",
            ),
            _raw_event(
                user_id=viewer.id,
                meme_id=removed_save_meme.id,
                event_type=AnalyticsEventType.MEME_SAVE,
                occurred_at=observed_at,
                action="remove",
            ),
            UserRecommendationProfileStatus(user_id=viewer.id, dirty_since=observed_at),
        ]
    )
    await migrated_db_session.flush()

    settings = Settings.model_validate(
        {
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_cluster_activation_signals": 500,
        }
    )
    store = RecommendationProfileStore(migrated_db_session, settings=settings)

    assert await store.rebuild_user(viewer.id) == 1
    bundle = await store.load_online_bundle(viewer.id)

    materialized = (
        await migrated_db_session.scalars(
            select(UserRecommendationProfileSignal)
            .where(UserRecommendationProfileSignal.user_id == viewer.id)
            .order_by(UserRecommendationProfileSignal.weight.desc())
        )
    ).all()
    assert {signal.meme_id for signal in materialized} == {
        download_meme.id,
        durable_meme.id,
        old_download_meme.id,
        old_durable_meme.id,
    }
    assert detail_meme.id not in {signal.meme_id for signal in materialized}
    assert removed_save_meme.id not in {signal.meme_id for signal in materialized}
    assert bundle.short_term_vector is not None
    # The recent current Save contributes weight 5 alongside the weight-4
    # download. Durable and event positives outside the configured seven-day
    # windows remain long-term only.
    assert bundle.short_term_vector[1] / bundle.short_term_vector[0] == pytest.approx(9.0, rel=1e-3)
    assert len(bundle.long_term_vectors) == 1
    assert bundle.long_term_vectors[0] == pytest.approx((0.0, 1.0), abs=1e-6)
    assert set(bundle.recent_positive_file_ids) == {
        download_meme.primary_file_id,
        durable_meme.primary_file_id,
    }
    assert bundle.strong_positive_count == 4

    profile = await migrated_db_session.scalar(
        select(UserRecommendationProfile).where(UserRecommendationProfile.user_id == viewer.id)
    )
    status = await migrated_db_session.get(UserRecommendationProfileStatus, viewer.id)
    assert profile is not None
    assert profile.signal_count == 4
    assert status is not None
    assert status.dirty_since is None
    assert status.event_watermark == observed_at


async def test_profile_rebuild_materializes_only_the_bounded_top_signal_set(
    migrated_db_session: AsyncSession,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    observed_at = datetime.now(UTC) - timedelta(hours=1)
    memes: list[Meme] = []
    for index in range(25):
        meme = await _create_meme(migrated_db_session)
        await _add_embedding(migrated_db_session, meme, (1.0, 0.0))
        memes.append(meme)
        migrated_db_session.add(
            _raw_event(
                user_id=viewer.id,
                meme_id=meme.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at + timedelta(minutes=index),
            )
        )
    await migrated_db_session.flush()
    settings = Settings.model_validate(
        {
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_long_term_signal_limit": 20,
            "recommendation_cluster_activation_signals": 500,
        }
    )

    rebuilt = await RecommendationProfileStore(
        migrated_db_session,
        settings=settings,
    ).rebuild_user(viewer.id)

    materialized_ids = set(
        await migrated_db_session.scalars(
            select(UserRecommendationProfileSignal.meme_id).where(UserRecommendationProfileSignal.user_id == viewer.id)
        )
    )
    profiles = (
        await migrated_db_session.scalars(
            select(UserRecommendationProfile).where(UserRecommendationProfile.user_id == viewer.id)
        )
    ).all()
    assert rebuilt == 1
    assert len(materialized_ids) == settings.recommendation_long_term_signal_limit
    assert materialized_ids == {meme.id for meme in memes[-20:]}
    assert len(profiles) == 1
    assert profiles[0].signal_count == settings.recommendation_long_term_signal_limit


async def test_profile_rebuild_ignores_events_for_deleted_memes(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    observed_at = datetime.now(UTC) - timedelta(hours=1)
    existing_memes: list[Meme] = []
    for index in range(20):
        existing_meme = await _create_meme(migrated_db_session)
        await _add_embedding(migrated_db_session, existing_meme, (1.0, 0.0))
        existing_memes.append(existing_meme)
        migrated_db_session.add(
            _raw_event(
                user_id=viewer.id,
                meme_id=existing_meme.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at + timedelta(seconds=index),
            )
        )
    migrated_db_session.add_all(
        [
            _raw_event(
                user_id=viewer.id,
                meme_id=uuid.uuid7(),
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at + timedelta(minutes=1),
            ),
            UserRecommendationProfileStatus(user_id=viewer.id, dirty_since=observed_at),
        ]
    )
    await migrated_db_session.commit()
    settings = Settings.model_validate(
        {
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_long_term_signal_limit": 20,
            "recommendation_cluster_activation_signals": 500,
        }
    )

    result = await rebuild_dirty_recommendation_profiles(
        postgres_session_factory,
        settings=settings,
    )

    async with postgres_session_factory() as verification_session:
        materialized_ids = set(
            await verification_session.scalars(
                select(UserRecommendationProfileSignal.meme_id).where(
                    UserRecommendationProfileSignal.user_id == viewer.id
                )
            )
        )
        status = await verification_session.get(UserRecommendationProfileStatus, viewer.id)
    assert result.claimed_users == 1
    assert result.rebuilt_users == 1
    assert result.failed_users == 0
    assert materialized_ids == {meme.id for meme in existing_memes}
    assert status is not None
    assert status.dirty_since is None


@pytest.mark.parametrize(
    ("persisted_model_version", "persisted_profile_version"),
    [
        ("retired-voyage-model", "current-profile:snapshot"),
        ("current-voyage-model", "retired-profile:snapshot"),
    ],
)
async def test_online_personalization_ignores_stale_persisted_profile_versions(
    migrated_db_session: AsyncSession,
    persisted_model_version: str,
    persisted_profile_version: str,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    candidate = await _create_meme(migrated_db_session)
    await _add_embedding(migrated_db_session, candidate, (1.0, 0.0))
    migrated_db_session.add(
        UserRecommendationProfile(
            user_id=viewer.id,
            profile_slot=0,
            model_version=persisted_model_version,
            profile_version=persisted_profile_version,
            signal_count=1,
            total_weight=5.0,
            event_watermark=datetime.now(UTC),
            vector=encode_vector((1.0, 0.0)),
            generated_at=datetime.now(UTC),
        )
    )
    await migrated_db_session.flush()
    settings = Settings.model_validate(
        {
            "pipeline_voyage_model": "current-voyage-model",
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_profile_version": "current-profile",
        }
    )

    bundle = await RecommendationProfileStore(
        migrated_db_session,
        settings=settings,
    ).load_online_bundle(viewer.id)
    taste = await TastePersonalizationService(
        migrated_db_session,
        settings=settings,
    ).score_items(viewer_user_id=viewer.id, meme_ids=(candidate.id,))

    assert bundle.long_term_vectors == ()
    assert bundle.profile_version is None
    assert taste[candidate.id].taste == 0.0
    assert taste[candidate.id].profile_version is None


async def test_stale_profile_versions_are_rebuilt_in_bounded_scheduler_batches(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(minutes=1)
    settings = Settings.model_validate(
        {
            "pipeline_voyage_model": "test-voyage",
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_profile_version": "current-profile",
            "recommendation_cluster_activation_signals": 500,
            "scheduler_recommendation_profile_rebuild_batch_size": 1,
        }
    )
    users = [build_full_user(), build_full_user()]
    migrated_db_session.add_all(users)
    await migrated_db_session.flush()
    for index, user in enumerate(users):
        meme = await _create_meme(migrated_db_session)
        await _add_embedding(migrated_db_session, meme, (1.0, float(index)))
        migrated_db_session.add(
            _raw_event(
                user_id=user.id,
                meme_id=meme.id,
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                occurred_at=observed_at,
            )
        )
        migrated_db_session.add(
            UserRecommendationProfile(
                user_id=user.id,
                profile_slot=0,
                model_version="retired-voyage" if index == 0 else settings.pipeline_voyage_model,
                profile_version=("current-profile:old-snapshot" if index == 0 else "retired-profile:old-snapshot"),
                signal_count=1,
                total_weight=4.0,
                event_watermark=observed_at,
                vector=encode_vector((1.0, 0.0)),
                generated_at=observed_at,
            )
        )
        if index == 0:
            migrated_db_session.add(
                UserRecommendationProfileStatus(
                    user_id=user.id,
                    dirty_since=None,
                    last_rebuilt_at=observed_at,
                    event_watermark=observed_at,
                )
            )
    await migrated_db_session.commit()

    first = await rebuild_dirty_recommendation_profiles(postgres_session_factory, settings=settings)
    async with postgres_session_factory() as verification_session:
        after_first = (
            await verification_session.scalars(
                select(UserRecommendationProfile).where(
                    UserRecommendationProfile.user_id.in_(tuple(user.id for user in users)),
                    UserRecommendationProfile.profile_slot == 0,
                )
            )
        ).all()
    assert first.claimed_users == 1
    assert first.rebuilt_users == 1
    assert first.failed_users == 0
    assert (
        sum(
            is_profile_materialization_current(
                model_version=profile.model_version,
                profile_version=profile.profile_version,
                expected_model_version=settings.pipeline_voyage_model,
                expected_profile_base_version=settings.recommendation_profile_version,
            )
            for profile in after_first
        )
        == 1
    )

    second = await rebuild_dirty_recommendation_profiles(postgres_session_factory, settings=settings)
    async with postgres_session_factory() as verification_session:
        after_second = (
            await verification_session.scalars(
                select(UserRecommendationProfile).where(
                    UserRecommendationProfile.user_id.in_(tuple(user.id for user in users)),
                    UserRecommendationProfile.profile_slot == 0,
                )
            )
        ).all()
        dirty_count = await verification_session.scalar(
            select(func.count())
            .select_from(UserRecommendationProfileStatus)
            .where(
                UserRecommendationProfileStatus.user_id.in_(tuple(user.id for user in users)),
                UserRecommendationProfileStatus.dirty_since.is_not(None),
            )
        )
    assert second.claimed_users == 1
    assert second.rebuilt_users == 1
    assert second.failed_users == 0
    assert all(
        is_profile_materialization_current(
            model_version=profile.model_version,
            profile_version=profile.profile_version,
            expected_model_version=settings.pipeline_voyage_model,
            expected_profile_base_version=settings.recommendation_profile_version,
        )
        for profile in after_second
    )
    assert dirty_count == 0


async def test_durable_mutations_project_state_and_profile_dirtiness_without_analytics(
    migrated_db_session: AsyncSession,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    favorite_meme = await _create_meme(migrated_db_session)
    saved_meme = await _create_meme(migrated_db_session)
    pinned_meme = await _create_meme(migrated_db_session)
    for meme, vector in (
        (favorite_meme, (1.0, 0.0)),
        (saved_meme, (0.0, 1.0)),
        (pinned_meme, (1.0, 1.0)),
    ):
        await _add_embedding(migrated_db_session, meme, vector)

    collection_service = CollectionService(migrated_db_session)
    saved_collection = await collection_service.create_custom_collection(
        owner_user_id=viewer.id,
        title="Durable recommendation preferences",
    )
    favorite_result = await collection_service.favorite_meme_result(
        user_id=viewer.id,
        meme_id=favorite_meme.id,
    )
    saved_result = await collection_service.save_meme_to_collection_result(
        collection_id=saved_collection.id,
        user_id=viewer.id,
        meme_id=saved_meme.id,
    )
    pinned_result = await collection_service.pin_meme_result(
        user_id=viewer.id,
        meme_id=pinned_meme.id,
    )

    assert favorite_result.changed is True
    assert saved_result.changed is True
    assert pinned_result.changed is True
    added_state_rows = (
        await migrated_db_session.execute(
            select(
                UserMemeRecommendationState.meme_id,
                UserMemeRecommendationState.latest_strong_action_at,
            ).where(UserMemeRecommendationState.user_id == viewer.id)
        )
    ).all()
    added_strong_at = {meme_id: strong_at for meme_id, strong_at in added_state_rows}
    assert set(added_strong_at) == {favorite_meme.id, saved_meme.id, pinned_meme.id}
    assert all(strong_at is not None for strong_at in added_strong_at.values())
    assert await migrated_db_session.scalar(select(func.count()).select_from(AnalyticsEvent)) == 0
    assert (
        await migrated_db_session.scalar(
            select(UserRecommendationProfileStatus.dirty_since).where(
                UserRecommendationProfileStatus.user_id == viewer.id
            )
        )
        is not None
    )

    settings = Settings.model_validate(
        {
            "pipeline_voyage_model": "test-voyage",
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_cluster_activation_signals": 500,
        }
    )
    store = RecommendationProfileStore(migrated_db_session, settings=settings)
    assert await store.rebuild_user(viewer.id) == 1
    assert await collection_service.unfavorite_meme(user_id=viewer.id, meme_id=favorite_meme.id) is True
    assert (
        await collection_service.remove_meme_from_collection(
            collection_id=saved_collection.id,
            user_id=viewer.id,
            meme_id=saved_meme.id,
        )
        is True
    )
    assert await collection_service.unpin_meme(user_id=viewer.id, meme_id=pinned_meme.id) is True

    assert (
        await migrated_db_session.scalar(
            select(func.count())
            .select_from(UserRecommendationProfile)
            .where(UserRecommendationProfile.user_id == viewer.id)
        )
        == 0
    )
    assert (
        await migrated_db_session.scalar(
            select(func.count())
            .select_from(UserRecommendationProfileSignal)
            .where(UserRecommendationProfileSignal.user_id == viewer.id)
        )
        == 0
    )

    removed_state_rows = (
        await migrated_db_session.execute(
            select(
                UserMemeRecommendationState.meme_id,
                UserMemeRecommendationState.latest_strong_action_at,
            ).where(UserMemeRecommendationState.user_id == viewer.id)
        )
    ).all()
    assert {meme_id: strong_at for meme_id, strong_at in removed_state_rows} == added_strong_at
    assert (
        await migrated_db_session.scalar(
            select(UserRecommendationProfileStatus.dirty_since).where(
                UserRecommendationProfileStatus.user_id == viewer.id
            )
        )
        is not None
    )
    assert await store.rebuild_user(viewer.id) == 0
    assert (
        await migrated_db_session.scalar(
            select(func.count())
            .select_from(UserRecommendationProfileSignal)
            .where(UserRecommendationProfileSignal.user_id == viewer.id)
        )
        == 0
    )


async def test_exact_cooldown_excludes_an_impression_older_than_eighty_newer_states(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    cooldown_candidate = await _create_meme(migrated_db_session)
    newer_impressions = [await _create_meme(migrated_db_session) for _ in range(80)]
    fresh_candidate = await _create_meme(migrated_db_session)
    now = datetime.now(UTC)
    migrated_db_session.add(
        UserMemeRecommendationState(
            user_id=viewer.id,
            meme_id=cooldown_candidate.id,
            first_seen_at=now - timedelta(hours=2),
            latest_impression_at=now - timedelta(hours=2),
            impression_count=1,
        )
    )
    migrated_db_session.add_all(
        [
            UserMemeRecommendationState(
                user_id=viewer.id,
                meme_id=meme.id,
                first_seen_at=now - timedelta(minutes=80 - index),
                latest_impression_at=now - timedelta(minutes=80 - index),
                impression_count=1,
            )
            for index, meme in enumerate(newer_impressions)
        ]
    )
    await migrated_db_session.flush()
    service, _redis = _build_service(migrated_db_session)

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        return _cached_candidates((cooldown_candidate, fresh_candidate)), None

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    page = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.PUBLIC),
        limit=2,
    )

    state_count = await migrated_db_session.scalar(
        select(func.count())
        .select_from(UserMemeRecommendationState)
        .where(UserMemeRecommendationState.user_id == viewer.id)
    )
    assert state_count == 81
    assert [item.meme.id for item in page.items] == [fresh_candidate.id]
    assert cooldown_candidate.id not in {item.meme.id for item in page.items}


async def test_frozen_cursor_keeps_order_after_page_one_strong_actions(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    memes = [await _create_meme(migrated_db_session) for _ in range(6)]
    service, _redis = _build_service(migrated_db_session)

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], str]:
        return _cached_candidates(memes), "profile:test"

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    filters = MemeSearchFilters(scope=MemeSearchScope.PUBLIC)
    first = await service.home_feed(viewer_user_id=viewer.id, filters=filters, limit=2)
    assert first.next_cursor is not None
    await AnalyticsService(migrated_db_session).record_interaction_events(
        tuple(
            InteractionEventWrite(
                event_type=AnalyticsEventType.MEME_DOWNLOAD,
                event_id=uuid.uuid7(),
                user_id=viewer.id,
                surface="web_home",
                refs=InteractionEventRefs(meme_id=item.meme.id),
            )
            for item in first.items
        )
    )

    second = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=filters,
        limit=2,
        cursor=first.next_cursor,
    )
    assert second.next_cursor is not None
    third = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=filters,
        limit=2,
        cursor=second.next_cursor,
    )

    assert [item.meme.id for item in first.items] == [meme.id for meme in memes[:2]]
    assert [item.meme.id for item in second.items] == [meme.id for meme in memes[2:4]]
    assert [item.meme.id for item in third.items] == [meme.id for meme in memes[4:]]
    assert [item.attribution.rank for item in first.items + second.items + third.items] == list(range(1, 7))
    assert {first.feed_session_id, second.feed_session_id, third.feed_session_id} == {first.feed_session_id}
    assert {first.request_id, second.request_id, third.request_id} == {first.request_id}
    assert third.has_more is False
    assert third.next_cursor is None


async def test_pool_continuation_redis_failure_excludes_exact_served_ids_across_keyset_pages(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    memes = [await _create_meme(migrated_db_session) for _ in range(6)]
    await _refresh_recommendation_views(migrated_db_session)
    redis = ReadFailingRecommendationRedis()
    service, _redis = _build_service(migrated_db_session, redis=redis)
    trending_order = sorted(memes, key=lambda meme: str(meme.id))
    pool_order = [
        trending_order[0],
        trending_order[3],
        trending_order[1],
        trending_order[2],
        trending_order[4],
        trending_order[5],
    ]

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        return _cached_candidates(pool_order), None

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    filters = MemeSearchFilters(scope=MemeSearchScope.PUBLIC)
    first = await service.home_feed(viewer_user_id=viewer.id, filters=filters, limit=2)
    assert first.next_cursor is not None
    assert (
        await migrated_db_session.scalar(
            select(func.count())
            .select_from(UserMemeRecommendationState)
            .where(UserMemeRecommendationState.user_id == viewer.id)
        )
        == 0
    )

    redis.fail_reads = True
    second = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=filters,
        limit=2,
        cursor=first.next_cursor,
    )
    assert second.next_cursor is not None
    third = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=filters,
        limit=2,
        cursor=second.next_cursor,
    )

    first_ids = [item.meme.id for item in first.items]
    second_ids = [item.meme.id for item in second.items]
    third_ids = [item.meme.id for item in third.items]
    assert first_ids == [trending_order[0].id, trending_order[3].id]
    assert second_ids == [trending_order[1].id, trending_order[2].id]
    assert third_ids == [trending_order[4].id, trending_order[5].id]
    assert len(set(first_ids + second_ids + third_ids)) == 6
    assert [item.attribution.rank for item in second.items + third.items] == [3, 4, 5, 6]
    assert second.offset == 2
    assert third.offset == 4
    assert third.has_more is False
    assert third.next_cursor is None


async def test_redis_failure_uses_stable_postgres_trending_keyset_pages(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    memes = [await _create_meme(migrated_db_session) for _ in range(5)]
    await _refresh_recommendation_views(migrated_db_session)
    failing_redis = FailingRecommendationRedis()
    service, _redis = _build_service(migrated_db_session, redis=failing_redis)
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def capture_info(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.services.recommendations.service.logger.info", capture_info)

    candidate_generation_calls = 0

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        nonlocal candidate_generation_calls
        candidate_generation_calls += 1
        return _cached_candidates(memes), None

    hydrated_pages: list[tuple[uuid.UUID, ...]] = []
    original_hydrate = service._meme_search.get_public_meme_cards_by_ids

    async def hydrate_requested_page(
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ):
        hydrated_pages.append(meme_ids)
        return await original_hydrate(
            meme_ids,
            viewer_user_id=viewer_user_id,
            include_nsfw=include_nsfw,
        )

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    monkeypatch.setattr(service._meme_search, "get_public_meme_cards_by_ids", hydrate_requested_page)
    filters = MemeSearchFilters(scope=MemeSearchScope.PUBLIC)

    first = await service.home_feed(viewer_user_id=viewer.id, filters=filters, limit=2)
    assert first.next_cursor is not None
    second = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=filters,
        limit=2,
        cursor=first.next_cursor,
    )

    expected = sorted((meme.id for meme in memes), key=str)
    assert [item.meme.id for item in first.items] == expected[:2]
    assert [item.meme.id for item in second.items] == expected[2:4]
    assert set(item.meme.id for item in first.items).isdisjoint(item.meme.id for item in second.items)
    assert first.feed_session_id.startswith("fallback:")
    assert second.feed_session_id.startswith("fallback:")
    assert all(item.attribution.algorithm_version == "public_trending_keyset_v1" for item in first.items + second.items)
    assert all(
        {source.source for source in item.attribution.candidate_sources} == {"trending"}
        for item in first.items + second.items
    )
    assert [len(ids) for ids in hydrated_pages] == [2, 2]
    assert candidate_generation_calls == 0
    hydration_logs = [extra for message, extra in info_calls if message == "recommendation_page_hydration_completed"]
    assert len(hydration_logs) == 2
    assert all(extra is not None for extra in hydration_logs)
    assert all(extra["algorithm_version"] == "public_trending_keyset_v1" for extra in hydration_logs if extra)
    assert all(extra["page_mode"] == "postgres_trending" for extra in hydration_logs if extra)
    for extra in hydration_logs:
        assert extra is not None
        hydration_latency = extra["hydration_latency_seconds"]
        assert isinstance(hydration_latency, int | float)
        assert hydration_latency >= 0.0
    home_logs = [extra for message, extra in info_calls if message == "recommendation_home_page_completed"]
    assert [extra["fallback_category"] for extra in home_logs if extra] == ["redis", "postgres_trending"]
    assert home_logs[0] is not None
    redis_preflight_latency = home_logs[0]["redis_preflight_latency_seconds"]
    assert isinstance(redis_preflight_latency, int | float)
    assert redis_preflight_latency >= 0.0


async def test_qdrant_failure_degrades_to_postgres_candidates(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    positive = await _create_meme(migrated_db_session)
    fallbacks = [
        await _create_meme(migrated_db_session),
        await _create_meme(migrated_db_session),
    ]
    cooled = await _create_meme(migrated_db_session)
    private = await _create_meme(migrated_db_session, is_public=False)
    nsfw = await _create_meme(migrated_db_session, is_nsfw=True)
    await _add_embedding(migrated_db_session, positive, (1.0, 0.0))
    await migrated_db_session.commit()
    analytics = AnalyticsService(migrated_db_session)
    await analytics.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_DOWNLOAD,
            event_id=uuid.uuid7(),
            user_id=viewer.id,
            surface="web_home",
            refs=InteractionEventRefs(meme_id=positive.id),
        )
    )
    await analytics.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_IMPRESSION,
            event_id=uuid.uuid7(),
            user_id=viewer.id,
            surface="web_home",
            refs=InteractionEventRefs(meme_id=cooled.id),
        )
    )
    await _refresh_recommendation_views(migrated_db_session)
    qdrant = FailingRecommendationQdrant()
    settings = Settings.model_validate(
        {
            "pipeline_voyage_output_dimensions": 2,
            "recommendation_enabled": True,
            "recommendation_shadow_mode": False,
            "recommendation_canary_percent": 100,
        }
    )
    service, _redis = _build_service(
        migrated_db_session,
        settings=settings,
        qdrant=qdrant,
    )
    info_calls: list[tuple[str, dict[str, object] | None]] = []
    warning_calls: list[tuple[str, dict[str, object] | None]] = []

    def capture_info(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    def capture_warning(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        warning_calls.append((message, extra))

    monkeypatch.setattr("memexpert.services.recommendations.service.logger.info", capture_info)
    monkeypatch.setattr("memexpert.services.recommendations.service.logger.warning", capture_warning)

    page = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.PUBLIC),
        limit=10,
    )

    assert qdrant.calls == 1
    returned_ids = {item.meme.id for item in page.items}
    assert returned_ids == {meme.id for meme in fallbacks}
    assert returned_ids.isdisjoint({positive.id, cooled.id, private.id, nsfw.id})
    assert all({source.source for source in item.attribution.candidate_sources} == {"trending"} for item in page.items)
    degraded = next(extra for message, extra in warning_calls if message == "recommendation_qdrant_degraded")
    assert degraded is not None
    assert degraded["surface"] == "web_home"
    assert degraded["fallback_category"] == "qdrant_provider"
    completed = next(
        extra for message, extra in info_calls if message == "recommendation_candidate_generation_completed"
    )
    assert completed is not None
    assert completed["qdrant_degraded"] is True
    assert completed["cold_start"] is False
    assert completed["fallback_category"] == "qdrant_provider"


async def test_continuation_reauthorizes_public_and_nsfw_state(
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    first_meme = await _create_meme(migrated_db_session)
    later_private = await _create_meme(migrated_db_session)
    later_nsfw = await _create_meme(migrated_db_session)
    later_safe = await _create_meme(migrated_db_session)
    memes = (first_meme, later_private, later_nsfw, later_safe)
    service, _redis = _build_service(migrated_db_session)

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        return _cached_candidates(memes), None

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    filters = MemeSearchFilters(scope=MemeSearchScope.PUBLIC, include_nsfw=False)
    first = await service.home_feed(viewer_user_id=viewer.id, filters=filters, limit=1)
    assert first.next_cursor is not None

    later_private.is_public = False
    later_nsfw.is_nsfw = True
    await migrated_db_session.flush()
    continuation = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=filters,
        limit=3,
        cursor=first.next_cursor,
    )

    assert [item.meme.id for item in first.items] == [first_meme.id]
    assert [item.meme.id for item in continuation.items] == [later_safe.id]
    assert later_private.id not in {item.meme.id for item in continuation.items}
    assert later_nsfw.id not in {item.meme.id for item in continuation.items}
    assert continuation.has_more is False


async def test_missing_frozen_pool_maps_to_feed_cursor_expired_410(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    memes = [await _create_meme(migrated_db_session) for _ in range(2)]
    service, redis = _build_service(migrated_db_session)

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        return _cached_candidates(memes), None

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    filters = MemeSearchFilters(scope=MemeSearchScope.PUBLIC)
    first = await service.home_feed(viewer_user_id=viewer.id, filters=filters, limit=1)
    assert first.next_cursor is not None
    redis.values.clear()

    async def override_current_user() -> UserRead:
        return UserRead.model_validate(viewer)

    app.dependency_overrides[get_or_bootstrap_guest_user] = override_current_user
    app.dependency_overrides[get_recommendation_service] = lambda: service
    route_info_calls: list[tuple[str, dict[str, object] | None]] = []

    def capture_route_info(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        route_info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.api.routes.v1.memes.logger.info", capture_route_info)
    try:
        response = await client.get(
            "/api/v1/memes/home-feed",
            params={"limit": 1, "cursor": first.next_cursor},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 410
    assert response.json() == {"detail": "feed_cursor_expired"}
    expired = next(extra for message, extra in route_info_calls if message == "recommendation_feed_cursor_expired")
    assert expired is not None
    assert expired["algorithm_version"] == service.configured_algorithm_version
    assert expired["profile_version"] == "none"
    assert expired["fallback_category"] == "cache_expiry"


async def test_saved_home_feed_reauthorization_drops_newly_unsafe_items_and_rejects_tampering(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = build_full_user()
    migrated_db_session.add(viewer)
    await migrated_db_session.flush()
    safe = await _create_meme(migrated_db_session)
    newly_private = await _create_meme(migrated_db_session)
    newly_nsfw = await _create_meme(migrated_db_session)
    settings = Settings.model_validate(
        {
            "recommendation_enabled": True,
            "recommendation_shadow_mode": False,
            "recommendation_canary_percent": 100,
        }
    )
    service, _redis = _build_service(migrated_db_session, settings=settings)

    async def generate_pool_candidates(**_kwargs: object) -> tuple[list[CachedFeedCandidate], None]:
        return _cached_candidates((safe, newly_private, newly_nsfw)), None

    monkeypatch.setattr(service, "_generate_pool_candidates", generate_pool_candidates)
    first = await service.home_feed(
        viewer_user_id=viewer.id,
        filters=MemeSearchFilters(scope=MemeSearchScope.PUBLIC),
        limit=3,
    )
    newly_private.is_public = False
    newly_nsfw.is_nsfw = True
    await migrated_db_session.flush()

    async def override_current_user() -> UserRead:
        return UserRead.model_validate(viewer)

    app.dependency_overrides[get_or_bootstrap_guest_user] = override_current_user
    app.dependency_overrides[get_recommendation_service] = lambda: service
    payload = {
        "items": [
            {
                "meme_id": str(item.meme.id),
                "attribution_token": item.attribution.attribution_token,
            }
            for item in first.items
        ]
    }
    try:
        response = await client.post(
            "/api/v1/memes/home-feed/reauthorize",
            json=payload,
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        tampered_payload = {
            "items": [
                {
                    "meme_id": str(safe.id),
                    "attribution_token": f"{first.items[0].attribution.attribution_token}x",
                }
            ]
        }
        tampered = await client.post(
            "/api/v1/memes/home-feed/reauthorize",
            json=tampered_payload,
            headers={"x-requested-with": "XMLHttpRequest"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item["meme"]["id"] for item in response.json()["items"]] == [str(safe.id)]
    assert response.json()["items"][0]["attribution"]["attribution_token"] == (
        first.items[0].attribution.attribution_token
    )
    assert tampered.status_code == 422
    assert tampered.json() == {"detail": "feed_attribution_invalid"}
