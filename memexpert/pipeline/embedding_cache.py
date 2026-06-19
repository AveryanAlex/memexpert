# ruff: noqa: TC001,TC003
"""Embedding-cache persistence helpers for the embed stage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.core.config import Settings
from memexpert.core.voyage import VoyageMalformedResponseError
from memexpert.models.content import EmbeddingCache
from memexpert.models.enums import EmbeddingInputType
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.voyage import VoyageEmbeddingResult
    from memexpert.models.content import MemeFile


def validate_embedding_contract(settings: Settings, embedding_result: VoyageEmbeddingResult) -> None:
    expected_dimensions = settings.pipeline_voyage_output_dimensions
    if embedding_result.dimensions != expected_dimensions:
        raise PipelineIngestError(
            "Rejected embedding vector with unexpected dimensionality "
            f"(got {embedding_result.dimensions}, expected {expected_dimensions}).",
        )
    if len(embedding_result.vector) != expected_dimensions:
        raise PipelineIngestError(
            "Rejected embedding vector whose length does not match the declared dimensions.",
        )
    try:
        _ = embedding_result.embedding_bytes
    except VoyageMalformedResponseError as exc:
        raise PipelineIngestError(str(exc)) from exc


async def persist_embedding_cache_row(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    embedding_result: VoyageEmbeddingResult,
) -> None:
    existing_result = await session.execute(
        select(EmbeddingCache).where(
            EmbeddingCache.input_hash == embedding_result.input_hash,
            EmbeddingCache.model_version == embedding_result.model,
            EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
        )
    )
    existing_cache_row = existing_result.scalar_one_or_none()
    if existing_cache_row is not None:
        existing_cache_row.embedding = embedding_result.embedding_bytes
        existing_cache_row.source_file_id = meme_file.id
        await session.flush()
        return

    session.add(
        EmbeddingCache(
            input_hash=embedding_result.input_hash,
            input_type=EmbeddingInputType.IMAGE,
            embedding=embedding_result.embedding_bytes,
            model_version=embedding_result.model,
            source_file_id=meme_file.id,
        )
    )
    await session.flush()


__all__ = ["persist_embedding_cache_row", "validate_embedding_contract"]
