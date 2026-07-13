"""Qdrant similarity lookup and sync adapter boundary used by the heavy content pipeline."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from memexpert.core._network import is_timeout_exception as _is_timeout_exception
from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.enums import SyncTargetKind
from memexpert.schemas.content_pipeline import ContentPipelineSyncTargetPreview

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from memexpert.core.search_index_prefilter import SearchIndexPrefilter
    from memexpert.ingest.policy import ApproximateMergeScope


@dataclass(frozen=True, slots=True)
class QdrantSimilarityMatch:
    """One similarity candidate returned by Qdrant for a just-embedded meme file."""

    meme_file_id: uuid.UUID
    meme_id: uuid.UUID
    similarity_score: float


@dataclass(frozen=True, slots=True)
class QdrantUserSearchMatch:
    """One semantic meme-search candidate returned by a user-facing Qdrant adapter."""

    meme_file_id: uuid.UUID
    meme_id: uuid.UUID
    semantic_score: float


@dataclass(frozen=True, slots=True)
class QdrantSyncPayload:
    """Durable payload advertised to Qdrant as part of the per-file sync upsert.

    The runtime rebuilds this struct from canonical PostgreSQL state once per
    sync attempt. Keeping the shape frozen makes it obvious which safe access,
    collection, and ranking hints are advertised to Qdrant; PostgreSQL remains
    the final access authority for user-facing results.
    """

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    search_index_algorithm_version: str
    is_public: bool
    media_type: str
    language: str
    is_nsfw: bool
    seo_page_slug: str | None
    template_id: str | None
    template_slug: str | None
    popularity_score: float
    like_count: int
    created_at: datetime
    updated_at: datetime
    uploader_user_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    collection_ids: list[str] = field(default_factory=list)
    public_collection_ids: list[str] = field(default_factory=list)
    unlisted_collection_ids: list[str] = field(default_factory=list)
    private_collection_ids: list[str] = field(default_factory=list)
    shared_collection_ids: list[str] = field(default_factory=list)
    collection_owner_user_ids: list[str] = field(default_factory=list)
    collection_member_user_ids: list[str] = field(default_factory=list)
    ocr_snippet: str | None = None
    quality_score: float | None = None
    source_object_key: str | None = None


class QdrantSimilarityError(RuntimeError):
    """Base error raised when Qdrant similarity work cannot complete."""


class QdrantProviderUnavailableError(QdrantSimilarityError):
    """Raised when the Qdrant provider is unreachable or refuses the query."""


class QdrantTimeoutError(QdrantSimilarityError):
    """Raised when Qdrant query execution exceeds the configured timeout."""


class QdrantMalformedResponseError(QdrantSimilarityError):
    """Raised when Qdrant returns payloads the pipeline cannot trust."""


class QdrantSyncError(RuntimeError):
    """Base error raised when Qdrant sync work (upsert/fetch/delete) cannot complete.

    Kept structurally parallel to :class:`QdrantSimilarityError` so the runtime
    dispatcher can treat sync failures with the same normalize-and-classify
    logic, while keeping the two concerns at distinct class hierarchies so
    operators always see which side of the Qdrant boundary is failing.
    """


class QdrantSyncProviderUnavailableError(QdrantSyncError):
    """Raised when the Qdrant provider is unreachable or refuses the sync write."""


class QdrantSyncTimeoutError(QdrantSyncError):
    """Raised when Qdrant sync execution exceeds the configured timeout."""


class QdrantSyncMalformedResponseError(QdrantSyncError):
    """Raised when Qdrant returns a sync-side payload the pipeline cannot trust."""


class QdrantSyncConflictError(QdrantSyncError):
    """Raised when Qdrant refuses a sync write because of a conflict (HTTP 409).

    Conflicts stay replayable because they represent transient races between
    concurrent sync attempts or optimistic-locking rejections — the runtime
    can safely retry once the colliding operation completes.
    """


class QdrantSimilarityClientProtocol(Protocol):
    """Typed Qdrant adapter surface used by the worker runtime and tests."""

    async def find_similar_memes(
        self,
        *,
        vector: tuple[float, ...],
        current_meme_file_id: uuid.UUID,
        scope: ApproximateMergeScope | None = None,
        limit: int | None = None,
    ) -> tuple[QdrantSimilarityMatch, ...]: ...


class QdrantUserSearchClientProtocol(Protocol):
    """Narrow semantic search boundary for user-facing meme search.

    Qdrant does not embed free text itself. Callers either pass a precomputed
    query vector from an embedding provider or use a fake implementation in
    tests. This protocol deliberately performs no provider construction or
    network I/O at import time.
    """

    async def search_memes_by_vector(
        self,
        *,
        query_vector: tuple[float, ...],
        limit: int = 20,
        prefilter: SearchIndexPrefilter | None = None,
    ) -> tuple[QdrantUserSearchMatch, ...]: ...


class QdrantSyncClientProtocol(Protocol):
    """Typed Qdrant sync adapter surface used by the runtime and tests.

    The protocol is deliberately narrow: the runtime needs to upsert a point,
    fetch the current state for operator diagnostics, and (future-proofing)
    delete a point when a canonical meme is retired. Anything beyond that
    belongs on the similarity protocol or the raw SDK.
    """

    async def upsert_meme_point(
        self,
        payload: QdrantSyncPayload,
        vector: tuple[float, ...],
    ) -> None: ...

    async def fetch_meme_point(
        self,
        meme_file_id: uuid.UUID,
    ) -> ContentPipelineSyncTargetPreview | None: ...

    async def delete_meme_point(self, meme_file_id: uuid.UUID) -> None: ...


class _LazyQdrantClient:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                url=self._settings.qdrant_url,
                timeout=max(1, int(self._settings.pipeline_qdrant_timeout_seconds)),
            )
        return self._client


class PipelineQdrantClient(_LazyQdrantClient):
    """Real Qdrant adapter that wraps ``AsyncQdrantClient`` with lazy construction.

    S02 uses Qdrant only as an index of already-persisted embeddings — the real source of
    truth for vectors is ``EmbeddingCache``. The adapter is lazy so that importing the
    module during app startup does not open any network connections.
    """

    async def find_similar_memes(
        self,
        *,
        vector: tuple[float, ...],
        current_meme_file_id: uuid.UUID,
        scope: ApproximateMergeScope | None = None,
        limit: int | None = None,
    ) -> tuple[QdrantSimilarityMatch, ...]:
        client = await self._ensure_client()
        resolved_limit = limit or self._settings.pipeline_qdrant_search_top_k

        try:
            raw_matches = await client.query_points(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                query=list(vector),
                query_filter=_build_approximate_dedupe_filter(scope),
                limit=resolved_limit,
                with_payload=True,
                with_vectors=False,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:  # pragma: no cover - exercised via monkeypatch
            raise QdrantTimeoutError(f"Qdrant similarity lookup timed out: {exc}") from exc
        except Exception as exc:  # pragma: no cover - exercised via monkeypatch in tests
            if _extract_sdk_status_code(exc) == 404:
                # A brand-new deployment has no collection yet. There cannot
                # be a duplicate match; the first sync upsert creates it.
                return ()
            if _is_timeout_exception(exc):
                raise QdrantTimeoutError(f"Qdrant similarity lookup timed out: {exc}") from exc
            raise QdrantProviderUnavailableError(f"Qdrant similarity lookup failed: {exc}") from exc

        return _parse_qdrant_matches(raw_matches, current_meme_file_id=current_meme_file_id)


def _build_approximate_dedupe_filter(scope: ApproximateMergeScope | None) -> object | None:
    """Build the strict public or same-single-uploader Qdrant candidate filter."""

    if scope is None:
        return None

    from qdrant_client.http.models import FieldCondition, Filter, MatchValue, ValuesCount

    if scope.is_public:
        return Filter(must=[FieldCondition(key="is_public", match=MatchValue(value=True))])
    if scope.uploader_user_id is None:
        return Filter(
            must=[
                FieldCondition(key="is_public", match=MatchValue(value=True)),
                FieldCondition(key="is_public", match=MatchValue(value=False)),
            ]
        )
    return Filter(
        must=[
            FieldCondition(key="is_public", match=MatchValue(value=False)),
            FieldCondition(
                key="uploader_user_ids",
                match=MatchValue(value=str(scope.uploader_user_id)),
            ),
            FieldCondition(
                key="uploader_user_ids",
                values_count=ValuesCount(gte=1, lte=1),
            ),
        ]
    )

class PipelineQdrantUserSearchClient(_LazyQdrantClient):
    """Lazy Qdrant adapter for user-facing semantic meme search.

    The adapter accepts an already-computed text query vector because Qdrant is
    not responsible for embedding user text. Importing or constructing this
    class does not contact Qdrant; the SDK client is created only when semantic
    search is actually requested.
    """

    async def search_memes_by_vector(
        self,
        *,
        query_vector: tuple[float, ...],
        limit: int = 20,
        prefilter: SearchIndexPrefilter | None = None,
    ) -> tuple[QdrantUserSearchMatch, ...]:
        client = await self._ensure_client()
        resolved_limit = max(1, limit)
        query_filter = prefilter.to_qdrant_filter() if prefilter is not None else None

        try:
            raw_matches = await client.query_points(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                query=list(query_vector),
                query_filter=query_filter,
                limit=resolved_limit,
                with_payload=True,
                with_vectors=False,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:  # pragma: no cover - exercised via monkeypatch
            raise QdrantTimeoutError(f"Qdrant user search timed out: {exc}") from exc
        except Exception as exc:  # pragma: no cover - exercised via monkeypatch in tests
            if _extract_sdk_status_code(exc) == 404:
                return ()
            if _is_timeout_exception(exc):
                raise QdrantTimeoutError(f"Qdrant user search timed out: {exc}") from exc
            raise QdrantProviderUnavailableError(f"Qdrant user search failed: {exc}") from exc

        return _parse_qdrant_user_search_matches(raw_matches)

class PipelineQdrantSyncClient(_LazyQdrantClient):
    """Lazy Qdrant sync adapter for per-file upsert/fetch/delete operations.

    Shares the same ``AsyncQdrantClient`` construction pattern as
    :class:`PipelineQdrantClient` but keeps its own instance so the similarity
    read path and the sync write path can be instrumented or mocked independently.
    Every SDK exception is mapped onto exactly one of the four typed
    ``QdrantSync*`` errors — callers never see raw SDK exceptions or reach past
    the adapter to httpx/transport errors.
    """

    async def upsert_meme_point(
        self,
        payload: QdrantSyncPayload,
        vector: tuple[float, ...],
    ) -> None:
        """Advertise one canonical meme to Qdrant so it becomes searchable."""

        client = await self._ensure_client()
        point = _build_meme_point(payload=payload, vector=vector)
        try:
            _ = await client.upsert(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                points=[point],
                wait=True,
            )
        except Exception as exc:
            if _extract_sdk_status_code(exc) == 404:
                await self._create_collection(client)
                try:
                    _ = await client.upsert(
                        collection_name=self._settings.pipeline_qdrant_collection_name,
                        points=[point],
                        wait=True,
                    )
                except Exception as retry_exc:
                    _raise_sync_error_from(retry_exc, operation="upsert_meme_point")
                return
            _raise_sync_error_from(exc, operation="upsert_meme_point")

    async def _create_collection(self, client: Any) -> None:
        """Create the vector collection lazily for the first indexed meme."""

        from qdrant_client.http.models import Distance, VectorParams

        try:
            _ = await client.create_collection(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                vectors_config=VectorParams(
                    size=self._settings.pipeline_voyage_output_dimensions,
                    distance=Distance.COSINE,
                ),
            )
        except Exception as exc:
            # Another worker may have won the first-upsert race.
            if _extract_sdk_status_code(exc) == 409:
                return
            _raise_sync_error_from(exc, operation="create_collection")

    async def fetch_meme_point(
        self,
        meme_file_id: uuid.UUID,
    ) -> ContentPipelineSyncTargetPreview | None:
        """Return the Qdrant-side payload preview used by the operator inspect surface.

        Returns ``None`` when Qdrant has no record of the point — the runtime
        treats this case as a best-effort preview miss, not a sync failure.
        """

        client = await self._ensure_client()
        try:
            raw_points = await client.retrieve(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                ids=[str(meme_file_id)],
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            _raise_sync_error_from(exc, operation="fetch_meme_point")
            return None  # pragma: no cover - _raise_sync_error_from never returns

        return _build_sync_preview(raw_points, fetched_at=utcnow())

    async def delete_meme_point(self, meme_file_id: uuid.UUID) -> None:
        """Remove the canonical meme point from Qdrant (e.g. after cascade delete)."""

        client = await self._ensure_client()
        try:
            _ = await client.delete(
                collection_name=self._settings.pipeline_qdrant_collection_name,
                points_selector=[str(meme_file_id)],
                wait=True,
            )
        except Exception as exc:
            _raise_sync_error_from(exc, operation="delete_meme_point")

def _raise_sync_error_from(exc: BaseException, *, operation: str) -> None:
    """Map an arbitrary SDK/transport exception onto the typed sync-error taxonomy.

    Ordering matters: timeouts are detected first (so transport timeouts wrapped
    inside ``ResponseHandlingException`` surface as timeouts, not conflicts),
    then HTTP-status-specific conflict/malformed classification, and finally
    everything else falls through as ``QdrantSyncProviderUnavailableError``.
    """

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        raise QdrantSyncTimeoutError(f"Qdrant {operation} timed out: {exc}") from exc
    if _is_timeout_exception(exc):
        raise QdrantSyncTimeoutError(f"Qdrant {operation} timed out: {exc}") from exc

    status_code = _extract_sdk_status_code(exc)
    if status_code == 409:
        raise QdrantSyncConflictError(
            f"Qdrant {operation} rejected with a 409 conflict: {exc}",
        ) from exc
    if status_code is not None and 500 <= status_code < 600:
        raise QdrantSyncProviderUnavailableError(
            f"Qdrant {operation} failed with status {status_code}: {exc}",
        ) from exc

    if _is_structural_parse_error(exc):
        raise QdrantSyncMalformedResponseError(
            f"Qdrant {operation} returned a payload the pipeline cannot trust: {exc}",
        ) from exc

    raise QdrantSyncProviderUnavailableError(
        f"Qdrant {operation} failed: {exc}",
    ) from exc


def _extract_sdk_status_code(exc: BaseException) -> int | None:
    """Pull an HTTP status code off an ``UnexpectedResponse`` without importing the SDK."""

    raw_status = getattr(exc, "status_code", None)
    if isinstance(raw_status, int):
        return raw_status
    return None


def _is_structural_parse_error(exc: BaseException) -> bool:
    """Return ``True`` when the SDK raised a validation/parse failure against its schemas.

    Pydantic ``ValidationError`` has a module path of ``pydantic``; any
    ``ValueError`` raised while decoding a Qdrant payload ends up here because
    the SDK wraps them in ``ResponseHandlingException``. Treating these as
    malformed responses mirrors the similarity-side behavior in
    :func:`_parse_qdrant_matches`.
    """

    if exc.__class__.__module__.startswith("pydantic"):
        return True
    cause = exc.__cause__
    while cause is not None:
        if cause.__class__.__module__.startswith("pydantic"):
            return True
        if isinstance(cause, ValueError) and not isinstance(cause, TypeError):
            return True
        cause = cause.__cause__
    return False


def _build_meme_point(
    *,
    payload: QdrantSyncPayload,
    vector: tuple[float, ...],
) -> Any:
    """Construct a Qdrant ``PointStruct`` from the sync payload and embedding vector.

    The import is local so importing ``memexpert.core.qdrant`` during app
    startup never forces the qdrant_client SDK to load — the sync client is
    lazy end-to-end, exactly like the similarity client.
    """

    from qdrant_client.http.models import PointStruct

    qdrant_payload: dict[str, Any] = {
        "meme_id": str(payload.meme_id),
        "meme_file_id": str(payload.meme_file_id),
        "search_index_algorithm_version": payload.search_index_algorithm_version,
        "is_public": payload.is_public,
        "uploader_user_ids": list(payload.uploader_user_ids),
        "media_type": payload.media_type,
        "language": payload.language,
        "is_nsfw": payload.is_nsfw,
        "seo_page_slug": payload.seo_page_slug,
        "template_id": payload.template_id,
        "template_slug": payload.template_slug,
        "popularity_score": float(payload.popularity_score),
        "like_count": int(payload.like_count),
        "created_at": payload.created_at.isoformat(),
        "updated_at": payload.updated_at.isoformat(),
        "tags": list(payload.tags),
        "collection_ids": list(payload.collection_ids),
        "public_collection_ids": list(payload.public_collection_ids),
        "unlisted_collection_ids": list(payload.unlisted_collection_ids),
        "private_collection_ids": list(payload.private_collection_ids),
        "shared_collection_ids": list(payload.shared_collection_ids),
        "collection_owner_user_ids": list(payload.collection_owner_user_ids),
        "collection_member_user_ids": list(payload.collection_member_user_ids),
    }
    if payload.ocr_snippet is not None:
        qdrant_payload["ocr_snippet"] = payload.ocr_snippet
    if payload.quality_score is not None:
        qdrant_payload["quality_score"] = float(payload.quality_score)
    if payload.source_object_key is not None:
        qdrant_payload["source_object_key"] = payload.source_object_key

    return PointStruct(
        id=str(payload.meme_file_id),
        vector=list(vector),
        payload=qdrant_payload,
    )


def _build_sync_preview(
    raw_points: object,
    *,
    fetched_at: datetime,
) -> ContentPipelineSyncTargetPreview | None:
    """Decode the ``retrieve`` response into a bounded preview the inspect surface uses."""

    if raw_points is None:
        return None
    if not isinstance(raw_points, (list, tuple)) or not raw_points:
        return None
    first_point = raw_points[0]
    raw_payload = getattr(first_point, "payload", None)
    if not isinstance(raw_payload, dict):
        return None

    # Only advertise the keys the sync client knows how to shape. Anything else
    # that Qdrant returns (scoring, unrelated metadata) is deliberately dropped
    # so the preview row does not grow unbounded.
    allowed_keys = {
        "meme_id",
        "meme_file_id",
        "search_index_algorithm_version",
        "is_public",
        "uploader_user_ids",
        "media_type",
        "language",
        "is_nsfw",
        "seo_page_slug",
        "template_id",
        "template_slug",
        "popularity_score",
        "like_count",
        "created_at",
        "updated_at",
        "tags",
        "collection_ids",
        "public_collection_ids",
        "unlisted_collection_ids",
        "private_collection_ids",
        "shared_collection_ids",
        "collection_owner_user_ids",
        "collection_member_user_ids",
        "ocr_snippet",
        "quality_score",
        "source_object_key",
    }
    preview_fields: dict[str, object] = {
        key: value for key, value in raw_payload.items() if key in allowed_keys
    }
    return ContentPipelineSyncTargetPreview(
        target=SyncTargetKind.QDRANT,
        preview_fields=preview_fields,
        preview_fetched_at=fetched_at,
    )


def _parse_qdrant_matches(
    raw_matches: object,
    *,
    current_meme_file_id: uuid.UUID,
) -> tuple[QdrantSimilarityMatch, ...]:
    # The adapter is defensive: if the SDK returns a payload we cannot iterate at
    # all, the entire response is structurally untrustworthy and must surface as a
    # malformed-response failure. Per-row noise (missing ids, wrong score types)
    # keeps the existing silent-drop behavior so the pipeline can still ingest
    # partially-valid similarity payloads.
    points = _extract_qdrant_points(raw_matches)
    if points is None:
        raise QdrantMalformedResponseError(
            "Qdrant similarity response is not an iterable sequence of matches.",
        )

    resolved_matches: list[QdrantSimilarityMatch] = []
    typed_matches: Iterable[object] = points
    for raw_entry in typed_matches:
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


def _parse_qdrant_user_search_matches(raw_matches: object) -> tuple[QdrantUserSearchMatch, ...]:
    """Decode Qdrant search results for user-facing semantic search."""

    points = _extract_qdrant_points(raw_matches)
    if points is None:
        raise QdrantMalformedResponseError(
            "Qdrant user search response is not an iterable sequence of matches.",
        )

    resolved_matches: list[QdrantUserSearchMatch] = []
    typed_matches: Iterable[object] = points
    for raw_entry in typed_matches:
        payload = _extract_payload(raw_entry)
        if payload is None:
            continue

        raw_meme_file_id = payload.get("meme_file_id")
        raw_meme_id = payload.get("meme_id")
        if not isinstance(raw_meme_file_id, str) or not isinstance(raw_meme_id, str):
            continue

        try:
            meme_file_id = uuid.UUID(raw_meme_file_id)
            meme_id = uuid.UUID(raw_meme_id)
        except ValueError:
            continue

        raw_score = getattr(raw_entry, "score", None)
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            continue
        semantic_score = float(raw_score)
        if semantic_score < 0.0:
            continue

        resolved_matches.append(
            QdrantUserSearchMatch(
                meme_file_id=meme_file_id,
                meme_id=meme_id,
                semantic_score=semantic_score,
            ),
        )
    return tuple(resolved_matches)


def _extract_payload(raw_entry: object) -> dict[str, object] | None:
    payload = getattr(raw_entry, "payload", None)
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, object]", payload)


def _extract_qdrant_points(raw_matches: object) -> Sequence[object] | None:
    if isinstance(raw_matches, (list, tuple)):
        return cast("Sequence[object]", raw_matches)
    raw_points = getattr(raw_matches, "points", None)
    if isinstance(raw_points, (list, tuple)):
        return cast("Sequence[object]", raw_points)
    return None


__all__ = [
    "PipelineQdrantClient",
    "PipelineQdrantSyncClient",
    "PipelineQdrantUserSearchClient",
    "QdrantMalformedResponseError",
    "QdrantProviderUnavailableError",
    "QdrantSimilarityClientProtocol",
    "QdrantSimilarityError",
    "QdrantSimilarityMatch",
    "QdrantSyncClientProtocol",
    "QdrantSyncConflictError",
    "QdrantSyncError",
    "QdrantSyncMalformedResponseError",
    "QdrantSyncPayload",
    "QdrantSyncProviderUnavailableError",
    "QdrantSyncTimeoutError",
    "QdrantTimeoutError",
    "QdrantUserSearchClientProtocol",
    "QdrantUserSearchMatch",
]
