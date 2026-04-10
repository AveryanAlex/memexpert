"""Qdrant similarity lookup adapter boundary used by the heavy content pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from memexpert.core.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class QdrantSimilarityMatch:
    """One similarity candidate returned by Qdrant for a just-embedded meme file."""

    meme_file_id: uuid.UUID
    meme_id: uuid.UUID
    similarity_score: float


class QdrantSimilarityError(RuntimeError):
    """Base error raised when Qdrant similarity work cannot complete."""


class QdrantProviderUnavailableError(QdrantSimilarityError):
    """Raised when the Qdrant provider is unreachable or refuses the query."""


class QdrantTimeoutError(QdrantSimilarityError):
    """Raised when Qdrant query execution exceeds the configured timeout."""


class QdrantMalformedResponseError(QdrantSimilarityError):
    """Raised when Qdrant returns payloads the pipeline cannot trust."""


class QdrantSimilarityClientProtocol(Protocol):
    """Typed Qdrant adapter surface used by the worker runtime and tests."""

    async def find_similar_memes(
        self,
        *,
        vector: tuple[float, ...],
        current_meme_file_id: uuid.UUID,
        limit: int | None = None,
    ) -> tuple[QdrantSimilarityMatch, ...]: ...


class PipelineQdrantClient:
    """Real Qdrant adapter that wraps ``AsyncQdrantClient`` with lazy construction.

    S02 uses Qdrant only as an index of already-persisted embeddings — the real source of
    truth for vectors is ``EmbeddingCache``. The adapter is lazy so that importing the
    module during app startup does not open any network connections.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None

    async def find_similar_memes(
        self,
        *,
        vector: tuple[float, ...],
        current_meme_file_id: uuid.UUID,
        limit: int | None = None,
    ) -> tuple[QdrantSimilarityMatch, ...]:
        client = await self._ensure_client()
        resolved_limit = limit or self._settings.pipeline_qdrant_search_top_k

        try:
            raw_matches = await client.search(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                query_vector=list(vector),
                limit=resolved_limit,
                with_payload=True,
            )
        except Exception as exc:  # pragma: no cover - exercised via monkeypatch in tests
            raise QdrantProviderUnavailableError(f"Qdrant similarity lookup failed: {exc}") from exc

        return _parse_qdrant_matches(raw_matches, current_meme_file_id=current_meme_file_id)

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                url=self._settings.qdrant_url,
                timeout=max(1, int(self._settings.pipeline_qdrant_timeout_seconds)),
            )
        return self._client


def _parse_qdrant_matches(
    raw_matches: Sequence[object],
    *,
    current_meme_file_id: uuid.UUID,
) -> tuple[QdrantSimilarityMatch, ...]:
    resolved_matches: list[QdrantSimilarityMatch] = []
    for raw_entry in raw_matches:
        payload = _extract_payload(raw_entry)
        if payload is None:
            continue

        raw_meme_file_id = payload.get("meme_file_id")
        raw_meme_id = payload.get("meme_id")
        if not isinstance(raw_meme_file_id, str) or not isinstance(raw_meme_id, str):
            continue

        try:
            matched_meme_file_id = uuid.UUID(raw_meme_file_id)
            matched_meme_id = uuid.UUID(raw_meme_id)
        except ValueError:
            continue

        if matched_meme_file_id == current_meme_file_id:
            # Ignore self-matches — Qdrant might return the freshly upserted point
            # before the transaction publishes the new stage.
            continue

        raw_score = getattr(raw_entry, "score", None)
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            continue
        similarity_score = float(raw_score)
        if not 0.0 <= similarity_score <= 1.0:
            # Reject invalid similarity payloads rather than let them drive merges.
            continue

        resolved_matches.append(
            QdrantSimilarityMatch(
                meme_file_id=matched_meme_file_id,
                meme_id=matched_meme_id,
                similarity_score=similarity_score,
            )
        )
    return tuple(resolved_matches)


def _extract_payload(raw_entry: object) -> dict[str, object] | None:
    payload = getattr(raw_entry, "payload", None)
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, object]", payload)


__all__ = [
    "PipelineQdrantClient",
    "QdrantMalformedResponseError",
    "QdrantProviderUnavailableError",
    "QdrantSimilarityClientProtocol",
    "QdrantSimilarityError",
    "QdrantSimilarityMatch",
    "QdrantTimeoutError",
]
