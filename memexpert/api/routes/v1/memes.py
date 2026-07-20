# ruff: noqa: TC001,TC003
"""Reusable meme search/read routes backed by the shared service layer."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from memexpert.api.dependencies import (
    AdminUserDep,
    AnalyticsServiceDep,
    AutoGuestUserDep,
    CollectionServiceDep,
    CurrentUserDep,
    FullAccountUserDep,
    MemeOfTheDayServiceDep,
    MemeReportServiceDep,
    MemeSearchServiceDep,
    OptionalCurrentUserDep,
    PublicMemeInsightsServiceDep,
    PublicTrendsServiceDep,
)
from memexpert.api.routes._collection_errors import collection_service_http_error
from memexpert.api.routes._meme_interactions import (
    MemeActionAttributionRequest,
    MemeInteractionAttributionRequest,
    payload_attribution,
    record_meme_interaction,
)
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage
from memexpert.schemas.collection import CollectionMemeRead, CollectionRead, MemeLibraryRead, PinnedMemeRead
from memexpert.schemas.meme import (
    MemeFavoriteMutationRead,
    MemeSlugRedirectRead,
    PublicMemeAnalyticsRead,
    PublicMemeAnalyticsWindow,
    PublicMemeDetailRead,
    PublicMemeLandingRead,
    PublicMemeOfTheDayRead,
    PublicMemePopularitySummaryRead,
    PublicMemeSearchPageRead,
    PublicMemeSearchResultRead,
    PublicMemeSourcePageRead,
    PublicMemeSourceSort,
    PublicMemeTrendPageRead,
    PublicTrendComparisonRead,
    PublicTrendSummaryRead,
    PublicTrendTimelinePageRead,
)
from memexpert.schemas.report import MemeReportCreateRequest, MemeReportRead
from memexpert.schemas.user import UserRead
from memexpert.services import (
    CollectionService,
    CollectionServiceError,
    InvalidPinnedMemeOrderError,
    PinLimitExceededError,
)
from memexpert.services.analytics import InteractionEventWrite
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters, MemeSearchScope
from memexpert.services.public_trends import PublicTrendRanking, PublicTrendTimelineGranularity
from memexpert.services.report import MemeReportTargetNotVisibleError

router = APIRouter(prefix="/memes", tags=["memes"])

SEARCH_SCOPE_DESCRIPTION = (
    "Optional search visibility scope. If omitted, requests use public results for HTTP public API "
    "compatibility. scope=private, scope=all, and scope=collections require a current user."
)
COLLECTION_IDS_DESCRIPTION = (
    "Repeated collection UUIDs for scope=collections only. At least one value is required for "
    "scope=collections; values are deduplicated in request order and every collection must be readable "
    "by the current user before search runs."
)


class ActiveSaveCollectionUpdateRequest(BaseModel):
    """Request body for selecting a writable active save collection."""

    collection_id: uuid.UUID


class PinReorderRequest(BaseModel):
    """Request body containing the complete desired pin order."""

    meme_ids: list[uuid.UUID] = Field(max_length=20)


class MemeReportCreateWithAttributionRequest(MemeReportCreateRequest):
    """Report payload plus optional discovery attribution for telemetry."""

    attribution: MemeInteractionAttributionRequest | None = None


class MemeInteractionRecordedRead(BaseModel):
    """Small acknowledgement for action-only telemetry endpoints."""

    ok: bool = True


@router.get("/search", response_model=PublicMemeSearchPageRead, summary="Search memes")
async def search_memes(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    collection_service: CollectionServiceDep,
    current_user: OptionalCurrentUserDep,
    query: Annotated[str, Query(max_length=500)] = "",
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    scope: Annotated[MemeSearchScope, Query(description=SEARCH_SCOPE_DESCRIPTION)] = MemeSearchScope.PUBLIC,
    collection_ids: Annotated[list[uuid.UUID] | None, Query(description=COLLECTION_IDS_DESCRIPTION)] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeSearchPageRead:
    """Run hybrid indexed search and return DB-backed meme card DTOs.

    Plain text in ``query`` is embedded inside the service boundary before Qdrant search.
    """

    filters = await _validated_search_filters(
        collection_service=collection_service,
        current_user=current_user,
        language=language,
        media_type=media_type,
        scope=scope,
        collection_ids=collection_ids,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        tags=tags,
    )
    normalized_query = query.strip()
    search_started = time.perf_counter()
    page = await meme_search_service.search_public_memes(
        query,
        viewer_user_id=current_user.id if current_user else None,
        filters=filters,
        limit=limit,
        offset=offset,
        surface="public_api_search",
    )
    if normalized_query and offset == 0:
        await analytics_service.record_interaction_event_best_effort(
            InteractionEventWrite(
                event_type=AnalyticsEventType.SEARCH_QUERY,
                user_id=current_user.id if current_user else None,
                surface="public_api_search",
                query=normalized_query,
                request_id=page.request_id,
                properties={
                    "filters": {
                        "language": language.value if language is not None else None,
                        "media_type": media_type.value if media_type is not None else None,
                        "scope": filters.scope.value if filters.scope is not None else None,
                        "collection_ids": _collection_id_strings(filters.collection_ids),
                        "include_nsfw": filters.include_nsfw,
                        "tags": list(filters.tags),
                    },
                    "result_total": page.total,
                    "returned_count": len(page.items),
                    "has_more": page.has_more,
                    "latency_ms": round((time.perf_counter() - search_started) * 1000),
                },
            )
        )
    return page


@router.get("/browse", response_model=PublicMemeSearchPageRead, summary="Browse popular memes")
async def browse_memes(
    meme_search_service: MemeSearchServiceDep,
    collection_service: CollectionServiceDep,
    current_user: OptionalCurrentUserDep,
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    scope: Annotated[MemeSearchScope, Query(description=SEARCH_SCOPE_DESCRIPTION)] = MemeSearchScope.PUBLIC,
    collection_ids: Annotated[list[uuid.UUID] | None, Query(description=COLLECTION_IDS_DESCRIPTION)] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeSearchPageRead:
    """Return a stable popular catalog page with the same filters as search."""

    filters = await _validated_search_filters(
        collection_service=collection_service,
        current_user=current_user,
        language=language,
        media_type=media_type,
        scope=scope,
        collection_ids=collection_ids,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        tags=tags,
    )
    page = await meme_search_service.browse_public_memes(
        viewer_user_id=current_user.id if current_user else None,
        filters=filters,
        limit=limit,
        offset=offset,
        surface="public_api_browse",
    )
    return page


@router.get("/home-feed", response_model=PublicMemeSearchPageRead, summary="Read personalized home feed")
async def home_feed_memes(
    meme_search_service: MemeSearchServiceDep,
    current_user: AutoGuestUserDep,
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeSearchPageRead:
    """Return a public-catalog home feed personalized to the current cookie user."""

    filters = _build_filters(
        language=language,
        media_type=media_type,
        scope=MemeSearchScope.PUBLIC,
        collection_ids=(),
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        tags=tags,
    )
    return await meme_search_service.home_feed_public_memes(
        viewer_user_id=current_user.id,
        filters=filters,
        limit=limit,
        offset=offset,
        surface="public_api_home_feed",
    )


@router.get("/meme-of-the-day", response_model=PublicMemeOfTheDayRead, summary="Read Meme of the Day")
async def get_meme_of_the_day(
    meme_of_the_day_service: MemeOfTheDayServiceDep,
    current_user: OptionalCurrentUserDep,
) -> PublicMemeOfTheDayRead:
    """Return today's public safe Meme of the Day, refreshing the cache on miss."""

    return await meme_of_the_day_service.get_today(
        surface="web_home",
        viewer_user_id=current_user.id if current_user is not None else None,
    )


