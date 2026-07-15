# ruff: noqa: TC001,TC002
"""Shared hybrid meme search and read service for web and Telegram bot surfaces."""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import Select, and_, any_, bindparam, false, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from memexpert.core.config import Settings, get_settings
from memexpert.core.qdrant import (
    QdrantSimilarityClientProtocol,
    QdrantSimilarityMatch,
    QdrantUserSearchClientProtocol,
    QdrantUserSearchMatch,
)
from memexpert.core.search_index_prefilter import SearchIndexPrefilter, SearchIndexPrefilterScope
from memexpert.core.voyage import VoyageEmbeddingError, decode_embedding_bytes
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import EmbeddingCache, Meme, MemeFile, MemeSeoPage, MemeTemplate
from memexpert.models.enums import AnalyticsEventType, CollectionKind, ContentKind, ContentLanguage, EmbeddingInputType
from memexpert.models.user import AnalyticsEvent
from memexpert.schemas.meme import (
    MemeCardRead,
    MemeDetailRead,
    MemeFileRead,
    MemeResultAttributionFiltersRead,
    MemeResultAttributionRead,
    MemeSearchPageRead,
    MemeSearchResultRead,
    MemeSearchScoreRead,
    MemeSlugRedirectRead,
    PublicMemeCardRead,
    PublicMemeDetailRead,
    PublicMemeFileRead,
    PublicMemeSearchPageRead,
    PublicMemeSearchResultRead,
    PublicMemeViewerAccess,
    PublicMemeViewerAccessRead,
    new_discovery_impression_id,
    new_discovery_request_id,
)
from memexpert.services.engagement_read_model import load_derived_popularity_scores
from memexpert.services.media_render_urls import MediaRenderUrlService, PublicMediaRenderContext
from memexpert.services.search_index_sync import SEARCH_INDEX_ALGORITHM_VERSION

TEXT_SCORE_KEYS = ("_rankingScore", "_score", "rankingScore", "score")
SEMANTIC_WEIGHT = 0.50
TEXT_WEIGHT = 0.35
POPULARITY_WEIGHT = 0.15
TRENDING_EVENT_WEIGHT = 0.55
TRENDING_SNAPSHOT_WEIGHT = 0.25
TRENDING_POPULARITY_WEIGHT = 0.15
TRENDING_LIKE_WEIGHT = 0.05
POPULAR_ALGORITHM_VERSION = "popular_v1"
LEGACY_TRENDING_ALGORITHM_VERSION = "source_engagement_trending_v1"
QDRANT_SIMILARITY_ALGORITHM_VERSION = SEARCH_INDEX_ALGORITHM_VERSION
PERSONALIZED_RECOMMENDATION_ALGORITHM_VERSION = f"{SEARCH_INDEX_ALGORITHM_VERSION}_personalized_v1"
TRENDING_EVENT_WEIGHTS = {
    AnalyticsEventType.MEME_DOWNLOAD: 2.0,
    AnalyticsEventType.MEME_VIEW: 1.0,
    AnalyticsEventType.MEME_SEND: 3.0,
    AnalyticsEventType.MEME_SAVE: 4.0,
    AnalyticsEventType.SAVE: 4.0,
    AnalyticsEventType.FAVORITE: 4.0,
    AnalyticsEventType.MEME_LIKE: 5.0,
}

logger = logging.getLogger(__name__)
_DERIVED_POPULARITY_ATTR = "_derived_popularity_score"

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.sql.elements import ColumnElement


class MemeNotFoundError(LookupError):
    """Raised when a meme does not exist or is not visible to the caller."""


