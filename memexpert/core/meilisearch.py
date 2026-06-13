"""Meilisearch sync adapter boundary used by the heavy content pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from memexpert.core._network import is_timeout_exception
from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.enums import SyncTargetKind
from memexpert.schemas.content_pipeline import ContentPipelineSyncTargetPreview

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping
    from datetime import datetime

    from memexpert.core.search_index_prefilter import SearchIndexPrefilter


MEILISEARCH_FILTERABLE_ATTRIBUTES: tuple[str, ...] = (
    "search_index_algorithm_version",
    "is_public",
    "author_user_id",
    "collection_owner_user_ids",
    "collection_member_user_ids",
    "collection_ids",
    "public_collection_ids",
    "unlisted_collection_ids",
    "private_collection_ids",
    "shared_collection_ids",
    "media_type",
    "language",
    "is_nsfw",
    "tags",
)


@dataclass(frozen=True, slots=True)
class PipelineMeilisearchDocument:
    """Durable per-file document advertised to Meilisearch as part of the sync upsert.

    The runtime rebuilds this document from canonical PostgreSQL state once per
    sync attempt and hands it to :class:`PipelineMeilisearchSyncClient`. The
    document ``id`` is the ``meme_file_id`` hex string so route lookups and the
    snapshot-row surface share the same primary key shape as Qdrant. Access
    fields are index hints only; PostgreSQL remains the final authority.
    """

    id: str
    meme_id: str
    meme_file_id: str
    search_index_algorithm_version: str
    is_public: bool
    author_user_id: str | None
    media_type: str
    language: str
    is_nsfw: bool
    created_at: datetime
    updated_at: datetime
    seo_page_slug: str | None
    template_id: str | None
    template_slug: str | None
    popularity_score: float
    like_count: int
    tags: list[str] = field(default_factory=list)
    collection_ids: list[str] = field(default_factory=list)
    public_collection_ids: list[str] = field(default_factory=list)
    unlisted_collection_ids: list[str] = field(default_factory=list)
    private_collection_ids: list[str] = field(default_factory=list)
    shared_collection_ids: list[str] = field(default_factory=list)
    collection_owner_user_ids: list[str] = field(default_factory=list)
    collection_member_user_ids: list[str] = field(default_factory=list)
    ocr_text: str | None = None
    quality_score: float | None = None


class MeilisearchSyncError(RuntimeError):
    """Base error raised when Meilisearch sync work cannot complete.

    Kept structurally parallel to :class:`memexpert.core.qdrant.QdrantSyncError`
    so the runtime dispatcher can reuse its normalize-and-classify logic while
    still reporting which sync target is failing to operators.
    """


class MeilisearchSyncProviderUnavailableError(MeilisearchSyncError):
    """Raised when the Meilisearch provider is unreachable or refuses the sync write."""


class MeilisearchSyncTimeoutError(MeilisearchSyncError):
    """Raised when Meilisearch sync execution exceeds the configured timeout."""


class MeilisearchSyncMalformedResponseError(MeilisearchSyncError):
    """Raised when Meilisearch returns a sync-side payload the pipeline cannot trust."""


class MeilisearchSyncConflictError(MeilisearchSyncError):
    """Raised when Meilisearch refuses a sync write because of a conflict (HTTP 409).

    Conflicts stay replayable because they represent transient races between
    concurrent sync attempts — the runtime can safely retry once the colliding
    operation completes.
    """


class MeilisearchSyncClientProtocol(Protocol):
    """Typed Meilisearch sync adapter surface used by the runtime and tests.

    The protocol is intentionally narrow: the runtime needs to upsert a
    document, fetch the current state for operator diagnostics, delete a
    document when a canonical file is retired, ensure the index exists
    before the first write, and — as of T04 — run a bounded text search
    so the smoke-proof path can prove documents are actually retrievable.
    Anything beyond that belongs on the SDK.
    """

    async def upsert_document(
        self,
        document: PipelineMeilisearchDocument,
    ) -> None: ...

    async def fetch_document(
        self,
        meme_file_id: uuid.UUID,
    ) -> ContentPipelineSyncTargetPreview | None: ...

    async def delete_document(self, meme_file_id: uuid.UUID) -> None: ...

    async def ensure_index(self) -> None: ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        prefilter: SearchIndexPrefilter | None = None,
    ) -> list[dict[str, Any]]: ...


class PipelineMeilisearchSyncClient:
    """Lazy Meilisearch sync adapter for per-file upsert/fetch/delete operations.

    Mirrors :class:`memexpert.core.qdrant.PipelineQdrantSyncClient` so the
    worker runtime can use the same normalize/classify path for both targets.
    Every SDK exception is mapped onto exactly one of the four typed
    ``MeilisearchSync*`` errors — callers never see raw SDK exceptions or
    reach past the adapter to httpx/transport errors.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None
        self._index: Any | None = None

    async def upsert_document(
        self,
        document: PipelineMeilisearchDocument,
    ) -> None:
        """Advertise one canonical meme to Meilisearch so it becomes text-searchable."""

        index = await self._ensure_index_client()
        payload = _build_document_payload(document)
        try:
            _ = await index.update_documents([payload], primary_key="id")
        except Exception as exc:
            _raise_sync_error_from(exc, operation="upsert_document")

    async def fetch_document(
        self,
        meme_file_id: uuid.UUID,
    ) -> ContentPipelineSyncTargetPreview | None:
        """Return the Meilisearch-side document preview used by the operator inspect surface.

        Returns ``None`` when Meilisearch has no record of the document — the
        runtime treats this case as a best-effort preview miss, not a sync
        failure. The SDK raises ``MeilisearchApiError`` (HTTP 404) for missing
        documents; we translate that specific case into ``None`` instead of
        surfacing a malformed-response error so operators can tell "not yet
        searchable" apart from "sync actually broken".
        """

        index = await self._ensure_index_client()
        try:
            raw_document = await index.get_document(_document_id_for_meme_file(meme_file_id))
        except Exception as exc:
            status_code = _extract_sdk_status_code(exc)
            if status_code == 404:
                return None
            _raise_sync_error_from(exc, operation="fetch_document")
            return None  # pragma: no cover - _raise_sync_error_from never returns

        return _build_sync_preview(raw_document, fetched_at=utcnow())

    async def delete_document(self, meme_file_id: uuid.UUID) -> None:
        """Remove the canonical meme document from Meilisearch (e.g. after cascade delete)."""

        index = await self._ensure_index_client()
        try:
            _ = await index.delete_document(_document_id_for_meme_file(meme_file_id))
        except Exception as exc:
            _raise_sync_error_from(exc, operation="delete_document")

    async def ensure_index(self) -> None:
        """Create the configured index if it does not already exist.

        The runtime calls this lazily on startup so the first sync attempt does
        not race the index-creation round-trip. Idempotent: the SDK's
        ``get_or_create_index`` is a no-op when the index already exists.
        """

        client = await self._ensure_client()
        try:
            self._index = await client.get_or_create_index(
                self._settings.pipeline_meilisearch_index_name,
                primary_key="id",
            )
            index = self._index
            assert index is not None
            _ = await index.update_filterable_attributes(list(MEILISEARCH_FILTERABLE_ATTRIBUTES))
        except Exception as exc:
            _raise_sync_error_from(exc, operation="ensure_index")

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        prefilter: SearchIndexPrefilter | None = None,
    ) -> list[dict[str, Any]]:
        """Run a bounded text search against the configured Meilisearch index.

        Returns the raw ``hits`` list from the Meilisearch response as plain
        dicts so the smoke-proof path can locate the target ``meme_file_id``
        via the ``id`` key without the caller having to decode a full SDK
        response model. Errors are mapped onto the same typed taxonomy as
        the other adapter methods so the smoke proof sees consistent
        provider-blocked / timeout / malformed-response reasons.
        """

        index = await self._ensure_index_client()
        filter_expression = prefilter.to_meilisearch_filter() if prefilter is not None else None
        try:
            raw_response = await index.search(query, limit=limit, filter=filter_expression)
        except Exception as exc:
            _raise_sync_error_from(exc, operation="search")
            return []  # pragma: no cover - _raise_sync_error_from never returns

        return _coerce_search_hits(raw_response)

    async def _ensure_index_client(self) -> Any:
        if self._index is None:
            client = await self._ensure_client()
            # ``client.index`` is a cheap in-process factory — it does not
            # contact the server. Real network work happens only on the first
            # ``update_documents`` / ``get_document`` call below, preserving
            # the "no I/O at import time" invariant.
            self._index = client.index(self._settings.pipeline_meilisearch_index_name)
        return self._index

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from meilisearch_python_sdk import AsyncClient

            api_key = self._settings.meilisearch_master_key
            self._client = AsyncClient(
                url=self._settings.meilisearch_url,
                api_key=api_key,
                timeout=max(1, int(self._settings.pipeline_meilisearch_timeout_seconds)),
            )
        return self._client


