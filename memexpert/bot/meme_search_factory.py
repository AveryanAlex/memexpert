"""Shared meme search service factory for Telegram bot surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from memexpert.core.database import get_async_session_factory
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantRecommendationClient, PipelineQdrantUserSearchClient
from memexpert.core.voyage import PipelineVoyageClient
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService
from memexpert.services.recommendations.service import RecommendationService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.services.telegram_inline import MemeSearchServiceFactory, RecommendationServiceFactory


def build_default_meme_search_service_factory(settings: Settings) -> MemeSearchServiceFactory:
    """Build the production search service used by Telegram search surfaces."""

    def factory(session: AsyncSession) -> MemeSearchService:
        return MemeSearchService(
            session,
            text_client=PipelineMeilisearchSyncClient(),
            semantic_client=PipelineQdrantUserSearchClient(),
            query_embedding_client=CachedTextQueryEmbeddingService(
                session,
                provider=PipelineVoyageClient(),
                cache_session_factory=get_async_session_factory(),
                settings=settings,
            ),
        )

    return factory


def build_default_recommendation_service_factory(
    settings: Settings,
    *,
    meme_search_service_factory: MemeSearchServiceFactory,
) -> RecommendationServiceFactory:
    """Build the personalized home service reused by Telegram empty queries."""

    def factory(session: AsyncSession) -> RecommendationService:
        meme_search_service = meme_search_service_factory(session)
        return RecommendationService(
            session,
            meme_search_service=cast("MemeSearchService", meme_search_service),
            qdrant_client=PipelineQdrantRecommendationClient(settings=settings),
            settings=settings,
        )

    return factory


__all__ = [
    "build_default_meme_search_service_factory",
    "build_default_recommendation_service_factory",
]
