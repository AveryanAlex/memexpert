# ruff: noqa: TC001,TC003
"""Bounded chronological recommendation evaluation over PostgreSQL artifacts."""

from __future__ import annotations

import heapq
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from sqlalchemy import text

from memexpert.core.config import Settings
from memexpert.services.recommendations.evaluation import (
    EvaluationItem,
    RecommendationEvaluation,
    evaluate_ranking,
)
from memexpert.services.recommendations.math import (
    exponential_decay,
    normalize_vector,
    weighted_centroid,
)
from memexpert.services.recommendations.profiles import ProfileSignalVector, build_profile_vectors

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_MAX_USERS = 25
DEFAULT_MAX_CATALOG = 1_000
DEFAULT_MAX_CASES = 50
DEFAULT_K = 50

HARD_MAX_USERS = 500
HARD_MAX_CATALOG = 50_000
HARD_MAX_CASES = 10_000
HARD_MAX_K = 200
MAX_HISTORY_PER_USER = 500
MINIMUM_HISTORY_ITEMS = 2
MULTI_POSITIVE_EXAMPLE_LIMIT = 20


class OfflineRetrievalVariant(StrEnum):
    """Local retrieval representations compared by the chronological replay."""

    CURRENT_CENTROID = "current_centroid"
    TWO_PROFILE = "two_profile"
    CLUSTERED = "clustered"
    MULTI_POSITIVE = "multi_positive"


@dataclass(frozen=True, slots=True)
class OfflineEvaluationBounds:
    """Operator-controlled bounds enforced before any evaluation work."""

    max_users: int = DEFAULT_MAX_USERS
    max_catalog: int = DEFAULT_MAX_CATALOG
    max_cases: int = DEFAULT_MAX_CASES
    k: int = DEFAULT_K

    def __post_init__(self) -> None:
        _require_bound("max_users", self.max_users, HARD_MAX_USERS)
        _require_bound("max_catalog", self.max_catalog, HARD_MAX_CATALOG)
        _require_bound("max_cases", self.max_cases, HARD_MAX_CASES)
        _require_bound("k", self.k, HARD_MAX_K)
        if self.k > self.max_catalog:
            raise ValueError("k must not exceed max_catalog")


@dataclass(frozen=True, slots=True)
class OfflineEvaluationPolicy:
    """Serving-aligned decay and clustering inputs used by every replay case."""

    current_window_hours: int = 168
    short_term_window_hours: int = 168
    short_term_half_life_hours: float = 24.0
    long_term_half_life_days: float = 90.0
    cluster_activation_signals: int = 20
    cluster_min_items: int = 3
    cluster_iterations: int = 5
    multi_positive_limit: int = MULTI_POSITIVE_EXAMPLE_LIMIT

    @classmethod
    def from_settings(cls, settings: Settings) -> OfflineEvaluationPolicy:
        return cls(
            current_window_hours=settings.recommendation_positive_lookback_hours,
            short_term_window_hours=settings.recommendation_short_term_window_hours,
            short_term_half_life_hours=settings.recommendation_short_term_half_life_hours,
            long_term_half_life_days=settings.recommendation_long_term_half_life_days,
            cluster_activation_signals=settings.recommendation_cluster_activation_signals,
            cluster_min_items=settings.recommendation_cluster_min_items,
            cluster_iterations=settings.recommendation_cluster_iterations,
        )


@dataclass(frozen=True, slots=True)
class OfflineCatalogItem:
    meme_id: uuid.UUID
    embedding: tuple[float, ...]
    available_at: datetime
    source_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RecommendationObservation:
    meme_id: uuid.UUID
    occurred_at: datetime
    weight: float
    is_strong_positive: bool = True


@dataclass(frozen=True, slots=True)
class UserPositiveHistory:
    """Internal-only history. User identity is never copied into a report."""

    user_id: uuid.UUID
    observations: tuple[RecommendationObservation, ...]


@dataclass(frozen=True, slots=True)
class ChronologicalEvaluationCase:
    user_id: uuid.UUID
    cutoff_at: datetime
    history: tuple[RecommendationObservation, ...]
    target_meme_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class VariantEvaluationSummary:
    cases: int
    recall_at_k: float
    ndcg_at_k: float
    catalog_coverage: float
    source_concentration: float
    template_concentration: float
    intra_list_diversity: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "cases": self.cases,
            "recall_at_k": _rounded(self.recall_at_k),
            "ndcg_at_k": _rounded(self.ndcg_at_k),
            "catalog_coverage": _rounded(self.catalog_coverage),
            "source_concentration": _rounded(self.source_concentration),
            "template_concentration": _rounded(self.template_concentration),
            "intra_list_diversity": _rounded(self.intra_list_diversity),
        }


