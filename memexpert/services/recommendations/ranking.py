# ruff: noqa: TC001,TC003
"""Configurable home scoring, diversity, caps, and exploration placement."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, replace

from memexpert.services.recommendations.candidates import CandidateContribution
from memexpert.services.recommendations.features import (
    RecommendationItemFeatures,
    freshness_score,
    popularity_alignment,
)
from memexpert.services.recommendations.math import clamp01, cosine_similarity


@dataclass(frozen=True, slots=True)
class HomeRankingWeights:
    personal_fit: float = 0.40
    current_intent: float = 0.15
    fused_candidate: float = 0.10
    quality: float = 0.15
    freshness: float = 0.10
    popularity_alignment: float = 0.05
    exploration: float = 0.05

    def __post_init__(self) -> None:
        values = (
            self.personal_fit,
            self.current_intent,
            self.fused_candidate,
            self.quality,
            self.freshness,
            self.popularity_alignment,
            self.exploration,
        )
        if any(value < 0.0 for value in values):
            raise ValueError("ranking weights must be non-negative")
        if abs(sum(values) - 1.0) > 1e-6:
            raise ValueError("ranking weights must sum to 1")


@dataclass(frozen=True, slots=True)
class RankableCandidate:
    meme_id: uuid.UUID
    fused_score: float
    personal_fit: float
    current_intent: float
    features: RecommendationItemFeatures
    embedding: tuple[float, ...] | None = None
    contributions: tuple[CandidateContribution, ...] = ()
    is_exploration: bool = False
    total_score: float = 0.0
    score_components: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class DiversityPolicy:
    semantic_penalty: float = 0.15
    source_penalty: float = 0.08
    template_penalty: float = 0.08
    source_cap_per_20: int = 2
    template_cap_per_20: int = 2
    exploration_slot_interval: int = 20
    greedy_window: int = 100


def score_home_candidates(
    candidates: list[RankableCandidate],
    *,
    weights: HomeRankingWeights | None = None,
    freshness_half_life_days: float = 45.0,
    user_median_popularity_quantile: float | None = None,
    qualifying_positive_count: int = 0,
) -> list[RankableCandidate]:
    """Apply the algorithm-versioned v2 home formula."""

    resolved_weights = weights or HomeRankingWeights()
    max_fused = max((candidate.fused_score for candidate in candidates), default=0.0)
    scored: list[RankableCandidate] = []
    for candidate in candidates:
        fused = clamp01(candidate.fused_score / max_fused) if max_fused > 0.0 else 0.0
        quality = candidate.features.quality_prior
        freshness = freshness_score(
            candidate.features.latest_published_at,
            half_life_days=freshness_half_life_days,
        )
        alignment = popularity_alignment(
            candidate.features.popularity_quantile,
            user_median_quantile=user_median_popularity_quantile,
            qualifying_positive_count=qualifying_positive_count,
        )
        exploration = 1.0 if candidate.is_exploration else 0.0
        components = {
            "personal_fit": clamp01(candidate.personal_fit),
            "current_intent": clamp01(candidate.current_intent),
            "fused_candidate_score": fused,
            "quality_prior": quality,
            "freshness": freshness,
            "popularity_alignment": alignment,
            "exploration_bonus": exploration,
        }
        total = (
            resolved_weights.personal_fit * components["personal_fit"]
            + resolved_weights.current_intent * components["current_intent"]
            + resolved_weights.fused_candidate * components["fused_candidate_score"]
            + resolved_weights.quality * components["quality_prior"]
            + resolved_weights.freshness * components["freshness"]
            + resolved_weights.popularity_alignment * components["popularity_alignment"]
            + resolved_weights.exploration * components["exploration_bonus"]
        )
        components["total"] = total
        scored.append(replace(candidate, total_score=total, score_components=components))
    return sorted(
        scored,
        key=lambda candidate: (candidate.total_score, candidate.fused_score, -candidate.meme_id.int),
        reverse=True,
    )


def diversity_rerank(
    candidates: list[RankableCandidate],
    *,
    limit: int,
    policy: DiversityPolicy | None = None,
) -> list[RankableCandidate]:
    """Greedily rerank the top window, relaxing caps only to fill output."""

    if limit <= 0:
        return []
    resolved = policy or DiversityPolicy()
    remaining: list[RankableCandidate] = []
    seen_meme_ids: set[uuid.UUID] = set()
    for candidate in candidates:
        if candidate.meme_id in seen_meme_ids:
            continue
        seen_meme_ids.add(candidate.meme_id)
        remaining.append(candidate)
    selected: list[RankableCandidate] = []
    greedy_target = min(limit, resolved.greedy_window, len(remaining))

    while len(selected) < greedy_target and remaining:
        block_start = (len(selected) // 20) * 20
        block = selected[block_start:]
        require_exploration = (
            resolved.exploration_slot_interval > 0
            and (len(selected) + 1) % resolved.exploration_slot_interval == 0
            and not any(candidate.is_exploration for candidate in block)
            and any(candidate.is_exploration for candidate in remaining)
        )
        eligible = [candidate for candidate in remaining if not require_exploration or candidate.is_exploration]
        capped = [candidate for candidate in eligible if _within_caps(candidate, block, resolved)]
        pool = capped or eligible or remaining
        chosen = max(
            pool,
            key=lambda candidate: (
                _diversity_adjusted_score(candidate, selected, resolved),
                candidate.total_score,
                -candidate.meme_id.int,
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)

    # The contract applies greedy MMR to the top 100. The remainder of a
    # cached 200-item pool keeps the stable base score order.
    selected.extend(remaining[: max(0, limit - len(selected))])
    return selected[:limit]


def _within_caps(
    candidate: RankableCandidate,
    block: list[RankableCandidate],
    policy: DiversityPolicy,
) -> bool:
    source_counts = Counter(
        selected.features.representative_source_channel_id
        for selected in block
        if selected.features.representative_source_channel_id is not None
    )
    template_counts = Counter(
        selected.features.template_id for selected in block if selected.features.template_id is not None
    )
    source = candidate.features.representative_source_channel_id
    template = candidate.features.template_id
    return not (
        source is not None and source_counts[source] >= policy.source_cap_per_20
        or template is not None and template_counts[template] >= policy.template_cap_per_20
    )


def _diversity_adjusted_score(
    candidate: RankableCandidate,
    selected: list[RankableCandidate],
    policy: DiversityPolicy,
) -> float:
    score = candidate.total_score
    if candidate.embedding is not None:
        similarities = [
            cosine_similarity(candidate.embedding, previous.embedding)
            for previous in selected[-10:]
            if previous.embedding is not None
        ]
        score -= policy.semantic_penalty * max([0.0, *similarities])

    candidate_sources = set(candidate.features.source_channel_ids)
    if candidate.features.representative_source_channel_id is not None:
        candidate_sources.add(candidate.features.representative_source_channel_id)
    if candidate_sources and any(
        candidate_sources.intersection(previous.features.source_channel_ids)
        or previous.features.representative_source_channel_id in candidate_sources
        for previous in selected[-5:]
    ):
        score -= policy.source_penalty

    template = candidate.features.template_id
    if template is not None and any(previous.features.template_id == template for previous in selected[-3:]):
        score -= policy.template_penalty
    return score


__all__ = [
    "DiversityPolicy",
    "HomeRankingWeights",
    "RankableCandidate",
    "diversity_rerank",
    "score_home_candidates",
]
