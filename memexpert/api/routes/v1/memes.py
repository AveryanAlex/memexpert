# ruff: noqa: TC001,TC003
"""Reusable meme search/read routes backed by the shared service layer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from memexpert.api.dependencies import MemeSearchServiceDep, OptionalCurrentUserDep
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import MemeDetailRead, MemeSearchPageRead
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchFilters

router = APIRouter(prefix="/memes", tags=["memes"])


@router.get("/search", response_model=MemeSearchPageRead, summary="Search memes")
async def search_memes(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    query: Annotated[str, Query(max_length=500)] = "",
    language: Annotated[ContentLanguage | None, Query()] = None,
    media_type: Annotated[ContentKind | None, Query()] = None,
    include_nsfw: Annotated[bool, Query()] = False,
    tags: Annotated[list[str] | None, Query()] = None,
    query_vector: Annotated[list[float] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemeSearchPageRead:
    """Run hybrid indexed search and return DB-backed meme card DTOs."""

    nsfw_allowed = include_nsfw and bool(current_user and current_user.nsfw_enabled)
    filters = MemeSearchFilters(
        language=language,
        media_type=media_type,
        include_nsfw=nsfw_allowed,
        tags=tuple(tag for tag in tags or () if tag.strip()),
    )
    return await meme_search_service.search_memes(
        query,
        viewer_user_id=current_user.id if current_user else None,
        query_vector=tuple(query_vector) if query_vector else None,
        filters=filters,
        limit=limit,
        offset=offset,
    )


@router.get("/{meme_id}", response_model=MemeDetailRead, summary="Read meme details")
async def get_meme_detail(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    meme_id: Annotated[uuid.UUID, Path()],
    include_nsfw: Annotated[bool, Query()] = False,
) -> MemeDetailRead:
    """Return a detail DTO for a visible meme."""

    try:
        return await meme_search_service.get_meme_detail(
            meme_id,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=include_nsfw and bool(current_user and current_user.nsfw_enabled),
        )
    except MemeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme was not found.",
        ) from exc


__all__ = ["router"]
