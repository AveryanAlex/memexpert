# ruff: noqa: TC003
"""Bounded multi-source candidate fusion using weighted reciprocal rank."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class CandidateSource(StrEnum):
    SHORT_TERM = "short_term"
    CURRENT_INTENT = "current_intent"
    LONG_TERM_GLOBAL = "long_term_global"
    LONG_TERM_CLUSTER = "long_term_cluster"
    MULTI_POSITIVE = "multi_positive"
    TRENDING = "trending"
    EXPLORATION = "exploration"


@dataclass(frozen=True, slots=True)
class CandidateHit:
    meme_id: uuid.UUID
    score: float


@dataclass(frozen=True, slots=True)
class CandidateRanking:
    source: CandidateSource
    hits: tuple[CandidateHit, ...]
    weight: float = 1.0
    source_key: str | None = None
    normalization_group: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateContribution:
    source: CandidateSource
    source_key: str
    rank: int
    source_score: float
    rrf_contribution: float


@dataclass(slots=True)
class FusedCandidate:
    meme_id: uuid.UUID
    fused_score: float = 0.0
    contributions: list[CandidateContribution] = field(default_factory=list)


def fuse_candidate_rankings(
    rankings: list[CandidateRanking],
    *,
    constant: float = 60.0,
    limit: int = 600,
) -> list[FusedCandidate]:
    """Fuse ranked sources while normalizing multi-cluster total influence."""

    if constant <= 0.0:
        raise ValueError("constant must be positive")
    group_counts: dict[str, int] = {}
    for ranking in rankings:
        if ranking.normalization_group:
            group_counts[ranking.normalization_group] = group_counts.get(ranking.normalization_group, 0) + 1

    fused: dict[uuid.UUID, FusedCandidate] = {}
    for ranking in rankings:
        effective_weight = max(0.0, ranking.weight)
        if ranking.normalization_group:
            effective_weight /= group_counts[ranking.normalization_group]
        seen: set[uuid.UUID] = set()
        for rank, hit in enumerate(ranking.hits, start=1):
            if hit.meme_id in seen:
                continue
            seen.add(hit.meme_id)
            contribution = effective_weight / (constant + rank)
            candidate = fused.setdefault(hit.meme_id, FusedCandidate(meme_id=hit.meme_id))
            candidate.fused_score += contribution
            candidate.contributions.append(
                CandidateContribution(
                    source=ranking.source,
                    source_key=ranking.source_key or ranking.source.value,
                    rank=rank,
                    source_score=hit.score,
                    rrf_contribution=contribution,
                )
            )
    ordered = sorted(
        fused.values(),
        key=lambda candidate: (candidate.fused_score, -candidate.meme_id.int),
        reverse=True,
    )
    return ordered[: max(0, limit)]


__all__ = [
    "CandidateContribution",
    "CandidateHit",
    "CandidateRanking",
    "CandidateSource",
    "FusedCandidate",
    "fuse_candidate_rankings",
]
