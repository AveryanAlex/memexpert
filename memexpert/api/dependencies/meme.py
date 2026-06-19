# ruff: noqa: TC002
"""FastAPI dependencies for shared meme search/read services."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.core.database import get_async_session_factory, get_db_session
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantClient, PipelineQdrantUserSearchClient
from memexpert.core.voyage import build_pipeline_voyage_client
from memexpert.services.analytics import AnalyticsService
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.public_trends import PublicTrendsService
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService
from memexpert.services.report import MemeReportService
from memexpert.services.seo_catalog import SeoCatalogService


def get_meme_search_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> MemeSearchService:
    """Build the shared meme search service for request handlers.

    Index and embedding adapters are lazy: constructing them does not open
    network connections. Normal semantic search embeds the plain-text query via
    the cached embedding boundary, then hands the resulting vector to Qdrant.
    """

    return MemeSearchService(
        session,
        text_client=PipelineMeilisearchSyncClient(),
        semantic_client=PipelineQdrantUserSearchClient(),
        similarity_client=PipelineQdrantClient(),
        query_embedding_client=CachedTextQueryEmbeddingService(
            session,
            provider=build_pipeline_voyage_client(),
            cache_session_factory=get_async_session_factory(),
        ),
    )


def get_analytics_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> AnalyticsService:
    """Build the best-effort analytics service for request handlers."""

    return AnalyticsService(session)


def get_public_trends_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> PublicTrendsService:
    """Build the MV-backed public trends service for request handlers."""

    return PublicTrendsService(session)


def get_meme_report_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> MemeReportService:
    """Build the user-facing meme report service for request handlers."""

    return MemeReportService(session=session)


def get_seo_catalog_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> SeoCatalogService:
    """Build the DB-only public SEO catalog service for request handlers."""

    return SeoCatalogService(session)


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
MemeReportServiceDep = Annotated[MemeReportService, Depends(get_meme_report_service)]
MemeSearchServiceDep = Annotated[MemeSearchService, Depends(get_meme_search_service)]
PublicTrendsServiceDep = Annotated[PublicTrendsService, Depends(get_public_trends_service)]
SeoCatalogServiceDep = Annotated[SeoCatalogService, Depends(get_seo_catalog_service)]


__all__ = [
    "AnalyticsServiceDep",
    "MemeReportServiceDep",
    "MemeSearchServiceDep",
    "PublicTrendsServiceDep",
    "SeoCatalogServiceDep",
    "get_analytics_service",
    "get_meme_search_service",
    "get_meme_report_service",
    "get_public_trends_service",
    "get_seo_catalog_service",
]
