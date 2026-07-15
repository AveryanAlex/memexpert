# ruff: noqa: TC001,TC002
"""Integration coverage for the browser-admin analytics API boundary."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memexpert.models.content import SourceChannel
from memexpert.models.enums import AnalyticsEventType, SourcePlatform
from memexpert.models.user import AnalyticsEvent, User
from memexpert.services.account_link_service import AccountLinkService
from memexpert.services.admin_analytics import AdminAnalyticsDateRange, AdminAnalyticsService
from memexpert.services.provider_auth_service import ProviderAuthService
from tests.integration.test_admin_routes import _issue_user_cookie
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME

if TYPE_CHECKING:
    from fastapi import FastAPI


pytestmark = pytest.mark.asyncio

PASSWORD = "correct-horse-battery"


async def test_admin_analytics_routes_require_admin_access_and_expose_aggregate_query_data_only(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All dashboard endpoints share the durable admin and privacy boundary."""

    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="analytics-admin@example.com",
        is_admin=True,
    )
    non_admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="analytics-non-admin@example.com",
        is_admin=False,
    )
    now = datetime.now(UTC)
    raw_query = "rare frog reaction"
    async with postgres_session_factory() as session:
        session.add(
            AnalyticsEvent(
                event_type=AnalyticsEventType.SEARCH_QUERY,
                payload={
                    "schema_version": 1,
                    "actor_type": "anonymous",
                    "surface": "web_search",
                    "query": raw_query,
                    "request_id": "request-id-must-not-leak",
                    "refs": {},
                    "score_components": {},
                    "properties": {"result_total": 0, "latency_ms": 12},
                },
                occurred_at=now,
            ),
        )
        await session.commit()

    today = now.date()
    range_params = {
        "start_date": (today - timedelta(days=1)).isoformat(),
        "end_date": today.isoformat(),
    }
    paths = (
        "/api/v1/admin/analytics/overview",
        "/api/v1/admin/analytics/engagement",
        "/api/v1/admin/analytics/audience",
        "/api/v1/admin/analytics/content",
        "/api/v1/admin/analytics/search-queries",
        "/api/v1/admin/analytics/search-queries/detail",
    )
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        anonymous_responses = [
            await anonymous_client.get(
                path,
                params={
                    **range_params,
                    **({"query_key": "0" * 64} if path.endswith("/detail") else {}),
                },
            )
            for path in paths
        ]

    async with AsyncClient(transport=transport, base_url="https://testserver") as non_admin_client:
        non_admin_client.cookies.set(ACCESS_COOKIE_NAME, non_admin_token)
        non_admin_response = await non_admin_client.get("/api/v1/admin/analytics/search-queries", params=range_params)

    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        overview_response = await admin_client.get("/api/v1/admin/analytics/overview", params=range_params)
        engagement_response = await admin_client.get("/api/v1/admin/analytics/engagement", params=range_params)
        audience_response = await admin_client.get("/api/v1/admin/analytics/audience", params=range_params)
        content_response = await admin_client.get("/api/v1/admin/analytics/content", params=range_params)
        queries_response = await admin_client.get("/api/v1/admin/analytics/search-queries", params=range_params)
        query_item = next(item for item in queries_response.json()["items"] if item["query"] == raw_query)
        detail_response = await admin_client.get(
            "/api/v1/admin/analytics/search-queries/detail",
            params={**range_params, "query_key": query_item["query_key"]},
        )
        future_response = await admin_client.get(
            "/api/v1/admin/analytics/overview",
            params={**range_params, "end_date": (today + timedelta(days=1)).isoformat()},
        )

    assert [response.status_code for response in anonymous_responses] == [401] * len(paths)
    assert non_admin_response.status_code == 403
    assert all(
        response.status_code == 200
        for response in (
            overview_response,
            engagement_response,
            audience_response,
            content_response,
            queries_response,
            detail_response,
        )
    )
    assert future_response.status_code == 422

    for response in (
        overview_response,
        engagement_response,
        audience_response,
        content_response,
        queries_response,
        detail_response,
    ):
        assert response.json()["range"]["start_date"] == range_params["start_date"]
        assert response.json()["range"]["end_date"] == range_params["end_date"]

    assert set(query_item) == {
        "query",
        "query_key",
        "searches",
        "zero_result_searches",
        "zero_result_rate",
        "average_latency_ms",
        "detail_clicks",
        "downloads",
    }
    assert query_item["searches"] == 1
    assert query_item["zero_result_searches"] == 1
    assert detail_response.json()["query_key"] == query_item["query_key"]
    assert raw_query not in str(detail_response.request.url)
    assert "request-id-must-not-leak" not in json.dumps(detail_response.json())
    assert "request_id" not in json.dumps(detail_response.json())


