# ruff: noqa: TC001,TC003
"""Reusable meme search/read routes backed by the shared service layer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from memexpert.api.dependencies import MemeSearchServiceDep, OptionalCurrentUserDep
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import (
    MemeSlugRedirectRead,
    PublicMemeDetailRead,
    PublicMemeLandingRead,
    PublicMemeSearchPageRead,
)
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


@router.get("/slug/{slug}", response_model=PublicMemeDetailRead, summary="Read meme details by SEO slug")
async def get_meme_detail_by_slug(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    slug: Annotated[str, Path(min_length=1, max_length=255)],
    include_nsfw: Annotated[bool, Query()] = False,
) -> PublicMemeDetailRead:
    """Resolve a visible meme detail DTO from its canonical SEO slug."""

    try:
        detail = await meme_search_service.get_meme_detail_by_slug(
            slug,
            viewer_user_id=current_user.id if current_user else None,
            include_nsfw=_nsfw_allowed(current_user, include_nsfw),
        )
    except MemeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meme was not found.",
        ) from exc
    return PublicMemeDetailRead.model_validate(detail.model_dump())


@router.get("/tags/{tag_slug}", response_model=PublicMemeLandingRead, summary="Browse memes by tag")
async def browse_tag_landing(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    tag_slug: Annotated[str, Path(min_length=1, max_length=64)],
    include_nsfw: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeLandingRead:
    """Return a minimal public organic landing contract for one tag."""

    normalized_tag = tag_slug.strip().lower()
    page = await meme_search_service.browse_tag(
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
        page=PublicMemeSearchPageRead.model_validate(page.model_dump()),
    )


@router.get("/templates/{template_slug}", response_model=PublicMemeLandingRead, summary="Browse memes by template")
async def browse_template_landing(
    meme_search_service: MemeSearchServiceDep,
    current_user: OptionalCurrentUserDep,
    template_slug: Annotated[str, Path(min_length=1, max_length=255)],
    include_nsfw: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicMemeLandingRead:
    """Return a minimal public organic landing contract for one meme template."""

    template, page = await meme_search_service.browse_template(
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
        page=PublicMemeSearchPageRead.model_validate(page.model_dump()),
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