@router.post(
    "/meme-of-the-day/refresh",
    response_model=PublicMemeOfTheDayRead,
    summary="Refresh Meme of the Day",
)
async def refresh_meme_of_the_day(
    meme_of_the_day_service: MemeOfTheDayServiceDep,
    _admin_user: AdminUserDep,
) -> PublicMemeOfTheDayRead:
    """Force a deterministic MOTD recompute without any manual override."""

    return await meme_of_the_day_service.refresh(surface="web_home")


@router.get("/trending", response_model=PublicMemeSearchPageRead, summary="Browse trending memes")
async def trending_memes(
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    lookback_hours: Annotated[
        int,
        Query(
            ge=1,
            le=24 * 30,
            description=(
                "Deprecated compatibility parameter. Current MV-backed public trending uses the materialized "
                "view windows and ignores this value."
            ),
        ),
    ] = 168,
) -> PublicMemeSearchPageRead:
    """Return memes ranked by materialized public trend projections.

    ``lookback_hours`` is accepted only to avoid breaking existing clients; the
    materialized view owns the ranking windows until versioned APIs are added.
    """

    _ = lookback_hours  # Compatibility-only parameter; MV windows are fixed at refresh time.
    page = await public_trends_service.rank_memes(
        ranking="trending",
        language=language,
        media_type=media_type,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        tags=tuple(tag.strip() for tag in tags or () if tag.strip()),
        limit=limit,
        offset=offset,
        surface="public_api_trending",
    )
    return _trend_page_to_search_page(page)