async def test_admin_analytics_rejects_event_volumes_above_the_memory_safety_ceiling(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="analytics-volume-admin@example.com",
        is_admin=True,
    )
    now = datetime.now(UTC)
    async with postgres_session_factory() as session:
        session.add(
            AnalyticsEvent(
                event_type=AnalyticsEventType.PAGE_VIEW,
                payload={"surface": "web_home"},
                occurred_at=now,
            ),
        )
        await session.commit()
    monkeypatch.setattr("memexpert.services.admin_analytics.MAX_ANALYTICS_EVENT_ROWS", 0)

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.get(
            "/api/v1/admin/analytics/overview",
            params={"start_date": now.date().isoformat(), "end_date": now.date().isoformat()},
        )
        content_response = await admin_client.get(
            "/api/v1/admin/analytics/content",
            params={"start_date": now.date().isoformat(), "end_date": now.date().isoformat()},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "This reporting range contains too many analytics events. Choose a shorter date range."
    )
    assert content_response.status_code == 200


async def test_admin_analytics_uses_search_request_attribution_event_time_actor_state_and_telegram_orphans(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="analytics-semantic-admin@example.com",
        is_admin=True,
    )
    now = datetime.now(UTC)
    today = now.date()
    strict_guest_user = User(email="analytics-strict-guest@example.com", password_hash="hash")
    strict_full_user = User(email="analytics-strict-full@example.com", password_hash="hash")
    legacy_full_user = User(email="analytics-legacy-full@example.com", password_hash="hash")
    async with postgres_session_factory() as session:
        session.add_all([strict_guest_user, strict_full_user, legacy_full_user])
        await session.flush()
        search_request_id = "attributed-search-request"
        session.add_all(
            [
                AnalyticsEvent(
                    event_type=AnalyticsEventType.SEARCH_QUERY,
                    payload={
                        "query": "request-attributed meme",
                        "request_id": search_request_id,
                        "properties": {"result_total": 1},
                    },
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    event_type=AnalyticsEventType.MEME_DETAIL_CLICK,
                    payload={"request_id": search_request_id, "refs": {"meme_id": str(uuid.uuid7())}},
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    event_type=AnalyticsEventType.MEME_DOWNLOAD,
                    payload={"request_id": search_request_id, "refs": {"meme_id": str(uuid.uuid7())}},
                    occurred_at=now,
                ),
                # A raw query string alone is not sufficient attribution: it
                # must join a search request recorded in the selected range.
                AnalyticsEvent(
                    event_type=AnalyticsEventType.MEME_DOWNLOAD,
                    payload={"query": "request-attributed meme", "refs": {"meme_id": str(uuid.uuid7())}},
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    user_id=strict_guest_user.id,
                    event_type=AnalyticsEventType.MEME_VIEW,
                    payload={"actor_account_type": "guest", "refs": {"meme_id": str(uuid.uuid7())}},
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    user_id=strict_full_user.id,
                    event_type=AnalyticsEventType.MEME_VIEW,
                    payload={"actor_account_type": "full", "refs": {"meme_id": str(uuid.uuid7())}},
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    user_id=legacy_full_user.id,
                    event_type=AnalyticsEventType.MEME_VIEW,
                    payload={"refs": {"meme_id": str(uuid.uuid7())}},
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    event_type=AnalyticsEventType.AUTH_EVENT,
                    payload={
                        "properties": {
                            "action": "guest_upgraded",
                            "guest_was_persistent": True,
                            "full_account_created": True,
                            "merge_performed": False,
                        },
                        "refs": {"source_user_id": str(uuid.uuid7())},
                    },
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    event_type=AnalyticsEventType.AUTH_EVENT,
                    payload={
                        "properties": {
                            "action": "guest_upgraded",
                            "guest_was_persistent": True,
                            "full_account_created": False,
                            "merge_performed": True,
                        },
                        "refs": {"source_user_id": str(uuid.uuid7())},
                    },
                    occurred_at=now,
                ),
                AnalyticsEvent(
                    event_type=AnalyticsEventType.AUTH_EVENT,
                    payload={
                        "properties": {
                            "action": "guest_upgraded",
                            "guest_was_persistent": False,
                            "full_account_created": True,
                            "merge_performed": False,
                        },
                        "refs": {"source_user_id": str(uuid.uuid7())},
                    },
                    occurred_at=now,
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="analytics-orphaned-telegram",
                    title="Orphaned Telegram source",
                ),
                SourceChannel(
                    platform=SourcePlatform.REDDIT,
                    platform_id="analytics-healthy-reddit",
                    title="Healthy Reddit source",
                    last_fetched_at=now,
                ),
            ],
        )
        await session.commit()

    range_params = {"start_date": today.isoformat(), "end_date": today.isoformat()}
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        overview_response = await admin_client.get("/api/v1/admin/analytics/overview", params=range_params)
        audience_response = await admin_client.get("/api/v1/admin/analytics/audience", params=range_params)
        content_response = await admin_client.get("/api/v1/admin/analytics/content", params=range_params)

    assert overview_response.status_code == 200
    assert audience_response.status_code == 200
    assert content_response.status_code == 200
    funnel = overview_response.json()["discovery_funnel"]
    assert funnel["detail_clicks"] == 1
    assert funnel["downloads"] == 1

    audience_metrics = audience_response.json()["metrics"]
    assert audience_metrics["active_guests"]["value"] == 1
    assert audience_metrics["active_full_accounts"]["value"] == 2
    assert audience_metrics["new_full_accounts"]["value"] == 2
    assert audience_metrics["guest_to_full_conversions"]["value"] == 2

    source_health = {item["key"]: item["count"] for item in content_response.json()["source_health"]}
    assert source_health["orphaned"] == 1
    assert source_health["healthy"] == 1


