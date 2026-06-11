# ruff: noqa: TC002
"""FastAPI dependencies for shared meme search/read services."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.core.database import get_db_session
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantUserSearchClient
from memexpert.services.meme_search import MemeSearchService


def get_meme_search_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> MemeSearchService:
    """Build the shared meme search service for request handlers.

    Both index adapters are lazy: constructing them does not open network
    connections. Semantic search runs only when a request supplies a precomputed
    query vector.
    """

    return MemeSearchService(
        session,
        text_client=PipelineMeilisearchSyncClient(),
        semantic_client=PipelineQdrantUserSearchClient(),
    )


MemeSearchServiceDep = Annotated[MemeSearchService, Depends(get_meme_search_service)]


__all__ = ["MemeSearchServiceDep", "get_meme_search_service"]
