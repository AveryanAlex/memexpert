# ruff: noqa: TC001,TC003
"""Reusable meme search/read routes backed by the shared service layer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from memexpert.api.dependencies import MemeSearchServiceDep, OptionalCurrentUserDep
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import PublicMemeDetailRead, PublicMemeSearchPageRead
from memexpert.schemas.user import UserRead
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters

router = APIRouter(prefix="/memes", tags=["memes"])


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


__all__ = ["router"]
