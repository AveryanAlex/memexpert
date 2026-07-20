"""Personalized recommendation subsystem with PostgreSQL authority."""

from memexpert.services.recommendations.candidates import (
    CandidateContribution,
    CandidateHit,
    CandidateRanking,
    CandidateSource,
    FusedCandidate,
    fuse_candidate_rankings,
)
from memexpert.services.recommendations.features import RecommendationItemFeatures
from memexpert.services.recommendations.profiles import BuiltProfileVector, ProfileSignalVector, build_profile_vectors
from memexpert.services.recommendations.ranking import (
    DiversityPolicy,
    HomeRankingWeights,
    RankableCandidate,
    diversity_rerank,
    score_home_candidates,
)

__all__ = [
    "BuiltProfileVector",
    "CandidateContribution",
    "CandidateHit",
    "CandidateRanking",
    "CandidateSource",
    "DiversityPolicy",
    "FusedCandidate",
    "HomeRankingWeights",
    "ProfileSignalVector",
    "RankableCandidate",
    "RecommendationItemFeatures",
    "build_profile_vectors",
    "diversity_rerank",
    "fuse_candidate_rankings",
    "score_home_candidates",
]
