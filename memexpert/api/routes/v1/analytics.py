# ruff: noqa: TC001
"""Privacy-bounded first-party telemetry endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from memexpert.api.dependencies import AnalyticsServiceDep, OptionalCurrentUserDep
from memexpert.models.enums import AnalyticsEventType
from memexpert.schemas.analytics import PageViewCreateRequest, PageViewRecordedRead
from memexpert.services.analytics import InteractionEventWrite

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.post(
    "/page-views",
    response_model=PageViewRecordedRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record a consumer page-view category",
)
async def record_page_view(
    page_view: PageViewCreateRequest,
    current_user: OptionalCurrentUserDep,
    analytics_service: AnalyticsServiceDep,
) -> PageViewRecordedRead:
    """Record a first-party page view without accepting URLs or visitor metadata.

    The writer is deliberately best-effort: the browser should never see a
    product error because analytics storage is temporarily unavailable.
    """

    await analytics_service.record_interaction_event_best_effort(
        InteractionEventWrite(
            event_type=AnalyticsEventType.PAGE_VIEW,
            user_id=current_user.id if current_user else None,
            surface=page_view.surface.value,
        )
    )
    return PageViewRecordedRead()


__all__ = ["router"]
