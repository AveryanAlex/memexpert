# ruff: noqa: TC001,TC003
"""Reusable meme search/read routes backed by the shared service layer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from memexpert.api.dependencies import (
    AnalyticsServiceDep,
    AutoGuestUserDep,
    CollectionServiceDep,
    CurrentUserDep,
    FullAccountUserDep,
    MemeReportServiceDep,
    MemeSearchServiceDep,
    OptionalCurrentUserDep,
    PublicTrendsServiceDep,
)
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage
from memexpert.schemas.collection import CollectionMemeRead, CollectionRead, MemeLibraryRead, PinnedMemeRead
from memexpert.schemas.meme import (
    MemeSlugRedirectRead,
    PublicMemeDetailRead,
    PublicMemeLandingRead,
    PublicMemePopularitySummaryRead,
    PublicMemeSearchPageRead,
    PublicMemeSearchResultRead,
    PublicMemeTrendPageRead,
    PublicTrendSummaryRead,
)
from memexpert.schemas.report import MemeReportCreateRequest, MemeReportRead
from memexpert.schemas.user import UserRead
from memexpert.services import (
    CollectionNotFoundError,
    CollectionServiceError,
    CollectionWriteAccessError,
    GuestCollectionAccessError,
    InvalidPinnedMemeOrderError,
    PinLimitExceededError,
    UserNotFoundError,
)
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters
from memexpert.services.public_trends import PublicTrendRanking
from memexpert.services.report import MemeReportTargetNotVisibleError

router = APIRouter(prefix="/memes", tags=["memes"])


class ActiveSaveCollectionUpdateRequest(BaseModel):
    """Request body for selecting a writable active save collection."""

    collection_id: uuid.UUID


class PinReorderRequest(BaseModel):
    """Request body containing the complete desired pin order."""

    meme_ids: list[uuid.UUID] = Field(max_length=20)


@router.get("/search", response_model=PublicMemeSearchPageRead, summary="Search memes")
async def search_memes(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
    current_user: OptionalCurrentUserDep,
    query: Annotated[str, Query(max_length=500)] = "",
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeSearchPageRead:
    """Run hybrid indexed search and return DB-backed meme card DTOs.

    Plain text in ``query`` is embedded inside the service boundary before Qdrant search.
    """

    page = await meme_search_service.search_public_memes(
        query,
        viewer_user_id=current_user.id if current_user else None,
        filters=_build_filters(
            language=language,
            media_type=media_type,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
            tags=tags,
        ),
        limit=limit,
        offset=offset,
    )
    await analytics_service.record_event(
        AnalyticsEventType.SEARCH_QUERY,
        user_id=current_user.id if current_user else None,
        payload={
            "surface": "public_api",
            "query": query.strip(),
            "language": language.value if language is not None else None,
            "media_type": media_type.value if media_type is not None else None,
            "include_nsfw": _nsfw_allowed(current_user, include_nsfw),
            "tags": [tag.strip() for tag in tags or [] if tag.strip()],
            "limit": limit,
            "offset": offset,
            "result_count": len(page.items),
            "has_more": page.has_more,
        },
    )
    return page


@router.get("/browse", response_model=PublicMemeSearchPageRead, summary="Browse popular memes")
async def browse_memes(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeSearchPageRead:
    """Return a stable popular catalog page with the same filters as search."""

    page = await meme_search_service.browse_public_memes(
        viewer_user_id=current_user.id if current_user else None,
        filters=_build_filters(
            language=language,
            media_type=media_type,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
            tags=tags,
        ),
        limit=limit,
        offset=offset,
    )
    return page


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


@router.post("/{meme_id}/favorite", response_model=CollectionMemeRead, summary="Favorite a meme")
async def favorite_meme(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> CollectionMemeRead:
    """Save a meme to Favorites; the first save increments the meme like count."""

    try:
        return await collection_service.favorite_meme(user_id=current_user.id, meme_id=meme_id)
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


@router.delete("/{meme_id}/favorite", response_model=dict[str, bool], summary="Unfavorite a meme")
async def unfavorite_meme(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> dict[str, bool]:
    """Remove a meme from Favorites when present."""

    try:
        removed = await collection_service.unfavorite_meme(user_id=current_user.id, meme_id=meme_id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return {"removed": removed}


@router.post("/{meme_id}/save", response_model=CollectionMemeRead, summary="Save a meme")
async def save_meme_to_active_collection(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> CollectionMemeRead:
    """Save a meme into the caller's active collection, defaulting guests to Favorites."""

    try:
        return await collection_service.save_meme_to_active_collection(user_id=current_user.id, meme_id=meme_id)
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


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
    current_user: FullAccountUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> PinnedMemeRead:
    """Append a meme to the caller's full-account pins."""

    try:
        return await collection_service.pin_meme(user_id=current_user.id, meme_id=meme_id)
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc


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
    current_user: FullAccountUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    payload: MemeReportCreateRequest,
) -> MemeReportRead:
    """Create or reuse the caller's open moderation report for a visible meme."""

    try:
        return await report_service.report_meme(
            meme_id,
            reporter_user_id=current_user.id,
            reporter_nsfw_enabled=current_user.nsfw_enabled,
            request=payload,
        )
    except MemeReportTargetNotVisibleError as exc:
        raise _meme_not_found_http_error() from exc


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


@router.get("/{meme_id}", response_model=PublicMemeDetailRead, summary="Read meme details")
async def get_meme_detail(
    meme_search_service: MemeSearchServiceDep,
    analytics_service: AnalyticsServiceDep,
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
    await analytics_service.record_event(
        AnalyticsEventType.MEME_VIEW,
        user_id=current_user.id if current_user else None,
        payload={
            "surface": "public_api",
            "meme_id": str(detail.id),
            "include_nsfw": _nsfw_allowed(current_user, include_nsfw),
        },
    )
    return detail


def _build_filters(
    *,
    language: ContentLanguage | None,
    media_type: ContentKind | None,
    include_nsfw: bool,
    tags: list[str] | None,
) -> MemeSearchFilters:
    return MemeSearchFilters(
        language=language,
        media_type=media_type,
        include_nsfw=include_nsfw,
        tags=tuple(tag.strip() for tag in tags or () if tag.strip()),
    )


def _nsfw_allowed(current_user: UserRead | None, include_nsfw: bool) -> bool:
    return include_nsfw and bool(current_user and current_user.nsfw_enabled)


def _trend_page_to_search_page(page: PublicMemeTrendPageRead) -> PublicMemeSearchPageRead:
    return PublicMemeSearchPageRead(
        items=[PublicMemeSearchResultRead(meme=item.meme) for item in page.items],
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
    )


def _meme_not_found_http_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Meme was not found.",
    )


def _collection_http_error(exc: CollectionServiceError) -> HTTPException:
    if isinstance(exc, (CollectionNotFoundError, UserNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (CollectionWriteAccessError, GuestCollectionAccessError)):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, (InvalidPinnedMemeOrderError, PinLimitExceededError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


__all__ = ["router"]