class MemeTextSearchClientProtocol(Protocol):
    """Narrow text-search boundary used by the shared meme search service."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        prefilter: SearchIndexPrefilter | None = None,
    ) -> list[dict[str, Any]]: ...


class MemeQueryEmbeddingClientProtocol(Protocol):
    """Plain-text query embedding boundary used before user-facing semantic search."""

    async def embed_query(self, query: str) -> tuple[float, ...]: ...


class MemeSearchScope(StrEnum):
    """Service-level access scopes for search and browse surfaces."""

    PUBLIC = "public"
    PRIVATE = "private"
    ALL = "all"
    COLLECTIONS = "collections"


@dataclass(frozen=True, slots=True)
class MemeSearchFilters:
    """Filters supported by web and bot search surfaces.

    ``tags`` is the currently available taxonomy field on ``Meme``. Categories
    are intentionally not represented until the data model gains a category
    source of truth.
    """

    language: ContentLanguage | None = None
    media_type: ContentKind | None = None
    include_nsfw: bool = False
    tags: tuple[str, ...] = ()
    scope: MemeSearchScope | None = None
    collection_ids: tuple[uuid.UUID, ...] = ()


@dataclass(slots=True)
class _CandidateScore:
    meme_id: uuid.UUID | None = None
    meme_file_id: uuid.UUID | None = None
    semantic_raw: float = 0.0
    text_raw: float = 0.0
    semantic: float = 0.0
    text: float = 0.0
    popularity: float = 0.0
    total: float = 0.0


@dataclass(frozen=True, slots=True)
class _CollectedCandidates:
    candidates: dict[uuid.UUID, _CandidateScore]
    fallback_reason: str | None = None
    degraded_reason: str | None = None
    text_latency_seconds: float = 0.0
    semantic_latency_seconds: float = 0.0
    text_candidate_count: int = 0
    semantic_candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class _SimilarCandidate:
    meme: Meme
    source_algorithm: str
    reason: str
    score: float | None = None
    score_components: dict[str, float] | None = None
    algorithm_version: str | None = None


@dataclass(frozen=True, slots=True)
class _RecommendationCandidate:
    meme: Meme
    semantic_score: float
    total_score: float


@dataclass(frozen=True, slots=True)
class _ViewerMemeActionState:
    favorited_meme_ids: frozenset[uuid.UUID] = frozenset()
    saved_meme_ids: frozenset[uuid.UUID] = frozenset()
    pinned_meme_ids: frozenset[uuid.UUID] = frozenset()

    def has_favorited(self, meme_id: uuid.UUID) -> bool:
        return meme_id in self.favorited_meme_ids

    def has_saved(self, meme_id: uuid.UUID) -> bool:
        return meme_id in self.saved_meme_ids

    def has_pinned(self, meme_id: uuid.UUID) -> bool:
        return meme_id in self.pinned_meme_ids


class MemeSearchService:
    """Hybrid search/read service over indexed candidates and canonical DB DTOs.

    Initial ranking strategy: collect candidate meme IDs from Meilisearch text
    hits and Qdrant semantic hits, normalize semantic relevance, text relevance,
    and DB popularity independently to 0..1 over the candidate set, then sort by
    ``0.50 * semantic + 0.35 * text + 0.15 * popularity``. This deliberately
    favors semantic intent while preserving exact-text matches and giving popular
    memes a small stable boost.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        text_client: MemeTextSearchClientProtocol | None = None,
        semantic_client: QdrantUserSearchClientProtocol | None = None,
        similarity_client: QdrantSimilarityClientProtocol | None = None,
        query_embedding_client: MemeQueryEmbeddingClientProtocol | None = None,
        media_render_service: MediaRenderUrlService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._text_client = text_client
        self._semantic_client = semantic_client
        self._similarity_client = similarity_client
        self._query_embedding_client = query_embedding_client
        self._media_render_service = media_render_service or MediaRenderUrlService()
        self._settings = settings or get_settings()

    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        query_vector: tuple[float, ...] | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "service_search",
    ) -> MemeSearchPageRead:
        started_seconds = time.perf_counter()
        resolved_filters = _resolve_search_filters(filters, viewer_user_id=viewer_user_id)
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        request_id = new_discovery_request_id()
        candidate_limit = self._settings.search_candidate_pool_limit_per_source

        normalized_query = query.strip()
        embedding_started_seconds = time.perf_counter()
        resolved_query_vector, query_vector_provider_failed = await self._resolve_query_vector(
            normalized_query,
            query_vector=query_vector,
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )
        embedding_latency_seconds = time.perf_counter() - embedding_started_seconds
        index_prefilter = _build_search_index_prefilter(resolved_filters, viewer_user_id=viewer_user_id)
        index_started_seconds = time.perf_counter()
        collected_candidates = await self._collect_index_candidates(
            normalized_query,
            query_vector=resolved_query_vector,
            prefilter=index_prefilter,
            limit=candidate_limit,
            provider_failed=query_vector_provider_failed,
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )
        index_latency_seconds = time.perf_counter() - index_started_seconds
        candidates = collected_candidates.candidates
        index_candidate_count = len(candidates)
        if not candidates:
            fallback_reason = collected_candidates.fallback_reason or "index_candidates_empty"
            db_started_seconds = time.perf_counter()
            page = await self._popular_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                source_algorithm="fallback_popular",
                query=normalized_query,
                reason=fallback_reason,
            )
            db_latency_seconds = time.perf_counter() - db_started_seconds
            _log_discovery_completed(
                event="meme_search_completed",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                query=normalized_query,
                source_algorithm="fallback_popular",
                algorithm_version=POPULAR_ALGORITHM_VERSION,
                degraded_mode=True,
                reason=fallback_reason,
                fallback_reason=fallback_reason,
                limit=resolved_limit,
                offset=resolved_offset,
                candidate_count=0,
                text_candidate_count=collected_candidates.text_candidate_count,
                semantic_candidate_count=collected_candidates.semantic_candidate_count,
                visible_count=page.total,
                result_count=len(page.items),
                total=page.total,
                embedding_latency_seconds=embedding_latency_seconds,
                text_latency_seconds=collected_candidates.text_latency_seconds,
                semantic_latency_seconds=collected_candidates.semantic_latency_seconds,
                index_latency_seconds=index_latency_seconds,
                db_latency_seconds=db_latency_seconds,
                total_latency_seconds=time.perf_counter() - started_seconds,
            )
            return page

        db_started_seconds = time.perf_counter()
        await self._resolve_missing_meme_ids(candidates)
        candidates = {score.meme_id: score for score in candidates.values() if score.meme_id is not None}
        if not candidates:
            page = MemeSearchPageRead(
                items=[],
                limit=resolved_limit,
                offset=resolved_offset,
                total=0,
                has_more=False,
                request_id=request_id,
            )
            _log_discovery_completed(
                event="meme_search_completed",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                query=normalized_query,
                source_algorithm="hybrid_search",
                algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
                degraded_mode=collected_candidates.degraded_reason is not None,
                reason=collected_candidates.degraded_reason,
                limit=resolved_limit,
                offset=resolved_offset,
                candidate_count=index_candidate_count,
                text_candidate_count=collected_candidates.text_candidate_count,
                semantic_candidate_count=collected_candidates.semantic_candidate_count,
                visible_count=0,
                result_count=0,
                total=0,
                embedding_latency_seconds=embedding_latency_seconds,
                text_latency_seconds=collected_candidates.text_latency_seconds,
                semantic_latency_seconds=collected_candidates.semantic_latency_seconds,
                index_latency_seconds=index_latency_seconds,
                db_latency_seconds=time.perf_counter() - db_started_seconds,
                total_latency_seconds=time.perf_counter() - started_seconds,
            )
            return page

        memes = await self._load_visible_memes(
            tuple(candidates),
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )
        visible_scores = {meme.id: candidates[meme.id] for meme in memes}
        popularity_by_meme_id = await self._attach_derived_popularity_scores(memes)
        self._apply_normalized_scores(visible_scores, popularity_by_meme_id)

        ranked_memes = sorted(
            memes,
            key=lambda meme: (
                visible_scores[meme.id].total,
                popularity_by_meme_id.get(meme.id, 0.0),
                meme.created_at,
                str(meme.id),
            ),
            reverse=True,
        )
        total = len(ranked_memes)
        page_memes = ranked_memes[resolved_offset : resolved_offset + resolved_limit]
        items = []
        for rank, meme in enumerate(page_memes, start=resolved_offset + 1):
            score = visible_scores[meme.id]
            score_read = _to_score_read(score)
            items.append(
                MemeSearchResultRead(
                    meme=_to_card_read(meme),
                    score=score_read,
                    attribution=_build_result_attribution(
                        request_id=request_id,
                        surface=surface,
                        source_algorithm="hybrid_search",
                        rank=rank,
                        query=normalized_query,
                        filters=resolved_filters,
                        score=score_read.total,
                        score_components=_score_components(score_read),
                        algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
                    ),
                )
            )

        page = MemeSearchPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total,
            has_more=resolved_offset + resolved_limit < total,
            request_id=request_id,
        )
        _log_discovery_completed(
            event="meme_search_completed",
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
            query=normalized_query,
            source_algorithm="hybrid_search",
            algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
            degraded_mode=collected_candidates.degraded_reason is not None,
            reason=collected_candidates.degraded_reason,
            limit=resolved_limit,
            offset=resolved_offset,
            candidate_count=index_candidate_count,
            text_candidate_count=collected_candidates.text_candidate_count,
            semantic_candidate_count=collected_candidates.semantic_candidate_count,
            visible_count=len(memes),
            result_count=len(items),
            total=total,
            embedding_latency_seconds=embedding_latency_seconds,
            text_latency_seconds=collected_candidates.text_latency_seconds,
            semantic_latency_seconds=collected_candidates.semantic_latency_seconds,
            index_latency_seconds=index_latency_seconds,
            db_latency_seconds=time.perf_counter() - db_started_seconds,
            total_latency_seconds=time.perf_counter() - started_seconds,
        )
        return page

    async def search_public_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_search",
    ) -> PublicMemeSearchPageRead:
        resolved_filters = _resolve_search_filters(
            filters,
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        page = await self.search_memes(
            query,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
            limit=limit,
            offset=offset,
            surface=surface,
        )
        return await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def recommendation_candidates(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "service_recommendation_candidates",
    ) -> MemeSearchPageRead:
        """Return reusable personalized meme candidates from short-term positive signals.

        The recommendation path keeps data volume intentionally bounded: recent
        positive rows are capped by settings, Qdrant returns a capped candidate
        pool, and PostgreSQL access/NSFW filtering is the final authority.
        """

        started_seconds = time.perf_counter()
        resolved_filters = _resolve_search_filters(filters, viewer_user_id=viewer_user_id)
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        request_id = new_discovery_request_id()

        if viewer_user_id is None:
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="cold_start_no_viewer",
                started_seconds=started_seconds,
            )

        positive_since = utcnow() - timedelta(hours=max(1, self._settings.recommendation_positive_lookback_hours))
        positive_weights = await self._load_recommendation_positive_weights(
            viewer_user_id=viewer_user_id,
            since=positive_since,
        )
        if not positive_weights:
            excluded_meme_ids = await self._load_recommendation_excluded_meme_ids(
                viewer_user_id=viewer_user_id,
                positive_meme_ids=set(),
            )
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="cold_start_no_positive_signals",
                exclude_meme_ids=excluded_meme_ids,
                started_seconds=started_seconds,
            )

        weighted_vectors = await self._load_weighted_signal_embedding_vectors(positive_weights)
        preference_vector = _weighted_centroid(weighted_vectors)
        if preference_vector is None:
            excluded_meme_ids = await self._load_recommendation_excluded_meme_ids(
                viewer_user_id=viewer_user_id,
                positive_meme_ids=set(positive_weights),
            )
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="cold_start_no_signal_embeddings",
                exclude_meme_ids=excluded_meme_ids,
                started_seconds=started_seconds,
            )

        if self._semantic_client is None:
            _log_discovery_degraded(
                event="meme_recommendation_provider_failure",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                source_algorithm="personalized_recommendations",
                algorithm_version=PERSONALIZED_RECOMMENDATION_ALGORITHM_VERSION,
                reason="qdrant_client_not_configured",
                fallback_reason="qdrant_failure",
            )
            excluded_meme_ids = await self._load_recommendation_excluded_meme_ids(
                viewer_user_id=viewer_user_id,
                positive_meme_ids=set(positive_weights),
            )
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="qdrant_failure",
                exclude_meme_ids=excluded_meme_ids,
                started_seconds=started_seconds,
            )

        candidate_limit = max(1, self._settings.recommendation_qdrant_candidate_limit)
        semantic_started_seconds = time.perf_counter()
        try:
            matches = await self._semantic_client.search_memes_by_vector(
                query_vector=preference_vector,
                limit=candidate_limit,
                prefilter=_build_search_index_prefilter(resolved_filters, viewer_user_id=viewer_user_id),
            )
        except Exception as exc:
            semantic_latency_seconds = time.perf_counter() - semantic_started_seconds
            _log_discovery_degraded(
                event="meme_recommendation_provider_failure",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                source_algorithm="personalized_recommendations",
                algorithm_version=PERSONALIZED_RECOMMENDATION_ALGORITHM_VERSION,
                reason="qdrant_lookup_failed",
                fallback_reason="qdrant_failure",
                semantic_latency_seconds=semantic_latency_seconds,
                exception_type=type(exc).__name__,
            )
            excluded_meme_ids = await self._load_recommendation_excluded_meme_ids(
                viewer_user_id=viewer_user_id,
                positive_meme_ids=set(positive_weights),
            )
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="qdrant_failure",
                exclude_meme_ids=excluded_meme_ids,
                started_seconds=started_seconds,
                semantic_latency_seconds=semantic_latency_seconds,
            )
        semantic_latency_seconds = time.perf_counter() - semantic_started_seconds

        excluded_meme_ids = await self._load_recommendation_excluded_meme_ids(
            viewer_user_id=viewer_user_id,
            positive_meme_ids=set(positive_weights),
        )
        ordered_matches = _dedupe_user_search_matches(matches, excluded_meme_ids=excluded_meme_ids)
        if not ordered_matches:
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="recommendation_candidates_empty",
                exclude_meme_ids=excluded_meme_ids,
                started_seconds=started_seconds,
                semantic_latency_seconds=semantic_latency_seconds,
            )

        db_started_seconds = time.perf_counter()
        memes = await self._load_visible_memes(
            tuple(match.meme_id for match in ordered_matches),
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )
        memes_by_id = {meme.id: meme for meme in memes}
        popularity_by_meme_id = await self._attach_derived_popularity_scores(memes)
        max_popularity = max(popularity_by_meme_id.values(), default=0.0)
        candidates: list[_RecommendationCandidate] = []
        for match in ordered_matches:
            meme = memes_by_id.get(match.meme_id)
            if meme is None:
                continue
            semantic_score = _safe_float(match.semantic_score)
            if semantic_score is None:
                continue
            popularity = _normalize_value(popularity_by_meme_id.get(meme.id, 0.0), max_popularity)
            total_score = 0.85 * semantic_score + 0.15 * popularity
            candidates.append(
                _RecommendationCandidate(
                    meme=meme,
                    semantic_score=semantic_score,
                    total_score=total_score,
                )
            )

        if not candidates:
            return await self._recommendation_fallback_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
                request_id=request_id,
                surface=surface,
                reason="postgres_filters_empty",
                exclude_meme_ids=excluded_meme_ids,
                started_seconds=started_seconds,
                semantic_latency_seconds=semantic_latency_seconds,
            )

        candidates.sort(
            key=lambda candidate: (
                candidate.total_score,
                candidate.semantic_score,
                popularity_by_meme_id.get(candidate.meme.id, 0.0),
                candidate.meme.created_at,
                str(candidate.meme.id),
            ),
            reverse=True,
        )
        page_candidates = candidates[resolved_offset : resolved_offset + resolved_limit]
        items: list[MemeSearchResultRead] = []
        for rank, candidate in enumerate(page_candidates, start=resolved_offset + 1):
            popularity = _normalize_value(popularity_by_meme_id.get(candidate.meme.id, 0.0), max_popularity)
            score_read = MemeSearchScoreRead(
                semantic=candidate.semantic_score,
                text=0.0,
                popularity=popularity,
                total=candidate.total_score,
            )
            items.append(
                MemeSearchResultRead(
                    meme=_to_card_read(candidate.meme),
                    score=score_read,
                    attribution=_build_result_attribution(
                        request_id=request_id,
                        surface=surface,
                        source_algorithm="personalized_recommendations",
                        rank=rank,
                        query=None,
                        filters=resolved_filters,
                        score=score_read.total,
                        score_components={
                            "semantic": score_read.semantic,
                            "popularity": score_read.popularity,
                            "positive_source_count": float(len(weighted_vectors)),
                            "total": score_read.total,
                        },
                        algorithm_version=PERSONALIZED_RECOMMENDATION_ALGORITHM_VERSION,
                        reason="qdrant_preference_vector",
                    ),
                )
            )

        page = MemeSearchPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=len(candidates),
            has_more=resolved_offset + resolved_limit < len(candidates),
            request_id=request_id,
        )
        _log_discovery_completed(
            event="meme_recommendation_completed",
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
            query=None,
            source_algorithm="personalized_recommendations",
            algorithm_version=PERSONALIZED_RECOMMENDATION_ALGORITHM_VERSION,
            degraded_mode=False,
            reason="qdrant_preference_vector",
            limit=resolved_limit,
            offset=resolved_offset,
            candidate_count=len(candidates),
            semantic_candidate_count=len(ordered_matches),
            visible_count=len(memes),
            result_count=len(items),
            total=len(candidates),
            semantic_latency_seconds=semantic_latency_seconds,
            db_latency_seconds=time.perf_counter() - db_started_seconds,
            total_latency_seconds=time.perf_counter() - started_seconds,
        )
        return page

    async def home_feed_public_memes(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_home_feed",
    ) -> PublicMemeSearchPageRead:
        resolved_filters = _resolve_search_filters(
            filters,
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        page = await self.recommendation_candidates(
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
            limit=limit,
            offset=offset,
            surface=surface,
        )
        return await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def get_meme_detail(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> MemeDetailRead:
        stmt = _visible_meme_stmt(viewer_user_id).where(Meme.id == meme_id)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))

        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme was not found or is not visible to this caller.")
        await self._attach_derived_popularity_scores([meme])
        return _to_detail_read(meme)

    async def get_meme_detail_by_slug(
        self,
        slug: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> MemeDetailRead:
        normalized_slug = slug.strip().lower()
        stmt = _visible_meme_stmt(viewer_user_id).join(MemeSeoPage).where(MemeSeoPage.slug == normalized_slug)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))

        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme slug was not found or is not visible to this caller.")
        await self._attach_derived_popularity_scores([meme])
        return _to_detail_read(meme)

    async def get_public_meme_detail(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> PublicMemeDetailRead:
        meme = await self._load_public_or_authorized_meme_by_id(
            meme_id,
            viewer_user_id=viewer_user_id,
            include_nsfw=include_nsfw,
        )
        await self._attach_derived_popularity_scores([meme])
        action_state = await self._load_viewer_action_state((meme.id,), viewer_user_id=viewer_user_id)
        viewer_access = await self._load_detail_viewer_access_marker(meme, viewer_user_id=viewer_user_id)
        return _to_authorized_detail_read(
            meme,
            media_render_service=self._media_render_service,
            viewer_action_state=action_state,
            viewer_access=viewer_access,
        )

    async def get_public_meme_detail_by_slug(
        self,
        slug: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> PublicMemeDetailRead:
        meme = await self._load_public_or_authorized_meme_by_slug(
            slug,
            viewer_user_id=viewer_user_id,
            include_nsfw=include_nsfw,
        )
        await self._attach_derived_popularity_scores([meme])
        action_state = await self._load_viewer_action_state((meme.id,), viewer_user_id=viewer_user_id)
        viewer_access = await self._load_detail_viewer_access_marker(meme, viewer_user_id=viewer_user_id)
        return _to_authorized_detail_read(
            meme,
            media_render_service=self._media_render_service,
            viewer_action_state=action_state,
            viewer_access=viewer_access,
        )

    async def get_public_similar_memes(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_meme_similar",
    ) -> PublicMemeSearchPageRead:
        """Return public memes related to a source meme, led by Qdrant visual similarity.

        Qdrant is treated only as a candidate source. PostgreSQL remains the access
        and NSFW authority before any public card leaves the service.
        """

        started_seconds = time.perf_counter()
        source_meme = await self._load_public_meme_by_id(meme_id, include_nsfw=include_nsfw)
        resolved_filters = _resolve_search_filters(
            MemeSearchFilters(include_nsfw=include_nsfw, scope=MemeSearchScope.PUBLIC),
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        request_id = new_discovery_request_id()
        target_count = resolved_offset + resolved_limit + 1

        candidates, fallback_reason = await self._qdrant_similar_candidates(
            source_meme,
            filters=resolved_filters,
            target_count=target_count,
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
        )
        seen_meme_ids = {source_meme.id, *(candidate.meme.id for candidate in candidates)}
        if len(candidates) < target_count:
            candidates.extend(
                await self._similar_fallback_candidates(
                    source_meme,
                    filters=resolved_filters,
                    seen_meme_ids=seen_meme_ids,
                    target_count=target_count,
                    reason=fallback_reason or "similarity_backfill",
                )
            )

        page_candidates = candidates[resolved_offset : resolved_offset + resolved_limit]
        items: list[MemeSearchResultRead] = []
        for rank, candidate in enumerate(page_candidates, start=resolved_offset + 1):
            score_read = _similar_candidate_score_read(candidate)
            items.append(
                MemeSearchResultRead(
                    meme=_to_card_read(candidate.meme),
                    score=score_read,
                    attribution=_build_result_attribution(
                        request_id=request_id,
                        surface=surface,
                        source_algorithm=candidate.source_algorithm,
                        rank=rank,
                        query=None,
                        filters=resolved_filters,
                        score=candidate.score,
                        score_components=candidate.score_components or {},
                        algorithm_version=candidate.algorithm_version,
                        source_meme_id=source_meme.id,
                        reason=candidate.reason,
                    ),
                )
            )

        page = MemeSearchPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=len(candidates),
            has_more=len(candidates) > resolved_offset + resolved_limit,
            request_id=request_id,
        )
        _log_discovery_completed(
            event="meme_similar_completed",
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
            query=None,
            source_algorithm="qdrant_similarity" if fallback_reason is None else "fallback_public_recommendations",
            algorithm_version=(
                QDRANT_SIMILARITY_ALGORITHM_VERSION if fallback_reason is None else POPULAR_ALGORITHM_VERSION
            ),
            degraded_mode=fallback_reason is not None,
            reason=fallback_reason,
            fallback_reason=fallback_reason,
            limit=resolved_limit,
            offset=resolved_offset,
            candidate_count=len(candidates),
            result_count=len(items),
            total=len(candidates),
            total_latency_seconds=time.perf_counter() - started_seconds,
        )
        return await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def get_public_meme_cards_by_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> list[PublicMemeCardRead]:
        """Return visible public card DTOs in the caller-supplied order."""

        if not meme_ids:
            return []

        stmt = _public_meme_stmt().where(Meme.id.in_(meme_ids))
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))
        result = await self._session.execute(stmt)
        memes_by_id = {meme.id: meme for meme in result.scalars().unique()}
        await self._attach_derived_popularity_scores(tuple(memes_by_id.values()))
        action_state = await self._load_viewer_action_state(meme_ids, viewer_user_id=viewer_user_id)
        return [
            _to_public_card_read(
                meme,
                media_render_service=self._media_render_service,
                viewer_action_state=action_state,
            )
            for meme_id in meme_ids
            if (meme := memes_by_id.get(meme_id)) is not None
        ]

    async def get_authorized_meme_cards_by_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID,
        include_nsfw: bool = False,
    ) -> list[PublicMemeCardRead]:
        """Return authorized card DTOs in caller order, using private render URLs for private files."""

        if not meme_ids:
            return []

        stmt = _visible_meme_stmt(viewer_user_id).where(Meme.id.in_(meme_ids))
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))
        result = await self._session.execute(stmt)
        memes_by_id = {meme.id: meme for meme in result.scalars().unique()}
        await self._attach_derived_popularity_scores(tuple(memes_by_id.values()))
        action_state = await self._load_viewer_action_state(meme_ids, viewer_user_id=viewer_user_id)
        return [
            _to_authorized_card_read(
                meme,
                media_render_service=self._media_render_service,
                viewer_action_state=action_state,
            )
            for meme_id in meme_ids
            if (meme := memes_by_id.get(meme_id)) is not None
        ]

    async def get_slug_redirect(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> MemeSlugRedirectRead:
        detail = await self.get_meme_detail(
            meme_id,
            viewer_user_id=viewer_user_id,
            include_nsfw=include_nsfw,
        )
        if not detail.seo_page_slug:
            return MemeSlugRedirectRead(meme_id=meme_id, slug="", path=f"/memes/{meme_id}", should_redirect=False)
        return MemeSlugRedirectRead(
            meme_id=meme_id,
            slug=detail.seo_page_slug,
            path=f"/memes/{detail.seo_page_slug}",
            should_redirect=True,
        )

    async def browse_memes(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "service_browse",
    ) -> MemeSearchPageRead:
        """Return a stable popular catalog page using the service fallback behavior."""

        return await self._popular_page(
            viewer_user_id=viewer_user_id,
            filters=_resolve_search_filters(filters, viewer_user_id=viewer_user_id),
            limit=_clamp_limit(limit),
            offset=max(0, offset),
            request_id=new_discovery_request_id(),
            surface=surface,
            source_algorithm="popular",
            reason="browse_popular",
        )

    async def browse_public_memes(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_browse",
    ) -> PublicMemeSearchPageRead:
        resolved_filters = _resolve_search_filters(
            filters,
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        page = await self.browse_memes(
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
            limit=limit,
            offset=offset,
            surface=surface,
        )
        return await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def trending_memes(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        lookback_hours: int = 168,
        surface: str = "service_trending",
        source_algorithm: str = "legacy_trending",
        reason: str = "recent_activity_popularity",
        request_id: str | None = None,
        exclude_meme_ids: set[uuid.UUID] | None = None,
    ) -> MemeSearchPageRead:
        """Return a deterministic popular/trending catalog page.

        Scoring is intentionally simple and explainable: recent platform event
        weight plus source engagement snapshot deltas in the lookback window,
        current derived engagement popularity, and durable in-app likes. Baseline
        source snapshots contribute no invented delta.
        """

        resolved_filters = _resolve_search_filters(filters, viewer_user_id=viewer_user_id)
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        request_id = request_id or new_discovery_request_id()
        since = utcnow() - timedelta(hours=max(1, lookback_hours))

        base_stmt = _apply_filters(
            _search_scope_meme_stmt(
                viewer_user_id,
                scope=resolved_filters.scope or _default_search_scope(viewer_user_id),
                collection_ids=resolved_filters.collection_ids,
            ),
            resolved_filters,
        )
        if exclude_meme_ids:
            base_stmt = base_stmt.where(~Meme.id.in_(tuple(exclude_meme_ids)))
        result = await self._session.execute(base_stmt)
        memes = list(result.scalars().all())
        if not memes:
            return MemeSearchPageRead(
                items=[],
                limit=resolved_limit,
                offset=resolved_offset,
                total=0,
                has_more=False,
                request_id=request_id,
            )

        meme_ids = {meme.id for meme in memes}
        recent_scores = await self._recent_event_scores(meme_ids, since=since)
        snapshot_scores = await self._source_delta_scores(meme_ids, since=since)
        popularity_by_meme_id = await self._attach_derived_popularity_scores(memes)
        max_recent = max(recent_scores.values(), default=0.0)
        max_snapshot = max(snapshot_scores.values(), default=0.0)
        max_popularity = max(popularity_by_meme_id.values(), default=0.0)
        max_likes = max((meme.like_count for meme in memes), default=0)

        ranked: list[tuple[Meme, _CandidateScore, float]] = []
        for meme in memes:
            recent = _normalize_value(recent_scores.get(meme.id, 0.0), max_recent)
            snapshot = _normalize_value(snapshot_scores.get(meme.id, 0.0), max_snapshot)
            popularity = _normalize_value(popularity_by_meme_id.get(meme.id, 0.0), max_popularity)
            likes = _normalize_value(float(meme.like_count), float(max_likes))
            total_score = (
                TRENDING_EVENT_WEIGHT * recent
                + TRENDING_SNAPSHOT_WEIGHT * snapshot
                + TRENDING_POPULARITY_WEIGHT * popularity
                + TRENDING_LIKE_WEIGHT * likes
            )
            ranked.append(
                (
                    meme,
                    _CandidateScore(semantic=recent, text=snapshot, popularity=popularity, total=total_score),
                    likes,
                )
            )

        ranked.sort(
            key=lambda item: (
                item[1].total,
                item[1].semantic,
                item[1].text,
                item[1].popularity,
                item[2],
                item[0].created_at,
                str(item[0].id),
            ),
            reverse=True,
        )
        page = ranked[resolved_offset : resolved_offset + resolved_limit]
        items = []
        for rank, (meme, score, likes) in enumerate(page, start=resolved_offset + 1):
            score_read = _to_score_read(score)
            items.append(
                MemeSearchResultRead(
                    meme=_to_card_read(meme),
                    score=score_read,
                    attribution=_build_result_attribution(
                        request_id=request_id,
                        surface=surface,
                        source_algorithm=source_algorithm,
                        rank=rank,
                        query=None,
                        filters=resolved_filters,
                        score=score.total,
                        score_components={
                            "recent_events": score.semantic,
                            "snapshot": score.text,
                            "popularity": score.popularity,
                            "likes": likes,
                            "total": score.total,
                        },
                        algorithm_version=LEGACY_TRENDING_ALGORITHM_VERSION,
                        reason=reason,
                    ),
                )
            )
        return MemeSearchPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=len(ranked),
            has_more=resolved_offset + resolved_limit < len(ranked),
            request_id=request_id,
        )

    async def trending_public_memes(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        lookback_hours: int = 168,
        surface: str = "public_api_trending_legacy",
    ) -> PublicMemeSearchPageRead:
        resolved_filters = _resolve_search_filters(
            filters,
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        page = await self.trending_memes(
            viewer_user_id=None,
            filters=resolved_filters,
            limit=limit,
            offset=offset,
            lookback_hours=lookback_hours,
            surface=surface,
        )
        return await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def _recent_event_scores(self, meme_ids: set[uuid.UUID], *, since: object) -> dict[uuid.UUID, float]:
        result = await self._session.execute(
            select(AnalyticsEvent).where(
                AnalyticsEvent.event_type.in_(tuple(TRENDING_EVENT_WEIGHTS)),
                AnalyticsEvent.occurred_at >= since,
            )
        )
        scores: dict[uuid.UUID, float] = {}
        for event in result.scalars():
            meme_id = _analytics_event_meme_id(event.payload)
            if meme_id is None or meme_id not in meme_ids:
                continue
            scores[meme_id] = scores.get(meme_id, 0.0) + TRENDING_EVENT_WEIGHTS[event.event_type]
        return scores

    async def _source_delta_scores(self, meme_ids: set[uuid.UUID], *, since: object) -> dict[uuid.UUID, float]:
        if not meme_ids:
            return {}
        result = await self._session.execute(
            text(
                """
                WITH successful AS (
                    SELECT
                        mf.meme_id,
                        ses.meme_source_id,
                        ses.id,
                        ses.captured_at,
                        ses.view_count,
                        ses.reaction_count,
                        ses.forward_count,
                        lag(ses.view_count) OVER (
                            PARTITION BY ses.meme_source_id
                            ORDER BY ses.captured_at, ses.id
                        ) AS previous_view_count,
                        lag(ses.reaction_count) OVER (
                            PARTITION BY ses.meme_source_id
                            ORDER BY ses.captured_at, ses.id
                        ) AS previous_reaction_count,
                        lag(ses.forward_count) OVER (
                            PARTITION BY ses.meme_source_id
                            ORDER BY ses.captured_at, ses.id
                        ) AS previous_forward_count
                    FROM meme_source_engagement_snapshots ses
                    JOIN meme_sources ms ON ms.id = ses.meme_source_id
                    JOIN meme_files mf ON mf.id = ms.file_id
                    WHERE ses.fetch_status::text = 'success'
                      AND mf.meme_id IN :meme_ids
                )
                SELECT
                    meme_id,
                    sum(
                        CASE
                            WHEN previous_view_count IS NULL OR view_count IS NULL THEN 0
                            ELSE GREATEST(view_count - previous_view_count, 0)
                        END * 1.0
                        + CASE
                            WHEN previous_reaction_count IS NULL OR reaction_count IS NULL THEN 0
                            ELSE GREATEST(reaction_count - previous_reaction_count, 0)
                        END * 2.0
                        + CASE
                            WHEN previous_forward_count IS NULL OR forward_count IS NULL THEN 0
                            ELSE GREATEST(forward_count - previous_forward_count, 0)
                        END * 3.0
                    )::double precision AS score
                FROM successful
                WHERE captured_at >= :since
                GROUP BY meme_id
                """
            ).bindparams(bindparam("meme_ids", expanding=True)),
            {"meme_ids": tuple(meme_ids), "since": since},
        )
        return {meme_id: float(score or 0.0) for meme_id, score in result.all()}

    async def _recommendation_fallback_page(
        self,
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
        limit: int,
        offset: int,
        request_id: str,
        surface: str,
        reason: str,
        exclude_meme_ids: set[uuid.UUID] | None = None,
        started_seconds: float | None = None,
        semantic_latency_seconds: float | None = None,
    ) -> MemeSearchPageRead:
        db_started_seconds = time.perf_counter()
        page = await self.trending_memes(
            viewer_user_id=viewer_user_id,
            filters=filters,
            limit=limit,
            offset=offset,
            lookback_hours=self._settings.recommendation_positive_lookback_hours,
            surface=surface,
            source_algorithm="fallback_trending",
            reason=reason,
            request_id=request_id,
            exclude_meme_ids=exclude_meme_ids,
        )
        total_latency_seconds = time.perf_counter() - started_seconds if started_seconds is not None else None
        _log_discovery_completed(
            event="meme_recommendation_completed",
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=filters,
            query=None,
            source_algorithm="fallback_trending",
            algorithm_version=LEGACY_TRENDING_ALGORITHM_VERSION,
            degraded_mode=True,
            reason=reason,
            fallback_reason=reason,
            limit=limit,
            offset=offset,
            candidate_count=page.total,
            visible_count=page.total,
            result_count=len(page.items),
            total=page.total,
            semantic_latency_seconds=semantic_latency_seconds,
            db_latency_seconds=time.perf_counter() - db_started_seconds,
            total_latency_seconds=total_latency_seconds,
        )
        return page

    async def _load_recommendation_excluded_meme_ids(
        self,
        *,
        viewer_user_id: uuid.UUID,
        positive_meme_ids: set[uuid.UUID],
    ) -> set[uuid.UUID]:
        impression_since = utcnow() - timedelta(hours=max(1, self._settings.recommendation_impression_lookback_hours))
        excluded_meme_ids = set(positive_meme_ids)
        excluded_meme_ids.update(
            await self._load_recent_impression_meme_ids(
                viewer_user_id=viewer_user_id,
                since=impression_since,
            )
        )
        return excluded_meme_ids

    async def _load_recommendation_positive_weights(
        self,
        *,
        viewer_user_id: uuid.UUID,
        since: datetime,
    ) -> dict[uuid.UUID, float]:
        weights: dict[uuid.UUID, float] = {}
        event_weights = self._recommendation_event_weights()
        result = await self._session.execute(
            select(AnalyticsEvent)
            .where(
                AnalyticsEvent.user_id == viewer_user_id,
                AnalyticsEvent.event_type.in_((*event_weights, AnalyticsEventType.COLLECTION_ACTION)),
                AnalyticsEvent.occurred_at >= since,
            )
            .order_by(AnalyticsEvent.occurred_at.desc())
            .limit(self._settings.recommendation_positive_signal_limit)
        )
        for event in result.scalars():
            meme_id = _analytics_event_meme_id(event.payload)
            if meme_id is None:
                continue
            weight = event_weights.get(event.event_type)
            if event.event_type == AnalyticsEventType.COLLECTION_ACTION:
                weight = (
                    self._settings.recommendation_signal_collection_add_weight
                    if _is_collection_add_event(event.payload)
                    else None
                )
            if weight is None or weight <= 0.0:
                continue
            weights[meme_id] = weights.get(meme_id, 0.0) + weight

        pinned_result = await self._session.execute(
            select(PinnedMeme.meme_id)
            .where(PinnedMeme.user_id == viewer_user_id, PinnedMeme.pinned_at >= since)
            .order_by(PinnedMeme.pinned_at.desc())
            .limit(self._settings.recommendation_positive_signal_limit)
        )
        for meme_id in pinned_result.scalars():
            _add_positive_weight(weights, meme_id, self._settings.recommendation_signal_durable_pin_weight)

        collection_result = await self._session.execute(
            select(CollectionMeme.meme_id)
            .join(Collection, Collection.id == CollectionMeme.collection_id)
            .where(
                or_(Collection.owner_id == viewer_user_id, CollectionMeme.added_by_user_id == viewer_user_id),
                CollectionMeme.added_at >= since,
            )
            .order_by(CollectionMeme.added_at.desc())
            .limit(self._settings.recommendation_positive_signal_limit)
        )
        for meme_id in collection_result.scalars():
            _add_positive_weight(weights, meme_id, self._settings.recommendation_signal_durable_collection_weight)

        return dict(
            sorted(
                weights.items(),
                key=lambda item: (item[1], str(item[0])),
                reverse=True,
            )[: self._settings.recommendation_positive_signal_limit]
        )

    def _recommendation_event_weights(self) -> dict[AnalyticsEventType, float]:
        return {
            AnalyticsEventType.FAVORITE: self._settings.recommendation_signal_favorite_weight,
            AnalyticsEventType.MEME_LIKE: self._settings.recommendation_signal_like_weight,
            AnalyticsEventType.MEME_SAVE: self._settings.recommendation_signal_save_weight,
            AnalyticsEventType.SAVE: self._settings.recommendation_signal_save_weight,
            AnalyticsEventType.MEME_PIN: self._settings.recommendation_signal_pin_weight,
            AnalyticsEventType.MEME_DOWNLOAD: self._settings.recommendation_signal_download_weight,
            AnalyticsEventType.MEME_SEND: self._settings.recommendation_signal_telegram_send_weight,
            AnalyticsEventType.INLINE_CHOSEN: self._settings.recommendation_signal_telegram_chosen_inline_weight,
            AnalyticsEventType.INLINE_SENT: self._settings.recommendation_signal_telegram_sent_weight,
            AnalyticsEventType.MEME_DETAIL_CLICK: self._settings.recommendation_signal_detail_view_weight,
            AnalyticsEventType.MEME_VIEW: self._settings.recommendation_signal_view_weight,
            AnalyticsEventType.VIEW: self._settings.recommendation_signal_view_weight,
        }

    async def _load_weighted_signal_embedding_vectors(
        self,
        positive_weights: dict[uuid.UUID, float],
    ) -> list[tuple[tuple[float, ...], float]]:
        if not positive_weights:
            return []

        result = await self._session.execute(
            select(Meme.id, Meme.primary_file_id, EmbeddingCache.embedding)
            .join(
                EmbeddingCache,
                and_(
                    EmbeddingCache.source_file_id == Meme.primary_file_id,
                    EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
                ),
            )
            .where(Meme.id.in_(tuple(positive_weights)))
            .order_by(EmbeddingCache.source_file_id, EmbeddingCache.created_at.desc())
        )

        seen_file_ids: set[uuid.UUID] = set()
        weighted_vectors: list[tuple[tuple[float, ...], float]] = []
        for meme_id, file_id, embedding in result.all():
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            try:
                vector = decode_embedding_bytes(
                    embedding,
                    dimensions=self._settings.pipeline_voyage_output_dimensions,
                )
            except VoyageEmbeddingError:
                logger.exception("Stored recommendation signal embedding could not be decoded; skipping signal.")
                continue
            weight = positive_weights.get(meme_id, 0.0)
            if weight > 0.0:
                weighted_vectors.append((vector, weight))
        return weighted_vectors

    async def _load_recent_impression_meme_ids(
        self,
        *,
        viewer_user_id: uuid.UUID,
        since: datetime,
    ) -> set[uuid.UUID]:
        result = await self._session.execute(
            select(AnalyticsEvent)
            .where(
                AnalyticsEvent.user_id == viewer_user_id,
                AnalyticsEvent.event_type.in_((AnalyticsEventType.MEME_IMPRESSION, AnalyticsEventType.IMPRESSION)),
                AnalyticsEvent.occurred_at >= since,
            )
            .order_by(AnalyticsEvent.occurred_at.desc())
            .limit(
                max(
                    self._settings.recommendation_positive_signal_limit,
                    self._settings.recommendation_qdrant_candidate_limit,
                )
            )
        )
        return {
            meme_id
            for event in result.scalars()
            if (meme_id := _analytics_event_meme_id(event.payload)) is not None
        }

    async def browse_tag(
        self,
        tag: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
        limit: int = 20,
        offset: int = 0,
        surface: str = "service_tag_browse",
    ) -> MemeSearchPageRead:
        normalized_tag = tag.strip().lower()
        request_id = new_discovery_request_id()
        if not normalized_tag:
            return MemeSearchPageRead(
                items=[],
                limit=_clamp_limit(limit),
                offset=max(0, offset),
                total=0,
                has_more=False,
                request_id=request_id,
            )
        return await self._popular_page(
            viewer_user_id=viewer_user_id,
            filters=MemeSearchFilters(include_nsfw=include_nsfw, tags=(normalized_tag,)),
            limit=_clamp_limit(limit),
            offset=max(0, offset),
            request_id=request_id,
            surface=surface,
            source_algorithm="popular",
            reason="tag_popular",
        )

    async def browse_public_tag(
        self,
        tag: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_tag_landing",
    ) -> PublicMemeSearchPageRead:
        resolved_filters = _resolve_search_filters(
            MemeSearchFilters(include_nsfw=include_nsfw, tags=(tag.strip().lower(),)),
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        page = await self.browse_tag(
            tag,
            viewer_user_id=None,
            include_nsfw=include_nsfw,
            limit=limit,
            offset=offset,
            surface=surface,
        )
        return await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def browse_template(
        self,
        template_slug: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
        limit: int = 20,
        offset: int = 0,
        surface: str = "service_template_browse",
    ) -> tuple[MemeTemplate | None, MemeSearchPageRead]:
        normalized_slug = template_slug.strip().lower()
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        request_id = new_discovery_request_id()
        empty_page = MemeSearchPageRead(
            items=[],
            limit=resolved_limit,
            offset=resolved_offset,
            total=0,
            has_more=False,
            request_id=request_id,
        )
        if not normalized_slug:
            return None, empty_page

        template = await self._session.scalar(select(MemeTemplate).where(MemeTemplate.slug == normalized_slug))
        if template is None:
            return None, empty_page

        base_stmt = _visible_meme_stmt(viewer_user_id).where(Meme.template_id == template.id)
        base_stmt = _apply_filters(base_stmt, MemeSearchFilters(include_nsfw=include_nsfw))
        total = await self._session.scalar(select(func.count()).select_from(base_stmt.order_by(None).subquery())) or 0
        result = await self._session.execute(base_stmt)
        all_memes = list(result.scalars().all())
        popularity_by_meme_id = await self._attach_derived_popularity_scores(all_memes)
        sorted_memes = sorted(
            all_memes,
            key=lambda meme: (popularity_by_meme_id.get(meme.id, 0.0), meme.created_at, str(meme.id)),
            reverse=True,
        )
        memes = sorted_memes[resolved_offset : resolved_offset + resolved_limit]
        max_popularity = max((popularity_by_meme_id.get(meme.id, 0.0) for meme in memes), default=0.0)
        items = []
        filters = _resolve_search_filters(
            MemeSearchFilters(include_nsfw=include_nsfw),
            viewer_user_id=viewer_user_id,
        )
        for rank, meme in enumerate(memes, start=resolved_offset + 1):
            popularity = _normalize_value(popularity_by_meme_id.get(meme.id, 0.0), max_popularity)
            score = _CandidateScore(popularity=popularity, total=POPULARITY_WEIGHT * popularity)
            score_read = _to_score_read(score)
            items.append(
                MemeSearchResultRead(
                    meme=_to_card_read(meme),
                    score=score_read,
                    attribution=_build_result_attribution(
                        request_id=request_id,
                        surface=surface,
                        source_algorithm="popular",
                        rank=rank,
                        query=None,
                        filters=filters,
                        score=score_read.total,
                        score_components=_score_components(score_read),
                        algorithm_version=POPULAR_ALGORITHM_VERSION,
                        reason="template_popular",
                    ),
                )
            )
        return template, MemeSearchPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total,
            has_more=resolved_offset + resolved_limit < total,
            request_id=request_id,
        )

    async def browse_public_template(
        self,
        template_slug: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_template_landing",
    ) -> tuple[MemeTemplate | None, PublicMemeSearchPageRead]:
        resolved_filters = _resolve_search_filters(
            MemeSearchFilters(include_nsfw=include_nsfw),
            viewer_user_id=viewer_user_id,
            default_scope=MemeSearchScope.PUBLIC,
        )
        template, page = await self.browse_template(
            template_slug,
            viewer_user_id=None,
            include_nsfw=include_nsfw,
            limit=limit,
            offset=offset,
            surface=surface,
        )
        return template, await self._to_public_search_page(
            page,
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )

    async def _resolve_query_vector(
        self,
        query: str,
        *,
        query_vector: tuple[float, ...] | None,
        request_id: str,
        surface: str,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
    ) -> tuple[tuple[float, ...] | None, bool]:
        if query_vector is not None or not query or self._query_embedding_client is None:
            return query_vector, False

        try:
            vector = await self._query_embedding_client.embed_query(query)
        except Exception as exc:
            _log_discovery_degraded(
                event="meme_search_provider_failure",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=filters,
                query=query,
                source_algorithm="hybrid_search",
                algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
                degraded_component="embedding",
                reason="query_embedding_failed",
                fallback_reason="text_only_search",
                exception_type=type(exc).__name__,
            )
            return None, True
        return vector or None, False

    async def _collect_index_candidates(
        self,
        query: str,
        *,
        query_vector: tuple[float, ...] | None,
        prefilter: SearchIndexPrefilter,
        limit: int,
        request_id: str,
        surface: str,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
        provider_failed: bool = False,
    ) -> _CollectedCandidates:
        candidates: dict[uuid.UUID, _CandidateScore] = {}
        text_latency_seconds = 0.0
        semantic_latency_seconds = 0.0
        text_candidate_count = 0
        semantic_candidate_count = 0

        if self._text_client is not None and query:
            text_started_seconds = time.perf_counter()
            try:
                text_hits = await self._text_client.search(query, limit=limit, prefilter=prefilter)
            except Exception as exc:
                text_latency_seconds = time.perf_counter() - text_started_seconds
                _log_discovery_degraded(
                    event="meme_search_provider_failure",
                    request_id=request_id,
                    surface=surface,
                    viewer_user_id=viewer_user_id,
                    filters=filters,
                    query=query,
                    source_algorithm="hybrid_search",
                    algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
                    degraded_component="text",
                    reason="text_search_failed",
                    fallback_reason="semantic_or_popular_candidates",
                    exception_type=type(exc).__name__,
                    text_latency_seconds=text_latency_seconds,
                )
                provider_failed = True
                text_hits = []
            else:
                text_latency_seconds = time.perf_counter() - text_started_seconds
            text_candidate_count = len(text_hits)
            for rank, hit in enumerate(text_hits, start=1):
                key = _candidate_key_from_hit(hit)
                if key is None:
                    continue
                candidate = candidates.setdefault(key, _CandidateScore())
                _set_candidate_ids(candidate, hit)
                candidate.text_raw = max(candidate.text_raw, _text_score_from_hit(hit, rank))

        if self._semantic_client is not None and query_vector is not None:
            semantic_started_seconds = time.perf_counter()
            try:
                semantic_hits = await self._semantic_client.search_memes_by_vector(
                    query_vector=query_vector,
                    limit=limit,
                    prefilter=prefilter,
                )
            except Exception as exc:
                semantic_latency_seconds = time.perf_counter() - semantic_started_seconds
                _log_discovery_degraded(
                    event="meme_search_provider_failure",
                    request_id=request_id,
                    surface=surface,
                    viewer_user_id=viewer_user_id,
                    filters=filters,
                    query=query,
                    source_algorithm="hybrid_search",
                    algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
                    degraded_component="semantic",
                    reason="semantic_search_failed",
                    fallback_reason="text_only_candidates",
                    exception_type=type(exc).__name__,
                    semantic_latency_seconds=semantic_latency_seconds,
                )
                provider_failed = True
                semantic_hits = ()
            else:
                semantic_latency_seconds = time.perf_counter() - semantic_started_seconds
            semantic_candidate_count = len(semantic_hits)
            for semantic_hit in semantic_hits:
                key = semantic_hit.meme_id
                candidate = candidates.setdefault(key, _CandidateScore(meme_id=semantic_hit.meme_id))
                candidate.meme_file_id = semantic_hit.meme_file_id
                candidate.semantic_raw = max(candidate.semantic_raw, semantic_hit.semantic_score)

        fallback_reason = None
        if not candidates:
            fallback_reason = "provider_failure" if provider_failed else "index_candidates_empty"
        return _CollectedCandidates(
            candidates=candidates,
            fallback_reason=fallback_reason,
            degraded_reason="provider_failure" if provider_failed else None,
            text_latency_seconds=text_latency_seconds,
            semantic_latency_seconds=semantic_latency_seconds,
            text_candidate_count=text_candidate_count,
            semantic_candidate_count=semantic_candidate_count,
        )

    async def _resolve_missing_meme_ids(self, candidates: dict[uuid.UUID, _CandidateScore]) -> None:
        missing_file_ids = tuple(
            score.meme_file_id
            for score in candidates.values()
            if score.meme_id is None and score.meme_file_id is not None
        )
        if not missing_file_ids:
            return

        result = await self._session.execute(
            select(MemeFile.id, MemeFile.meme_id).where(MemeFile.id.in_(missing_file_ids)),
        )
        file_to_meme_id: dict[uuid.UUID, uuid.UUID] = {file_id: meme_id for file_id, meme_id in result.all()}
        for score in candidates.values():
            if score.meme_id is None and score.meme_file_id is not None:
                score.meme_id = file_to_meme_id.get(score.meme_file_id)

    async def _load_visible_memes(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
    ) -> list[Meme]:
        stmt = _search_scope_meme_stmt(
            viewer_user_id,
            scope=filters.scope or _default_search_scope(viewer_user_id),
            collection_ids=filters.collection_ids,
        ).where(Meme.id.in_(meme_ids))
        stmt = _apply_filters(stmt, filters)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def _load_visible_meme_by_id(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None,
        include_nsfw: bool,
    ) -> Meme:
        stmt = _visible_meme_stmt(viewer_user_id).where(Meme.id == meme_id)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))
        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme was not found or is not visible to this caller.")
        return meme

    async def _load_visible_meme_by_slug(
        self,
        slug: str,
        *,
        viewer_user_id: uuid.UUID | None,
        include_nsfw: bool,
    ) -> Meme:
        normalized_slug = slug.strip().lower()
        stmt = _visible_meme_stmt(viewer_user_id).join(MemeSeoPage).where(MemeSeoPage.slug == normalized_slug)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))
        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme slug was not found or is not visible to this caller.")
        return meme

    async def _load_public_meme_by_id(
        self,
        meme_id: uuid.UUID,
        *,
        include_nsfw: bool,
    ) -> Meme:
        stmt = _public_meme_stmt().where(Meme.id == meme_id)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))
        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme was not found or is not publicly visible.")
        return meme

    async def _load_public_or_authorized_meme_by_id(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None,
        include_nsfw: bool,
    ) -> Meme:
        if viewer_user_id is None:
            return await self._load_public_meme_by_id(meme_id, include_nsfw=include_nsfw)
        return await self._load_visible_meme_by_id(
            meme_id,
            viewer_user_id=viewer_user_id,
            include_nsfw=include_nsfw,
        )

    async def _load_public_meme_by_slug(
        self,
        slug: str,
        *,
        include_nsfw: bool,
    ) -> Meme:
        normalized_slug = slug.strip().lower()
        stmt = _public_meme_stmt().join(MemeSeoPage).where(MemeSeoPage.slug == normalized_slug)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))
        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme slug was not found or is not publicly visible.")
        return meme

    async def _load_public_or_authorized_meme_by_slug(
        self,
        slug: str,
        *,
        viewer_user_id: uuid.UUID | None,
        include_nsfw: bool,
    ) -> Meme:
        if viewer_user_id is None:
            return await self._load_public_meme_by_slug(slug, include_nsfw=include_nsfw)
        return await self._load_visible_meme_by_slug(
            slug,
            viewer_user_id=viewer_user_id,
            include_nsfw=include_nsfw,
        )

    async def _qdrant_similar_candidates(
        self,
        source_meme: Meme,
        *,
        filters: MemeSearchFilters,
        target_count: int,
        request_id: str,
        surface: str,
        viewer_user_id: uuid.UUID | None,
    ) -> tuple[list[_SimilarCandidate], str | None]:
        if source_meme.primary_file_id is None:
            return [], "missing_embedding"

        vector = await self._load_primary_image_embedding_vector(
            source_meme.primary_file_id,
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=filters,
        )
        if vector is None:
            return [], "missing_embedding"

        if self._similarity_client is None:
            _log_discovery_degraded(
                event="meme_similar_provider_failure",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=filters,
                source_algorithm="qdrant_similarity",
                algorithm_version=QDRANT_SIMILARITY_ALGORITHM_VERSION,
                reason="qdrant_client_not_configured",
                fallback_reason="qdrant_failure",
            )
            return [], "qdrant_failure"

        semantic_started_seconds = time.perf_counter()
        try:
            matches = await self._similarity_client.find_similar_memes(
                vector=vector,
                current_meme_file_id=source_meme.primary_file_id,
                limit=max(20, target_count * 4),
            )
        except Exception as exc:
            semantic_latency_seconds = time.perf_counter() - semantic_started_seconds
            _log_discovery_degraded(
                event="meme_similar_provider_failure",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=filters,
                source_algorithm="qdrant_similarity",
                algorithm_version=QDRANT_SIMILARITY_ALGORITHM_VERSION,
                reason="qdrant_lookup_failed",
                fallback_reason="qdrant_failure",
                exception_type=type(exc).__name__,
                semantic_latency_seconds=semantic_latency_seconds,
            )
            return [], "qdrant_failure"

        ordered_matches = _dedupe_similarity_matches(matches, source_meme_id=source_meme.id)
        if not ordered_matches:
            return [], "similarity_empty"

        memes = await self._load_visible_memes(
            tuple(match.meme_id for match in ordered_matches),
            viewer_user_id=None,
            filters=filters,
        )
        await self._attach_derived_popularity_scores(memes)
        memes_by_id = {meme.id: meme for meme in memes}
        candidates: list[_SimilarCandidate] = []
        for match in ordered_matches:
            meme = memes_by_id.get(match.meme_id)
            if meme is None:
                continue
            candidates.append(
                _SimilarCandidate(
                    meme=meme,
                    source_algorithm="qdrant_similarity",
                    reason="qdrant_similarity",
                    score=match.similarity_score,
                    score_components={"similarity": match.similarity_score, "total": match.similarity_score},
                    algorithm_version=QDRANT_SIMILARITY_ALGORITHM_VERSION,
                )
            )
            if len(candidates) >= target_count:
                break

        return (candidates, None) if candidates else ([], "similarity_empty")

    async def _load_primary_image_embedding_vector(
        self,
        meme_file_id: uuid.UUID,
        *,
        request_id: str,
        surface: str,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
    ) -> tuple[float, ...] | None:
        cache_row = await self._session.scalar(
            select(EmbeddingCache)
            .where(
                EmbeddingCache.source_file_id == meme_file_id,
                EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
            )
            .order_by(EmbeddingCache.created_at.desc())
            .limit(1)
        )
        if cache_row is None:
            return None

        try:
            return decode_embedding_bytes(
                cache_row.embedding,
                dimensions=self._settings.pipeline_voyage_output_dimensions,
            )
        except VoyageEmbeddingError:
            _log_discovery_degraded(
                event="meme_similar_provider_failure",
                request_id=request_id,
                surface=surface,
                viewer_user_id=viewer_user_id,
                filters=filters,
                source_algorithm="qdrant_similarity",
                algorithm_version=QDRANT_SIMILARITY_ALGORITHM_VERSION,
                degraded_component="embedding",
                reason="stored_image_embedding_decode_failed",
                fallback_reason="missing_embedding",
                exception_type=VoyageEmbeddingError.__name__,
            )
            return None

    async def _similar_fallback_candidates(
        self,
        source_meme: Meme,
        *,
        filters: MemeSearchFilters,
        seen_meme_ids: set[uuid.UUID],
        target_count: int,
        reason: str,
    ) -> list[_SimilarCandidate]:
        candidates: list[_SimilarCandidate] = []
        source_tags = frozenset(tag.strip().lower() for tag in source_meme.tags if tag.strip())

        if source_tags:
            tag_predicate = or_(*(literal(tag) == any_(Meme.tags) for tag in source_tags))
            candidates.extend(
                await self._fallback_candidate_rows(
                    _apply_filters(_public_meme_stmt(), filters).where(tag_predicate),
                    seen_meme_ids=seen_meme_ids,
                    target_count=target_count,
                    source_algorithm="fallback_tag",
                    reason=reason,
                    source_tags=source_tags,
                )
            )

        if source_meme.template_id is not None and len(seen_meme_ids) - 1 < target_count:
            candidates.extend(
                await self._fallback_candidate_rows(
                    _apply_filters(_public_meme_stmt(), filters).where(Meme.template_id == source_meme.template_id),
                    seen_meme_ids=seen_meme_ids,
                    target_count=target_count,
                    source_algorithm="fallback_template",
                    reason=reason,
                    template_match=True,
                )
            )

        if len(seen_meme_ids) - 1 < target_count:
            candidates.extend(
                await self._fallback_candidate_rows(
                    _apply_filters(_public_meme_stmt(), filters),
                    seen_meme_ids=seen_meme_ids,
                    target_count=target_count,
                    source_algorithm="fallback_popular",
                    reason=reason,
                )
            )

        return candidates

    async def _fallback_candidate_rows(
        self,
        stmt: Select[tuple[Meme]],
        *,
        seen_meme_ids: set[uuid.UUID],
        target_count: int,
        source_algorithm: str,
        reason: str,
        source_tags: frozenset[str] | None = None,
        template_match: bool = False,
    ) -> list[_SimilarCandidate]:
        remaining = target_count - (len(seen_meme_ids) - 1)
        if remaining <= 0:
            return []

        if seen_meme_ids:
            stmt = stmt.where(~Meme.id.in_(tuple(seen_meme_ids)))
        result = await self._session.execute(stmt)
        all_memes = list(result.scalars().all())
        popularity_by_meme_id = await self._attach_derived_popularity_scores(all_memes)
        memes = sorted(
            all_memes,
            key=lambda meme: (popularity_by_meme_id.get(meme.id, 0.0), meme.created_at, str(meme.id)),
            reverse=True,
        )[:remaining]
        max_popularity = max((popularity_by_meme_id.get(meme.id, 0.0) for meme in memes), default=0.0)
        candidates: list[_SimilarCandidate] = []
        for meme in memes:
            if meme.id in seen_meme_ids:
                continue
            seen_meme_ids.add(meme.id)
            popularity = _normalize_value(popularity_by_meme_id.get(meme.id, 0.0), max_popularity)
            score = POPULARITY_WEIGHT * popularity
            score_components = {"popularity": popularity, "total": score}
            if source_tags is not None:
                score_components["tag_overlap"] = _tag_overlap_score(source_tags, meme.tags)
            if template_match:
                score_components["template_match"] = 1.0
            candidates.append(
                _SimilarCandidate(
                    meme=meme,
                    source_algorithm=source_algorithm,
                    reason=reason,
                    score=score,
                    score_components=score_components,
                    algorithm_version=POPULAR_ALGORITHM_VERSION,
                )
            )
        return candidates

    async def _to_public_search_page(
        self,
        page: MemeSearchPageRead,
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
    ) -> PublicMemeSearchPageRead:
        meme_ids = tuple(item.meme.id for item in page.items)
        if not meme_ids:
            return PublicMemeSearchPageRead(
                items=[],
                limit=page.limit,
                offset=page.offset,
                total=page.total,
                has_more=page.has_more,
                request_id=page.request_id,
            )
        use_authorized_cards = viewer_user_id is not None and filters.scope is not MemeSearchScope.PUBLIC
        stmt = (
            _search_scope_meme_stmt(
                viewer_user_id,
                scope=filters.scope or MemeSearchScope.PUBLIC,
                collection_ids=filters.collection_ids,
            )
            if use_authorized_cards
            else _public_meme_stmt()
        )
        result = await self._session.execute(stmt.where(Meme.id.in_(meme_ids)))
        memes_by_id = {meme.id: meme for meme in result.scalars().all()}
        for source_item in page.items:
            if meme := memes_by_id.get(source_item.meme.id):
                setattr(meme, _DERIVED_POPULARITY_ATTR, source_item.meme.popularity_score)
        action_state = await self._load_viewer_action_state(meme_ids, viewer_user_id=viewer_user_id)
        access_markers = await self._load_viewer_access_markers(
            tuple(memes_by_id.values()),
            viewer_user_id=viewer_user_id,
            filters=filters,
        )
        public_items = []
        for source_item in page.items:
            meme = memes_by_id.get(source_item.meme.id)
            if meme is None:
                continue
            public_items.append(
                PublicMemeSearchResultRead(
                    meme=(
                        _to_authorized_card_read(
                            meme,
                            media_render_service=self._media_render_service,
                            viewer_action_state=action_state,
                            viewer_access=access_markers.get(meme.id),
                        )
                        if use_authorized_cards
                        else _to_public_card_read(
                            meme,
                            media_render_service=self._media_render_service,
                            viewer_action_state=action_state,
                        )
                    ),
                    attribution=_with_request_id(source_item.attribution, page.request_id),
                )
            )
        return PublicMemeSearchPageRead(
            items=public_items,
            limit=page.limit,
            offset=page.offset,
            total=page.total,
            has_more=page.has_more,
            request_id=page.request_id,
        )

    async def _load_viewer_action_state(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID | None,
    ) -> _ViewerMemeActionState:
        if viewer_user_id is None or not meme_ids:
            return _ViewerMemeActionState()

        unique_meme_ids = tuple(dict.fromkeys(meme_ids))
        viewer_has_favorited = (
            select(CollectionMeme.meme_id)
            .join(Collection, Collection.id == CollectionMeme.collection_id)
            .where(
                CollectionMeme.meme_id == Meme.id,
                Collection.owner_id == viewer_user_id,
                Collection.kind == CollectionKind.FAVORITES,
            )
            .exists()
        )
        viewer_has_saved = (
            select(CollectionMeme.meme_id)
            .join(Collection, Collection.id == CollectionMeme.collection_id)
            .outerjoin(
                CollectionMember,
                and_(
                    CollectionMember.collection_id == Collection.id,
                    CollectionMember.user_id == viewer_user_id,
                ),
            )
            .where(
                CollectionMeme.meme_id == Meme.id,
                Collection.kind != CollectionKind.FAVORITES,
                or_(
                    Collection.owner_id == viewer_user_id,
                    CollectionMember.user_id == viewer_user_id,
                ),
            )
            .exists()
        )
        viewer_has_pinned = (
            select(PinnedMeme.meme_id)
            .where(
                PinnedMeme.meme_id == Meme.id,
                PinnedMeme.user_id == viewer_user_id,
            )
            .exists()
        )

        result = await self._session.execute(
            select(
                Meme.id,
                viewer_has_favorited.label("viewer_has_favorited"),
                viewer_has_saved.label("viewer_has_saved"),
                viewer_has_pinned.label("viewer_has_pinned"),
            ).where(Meme.id.in_(unique_meme_ids))
        )
        favorited_meme_ids: set[uuid.UUID] = set()
        saved_meme_ids: set[uuid.UUID] = set()
        pinned_meme_ids: set[uuid.UUID] = set()
        for meme_id, has_favorited, has_saved, has_pinned in result.all():
            if has_favorited:
                favorited_meme_ids.add(meme_id)
            if has_saved:
                saved_meme_ids.add(meme_id)
            if has_pinned:
                pinned_meme_ids.add(meme_id)
        return _ViewerMemeActionState(
            favorited_meme_ids=frozenset(favorited_meme_ids),
            saved_meme_ids=frozenset(saved_meme_ids),
            pinned_meme_ids=frozenset(pinned_meme_ids),
        )

    async def _load_viewer_access_markers(
        self,
        memes: tuple[Meme, ...],
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
    ) -> dict[uuid.UUID, PublicMemeViewerAccessRead]:
        if viewer_user_id is None or not memes or filters.scope is MemeSearchScope.PUBLIC:
            return {}

        collection_access = await self._load_collection_access_markers(
            tuple(meme.id for meme in memes),
            viewer_user_id=viewer_user_id,
            filters=filters,
        )
        markers: dict[uuid.UUID, PublicMemeViewerAccessRead] = {}
        for meme in memes:
            if meme.is_public:
                visibility = PublicMemeViewerAccess.PUBLIC
            else:
                visibility = collection_access.get(meme.id, PublicMemeViewerAccess.PRIVATE)
            markers[meme.id] = PublicMemeViewerAccessRead(visibility=visibility)
        return markers

    async def _load_detail_viewer_access_marker(
        self,
        meme: Meme,
        *,
        viewer_user_id: uuid.UUID | None,
    ) -> PublicMemeViewerAccessRead | None:
        if viewer_user_id is None or meme.is_public:
            return None
        markers = await self._load_viewer_access_markers(
            (meme,),
            viewer_user_id=viewer_user_id,
            filters=MemeSearchFilters(scope=MemeSearchScope.ALL),
        )
        return markers.get(meme.id)

    async def _load_collection_access_markers(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
    ) -> dict[uuid.UUID, PublicMemeViewerAccess]:
        stmt = (
            select(CollectionMeme.meme_id, Collection.owner_id)
            .select_from(CollectionMeme)
            .join(Collection, Collection.id == CollectionMeme.collection_id)
            .outerjoin(
                CollectionMember,
                and_(
                    CollectionMember.collection_id == Collection.id,
                    CollectionMember.user_id == viewer_user_id,
                ),
            )
            .where(
                CollectionMeme.meme_id.in_(meme_ids),
                or_(Collection.owner_id == viewer_user_id, CollectionMember.user_id.is_not(None)),
            )
        )
        if filters.scope is MemeSearchScope.COLLECTIONS:
            stmt = stmt.where(Collection.id.in_(filters.collection_ids))

        result = await self._session.execute(stmt)
        markers: dict[uuid.UUID, PublicMemeViewerAccess] = {}
        for meme_id, owner_id in result.all():
            if owner_id == viewer_user_id:
                markers[meme_id] = PublicMemeViewerAccess.PRIVATE
            else:
                markers.setdefault(meme_id, PublicMemeViewerAccess.SHARED)
        return markers

    async def load_public_meme_cards(
        self,
        meme_ids: Sequence[uuid.UUID],
        *,
        viewer_user_id: uuid.UUID | None,
    ) -> list[PublicMemeCardRead]:
        """Return visible public meme cards in caller-provided order with viewer action state."""

        if not meme_ids:
            return []

        unique_meme_ids = tuple(dict.fromkeys(meme_ids))
        result = await self._session.execute(
            _public_meme_stmt().where(Meme.id.in_(unique_meme_ids)),
        )
        memes_by_id = {meme.id: meme for meme in result.scalars().all()}
        await self._attach_derived_popularity_scores(tuple(memes_by_id.values()))
        action_state = await self._load_viewer_action_state(unique_meme_ids, viewer_user_id=viewer_user_id)
        return [
            _to_public_card_read(
                meme,
                media_render_service=self._media_render_service,
                viewer_action_state=action_state,
            )
            for meme_id in meme_ids
            if (meme := memes_by_id.get(meme_id)) is not None
        ]

    async def _popular_page(
        self,
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
        limit: int,
        offset: int,
        request_id: str,
        surface: str,
        source_algorithm: str,
        query: str | None = None,
        reason: str | None = None,
    ) -> MemeSearchPageRead:
        resolved_filters = _resolve_search_filters(filters, viewer_user_id=viewer_user_id)
        base_stmt = _apply_filters(
            _search_scope_meme_stmt(
                viewer_user_id,
                scope=resolved_filters.scope or _default_search_scope(viewer_user_id),
                collection_ids=resolved_filters.collection_ids,
            ),
            resolved_filters,
        )
        total = await self._session.scalar(select(func.count()).select_from(base_stmt.order_by(None).subquery())) or 0
        result = await self._session.execute(base_stmt)
        all_memes = list(result.scalars().all())
        popularity_by_meme_id = await self._attach_derived_popularity_scores(all_memes)
        sorted_memes = sorted(
            all_memes,
            key=lambda meme: (popularity_by_meme_id.get(meme.id, 0.0), meme.created_at, str(meme.id)),
            reverse=True,
        )
        memes = sorted_memes[offset : offset + limit]
        max_popularity = max((popularity_by_meme_id.get(meme.id, 0.0) for meme in memes), default=0.0)
        items = []
        for rank, meme in enumerate(memes, start=offset + 1):
            popularity = _normalize_value(popularity_by_meme_id.get(meme.id, 0.0), max_popularity)
            score = _CandidateScore(popularity=popularity, total=POPULARITY_WEIGHT * popularity)
            score_read = _to_score_read(score)
            items.append(
                MemeSearchResultRead(
                    meme=_to_card_read(meme),
                    score=score_read,
                    attribution=_build_result_attribution(
                        request_id=request_id,
                        surface=surface,
                        source_algorithm=source_algorithm,
                        rank=rank,
                        query=query,
                        filters=resolved_filters,
                        score=score_read.total,
                        score_components=_score_components(score_read),
                        algorithm_version=POPULAR_ALGORITHM_VERSION,
                        reason=reason,
                    ),
                )
            )
        return MemeSearchPageRead(
            items=items,
            limit=limit,
            offset=offset,
            total=total,
            has_more=offset + limit < total,
            request_id=request_id,
        )

    async def _attach_derived_popularity_scores(self, memes: Sequence[Meme]) -> dict[uuid.UUID, float]:
        meme_ids = tuple(dict.fromkeys(meme.id for meme in memes))
        scores = await load_derived_popularity_scores(self._session, meme_ids)
        for meme in memes:
            setattr(meme, _DERIVED_POPULARITY_ATTR, scores.get(meme.id, 0.0))
        return scores

    def _apply_normalized_scores(
        self,
        scores: dict[uuid.UUID, _CandidateScore],
        popularity_by_meme_id: dict[uuid.UUID, float],
    ) -> None:
        max_semantic = max((score.semantic_raw for score in scores.values()), default=0.0)
        max_text = max((score.text_raw for score in scores.values()), default=0.0)
        max_popularity = max(popularity_by_meme_id.values(), default=0.0)
        for meme_id, score in scores.items():
            score.semantic = _normalize_value(score.semantic_raw, max_semantic)
            score.text = _normalize_value(score.text_raw, max_text)
            score.popularity = _normalize_value(popularity_by_meme_id.get(meme_id, 0.0), max_popularity)
            score.total = (
                SEMANTIC_WEIGHT * score.semantic + TEXT_WEIGHT * score.text + POPULARITY_WEIGHT * score.popularity
            )


