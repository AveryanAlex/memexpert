# ruff: noqa: TC001
"""Public SEO catalog routes for frontend-owned XML/feed generation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from memexpert.api.dependencies import SeoCatalogServiceDep
from memexpert.schemas.seo import (
    SeoCatalogMemePageRead,
    SeoCatalogSummaryRead,
    SeoCatalogTagPageRead,
    SeoCatalogTemplatePageRead,
)
from memexpert.services.seo_catalog import (
    DEFAULT_CATALOG_LIMIT,
    DEFAULT_PINTEREST_FEED_LIMIT,
    MAX_CATALOG_LIMIT,
    MAX_PINTEREST_FEED_LIMIT,
)

router = APIRouter(prefix="/seo", tags=["seo"])


@router.get("/summary", response_model=SeoCatalogSummaryRead, summary="Read SEO catalog summary")
async def get_seo_catalog_summary(seo_catalog_service: SeoCatalogServiceDep) -> SeoCatalogSummaryRead:
    """Return DB-only public safe counts and freshness for SEO planning."""

    return await seo_catalog_service.get_summary()


@router.get("/memes", response_model=SeoCatalogMemePageRead, summary="List public safe SEO memes")
async def list_seo_catalog_memes(
    seo_catalog_service: SeoCatalogServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_CATALOG_LIMIT)] = DEFAULT_CATALOG_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SeoCatalogMemePageRead:
    """Return public safe meme catalog rows without search or analytics side effects."""

    return await seo_catalog_service.list_memes(limit=limit, offset=offset)


@router.get("/tags", response_model=SeoCatalogTagPageRead, summary="List public safe SEO tag landings")
async def list_seo_catalog_tags(
    seo_catalog_service: SeoCatalogServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_CATALOG_LIMIT)] = DEFAULT_CATALOG_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SeoCatalogTagPageRead:
    """Return tag landing records derived from public safe meme tags."""

    return await seo_catalog_service.list_tags(limit=limit, offset=offset)


@router.get("/templates", response_model=SeoCatalogTemplatePageRead, summary="List public safe SEO template landings")
async def list_seo_catalog_templates(
    seo_catalog_service: SeoCatalogServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_CATALOG_LIMIT)] = DEFAULT_CATALOG_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SeoCatalogTemplatePageRead:
    """Return templates that have at least one public safe meme."""

    return await seo_catalog_service.list_templates(limit=limit, offset=offset)


@router.get("/pinterest-feed", response_model=SeoCatalogMemePageRead, summary="List Pinterest feed memes")
async def list_seo_pinterest_feed(
    seo_catalog_service: SeoCatalogServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PINTEREST_FEED_LIMIT)] = DEFAULT_PINTEREST_FEED_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SeoCatalogMemePageRead:
    """Return a deterministic feed-friendly page of recent/popular public safe memes."""

    return await seo_catalog_service.list_pinterest_feed(limit=limit, offset=offset)


__all__ = ["router"]
