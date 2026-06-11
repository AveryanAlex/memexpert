# ruff: noqa: TC001,TC002
"""Shared hybrid meme search and read service for web and Telegram bot surfaces."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Select, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from memexpert.core.qdrant import QdrantUserSearchClientProtocol, QdrantUserSearchMatch
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import (
    MemeCardRead,
    MemeDetailRead,
    MemeFileRead,
    MemeSearchPageRead,
    MemeSearchResultRead,
    MemeSearchScoreRead,
)

TEXT_SCORE_KEYS = ("_rankingScore", "_score", "rankingScore", "score")
SEMANTIC_WEIGHT = 0.50
TEXT_WEIGHT = 0.35
POPULARITY_WEIGHT = 0.15

logger = logging.getLogger(__name__)


class MemeNotFoundError(LookupError):
    """Raised when a meme does not exist or is not visible to the caller."""


class MemeTextSearchClientProtocol(Protocol):
    """Narrow text-search boundary used by the shared meme search service."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...


class MemeQueryEmbeddingClientProtocol(Protocol):
    """Plain-text query embedding boundary used before user-facing semantic search."""

    async def embed_query(self, query: str) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class MemeSearchFilters:
    """Filters supported by web and bot search surfaces.

    ``tags`` is the currently available taxonomy field on ``Meme``. Categories
    are intentionally not represented until the data model gains a category
    source of truth.
    """

    language: ContentLanguage | None = None
    media_type: ContentKind | None = None
    include_nsfw: bool = False
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class _CandidateScore:
    meme_id: uuid.UUID | None = None
    meme_file_id: uuid.UUID | None = None
    semantic_raw: float = 0.0
    text_raw: float = 0.0
    semantic: float = 0.0
    text: float = 0.0
    popularity: float = 0.0
    total: float = 0.0