@router.get("/trends", response_model=PublicMemeTrendPageRead, summary="Browse public trend rankings")
async def trend_rankings(
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    ranking: Annotated[PublicTrendRanking, Query()] = "trending",
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeTrendPageRead:
    """Return aggregate-only public meme trend rankings from materialized views."""

    return await public_trends_service.rank_memes(
        ranking=ranking,
        language=language,
        media_type=media_type,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        tags=tuple(tag.strip() for tag in tags or () if tag.strip()),
        limit=limit,
        offset=offset,
        surface="public_api_trends",
    )


@router.get("/trends/tags", response_model=list[PublicTrendSummaryRead], summary="Browse public tag trends")
async def tag_trend_summaries(
    public_trends_service: PublicTrendsServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PublicTrendSummaryRead]:
    """Return aggregate-only public tag trend summaries from materialized views."""

    return await public_trends_service.tag_summaries(limit=limit, offset=offset)


@router.get("/trends/templates", response_model=list[PublicTrendSummaryRead], summary="Browse public template trends")
async def template_trend_summaries(
    public_trends_service: PublicTrendsServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PublicTrendSummaryRead]:
    """Return aggregate-only public template trend summaries from materialized views."""

    return await public_trends_service.template_summaries(limit=limit, offset=offset)


@router.get("/trends/compare", response_model=PublicTrendComparisonRead, summary="Compare public trend items")
async def compare_public_trends(
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    item: Annotated[
        list[str] | None,
        Query(description="Repeated item specs: meme:<uuid-or-slug>, tag:<slug>, or template:<slug>."),
    ] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicTrendComparisonRead:
    """Return shareable comparison series backed only by real public trend data."""

    return await public_trends_service.compare_items(
        tuple(item or ()),
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
    )


@router.get("/trends/timeline", response_model=PublicTrendTimelinePageRead, summary="Browse public meme timeline")
async def public_trend_timeline(
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    granularity: Annotated[PublicTrendTimelineGranularity, Query()] = "month",
    include_nsfw: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 12,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicTrendTimelinePageRead:
    """Return month/year timeline periods from derived public engagement points."""

    return await public_trends_service.timeline_periods(
        granularity=granularity,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        limit=limit,
        offset=offset,
    )


@router.get("/favorites", response_model=list[CollectionMemeRead], summary="List favorite memes")
async def list_favorites(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
) -> list[CollectionMemeRead]:
    """Return the caller's Favorites saves without requiring frontend guest bootstrap."""

    return await collection_service.list_favorite_memes(user_id=current_user.id)


@router.get("/library", response_model=MemeLibraryRead, summary="Read profile meme library")
async def get_meme_library(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
) -> MemeLibraryRead:
    """Return renderable profile/library data without frontend ID stitching."""

    try:
        return await collection_service.get_meme_library(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


@router.post("/{meme_id}/favorite", response_model=MemeFavoriteMutationRead, summary="Favorite a meme")
async def favorite_meme(
    collection_service: CollectionServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
) -> MemeFavoriteMutationRead:
    """Save a meme to Favorites; the first save increments the meme like count."""

    try:
        mutation = await collection_service.favorite_meme_result(user_id=current_user.id, meme_id=meme_id)
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    if mutation.changed:
        await record_meme_interaction(
            analytics_service,
            AnalyticsEventType.MEME_LIKE,
            meme_id=meme_id,
            current_user=current_user,
            attribution=payload_attribution(payload),
            default_surface="public_api_meme_action",
            collection_id=mutation.item.collection_id,
            properties={"action": "favorite"},
        )
    favorite_state = await collection_service.get_meme_favorite_state(
        user_id=current_user.id,
        meme_id=meme_id,
    )
    return MemeFavoriteMutationRead(
        favorited=favorite_state.favorited,
        changed=mutation.changed,
        like_count=favorite_state.like_count,
    )


@router.delete("/{meme_id}/favorite", response_model=MemeFavoriteMutationRead, summary="Unfavorite a meme")
async def unfavorite_meme(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> MemeFavoriteMutationRead:
    """Remove a meme from Favorites when present."""

    try:
        removed = await collection_service.unfavorite_meme(user_id=current_user.id, meme_id=meme_id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    favorite_state = await collection_service.get_meme_favorite_state(
        user_id=current_user.id,
        meme_id=meme_id,
    )
    return MemeFavoriteMutationRead(
        favorited=favorite_state.favorited,
        changed=removed,
        like_count=favorite_state.like_count,
    )


@router.post("/{meme_id}/save", response_model=CollectionMemeRead, summary="Save a meme")
async def save_meme_to_active_collection(
    collection_service: CollectionServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
) -> CollectionMemeRead:
    """Save a meme into the caller's active collection, defaulting guests to Favorites."""

    try:
        mutation = await collection_service.save_meme_to_active_collection_result(
            user_id=current_user.id,
            meme_id=meme_id,
        )
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    if mutation.changed:
        await record_meme_interaction(
            analytics_service,
            AnalyticsEventType.MEME_SAVE,
            meme_id=meme_id,
            current_user=current_user,
            attribution=payload_attribution(payload),
            default_surface="public_api_meme_action",
            collection_id=mutation.item.collection_id,
            properties={"action": "save"},
        )
    return mutation.item


@router.delete("/{meme_id}/save", response_model=dict[str, bool], summary="Remove a saved meme")
async def remove_meme_from_active_collection(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> dict[str, bool]:
    """Remove a meme from the active save collection when present."""

    try:
        removed = await collection_service.remove_meme_from_active_collection(user_id=current_user.id, meme_id=meme_id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return {"removed": removed}


@router.get("/active-save-collection", response_model=CollectionRead, summary="Read active save collection")
async def get_active_save_collection(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
) -> CollectionRead:
    """Return the current save destination, lazily creating Favorites when needed."""

    try:
        return await collection_service.get_active_save_collection(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


@router.put("/active-save-collection", response_model=UserRead, summary="Update active save collection")
async def update_active_save_collection(
    collection_service: CollectionServiceDep,
    current_user: CurrentUserDep,
    payload: ActiveSaveCollectionUpdateRequest,
) -> UserRead:
    """Point the caller's save action at a writable collection."""

    try:
        return await collection_service.update_active_save_collection(
            user_id=current_user.id,
            collection_id=payload.collection_id,
        )
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


@router.get("/pins", response_model=list[PinnedMemeRead], summary="List pinned memes")
async def list_pins(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
) -> list[PinnedMemeRead]:
    """Return full-account pins in display order."""

    return await collection_service.list_pinned_memes(user_id=current_user.id)


@router.post("/{meme_id}/pin", response_model=PinnedMemeRead, summary="Pin a meme")
async def pin_meme(
    collection_service: CollectionServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: FullAccountUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
) -> PinnedMemeRead:
    """Append a meme to the caller's full-account pins."""

    try:
        mutation = await collection_service.pin_meme_result(user_id=current_user.id, meme_id=meme_id)
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    if mutation.changed:
        await record_meme_interaction(
            analytics_service,
            AnalyticsEventType.MEME_PIN,
            meme_id=meme_id,
            current_user=current_user,
            attribution=payload_attribution(payload),
            default_surface="public_api_meme_action",
            properties={"action": "pin", "position": mutation.item.position},
        )
    return mutation.item


@router.delete("/{meme_id}/pin", response_model=dict[str, bool], summary="Unpin a meme")
async def unpin_meme(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> dict[str, bool]:
    """Remove a pin and compact the remaining order."""

    try:
        removed = await collection_service.unpin_meme(user_id=current_user.id, meme_id=meme_id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return {"removed": removed}


@router.put("/pins/reorder", response_model=list[PinnedMemeRead], summary="Reorder pinned memes")
async def reorder_pins(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    payload: PinReorderRequest,
) -> list[PinnedMemeRead]:
    """Replace the full pin order with the supplied ordered meme IDs."""

    try:
        return await collection_service.reorder_pins(user_id=current_user.id, meme_ids=payload.meme_ids)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


@router.post("/{meme_id}/report", response_model=MemeReportRead, summary="Report a meme")
async def report_meme(
    report_service: MemeReportServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: FullAccountUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: MemeReportCreateWithAttributionRequest,
) -> MemeReportRead:
    """Create or reuse the caller's open moderation report for a visible meme."""

    try:
        report = await report_service.report_meme(
            meme_id,
            reporter_user_id=current_user.id,
            reporter_nsfw_enabled=current_user.nsfw_enabled,
            request=payload,
        )
    except MemeReportTargetNotVisibleError as exc:
        raise _meme_not_found_http_error() from exc
    await record_meme_interaction(
        analytics_service,
        AnalyticsEventType.MEME_REPORT,
        meme_id=meme_id,
        current_user=current_user,
        attribution=payload.attribution,
        default_surface="public_api_meme_action",
        report_id=report.id,
        properties={"action": "report", "reason": report.reason.value, "status": report.status.value},
    )
    return report


@router.post("/{meme_id}/share", response_model=MemeInteractionRecordedRead, summary="Record meme share intent")
async def share_meme(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeInteractionRecordedRead:
    """Record a visible meme share action without serving media or redirecting."""

    await _ensure_visible_meme_for_interaction(
        meme_search_service,
        meme_id=meme_id,
        current_user=current_user,
        include_nsfw=include_nsfw,
    )
    await record_meme_interaction(
        analytics_service,
        AnalyticsEventType.MEME_SHARE,
        meme_id=meme_id,
        current_user=current_user,
        attribution=payload_attribution(payload),
        default_surface="public_api_meme_action",
        properties={
            "action": "share",
            "channel": "telegram",
            "include_nsfw": _nsfw_allowed(current_user, include_nsfw),
        },
    )
    return MemeInteractionRecordedRead()


@router.post("/{meme_id}/impression", response_model=MemeInteractionRecordedRead, summary="Record meme card impression")
async def record_meme_impression(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeInteractionRecordedRead:
    """Record a visible list-card impression without changing meme state."""

    await _ensure_visible_meme_for_interaction(
        meme_search_service,
        meme_id=meme_id,
        current_user=current_user,
        include_nsfw=include_nsfw,
    )
    await record_meme_interaction(
        analytics_service,
        AnalyticsEventType.MEME_IMPRESSION,
        meme_id=meme_id,
        current_user=current_user,
        attribution=payload_attribution(payload),
        default_surface="public_api_meme_card",
        properties={"action": "impression", "include_nsfw": _nsfw_allowed(current_user, include_nsfw)},
    )
    return MemeInteractionRecordedRead()


@router.post("/{meme_id}/view", response_model=MemeInteractionRecordedRead, summary="Record meme detail view")
async def record_meme_view(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeInteractionRecordedRead:
    """Record one visible detail-page visit independently of detail reads."""

    await _ensure_visible_meme_for_interaction(
        meme_search_service,
        meme_id=meme_id,
        current_user=current_user,
        include_nsfw=include_nsfw,
    )
    await record_meme_interaction(
        analytics_service,
        AnalyticsEventType.MEME_VIEW,
        meme_id=meme_id,
        current_user=current_user,
        attribution=payload_attribution(payload),
        default_surface="public_api_meme_detail",
        properties={"include_nsfw": _nsfw_allowed(current_user, include_nsfw)},
    )
    return MemeInteractionRecordedRead()


@router.post(
    "/{meme_id}/detail-click",
    response_model=MemeInteractionRecordedRead,
    summary="Record meme card detail click",
)
async def record_meme_detail_click(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeInteractionRecordedRead:
    """Record a visible list-card click before the caller navigates to detail."""

    await _ensure_visible_meme_for_interaction(
        meme_search_service,
        meme_id=meme_id,
        current_user=current_user,
        include_nsfw=include_nsfw,
    )
    await record_meme_interaction(
        analytics_service,
        AnalyticsEventType.MEME_DETAIL_CLICK,
        meme_id=meme_id,
        current_user=current_user,
        attribution=payload_attribution(payload),
        default_surface="public_api_meme_card",
        properties={"action": "detail_click", "include_nsfw": _nsfw_allowed(current_user, include_nsfw)},
    )
    return MemeInteractionRecordedRead()


@router.post("/{meme_id}/download", response_model=MemeInteractionRecordedRead, summary="Record meme download intent")
async def download_meme(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[MemeActionAttributionRequest | None, Body()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeInteractionRecordedRead:
    """Record a visible meme download action without serving or signing media."""

    await _ensure_visible_meme_for_interaction(
        meme_search_service,
        meme_id=meme_id,
        current_user=current_user,
        include_nsfw=include_nsfw,
    )
    await record_meme_interaction(
        analytics_service,
        AnalyticsEventType.MEME_DOWNLOAD,
        meme_id=meme_id,
        current_user=current_user,
        attribution=payload_attribution(payload),
        default_surface="public_api_meme_action",
        properties={"action": "download", "include_nsfw": _nsfw_allowed(current_user, include_nsfw)},
    )
    return MemeInteractionRecordedRead()


@router.get("/slug/{slug}", response_model=PublicMemeDetailRead, summary="Read meme details by SEO slug")
async def get_meme_detail_by_slug(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    slug: Annotated[str, Path(min_length=1, max_length=255)],
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemeDetailRead:
    """Resolve a visible meme detail DTO from its canonical SEO slug."""

    try:
        detail = await meme_search_service.get_public_meme_detail_by_slug(
            slug,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        )
    except MemeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme was not found.",
        ) from exc
    return detail


@router.get("/tags/{tag_slug}", response_model=PublicMemeLandingRead, summary="Browse memes by tag")
async def browse_tag_landing(
    meme_search_service: MemeSearchServiceDep,
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    tag_slug: Annotated[str, Path(min_length=1, max_length=64)],
    include_nsfw: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeLandingRead:
    """Return a minimal public organic landing contract for one tag."""

    normalized_tag = tag_slug.strip().lower()
    page = await meme_search_service.browse_public_tag(
        normalized_tag,
        viewer_user_id=current_user.id if current_user else None,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        limit=limit,
        offset=offset,
        surface="public_api_tag_landing",
    )
    return PublicMemeLandingRead(
        kind="tag",
        slug=normalized_tag,
        title=f"{normalized_tag.replace('-', ' ').title()} memes",
        description=f"Browse public memes tagged {normalized_tag}.",
        page=page,
        trend_summary=await public_trends_service.tag_summary(normalized_tag),
    )


@router.get("/templates/{template_slug}", response_model=PublicMemeLandingRead, summary="Browse memes by template")
async def browse_template_landing(
    meme_search_service: MemeSearchServiceDep,
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    template_slug: Annotated[str, Path(min_length=1, max_length=255)],
    include_nsfw: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeLandingRead:
    """Return a minimal public organic landing contract for one meme template."""

    template, page = await meme_search_service.browse_public_template(
        template_slug,
        viewer_user_id=current_user.id if current_user else None,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        limit=limit,
        offset=offset,
        surface="public_api_template_landing",
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme template was not found.",
        )
    return PublicMemeLandingRead(
        kind="template",
        slug=template.slug,
        title=f"{template.name} memes",
        description=template.description,
        page=page,
        trend_summary=await public_trends_service.template_summary(template.slug),
    )


@router.get("/{meme_id}/canonical", response_model=MemeSlugRedirectRead, summary="Read canonical meme slug metadata")
async def get_meme_canonical_slug(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeSlugRedirectRead:
    """Return id-to-slug redirect metadata when SEO has been generated."""

    try:
        return await meme_search_service.get_slug_redirect(
            meme_id,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        )
    except MemeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme was not found.",
        ) from exc


@router.get(
    "/{meme_id}/sources",
    response_model=PublicMemeSourcePageRead,
    summary="Read public meme source posts",
)
async def get_meme_sources(
    public_meme_insights_service: PublicMemeInsightsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    sort: Annotated[PublicMemeSourceSort, Query()] = PublicMemeSourceSort.VIEWS_DESC,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    snapshot_at: Annotated[
        datetime | None,
        Query(description="API-issued RFC3339 cutoff to keep metric sorting and pagination stable."),
    ] = None,
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemeSourcePageRead:
    """Return public-crawler Telegram attribution across every file of a public meme."""

    page = await public_meme_insights_service.source_page(
        meme_id,
        sort=sort,
        limit=limit,
        offset=offset,
        snapshot_at=snapshot_at,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
    )
    if page is None:
        raise _meme_not_found_http_error()
    return page


@router.get(
    "/{meme_id}/analytics",
    response_model=PublicMemeAnalyticsRead,
    summary="Read public meme professional analytics",
)
async def get_meme_analytics(
    public_meme_insights_service: PublicMemeInsightsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    window: Annotated[PublicMemeAnalyticsWindow, Query()] = PublicMemeAnalyticsWindow.THIRTY_DAYS,
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemeAnalyticsRead:
    """Return source activity, observed counters, audience coverage, and separate exposure funnels."""

    analytics = await public_meme_insights_service.analytics(
        meme_id,
        window=window,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
    )
    if analytics is None:
        raise _meme_not_found_http_error()
    return analytics


@router.get(
    "/{meme_id}/popularity",
    response_model=PublicMemePopularitySummaryRead,
    summary="Read public meme popularity summary",
)
async def get_meme_popularity_summary(
    public_trends_service: PublicTrendsServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemePopularitySummaryRead:
    """Return aggregate public trend metrics plus real snapshot sparkline points."""

    summary = await public_trends_service.meme_popularity_summary(
        meme_id,
        include_nsfw=_nsfw_allowed(current_user, include_nsfw),
    )
    if summary is None:
        raise _meme_not_found_http_error()
    return summary


@router.get("/{meme_id}/similar", response_model=PublicMemeSearchPageRead, summary="Browse similar memes")
async def get_similar_memes(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    include_nsfw: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeSearchPageRead:
    """Return Qdrant-led similar public memes with explicit fallback attribution."""

    try:
        return await meme_search_service.get_public_similar_memes(
            meme_id,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
            limit=limit,
            offset=offset,
            surface="public_api_meme_similar",
        )
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc


@router.get("/{meme_id}", response_model=PublicMemeDetailRead, summary="Read meme details")
async def get_meme_detail(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemeDetailRead:
    """Return a detail DTO for a visible meme."""

    try:
        detail = await meme_search_service.get_public_meme_detail(
            meme_id,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        )
    except MemeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme was not found.",
        ) from exc
    return detail


async def _ensure_visible_meme_for_interaction(
    meme_search_service,
    *,
    meme_id: uuid.UUID,
    current_user: UserRead | None,
    include_nsfw: bool,
) -> None:
    try:
        await meme_search_service.get_meme_detail(
            meme_id,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        )
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc


async def _validated_search_filters(
    *,
    collection_service: CollectionService,
    current_user: UserRead | None,
    language: ContentLanguage | None,
    media_type: ContentKind | None,
    scope: MemeSearchScope,
    collection_ids: list[uuid.UUID] | None,
    include_nsfw: bool,
    tags: list[str] | None,
) -> MemeSearchFilters:
    normalized_collection_ids = _normalized_collection_ids(collection_ids)

    if scope is not MemeSearchScope.COLLECTIONS and normalized_collection_ids:
        raise _invalid_search_scope_http_error("collection_ids are only valid when scope=collections.")
    if scope is not MemeSearchScope.PUBLIC and current_user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authentication is required for private, all, or collections meme search scopes.",
        )
    if scope is MemeSearchScope.COLLECTIONS:
        if not normalized_collection_ids:
            raise _invalid_search_scope_http_error("scope=collections requires at least one collection_ids value.")
        assert current_user is not None
        for collection_id in normalized_collection_ids:
            try:
                await collection_service.get_collection_for_user(collection_id=collection_id, user_id=current_user.id)
            except CollectionServiceError as exc:
                raise _collection_http_error(exc) from exc

    return _build_filters(
        language=language,
        media_type=media_type,
        scope=scope,
        collection_ids=normalized_collection_ids,
        include_nsfw=include_nsfw,
        tags=tags,
    )


def _build_filters(
    *,
    language: ContentLanguage | None,
    media_type: ContentKind | None,
    scope: MemeSearchScope,
    collection_ids: tuple[uuid.UUID, ...],
    include_nsfw: bool,
    tags: list[str] | None,
) -> MemeSearchFilters:
    return MemeSearchFilters(
        language=language,
        media_type=media_type,
        scope=scope,
        collection_ids=collection_ids,
        include_nsfw=include_nsfw,
        tags=tuple(tag.strip() for tag in tags or () if tag.strip()),
    )


def _nsfw_allowed(current_user: UserRead | None, include_nsfw: bool) -> bool:
    return include_nsfw and bool(current_user and current_user.nsfw_enabled)


def _normalized_collection_ids(collection_ids: list[uuid.UUID] | None) -> tuple[uuid.UUID, ...]:
    return tuple(dict.fromkeys(collection_ids or []))


def _collection_id_strings(collection_ids: tuple[uuid.UUID, ...]) -> list[str]:
    return [str(collection_id) for collection_id in collection_ids]


def _invalid_search_scope_http_error(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _trend_page_to_search_page(page: PublicMemeTrendPageRead) -> PublicMemeSearchPageRead:
    return PublicMemeSearchPageRead(
        items=[PublicMemeSearchResultRead(meme=item.meme, attribution=item.attribution) for item in page.items],
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
        request_id=page.request_id,
    )


def _meme_not_found_http_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Meme was not found.",
    )


def _collection_http_error(exc: CollectionServiceError) -> HTTPException:
    return collection_service_http_error(
        exc,
        conflict_errors=(InvalidPinnedMemeOrderError, PinLimitExceededError),
    )


__all__ = ["router"]