def _raise_sync_error_from(exc: BaseException, *, operation: str) -> None:
    """Map an arbitrary SDK/transport exception onto the typed sync-error taxonomy.

    Ordering matters: timeouts are detected first (so transport timeouts
    wrapped in SDK exceptions surface as timeouts, not conflicts), then
    HTTP-status-specific conflict/malformed classification, and finally
    everything else falls through as ``MeilisearchSyncProviderUnavailableError``.
    """

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        raise MeilisearchSyncTimeoutError(f"Meilisearch {operation} timed out: {exc}") from exc
    if is_timeout_exception(exc):
        raise MeilisearchSyncTimeoutError(f"Meilisearch {operation} timed out: {exc}") from exc

    # The SDK exposes its own timeout error class (``MeilisearchTimeoutError``)
    # which is NOT a subclass of ``asyncio.TimeoutError``. Detect by name so
    # we don't have to import the SDK at module load time.
    if exc.__class__.__name__ == "MeilisearchTimeoutError":
        raise MeilisearchSyncTimeoutError(f"Meilisearch {operation} timed out: {exc}") from exc

    status_code = _extract_sdk_status_code(exc)
    if status_code == 409:
        raise MeilisearchSyncConflictError(
            f"Meilisearch {operation} rejected with a 409 conflict: {exc}",
        ) from exc
    if status_code is not None and 500 <= status_code < 600:
        raise MeilisearchSyncProviderUnavailableError(
            f"Meilisearch {operation} failed with status {status_code}: {exc}",
        ) from exc
    if status_code is not None and 400 <= status_code < 500 and status_code != 404:
        # 4xx non-404 responses typically indicate a malformed payload the
        # pipeline cannot repair by replaying — e.g. schema mismatch, bad
        # primary key. Treat as terminal so the dead-letter path fires.
        raise MeilisearchSyncMalformedResponseError(
            f"Meilisearch {operation} rejected the request with status {status_code}: {exc}",
        ) from exc

    if _is_structural_parse_error(exc):
        raise MeilisearchSyncMalformedResponseError(
            f"Meilisearch {operation} returned a payload the pipeline cannot trust: {exc}",
        ) from exc

    raise MeilisearchSyncProviderUnavailableError(
        f"Meilisearch {operation} failed: {exc}",
    ) from exc