async def test_admin_retention_cohort_survives_guest_merge_into_older_full_account(
    migrated_db_session: AsyncSession,
) -> None:
    today = datetime.now(UTC).date()
    cohort_date = today - timedelta(days=40)
    cohort_at = datetime.combine(cohort_date, datetime.min.time(), tzinfo=UTC)
    provider_auth_service = ProviderAuthService(
        migrated_db_session,
        password_hash_rounds=4,
    )
    target_identity = provider_auth_service.prepare_email_signup_identity(
        email="retention-target@example.com",
        password=PASSWORD,
    )
    merged_guest = User(created_at=cohort_at)
    older_full_user = User(
        email=target_identity.email,
        password_hash=target_identity.password_hash,
        created_at=cohort_at - timedelta(days=365),
    )
    unmerged_guest = User(created_at=cohort_at)
    direct_full_user = User(
        email="retention-direct@example.com",
        password_hash="test-password-hash",
        created_at=cohort_at,
    )
    migrated_db_session.add_all([merged_guest, older_full_user, unmerged_guest, direct_full_user])
    await migrated_db_session.flush()

    def guest_created_event(user: User) -> AnalyticsEvent:
        return AnalyticsEvent(
            user_id=user.id,
            event_type=AnalyticsEventType.AUTH_EVENT,
            payload={
                "properties": {"action": "guest_created"},
                "refs": {"source_user_id": str(user.id)},
            },
            occurred_at=cohort_at,
        )

    def activity_event(user: User, *, days: int) -> AnalyticsEvent:
        return AnalyticsEvent(
            user_id=user.id,
            event_type=AnalyticsEventType.PAGE_VIEW,
            payload={"surface": "web_home"},
            occurred_at=cohort_at + timedelta(days=days, hours=1),
        )

    migrated_db_session.add_all(
        [
            guest_created_event(merged_guest),
            activity_event(merged_guest, days=1),
            activity_event(merged_guest, days=7),
            activity_event(merged_guest, days=30),
            guest_created_event(unmerged_guest),
            activity_event(unmerged_guest, days=1),
            activity_event(direct_full_user, days=7),
        ]
    )
    await migrated_db_session.commit()

    link_result = await AccountLinkService(
        migrated_db_session,
        provider_auth_service=provider_auth_service,
    ).link_guest_with_email_login(
        guest_user_id=merged_guest.id,
        email=target_identity.email,
        password=PASSWORD,
    )

    assert link_result.merge_performed is True
    assert link_result.canonical_user_id == older_full_user.id
    assert link_result.deleted_guest_user_id == merged_guest.id

    audience = await AdminAnalyticsService(
        migrated_db_session,
        query_key_secret="retention-test-secret",
    ).get_audience(
        AdminAnalyticsDateRange(start_date=cohort_date, end_date=cohort_date)
    )

    assert len(audience.retention_cohorts) == 1
    cohort = audience.retention_cohorts[0]
    assert cohort.cohort_date == cohort_date
    assert cohort.cohort_size == 3
    assert cohort.d1 is not None
    assert cohort.d1.eligible_users == 3
    assert cohort.d1.retained_users == 2
    assert cohort.d7 is not None
    assert cohort.d7.eligible_users == 3
    assert cohort.d7.retained_users == 2
    assert cohort.d30 is not None
    assert cohort.d30.eligible_users == 3
    assert cohort.d30.retained_users == 1
