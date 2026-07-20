# ruff: noqa: TC003
"""Cursor binding, pool expiry, and privacy-bounded intent tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from memexpert.core.config import Settings
from memexpert.services.recommendations.candidates import CandidateContribution, CandidateSource
from memexpert.services.recommendations.feed_sessions import (
    MAX_FEED_CURSOR_LENGTH,
    CachedFeedCandidate,
    FeedCacheUnavailableError,
    FeedCursorError,
    FeedCursorExpiredError,
    FeedSessionStore,
)
from memexpert.services.recommendations.intent import RecommendationIntentStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.fail = False

    async def get(self, key: str) -> object:
        self._maybe_fail()
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> object:
        self._maybe_fail()
        assert ex > 0
        self.values[key] = value
        return True

    async def sadd(self, key: str, *values: str) -> object:
        self._maybe_fail()
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    async def smembers(self, key: str) -> object:
        self._maybe_fail()
        return self.sets.get(key, set())

    async def expire(self, key: str, seconds: int) -> object:
        self._maybe_fail()
        assert key in self.sets or key in self.values
        return seconds > 0

    async def delete(self, *keys: str) -> object:
        self._maybe_fail()
        for key in keys:
            self.values.pop(key, None)
            self.sets.pop(key, None)
            self.sorted_sets.pop(key, None)
        return len(keys)

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        self._maybe_fail()
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
        self._maybe_fail()
        index = self.sorted_sets.get(key, {})
        ordered = sorted(index, key=lambda value: (index[value], value))
        return ordered[start:] if end == -1 else ordered[start : end + 1]

    def _maybe_fail(self) -> None:
        if self.fail:
            raise ConnectionError("redis unavailable")


class StalledRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.read_cancelled = False

    async def get(self, key: str) -> object:
        del key
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise


def _settings() -> Settings:
    return Settings(
        pipeline_voyage_output_dimensions=2,
        recommendation_feed_pool_ttl_seconds=7200,
        recommendation_feed_pool_max_per_viewer=2,
        recommendation_search_intent_ttl_seconds=7200,
    )


@pytest.mark.asyncio
async def test_feed_cursor_is_bound_to_viewer_filters_and_algorithm() -> None:
    redis = FakeRedis()
    store = FeedSessionStore(redis=redis, settings=_settings())
    viewer_id = uuid.uuid7()
    candidate = CachedFeedCandidate(
        meme_id=uuid.uuid7(),
        score=0.8,
        score_components={"total": 0.8},
        contributions=(
            CandidateContribution(
                source=CandidateSource.SHORT_TERM,
                source_key="short_term",
                rank=1,
                source_score=0.9,
                rrf_contribution=1 / 61,
            ),
        ),
        reason="multi_source_personalized",
    )
    pool = await store.freeze(
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
        request_id="req_test",
        algorithm_version="personalized_v2",
        profile_version="taste_v2:test",
        candidates=[candidate],
    )
    cursor = store.issue_pool_cursor(
        pool,
        next_index=1,
        served_meme_ids=(candidate.meme_id,),
    )

    claims = store.verify_cursor(cursor, viewer_user_id=viewer_id, filter_key="a" * 64)
    loaded = await store.load_pool(claims, viewer_user_id=viewer_id, filter_key="a" * 64)

    assert loaded == pool
    assert claims.served_meme_ids == (candidate.meme_id,)
    assert loaded.candidates[0].contributions == candidate.contributions
    with pytest.raises(FeedCursorError, match="viewer"):
        store.verify_cursor(cursor, viewer_user_id=uuid.uuid7(), filter_key="a" * 64)
    with pytest.raises(FeedCursorError, match="filters"):
        store.verify_cursor(cursor, viewer_user_id=viewer_id, filter_key="b" * 64)


@pytest.mark.asyncio
async def test_feed_cursor_compactly_preserves_bounded_exact_served_ids_across_modes() -> None:
    redis = FakeRedis()
    store = FeedSessionStore(redis=redis, settings=_settings())
    viewer_id = uuid.uuid7()
    served_meme_ids = tuple(uuid.uuid7() for _ in range(200))
    pool = await store.freeze(
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
        request_id="req_test",
        algorithm_version="personalized_v2",
        profile_version=None,
        candidates=[],
    )

    pool_cursor = store.issue_pool_cursor(
        pool,
        next_index=200,
        served_meme_ids=served_meme_ids,
    )
    pool_claims = store.verify_cursor(
        pool_cursor,
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
    )
    trending_cursor = store.issue_trending_cursor(
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
        algorithm_version="personalized_v2",
        last_score=0.5,
        last_meme_id=uuid.uuid7(),
        next_index=202,
        served_meme_ids=pool_claims.served_meme_ids,
    )
    trending_claims = store.verify_cursor(
        trending_cursor,
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
    )

    assert pool_claims.served_meme_ids == served_meme_ids
    assert trending_claims.served_meme_ids == served_meme_ids
    assert len(pool_cursor) <= MAX_FEED_CURSOR_LENGTH
    assert all(str(meme_id) not in pool_cursor for meme_id in served_meme_ids)
    with pytest.raises(ValueError, match="bounded feed pool"):
        store.issue_pool_cursor(
            pool,
            next_index=201,
            served_meme_ids=(*served_meme_ids, uuid.uuid7()),
        )


@pytest.mark.asyncio
async def test_redis_preflight_is_bounded_by_the_recommendation_timeout() -> None:
    redis = StalledRedis()
    settings = _settings().model_copy(update={"recommendation_redis_timeout_seconds": 0.01})
    store = FeedSessionStore(redis=redis, settings=settings)

    with pytest.raises(FeedCacheUnavailableError, match="unavailable"):
        await store.ensure_available()

    assert redis.read_cancelled is True


@pytest.mark.asyncio
async def test_missing_pool_is_a_typed_expired_cursor() -> None:
    redis = FakeRedis()
    store = FeedSessionStore(redis=redis, settings=_settings())
    viewer_id = uuid.uuid7()
    pool = await store.freeze(
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
        request_id="req_test",
        algorithm_version="personalized_v2",
        profile_version=None,
        candidates=[],
    )
    cursor = store.issue_pool_cursor(pool, next_index=0)
    claims = store.verify_cursor(cursor, viewer_user_id=viewer_id, filter_key="a" * 64)
    redis.values.clear()

    with pytest.raises(FeedCursorExpiredError):
        await store.load_pool(claims, viewer_user_id=viewer_id, filter_key="a" * 64)


@pytest.mark.asyncio
async def test_freeze_caps_each_viewer_and_expires_only_the_oldest_cursor() -> None:
    redis = FakeRedis()
    store = FeedSessionStore(redis=redis, settings=_settings())
    viewer_id = uuid.uuid7()

    pools = [
        await store.freeze(
            viewer_user_id=viewer_id,
            filter_key="a" * 64,
            request_id=f"request-{index}",
            algorithm_version="personalized_v2",
            profile_version=None,
            candidates=[],
        )
        for index in range(3)
    ]

    assert sum(key.startswith("recommendation:feed_pool:") for key in redis.values) == 2
    assert [len(index) for index in redis.sorted_sets.values()] == [2]
    first_claims = store.verify_cursor(
        store.issue_pool_cursor(pools[0], next_index=0),
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
    )
    with pytest.raises(FeedCursorExpiredError):
        await store.load_pool(first_claims, viewer_user_id=viewer_id, filter_key="a" * 64)

    for pool in pools[1:]:
        claims = store.verify_cursor(
            store.issue_pool_cursor(pool, next_index=0),
            viewer_user_id=viewer_id,
            filter_key="a" * 64,
        )
        assert (
            await store.load_pool(
                claims,
                viewer_user_id=viewer_id,
                filter_key="a" * 64,
            )
            == pool
        )


@pytest.mark.asyncio
async def test_redis_failure_is_distinct_from_expiry_and_invalidation_removes_all_pools() -> None:
    redis = FakeRedis()
    store = FeedSessionStore(redis=redis, settings=_settings())
    viewer_id = uuid.uuid7()
    first = await store.freeze(
        viewer_user_id=viewer_id,
        filter_key="a" * 64,
        request_id="one",
        algorithm_version="personalized_v2",
        profile_version=None,
        candidates=[],
    )
    second = await store.freeze(
        viewer_user_id=viewer_id,
        filter_key="b" * 64,
        request_id="two",
        algorithm_version="personalized_v2",
        profile_version=None,
        candidates=[],
    )
    await store.invalidate_viewer(viewer_id)
    assert not redis.values

    redis.fail = True
    with pytest.raises(FeedCacheUnavailableError):
        await store.freeze(
            viewer_user_id=viewer_id,
            filter_key="a" * 64,
            request_id="three",
            algorithm_version="personalized_v2",
            profile_version=None,
            candidates=[],
        )
    assert first.pool_id != second.pool_id


@pytest.mark.asyncio
async def test_search_intent_blends_vectors_without_storing_query_text() -> None:
    redis = FakeRedis()
    store = RecommendationIntentStore(redis=redis, settings=_settings())
    viewer_id = uuid.uuid7()

    assert await store.record_successful_search(user_id=viewer_id, query_vector=(1.0, 0.0))
    assert await store.record_successful_search(user_id=viewer_id, query_vector=(0.0, 1.0))
    vector = await store.load(user_id=viewer_id)

    assert vector is not None
    assert vector[0] == pytest.approx(vector[1], rel=1e-3)
    stored_payload = next(iter(redis.values.values()))
    assert "query" not in stored_payload
    assert "vector" in stored_payload
