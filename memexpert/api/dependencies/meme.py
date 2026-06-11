# ruff: noqa: TC002
"""FastAPI dependencies for shared meme search/read services."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.core.database import get_async_session_factory, get_db_session
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantUserSearchClient
from memexpert.core.voyage import PipelineVoyageClient
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService


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
        query_embedding_client=CachedTextQueryEmbeddingService(
            session,
            provider=PipelineVoyageClient(),
            cache_session_factory=get_async_session_factory(),
        ),
    )


MemeSearchServiceDep = Annotated[MemeSearchService, Depends(get_meme_search_service)]


__all__ = ["MemeSearchServiceDep", "get_meme_search_service"]
