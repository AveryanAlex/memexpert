"""Service boundary for Telegram inline meme discovery and sendability."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.models.collection import PinnedMeme
from memexpert.models.content import Meme, MemeFile, TelegramFileIdCache
from memexpert.models.enums import AnalyticsEventType, ContentKind, TelegramMediaFormat
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.meme import MemeResultAttributionRead, new_discovery_request_id
from memexpert.services.meme_search import (
    MemeSearchFilters,
    MemeSearchScope,
    _apply_filters,
    _build_result_attribution,
    _search_scope_meme_stmt,
    _to_card_read,
)
from memexpert.services.telegram_accounts import resolve_or_create_active_telegram_user

if TYPE_CHECKING:
    from memexpert.schemas.meme import MemeCardRead, MemeFileRead, MemeSearchPageRead, MemeSearchResultRead

logger = logging.getLogger(__name__)

MPEG4_GIF_MIME_TYPE = "video/mp4"


class TelegramInlineMemeSearchService(Protocol):
    """Search boundary consumed by the Telegram inline service."""

    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "telegram_inline_search",
    ) -> MemeSearchPageRead: ...


class TelegramInlineMediaUrlProvider(Protocol):
    """Build Telegram-fetchable public HTTPS URLs for private original media."""

    async def get_media_url(self, file: MemeFileRead) -> str | None: ...


@dataclass(frozen=True, slots=True)
class TelegramInlineMediaResult:
    """Plain service DTO for one sendable Telegram inline result candidate."""

    meme: MemeCardRead
    file: MemeFileRead
    media_format: TelegramMediaFormat
    cached_file_id: str | None
    media_url: str | None
    attribution: MemeResultAttributionRead = field(default_factory=MemeResultAttributionRead)


@dataclass(frozen=True, slots=True)
class TelegramInlineSearchPage:
    """Plain service page returned to the aiogram adapter."""

    items: list[TelegramInlineMediaResult]
    limit: int
    offset: int
    total: int
    has_more: bool
    is_personal: bool
    request_id: str = field(default_factory=new_discovery_request_id)


class TelegramInlineServiceProtocol(Protocol):
    async def search_inline_memes(
        self,
        *,
        telegram_user_id: int,
        query: str,
        limit: int = 20,
        offset: int = 0,
    ) -> TelegramInlineSearchPage: ...


type MemeSearchServiceFactory = Callable[[AsyncSession], TelegramInlineMemeSearchService]
type TelegramInlineServiceFactory = Callable[[AsyncSession], TelegramInlineServiceProtocol]


@dataclass(slots=True)
class _InlineCandidate:
    meme: MemeCardRead
    file: MemeFileRead
    media_format: TelegramMediaFormat
    attribution: MemeResultAttributionRead


@dataclass(slots=True)
class _InlineAttributedMeme:
    meme: MemeCardRead
    attribution: MemeResultAttributionRead


@dataclass(frozen=True, slots=True)
class _InlineSourcedMeme:
    meme: Meme
    source_algorithm: str
    reason: str
    score: float | None = None
    score_components: dict[str, float] = field(default_factory=dict)


class TelegramInlineService:
    """Own Telegram inline meme lookup, access filtering, and media sendability."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        meme_search_service: TelegramInlineMemeSearchService,
        media_url_provider: TelegramInlineMediaUrlProvider,
        bot_scope: str,
    ) -> None:
        self._session = session
        self._meme_search_service = meme_search_service
        self._media_url_provider = media_url_provider
        self._bot_scope = bot_scope

    async def search_inline_memes(
        self,
        *,
        telegram_user_id: int,
        query: str,
        limit: int = 20,
        offset: int = 0,
    ) -> TelegramInlineSearchPage:
        """Return sendable Telegram inline meme results for a Telegram user/query."""

        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        account_resolution = await resolve_or_create_active_telegram_user(
            self._session,
            telegram_user_id=telegram_user_id,
        )
        linked_user = account_resolution.user if account_resolution.is_active else None
        normalized_query = query.strip()

        if normalized_query:
            return await self._search_text_query(
                normalized_query,
                linked_user=linked_user,
                limit=resolved_limit,
                offset=resolved_offset,
            )

        return await self._search_empty_query(
            linked_user=linked_user,
            limit=resolved_limit,
            offset=resolved_offset,
        )

    async def _search_text_query(
        self,
        query: str,
        *,
        linked_user: User | None,
        limit: int,
        offset: int,
    ) -> TelegramInlineSearchPage:
        filters = MemeSearchFilters(
            include_nsfw=linked_user.nsfw_enabled if linked_user else False,
            scope=MemeSearchScope.ALL if linked_user else MemeSearchScope.PUBLIC,
        )
        page = await self._meme_search_service.search_memes(
            query,
            viewer_user_id=linked_user.id if linked_user else None,
            filters=filters,
            limit=limit,
            offset=offset,
            surface="telegram_inline_search",
        )
        items = await self._build_sendable_results(_inline_items_from_search_results(page.items))
        return TelegramInlineSearchPage(
            items=items,
            limit=page.limit,
            offset=page.offset,
            total=page.total,
            has_more=page.has_more,
            is_personal=linked_user is not None,
            request_id=page.request_id,
        )

    async def _search_empty_query(
        self,
        *,
        linked_user: User | None,
        limit: int,
        offset: int,
    ) -> TelegramInlineSearchPage:
        request_id = new_discovery_request_id()
        if linked_user is None:
            candidate_memes = [
                _InlineSourcedMeme(
                    meme=meme,
                    source_algorithm="popular",
                    reason="empty_query_public_popular",
                    score=meme.popularity_score,
                    score_components={"popularity": meme.popularity_score},
                )
                for meme in await self._load_popular_memes(viewer_user_id=None, include_nsfw=False)
            ]
            filters = MemeSearchFilters(include_nsfw=False, scope=MemeSearchScope.PUBLIC)
            is_personal = False
        else:
            pinned_memes = await self._load_pinned_memes(linked_user)
            recent_memes = await self._load_recent_send_memes(linked_user)
            popular_memes = await self._load_popular_memes(
                viewer_user_id=linked_user.id,
                include_nsfw=linked_user.nsfw_enabled,
            )
            candidate_memes = _dedupe_sourced_memes(
                (
                    _InlineSourcedMeme(
                        meme=meme,
                        source_algorithm="personalized_discovery",
                        reason="pinned",
                    )
                    for meme in pinned_memes
                ),
                (
                    _InlineSourcedMeme(
                        meme=meme,
                        source_algorithm="personalized_discovery",
                        reason="recent_send",
                    )
                    for meme in recent_memes
                ),
                (
                    _InlineSourcedMeme(
                        meme=meme,
                        source_algorithm="popular",
                        reason="empty_query_popular_fallback",
                        score=meme.popularity_score,
                        score_components={"popularity": meme.popularity_score},
                    )
                    for meme in popular_memes
                ),
            )
            filters = MemeSearchFilters(
                include_nsfw=linked_user.nsfw_enabled,
                scope=MemeSearchScope.ALL,
            )
            is_personal = True

        page_memes = candidate_memes[offset : offset + limit]
        return TelegramInlineSearchPage(
            items=await self._build_sendable_results(
                [
                    _InlineAttributedMeme(
                        meme=_to_card_read(item.meme),
                        attribution=_build_result_attribution(
                            request_id=request_id,
                            surface="telegram_inline_empty_query",
                            source_algorithm=item.source_algorithm,
                            rank=rank,
                            query=None,
                            filters=filters,
                            score=item.score,
                            score_components=item.score_components,
                            reason=item.reason,
                        ),
                    )
                    for rank, item in enumerate(page_memes, start=offset + 1)
                ]
            ),
            limit=limit,
            offset=offset,
            total=len(candidate_memes),
            has_more=offset + limit < len(candidate_memes),
            is_personal=is_personal,
            request_id=request_id,
        )

    async def _load_pinned_memes(self, user: User) -> list[Meme]:
        stmt = (
            _visible_meme_stmt(user.id, include_nsfw=user.nsfw_enabled)
            .join(PinnedMeme, PinnedMeme.meme_id == Meme.id)
            .where(PinnedMeme.user_id == user.id)
            .order_by(PinnedMeme.position.asc(), PinnedMeme.pinned_at.desc(), Meme.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def _load_recent_send_memes(self, user: User) -> list[Meme]:
        result = await self._session.execute(
            select(AnalyticsEvent.payload)
            .where(AnalyticsEvent.user_id == user.id, AnalyticsEvent.event_type == AnalyticsEventType.MEME_SEND)
            .order_by(AnalyticsEvent.occurred_at.desc())
            .limit(200)
        )

        meme_ids: list[uuid.UUID] = []
        file_ids: list[uuid.UUID] = []
        for payload in result.scalars():
            if not isinstance(payload, dict):
                continue
            refs = payload.get("refs")
            if not isinstance(refs, dict):
                refs = {}
            meme_id = _parse_payload_uuid(payload.get("meme_id")) or _parse_payload_uuid(refs.get("meme_id"))
            if meme_id is not None:
                _append_unique(meme_ids, meme_id)
                continue

            file_id = _parse_payload_uuid(payload.get("meme_file_id")) or _parse_payload_uuid(refs.get("meme_file_id"))
            if file_id is not None:
                _append_unique(file_ids, file_id)

        if file_ids:
            file_result = await self._session.execute(
                select(MemeFile.id, MemeFile.meme_id).where(MemeFile.id.in_(tuple(file_ids)))
            )
            file_to_meme_id: dict[uuid.UUID, uuid.UUID] = {file_id: meme_id for file_id, meme_id in file_result.all()}
            for file_id in file_ids:
                meme_id = file_to_meme_id.get(file_id)
                if meme_id is not None:
                    _append_unique(meme_ids, meme_id)

        return await self._load_visible_memes_by_ids(
            tuple(meme_ids),
            viewer_user_id=user.id,
            include_nsfw=user.nsfw_enabled,
        )

    async def _load_popular_memes(self, *, viewer_user_id: uuid.UUID | None, include_nsfw: bool) -> list[Meme]:
        stmt = _visible_meme_stmt(viewer_user_id, include_nsfw=include_nsfw).order_by(
            Meme.popularity_score.desc(),
            Meme.created_at.desc(),
            Meme.id.asc(),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def _load_visible_memes_by_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID,
        include_nsfw: bool,
    ) -> list[Meme]:
        if not meme_ids:
            return []

        result = await self._session.execute(
            _visible_meme_stmt(viewer_user_id, include_nsfw=include_nsfw).where(Meme.id.in_(meme_ids))
        )
        memes_by_id = {meme.id: meme for meme in result.scalars().unique().all()}
        return [memes_by_id[meme_id] for meme_id in meme_ids if meme_id in memes_by_id]

    async def _build_sendable_results(self, memes: list[_InlineAttributedMeme]) -> list[TelegramInlineMediaResult]:
        candidates = [_to_inline_candidate(meme) for meme in memes]
        supported_candidates = [candidate for candidate in candidates if candidate is not None]
        cache = await self._load_file_id_cache(
            file_formats={candidate.file.id: candidate.media_format for candidate in supported_candidates},
        )

        results: list[TelegramInlineMediaResult] = []
        for candidate in supported_candidates:
            cached_file_id = cache.get((candidate.file.id, candidate.media_format))
            media_url = None if cached_file_id is not None else await self._resolve_inline_media_url(candidate.file)
            if cached_file_id is None and media_url is None:
                continue
            results.append(
                TelegramInlineMediaResult(
                    meme=candidate.meme,
                    file=candidate.file,
                    media_format=candidate.media_format,
                    cached_file_id=cached_file_id,
                    media_url=media_url,
                    attribution=candidate.attribution,
                )
            )
        return results

    async def _resolve_inline_media_url(self, file: MemeFileRead) -> str | None:
        media_url = public_https_url(file.s3_original_key)
        if media_url is not None:
            return media_url

        media_url = public_https_url(await self._media_url_provider.get_media_url(file))
        if media_url is None:
            logger.info(
                "Telegram inline private media was skipped because no sendable URL exists for meme_file_id=%s.",
                file.id,
            )
        return media_url

    async def _load_file_id_cache(
        self,
        *,
        file_formats: dict[uuid.UUID, TelegramMediaFormat],
    ) -> dict[tuple[uuid.UUID, TelegramMediaFormat], str]:
        if not file_formats:
            return {}

        result = await self._session.execute(
            select(TelegramFileIdCache).where(
                TelegramFileIdCache.meme_file_id.in_(tuple(file_formats)),
                TelegramFileIdCache.bot_scope == self._bot_scope,
            )
        )
        cache: dict[tuple[uuid.UUID, TelegramMediaFormat], str] = {}
        for row in result.scalars():
            expected_format = file_formats.get(row.meme_file_id)
            if row.media_format == expected_format:
                cache[(row.meme_file_id, row.media_format)] = row.telegram_file_id
        return cache


def _visible_meme_stmt(viewer_user_id: uuid.UUID | None, *, include_nsfw: bool):
    scope = MemeSearchScope.ALL if viewer_user_id is not None else MemeSearchScope.PUBLIC
    return _apply_filters(
        _search_scope_meme_stmt(viewer_user_id, scope=scope),
        MemeSearchFilters(include_nsfw=include_nsfw, scope=scope),
    )


def _to_inline_candidate(item: _InlineAttributedMeme) -> _InlineCandidate | None:
    meme = item.meme
    file = meme.primary_file
    if file is None:
        return None

    media_format = _resolve_telegram_media_format(meme=meme, file=file)
    if media_format is None:
        return None
    return _InlineCandidate(meme=meme, file=file, media_format=media_format, attribution=item.attribution)


def _resolve_telegram_media_format(*, meme: MemeCardRead, file: MemeFileRead) -> TelegramMediaFormat | None:
    if meme.media_type is ContentKind.GIF or file.mime_type in {"image/gif", MPEG4_GIF_MIME_TYPE}:
        return TelegramMediaFormat.ANIMATION
    if meme.media_type is ContentKind.IMAGE:
        return TelegramMediaFormat.PHOTO
    return None


def public_https_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    return None


def _inline_items_from_search_results(items: list[MemeSearchResultRead]) -> list[_InlineAttributedMeme]:
    return [_InlineAttributedMeme(meme=item.meme, attribution=item.attribution) for item in items]


def _dedupe_sourced_memes(*buckets: Iterable[_InlineSourcedMeme]) -> list[_InlineSourcedMeme]:
    deduped: list[_InlineSourcedMeme] = []
    seen: set[uuid.UUID] = set()
    for bucket in buckets:
        for item in bucket:
            if item.meme.id in seen:
                continue
            seen.add(item.meme.id)
            deduped.append(item)
    return deduped


def _append_unique(values: list[uuid.UUID], value: uuid.UUID) -> None:
    if value not in values:
        values.append(value)


def _parse_payload_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _clamp_limit(limit: int) -> int:
    return min(100, max(1, limit))


__all__ = [
    "MPEG4_GIF_MIME_TYPE",
    "MemeSearchServiceFactory",
    "TelegramInlineMediaResult",
    "TelegramInlineMediaUrlProvider",
    "TelegramInlineSearchPage",
    "TelegramInlineService",
    "TelegramInlineServiceFactory",
    "TelegramInlineServiceProtocol",
    "public_https_url",
]
