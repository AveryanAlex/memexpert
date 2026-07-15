"""Read-only, aggregate-only analytics endpoints for browser admins."""
# ruff: noqa: TC001,TC003

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from memexpert.api.dependencies import AdminUserDep, DbSessionDep
from memexpert.schemas.admin_analytics import (
    AdminAnalyticsAudienceRead,
    AdminAnalyticsContentRead,
    AdminAnalyticsEngagementRead,
    AdminAnalyticsOverviewRead,
    AdminAnalyticsSearchQueryDetailRead,
    AdminAnalyticsSearchQueryPageRead,
)
from memexpert.services.admin_analytics import (
    QUERY_PAGE_LIMIT,
    AdminAnalyticsDateRange,
    AdminAnalyticsDateRangeError,
    AdminAnalyticsEventVolumeError,
    AdminAnalyticsQueryKeyNotFoundError,
    AdminAnalyticsService,
    resolve_admin_analytics_date_range,
)

router = APIRouter(prefix="/admin/analytics", tags=["admin analytics"])


def get_admin_analytics_service(session: DbSessionDep) -> AdminAnalyticsService:
    """Build the read-only analytics service for one request session."""

    return AdminAnalyticsService(session)


def get_admin_analytics_date_range(
    start_date: Annotated[date | None, Query(description="Inclusive UTC start date (YYYY-MM-DD).")] = None,
    end_date: Annotated[date | None, Query(description="Inclusive UTC end date (YYYY-MM-DD).")] = None,
) -> AdminAnalyticsDateRange:
    """Resolve the shared date controls used by every analytics screen."""

    try:
        return resolve_admin_analytics_date_range(start_date=start_date, end_date=end_date)
    except AdminAnalyticsDateRangeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


AdminAnalyticsServiceDep = Annotated[AdminAnalyticsService, Depends(get_admin_analytics_service)]
AdminAnalyticsDateRangeDep = Annotated[AdminAnalyticsDateRange, Depends(get_admin_analytics_date_range)]


async def get_bounded_admin_analytics_date_range(
    _admin: AdminUserDep,
    session: DbSessionDep,
    date_range: AdminAnalyticsDateRangeDep,
) -> AdminAnalyticsDateRange:
    """Guard raw-event dashboards before they materialize an unsafe range."""

    try:
        await AdminAnalyticsService(session).ensure_event_volume(date_range)
    except AdminAnalyticsEventVolumeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return date_range


BoundedAdminAnalyticsDateRangeDep = Annotated[
    AdminAnalyticsDateRange,
    Depends(get_bounded_admin_analytics_date_range),
]


@router.get("/overview", response_model=AdminAnalyticsOverviewRead, summary="Read admin analytics overview")
async def get_admin_analytics_overview(
    _admin: AdminUserDep,
    analytics_service: AdminAnalyticsServiceDep,
    date_range: BoundedAdminAnalyticsDateRangeDep,
) -> AdminAnalyticsOverviewRead:
    return await analytics_service.get_overview(date_range)


@router.get("/engagement", response_model=AdminAnalyticsEngagementRead, summary="Read admin engagement analytics")
async def get_admin_analytics_engagement(
    _admin: AdminUserDep,
    analytics_service: AdminAnalyticsServiceDep,
    date_range: BoundedAdminAnalyticsDateRangeDep,
) -> AdminAnalyticsEngagementRead:
    return await analytics_service.get_engagement(date_range)


@router.get("/audience", response_model=AdminAnalyticsAudienceRead, summary="Read admin audience analytics")
async def get_admin_analytics_audience(
    _admin: AdminUserDep,
    analytics_service: AdminAnalyticsServiceDep,
    date_range: BoundedAdminAnalyticsDateRangeDep,
) -> AdminAnalyticsAudienceRead:
    return await analytics_service.get_audience(date_range)


@router.get("/content", response_model=AdminAnalyticsContentRead, summary="Read admin content and source analytics")
async def get_admin_analytics_content(
    _admin: AdminUserDep,
    analytics_service: AdminAnalyticsServiceDep,
    date_range: AdminAnalyticsDateRangeDep,
) -> AdminAnalyticsContentRead:
    return await analytics_service.get_content(date_range)


@router.get(
    "/search-queries",
    response_model=AdminAnalyticsSearchQueryPageRead,
    summary="List aggregate raw search-query analytics",
)
async def list_admin_analytics_search_queries(
    _admin: AdminUserDep,
    analytics_service: AdminAnalyticsServiceDep,
    date_range: BoundedAdminAnalyticsDateRangeDep,
    limit: Annotated[int, Query(ge=1, le=QUERY_PAGE_LIMIT)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[Literal["searches", "zero_result_rate", "downloads", "niche"], Query()] = "searches",
) -> AdminAnalyticsSearchQueryPageRead:
    return await analytics_service.get_search_queries(date_range, limit=limit, offset=offset, sort=sort)


@router.get(
    "/search-queries/detail",
    response_model=AdminAnalyticsSearchQueryDetailRead,
    summary="Read aggregate meme outcomes for one raw search query",
)
async def get_admin_analytics_search_query_detail(
    _admin: AdminUserDep,
    analytics_service: AdminAnalyticsServiceDep,
    date_range: BoundedAdminAnalyticsDateRangeDep,
    query_key: Annotated[str, Query(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")],
) -> AdminAnalyticsSearchQueryDetailRead:
    try:
        return await analytics_service.get_search_query_detail(date_range, query_key=query_key)
    except AdminAnalyticsQueryKeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = [
    "AdminAnalyticsDateRangeDep",
    "AdminAnalyticsServiceDep",
    "BoundedAdminAnalyticsDateRangeDep",
    "get_bounded_admin_analytics_date_range",
    "get_admin_analytics_date_range",
    "get_admin_analytics_service",
    "router",
]
