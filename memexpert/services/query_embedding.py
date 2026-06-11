"""Cached plain-text query embedding service for user-facing meme search."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.config import Settings, get_settings
from memexpert.core.voyage import (
    VoyageEmbeddingError,
    VoyageEmbeddingResult,
    build_voyage_text_input_hash,
    decode_embedding_bytes,
)
from memexpert.models.content import EmbeddingCache
from memexpert.models.enums import EmbeddingInputType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class TextEmbeddingProviderProtocol(Protocol):
    """Provider boundary for embedding already-normalized plain-text queries."""

    async def embed_text(self, *, text: str) -> VoyageEmbeddingResult: ...


class CachedTextQueryEmbeddingService:
    """Embed text queries through a lazy provider and persist reusable cache rows.

    Cache failures are intentionally non-fatal: the caller can either use the
    freshly returned provider vector or, when provider embedding itself fails,
    fall back to Meilisearch-only ranking. This keeps user search available when
    the optional semantic path is degraded.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: TextEmbeddingProviderProtocol,
        cache_session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._cache_session_factory = cache_session_factory
        self._settings = settings or get_settings()

    async def embed_query(self, query: str) -> tuple[float, ...]:
        normalized_query = query.strip()
        if not normalized_query:
            return ()

        cached_vector = await self._read_cached_vector(normalized_query)
        if cached_vector is not None:
            return cached_vector

        embedding_result = await self._provider.embed_text(text=normalized_query)
        await self._persist_cached_vector(embedding_result)
        return embedding_result.vector

    async def _read_cached_vector(self, query: str) -> tuple[float, ...] | None:
        async with self._open_cache_session() as session:
            try:
                cache_row = await session.scalar(
                    select(EmbeddingCache).where(
                        EmbeddingCache.input_hash == build_voyage_text_input_hash(query),
                        EmbeddingCache.model_version == self._settings.pipeline_voyage_model,
                        EmbeddingCache.input_type == EmbeddingInputType.TEXT,
                    ),
                )
            except SQLAlchemyError:
                logger.exception("Text query embedding cache read failed; continuing without cache.")
                await session.rollback()
                return None

        if cache_row is None:
            return None

        try:
            return decode_embedding_bytes(
                cache_row.embedding,
                dimensions=self._settings.pipeline_voyage_output_dimensions,
            )
        except VoyageEmbeddingError:
            logger.exception("Cached text query embedding was malformed; requesting a fresh embedding.")
            return None

    async def _persist_cached_vector(self, embedding_result: VoyageEmbeddingResult) -> None:
        async with self._open_cache_session() as session:
            try:
                session.add(
                    EmbeddingCache(
                        input_hash=embedding_result.input_hash,
                        input_type=EmbeddingInputType.TEXT,
                        embedding=embedding_result.embedding_bytes,
                        model_version=embedding_result.model,
                        source_file_id=None,
                    ),
                )
                if self._cache_session_factory is None:
                    await session.flush()
                else:
                    await session.commit()
            except SQLAlchemyError:
                logger.exception("Text query embedding cache write failed; continuing with uncached vector.")
                await session.rollback()

    @asynccontextmanager
    async def _open_cache_session(self) -> AsyncIterator[AsyncSession]:
        if self._cache_session_factory is None:
            yield self._session
            return

        async with self._cache_session_factory() as session:
            yield session


__all__ = ["CachedTextQueryEmbeddingService", "TextEmbeddingProviderProtocol"]
