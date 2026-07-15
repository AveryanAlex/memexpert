"""Integration coverage for the privacy-bounded public page-view endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from memexpert.api.dependencies.meme import get_analytics_service
from memexpert.models.enums import AnalyticsEventType
from memexpert.models.user import AnalyticsEvent
from memexpert.services.analytics import AnalyticsService

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_page_view_records_only_a_coarse_surface(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    app.dependency_overrides[get_analytics_service] = override_analytics_service
    try:
        response = await client.post(
            "/api/v1/analytics/page-views",
            json={"surface": "web_search"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json() == {"ok": True}

    event = await migrated_db_session.scalar(
        select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.PAGE_VIEW)
    )
    assert event is not None
    assert event.user_id is None
    assert event.payload == {
        "schema_version": 1,
        "actor_type": "anonymous",
        "surface": "web_search",
        "refs": {},
        "score_components": {},
        "properties": {},
    }
    assert "url" not in event.payload
    assert "path" not in event.payload


async def test_page_view_rejects_raw_route_fields(
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    response = await client.post(
        "/api/v1/analytics/page-views",
        json={"surface": "web_search", "url": "/search?q=private+query"},
    )

    assert response.status_code == 422
    count = await migrated_db_session.scalar(
        select(func.count())
        .select_from(AnalyticsEvent)
        .where(AnalyticsEvent.event_type == AnalyticsEventType.PAGE_VIEW)
    )
    assert count == 0
