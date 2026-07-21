# ruff: noqa: TC001,TC003
"""Bounded personalized-v2 feed orchestration with PostgreSQL final authority."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, any_, literal, or_, select, text

from memexpert.core.config import Settings, get_settings
from memexpert.core.qdrant import (
    QdrantNearestSourceQuery,
    QdrantRecommendationClientProtocol,
    QdrantRecommendSourceQuery,
)
from memexpert.core.search_index_prefilter import SearchIndexPrefilter, SearchIndexPrefilterScope
from memexpert.core.voyage import VoyageEmbeddingError, decode_embedding_bytes
from memexpert.models.content import EmbeddingCache, Meme
from memexpert.models.enums import EmbeddingInputType
from memexpert.models.recommendation import UserMemeRecommendationState
from memexpert.schemas.meme import (
    MemeResultAttributionRead,
    PublicMemeSearchResultRead,
    RecommendationCandidateSource,
    RecommendationCandidateSourceContributionRead,
    RecommendationFeedPageRead,
    new_discovery_impression_id,
    new_discovery_request_id,
)
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchService
from memexpert.services.recommendations.attribution import (
    AttributionTokenClaims,
    AttributionTokenMismatchError,
    AttributionTokenService,
    sign_result_attribution,
)
from memexpert.services.recommendations.candidates import (
    CandidateContribution,
    CandidateHit,
    CandidateRanking,
    CandidateSource,
    FusedCandidate,
    fuse_candidate_rankings,
)
from memexpert.services.recommendations.features import RecommendationItemFeatures
from memexpert.services.recommendations.feed_sessions import (
    CachedFeedCandidate,
    FeedCacheUnavailableError,
    FeedCursorClaims,
    FeedSessionStore,
    FrozenFeedPool,
)
from memexpert.services.recommendations.intent import RecommendationIntentStore
from memexpert.services.recommendations.math import clamp01, cosine_similarity
from memexpert.services.recommendations.profile_store import RecommendationProfileStore
from memexpert.services.recommendations.ranking import (
    DiversityPolicy,
    HomeRankingWeights,
    RankableCandidate,
    diversity_rerank,
    score_home_candidates,
)
from memexpert.services.recommendations.rollout import (
    personalized_v2_serving_enabled as _personalized_v2_serving_enabled,
)
from memexpert.services.search_index_sync import SEARCH_INDEX_ALGORITHM_VERSION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
FALLBACK_ALGORITHM_VERSION = "public_trending_keyset_v1"


@dataclass(frozen=True, slots=True)
class _TrendingCandidate:
    meme_id: uuid.UUID
    score: float


class RecommendationService:
    """Generate/freeze home pools and re-authorize each requested page."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        meme_search_service: MemeSearchService,
        qdrant_client: QdrantRecommendationClientProtocol,
        feed_sessions: FeedSessionStore | None = None,
        intent_store: RecommendationIntentStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._meme_search = meme_search_service
        self._qdrant = qdrant_client
        self._settings = settings or get_settings()
        self._feed_sessions = feed_sessions or FeedSessionStore(settings=self._settings)
        self._intent_store = intent_store or RecommendationIntentStore(settings=self._settings)
        self._profiles = RecommendationProfileStore(session, settings=self._settings)
        self._attribution_tokens = AttributionTokenService.from_settings(self._settings)

    @property
    def configured_algorithm_version(self) -> str:
        """Expose the non-sensitive rollout version for route-level telemetry."""

        return self._settings.recommendation_algorithm_version

    async def home_feed(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
        cursor: str | None = None,
        offset: int = 0,
    ) -> RecommendationFeedPageRead:
        """Return a stable cursor page or a keyset MV fallback when Redis fails."""

        started_seconds = time.perf_counter()
        resolved_limit = min(100, max(1, limit))
        filter_key = _filter_key(filters)
        if cursor:
            claims = self._feed_sessions.verify_cursor(
                cursor,
                viewer_user_id=viewer_user_id,
                filter_key=filter_key,
            )
            if claims.mode == "trending":
                return _logged_home_page(
                    await self._trending_fallback_page(
                        viewer_user_id=viewer_user_id,
                        filters=filters,
                        filter_key=filter_key,
                        limit=resolved_limit,
                        claims=claims,
                    ),
                    started_seconds=started_seconds,
                    cache_status="trending_cursor",
                    reason="postgres_trending_continuation",
                    configured_algorithm_version=self.configured_algorithm_version,
                    served_algorithm_version=FALLBACK_ALGORITHM_VERSION,
                )
            redis_pool_started_seconds = time.perf_counter()
            try:
                pool = await self._feed_sessions.load_pool(
                    claims,
                    viewer_user_id=viewer_user_id,
                    filter_key=filter_key,
                )
            except FeedCacheUnavailableError:
                redis_pool_latency_seconds = time.perf_counter() - redis_pool_started_seconds
                return _logged_home_page(
                    await self._trending_fallback_page(
                        viewer_user_id=viewer_user_id,
                        filters=filters,
                        filter_key=filter_key,
                        limit=resolved_limit,
                        claims=claims,
                    ),
                    started_seconds=started_seconds,
                    cache_status="redis_unavailable",
                    reason="pool_continuation_redis_unavailable",
                    configured_algorithm_version=self.configured_algorithm_version,
                    served_algorithm_version=FALLBACK_ALGORITHM_VERSION,
                    redis_pool_latency_seconds=redis_pool_latency_seconds,
                )
            redis_pool_latency_seconds = time.perf_counter() - redis_pool_started_seconds
            return _logged_home_page(
                await self._pool_page(
                    pool,
                    viewer_user_id=viewer_user_id,
                    filters=filters,
                    limit=resolved_limit,
                    start_index=claims.next_index,
                    served_meme_ids=claims.served_meme_ids,
                ),
                started_seconds=started_seconds,
                cache_status="pool_hit",
                reason="frozen_pool_continuation",
                configured_algorithm_version=self.configured_algorithm_version,
                served_algorithm_version=pool.algorithm_version,
                profile_version=pool.profile_version,
                redis_pool_latency_seconds=redis_pool_latency_seconds,
            )

        request_id = new_discovery_request_id()
        if not _personalized_v2_serving_enabled(self._settings, viewer_user_id):
            if self._settings.recommendation_enabled and self._settings.recommendation_shadow_mode:
                try:
                    async with asyncio.timeout(self._settings.recommendation_shadow_timeout_seconds):
                        shadow_candidates, shadow_profile_version = await self._generate_pool_candidates(
                            viewer_user_id=viewer_user_id,
                            filters=filters,
                            request_id=request_id,
                        )
                except Exception as exc:
                    await self._session.rollback()
                    logger.warning(
                        "recommendation_shadow_failed",
                        extra={
                            "event": "recommendation_shadow_failed",
                            "surface": "web_home",
                            "algorithm_version": self._settings.recommendation_algorithm_version,
                            "profile_version": "none",
                            "reason": "shadow_generation_failed",
                            "fallback_category": "shadow_failure",
                            "exception_type": type(exc).__name__,
                            "timeout_seconds": self._settings.recommendation_shadow_timeout_seconds,
                        },
                    )
                else:
                    logger.info(
                        "recommendation_shadow_completed",
                        extra={
                            "event": "recommendation_shadow_completed",
                            "surface": "web_home",
                            "algorithm_version": self._settings.recommendation_algorithm_version,
                            "profile_version": shadow_profile_version or "none",
                            "reason": "shadow_generation_completed",
                            "fallback_category": "shadow",
                            "candidate_count": len(shadow_candidates),
                        },
                    )
            return _logged_home_page(
                await self._trending_fallback_page(
                    viewer_user_id=viewer_user_id,
                    filters=filters,
                    filter_key=filter_key,
                    limit=resolved_limit,
                    legacy_offset=max(0, offset),
                    request_id=request_id,
                ),
                started_seconds=started_seconds,
                cache_status="bypassed",
                reason=("shadow_mode" if self._settings.recommendation_shadow_mode else "disabled_or_outside_canary"),
                configured_algorithm_version=self.configured_algorithm_version,
                served_algorithm_version=FALLBACK_ALGORITHM_VERSION,
            )

        redis_preflight_started_seconds = time.perf_counter()
        try:
            await self._feed_sessions.ensure_available()
        except FeedCacheUnavailableError:
            redis_preflight_latency_seconds = time.perf_counter() - redis_preflight_started_seconds
            return _logged_home_page(
                await self._trending_fallback_page(
                    viewer_user_id=viewer_user_id,
                    filters=filters,
                    filter_key=filter_key,
                    limit=resolved_limit,
                    legacy_offset=max(0, offset),
                    request_id=request_id,
                ),
                started_seconds=started_seconds,
                cache_status="redis_unavailable",
                reason="redis_preflight_unavailable",
                configured_algorithm_version=self.configured_algorithm_version,
                served_algorithm_version=FALLBACK_ALGORITHM_VERSION,
                redis_preflight_latency_seconds=redis_preflight_latency_seconds,
            )
        redis_preflight_latency_seconds = time.perf_counter() - redis_preflight_started_seconds

        candidates, profile_version = await self._generate_pool_candidates(
            viewer_user_id=viewer_user_id,
            filters=filters,
            request_id=request_id,
        )
        redis_pool_started_seconds = time.perf_counter()
        try:
            pool = await self._feed_sessions.freeze(
                viewer_user_id=viewer_user_id,
                filter_key=filter_key,
                request_id=request_id,
                algorithm_version=self._settings.recommendation_algorithm_version,
                profile_version=profile_version,
                candidates=candidates,
            )
        except FeedCacheUnavailableError:
            redis_pool_latency_seconds = time.perf_counter() - redis_pool_started_seconds
            return _logged_home_page(
                await self._trending_fallback_page(
                    viewer_user_id=viewer_user_id,
                    filters=filters,
                    filter_key=filter_key,
                    limit=resolved_limit,
                    legacy_offset=max(0, offset),
                    request_id=request_id,
                ),
                started_seconds=started_seconds,
                cache_status="redis_unavailable",
                reason="pool_freeze_redis_unavailable",
                configured_algorithm_version=self.configured_algorithm_version,
                served_algorithm_version=FALLBACK_ALGORITHM_VERSION,
                profile_version=profile_version,
                redis_preflight_latency_seconds=redis_preflight_latency_seconds,
                redis_pool_latency_seconds=redis_pool_latency_seconds,
            )
        redis_pool_latency_seconds = time.perf_counter() - redis_pool_started_seconds
        return _logged_home_page(
            await self._pool_page(
                pool,
                viewer_user_id=viewer_user_id,
                filters=filters,
                limit=resolved_limit,
                start_index=max(0, offset),
            ),
            started_seconds=started_seconds,
            cache_status="pool_miss",
            reason="generated_frozen_pool",
            configured_algorithm_version=self.configured_algorithm_version,
            served_algorithm_version=pool.algorithm_version,
            profile_version=pool.profile_version,
            redis_preflight_latency_seconds=redis_preflight_latency_seconds,
            redis_pool_latency_seconds=redis_pool_latency_seconds,
        )

    async def reauthorize_feed_items(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        items: tuple[tuple[uuid.UUID, str], ...],
    ) -> list[PublicMemeSearchResultRead]:
        """Rehydrate saved browser state only after fresh PostgreSQL checks."""

        verified: list[tuple[uuid.UUID, str, AttributionTokenClaims]] = []
        seen: set[uuid.UUID] = set()
        for meme_id, token in items[:200]:
            if meme_id in seen:
                continue
            claims = self._attribution_tokens.verify(
                token,
                expected_meme_id=meme_id,
                viewer_user_id=viewer_user_id,
            )
            if claims.surface != "web_home":
                raise AttributionTokenMismatchError(
                    "Attribution token does not belong to the Home surface.",
                )
            seen.add(meme_id)
            verified.append((meme_id, token, claims))

        meme_ids = tuple(meme_id for meme_id, _token, _claims in verified)
        visible_ids = await self._load_currently_visible_ids(meme_ids, filters=filters)
        cards = await self._meme_search.get_public_meme_cards_by_ids(
            tuple(meme_id for meme_id in meme_ids if meme_id in visible_ids),
            viewer_user_id=viewer_user_id,
            include_nsfw=filters.include_nsfw,
        )
        cards_by_id = {card.id: card for card in cards}
        return [
            PublicMemeSearchResultRead(
                meme=cards_by_id[meme_id],
                attribution=_attribution_from_claims(claims, token=token),
            )
            for meme_id, token, claims in verified
            if meme_id in cards_by_id
        ]

    async def _generate_pool_candidates(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        request_id: str | None = None,
    ) -> tuple[list[CachedFeedCandidate], str | None]:
        started_seconds = time.perf_counter()
        bundle = await self._profiles.load_online_bundle(viewer_user_id)
        intent_vector = await self._intent_store.load(user_id=viewer_user_id)
        rankings: list[CandidateRanking] = []
        nearest_queries: list[QdrantNearestSourceQuery] = []
        recommend_queries: list[QdrantRecommendSourceQuery] = []
        if bundle.short_term_vector is not None:
            nearest_queries.append(
                QdrantNearestSourceQuery(
                    source="short_term",
                    vector=bundle.short_term_vector,
                    limit=self._settings.recommendation_short_term_candidate_limit,
                )
            )
        if intent_vector is not None:
            nearest_queries.append(
                QdrantNearestSourceQuery(
                    source="current_intent",
                    vector=intent_vector,
                    limit=self._settings.recommendation_intent_candidate_limit,
                )
            )
        if bundle.long_term_vectors:
            per_profile_limit = max(
                1,
                math.ceil(self._settings.recommendation_long_term_candidate_limit / len(bundle.long_term_vectors)),
            )
            for index, vector in enumerate(bundle.long_term_vectors):
                nearest_queries.append(
                    QdrantNearestSourceQuery(
                        source="long_term_global" if index == 0 else f"long_term_cluster:{index}",
                        vector=vector,
                        limit=per_profile_limit,
                    )
                )
        if bundle.recent_positive_file_ids:
            recommend_queries.append(
                QdrantRecommendSourceQuery(
                    source="multi_positive",
                    positive_meme_file_ids=bundle.recent_positive_file_ids,
                    limit=self._settings.recommendation_multi_positive_candidate_limit,
                )
            )

        cold_start = not nearest_queries and not recommend_queries
        source_candidate_counts: dict[str, int] = {}
        qdrant_degraded = False
        qdrant_started_seconds = time.perf_counter()
        if nearest_queries or recommend_queries:
            try:
                source_results = await self._qdrant.query_recommendation_sources(
                    nearest_queries=nearest_queries,
                    recommend_queries=recommend_queries,
                    prefilter=_recommendation_prefilter(filters, viewer_user_id=viewer_user_id),
                    excluded_meme_file_ids=bundle.recent_positive_file_ids,
                )
            except Exception as exc:
                qdrant_degraded = True
                logger.warning(
                    "recommendation_qdrant_degraded",
                    extra={
                        "event": "recommendation_qdrant_degraded",
                        "surface": "web_home",
                        "algorithm_version": self._settings.recommendation_algorithm_version,
                        "profile_version": bundle.profile_version or "none",
                        "reason": "qdrant_lookup_failed",
                        "fallback_category": "qdrant_provider",
                        "exception_type": type(exc).__name__,
                    },
                )
                source_results = ()
            for result in source_results:
                source_candidate_counts[result.source] = len(result.matches)
                source, source_key, group = _candidate_source_for_qdrant(result.source)
                rankings.append(
                    CandidateRanking(
                        source=source,
                        source_key=source_key,
                        normalization_group=group,
                        hits=tuple(
                            CandidateHit(meme_id=match.meme_id, score=match.candidate_score) for match in result.matches
                        ),
                    )
                )
        qdrant_latency_seconds = time.perf_counter() - qdrant_started_seconds

        postgres_candidates_started_seconds = time.perf_counter()
        trending = await self._load_trending_candidates(
            viewer_user_id=viewer_user_id,
            filters=filters,
            limit=self._settings.recommendation_trending_candidate_limit,
        )
        exploration = await self._load_exploration_candidates(
            viewer_user_id=viewer_user_id,
            filters=filters,
            limit=self._settings.recommendation_exploration_candidate_limit,
        )
        source_candidate_counts[CandidateSource.TRENDING.value] = len(trending)
        source_candidate_counts[CandidateSource.EXPLORATION.value] = len(exploration)
        postgres_candidate_latency_seconds = time.perf_counter() - postgres_candidates_started_seconds
        rankings.extend(
            [
                CandidateRanking(
                    source=CandidateSource.TRENDING,
                    hits=tuple(CandidateHit(item.meme_id, item.score) for item in trending),
                ),
                CandidateRanking(
                    source=CandidateSource.EXPLORATION,
                    hits=tuple(CandidateHit(item.meme_id, item.score) for item in exploration),
                ),
            ]
        )
        fusion_started_seconds = time.perf_counter()
        fused = fuse_candidate_rankings(
            rankings,
            constant=self._settings.recommendation_rrf_constant,
            limit=self._settings.recommendation_feed_candidate_limit,
        )
        union_count = len(fused)
        fusion_latency_seconds = time.perf_counter() - fusion_started_seconds
        filter_started_seconds = time.perf_counter()
        excluded = await self._load_exact_excluded_meme_ids(viewer_user_id)
        fused = [candidate for candidate in fused if candidate.meme_id not in excluded]
        exploration_ids = {candidate.meme_id for candidate in exploration}
        features = await self._load_features(tuple(candidate.meme_id for candidate in fused))
        embeddings = await self._load_candidate_embeddings(tuple(candidate.meme_id for candidate in fused))
        filter_feature_latency_seconds = time.perf_counter() - filter_started_seconds
        profile_vectors = tuple(
            vector for vector in (bundle.short_term_vector, *bundle.long_term_vectors) if vector is not None
        )
        rankable = [
            _rankable_candidate(
                candidate,
                features=features[candidate.meme_id],
                embedding=embeddings.get(candidate.meme_id),
                profile_vectors=profile_vectors,
                intent_vector=intent_vector,
                is_exploration=candidate.meme_id in exploration_ids,
            )
            for candidate in fused
            if candidate.meme_id in features
        ]
        ranking_started_seconds = time.perf_counter()
        scored = score_home_candidates(
            rankable,
            weights=_home_weights(self._settings),
            freshness_half_life_days=self._settings.recommendation_freshness_half_life_days,
            user_median_popularity_quantile=bundle.median_popularity_quantile,
            qualifying_positive_count=bundle.strong_positive_count,
        )[: self._settings.recommendation_feed_rerank_limit]
        reranked = diversity_rerank(
            scored,
            limit=self._settings.recommendation_feed_pool_limit,
            policy=_diversity_policy(self._settings),
        )
        ranking_latency_seconds = time.perf_counter() - ranking_started_seconds
        filtered_count = len(rankable)
        logger.info(
            "recommendation_candidate_generation_completed",
            extra={
                "event": "recommendation_candidate_generation_completed",
                "request_id": request_id,
                "surface": "web_home",
                "algorithm_version": self._settings.recommendation_algorithm_version,
                "profile_version": bundle.profile_version or "none",
                "cold_start": cold_start,
                "qdrant_degraded": qdrant_degraded,
                "reason": (
                    "qdrant_lookup_failed"
                    if qdrant_degraded
                    else "cold_start_no_personalized_sources"
                    if cold_start
                    else "personalized_sources_available"
                ),
                "fallback_category": ("qdrant_provider" if qdrant_degraded else "cold_start" if cold_start else "none"),
                "candidate_source_counts": source_candidate_counts,
                "candidate_union_count": union_count,
                "post_filter_count": filtered_count,
                "filtered_ratio": ((union_count - filtered_count) / union_count if union_count > 0 else 0.0),
                "rerank_count": len(scored),
                "pool_count": len(reranked),
                "qdrant_latency_seconds": qdrant_latency_seconds,
                "postgres_candidate_latency_seconds": postgres_candidate_latency_seconds,
                "fusion_latency_seconds": fusion_latency_seconds,
                "filter_feature_latency_seconds": filter_feature_latency_seconds,
                "ranking_diversity_latency_seconds": ranking_latency_seconds,
                "total_latency_seconds": time.perf_counter() - started_seconds,
            },
        )
        return (
            [
                CachedFeedCandidate(
                    meme_id=candidate.meme_id,
                    score=candidate.total_score,
                    score_components=candidate.score_components or {},
                    contributions=candidate.contributions,
                    reason="quality_exploration" if candidate.is_exploration else "multi_source_personalized",
                    is_exploration=candidate.is_exploration,
                )
                for candidate in reranked
            ],
            bundle.profile_version,
        )

    async def _pool_page(
        self,
        pool: FrozenFeedPool,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
        start_index: int,
        served_meme_ids: tuple[uuid.UUID, ...] = (),
    ) -> RecommendationFeedPageRead:
        page_started_seconds = time.perf_counter()
        authorization_started_seconds = time.perf_counter()
        excluded = await self._load_exact_excluded_meme_ids(viewer_user_id)
        remaining = pool.candidates[min(start_index, len(pool.candidates)) :]
        visible_ids = await self._load_currently_visible_ids(
            tuple(candidate.meme_id for candidate in remaining if candidate.meme_id not in excluded),
            filters=filters,
        )
        authorization_latency_seconds = time.perf_counter() - authorization_started_seconds
        selected: list[tuple[int, CachedFeedCandidate]] = []
        next_index = min(start_index, len(pool.candidates))
        for relative_index, candidate in enumerate(remaining):
            next_index = start_index + relative_index + 1
            if candidate.meme_id in excluded or candidate.meme_id not in visible_ids:
                continue
            selected.append((start_index + relative_index, candidate))
            if len(selected) >= limit:
                break
        has_more = any(
            candidate.meme_id not in excluded and candidate.meme_id in visible_ids
            for candidate in pool.candidates[next_index:]
        )
        hydration_started_seconds = time.perf_counter()
        cards = await self._meme_search.get_public_meme_cards_by_ids(
            tuple(candidate.meme_id for _index, candidate in selected),
            viewer_user_id=viewer_user_id,
            include_nsfw=filters.include_nsfw,
        )
        hydration_latency_seconds = time.perf_counter() - hydration_started_seconds
        cards_by_id = {card.id: card for card in cards}
        items = [
            PublicMemeSearchResultRead(
                meme=cards_by_id[candidate.meme_id],
                attribution=self._signed_attribution(
                    candidate,
                    meme_id=candidate.meme_id,
                    viewer_user_id=viewer_user_id,
                    request_id=pool.request_id,
                    rank=index + 1,
                    profile_version=pool.profile_version,
                    surface="web_home",
                ),
            )
            for index, candidate in selected
            if candidate.meme_id in cards_by_id
        ]
        continued_served_meme_ids = tuple(dict.fromkeys((*served_meme_ids, *(item.meme.id for item in items))))
        next_cursor = (
            self._feed_sessions.issue_pool_cursor(
                pool,
                next_index=next_index,
                served_meme_ids=continued_served_meme_ids,
            )
            if has_more
            else None
        )
        page = RecommendationFeedPageRead(
            items=items,
            request_id=pool.request_id,
            feed_session_id=str(pool.pool_id),
            next_cursor=next_cursor,
            expires_at=pool.expires_at,
            has_more=has_more,
            limit=limit,
            offset=start_index,
            total=len(pool.candidates),
        )
        logger.info(
            "recommendation_page_hydration_completed",
            extra={
                "event": "recommendation_page_hydration_completed",
                "request_id": pool.request_id,
                "feed_session_id": str(pool.pool_id),
                "surface": "web_home",
                "algorithm_version": pool.algorithm_version,
                "configured_algorithm_version": self.configured_algorithm_version,
                "profile_version": pool.profile_version or "none",
                "page_mode": "frozen_pool",
                "reason": "frozen_pool_page",
                "fallback_category": "none",
                "authorization_latency_seconds": authorization_latency_seconds,
                "hydration_latency_seconds": hydration_latency_seconds,
                "total_latency_seconds": time.perf_counter() - page_started_seconds,
                "scanned_count": len(remaining),
                "selected_count": len(selected),
                "returned_count": len(items),
            },
        )
        return page

    async def _trending_fallback_page(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        filter_key: str,
        limit: int,
        claims: FeedCursorClaims | None = None,
        legacy_offset: int = 0,
        request_id: str | None = None,
    ) -> RecommendationFeedPageRead:
        page_started_seconds = time.perf_counter()
        request_id = request_id or new_discovery_request_id()
        postgres_candidate_started_seconds = time.perf_counter()
        rows = await self._load_trending_candidates(
            viewer_user_id=viewer_user_id,
            filters=filters,
            limit=limit + 1,
            last_score=claims.last_score if claims else None,
            last_meme_id=claims.last_meme_id if claims else None,
            offset=legacy_offset if claims is None else 0,
            excluded_meme_ids=claims.served_meme_ids if claims else (),
        )
        postgres_candidate_latency_seconds = time.perf_counter() - postgres_candidate_started_seconds
        page_rows = rows[:limit]
        hydration_started_seconds = time.perf_counter()
        cards = await self._meme_search.get_public_meme_cards_by_ids(
            tuple(row.meme_id for row in page_rows),
            viewer_user_id=viewer_user_id,
            include_nsfw=filters.include_nsfw,
        )
        hydration_latency_seconds = time.perf_counter() - hydration_started_seconds
        cards_by_id = {card.id: card for card in cards}
        start_rank = claims.next_index if claims else legacy_offset
        items = []
        for index, row in enumerate(page_rows, start=start_rank + 1):
            candidate = CachedFeedCandidate(
                meme_id=row.meme_id,
                score=row.score,
                score_components={"trending": row.score, "total": row.score},
                contributions=(
                    CandidateContribution(
                        source=CandidateSource.TRENDING,
                        source_key=CandidateSource.TRENDING.value,
                        rank=index,
                        source_score=row.score,
                        rrf_contribution=1.0 / (self._settings.recommendation_rrf_constant + index),
                    ),
                ),
                reason="redis_or_personalization_fallback",
            )
            card = cards_by_id.get(row.meme_id)
            if card is None:
                continue
            items.append(
                PublicMemeSearchResultRead(
                    meme=card,
                    attribution=self._signed_attribution(
                        candidate,
                        meme_id=row.meme_id,
                        viewer_user_id=viewer_user_id,
                        request_id=request_id,
                        rank=index,
                        profile_version=None,
                        surface="web_home",
                        algorithm_version=FALLBACK_ALGORITHM_VERSION,
                    ),
                )
            )
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self._feed_sessions.issue_trending_cursor(
                viewer_user_id=viewer_user_id,
                filter_key=filter_key,
                algorithm_version=self._settings.recommendation_algorithm_version,
                last_score=last.score,
                last_meme_id=last.meme_id,
                next_index=start_rank + len(page_rows),
                served_meme_ids=claims.served_meme_ids if claims else (),
            )
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.recommendation_feed_pool_ttl_seconds)
        page = RecommendationFeedPageRead(
            items=items,
            request_id=request_id,
            feed_session_id=f"fallback:{request_id}",
            next_cursor=next_cursor,
            expires_at=expires_at,
            has_more=has_more,
            limit=limit,
            offset=start_rank,
            total=start_rank + len(rows),
        )
        logger.info(
            "recommendation_page_hydration_completed",
            extra={
                "event": "recommendation_page_hydration_completed",
                "request_id": request_id,
                "feed_session_id": page.feed_session_id,
                "surface": "web_home",
                "algorithm_version": FALLBACK_ALGORITHM_VERSION,
                "configured_algorithm_version": self.configured_algorithm_version,
                "profile_version": "none",
                "page_mode": "postgres_trending",
                "reason": "mv_keyset_fallback_page",
                "fallback_category": "postgres_trending",
                "postgres_candidate_latency_seconds": postgres_candidate_latency_seconds,
                "hydration_latency_seconds": hydration_latency_seconds,
                "total_latency_seconds": time.perf_counter() - page_started_seconds,
                "selected_count": len(page_rows),
                "returned_count": len(items),
            },
        )
        return page

    async def _load_trending_candidates(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
        last_score: float | None = None,
        last_meme_id: uuid.UUID | None = None,
        offset: int = 0,
        excluded_meme_ids: tuple[uuid.UUID, ...] = (),
    ) -> list[_TrendingCandidate]:
        result = await self._session.execute(
            text(_TRENDING_CANDIDATES_SQL),
            {
                **_filter_params(filters),
                "viewer_user_id": viewer_user_id,
                "impression_since": datetime.now(UTC)
                - timedelta(hours=self._settings.recommendation_impression_cooldown_hours),
                "strong_since": datetime.now(UTC)
                - timedelta(hours=self._settings.recommendation_strong_positive_cooldown_hours),
                "last_score": last_score,
                "last_meme_id": last_meme_id,
                "excluded_meme_ids": list(excluded_meme_ids),
                "limit": limit,
                "offset": offset,
            },
        )
        return [
            _TrendingCandidate(meme_id=cast("uuid.UUID", row.meme_id), score=float(row.score or 0.0)) for row in result
        ]

    async def _load_exploration_candidates(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
    ) -> list[_TrendingCandidate]:
        result = await self._session.execute(
            text(_EXPLORATION_CANDIDATES_SQL),
            {
                **_filter_params(filters),
                "viewer_user_id": viewer_user_id,
                "impression_since": datetime.now(UTC)
                - timedelta(hours=self._settings.recommendation_impression_cooldown_hours),
                "strong_since": datetime.now(UTC)
                - timedelta(hours=self._settings.recommendation_strong_positive_cooldown_hours),
                "min_source_quality": self._settings.recommendation_exploration_min_source_quality,
                "min_technical_quality": self._settings.recommendation_exploration_min_technical_quality,
                "max_popularity": self._settings.recommendation_exploration_max_popularity_quantile,
                "limit": limit,
            },
        )
        return [
            _TrendingCandidate(meme_id=cast("uuid.UUID", row.meme_id), score=float(row.score or 0.0)) for row in result
        ]

    async def _load_exact_excluded_meme_ids(self, viewer_user_id: uuid.UUID) -> set[uuid.UUID]:
        now = datetime.now(UTC)
        result = await self._session.scalars(
            select(UserMemeRecommendationState.meme_id).where(
                UserMemeRecommendationState.user_id == viewer_user_id,
                or_(
                    UserMemeRecommendationState.latest_impression_at
                    >= now - timedelta(hours=self._settings.recommendation_impression_cooldown_hours),
                    UserMemeRecommendationState.latest_strong_action_at
                    >= now - timedelta(hours=self._settings.recommendation_strong_positive_cooldown_hours),
                ),
            )
        )
        return set(result)

    async def _load_currently_visible_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        filters: MemeSearchFilters,
    ) -> set[uuid.UUID]:
        if not meme_ids:
            return set()
        statement = select(Meme.id).where(Meme.id.in_(meme_ids), Meme.is_public.is_(True))
        if filters.language is not None:
            statement = statement.where(Meme.language == filters.language)
        if filters.media_type is not None:
            statement = statement.where(Meme.media_type == filters.media_type)
        if not filters.include_nsfw:
            statement = statement.where(Meme.is_nsfw.is_(False))
        for tag in filters.tags:
            statement = statement.where(literal(tag) == any_(Meme.tags))
        return set(await self._session.scalars(statement))

    async def _load_features(
        self,
        meme_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, RecommendationItemFeatures]:
        if not meme_ids:
            return {}
        result = await self._session.execute(
            text(_FEATURES_SQL),
            {"meme_ids": meme_ids},
        )
        features: dict[uuid.UUID, RecommendationItemFeatures] = {}
        for row in result:
            meme_id = cast("uuid.UUID", row.meme_id)
            coverage = row.coverage_flags if isinstance(row.coverage_flags, dict) else {}
            features[meme_id] = RecommendationItemFeatures(
                meme_id=meme_id,
                latest_published_at=cast("datetime", row.latest_published_at),
                source_channel_ids=tuple(row.source_channel_ids or ()),
                representative_source_channel_id=cast("uuid.UUID | None", row.representative_source_channel_id),
                source_popularity_quantile=float(row.source_popularity_quantile),
                source_quality_quantile=float(row.source_quality_quantile),
                technical_quality=float(row.technical_quality),
                platform_response=float(row.platform_response),
                popularity_quantile=float(row.popularity_quantile),
                trend_quantile=float(row.trend_quantile),
                template_id=cast("uuid.UUID | None", row.template_id),
                coverage={str(key): bool(value) for key, value in coverage.items()},
            )
        return features

    async def _load_candidate_embeddings(
        self,
        meme_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        if not meme_ids:
            return {}
        result = await self._session.execute(
            select(Meme.id, EmbeddingCache.embedding)
            .join(
                EmbeddingCache,
                and_(
                    EmbeddingCache.source_file_id == Meme.primary_file_id,
                    EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
                ),
            )
            .where(Meme.id.in_(meme_ids))
            .order_by(Meme.id, EmbeddingCache.created_at.desc())
        )
        embeddings: dict[uuid.UUID, tuple[float, ...]] = {}
        for meme_id, raw_vector in result:
            if meme_id in embeddings:
                continue
            try:
                embeddings[meme_id] = decode_embedding_bytes(
                    raw_vector,
                    dimensions=self._settings.pipeline_voyage_output_dimensions,
                )
            except VoyageEmbeddingError:
                continue
        return embeddings

    def _signed_attribution(
        self,
        candidate: CachedFeedCandidate,
        *,
        meme_id: uuid.UUID,
        viewer_user_id: uuid.UUID,
        request_id: str,
        rank: int,
        profile_version: str | None,
        surface: str,
        algorithm_version: str | None = None,
    ) -> MemeResultAttributionRead:
        attribution = MemeResultAttributionRead(
            request_id=request_id,
            impression_id=new_discovery_impression_id(),
            surface=surface,
            source_algorithm="personalized_recommendations",
            rank=rank,
            algorithm_version=algorithm_version or self._settings.recommendation_algorithm_version,
            profile_version=profile_version,
            score=candidate.score,
            score_components=candidate.score_components,
            candidate_sources=[_to_read_contribution(contribution) for contribution in candidate.contributions],
            reason=candidate.reason,
        )
        return sign_result_attribution(
            attribution,
            meme_id=meme_id,
            viewer_user_id=viewer_user_id,
            settings=self._settings,
        )


def _logged_home_page(
    page: RecommendationFeedPageRead,
    *,
    started_seconds: float,
    cache_status: str,
    reason: str,
    configured_algorithm_version: str,
    served_algorithm_version: str,
    profile_version: str | None = None,
    redis_preflight_latency_seconds: float | None = None,
    redis_pool_latency_seconds: float | None = None,
) -> RecommendationFeedPageRead:
    """Emit privacy-bounded page telemetry and return the unchanged DTO."""

    first_attribution = page.items[0].attribution if page.items else None
    logger.info(
        "recommendation_home_page_completed",
        extra={
            "event": "recommendation_home_page_completed",
            "request_id": page.request_id,
            "feed_session_id": page.feed_session_id,
            "surface": "web_home",
            "algorithm_version": (
                first_attribution.algorithm_version if first_attribution is not None else served_algorithm_version
            ),
            "configured_algorithm_version": configured_algorithm_version,
            "profile_version": (
                first_attribution.profile_version or "none"
                if first_attribution is not None
                else profile_version or "none"
            ),
            "cache_status": cache_status,
            "degraded_mode": cache_status in {"bypassed", "redis_unavailable", "trending_cursor"},
            "reason": reason,
            "fallback_category": _home_fallback_category(reason),
            "returned_count": len(page.items),
            "has_more": page.has_more,
            "next_index": page.offset + len(page.items),
            "redis_preflight_latency_seconds": redis_preflight_latency_seconds,
            "redis_pool_latency_seconds": redis_pool_latency_seconds,
            "total_latency_seconds": time.perf_counter() - started_seconds,
        },
    )
    return page


def _home_fallback_category(reason: str) -> str:
    if "redis" in reason:
        return "redis"
    if reason == "postgres_trending_continuation":
        return "postgres_trending"
    if reason == "shadow_mode":
        return "shadow"
    if reason == "disabled_or_outside_canary":
        return "rollout_gate"
    return "none"


def _attribution_from_claims(
    claims: AttributionTokenClaims,
    *,
    token: str,
) -> MemeResultAttributionRead:
    return MemeResultAttributionRead(
        request_id=claims.request_id,
        impression_id=claims.impression_id,
        surface=claims.surface,
        source_algorithm=claims.source_algorithm,
        rank=claims.rank,
        source_meme_id=claims.source_meme_id,
        algorithm_version=claims.algorithm_version,
        profile_version=claims.profile_version,
        score=claims.score,
        candidate_sources=claims.candidate_sources,
        reason=claims.reason,
        attribution_token=token,
    )


def _rankable_candidate(
    candidate: FusedCandidate,
    *,
    features: RecommendationItemFeatures,
    embedding: tuple[float, ...] | None,
    profile_vectors: tuple[tuple[float, ...], ...],
    intent_vector: tuple[float, ...] | None,
    is_exploration: bool,
) -> RankableCandidate:
    personal_fit = 0.0
    current_intent = 0.0
    if embedding is not None and profile_vectors:
        personal_fit = max(clamp01(cosine_similarity(embedding, vector)) for vector in profile_vectors)
    if embedding is not None and intent_vector is not None:
        current_intent = clamp01(cosine_similarity(embedding, intent_vector))
    return RankableCandidate(
        meme_id=candidate.meme_id,
        fused_score=candidate.fused_score,
        personal_fit=personal_fit,
        current_intent=current_intent,
        features=features,
        embedding=embedding,
        contributions=tuple(candidate.contributions),
        is_exploration=is_exploration,
    )


def _candidate_source_for_qdrant(source: str) -> tuple[CandidateSource, str, str | None]:
    if source == "short_term":
        return CandidateSource.SHORT_TERM, source, None
    if source == "current_intent":
        return CandidateSource.CURRENT_INTENT, source, None
    if source == "long_term_global":
        return CandidateSource.LONG_TERM_GLOBAL, source, None
    if source.startswith("long_term_cluster:"):
        return CandidateSource.LONG_TERM_CLUSTER, source, "long_term_clusters"
    return CandidateSource.MULTI_POSITIVE, source, None


def _to_read_contribution(contribution: object) -> RecommendationCandidateSourceContributionRead:
    typed = cast("CandidateContributionLike", contribution)
    source = (
        RecommendationCandidateSource.MULTI_POSITIVE
        if typed.source is CandidateSource.MULTI_POSITIVE
        else RecommendationCandidateSource(typed.source.value)
    )
    return RecommendationCandidateSourceContributionRead(
        source=source,
        rank=typed.rank,
        score=typed.source_score,
        contribution=typed.rrf_contribution,
    )


class CandidateContributionLike:
    source: CandidateSource
    rank: int
    source_score: float
    rrf_contribution: float


def _home_weights(settings: Settings) -> HomeRankingWeights:
    return HomeRankingWeights(
        personal_fit=settings.recommendation_personal_fit_weight,
        current_intent=settings.recommendation_current_intent_weight,
        fused_candidate=settings.recommendation_fused_candidate_weight,
        quality=settings.recommendation_quality_weight,
        freshness=settings.recommendation_freshness_weight,
        popularity_alignment=settings.recommendation_popularity_alignment_weight,
        exploration=settings.recommendation_exploration_weight,
    )


def _diversity_policy(settings: Settings) -> DiversityPolicy:
    return DiversityPolicy(
        semantic_penalty=settings.recommendation_diversity_semantic_penalty,
        source_penalty=settings.recommendation_diversity_source_penalty,
        template_penalty=settings.recommendation_diversity_template_penalty,
        source_cap_per_20=settings.recommendation_diversity_source_cap_per_20,
        template_cap_per_20=settings.recommendation_diversity_template_cap_per_20,
        exploration_slot_interval=settings.recommendation_exploration_slot_interval,
    )


def _recommendation_prefilter(
    filters: MemeSearchFilters,
    *,
    viewer_user_id: uuid.UUID,
) -> SearchIndexPrefilter:
    return SearchIndexPrefilter(
        scope=SearchIndexPrefilterScope.PUBLIC,
        search_index_algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
        viewer_user_id=str(viewer_user_id),
        media_type=filters.media_type.value if filters.media_type else None,
        language=filters.language.value if filters.language else None,
        include_nsfw=filters.include_nsfw,
        tags=filters.tags,
    )


def _filter_key(filters: MemeSearchFilters) -> str:
    import json

    payload = {
        "language": filters.language.value if filters.language else None,
        "media_type": filters.media_type.value if filters.media_type else None,
        "include_nsfw": filters.include_nsfw,
        "tags": list(filters.tags),
        "scope": filters.scope.value if filters.scope else "public",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _filter_params(filters: MemeSearchFilters) -> dict[str, object]:
    return {
        "language": filters.language.value if filters.language else None,
        "media_type": filters.media_type.value if filters.media_type else None,
        "include_nsfw": filters.include_nsfw,
        "tags": list(filters.tags),
        "tag_count": len(filters.tags),
    }


_SAFETY_FILTER_SQL = """
    meme.is_public IS TRUE
    AND (:include_nsfw OR meme.is_nsfw IS FALSE)
    AND (CAST(:language AS text) IS NULL OR meme.language::text = CAST(:language AS text))
    AND (CAST(:media_type AS text) IS NULL OR meme.media_type::text = CAST(:media_type AS text))
    AND (:tag_count = 0 OR meme.tags @> CAST(:tags AS varchar[]))
"""

_COOLDOWN_FILTER_SQL = """
    NOT EXISTS (
        SELECT 1
        FROM user_meme_recommendation_state state
        WHERE state.user_id = :viewer_user_id
          AND state.meme_id = meme.id
          AND (
              state.latest_impression_at >= :impression_since
              OR state.latest_strong_action_at >= :strong_since
          )
    )
"""

_TRENDING_CANDIDATES_SQL = f"""
SELECT
    trend.meme_id,
    trend.trending_score::double precision AS score
FROM public_meme_trends_mv trend
JOIN memes meme ON meme.id = trend.meme_id
WHERE {_SAFETY_FILTER_SQL}
  AND {_COOLDOWN_FILTER_SQL}
  AND NOT (meme.id = ANY(CAST(:excluded_meme_ids AS uuid[])))
  AND (
      CAST(:last_score AS double precision) IS NULL
      OR trend.trending_score < CAST(:last_score AS double precision)
      OR (
          trend.trending_score = CAST(:last_score AS double precision)
          AND trend.meme_id > CAST(:last_meme_id AS uuid)
      )
  )
ORDER BY trend.trending_score DESC, trend.meme_id ASC
LIMIT :limit
OFFSET :offset
"""

_EXPLORATION_CANDIDATES_SQL = f"""
SELECT
    feature.meme_id,
    (
        0.40 * feature.source_quality_quantile
        + 0.30 * feature.technical_quality
        + 0.30 * feature.platform_response
    )::double precision AS score
FROM public_meme_recommendation_features_mv feature
JOIN memes meme ON meme.id = feature.meme_id
WHERE {_SAFETY_FILTER_SQL}
  AND {_COOLDOWN_FILTER_SQL}
  AND feature.latest_published_at >= now() - interval '90 days'
  AND feature.source_quality_quantile >= :min_source_quality
  AND feature.technical_quality >= :min_technical_quality
  AND feature.popularity_quantile < :max_popularity
ORDER BY md5(feature.meme_id::text || CAST(:viewer_user_id AS text)), feature.meme_id
LIMIT :limit
"""

_FEATURES_SQL = """
SELECT
    meme.id AS meme_id,
    COALESCE(feature.latest_published_at, meme.created_at) AS latest_published_at,
    COALESCE(feature.source_channel_ids, ARRAY[]::uuid[]) AS source_channel_ids,
    feature.representative_source_channel_id,
    COALESCE(feature.source_popularity_quantile, 0.5) AS source_popularity_quantile,
    COALESCE(feature.source_quality_quantile, 0.5) AS source_quality_quantile,
    COALESCE(feature.technical_quality, 0.5) AS technical_quality,
    COALESCE(feature.platform_response, 0.5) AS platform_response,
    COALESCE(feature.popularity_quantile, 0.5) AS popularity_quantile,
    COALESCE(feature.trend_quantile, 0.5) AS trend_quantile,
    meme.template_id,
    COALESCE(feature.coverage_flags, '{}'::jsonb) AS coverage_flags
FROM memes meme
LEFT JOIN public_meme_recommendation_features_mv feature ON feature.meme_id = meme.id
WHERE meme.id = ANY(CAST(:meme_ids AS uuid[]))
  AND meme.is_public IS TRUE
"""


__all__ = ["FALLBACK_ALGORITHM_VERSION", "RecommendationService"]