def _visible_meme_stmt(viewer_user_id: uuid.UUID | None) -> Select[tuple[Meme]]:
    return _search_scope_meme_stmt(viewer_user_id, scope=_default_search_scope(viewer_user_id))


def _search_scope_meme_stmt(
    viewer_user_id: uuid.UUID | None,
    *,
    scope: MemeSearchScope,
    collection_ids: tuple[uuid.UUID, ...] = (),
) -> Select[tuple[Meme]]:
    return _meme_stmt_with_files().where(
        _meme_access_predicate(viewer_user_id, scope=scope, collection_ids=collection_ids)
    )


def _public_meme_stmt() -> Select[tuple[Meme]]:
    return _meme_stmt_with_files().where(Meme.is_public.is_(True))


def _meme_stmt_with_files() -> Select[tuple[Meme]]:
    return select(Meme).options(
        selectinload(Meme.primary_file),
        selectinload(Meme.files),
        selectinload(Meme.seo_page),
    )


def _apply_filters(stmt: Select[tuple[Meme]], filters: MemeSearchFilters) -> Select[tuple[Meme]]:
    if filters.language is not None:
        stmt = stmt.where(Meme.language == filters.language)
    if filters.media_type is not None:
        stmt = stmt.where(Meme.media_type == filters.media_type)
    if not filters.include_nsfw:
        stmt = stmt.where(Meme.is_nsfw.is_(False))
    for tag in filters.tags:
        stmt = stmt.where(literal(tag) == any_(Meme.tags))
    return stmt