@dataclass(frozen=True, slots=True)
class OfflineEvaluationReport:
    generated_at: datetime
    bounds: OfflineEvaluationBounds
    embedding_dimensions: int
    catalog_items: int
    histories_loaded: int
    eligible_users: int
    observations_loaded: int
    cases: int
    variants: Mapping[OfflineRetrievalVariant, VariantEvaluationSummary]

    def to_dict(self) -> dict[str, object]:
        """Return an aggregate-only payload with no user, query, or vector data."""

        return {
            "schema_version": 1,
            "mode": "chronological_read_only",
            "generated_at": _as_utc(self.generated_at).isoformat(),
            "bounds": {
                "max_users": self.bounds.max_users,
                "max_catalog": self.bounds.max_catalog,
                "max_cases": self.bounds.max_cases,
                "k": self.bounds.k,
            },
            "sample": {
                "embedding_dimensions": self.embedding_dimensions,
                "catalog_items": self.catalog_items,
                "histories_loaded": self.histories_loaded,
                "eligible_users": self.eligible_users,
                "observations_loaded": self.observations_loaded,
                "cases": self.cases,
            },
            "variants": {
                variant.value: self.variants[variant].to_dict()
                for variant in OfflineRetrievalVariant
            },
        }


class PostgresOfflineEvaluationLoader:
    """Read bounded catalog/history snapshots without any provider dependency."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def load_catalog(self, *, limit: int) -> list[OfflineCatalogItem]:
        result = await self._session.execute(
            text(_CATALOG_SQL),
            {
                "embedding_bytes": self._settings.pipeline_voyage_output_dimensions * 4,
                "model_version": self._settings.pipeline_voyage_model,
                "limit": limit,
                "scan_limit": min(HARD_MAX_CATALOG, max(limit, limit * 5)),
            },
        )
        catalog: list[OfflineCatalogItem] = []
        for row in result:
            raw_embedding = cast("bytes", row.embedding)
            normalized = _decode_normalized_vector(
                raw_embedding,
                dimensions=self._settings.pipeline_voyage_output_dimensions,
            )
            if normalized is None:
                continue
            catalog.append(
                OfflineCatalogItem(
                    meme_id=cast("uuid.UUID", row.meme_id),
                    embedding=normalized,
                    available_at=_as_utc(cast("datetime", row.available_at)),
                    source_id=cast("uuid.UUID | None", row.source_id),
                    template_id=cast("uuid.UUID | None", row.template_id),
                )
            )
        return catalog

    async def load_histories(
        self,
        *,
        catalog_meme_ids: Sequence[uuid.UUID],
        max_users: int,
    ) -> list[UserPositiveHistory]:
        if not catalog_meme_ids:
            return []
        result = await self._session.execute(
            text(_HISTORIES_SQL),
            {
                "catalog_meme_ids": tuple(catalog_meme_ids),
                "max_users": max_users,
                "candidate_user_limit": min(HARD_MAX_USERS, max_users * 5),
                "minimum_history": MINIMUM_HISTORY_ITEMS,
                "max_history_per_user": MAX_HISTORY_PER_USER,
            },
        )
        by_user: dict[uuid.UUID, list[RecommendationObservation]] = {}
        for row in result:
            user_id = cast("uuid.UUID", row.user_id)
            by_user.setdefault(user_id, []).append(
                RecommendationObservation(
                    meme_id=cast("uuid.UUID", row.meme_id),
                    occurred_at=_as_utc(cast("datetime", row.occurred_at)),
                    weight=float(row.weight),
                    is_strong_positive=bool(row.is_strong_positive),
                )
            )
        return [
            UserPositiveHistory(
                user_id=user_id,
                observations=tuple(
                    sorted(observations, key=lambda item: (item.occurred_at, item.meme_id.int))
                ),
            )
            for user_id, observations in by_user.items()
        ]


async def evaluate_postgres_recommendations(
    session: AsyncSession,
    *,
    settings: Settings,
    bounds: OfflineEvaluationBounds,
    generated_at: datetime | None = None,
) -> OfflineEvaluationReport:
    """Load one bounded snapshot and evaluate it entirely in process."""

    loader = PostgresOfflineEvaluationLoader(session, settings=settings)
    catalog = await loader.load_catalog(limit=bounds.max_catalog)
    histories = await loader.load_histories(
        catalog_meme_ids=tuple(item.meme_id for item in catalog),
        max_users=bounds.max_users,
    )
    return evaluate_chronological_retrieval(
        catalog=catalog,
        histories=histories,
        bounds=bounds,
        policy=OfflineEvaluationPolicy.from_settings(settings),
        generated_at=generated_at,
    )


def build_chronological_cases(
    *,
    catalog: Sequence[OfflineCatalogItem],
    histories: Sequence[UserPositiveHistory],
    max_users: int,
    max_cases: int,
    minimum_history: int = MINIMUM_HISTORY_ITEMS,
) -> list[ChronologicalEvaluationCase]:
    """Build rolling next-positive cases without using same-time or future data."""

    catalog_by_id = {item.meme_id: item for item in catalog}
    cases_by_user: list[list[ChronologicalEvaluationCase]] = []
    for user_history in histories[:max_users]:
        available_observations: list[RecommendationObservation] = []
        for observation in user_history.observations:
            item = catalog_by_id.get(observation.meme_id)
            if item is None or item.available_at > observation.occurred_at:
                continue
            available_observations.append(observation)
        ordered = sorted(
            available_observations,
            key=lambda item: (item.occurred_at, item.meme_id.int),
        )
        earliest_targets: dict[uuid.UUID, RecommendationObservation] = {}
        for observation in ordered:
            if observation.is_strong_positive and observation.meme_id not in earliest_targets:
                earliest_targets[observation.meme_id] = observation
        user_cases: list[ChronologicalEvaluationCase] = []
        for target in earliest_targets.values():
            history = tuple(
                item
                for item in ordered
                if item.occurred_at < target.occurred_at and item.meme_id != target.meme_id
            )
            if len({item.meme_id for item in history}) < minimum_history:
                continue
            user_cases.append(
                ChronologicalEvaluationCase(
                    user_id=user_history.user_id,
                    cutoff_at=target.occurred_at,
                    history=history,
                    target_meme_id=target.meme_id,
                )
            )
        user_cases.sort(key=lambda item: (item.cutoff_at, item.target_meme_id.int), reverse=True)
        if user_cases:
            cases_by_user.append(user_cases)

    # Round-robin recent cases so a single highly active account cannot consume
    # the entire case budget while every case remains a strict temporal holdout.
    selected: list[ChronologicalEvaluationCase] = []
    depth = 0
    while len(selected) < max_cases:
        added = False
        for user_cases in cases_by_user:
            if depth >= len(user_cases):
                continue
            selected.append(user_cases[depth])
            added = True
            if len(selected) >= max_cases:
                break
        if not added:
            break
        depth += 1
    return selected


def evaluate_chronological_retrieval(
    *,
    catalog: Sequence[OfflineCatalogItem],
    histories: Sequence[UserPositiveHistory],
    bounds: OfflineEvaluationBounds,
    policy: OfflineEvaluationPolicy | None = None,
    generated_at: datetime | None = None,
) -> OfflineEvaluationReport:
    """Compare four local retrieval representations over chronological cases."""

    resolved_policy = policy or OfflineEvaluationPolicy()
    normalized_catalog = _normalize_catalog(catalog[: bounds.max_catalog])
    catalog_by_id = {item.meme_id: item for item in normalized_catalog}
    cases = build_chronological_cases(
        catalog=normalized_catalog,
        histories=histories,
        max_users=bounds.max_users,
        max_cases=bounds.max_cases,
    )
    evaluations: dict[OfflineRetrievalVariant, list[RecommendationEvaluation]] = {
        variant: [] for variant in OfflineRetrievalVariant
    }
    recommended_ids: dict[OfflineRetrievalVariant, set[uuid.UUID]] = {
        variant: set() for variant in OfflineRetrievalVariant
    }

    for case in cases:
        for variant in OfflineRetrievalVariant:
            ranked = rank_chronological_case(
                case,
                catalog=normalized_catalog,
                catalog_by_id=catalog_by_id,
                variant=variant,
                k=bounds.k,
                policy=resolved_policy,
            )
            recommended_ids[variant].update(item.meme_id for item in ranked)
            evaluations[variant].append(
                evaluate_ranking(
                    ranked,
                    relevant_meme_ids={case.target_meme_id},
                    catalog_size=len(normalized_catalog),
                    k=bounds.k,
                )
            )

    summaries = {
        variant: _aggregate_variant(
            evaluations[variant],
            recommended_meme_ids=recommended_ids[variant],
            catalog_size=len(normalized_catalog),
        )
        for variant in OfflineRetrievalVariant
    }
    eligible_users = len({case.user_id for case in cases})
    observations_loaded = sum(len(history.observations) for history in histories[: bounds.max_users])
    dimensions = len(normalized_catalog[0].embedding) if normalized_catalog else 0
    return OfflineEvaluationReport(
        generated_at=generated_at or datetime.now(UTC),
        bounds=bounds,
        embedding_dimensions=dimensions,
        catalog_items=len(normalized_catalog),
        histories_loaded=min(len(histories), bounds.max_users),
        eligible_users=eligible_users,
        observations_loaded=observations_loaded,
        cases=len(cases),
        variants=summaries,
    )


def rank_chronological_case(
    case: ChronologicalEvaluationCase,
    *,
    catalog: Sequence[OfflineCatalogItem],
    catalog_by_id: Mapping[uuid.UUID, OfflineCatalogItem],
    variant: OfflineRetrievalVariant,
    k: int,
    policy: OfflineEvaluationPolicy,
) -> list[EvaluationItem]:
    """Rank at most K locally, excluding seen and not-yet-available content."""

    vectors, combination = _variant_vectors(
        case,
        catalog_by_id=catalog_by_id,
        variant=variant,
        policy=policy,
    )
    if not vectors or k <= 0:
        return []
    seen_ids = {observation.meme_id for observation in case.history}
    heap: list[tuple[float, int, OfflineCatalogItem]] = []
    for item in catalog:
        if item.meme_id in seen_ids or item.available_at > case.cutoff_at:
            continue
        similarities = tuple(_unit_dot(item.embedding, vector) for vector in vectors)
        score = sum(similarities) / len(similarities) if combination == "mean" else max(similarities)
        entry = (score, -item.meme_id.int, item)
        if len(heap) < k:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)
    ordered = sorted(heap, key=lambda entry: entry[:2], reverse=True)
    return [
        EvaluationItem(
            meme_id=item.meme_id,
            relevance=1.0 if item.meme_id == case.target_meme_id else 0.0,
            source_id=item.source_id,
            template_id=item.template_id,
            embedding=item.embedding,
        )
        for _score, _tie_breaker, item in ordered
    ]


def _variant_vectors(
    case: ChronologicalEvaluationCase,
    *,
    catalog_by_id: Mapping[uuid.UUID, OfflineCatalogItem],
    variant: OfflineRetrievalVariant,
    policy: OfflineEvaluationPolicy,
) -> tuple[tuple[tuple[float, ...], ...], str]:
    if variant is OfflineRetrievalVariant.CURRENT_CENTROID:
        since = case.cutoff_at - timedelta(hours=policy.current_window_hours)
        centroid = weighted_centroid(
            (catalog_by_id[item.meme_id].embedding, item.weight)
            for item in case.history
            if item.occurred_at >= since and item.meme_id in catalog_by_id
        )
        return ((centroid,) if centroid is not None else ()), "max"

    if variant is OfflineRetrievalVariant.TWO_PROFILE:
        short = _decayed_centroid(
            case,
            catalog_by_id=catalog_by_id,
            half_life_seconds=policy.short_term_half_life_hours * 3600.0,
            window=timedelta(hours=policy.short_term_window_hours),
        )
        long = _decayed_centroid(
            case,
            catalog_by_id=catalog_by_id,
            half_life_seconds=policy.long_term_half_life_days * 86400.0,
        )
        return tuple(vector for vector in (short, long) if vector is not None), "mean"

    if variant is OfflineRetrievalVariant.CLUSTERED:
        profile_signals = _profile_signals(case, catalog_by_id=catalog_by_id, policy=policy)
        profiles = build_profile_vectors(
            profile_signals,
            activation_threshold=policy.cluster_activation_signals,
            min_cluster_items=policy.cluster_min_items,
            max_iterations=policy.cluster_iterations,
        )
        return tuple(profile.vector for profile in profiles), "max"

    recent = sorted(
        (item for item in case.history if item.is_strong_positive),
        key=lambda item: (item.occurred_at, item.meme_id.int),
        reverse=True,
    )[: policy.multi_positive_limit]
    return tuple(
        catalog_by_id[item.meme_id].embedding
        for item in recent
        if item.meme_id in catalog_by_id
    ), "max"


def _decayed_centroid(
    case: ChronologicalEvaluationCase,
    *,
    catalog_by_id: Mapping[uuid.UUID, OfflineCatalogItem],
    half_life_seconds: float,
    window: timedelta | None = None,
) -> tuple[float, ...] | None:
    since = case.cutoff_at - window if window is not None else None
    return weighted_centroid(
        (
            catalog_by_id[item.meme_id].embedding,
            item.weight
            * exponential_decay(
                age_seconds=(case.cutoff_at - item.occurred_at).total_seconds(),
                half_life_seconds=half_life_seconds,
            ),
        )
        for item in case.history
        if item.meme_id in catalog_by_id and (since is None or item.occurred_at >= since)
    )


def _profile_signals(
    case: ChronologicalEvaluationCase,
    *,
    catalog_by_id: Mapping[uuid.UUID, OfflineCatalogItem],
    policy: OfflineEvaluationPolicy,
) -> list[ProfileSignalVector]:
    half_life_seconds = policy.long_term_half_life_days * 86400.0
    aggregated: dict[uuid.UUID, tuple[float, datetime, bool]] = {}
    for item in case.history:
        if item.meme_id not in catalog_by_id:
            continue
        decayed_weight = item.weight * exponential_decay(
            age_seconds=(case.cutoff_at - item.occurred_at).total_seconds(),
            half_life_seconds=half_life_seconds,
        )
        current_weight, current_at, current_strong = aggregated.get(
            item.meme_id,
            (0.0, item.occurred_at, False),
        )
        aggregated[item.meme_id] = (
            current_weight + decayed_weight,
            max(current_at, item.occurred_at),
            current_strong or item.is_strong_positive,
        )
    return [
        ProfileSignalVector(
            meme_id=meme_id,
            vector=catalog_by_id[meme_id].embedding,
            weight=weight,
            last_signal_at=last_signal_at,
            is_strong_positive=is_strong_positive,
        )
        for meme_id, (weight, last_signal_at, is_strong_positive) in aggregated.items()
    ]


def _normalize_catalog(catalog: Sequence[OfflineCatalogItem]) -> list[OfflineCatalogItem]:
    normalized: list[OfflineCatalogItem] = []
    dimensions: int | None = None
    seen: set[uuid.UUID] = set()
    for item in catalog:
        vector = normalize_vector(item.embedding)
        if vector is None or item.meme_id in seen:
            continue
        if dimensions is None:
            dimensions = len(vector)
        if len(vector) != dimensions:
            continue
        seen.add(item.meme_id)
        normalized.append(
            OfflineCatalogItem(
                meme_id=item.meme_id,
                embedding=vector,
                available_at=_as_utc(item.available_at),
                source_id=item.source_id,
                template_id=item.template_id,
            )
        )
    return normalized


def _aggregate_variant(
    evaluations: Sequence[RecommendationEvaluation],
    *,
    recommended_meme_ids: set[uuid.UUID],
    catalog_size: int,
) -> VariantEvaluationSummary:
    count = len(evaluations)
    if count == 0:
        return VariantEvaluationSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return VariantEvaluationSummary(
        cases=count,
        recall_at_k=sum(item.recall_at_k for item in evaluations) / count,
        ndcg_at_k=sum(item.ndcg_at_k for item in evaluations) / count,
        catalog_coverage=len(recommended_meme_ids) / catalog_size if catalog_size else 0.0,
        source_concentration=sum(item.source_concentration for item in evaluations) / count,
        template_concentration=sum(item.template_concentration for item in evaluations) / count,
        intra_list_diversity=sum(item.intra_list_diversity for item in evaluations) / count,
    )


def _decode_normalized_vector(value: bytes, *, dimensions: int) -> tuple[float, ...] | None:
    from memexpert.services.recommendations.math import decode_vector

    try:
        return normalize_vector(decode_vector(value, dimensions=dimensions))
    except ValueError:
        return None


def _unit_dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return -1.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def _require_bound(name: str, value: int, maximum: int) -> None:
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _rounded(value: float) -> float:
    return round(value, 6)


_CATALOG_SQL = """
WITH bounded_memes AS (
    SELECT
        meme.id AS meme_id,
        meme.created_at AS available_at,
        meme.template_id,
        meme.primary_file_id
    FROM memes meme
    JOIN meme_files primary_file ON primary_file.id = meme.primary_file_id
    WHERE meme.is_public IS TRUE
      AND primary_file.status::text = 'ready'
    ORDER BY meme.created_at DESC, meme.id
    LIMIT :scan_limit
)
SELECT
    item.meme_id,
    item.available_at,
    item.template_id,
    embedding.embedding,
    feature.representative_source_channel_id AS source_id