class MemeSearchService:
    """Hybrid search/read service over indexed candidates and canonical DB DTOs.

    Initial ranking strategy: collect candidate meme IDs from Meilisearch text
    hits and Qdrant semantic hits, normalize semantic relevance, text relevance,
    and DB popularity independently to 0..1 over the candidate set, then sort by
    ``0.50 * semantic + 0.35 * text + 0.15 * popularity``. This deliberately
    favors semantic intent while preserving exact-text matches and giving popular
    memes a small stable boost.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        text_client: MemeTextSearchClientProtocol | None = None,
        semantic_client: QdrantUserSearchClientProtocol | None = None,
        query_embedding_client: MemeQueryEmbeddingClientProtocol | None = None,
    ) -> None:
        self._session = session
        self._text_client = text_client
        self._semantic_client = semantic_client
        self._query_embedding_client = query_embedding_client

    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        query_vector: tuple[float, ...] | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> MemeSearchPageRead:
        resolved_filters = filters or MemeSearchFilters()
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        candidate_limit = max(resolved_limit + resolved_offset, resolved_limit) * 4

        normalized_query = query.strip()
        resolved_query_vector = await self._resolve_query_vector(normalized_query, query_vector=query_vector)
        candidates = await self._collect_index_candidates(
            normalized_query,
            query_vector=resolved_query_vector,
            limit=candidate_limit,
        )
        if not candidates:
            return await self._popular_page(
                viewer_user_id=viewer_user_id,
                filters=resolved_filters,
                limit=resolved_limit,
                offset=resolved_offset,
            )

        await self._resolve_missing_meme_ids(candidates)
        candidates = {score.meme_id: score for score in candidates.values() if score.meme_id is not None}
        if not candidates:
            return MemeSearchPageRead(items=[], limit=resolved_limit, offset=resolved_offset, total=0, has_more=False)

        memes = await self._load_visible_memes(
            tuple(candidates),
            viewer_user_id=viewer_user_id,
            filters=resolved_filters,
        )
        visible_scores = {meme.id: candidates[meme.id] for meme in memes}
        self._apply_normalized_scores(visible_scores, {meme.id: meme.popularity_score for meme in memes})

        ranked_memes = sorted(
            memes,
            key=lambda meme: (visible_scores[meme.id].total, meme.popularity_score, meme.created_at),
            reverse=True,
        )
        total = len(ranked_memes)
        page_memes = ranked_memes[resolved_offset : resolved_offset + resolved_limit]
        items = [
            MemeSearchResultRead(
                meme=_to_card_read(meme),
                score=_to_score_read(visible_scores[meme.id]),
            )
            for meme in page_memes
        ]

        return MemeSearchPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total,
            has_more=resolved_offset + resolved_limit < total,
        )

    async def get_meme_detail(
        self,
        meme_id: uuid.UUID,
        *,
        viewer_user_id: uuid.UUID | None = None,
        include_nsfw: bool = False,
    ) -> MemeDetailRead:
        stmt = _visible_meme_stmt(viewer_user_id).where(Meme.id == meme_id)
        if not include_nsfw:
            stmt = stmt.where(Meme.is_nsfw.is_(False))

        meme = await self._session.scalar(stmt)
        if meme is None:
            raise MemeNotFoundError("Meme was not found or is not visible to this caller.")
        return _to_detail_read(meme)

    async def browse_memes(
        self,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> MemeSearchPageRead:
        """Return a stable popular catalog page using the service fallback behavior."""

        return await self._popular_page(
            viewer_user_id=viewer_user_id,
            filters=filters or MemeSearchFilters(),
            limit=_clamp_limit(limit),
            offset=max(0, offset),
        )

    async def _resolve_query_vector(
        self,
        query: str,
        *,
        query_vector: tuple[float, ...] | None,
    ) -> tuple[float, ...] | None:
        if query_vector is not None or not query or self._query_embedding_client is None:
            return query_vector

        try:
            vector = await self._query_embedding_client.embed_query(query)
        except Exception:
            logger.exception("Text query embedding failed; falling back to text-only meme search.")
            return None
        return vector or None

    async def _collect_index_candidates(
        self,
        query: str,
        *,
        query_vector: tuple[float, ...] | None,
        limit: int,
    ) -> dict[uuid.UUID, _CandidateScore]:
        candidates: dict[uuid.UUID, _CandidateScore] = {}

        if self._text_client is not None and query:
            try:
                text_hits = await self._text_client.search(query, limit=limit)
            except Exception:
                logger.exception("Text meme search failed; falling back to semantic/popular candidates.")
                text_hits = []
            for rank, hit in enumerate(text_hits, start=1):
                key = _candidate_key_from_hit(hit)
                if key is None:
                    continue
                candidate = candidates.setdefault(key, _CandidateScore())
                _set_candidate_ids(candidate, hit)
                candidate.text_raw = max(candidate.text_raw, _text_score_from_hit(hit, rank))

        if self._semantic_client is not None and query_vector is not None:
            try:
                semantic_hits = await self._semantic_client.search_memes_by_vector(
                    query_vector=query_vector,
                    limit=limit,
                )
            except Exception:
                logger.exception("Semantic meme search failed; falling back to text-only candidates.")
                semantic_hits = ()
            for semantic_hit in semantic_hits:
                key = semantic_hit.meme_id
                candidate = candidates.setdefault(key, _CandidateScore(meme_id=semantic_hit.meme_id))
                candidate.meme_file_id = semantic_hit.meme_file_id
                candidate.semantic_raw = max(candidate.semantic_raw, semantic_hit.semantic_score)

        return candidates

    async def _resolve_missing_meme_ids(self, candidates: dict[uuid.UUID, _CandidateScore]) -> None:
        missing_file_ids = tuple(
            score.meme_file_id
            for score in candidates.values()
            if score.meme_id is None and score.meme_file_id is not None
        )
        if not missing_file_ids:
            return

        result = await self._session.execute(
            select(MemeFile.id, MemeFile.meme_id).where(MemeFile.id.in_(missing_file_ids)),
        )
        file_to_meme_id: dict[uuid.UUID, uuid.UUID] = {file_id: meme_id for file_id, meme_id in result.all()}
        for score in candidates.values():
            if score.meme_id is None and score.meme_file_id is not None:
                score.meme_id = file_to_meme_id.get(score.meme_file_id)

    async def _load_visible_memes(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
    ) -> list[Meme]:
        stmt = _visible_meme_stmt(viewer_user_id).where(Meme.id.in_(meme_ids))
        stmt = _apply_filters(stmt, filters)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def _popular_page(
        self,
        *,
        viewer_user_id: uuid.UUID | None,
        filters: MemeSearchFilters,
        limit: int,
        offset: int,
    ) -> MemeSearchPageRead:
        base_stmt = _apply_filters(_visible_meme_stmt(viewer_user_id), filters)
        total = await self._session.scalar(select(func.count()).select_from(base_stmt.order_by(None).subquery())) or 0
        result = await self._session.execute(
            base_stmt.order_by(Meme.popularity_score.desc(), Meme.created_at.desc()).limit(limit).offset(offset),
        )
        memes = list(result.scalars().all())
        max_popularity = max((meme.popularity_score for meme in memes), default=0.0)
        items = []
        for meme in memes:
            popularity = _normalize_value(meme.popularity_score, max_popularity)
            score = _CandidateScore(popularity=popularity, total=POPULARITY_WEIGHT * popularity)
            items.append(MemeSearchResultRead(meme=_to_card_read(meme), score=_to_score_read(score)))
        return MemeSearchPageRead(items=items, limit=limit, offset=offset, total=total, has_more=offset + limit < total)

    def _apply_normalized_scores(
        self,
        scores: dict[uuid.UUID, _CandidateScore],
        popularity_by_meme_id: dict[uuid.UUID, float],
    ) -> None:
        max_semantic = max((score.semantic_raw for score in scores.values()), default=0.0)
        max_text = max((score.text_raw for score in scores.values()), default=0.0)
        max_popularity = max(popularity_by_meme_id.values(), default=0.0)
        for meme_id, score in scores.items():
            score.semantic = _normalize_value(score.semantic_raw, max_semantic)
            score.text = _normalize_value(score.text_raw, max_text)
            score.popularity = _normalize_value(popularity_by_meme_id.get(meme_id, 0.0), max_popularity)
            score.total = (
                SEMANTIC_WEIGHT * score.semantic
                + TEXT_WEIGHT * score.text
                + POPULARITY_WEIGHT * score.popularity
            )


def _visible_meme_stmt(viewer_user_id: uuid.UUID | None) -> Select[tuple[Meme]]:
    stmt = select(Meme).options(
        selectinload(Meme.primary_file),
        selectinload(Meme.files),
        selectinload(Meme.seo_page),
    )
    if viewer_user_id is None:
        return stmt.where(Meme.is_public.is_(True))

    authorized_collection = (
        select(CollectionMeme.meme_id)
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .outerjoin(CollectionMember, CollectionMember.collection_id == Collection.id)
        .where(
            CollectionMeme.meme_id == Meme.id,
            or_(Collection.owner_id == viewer_user_id, CollectionMember.user_id == viewer_user_id),
        )
        .exists()
    )
    return stmt.where(
        or_(
            Meme.is_public.is_(True),
            Meme.author_user_id == viewer_user_id,
            authorized_collection,
        ),
    )


def _apply_filters(stmt: Select[tuple[Meme]], filters: MemeSearchFilters) -> Select[tuple[Meme]]:
    if filters.language is not None:
        stmt = stmt.where(Meme.language == filters.language)
    if filters.media_type is not None:
        stmt = stmt.where(Meme.media_type == filters.media_type)
    if not filters.include_nsfw:
        stmt = stmt.where(Meme.is_nsfw.is_(False))
    for tag in filters.tags:
        stmt = stmt.where(Meme.tags.any(literal(tag)))
    return stmt


def _candidate_key_from_hit(hit: dict[str, Any]) -> uuid.UUID | None:
    raw_id = hit.get("meme_id") or hit.get("id")
    return _parse_uuid(raw_id)


def _set_candidate_ids(candidate: _CandidateScore, hit: dict[str, Any]) -> None:
    meme_id = _parse_uuid(hit.get("meme_id"))
    file_id = _parse_uuid(hit.get("id") or hit.get("meme_file_id"))
    candidate.meme_id = candidate.meme_id or meme_id
    candidate.meme_file_id = candidate.meme_file_id or file_id


def _text_score_from_hit(hit: dict[str, Any], rank: int) -> float:
    for key in TEXT_SCORE_KEYS:
        raw_score = hit.get(key)
        if isinstance(raw_score, int | float):
            return max(0.0, float(raw_score))
    return 1.0 / max(1, rank)


def _parse_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _normalize_value(value: float, max_value: float) -> float:
    if max_value <= 0.0:
        return 0.0
    return max(0.0, min(1.0, value / max_value))


def _clamp_limit(limit: int) -> int:
    return min(100, max(1, limit))


def _to_file_read(file: MemeFile) -> MemeFileRead:
    return MemeFileRead(
        id=file.id,
        mime_type=file.mime_type,
        width=file.width,
        height=file.height,
        file_size_bytes=file.file_size_bytes,
        s3_original_key=file.s3_original_key,
        s3_web_video_key=file.s3_web_video_key,
        blur_hash=file.blur_hash,
        quality_score=file.quality_score,
    )


def _to_card_read(meme: Meme) -> MemeCardRead:
    return MemeCardRead(
        id=meme.id,
        media_type=meme.media_type,
        language=meme.language,
        is_nsfw=meme.is_nsfw,
        popularity_score=meme.popularity_score,
        like_count=meme.like_count,
        tags=list(meme.tags),
        primary_file=_to_file_read(meme.primary_file) if meme.primary_file else None,
        caption=meme.seo_page.caption if meme.seo_page else None,
        created_at=meme.created_at,
        updated_at=meme.updated_at,
    )


def _to_detail_read(meme: Meme) -> MemeDetailRead:
    card = _to_card_read(meme)
    return MemeDetailRead(
        **card.model_dump(),
        ocr_text=meme.ocr_text,
        is_public=meme.is_public,
        author_user_id=meme.author_user_id,
        seo_page_slug=meme.seo_page.slug if meme.seo_page else None,
        seo_title=meme.seo_page.page_title if meme.seo_page else None,
        seo_description=meme.seo_page.meta_description if meme.seo_page else None,
        files=[_to_file_read(file) for file in meme.files],
    )


def _to_score_read(score: _CandidateScore) -> MemeSearchScoreRead:
    return MemeSearchScoreRead(
        semantic=score.semantic,
        text=score.text,
        popularity=score.popularity,
        total=score.total,
    )


__all__ = [
    "MemeNotFoundError",
    "MemeSearchFilters",
    "MemeSearchService",
    "MemeQueryEmbeddingClientProtocol",
    "MemeTextSearchClientProtocol",
    "QdrantUserSearchMatch",
]