def _resolve_search_filters(
    filters: MemeSearchFilters | None,
    *,
    viewer_user_id: uuid.UUID | None,
    default_scope: MemeSearchScope | None = None,
) -> MemeSearchFilters:
    resolved_filters = filters or MemeSearchFilters()
    return replace(
        resolved_filters,
        scope=resolved_filters.scope or default_scope or _default_search_scope(viewer_user_id),
        collection_ids=_normalize_collection_ids(resolved_filters.collection_ids),
    )


def _default_search_scope(viewer_user_id: uuid.UUID | None) -> MemeSearchScope:
    return MemeSearchScope.ALL if viewer_user_id is not None else MemeSearchScope.PUBLIC


def _normalize_collection_ids(collection_ids: tuple[uuid.UUID, ...]) -> tuple[uuid.UUID, ...]:
    return tuple(dict.fromkeys(collection_ids))


def _log_discovery_completed(
    *,
    event: str,
    request_id: str,
    surface: str,
    viewer_user_id: uuid.UUID | None,
    filters: MemeSearchFilters,
    query: str | None,
    source_algorithm: str,
    algorithm_version: str | None,
    degraded_mode: bool,
    reason: str | None = None,
    fallback_reason: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    candidate_count: int | None = None,
    text_candidate_count: int | None = None,
    semantic_candidate_count: int | None = None,
    visible_count: int | None = None,
    result_count: int | None = None,
    total: int | None = None,
    embedding_latency_seconds: float | None = None,
    text_latency_seconds: float | None = None,
    semantic_latency_seconds: float | None = None,
    index_latency_seconds: float | None = None,
    db_latency_seconds: float | None = None,
    total_latency_seconds: float | None = None,
) -> None:
    logger.info(
        event,
        extra=_discovery_log_extra(
            event=event,
            request_id=request_id,
            surface=surface,
            viewer_user_id=viewer_user_id,
            filters=filters,
            query=query,
            source_algorithm=source_algorithm,
            algorithm_version=algorithm_version,
            degraded_mode=degraded_mode,
            reason=reason,
            fallback_reason=fallback_reason,
            limit=limit,
            offset=offset,
            candidate_count=candidate_count,
            text_candidate_count=text_candidate_count,
            semantic_candidate_count=semantic_candidate_count,
            visible_count=visible_count,
            result_count=result_count,
            total=total,
            embedding_latency_seconds=embedding_latency_seconds,
            text_latency_seconds=text_latency_seconds,
            semantic_latency_seconds=semantic_latency_seconds,
            index_latency_seconds=index_latency_seconds,
            db_latency_seconds=db_latency_seconds,
            total_latency_seconds=total_latency_seconds,
        ),
    )