def _extract_sdk_status_code(exc: BaseException) -> int | None:
    """Pull an HTTP status code off a ``MeilisearchApiError`` without importing the SDK."""

    raw_status = getattr(exc, "status_code", None)
    if isinstance(raw_status, int):
        return raw_status
    return None


def _is_structural_parse_error(exc: BaseException) -> bool:
    """Return ``True`` when the SDK raised a validation/parse failure.

    Pydantic ``ValidationError`` has a module path of ``pydantic``; any
    ``ValueError`` raised while decoding a Meilisearch payload ends up here.
    Treating these as malformed responses mirrors the Qdrant-side behavior.
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


def _build_document_payload(document: PipelineMeilisearchDocument) -> dict[str, Any]:
    """Serialize a ``PipelineMeilisearchDocument`` into the dict Meilisearch accepts."""

    payload: dict[str, Any] = {
        "id": document.id,
        "meme_id": document.meme_id,
        "meme_file_id": document.meme_file_id,
        "search_index_algorithm_version": document.search_index_algorithm_version,
        "is_public": document.is_public,
        "author_user_id": document.author_user_id,
        "media_type": document.media_type,
        "language": document.language,
        "is_nsfw": document.is_nsfw,
        "created_at": document.created_at.isoformat(),
        "tags": list(document.tags),
        "seo_page_slug": document.seo_page_slug,
        "template_id": document.template_id,
        "template_slug": document.template_slug,
        "popularity_score": float(document.popularity_score),
        "like_count": int(document.like_count),
        "collection_ids": list(document.collection_ids),
        "public_collection_ids": list(document.public_collection_ids),
        "unlisted_collection_ids": list(document.unlisted_collection_ids),
        "private_collection_ids": list(document.private_collection_ids),
        "shared_collection_ids": list(document.shared_collection_ids),
        "collection_owner_user_ids": list(document.collection_owner_user_ids),
        "collection_member_user_ids": list(document.collection_member_user_ids),
        "updated_at": document.updated_at.isoformat(),
    }
    if document.ocr_text is not None:
        payload["ocr_text"] = document.ocr_text
    if document.quality_score is not None:
        payload["quality_score"] = float(document.quality_score)
    return payload


def _document_id_for_meme_file(meme_file_id: uuid.UUID) -> str:
    return meme_file_id.hex


def _build_sync_preview(
    raw_document: object,
    *,
    fetched_at: datetime,
) -> ContentPipelineSyncTargetPreview | None:
    """Decode the ``get_document`` response into a bounded preview the inspect surface uses.

    The SDK returns a ``Mapping``-like object (pydantic model or dict) — we
    extract the well-known keys and drop anything else so the preview row does
    not grow unbounded across schema drift.
    """

    if raw_document is None:
        return None
    raw_payload = _coerce_document_payload(raw_document)
    if raw_payload is None:
        return None

    allowed_keys = {
        "id",
        "meme_id",
        "meme_file_id",
        "search_index_algorithm_version",
        "is_public",
        "author_user_id",
        "media_type",
        "language",
        "is_nsfw",
        "created_at",
        "tags",
        "seo_page_slug",
        "template_id",
        "template_slug",
        "popularity_score",
        "like_count",
        "collection_ids",
        "public_collection_ids",
        "unlisted_collection_ids",
        "private_collection_ids",
        "shared_collection_ids",
        "collection_owner_user_ids",
        "collection_member_user_ids",
        "ocr_text",
        "quality_score",
        "updated_at",
    }
    preview_fields: dict[str, object] = {
        key: value for key, value in raw_payload.items() if key in allowed_keys
    }
    return ContentPipelineSyncTargetPreview(
        target=SyncTargetKind.MEILISEARCH,
        preview_fields=preview_fields,
        preview_fetched_at=fetched_at,
    )


def _coerce_search_hits(raw_response: object) -> list[dict[str, Any]]:
    """Return the ``hits`` list from a Meilisearch search response as plain dicts.

    The SDK returns a pydantic ``SearchResults`` model; older versions may
    return a plain dict. We normalize both shapes here so the smoke-proof
    caller always sees ``list[dict[str, Any]]`` regardless of which SDK
    release the environment runs against.
    """

    raw_hits: object = None
    if isinstance(raw_response, dict):
        raw_hits = raw_response.get("hits")
    else:
        model_dump = getattr(raw_response, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump()
            except Exception as exc:  # noqa: BLE001 - degrade a broken SDK model to malformed.
                raise MeilisearchSyncMalformedResponseError(
                    f"Meilisearch search returned a model we cannot decode: {exc}",
                ) from exc
            if isinstance(dumped, dict):
                raw_hits = dumped.get("hits")
        if raw_hits is None:
            raw_hits = getattr(raw_response, "hits", None)

    if raw_hits is None:
        return []
    if not isinstance(raw_hits, list):
        raise MeilisearchSyncMalformedResponseError(
            "Meilisearch search response 'hits' field is not a list.",
        )

    hits: list[dict[str, Any]] = []
    for entry in raw_hits:
        if isinstance(entry, dict):
            hits.append(_plain_string_key_dict(cast("Mapping[object, object]", entry)))
            continue
        entry_dump = getattr(entry, "model_dump", None)
        if callable(entry_dump):
            try:
                dumped_entry = entry_dump()
            except Exception:  # noqa: BLE001 - skip malformed individual hits to stay honest.
                continue
            if isinstance(dumped_entry, dict):
                hits.append(_plain_string_key_dict(dumped_entry))
                continue
        entry_dict = getattr(entry, "__dict__", None)
        if isinstance(entry_dict, dict):
            hits.append(_plain_string_key_dict(entry_dict))
    return hits


def _coerce_document_payload(raw_document: object) -> Mapping[str, object] | None:
    """Return a dict-like view of the SDK response regardless of the concrete type.

    ``get_document`` on the SDK can return a pydantic model, a plain dict, or
    a custom container depending on the SDK version. The preview builder only
    needs key/value access, so we normalize to a plain dict and fall back
    through the usual suspects before giving up.
    """

    if isinstance(raw_document, dict):
        return _plain_string_key_dict(cast("Mapping[object, object]", raw_document))
    model_dump = getattr(raw_document, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:  # noqa: BLE001 - malformed model dumps degrade to None.
            return None
        if isinstance(dumped, dict):
            return _plain_string_key_dict(dumped)
    as_dict = getattr(raw_document, "__dict__", None)
    if isinstance(as_dict, dict):
        return _plain_string_key_dict(as_dict)
    return None


def _plain_string_key_dict(raw_mapping: Mapping[object, object]) -> dict[str, Any]:
    """Copy an SDK mapping into a plain dict with string keys."""

    return {str(key): value for key, value in raw_mapping.items()}


__all__ = [
    "MEILISEARCH_FILTERABLE_ATTRIBUTES",
    "MeilisearchSyncClientProtocol",
    "MeilisearchSyncConflictError",
    "MeilisearchSyncError",
    "MeilisearchSyncMalformedResponseError",
    "MeilisearchSyncProviderUnavailableError",
    "MeilisearchSyncTimeoutError",
    "PipelineMeilisearchDocument",
    "PipelineMeilisearchSyncClient",
]
