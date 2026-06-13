"""Canonical PostgreSQL-derived builders for search-index sync payloads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from memexpert.core.meilisearch import PipelineMeilisearchDocument
from memexpert.core.qdrant import QdrantSyncPayload
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import EmbeddingCache, Meme, MemeFile, MemeSeoPage
from memexpert.models.enums import CollectionVisibility, EmbeddingInputType
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SEARCH_INDEX_ALGORITHM_VERSION = "collection-aware-v1"
type CollectionHintRow = tuple[
    uuid.UUID,
    uuid.UUID,
    CollectionVisibility,
    datetime,
    datetime,
    uuid.UUID | None,
    datetime | None,
]


@dataclass(frozen=True, slots=True)
class CanonicalSearchIndexState:
    """All safe canonical metadata the search-index sync stages advertise."""

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    search_index_algorithm_version: str
    is_public: bool
    author_user_id: str | None
    media_type: str
    language: str
    is_nsfw: bool
    tags: tuple[str, ...]
    seo_page_slug: str | None
    template_id: str | None
    template_slug: str | None
    popularity_score: float
    like_count: int
    created_at: datetime
    updated_at: datetime
    quality_score: float
    ocr_text: str | None
    source_object_key: str
    collection_ids: tuple[str, ...]
    public_collection_ids: tuple[str, ...]
    unlisted_collection_ids: tuple[str, ...]
    private_collection_ids: tuple[str, ...]
    shared_collection_ids: tuple[str, ...]
    collection_owner_user_ids: tuple[str, ...]
    collection_member_user_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoadedSearchIndexState:
    """Canonical search-index state plus the optional semantic vector."""

    canonical: CanonicalSearchIndexState
    vector: tuple[float, ...] | None = None


async def load_search_index_state(
    session: AsyncSession,
    meme_file_id: uuid.UUID,
    *,
    vector_dimensions: int | None = None,
) -> LoadedSearchIndexState:
    """Load canonical search-index inputs for one meme file from PostgreSQL."""

    meme_file = await session.scalar(
        select(MemeFile)
        .execution_options(populate_existing=True)
        .options(
            joinedload(MemeFile.meme).joinedload(Meme.seo_page),
            joinedload(MemeFile.meme).joinedload(Meme.template),
            joinedload(MemeFile.ocr_result),
        )
        .where(MemeFile.id == meme_file_id)
    )
    if meme_file is None:
        raise PipelineIngestError(
            f"Search-index sync could not find meme file {meme_file_id}.",
        )

    canonical_meme = meme_file.meme
    if canonical_meme is None:
        raise PipelineIngestError(
            f"Search-index sync could not find canonical meme for file {meme_file_id}.",
        )

    raw_collection_rows = (
        await session.execute(
            select(
                Collection.id,
                Collection.owner_id,
                Collection.visibility,
                Collection.updated_at,
                CollectionMeme.added_at,
                CollectionMember.user_id,
                CollectionMember.joined_at,
            )
            .select_from(CollectionMeme)
            .join(Collection, Collection.id == CollectionMeme.collection_id)
            .outerjoin(CollectionMember, CollectionMember.collection_id == Collection.id)
            .where(CollectionMeme.meme_id == canonical_meme.id)
        )
    ).all()
    collection_rows = cast("list[CollectionHintRow]", [tuple(row) for row in raw_collection_rows])

    seo_page = canonical_meme.seo_page
    canonical = CanonicalSearchIndexState(
        meme_id=canonical_meme.id,
        meme_file_id=meme_file.id,
        search_index_algorithm_version=SEARCH_INDEX_ALGORITHM_VERSION,
        is_public=canonical_meme.is_public,
        author_user_id=_stringify_uuid(canonical_meme.author_user_id),
        media_type=canonical_meme.media_type.value,
        language=canonical_meme.language.value,
        is_nsfw=canonical_meme.is_nsfw,
        tags=tuple(canonical_meme.tags),
        seo_page_slug=seo_page.slug if seo_page is not None else None,
        template_id=_stringify_uuid(canonical_meme.template_id),
        template_slug=canonical_meme.template.slug if canonical_meme.template is not None else None,
        popularity_score=float(canonical_meme.popularity_score),
        like_count=canonical_meme.like_count,
        created_at=canonical_meme.created_at,
        updated_at=_resolve_search_index_updated_at(
            meme=canonical_meme,
            meme_file=meme_file,
            seo_page=seo_page,
            collection_rows=collection_rows,
        ),
        quality_score=float(meme_file.quality_score),
        ocr_text=(
            meme_file.ocr_result.extracted_text
            if meme_file.ocr_result is not None
            else canonical_meme.ocr_text
        ),
        source_object_key=meme_file.s3_original_key,
        collection_ids=_collection_ids_for(collection_rows),
        public_collection_ids=_collection_ids_for(collection_rows, visibility=CollectionVisibility.PUBLIC),
        unlisted_collection_ids=_collection_ids_for(collection_rows, visibility=CollectionVisibility.UNLISTED),
        private_collection_ids=_collection_ids_for(collection_rows, visibility=CollectionVisibility.PRIVATE),
        shared_collection_ids=_shared_collection_ids_for(collection_rows),
        collection_owner_user_ids=_collection_owner_user_ids_for(collection_rows),
        collection_member_user_ids=_collection_member_user_ids_for(collection_rows),
    )

    vector: tuple[float, ...] | None = None
    if vector_dimensions is not None:
        from memexpert.core.voyage import decode_embedding_bytes

        cache_row = await session.scalar(
            select(EmbeddingCache)
            .where(
                EmbeddingCache.source_file_id == meme_file_id,
                EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
            )
            .order_by(EmbeddingCache.created_at.desc())
            .limit(1)
        )
        if cache_row is None:
            raise PipelineIngestError(
                f"Search-index sync could not find an embedding cache row for {meme_file_id}.",
            )
        vector = decode_embedding_bytes(cache_row.embedding, dimensions=vector_dimensions)

    return LoadedSearchIndexState(canonical=canonical, vector=vector)


def build_qdrant_sync_payload(canonical: CanonicalSearchIndexState) -> QdrantSyncPayload:
    """Project canonical PostgreSQL state into the Qdrant sync payload shape."""

    return QdrantSyncPayload(
        meme_id=canonical.meme_id,
        meme_file_id=canonical.meme_file_id,
        search_index_algorithm_version=canonical.search_index_algorithm_version,
        is_public=canonical.is_public,
        author_user_id=canonical.author_user_id,
        media_type=canonical.media_type,
        language=canonical.language,
        is_nsfw=canonical.is_nsfw,
        tags=list(canonical.tags),
        seo_page_slug=canonical.seo_page_slug,
        template_id=canonical.template_id,
        template_slug=canonical.template_slug,
        popularity_score=canonical.popularity_score,
        like_count=canonical.like_count,
        created_at=canonical.created_at,
        updated_at=canonical.updated_at,
        quality_score=canonical.quality_score,
        collection_ids=list(canonical.collection_ids),
        public_collection_ids=list(canonical.public_collection_ids),
        unlisted_collection_ids=list(canonical.unlisted_collection_ids),
        private_collection_ids=list(canonical.private_collection_ids),
        shared_collection_ids=list(canonical.shared_collection_ids),
        collection_owner_user_ids=list(canonical.collection_owner_user_ids),
        collection_member_user_ids=list(canonical.collection_member_user_ids),
        ocr_snippet=canonical.ocr_text,
        source_object_key=canonical.source_object_key,
    )


def build_meilisearch_document(canonical: CanonicalSearchIndexState) -> PipelineMeilisearchDocument:
    """Project canonical PostgreSQL state into the Meilisearch document shape."""

    return PipelineMeilisearchDocument(
        id=canonical.meme_file_id.hex,
        meme_id=str(canonical.meme_id),
        meme_file_id=str(canonical.meme_file_id),
        search_index_algorithm_version=canonical.search_index_algorithm_version,
        is_public=canonical.is_public,
        author_user_id=canonical.author_user_id,
        media_type=canonical.media_type,
        language=canonical.language,
        is_nsfw=canonical.is_nsfw,
        created_at=canonical.created_at,
        updated_at=canonical.updated_at,
        tags=list(canonical.tags),
        seo_page_slug=canonical.seo_page_slug,
        template_id=canonical.template_id,
        template_slug=canonical.template_slug,
        popularity_score=canonical.popularity_score,
        like_count=canonical.like_count,
        quality_score=canonical.quality_score,
        collection_ids=list(canonical.collection_ids),
        public_collection_ids=list(canonical.public_collection_ids),
        unlisted_collection_ids=list(canonical.unlisted_collection_ids),
        private_collection_ids=list(canonical.private_collection_ids),
        shared_collection_ids=list(canonical.shared_collection_ids),
        collection_owner_user_ids=list(canonical.collection_owner_user_ids),
        collection_member_user_ids=list(canonical.collection_member_user_ids),
        ocr_text=canonical.ocr_text,
    )


def _resolve_search_index_updated_at(
    *,
    meme: Meme,
    meme_file: MemeFile,
    seo_page: MemeSeoPage | None,
    collection_rows: list[CollectionHintRow],
) -> datetime:
    candidates = [meme.updated_at, meme_file.updated_at]
    if meme.template is not None:
        candidates.append(meme.template.updated_at)
    if seo_page is not None:
        candidates.append(seo_page.edited_at or seo_page.generated_at)
    for _, _, _, collection_updated_at, collection_added_at, _, member_joined_at in collection_rows:
        candidates.append(collection_updated_at)
        candidates.append(collection_added_at)
        if member_joined_at is not None:
            candidates.append(member_joined_at)
    return max(candidates)


def _collection_ids_for(
    collection_rows: list[CollectionHintRow],
    *,
    visibility: CollectionVisibility | None = None,
) -> tuple[str, ...]:
    collection_ids = {
        str(collection_id)
        for collection_id, _, row_visibility, _, _, _, _ in collection_rows
        if visibility is None or row_visibility is visibility
    }
    return tuple(sorted(collection_ids))


def _shared_collection_ids_for(
    collection_rows: list[CollectionHintRow],
) -> tuple[str, ...]:
    shared_ids = {
        str(collection_id)
        for collection_id, owner_id, _, _, _, member_user_id, _ in collection_rows
        if member_user_id is not None and member_user_id != owner_id
    }
    return tuple(sorted(shared_ids))


def _collection_owner_user_ids_for(
    collection_rows: list[CollectionHintRow],
) -> tuple[str, ...]:
    owner_user_ids = {str(owner_id) for _, owner_id, _, _, _, _, _ in collection_rows}
    return tuple(sorted(owner_user_ids))


def _collection_member_user_ids_for(
    collection_rows: list[CollectionHintRow],
) -> tuple[str, ...]:
    member_user_ids = {
        str(member_user_id)
        for _, _, _, _, _, member_user_id, _ in collection_rows
        if member_user_id is not None
    }
    return tuple(sorted(member_user_ids))


def _stringify_uuid(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "CanonicalSearchIndexState",
    "LoadedSearchIndexState",
    "SEARCH_INDEX_ALGORITHM_VERSION",
    "build_meilisearch_document",
    "build_qdrant_sync_payload",
    "load_search_index_state",
]
