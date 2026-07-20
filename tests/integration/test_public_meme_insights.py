# ruff: noqa: TC002,TC003
"""Focused public meme source and professional-analytics contract tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.api.dependencies.auth import get_optional_current_user
from memexpert.api.dependencies.meme import get_public_meme_insights_service
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeSource,
    MemeSourceEngagementSnapshot,
    SourceChannel,
    SourceChannelAudienceSnapshot,
)
from memexpert.models.enums import (
    AnalyticsEventType,
    ContentKind,
    ContentLanguage,
    IngestSourceKind,
    SourceChannelAudienceCaptureReason,
    SourceChannelAudienceFetchStatus,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
)
from memexpert.models.user import AnalyticsEvent, MemeExposure
from memexpert.services.public_meme_insights import PublicMemeInsightsService

pytestmark = pytest.mark.asyncio


async def _create_meme(
    session: AsyncSession,
    *,
    is_public: bool = True,
    like_count: int = 0,
    file_count: int = 1,
) -> tuple[Meme, list[MemeFile]]:
    meme_id = uuid.uuid7()
    files = [
        MemeFile(
            id=uuid.uuid7(),
            meme_id=meme_id,
            s3_original_key=f"memes/{meme_id}/{index}.jpg",
            mime_type="image/jpeg",
            quality_score=0.8,
        )
        for index in range(file_count)
    ]
    meme = Meme(
        id=meme_id,
        primary_file_id=files[0].id,
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        is_public=is_public,
        like_count=like_count,
    )
    session.add(meme)
    await session.flush()
    session.add_all(files)
    await session.flush()
    return meme, files


def _source(
    *,
    file_id: uuid.UUID,
    source_id: str,
    post_id: str,
    source_kind: IngestSourceKind,
    published_at: datetime,
    alive: bool = True,
) -> MemeSource:
    return MemeSource(
        file_id=file_id,
        platform=SourcePlatform.TELEGRAM,
        source_id=source_id,
        post_id=post_id,
        source_kind=source_kind,
        source_alive=alive,
        published_at=published_at,
        created_at=published_at,
    )


def _engagement(
    source: MemeSource,
    *,
    captured_at: datetime,
    views: int | None,
    reactions: int | None,
    comments: int | None,
    reposts: int | None,
    fetch_status: SourceEngagementFetchStatus = SourceEngagementFetchStatus.SUCCESS,
) -> MemeSourceEngagementSnapshot:
    return MemeSourceEngagementSnapshot(
        meme_source_id=source.id,
        captured_at=captured_at,
        capture_reason=SourceEngagementCaptureReason.SCHEDULED,
        view_count=views,
        reaction_count=reactions,
        comment_count=comments,
        forward_count=reposts,
        comments_state=SourceEngagementCommentsState.ENABLED,
        fetch_status=fetch_status,
        source_alive=source.source_alive,
    )


def _analytics_event(
    event_type: AnalyticsEventType,
    *,
    meme_id: uuid.UUID,
    occurred_at: datetime,
    impression_id: str | None = None,
) -> AnalyticsEvent:
    payload: dict[str, object] = {"refs": {"meme_id": str(meme_id)}, "surface": "test"}
    if impression_id is not None:
        payload["impression_id"] = impression_id
    return AnalyticsEvent(event_type=event_type, payload=payload, occurred_at=occurred_at)


def _install_overrides(app: FastAPI, session: AsyncSession) -> None:
    def insights_service() -> PublicMemeInsightsService:
        return PublicMemeInsightsService(session)

    async def anonymous_user() -> None:
        return None

    app.dependency_overrides[get_public_meme_insights_service] = insights_service
    app.dependency_overrides[get_optional_current_user] = anonymous_user


async def _seed_public_insights(
    session: AsyncSession,
) -> tuple[Meme, MemeSource, MemeSource, datetime]:
    now = datetime.now(UTC)
    meme, files = await _create_meme(session, like_count=7, file_count=2)
    channel = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id="-1001234567890",
        username="memexpert_public",
        title="MemeExpert Public",
    )
    second_channel = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id="-1009876543210",
        username="bad/name",
        title="Second channel",
    )
    session.add_all([channel, second_channel])
    await session.flush()
    first_published_at = now - timedelta(days=20)
    first = _source(
        file_id=files[0].id,
        source_id=channel.platform_id,
        post_id="101",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
        published_at=first_published_at,
    )
    second = _source(
        file_id=files[1].id,
        source_id=second_channel.platform_id,
        post_id="102",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
        published_at=now - timedelta(days=15),
        alive=False,
    )
    hidden_operator = _source(
        file_id=files[1].id,
        source_id="operator-private-source",
        post_id="999",
        source_kind=IngestSourceKind.OPERATOR_UPLOAD,
        published_at=now - timedelta(days=12),
    )
    session.add_all([first, second, hidden_operator])
    await session.flush()
    baseline_at = now - timedelta(days=10)
    session.add_all(
        [
            _engagement(
                first,
                captured_at=baseline_at,
                views=100,
                reactions=10,
                comments=2,
                reposts=3,
            ),
            _engagement(
                first,
                captured_at=now - timedelta(days=6),
                views=90,
                reactions=9,
                comments=2,
                reposts=2,
            ),
            _engagement(
                first,
                captured_at=now - timedelta(days=5),
                views=100,
                reactions=10,
                comments=2,
                reposts=3,
            ),
            _engagement(
                first,
                captured_at=now - timedelta(days=4),
                views=150,
                reactions=15,
                comments=4,
                reposts=5,
            ),
            _engagement(
                second,
                captured_at=now - timedelta(days=8),
                views=None,
                reactions=0,
                comments=None,
                reposts=0,
            ),
            _engagement(
                hidden_operator,
                captured_at=now - timedelta(days=1),
                views=999999,
                reactions=999999,
                comments=999999,
                reposts=999999,
            ),
            SourceChannelAudienceSnapshot(
                source_channel_id=channel.id,
                captured_at=first_published_at - timedelta(hours=1),
                capture_slot=(first_published_at - timedelta(hours=1)).date(),
                capture_reason=SourceChannelAudienceCaptureReason.CRAWLER_REFRESH,
                fetch_status=SourceChannelAudienceFetchStatus.SUCCESS,
                subscriber_count=1000,
            ),
            SourceChannelAudienceSnapshot(
                source_channel_id=channel.id,
                captured_at=now - timedelta(days=1),
                capture_slot=(now - timedelta(days=1)).date(),
                capture_reason=SourceChannelAudienceCaptureReason.SCHEDULED,
                fetch_status=SourceChannelAudienceFetchStatus.SUCCESS,
                subscriber_count=1200,
            ),
        ]
    )
    event_at = now - timedelta(days=2)
    session.add_all(
        [
            _analytics_event(AnalyticsEventType.MEME_VIEW, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.MEME_VIEW, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.MEME_SEND, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.MEME_SAVE, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.MEME_LIKE, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.MEME_DOWNLOAD, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.MEME_IMPRESSION, meme_id=meme.id, occurred_at=event_at),
            _analytics_event(AnalyticsEventType.INLINE_SERVED, meme_id=meme.id, occurred_at=event_at),
            MemeExposure(
                meme_id=meme.id,
                kind="web_card",
                exposure_key="web-1",
                exposed_at=event_at,
                detail_clicked_at=event_at + timedelta(minutes=1),
                high_intent_action_at=event_at + timedelta(minutes=2),
            ),
            MemeExposure(
                meme_id=meme.id,
                kind="web_card",
                exposure_key="web-2",
                exposed_at=event_at,
            ),
            MemeExposure(
                meme_id=meme.id,
                kind="telegram_inline",
                exposure_key="inline-1",
                exposed_at=event_at,
                inline_chosen_at=event_at + timedelta(minutes=1),
            ),
            MemeExposure(
                meme_id=meme.id,
                kind="telegram_inline",
                exposure_key="inline-2",
                exposed_at=event_at,
            ),
        ]
    )
    await session.commit()
    return meme, first, second, now


async def test_sources_are_stable_cross_file_public_only_and_null_honest(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme, _first, _second, now = await _seed_public_insights(migrated_db_session)
    cutoff = now - timedelta(days=4, seconds=-1)
    _install_overrides(app, migrated_db_session)
    try:
        response = await client.get(
            f"/api/v1/memes/{meme.id}/sources",
            params={"sort": "views_desc", "limit": 1, "snapshot_at": cutoff.isoformat()},
        )
        second_page = await client.get(
            f"/api/v1/memes/{meme.id}/sources",
            params={
                "sort": "views_desc",
                "limit": 1,
                "offset": 1,
                "snapshot_at": cutoff.isoformat(),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert second_page.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["has_more"] is True
    assert payload["summary"]["total_posts"] == 2
    assert payload["summary"]["available_posts"] == 1
    assert payload["summary"]["distinct_channels"] == 2
    assert payload["summary"]["totals"] == {
        "views": 150,
        "reactions": 15,
        "comments": 4,
        "reposts": 5,
    }
    assert payload["summary"]["coverage"]["views"] == {
        "measured_posts": 1,
        "total_posts": 2,
        "ratio": 0.5,
    }
    item = payload["items"][0]
    assert item["channel_title"] == "MemeExpert Public"
    assert item["channel_url"] == "https://t.me/memexpert_public"
    assert item["post_url"] == "https://t.me/memexpert_public/101"
    assert item["views"] == 150
    assert item["audience"]["audience_at_publish"] == 1000
    assert item["audience"]["current_audience"] == 1000
    null_item = second_page.json()["items"][0]
    assert null_item["available"] is False
    assert null_item["views"] is None
    assert null_item["channel_url"] is None
    assert null_item["post_url"] is None
    assert "source_id" not in str(payload)
    assert "operator-private-source" not in str(payload)


async def test_analytics_use_high_watermarks_absolute_observations_and_keyed_funnels(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    meme, _first, _second, now = await _seed_public_insights(migrated_db_session)
    _install_overrides(app, migrated_db_session)
    try:
        response = await client.get(
            f"/api/v1/memes/{meme.id}/analytics",
            params={"window": "7d"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    totals = payload["summary"]["totals"]
    assert totals == {
        "source_views": 50,
        "source_reactions": 5,
        "source_reposts": 2,
        "memeexpert_views": 2,
        "memeexpert_sends": 1,
        "memeexpert_saves": 1,
        "memeexpert_favorites": 1,
        "downloads": 1,
        "recorded_activity": 62,
    }
    assert payload["summary"]["current_favorites"] == 7
    assert payload["observed_source"]["opening_baseline"]["views"] == 100
    observed_views = [point["views"] for point in payload["observed_source"]["points"]]
    assert observed_views == [90, 100, 150]
    observed_times = [
        datetime.fromisoformat(point["observed_at"].replace("Z", "+00:00"))
        for point in payload["observed_source"]["points"]
    ]
    assert observed_times == [
        now - timedelta(days=6),
        now - timedelta(days=5),
        now - timedelta(days=4),
    ]
    assert payload["source_performance"]["totals"]["views"] == 150
    assert payload["audience_change"] == {
        "total_channels": 2,
        "current_known_channels": 1,
        "comparable_channels": 1,
        "net_known_subscriber_change": 200,
    }
    assert payload["exposure_funnels"]["web"] == {
        "recorded_card_impressions": 3,
        "attributed_impressions": 2,
        "matched_detail_clicks": 1,
        "matched_high_intent_actions": 1,
        "detail_click_rate": 0.5,
        "high_intent_rate": 0.5,
    }
    assert payload["exposure_funnels"]["telegram_inline"] == {
        "inline_results_served": 3,
        "attributed_results_served": 2,
        "matched_chosen": 1,
        "matched_sent": 0,
        "chosen_rate": 0.5,
        "sent_rate": 0.0,
    }


async def test_analytics_activity_days_stay_utc_in_a_non_utc_database_session(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    utc_day = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    activity_at = utc_day + timedelta(minutes=30)
    meme, files = await _create_meme(migrated_db_session)
    source = _source(
        file_id=files[0].id,
        source_id="-1001234567000",
        post_id="501",
        source_kind=IngestSourceKind.PUBLIC_CRAWLER,
        published_at=utc_day - timedelta(days=2),
    )
    migrated_db_session.add(source)
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            _engagement(
                source,
                captured_at=utc_day - timedelta(minutes=1),
                views=100,
                reactions=0,
                comments=0,
                reposts=0,
            ),
            _engagement(
                source,
                captured_at=activity_at,
                views=125,
                reactions=0,
                comments=0,
                reposts=0,
            ),
            _analytics_event(
                AnalyticsEventType.MEME_VIEW,
                meme_id=meme.id,
                occurred_at=activity_at,
            ),
        ]
    )
    await migrated_db_session.commit()
    await migrated_db_session.execute(text("SET LOCAL TIME ZONE 'Pacific/Honolulu'"))
    _install_overrides(app, migrated_db_session)
    try:
        response = await client.get(
            f"/api/v1/memes/{meme.id}/analytics",
            params={"window": "7d"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    activity_points = response.json()["activity_points"]
    matching_points = [
        point
        for point in activity_points
        if point["source_views"] or point["memeexpert_views"]
    ]
    assert len(matching_points) == 1
    point = matching_points[0]
    assert datetime.fromisoformat(point["bucket_start"].replace("Z", "+00:00")) == utc_day
    assert point["source_views"] == 25
    assert point["memeexpert_views"] == 1


async def test_private_memes_and_operator_only_provenance_are_not_public(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    private, private_files = await _create_meme(migrated_db_session, is_public=False)
    operator_only, operator_files = await _create_meme(migrated_db_session)
    published_at = datetime.now(UTC) - timedelta(days=1)
    migrated_db_session.add_all(
        [
            _source(
                file_id=private_files[0].id,
                source_id="-100private",
                post_id="1",
                source_kind=IngestSourceKind.PUBLIC_CRAWLER,
                published_at=published_at,
            ),
            _source(
                file_id=operator_files[0].id,
                source_id="operator",
                post_id="2",
                source_kind=IngestSourceKind.OPERATOR_UPLOAD,
                published_at=published_at,
            ),
        ]
    )
    await migrated_db_session.commit()
    _install_overrides(app, migrated_db_session)
    try:
        private_sources = await client.get(f"/api/v1/memes/{private.id}/sources")
        private_analytics = await client.get(f"/api/v1/memes/{private.id}/analytics")
        operator_sources = await client.get(f"/api/v1/memes/{operator_only.id}/sources")
    finally:
        app.dependency_overrides.clear()

    assert private_sources.status_code == 404
    assert private_analytics.status_code == 404
    assert operator_sources.status_code == 200
    assert operator_sources.json()["items"] == []
    assert operator_sources.json()["total"] == 0


async def test_openapi_exposes_insights_without_source_or_session_internals(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/memes/{meme_id}/sources" in paths
    assert "/api/v1/memes/{meme_id}/analytics" in paths
    source_parameters = {
        parameter["name"]: parameter
        for parameter in paths["/api/v1/memes/{meme_id}/sources"]["get"]["parameters"]
    }
    assert {"sort", "limit", "offset", "snapshot_at"} <= source_parameters.keys()
    assert source_parameters["limit"]["schema"]["maximum"] == 100
    analytics_parameters = {
        parameter["name"]: parameter
        for parameter in paths["/api/v1/memes/{meme_id}/analytics"]["get"]["parameters"]
    }
    assert analytics_parameters["window"]["schema"]["$ref"].endswith("PublicMemeAnalyticsWindow")
    public_schema_names = {
        name: component
        for name, component in schema["components"]["schemas"].items()
        if name.startswith("PublicMemeSource") or name.startswith("PublicMemeAnalytics")
    }
    serialized = str(public_schema_names).lower()
    for forbidden in (
        "uploader",
        "telegram_session",
        "raw_metrics",
        "error_code",
        "forwarded_from",
        "source_text",
        "source_id",
    ):
        assert forbidden not in serialized
