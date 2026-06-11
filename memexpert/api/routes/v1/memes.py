# ruff: noqa: TC001,TC003
"""Reusable meme search/read routes backed by the shared service layer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from memexpert.api.dependencies import (
    AutoGuestUserDep,
    CollectionServiceDep,
    CurrentUserDep,
    FullAccountUserDep,
    MemeSearchServiceDep,
    OptionalCurrentUserDep,
)
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.collection import CollectionMemeRead, CollectionRead, PinnedMemeRead
from memexpert.schemas.meme import PublicMemeDetailRead, PublicMemeSearchPageRead
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

    page = await meme_search_service.search_memes(
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
    return PublicMemeSearchPageRead.model_validate(page.model_dump())


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

    page = await meme_search_service.browse_memes(
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
    return PublicMemeSearchPageRead.model_validate(page.model_dump())


@router.get("/favorites", response_model=list[CollectionMemeRead], summary="List favorite memes")
async def list_favorites(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
) -> list[CollectionMemeRead]:
    """Return the caller's Favorites saves without requiring frontend guest bootstrap."""

    return await collection_service.list_favorite_memes(user_id=current_user.id)


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


@router.get("/{meme_id}", response_model=PublicMemeDetailRead, summary="Read meme details")
async def get_meme_detail(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemeDetailRead:
    """Return a detail DTO for a visible meme."""

    try:
        detail = await meme_search_service.get_meme_detail(
            meme_id,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        )
    except MemeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme was not found.",
        ) from exc
    return PublicMemeDetailRead.model_validate(detail.model_dump())


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
