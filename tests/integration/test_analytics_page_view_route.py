"""Integration coverage for the privacy-bounded public page-view endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import func, select

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_analytics_service, get_meme_search_service
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import (
    AnalyticsEventType,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
)
from memexpert.models.recommendation import UserMemeRecommendationState
from memexpert.models.user import AnalyticsEvent, MemeExposure
from memexpert.schemas.meme import MemeResultAttributionRead
from memexpert.schemas.user import UserRead
from memexpert.services.analytics import AnalyticsService
from memexpert.services.meme_search import MemeSearchService
from memexpert.services.recommendations.attribution import AttributionTokenService
from tests.factories import build_full_user

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


async def test_interaction_batch_verifies_tokens_and_retries_without_duplicate_state(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user = build_full_user()
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        primary_file_id=file_id,
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        is_public=True,
    )
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"analytics/{meme_id}.jpg",
    )
    migrated_db_session.add_all([user, meme])
    await migrated_db_session.flush()
    migrated_db_session.add(meme_file)
    await migrated_db_session.commit()
    current_user = UserRead.model_validate(user)

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    async def override_current_user() -> UserRead:
        return current_user

    token = AttributionTokenService.from_settings().issue_for_result(
        meme_id=meme_id,
        viewer_user_id=user.id,
        attribution=MemeResultAttributionRead(
            request_id="req-batch-1",
            impression_id="imp-batch-1",
            surface="web_home",
            source_algorithm="personalized",
            rank=1,
            algorithm_version="personalized_v2",
        ),
    )
    observed_at = datetime.now(UTC)
    events = [
        {
            "event_id": str(uuid.uuid7()),
            "event_type": event_type,
            "meme_id": str(meme_id),
            "occurred_at": (observed_at + timedelta(seconds=index)).isoformat(),
            "attribution_token": token,
            "properties": {"impression_id": "client-must-not-override-signed-claim"},
        }
        for index, event_type in enumerate(
            ("meme_impression", "meme_engaged_view", "meme_detail_click")
        )
    ]
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        first = await client.post("/api/v1/analytics/interactions/batch", json={"events": events})
        retried = await client.post("/api/v1/analytics/interactions/batch", json={"events": events})
        logical_retried = await client.post(
            "/api/v1/analytics/interactions/batch",
            json={
                "events": [
                    {**event, "event_id": str(uuid.uuid7())}
                    for event in events[:2]
                ]
            },
        )
        tampered_token = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"
        tampered = await client.post(
            "/api/v1/analytics/interactions/batch",
            json={
                "events": [
                    {
                        **events[0],
                        "event_id": str(uuid.uuid7()),
                        "attribution_token": tampered_token,
                    }
                ]
            },
        )
        future = await client.post(
            "/api/v1/analytics/interactions/batch",
            json={
                "events": [
                    {
                        **events[0],
                        "event_id": str(uuid.uuid7()),
                        "occurred_at": (datetime.now(UTC) + timedelta(minutes=6)).isoformat(),
                    }
                ]
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert first.json() == {"recorded": 3, "duplicates": 0}
    assert retried.status_code == 202
    assert retried.json() == {"recorded": 0, "duplicates": 3}
    assert logical_retried.status_code == 202
    assert logical_retried.json() == {"recorded": 0, "duplicates": 2}
    assert tampered.status_code == 422
    assert future.status_code == 422
    state = await migrated_db_session.get(UserMemeRecommendationState, (user.id, meme_id))
    assert state is not None
    assert state.impression_count == 1
    assert state.latest_engaged_view_at == observed_at + timedelta(seconds=1)
    stored = (
        await migrated_db_session.scalars(
            select(AnalyticsEvent).where(
                AnalyticsEvent.id.in_(
                    tuple(uuid.UUID(cast("str", event["event_id"])) for event in events)
                )
            )
        )
    ).all()
    assert len(stored) == 3
    for event in stored:
        assert event.payload["impression_id"] == "imp-batch-1"
        properties = event.payload.get("properties")
        assert isinstance(properties, dict)
        assert "impression_id" not in properties
        assert cast("dict[str, object]", properties)["attribution_trusted"] is True


async def test_tokenless_interaction_batch_promotes_client_impression_identity(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    user = build_full_user()
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        primary_file_id=file_id,
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        is_public=True,
    )
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"analytics/{meme_id}.jpg",
    )
    migrated_db_session.add_all([user, meme])
    await migrated_db_session.flush()
    migrated_db_session.add(meme_file)
    await migrated_db_session.commit()
    current_user = UserRead.model_validate(user)

    def override_analytics_service() -> AnalyticsService:
        return AnalyticsService(migrated_db_session)

    def override_meme_search_service() -> MemeSearchService:
        return MemeSearchService(migrated_db_session)

    async def override_current_user() -> UserRead:
        return current_user

    observed_at = datetime.now(UTC)
    client_impression_id = str(uuid.uuid7())
    events = [
        {
            "event_id": str(uuid.uuid7()),
            "event_type": event_type,
            "meme_id": str(meme_id),
            "occurred_at": (observed_at + timedelta(seconds=index)).isoformat(),
            "properties": {"impression_id": client_impression_id},
        }
        for index, event_type in enumerate(
            ("meme_impression", "meme_engaged_view", "meme_detail_click")
        )
    ]
    app.dependency_overrides[get_analytics_service] = override_analytics_service
    app.dependency_overrides[get_meme_search_service] = override_meme_search_service
    app.dependency_overrides[get_optional_current_user] = override_current_user
    try:
        first = await client.post("/api/v1/analytics/interactions/batch", json={"events": events})
        logical_retried = await client.post(
            "/api/v1/analytics/interactions/batch",
            json={
                "events": [
                    {**event, "event_id": str(uuid.uuid7())}
                    for event in events[:2]
                ]
            },
        )
        invalid_identity = await client.post(
            "/api/v1/analytics/interactions/batch",
            json={
                "events": [
                    {
                        **events[0],
                        "event_id": str(uuid.uuid7()),
                        "properties": {"impression_id": str(uuid.uuid4())},
                    }
                ]
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert first.json() == {"recorded": 3, "duplicates": 0}
    assert logical_retried.status_code == 202
    assert logical_retried.json() == {"recorded": 0, "duplicates": 2}
    assert invalid_identity.status_code == 422
    state = await migrated_db_session.get(UserMemeRecommendationState, (user.id, meme_id))
    assert state is not None
    assert state.impression_count == 1
    assert state.latest_engaged_view_at == observed_at + timedelta(seconds=1)
    exposure = await migrated_db_session.scalar(
        select(MemeExposure).where(
            MemeExposure.meme_id == meme_id,
            MemeExposure.exposure_key == client_impression_id,
        )
    )
    assert exposure is not None
    assert exposure.exposed_at == observed_at
    assert exposure.detail_clicked_at == observed_at + timedelta(seconds=2)
    stored = (
        await migrated_db_session.scalars(
            select(AnalyticsEvent).where(
                AnalyticsEvent.id.in_(
                    tuple(uuid.UUID(cast("str", event["event_id"])) for event in events)
                )
            )
        )
    ).all()
    assert len(stored) == 3
    for event in stored:
        assert event.payload["impression_id"] == client_impression_id
        properties = event.payload.get("properties")
        assert isinstance(properties, dict)
        assert "impression_id" not in properties
        assert cast("dict[str, object]", properties)["attribution_trusted"] is False
