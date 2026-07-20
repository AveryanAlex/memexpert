# ruff: noqa: TC003
"""Chronological offline metrics for evidence-gated recommendation changes."""

from __future__ import annotations

import math
import uuid
from collections import Counter
from dataclasses import dataclass

from memexpert.services.recommendations.math import cosine_similarity


@dataclass(frozen=True, slots=True)
class EvaluationItem:
    meme_id: uuid.UUID
    relevance: float = 0.0
    source_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class RecommendationEvaluation:
    recall_at_k: float
    ndcg_at_k: float
    catalog_coverage: float
    source_concentration: float
    template_concentration: float
    intra_list_diversity: float


def evaluate_ranking(
    ranked: list[EvaluationItem],
    *,
    relevant_meme_ids: set[uuid.UUID],
    catalog_size: int,
    k: int = 50,
) -> RecommendationEvaluation:
    """Compute the gates used to compare profile and vector variants."""

    top = ranked[: max(0, k)]
    matched = sum(item.meme_id in relevant_meme_ids for item in top)
    recall = matched / len(relevant_meme_ids) if relevant_meme_ids else 0.0
    ideal_relevances = sorted(
        [max(item.relevance, 1.0 if item.meme_id in relevant_meme_ids else 0.0) for item in ranked],
        reverse=True,
    )[: len(top)]
    actual_relevances = [max(item.relevance, 1.0 if item.meme_id in relevant_meme_ids else 0.0) for item in top]
    ideal_dcg = _dcg(ideal_relevances)
    ndcg = _dcg(actual_relevances) / ideal_dcg if ideal_dcg > 0.0 else 0.0
    unique_count = len({item.meme_id for item in top})
    coverage = unique_count / catalog_size if catalog_size > 0 else 0.0
    return RecommendationEvaluation(
        recall_at_k=recall,
        ndcg_at_k=ndcg,
        catalog_coverage=coverage,
        source_concentration=_concentration([item.source_id for item in top if item.source_id is not None]),
        template_concentration=_concentration(
            [item.template_id for item in top if item.template_id is not None]
        ),
        intra_list_diversity=_intra_list_diversity(top),
    )


def _dcg(relevances: list[float]) -> float:
    return sum((2.0**relevance - 1.0) / math.log2(rank + 1.0) for rank, relevance in enumerate(relevances, start=1))


def _concentration(values: list[uuid.UUID]) -> float:
    if not values:
        return 0.0
    counts = Counter(values)
    return sum((count / len(values)) ** 2 for count in counts.values())


def _intra_list_diversity(items: list[EvaluationItem]) -> float:
    vectors = [item.embedding for item in items if item.embedding is not None]
    if len(vectors) < 2:
        return 0.0
    distances = [
        1.0 - cosine_similarity(vectors[left], vectors[right])
        for left in range(len(vectors))
        for right in range(left + 1, len(vectors))
    ]
    return sum(distances) / len(distances)


__all__ = ["EvaluationItem", "RecommendationEvaluation", "evaluate_ranking"]