FROM bounded_memes item
JOIN LATERAL (
    SELECT cache.embedding
    FROM embedding_cache cache
    WHERE cache.source_file_id = item.primary_file_id
      AND cache.input_type::text = 'image'
      AND cache.model_version = :model_version
      AND octet_length(cache.embedding) = :embedding_bytes
    ORDER BY cache.created_at DESC, cache.id DESC
    LIMIT 1
) embedding ON TRUE
LEFT JOIN public_meme_recommendation_features_mv feature ON feature.meme_id = item.meme_id
ORDER BY item.available_at DESC, item.meme_id
LIMIT :limit
"""


_HISTORIES_SQL = """
WITH raw_events AS (
    SELECT
        event.user_id,
        event.id AS event_id,
        CASE
            WHEN jsonb_typeof(event.payload -> 'refs' -> 'meme_id') = 'string'
             AND event.payload -> 'refs' ->> 'meme_id'
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (event.payload -> 'refs' ->> 'meme_id')::uuid
            WHEN jsonb_typeof(event.payload -> 'meme_id') = 'string'
             AND event.payload ->> 'meme_id'
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (event.payload ->> 'meme_id')::uuid
            ELSE NULL
        END AS meme_id,
        event.event_type::text AS event_type,
        event.occurred_at,
        NULLIF(btrim(event.payload ->> 'impression_id'), '') AS impression_id,
        lower(COALESCE(event.payload -> 'properties' ->> 'action', '')) AS action,
        CASE
            WHEN event.event_type::text IN (
                'meme_download', 'meme_send', 'meme_share', 'share',
                'inline_chosen', 'inline_sent'
            ) THEN 4.0
            WHEN event.event_type::text = 'meme_engaged_view' THEN 2.0
            WHEN event.event_type::text IN ('meme_detail_click', 'meme_view', 'view') THEN 1.0
            ELSE 0.0
        END::double precision AS weight,
        event.event_type::text IN (
            'meme_download', 'meme_send', 'meme_share', 'share',
            'inline_chosen', 'inline_sent'
        ) AS is_strong_positive,
        CASE
            WHEN event.event_type::text IN (
                'meme_send', 'meme_share', 'share', 'inline_chosen', 'inline_sent'
            )
             AND NULLIF(btrim(event.payload ->> 'impression_id'), '') IS NOT NULL
            THEN 'send:' || (event.payload ->> 'impression_id')
            ELSE 'event:' || event.id::text
        END AS dedupe_key
    FROM analytics_events event
    WHERE event.user_id IS NOT NULL
      AND event.event_type::text IN (
          'meme_download', 'meme_send', 'meme_share', 'share',
          'inline_chosen', 'inline_sent', 'meme_engaged_view',
          'meme_detail_click', 'meme_view', 'view'
      )
),
deduplicated_events AS (
    SELECT DISTINCT ON (user_id, meme_id, dedupe_key)
        user_id,
        meme_id,
        occurred_at,
        weight,
        is_strong_positive,
        'event:' || event_id::text AS observation_key
    FROM raw_events
    WHERE meme_id = ANY(CAST(:catalog_meme_ids AS uuid[]))
      AND weight > 0.0
      AND action NOT IN (
          'delete', 'remove', 'remove_save', 'reorder', 'reorder_pin',
          'unfavorite', 'unlike', 'unpin', 'unsave'
      )
    ORDER BY
        user_id,
        meme_id,
        dedupe_key,
        weight DESC,
        occurred_at DESC,
        event_id DESC
),
durable_kinds AS (
    SELECT
        COALESCE(collection_meme.added_by_user_id, collection.owner_id) AS user_id,
        collection_meme.meme_id,
        CASE WHEN collection.kind::text = 'favorites' THEN 'favorite' ELSE 'save' END AS durable_kind,
        max(collection_meme.added_at) AS occurred_at
    FROM collection_memes collection_meme
    JOIN collections collection ON collection.id = collection_meme.collection_id
    WHERE collection_meme.meme_id = ANY(CAST(:catalog_meme_ids AS uuid[]))
    GROUP BY
        COALESCE(collection_meme.added_by_user_id, collection.owner_id),
        collection_meme.meme_id,
        CASE WHEN collection.kind::text = 'favorites' THEN 'favorite' ELSE 'save' END
    UNION ALL
    SELECT
        pinned.user_id,
        pinned.meme_id,
        'pin',
        max(pinned.pinned_at)
    FROM pinned_memes pinned
    WHERE pinned.meme_id = ANY(CAST(:catalog_meme_ids AS uuid[]))
    GROUP BY pinned.user_id, pinned.meme_id
),
observations AS (
    SELECT
        user_id,
        meme_id,
        occurred_at,
        weight,
        is_strong_positive,
        observation_key
    FROM deduplicated_events
    UNION ALL
    SELECT
        user_id,
        meme_id,
        occurred_at,
        5.0::double precision AS weight,
        TRUE AS is_strong_positive,
        'durable:' || durable_kind || ':' || meme_id::text AS observation_key
    FROM durable_kinds
    WHERE user_id IS NOT NULL
),
candidate_users AS (
    SELECT
        user_id,
        count(*) AS observation_count,
        count(DISTINCT meme_id) FILTER (WHERE is_strong_positive) AS strong_target_count
    FROM observations
    GROUP BY user_id
    HAVING count(DISTINCT meme_id) >= :minimum_history + 1
       AND count(DISTINCT meme_id) FILTER (WHERE is_strong_positive) >= 1
    ORDER BY observation_count DESC, strong_target_count DESC, md5(user_id::text)
    LIMIT :candidate_user_limit
),
ranked_observations AS (
    SELECT
        observation.*,
        row_number() OVER (
            PARTITION BY observation.user_id
            ORDER BY
                observation.occurred_at DESC,
                observation.meme_id,
                observation.observation_key
        ) AS recency_rank
    FROM observations observation
    JOIN candidate_users candidate USING (user_id)
),
bounded_observations AS (
    SELECT *
    FROM ranked_observations
    WHERE recency_rank <= :max_history_per_user
),
case_eligible_users AS (
    SELECT DISTINCT target.user_id
    FROM bounded_observations target
    WHERE target.is_strong_positive
      AND (
          SELECT count(DISTINCT prior.meme_id)
          FROM bounded_observations prior
          WHERE prior.user_id = target.user_id
            AND prior.meme_id != target.meme_id
            AND prior.occurred_at < target.occurred_at
      ) >= :minimum_history
),
eligible_users AS (
    SELECT
        observation.user_id,
        count(*) AS observation_count,
        count(DISTINCT observation.meme_id) FILTER (
            WHERE observation.is_strong_positive
        ) AS strong_target_count
    FROM bounded_observations observation
    JOIN case_eligible_users eligible USING (user_id)
    GROUP BY observation.user_id
    ORDER BY observation_count DESC, strong_target_count DESC, md5(observation.user_id::text)
    LIMIT :max_users
)
SELECT
    observation.user_id,
    observation.meme_id,
    observation.occurred_at,
    observation.weight,
    observation.is_strong_positive
FROM bounded_observations observation
JOIN eligible_users eligible USING (user_id)
ORDER BY
    md5(observation.user_id::text),
    observation.occurred_at,
    observation.meme_id,
    observation.observation_key
"""


__all__ = [
    "DEFAULT_K",
    "DEFAULT_MAX_CASES",
    "DEFAULT_MAX_CATALOG",
    "DEFAULT_MAX_USERS",
    "HARD_MAX_CASES",
    "HARD_MAX_CATALOG",
    "HARD_MAX_K",
    "HARD_MAX_USERS",
    "ChronologicalEvaluationCase",
    "OfflineCatalogItem",
    "OfflineEvaluationBounds",
    "OfflineEvaluationPolicy",
    "OfflineEvaluationReport",
    "OfflineRetrievalVariant",
    "PostgresOfflineEvaluationLoader",
    "RecommendationObservation",
    "UserPositiveHistory",
    "VariantEvaluationSummary",
    "build_chronological_cases",
    "evaluate_chronological_retrieval",
    "evaluate_postgres_recommendations",
    "rank_chronological_case",
]