def _log_discovery_degraded(
    *,
    event: str,
    request_id: str,
    surface: str,
    viewer_user_id: uuid.UUID | None,
    filters: MemeSearchFilters,
    source_algorithm: str,
    algorithm_version: str | None,
    query: str | None = None,
    degraded_component: str | None = None,
    reason: str | None = None,
    fallback_reason: str | None = None,
    exception_type: str | None = None,
    embedding_latency_seconds: float | None = None,
    text_latency_seconds: float | None = None,
    semantic_latency_seconds: float | None = None,
) -> None:
    extra = _discovery_log_extra(
        event=event,
        request_id=request_id,
        surface=surface,
        viewer_user_id=viewer_user_id,
        filters=filters,
        query=query,
        source_algorithm=source_algorithm,
        algorithm_version=algorithm_version,
        degraded_mode=True,
        reason=reason,
        fallback_reason=fallback_reason,
        embedding_latency_seconds=embedding_latency_seconds,
        text_latency_seconds=text_latency_seconds,
        semantic_latency_seconds=semantic_latency_seconds,
    )
    if degraded_component is not None:
        extra["degraded_component"] = degraded_component
    if exception_type is not None:
        extra["exception_type"] = exception_type
    logger.warning(event, extra=extra)


def _discovery_log_extra(
    *,
    event: str,
    request_id: str,
    surface: str,
    viewer_user_id: uuid.UUID | None,
    filters: MemeSearchFilters,
    query: str | None,
    source_algorithm: str,
    algorithm_version: str | None,
    degraded_mode: bool,
    reason: str | None = None,
    fallback_reason: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    candidate_count: int | None = None,
    text_candidate_count: int | None = None,
    semantic_candidate_count: int | None = None,
    visible_count: int | None = None,
    result_count: int | None = None,
    total: int | None = None,
    embedding_latency_seconds: float | None = None,
    text_latency_seconds: float | None = None,
    semantic_latency_seconds: float | None = None,
    index_latency_seconds: float | None = None,
    db_latency_seconds: float | None = None,
    total_latency_seconds: float | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": event,
        "request_id": request_id,
        "surface": surface,
        "source_algorithm": source_algorithm,
        "degraded_mode": degraded_mode,
        "query_present": bool(query),
        "query_length": len(query or ""),
        "scope": filters.scope.value if filters.scope is not None else None,
        "include_nsfw": filters.include_nsfw,
        "tag_count": len(filters.tags),
        "collection_count": len(filters.collection_ids),
        "filter_count": _search_filter_count(filters),
    }
    if viewer_user_id is not None:
        extra["user_id"] = str(viewer_user_id)
    if filters.language is not None:
        extra["language"] = filters.language.value
    if filters.media_type is not None:
        extra["media_type"] = filters.media_type.value
    _add_optional_log_field(extra, "algorithm_version", algorithm_version)
    _add_optional_log_field(extra, "reason", reason)
    _add_optional_log_field(extra, "fallback_reason", fallback_reason)
    _add_optional_log_field(extra, "limit", limit)
    _add_optional_log_field(extra, "offset", offset)
    _add_optional_log_field(extra, "candidate_count", candidate_count)
    _add_optional_log_field(extra, "text_candidate_count", text_candidate_count)
    _add_optional_log_field(extra, "semantic_candidate_count", semantic_candidate_count)
    _add_optional_log_field(extra, "visible_count", visible_count)
    _add_optional_log_field(extra, "result_count", result_count)
    _add_optional_log_field(extra, "total", total)
    _add_optional_log_field(extra, "embedding_latency_seconds", embedding_latency_seconds)
    _add_optional_log_field(extra, "text_latency_seconds", text_latency_seconds)
    _add_optional_log_field(extra, "semantic_latency_seconds", semantic_latency_seconds)
    _add_optional_log_field(extra, "index_latency_seconds", index_latency_seconds)
    _add_optional_log_field(extra, "db_latency_seconds", db_latency_seconds)
    _add_optional_log_field(extra, "total_latency_seconds", total_latency_seconds)
    return extra


