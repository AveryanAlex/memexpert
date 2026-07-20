# ruff: noqa: TC001
"""FastAPI dependency boundary for personalized recommendations."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from memexpert.api.dependencies.meme import MemeSearchServiceDep
from memexpert.core.qdrant import PipelineQdrantRecommendationClient
from memexpert.services.recommendations.service import RecommendationService

from .auth import DbSessionDep


def get_recommendation_service(
    session: DbSessionDep,
    meme_search_service: MemeSearchServiceDep,
) -> RecommendationService:
    """Build one request-scoped recommender with lazy provider adapters."""

    return RecommendationService(
        session,
        meme_search_service=meme_search_service,
        qdrant_client=PipelineQdrantRecommendationClient(),
    )


RecommendationServiceDep = Annotated[RecommendationService, Depends(get_recommendation_service)]


__all__ = ["RecommendationServiceDep", "get_recommendation_service"]
