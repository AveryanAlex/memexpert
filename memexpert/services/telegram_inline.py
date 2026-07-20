"""Service boundary for Telegram inline meme discovery and sendability."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.core.config import Settings, get_settings
from memexpert.models.collection import PinnedMeme
from memexpert.models.content import Meme, TelegramFileIdCache
from memexpert.models.enums import ContentKind, TelegramMediaFormat
from memexpert.schemas.meme import (
    MemeResultAttributionRead,
    new_discovery_impression_id,
    new_discovery_request_id,
)
from memexpert.services.meme_search import (
    MemeSearchFilters,
    MemeSearchScope,
    _apply_filters,
    _build_result_attribution,
    _search_scope_meme_stmt,
    _to_card_read,
)
from memexpert.services.recommendations.attribution import sign_result_attribution
from memexpert.services.recommendations.telegram_sessions import (
    PendingHomeRecommendation,
    TelegramInlineCacheUnavailableError,
    TelegramInlineFeedState,
    TelegramInlineSessionStore,
    new_telegram_inline_feed_state,
)
from memexpert.services.telegram_accounts import resolve_or_create_active_telegram_user

if TYPE_CHECKING:
    from memexpert.models.user import User
    from memexpert.schemas.meme import (
        MemeCardRead,
        MemeFileRead,
        MemeSearchPageRead,
        MemeSearchResultRead,
        RecommendationFeedPageRead,
    )

logger = logging.getLogger(__name__)

MPEG4_GIF_MIME_TYPE = "video/mp4"
_TELEGRAM_GUEST_VIEWER_NAMESPACE = uuid.UUID("0d4a3d7e-252c-4a87-a0ce-f73a004bbb46")
_EMPTY_QUERY_SURFACE = "telegram_inline_empty_query"


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


class TelegramInlineRecommendationService(Protocol):
    """Home recommendation boundary consumed by empty inline queries."""

    async def home_feed(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
        cursor: str | None = None,
        offset: int = 0,
    ) -> RecommendationFeedPageRead: ...


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
    next_cursor: str | None = None


class TelegramInlineServiceProtocol(Protocol):
    async def search_inline_memes(
        self,
        *,
        telegram_user_id: int,
        query: str,
        limit: int = 20,
        offset: int = 0,
        cursor: str | None = None,
    ) -> TelegramInlineSearchPage: ...


type MemeSearchServiceFactory = Callable[[AsyncSession], TelegramInlineMemeSearchService]
type RecommendationServiceFactory = Callable[[AsyncSession], TelegramInlineRecommendationService]
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


class TelegramInlineService:
    """Own Telegram inline meme lookup, access filtering, and media sendability."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        meme_search_service: TelegramInlineMemeSearchService,
        recommendation_service: TelegramInlineRecommendationService,
        media_url_provider: TelegramInlineMediaUrlProvider,
        bot_scope: str,
        inline_sessions: TelegramInlineSessionStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._meme_search_service = meme_search_service
        self._recommendation_service = recommendation_service
        self._media_url_provider = media_url_provider
        self._bot_scope = bot_scope
        self._settings = settings or get_settings()
        self._inline_sessions = inline_sessions or TelegramInlineSessionStore(settings=self._settings)

    async def search_inline_memes(
        self,
        *,
        telegram_user_id: int,
        query: str,
        limit: int = 20,
        offset: int = 0,
        cursor: str | None = None,
    ) -> TelegramInlineSearchPage:
        """Return sendable Telegram inline meme results for a Telegram user/query."""

        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        account_resolution = await resolve_or_create_active_telegram_user(
            self._session,
            telegram_user_id=telegram_user_id,
        )
        linked_user = account_resolution.user if account_resolution.is_active else None
        viewer_user_id = (
            linked_user.id
            if linked_user is not None
            else _telegram_guest_viewer_id(telegram_user_id)
        )
        normalized_query = query.strip()

        if normalized_query:
            return await self._search_text_query(
                normalized_query,
                linked_user=linked_user,
                limit=resolved_limit,
                offset=resolved_offset,
            )

        return await self._search_empty_query(
            telegram_user_id=telegram_user_id,
            linked_user=linked_user,
            viewer_user_id=viewer_user_id,
            limit=resolved_limit,
            offset=resolved_offset,
            cursor=cursor,
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
        telegram_user_id: int,
        linked_user: User | None,
        viewer_user_id: uuid.UUID,
        limit: int,
        offset: int,
        cursor: str | None,
    ) -> TelegramInlineSearchPage:
        include_nsfw = linked_user.nsfw_enabled if linked_user is not None else False
        is_personal = linked_user is not None
        if cursor is not None:
            state = await self._inline_sessions.load(
                cursor,
                telegram_user_id=telegram_user_id,
                viewer_user_id=viewer_user_id,
                include_nsfw=include_nsfw,
                is_personal=is_personal,
            )
            legacy_skip = 0
        else:
            pinned_memes = (
                await self._load_pinned_memes(linked_user) if linked_user is not None else []
            )
            state = new_telegram_inline_feed_state(
                telegram_user_id=telegram_user_id,
                viewer_user_id=viewer_user_id,
                include_nsfw=include_nsfw,
                is_personal=is_personal,
                request_id=new_discovery_request_id(),
                pinned_meme_ids=tuple(meme.id for meme in pinned_memes),
                settings=self._settings,
            )
            legacy_skip = offset

        if legacy_skip:
            _, state = await self._consume_empty_query_candidates(
                state,
                limit=legacy_skip,
            )
        page_offset = state.next_rank - 1
        items, state = await self._consume_empty_query_candidates(state, limit=limit)
        has_more = _empty_query_has_more(state)
        next_cursor = None
        if has_more:
            try:
                next_cursor = await self._inline_sessions.save(state)
            except TelegramInlineCacheUnavailableError:
                logger.warning(
                    "telegram_inline_cursor_unavailable",
                    extra={"event": "telegram_inline_cursor_unavailable"},
                )
                has_more = False
        total = max(
            page_offset + len(items) + int(has_more),
            len(state.pinned_meme_ids) + state.home_total,
        )
        return TelegramInlineSearchPage(
            items=items,
            limit=limit,
            offset=page_offset,
            total=total,
            has_more=has_more,
            is_personal=is_personal,
            request_id=state.request_id,
            next_cursor=next_cursor,
        )

    async def _consume_empty_query_candidates(
        self,
        state: TelegramInlineFeedState,
        *,
        limit: int,
    ) -> tuple[list[TelegramInlineMediaResult], TelegramInlineFeedState]:
        """Consume sendable pins, then frozen home candidates, until the page is full."""

        if limit <= 0:
            return [], state
        items: list[TelegramInlineMediaResult] = []
        state = await self._consume_pinned_candidates(state, items=items, limit=limit)
        fetch_attempts = 0
        max_fetch_attempts = max(
            2,
            self._settings.recommendation_feed_pool_limit // min(20, limit) + 2,
        )
        while len(items) < limit and (state.pending_home_items or not state.home_exhausted):
            state = await self._consume_pending_home_candidates(state, items=items, limit=limit)
            if len(items) >= limit or state.pending_home_items:
                break
            if state.home_exhausted:
                break
            fetch_attempts += 1
            if fetch_attempts > max_fetch_attempts:
                logger.warning(
                    "telegram_inline_recommendation_cursor_stalled",
                    extra={"event": "telegram_inline_recommendation_cursor_stalled"},
                )
                state = replace(state, home_exhausted=True, home_cursor=None)
                break
            state = await self._fetch_home_candidates(
                state,
                requested_limit=min(100, max(20, limit - len(items))),
            )
        # Freeze the home pool on the initial request even when explicit pins
        # fill the page. Replaying the compact continuation then always resumes
        # the same recommendation session instead of generating a later pool.
        if not state.home_started:
            state = await self._fetch_home_candidates(state, requested_limit=20)
        return items, state

    async def _consume_pinned_candidates(
        self,
        state: TelegramInlineFeedState,
        *,
        items: list[TelegramInlineMediaResult],
        limit: int,
    ) -> TelegramInlineFeedState:
        if state.next_pinned_index >= len(state.pinned_meme_ids) or len(items) >= limit:
            return state
        remaining_ids = state.pinned_meme_ids[state.next_pinned_index :]
        memes = await self._load_visible_memes_by_ids(
            remaining_ids,
            viewer_user_id=state.viewer_user_id,
            include_nsfw=state.include_nsfw,
        )
        filters = _empty_query_filters(include_nsfw=state.include_nsfw)
        sendable = await self._build_sendable_results(
            [
                _InlineAttributedMeme(
                    meme=_to_card_read(meme),
                    attribution=_build_result_attribution(
                        request_id=state.request_id,
                        surface=_EMPTY_QUERY_SURFACE,
                        source_algorithm="explicit_pins",
                        rank=1,
                        query=None,
                        filters=filters,
                        algorithm_version=self._settings.recommendation_algorithm_version,
                        reason="pinned",
                    ),
                )
                for meme in memes
            ]
        )
        sendable_by_id = {item.meme.id: item for item in sendable}
        next_index = state.next_pinned_index
        next_rank = state.next_rank
        for absolute_index in range(state.next_pinned_index, len(state.pinned_meme_ids)):
            meme_id = state.pinned_meme_ids[absolute_index]
            candidate = sendable_by_id.get(meme_id)
            if candidate is not None and len(items) >= limit:
                break
            next_index = absolute_index + 1
            if candidate is None:
                continue
            items.append(
                self._with_inline_attribution(
                    candidate,
                    state=state,
                    rank=next_rank,
                    source_algorithm="explicit_pins",
                    reason="pinned",
                )
            )
            next_rank += 1
        return replace(state, next_pinned_index=next_index, next_rank=next_rank)

    async def _consume_pending_home_candidates(
        self,
        state: TelegramInlineFeedState,
        *,
        items: list[TelegramInlineMediaResult],
        limit: int,
    ) -> TelegramInlineFeedState:
        if not state.pending_home_items or len(items) >= limit:
            return state
        pending = state.pending_home_items
        memes = await self._load_public_memes_by_ids(
            tuple(item.meme_id for item in pending),
            include_nsfw=state.include_nsfw,
        )
        memes_by_id = {meme.id: meme for meme in memes}
        sendable = await self._build_sendable_results(
            [
                _InlineAttributedMeme(
                    meme=_to_card_read(memes_by_id[item.meme_id]),
                    attribution=item.attribution,
                )
                for item in pending
                if item.meme_id in memes_by_id
                and item.meme_id not in state.pinned_meme_ids
            ]
        )
        sendable_by_id = {item.meme.id: item for item in sendable}
        remaining: list[PendingHomeRecommendation] = []
        next_rank = state.next_rank
        for index, pending_item in enumerate(pending):
            candidate = sendable_by_id.get(pending_item.meme_id)
            if candidate is None:
                continue
            if len(items) >= limit:
                remaining.extend(
                    item
                    for item in pending[index:]
                    if item.meme_id in sendable_by_id
                )
                break
            items.append(
                self._with_inline_attribution(
                    candidate,
                    state=state,
                    rank=next_rank,
                )
            )
            next_rank += 1
        return replace(
            state,
            pending_home_items=tuple(remaining),
            next_rank=next_rank,
        )

    async def _fetch_home_candidates(
        self,
        state: TelegramInlineFeedState,
        *,
        requested_limit: int,
    ) -> TelegramInlineFeedState:
        previous_cursor = state.home_cursor
        page = await self._recommendation_service.home_feed(
            viewer_user_id=state.viewer_user_id,
            filters=_empty_query_filters(include_nsfw=state.include_nsfw),
            limit=requested_limit,
            cursor=state.home_cursor if state.home_started else None,
            offset=0,
        )
        pending = tuple(
            PendingHomeRecommendation(
                meme_id=item.meme.id,
                attribution=item.attribution,
            )
            for item in page.items
        )
        home_exhausted = not page.has_more or page.next_cursor is None
        if state.home_started and page.next_cursor == previous_cursor and not pending:
            home_exhausted = True
        return replace(
            state,
            pending_home_items=pending,
            home_cursor=page.next_cursor,
            home_started=True,
            home_exhausted=home_exhausted,
            home_total=max(state.home_total, page.total),
            expires_at=min(state.expires_at, page.expires_at),
        )

    def _with_inline_attribution(
        self,
        item: TelegramInlineMediaResult,
        *,
        state: TelegramInlineFeedState,
        rank: int,
        source_algorithm: str | None = None,
        reason: str | None = None,
    ) -> TelegramInlineMediaResult:
        attribution = item.attribution.model_copy(
            update={
                "request_id": state.request_id,
                "impression_id": new_discovery_impression_id(),
                "surface": _EMPTY_QUERY_SURFACE,
                "source_algorithm": source_algorithm or item.attribution.source_algorithm,
                "rank": rank,
                "query": None,
                "collection_scope": MemeSearchScope.PUBLIC.value,
                "collection_ids": [],
                "reason": reason or item.attribution.reason,
                "attribution_token": None,
            }
        )
        attribution = sign_result_attribution(
            attribution,
            meme_id=item.meme.id,
            viewer_user_id=state.viewer_user_id,
            settings=self._settings,
        )
        return replace(item, attribution=attribution)

    async def _load_pinned_memes(self, user: User) -> list[Meme]:
        stmt = (
            _apply_filters(
                _search_scope_meme_stmt(user.id, scope=MemeSearchScope.ALL),
                MemeSearchFilters(
                    include_nsfw=user.nsfw_enabled,
                    scope=MemeSearchScope.ALL,
                ),
            )
            .join(PinnedMeme, PinnedMeme.meme_id == Meme.id)
            .where(PinnedMeme.user_id == user.id)
            .order_by(PinnedMeme.position.asc(), PinnedMeme.pinned_at.desc(), Meme.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def _load_public_memes_by_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        include_nsfw: bool,
    ) -> list[Meme]:
        if not meme_ids:
            return []

        result = await self._session.execute(
            _public_meme_stmt(include_nsfw=include_nsfw).where(Meme.id.in_(meme_ids))
        )
        memes_by_id = {meme.id: meme for meme in result.scalars().unique().all()}
        return [memes_by_id[meme_id] for meme_id in meme_ids if meme_id in memes_by_id]

    async def _load_visible_memes_by_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        viewer_user_id: uuid.UUID,
        include_nsfw: bool,
    ) -> list[Meme]:
        if not meme_ids:
            return []

        stmt = _apply_filters(
            _search_scope_meme_stmt(viewer_user_id, scope=MemeSearchScope.ALL),
            MemeSearchFilters(
                include_nsfw=include_nsfw,
                scope=MemeSearchScope.ALL,
            ),
        ).where(Meme.id.in_(meme_ids))
        result = await self._session.execute(stmt)
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