def _add_optional_log_field(extra: dict[str, object], field_name: str, value: object | None) -> None:
    if value is not None:
        extra[field_name] = value


def _search_filter_count(filters: MemeSearchFilters) -> int:
    return sum(
        (
            filters.language is not None,
            filters.media_type is not None,
            filters.include_nsfw,
            bool(filters.tags),
            filters.scope is not None,
            bool(filters.collection_ids),
        )
    )


def _build_result_attribution(
    *,
    request_id: str,
    surface: str,
    source_algorithm: str,
    rank: int,
    query: str | None,
    filters: MemeSearchFilters,
    score: float | None = None,
    score_components: dict[str, float] | None = None,
    algorithm_version: str | None = None,
    source_meme_id: uuid.UUID | None = None,
    reason: str | None = None,
    impression_id: str | None = None,
) -> MemeResultAttributionRead:
    filter_read = _to_attribution_filters_read(filters)
    return MemeResultAttributionRead(
        request_id=request_id,
        impression_id=impression_id or new_discovery_impression_id(),
        surface=surface,
        source_algorithm=source_algorithm,
        rank=rank,
        query=query or None,
        filters=filter_read,
        collection_scope=filter_read.scope,
        collection_ids=list(filter_read.collection_ids),
        source_meme_id=source_meme_id,
        algorithm_version=algorithm_version,
        score=_safe_float(score),
        score_components=_safe_score_components(score_components or {}),
        reason=reason,
    )


def _to_attribution_filters_read(filters: MemeSearchFilters) -> MemeResultAttributionFiltersRead:
    return MemeResultAttributionFiltersRead(
        language=filters.language,
        media_type=filters.media_type,
        include_nsfw=filters.include_nsfw,
        tags=list(filters.tags),
        scope=filters.scope.value if filters.scope is not None else None,
        collection_ids=[str(collection_id) for collection_id in filters.collection_ids],
    )


def _score_components(score: MemeSearchScoreRead) -> dict[str, float]:
    return {
        "semantic": score.semantic,
        "text": score.text,
        "popularity": score.popularity,
        "total": score.total,
    }


def _similar_candidate_score_read(candidate: _SimilarCandidate) -> MemeSearchScoreRead:
    components = candidate.score_components or {}
    similarity = _safe_float(components.get("similarity")) or 0.0
    relationship = (
        _safe_float(components.get("tag_overlap"))
        or _safe_float(components.get("template_match"))
        or 0.0
    )
    popularity = _safe_float(components.get("popularity")) or 0.0
    total = _safe_float(candidate.score) or _safe_float(components.get("total")) or 0.0
    return MemeSearchScoreRead(
        semantic=similarity,
        text=relationship,
        popularity=popularity,
        total=total,
    )


def _safe_score_components(values: dict[str, float]) -> dict[str, float]:
    return {key: safe_value for key, value in values.items() if (safe_value := _safe_float(value)) is not None}


def _safe_float(value: float | None) -> float | None:
    if value is None:
        return None
    converted = float(value)
    if not math.isfinite(converted):
        return None
    return converted


def _with_request_id(attribution: MemeResultAttributionRead, request_id: str) -> MemeResultAttributionRead:
    return attribution.model_copy(update={"request_id": attribution.request_id or request_id})


def _build_search_index_prefilter(
    filters: MemeSearchFilters,
    *,
    viewer_user_id: uuid.UUID | None,
) -> SearchIndexPrefilter:
    return SearchIndexPrefilter(
        scope=_search_index_prefilter_scope(
            filters.scope or _default_search_scope(viewer_user_id),
            viewer_user_id=viewer_user_id,
        ),
        search_index_algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
        viewer_user_id=None if viewer_user_id is None else str(viewer_user_id),
        collection_ids=tuple(str(collection_id) for collection_id in filters.collection_ids),
        media_type=filters.media_type.value if filters.media_type is not None else None,
        language=filters.language.value if filters.language is not None else None,
        include_nsfw=filters.include_nsfw,
        tags=filters.tags,
    )


def _search_index_prefilter_scope(
    scope: MemeSearchScope,
    *,
    viewer_user_id: uuid.UUID | None,
) -> SearchIndexPrefilterScope:
    if viewer_user_id is None and scope is not MemeSearchScope.PUBLIC:
        return SearchIndexPrefilterScope.NONE
    if scope is MemeSearchScope.PUBLIC:
        return SearchIndexPrefilterScope.PUBLIC
    if scope is MemeSearchScope.PRIVATE:
        return SearchIndexPrefilterScope.PRIVATE
    if scope is MemeSearchScope.ALL:
        return SearchIndexPrefilterScope.ALL
    if scope is MemeSearchScope.COLLECTIONS:
        return SearchIndexPrefilterScope.COLLECTIONS
    raise ValueError(f"Unsupported meme search scope: {scope!r}")


def _dedupe_similarity_matches(
    matches: Sequence[QdrantSimilarityMatch],
    *,
    source_meme_id: uuid.UUID,
) -> list[QdrantSimilarityMatch]:
    seen_meme_ids = {source_meme_id}
    ordered_matches: list[QdrantSimilarityMatch] = []
    for match in matches:
        if match.meme_id in seen_meme_ids:
            continue
        seen_meme_ids.add(match.meme_id)
        ordered_matches.append(match)
    return ordered_matches


def _dedupe_user_search_matches(
    matches: Sequence[QdrantUserSearchMatch],
    *,
    excluded_meme_ids: set[uuid.UUID],
) -> list[QdrantUserSearchMatch]:
    seen_meme_ids = set(excluded_meme_ids)
    ordered_matches: list[QdrantUserSearchMatch] = []
    for match in matches:
        if match.meme_id in seen_meme_ids or _safe_float(match.semantic_score) is None:
            continue
        seen_meme_ids.add(match.meme_id)
        ordered_matches.append(match)
    return ordered_matches


def _add_positive_weight(weights: dict[uuid.UUID, float], meme_id: uuid.UUID, weight: float) -> None:
    if weight <= 0.0:
        return
    weights[meme_id] = weights.get(meme_id, 0.0) + weight


def _weighted_centroid(weighted_vectors: list[tuple[tuple[float, ...], float]]) -> tuple[float, ...] | None:
    sums: list[float] | None = None
    total_weight = 0.0
    for vector, weight in weighted_vectors:
        if weight <= 0.0 or not vector:
            continue
        safe_vector = tuple(float(value) for value in vector)
        if any(not math.isfinite(value) for value in safe_vector):
            continue
        if sums is None:
            sums = [0.0 for _ in safe_vector]
        if len(safe_vector) != len(sums):
            continue
        for index, value in enumerate(safe_vector):
            sums[index] += value * weight
        total_weight += weight

    if sums is None or total_weight <= 0.0:
        return None
    centroid = tuple(value / total_weight for value in sums)
    if all(value == 0.0 for value in centroid):
        return None
    return centroid


def _tag_overlap_score(source_tags: frozenset[str], candidate_tags: Sequence[str]) -> float:
    if not source_tags:
        return 0.0
    normalized_candidate_tags = {tag.strip().lower() for tag in candidate_tags if tag.strip()}
    return len(source_tags & normalized_candidate_tags) / len(source_tags)