def _public_meme_stmt(*, include_nsfw: bool):
    return _apply_filters(
        _search_scope_meme_stmt(None, scope=MemeSearchScope.PUBLIC),
        _empty_query_filters(include_nsfw=include_nsfw),
    )


def _empty_query_filters(*, include_nsfw: bool) -> MemeSearchFilters:
    return MemeSearchFilters(
        include_nsfw=include_nsfw,
        scope=MemeSearchScope.PUBLIC,
    )


def _empty_query_has_more(state: TelegramInlineFeedState) -> bool:
    return (
        state.next_pinned_index < len(state.pinned_meme_ids)
        or bool(state.pending_home_items)
        or not state.home_exhausted
    )


def _telegram_guest_viewer_id(telegram_user_id: int) -> uuid.UUID:
    return uuid.uuid5(_TELEGRAM_GUEST_VIEWER_NAMESPACE, str(telegram_user_id))


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


def _clamp_limit(limit: int) -> int:
    return min(100, max(1, limit))


__all__ = [
    "MPEG4_GIF_MIME_TYPE",
    "MemeSearchServiceFactory",
    "RecommendationServiceFactory",
    "TelegramInlineMediaResult",
    "TelegramInlineMediaUrlProvider",
    "TelegramInlineRecommendationService",
    "TelegramInlineSearchPage",
    "TelegramInlineService",
    "TelegramInlineServiceFactory",
    "TelegramInlineServiceProtocol",
    "public_https_url",
]