def _meme_access_predicate(
    viewer_user_id: uuid.UUID | None,
    *,
    scope: MemeSearchScope,
    collection_ids: tuple[uuid.UUID, ...] = (),
) -> ColumnElement[bool]:
    if scope is MemeSearchScope.PUBLIC:
        return Meme.is_public.is_(True)
    if scope is MemeSearchScope.ALL and viewer_user_id is None:
        return Meme.is_public.is_(True)
    if viewer_user_id is None:
        return false()

    authorized_collection = _readable_collection_meme_exists(
        viewer_user_id,
        collection_ids=collection_ids if scope is MemeSearchScope.COLLECTIONS else (),
    )

    if scope is MemeSearchScope.PRIVATE:
        return and_(Meme.is_public.is_(False), authorized_collection)
    if scope is MemeSearchScope.ALL:
        return or_(
            Meme.is_public.is_(True),
            authorized_collection,
        )
    if scope is MemeSearchScope.COLLECTIONS:
        if not collection_ids:
            return false()
        return authorized_collection
    raise ValueError(f"Unsupported meme search scope: {scope!r}")


def _readable_collection_meme_exists(
    viewer_user_id: uuid.UUID,
    *,
    collection_ids: tuple[uuid.UUID, ...] = (),
) -> ColumnElement[bool]:
    stmt = (
        select(CollectionMeme.meme_id)
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .outerjoin(
            CollectionMember,
            and_(
                CollectionMember.collection_id == Collection.id,
                CollectionMember.user_id == viewer_user_id,
            ),
        )
        .where(
            CollectionMeme.meme_id == Meme.id,
            or_(Collection.owner_id == viewer_user_id, CollectionMember.user_id.is_not(None)),
        )
    )
    if collection_ids:
        stmt = stmt.where(Collection.id.in_(collection_ids))
    return stmt.exists()


def _candidate_key_from_hit(hit: dict[str, Any]) -> uuid.UUID | None:
    raw_id = hit.get("meme_id") or hit.get("id")
    return _parse_uuid(raw_id)


def _set_candidate_ids(candidate: _CandidateScore, hit: dict[str, Any]) -> None:
    meme_id = _parse_uuid(hit.get("meme_id"))
    file_id = _parse_uuid(hit.get("id") or hit.get("meme_file_id"))
    candidate.meme_id = candidate.meme_id or meme_id
    candidate.meme_file_id = candidate.meme_file_id or file_id


def _text_score_from_hit(hit: dict[str, Any], rank: int) -> float:
    for key in TEXT_SCORE_KEYS:
        raw_score = hit.get(key)
        if isinstance(raw_score, int | float):
            return max(0.0, float(raw_score))
    return 1.0 / max(1, rank)


def _parse_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _analytics_event_meme_id(payload: object) -> uuid.UUID | None:
    if not isinstance(payload, dict):
        return None
    flat_meme_id = _parse_uuid(payload.get("meme_id"))
    if flat_meme_id is not None:
        return flat_meme_id
    refs = payload.get("refs")
    if not isinstance(refs, dict):
        return None
    return _parse_uuid(refs.get("meme_id"))


def _is_collection_add_event(payload: object) -> bool:
    action = _analytics_event_action(payload)
    if action is None:
        return False
    normalized_action = action.strip().lower().replace("-", "_")
    return normalized_action in {
        "add",
        "added",
        "add_meme",
        "collection_add",
        "meme_add",
        "meme_save",
        "save",
        "saved",
    }


def _analytics_event_action(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    properties = payload.get("properties")
    if isinstance(properties, dict):
        action = properties.get("action")
        if isinstance(action, str):
            return action
    action = payload.get("action")
    return action if isinstance(action, str) else None


def _normalize_value(value: float, max_value: float) -> float:
    if max_value <= 0.0:
        return 0.0
    return max(0.0, min(1.0, value / max_value))


def _derived_popularity_score(meme: Meme) -> float:
    raw_value = getattr(meme, _DERIVED_POPULARITY_ATTR, 0.0)
    return float(raw_value or 0.0)


def _clamp_limit(limit: int) -> int:
    return min(100, max(1, limit))


def _to_file_read(file: MemeFile) -> MemeFileRead:
    return MemeFileRead(
        id=file.id,
        mime_type=file.mime_type,
        width=file.width,
        height=file.height,
        file_size_bytes=file.file_size_bytes,
        s3_original_key=file.s3_original_key,
        s3_web_video_key=file.s3_web_video_key,
        blur_hash=file.blur_hash,
        quality_score=file.quality_score,
    )


def _to_public_file_read(
    file: MemeFile,
    *,
    context: PublicMediaRenderContext,
    media_render_service: MediaRenderUrlService,
) -> PublicMemeFileRead:
    return PublicMemeFileRead(
        id=file.id,
        mime_type=file.mime_type,
        width=file.width,
        height=file.height,
        file_size_bytes=file.file_size_bytes,
        blur_hash=file.blur_hash,
        quality_score=file.quality_score,
        render=media_render_service.build_render(file, context=context),
    )


def _to_authorized_file_read(
    file: MemeFile,
    *,
    context: PublicMediaRenderContext,
    media_render_service: MediaRenderUrlService,
    is_public_meme: bool,
) -> PublicMemeFileRead:
    render = (
        media_render_service.build_render(file, context=context)
        if is_public_meme
        else media_render_service.build_private_render(file)
    )
    return PublicMemeFileRead(
        id=file.id,
        mime_type=file.mime_type,
        width=file.width,
        height=file.height,
        file_size_bytes=file.file_size_bytes,
        blur_hash=file.blur_hash,
        quality_score=file.quality_score,
        render=render,
    )


def _to_card_read(meme: Meme) -> MemeCardRead:
    return MemeCardRead(
        id=meme.id,
        media_type=meme.media_type,
        language=meme.language,
        is_nsfw=meme.is_nsfw,
        popularity_score=_derived_popularity_score(meme),
        like_count=meme.like_count,
        tags=list(meme.tags),
        primary_file=_to_file_read(meme.primary_file) if meme.primary_file else None,
        caption=meme.seo_page.caption if meme.seo_page else None,
        seo_page_slug=meme.seo_page.slug if meme.seo_page else None,
        created_at=meme.created_at,
        updated_at=meme.updated_at,
    )


def _to_public_card_read(
    meme: Meme,
    *,
    media_render_service: MediaRenderUrlService,
    viewer_action_state: _ViewerMemeActionState | None = None,
) -> PublicMemeCardRead:
    seo_slug = meme.seo_page.slug if meme.seo_page else None
    caption = meme.seo_page.caption if meme.seo_page else None
    context = PublicMediaRenderContext(meme_id=meme.id, seo_slug=seo_slug, caption=caption)
    return PublicMemeCardRead(
        id=meme.id,
        media_type=meme.media_type,
        language=meme.language,
        is_nsfw=meme.is_nsfw,
        popularity_score=_derived_popularity_score(meme),
        like_count=meme.like_count,
        tags=list(meme.tags),
        primary_file=(
            _to_public_file_read(
                meme.primary_file,
                context=context,
                media_render_service=media_render_service,
            )
            if meme.primary_file
            else None
        ),
        caption=caption,
        seo_page_slug=seo_slug,
        viewer_has_favorited=viewer_action_state.has_favorited(meme.id) if viewer_action_state else False,
        viewer_has_saved=viewer_action_state.has_saved(meme.id) if viewer_action_state else False,
        viewer_has_pinned=viewer_action_state.has_pinned(meme.id) if viewer_action_state else False,
        created_at=meme.created_at,
        updated_at=meme.updated_at,
    )


def _to_authorized_card_read(
    meme: Meme,
    *,
    media_render_service: MediaRenderUrlService,
    viewer_action_state: _ViewerMemeActionState | None = None,
    viewer_access: PublicMemeViewerAccessRead | None = None,
) -> PublicMemeCardRead:
    seo_slug = meme.seo_page.slug if meme.seo_page else None
    caption = meme.seo_page.caption if meme.seo_page else None
    context = PublicMediaRenderContext(meme_id=meme.id, seo_slug=seo_slug, caption=caption)
    return PublicMemeCardRead(
        id=meme.id,
        media_type=meme.media_type,
        language=meme.language,
        is_nsfw=meme.is_nsfw,
        popularity_score=_derived_popularity_score(meme),
        like_count=meme.like_count,
        tags=list(meme.tags),
        primary_file=(
            _to_authorized_file_read(
                meme.primary_file,
                context=context,
                media_render_service=media_render_service,
                is_public_meme=meme.is_public,
            )
            if meme.primary_file
            else None
        ),
        caption=caption,
        seo_page_slug=seo_slug,
        viewer_has_favorited=viewer_action_state.has_favorited(meme.id) if viewer_action_state else False,
        viewer_has_saved=viewer_action_state.has_saved(meme.id) if viewer_action_state else False,
        viewer_has_pinned=viewer_action_state.has_pinned(meme.id) if viewer_action_state else False,
        viewer_access=viewer_access,
        created_at=meme.created_at,
        updated_at=meme.updated_at,
    )


def _to_detail_read(meme: Meme) -> MemeDetailRead:
    card = _to_card_read(meme)
    return MemeDetailRead(
        **card.model_dump(),
        ocr_text=meme.ocr_text,
        is_public=meme.is_public,
        seo_title=meme.seo_page.page_title if meme.seo_page else None,
        seo_description=meme.seo_page.meta_description if meme.seo_page else None,
        seo_alt_text=meme.seo_page.alt_text if meme.seo_page else None,
        seo_body_text=meme.seo_page.body_text if meme.seo_page else None,
        seo_model_id=meme.seo_page.model_id if meme.seo_page else None,
        seo_prompt_version=meme.seo_page.prompt_version if meme.seo_page else None,
        seo_generated_at=meme.seo_page.generated_at if meme.seo_page else None,
        files=[_to_file_read(file) for file in meme.files],
    )


def _to_public_detail_read(
    meme: Meme,
    *,
    media_render_service: MediaRenderUrlService,
    viewer_action_state: _ViewerMemeActionState | None = None,
) -> PublicMemeDetailRead:
    card = _to_public_card_read(
        meme,
        media_render_service=media_render_service,
        viewer_action_state=viewer_action_state,
    )
    seo_slug = meme.seo_page.slug if meme.seo_page else None
    caption = meme.seo_page.caption if meme.seo_page else None
    context = PublicMediaRenderContext(meme_id=meme.id, seo_slug=seo_slug, caption=caption)
    return PublicMemeDetailRead(
        **card.model_dump(),
        ocr_text=meme.ocr_text,
        seo_title=meme.seo_page.page_title if meme.seo_page else None,
        seo_description=meme.seo_page.meta_description if meme.seo_page else None,
        seo_alt_text=meme.seo_page.alt_text if meme.seo_page else None,
        seo_body_text=meme.seo_page.body_text if meme.seo_page else None,
        seo_model_id=meme.seo_page.model_id if meme.seo_page else None,
        seo_prompt_version=meme.seo_page.prompt_version if meme.seo_page else None,
        seo_generated_at=meme.seo_page.generated_at if meme.seo_page else None,
        files=[
            _to_public_file_read(file, context=context, media_render_service=media_render_service)
            for file in meme.files
        ],
    )


def _to_authorized_detail_read(
    meme: Meme,
    *,
    media_render_service: MediaRenderUrlService,
    viewer_action_state: _ViewerMemeActionState | None = None,
    viewer_access: PublicMemeViewerAccessRead | None = None,
) -> PublicMemeDetailRead:
    card = _to_authorized_card_read(
        meme,
        media_render_service=media_render_service,
        viewer_action_state=viewer_action_state,
        viewer_access=viewer_access,
    )
    seo_slug = meme.seo_page.slug if meme.seo_page else None
    caption = meme.seo_page.caption if meme.seo_page else None
    context = PublicMediaRenderContext(meme_id=meme.id, seo_slug=seo_slug, caption=caption)
    return PublicMemeDetailRead(
        **card.model_dump(),
        ocr_text=meme.ocr_text,
        seo_title=meme.seo_page.page_title if meme.seo_page else None,
        seo_description=meme.seo_page.meta_description if meme.seo_page else None,
        seo_alt_text=meme.seo_page.alt_text if meme.seo_page else None,
        seo_body_text=meme.seo_page.body_text if meme.seo_page else None,
        seo_model_id=meme.seo_page.model_id if meme.seo_page else None,
        seo_prompt_version=meme.seo_page.prompt_version if meme.seo_page else None,
        seo_generated_at=meme.seo_page.generated_at if meme.seo_page else None,
        files=[
            _to_authorized_file_read(
                file,
                context=context,
                media_render_service=media_render_service,
                is_public_meme=meme.is_public,
            )
            for file in meme.files
        ],
    )


def _to_score_read(score: _CandidateScore) -> MemeSearchScoreRead:
    return MemeSearchScoreRead(
        semantic=score.semantic,
        text=score.text,
        popularity=score.popularity,
        total=score.total,
    )


__all__ = [
    "MemeNotFoundError",
    "MemeSearchFilters",
    "MemeSearchScope",
    "MemeSearchService",
    "MemeQueryEmbeddingClientProtocol",
    "MemeTextSearchClientProtocol",
    "QdrantSimilarityMatch",
    "QdrantUserSearchMatch",
]
