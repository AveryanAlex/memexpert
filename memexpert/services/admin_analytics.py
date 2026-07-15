"""Aggregate-only read model for the browser-admin analytics workspace.

This service intentionally reads the shared ``analytics_events`` stream instead
of introducing a second reporting store.  It understands both the historical
flat payload shape and the current strict envelope, so a dashboard range can
span the telemetry migration without inventing data.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import func, or_, select

from memexpert.core.config import get_settings
from memexpert.models.base import utcnow
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeSeoPage,
    MemeSource,
    MemeSourceEngagementSnapshot,
    SourceChannel,
)
from memexpert.models.enums import SourceEngagementFetchStatus, SourcePlatform
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, User
from memexpert.schemas.admin_analytics import (
    AdminAnalyticsAudienceActivityPointRead,
    AdminAnalyticsAudienceRead,
    AdminAnalyticsBreakdownRead,
    AdminAnalyticsCatalogGrowthPointRead,
    AdminAnalyticsContentRead,
    AdminAnalyticsDiscoveryFunnelRead,
    AdminAnalyticsEngagementActivityPointRead,
    AdminAnalyticsEngagementRead,
    AdminAnalyticsMetricRead,
    AdminAnalyticsOverviewActivityPointRead,
    AdminAnalyticsOverviewRead,
    AdminAnalyticsQueryMemeOutcomeRead,
    AdminAnalyticsRangeRead,
    AdminAnalyticsRetentionCohortRead,
    AdminAnalyticsRetentionPeriodRead,
    AdminAnalyticsSearchQueryDetailRead,
    AdminAnalyticsSearchQueryPageRead,
    AdminAnalyticsSearchQueryRead,
    AdminAnalyticsSourceActivityRead,
    AdminAnalyticsSourceEngagementPointRead,
    AdminAnalyticsSurfaceRead,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


MAX_ANALYTICS_RANGE_DAYS = 366
MAX_ANALYTICS_EVENT_ROWS = 250_000
DEFAULT_ANALYTICS_RANGE_DAYS = 30
QUERY_PAGE_LIMIT = 100
TOP_SEARCH_QUERY_LIMIT = 10
TOP_MEME_OUTCOME_LIMIT = 50
SOURCE_STALE_AFTER = timedelta(days=1)
_QUERY_KEY_DOMAIN = b"admin-analytics-query-key:v1\0"

_PAGE_VIEW_EVENT = "page_view"
_SEARCH_EVENT = "search_query"
_AUTH_EVENT = "auth_event"
_ACCOUNT_MERGE_EVENT = "account_merge"
_DOWNLOAD_EVENTS = frozenset({"meme_download"})
_DETAIL_CLICK_EVENTS = frozenset({"meme_detail_click", "click"})
_SEND_EVENTS = frozenset({"meme_send", "inline_sent"})
_SAVE_EVENTS = frozenset({"meme_save", "save", "favorite"})
_SHARE_EVENTS = frozenset({"meme_share", "share"})
_NON_INTERACTION_EVENTS = frozenset(
    {
        _PAGE_VIEW_EVENT,
        _SEARCH_EVENT,
        _AUTH_EVENT,
        _ACCOUNT_MERGE_EVENT,
        "channel_suggest",
        "impression",
        "inline_query",
        "inline_served",
        "meme_impression",
        "miniapp_open",
    },
)
_CONVERSION_ACTIONS = frozenset({"guest_upgraded", "telegram_link_redeemed"})


class AdminAnalyticsDateRangeError(ValueError):
    """Raised when date query parameters cannot describe a bounded UTC range."""


class AdminAnalyticsQueryKeyNotFoundError(ValueError):
    """Raised when an opaque query key is not present in the selected range."""


class AdminAnalyticsEventVolumeError(ValueError):
    """Raised before an admin range can materialize an unsafe event volume."""


@dataclass(frozen=True, slots=True)
class AdminAnalyticsDateRange:
    """Inclusive UTC calendar range plus a same-length preceding comparison."""

    start_date: date
    end_date: date

    @property
    def day_count(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def comparison_start_date(self) -> date:
        return self.start_date - timedelta(days=self.day_count)

    @property
    def comparison_end_date(self) -> date:
        return self.start_date - timedelta(days=1)

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start_date, time.min, tzinfo=UTC)

    @property
    def comparison_start_at(self) -> datetime:
        return datetime.combine(self.comparison_start_date, time.min, tzinfo=UTC)

    @property
    def end_exclusive_at(self) -> datetime:
        return datetime.combine(self.end_date + timedelta(days=1), time.min, tzinfo=UTC)

    def to_read(self) -> AdminAnalyticsRangeRead:
        return AdminAnalyticsRangeRead(
            start_date=self.start_date,
            end_date=self.end_date,
            comparison_start_date=self.comparison_start_date,
            comparison_end_date=self.comparison_end_date,
        )


def resolve_admin_analytics_date_range(
    *,
    start_date: date | None,
    end_date: date | None,
    now: datetime | None = None,
) -> AdminAnalyticsDateRange:
    """Resolve optional date query parameters to a bounded inclusive UTC range."""

    today = _utc_date(now or utcnow())
    if start_date is None and end_date is None:
        end_date = today
        start_date = end_date - timedelta(days=DEFAULT_ANALYTICS_RANGE_DAYS - 1)
    elif start_date is None:
        assert end_date is not None
        start_date = end_date - timedelta(days=DEFAULT_ANALYTICS_RANGE_DAYS - 1)
    elif end_date is None:
        end_date = today

    if end_date < start_date:
        raise AdminAnalyticsDateRangeError("end_date must be on or after start_date.")
    if end_date > today:
        raise AdminAnalyticsDateRangeError("end_date may not be in the future.")
    day_count = (end_date - start_date).days + 1
    if day_count > MAX_ANALYTICS_RANGE_DAYS:
        raise AdminAnalyticsDateRangeError(f"Date ranges may not exceed {MAX_ANALYTICS_RANGE_DAYS} days.")
    return AdminAnalyticsDateRange(start_date=start_date, end_date=end_date)


@dataclass(frozen=True, slots=True)
class _EventRecord:
    event_type: str
    payload: Mapping[str, object]
    user_id: uuid.UUID | None
    occurred_at: datetime

    @property
    def occurred_date(self) -> date:
        return _utc_date(self.occurred_at)


@dataclass(slots=True)
class _MemeOutcome:
    interactions: int = 0
    detail_clicks: int = 0
    downloads: int = 0
    saves: int = 0
    shares: int = 0


@dataclass(slots=True)
class _QueryStats:
    query: str
    searches: int = 0
    zero_result_searches: int = 0
    latency_total: float = 0.0
    latency_count: int = 0
    request_ids: set[str] = field(default_factory=set)
    detail_clicks: int = 0
    downloads: int = 0
    outcomes: dict[uuid.UUID, _MemeOutcome] = field(default_factory=dict)

    @property
    def zero_result_rate(self) -> float | None:
        return _percent(self.zero_result_searches, self.searches)

    @property
    def average_latency_ms(self) -> float | None:
        if self.latency_count == 0:
            return None
        return round(self.latency_total / self.latency_count, 2)

    def to_read(self, *, query_key: str) -> AdminAnalyticsSearchQueryRead:
        return AdminAnalyticsSearchQueryRead(
            query=self.query,
            query_key=query_key,
            searches=self.searches,
            zero_result_searches=self.zero_result_searches,
            zero_result_rate=self.zero_result_rate,
            average_latency_ms=self.average_latency_ms,
            detail_clicks=self.detail_clicks,
            downloads=self.downloads,
        )


class AdminAnalyticsService:
    """Read compact admin dashboard payloads without exposing event identities."""

    def __init__(self, session: AsyncSession, *, query_key_secret: str | None = None) -> None:
        self._session = session
        self._query_key_secret = (
            query_key_secret if query_key_secret is not None else get_settings().auth_jwt_secret.get_secret_value()
        )

    def _public_query_key(self, query: str) -> str:
        return build_admin_analytics_query_key(query, secret=self._query_key_secret)

    async def ensure_event_volume(self, date_range: AdminAnalyticsDateRange) -> None:
        """Reject ranges whose raw event materialization could exhaust the API process."""

        event_count = await self._session.scalar(
            select(func.count(AnalyticsEvent.id)).where(
                AnalyticsEvent.occurred_at >= date_range.comparison_start_at,
                AnalyticsEvent.occurred_at < date_range.end_exclusive_at,
            ),
        )
        if int(event_count or 0) > MAX_ANALYTICS_EVENT_ROWS:
            raise AdminAnalyticsEventVolumeError(
                "This reporting range contains too many analytics events. Choose a shorter date range.",
            )

    async def get_overview(self, date_range: AdminAnalyticsDateRange) -> AdminAnalyticsOverviewRead:
        events = await self._events_for_comparison(date_range)
        current_events, previous_events = _split_events(events, date_range)
        current_queries = _analyse_search_queries(current_events)
        current_meme_daily, previous_meme_daily = await self._meme_daily_counts(date_range)
        current_catalog = await self._catalog_snapshot(date_range.end_exclusive_at)
        previous_catalog = await self._catalog_snapshot(date_range.start_at)
        source_current, source_previous, _source_daily = await self._source_delta_summary(date_range)
        conversion_current, conversion_previous, _conversion_daily = await self._conversion_summary(
            date_range,
            current_events,
            previous_events,
        )

        current_searches = sum(stats.searches for stats in current_queries.values())
        current_downloads = _count_event_types(current_events, _DOWNLOAD_EVENTS)
        previous_downloads = _count_event_types(previous_events, _DOWNLOAD_EVENTS)
        current_interactions = _count_interactions(current_events)
        previous_interactions = _count_interactions(previous_events)

        activity = []
        for point_date in _date_sequence(date_range.start_date, date_range.end_date):
            day_events = [event for event in current_events if event.occurred_date == point_date]
            day_queries = _analyse_search_queries(day_events)
            activity.append(
                AdminAnalyticsOverviewActivityPointRead(
                    date=point_date,
                    page_views=_count_event_type(day_events, _PAGE_VIEW_EVENT),
                    active_users=_active_user_count(day_events),
                    interactions=_count_interactions(day_events),
                    searches=sum(stats.searches for stats in day_queries.values()),
                    downloads=_count_event_types(day_events, _DOWNLOAD_EVENTS),
                    new_memes=current_meme_daily.get(point_date, 0),
                )
            )

        searches_with_results = sum(
            stats.searches - stats.zero_result_searches for stats in current_queries.values()
        )
        searches_without_results = sum(stats.zero_result_searches for stats in current_queries.values())
        return AdminAnalyticsOverviewRead(
            range=date_range.to_read(),
            metrics={
                "catalog_memes": _metric(current_catalog["memes"], previous_catalog["memes"]),
                "new_memes": _metric(sum(current_meme_daily.values()), sum(previous_meme_daily.values())),
                "page_views": _metric(
                    _count_event_type(current_events, _PAGE_VIEW_EVENT),
                    _count_event_type(previous_events, _PAGE_VIEW_EVENT),
                ),
                "active_users": _metric(_active_user_count(current_events), _active_user_count(previous_events)),
                "interactions": _metric(current_interactions, previous_interactions),
                "downloads": _metric(current_downloads, previous_downloads),
                "guest_to_full_conversions": _metric(conversion_current, conversion_previous),
            },
            activity=activity,
            discovery_funnel=AdminAnalyticsDiscoveryFunnelRead(
                searches=current_searches,
                searches_with_results=searches_with_results,
                searches_without_results=searches_without_results,
                detail_clicks=sum(stats.detail_clicks for stats in current_queries.values()),
                downloads=sum(stats.downloads for stats in current_queries.values()),
            ),
            surface_mix=_surface_mix(current_events),
            source_activity=AdminAnalyticsSourceActivityRead(
                sources=await self._active_source_channel_count(date_range.end_exclusive_at),
                new_sources=await self._count_rows_between(
                    SourceChannel,
                    SourceChannel.created_at,
                    date_range.start_at,
                    date_range.end_exclusive_at,
                ),
                source_views=source_current["source_views"],
                source_reactions=source_current["source_reactions"],
                source_reposts=source_current["source_reposts"],
            ),
        )

    async def get_engagement(self, date_range: AdminAnalyticsDateRange) -> AdminAnalyticsEngagementRead:
        events = await self._events_for_comparison(date_range)
        current_events, previous_events = _split_events(events, date_range)
        current_queries = _analyse_search_queries(current_events)
        previous_queries = _analyse_search_queries(previous_events)

        def query_totals(stats: Mapping[str, _QueryStats]) -> tuple[int, int, float | None]:
            searches = sum(item.searches for item in stats.values())
            zero_results = sum(item.zero_result_searches for item in stats.values())
            latency_total = sum(item.latency_total for item in stats.values())
            latency_count = sum(item.latency_count for item in stats.values())
            average_latency = round(latency_total / latency_count, 2) if latency_count else None
            return searches, zero_results, average_latency

        current_searches, current_zero_results, current_latency = query_totals(current_queries)
        previous_searches, previous_zero_results, previous_latency = query_totals(previous_queries)
        current_average_latency_value = current_latency if current_latency is not None else 0.0
        previous_average_latency_value = previous_latency if previous_latency is not None else 0.0
        activity = []
        for point_date in _date_sequence(date_range.start_date, date_range.end_date):
            day_events = [event for event in current_events if event.occurred_date == point_date]
            day_queries = _analyse_search_queries(day_events)
            activity.append(
                AdminAnalyticsEngagementActivityPointRead(
                    date=point_date,
                    interactions=_count_interactions(day_events),
                    searches=sum(item.searches for item in day_queries.values()),
                    zero_result_searches=sum(item.zero_result_searches for item in day_queries.values()),
                    detail_clicks=_count_event_types(day_events, _DETAIL_CLICK_EVENTS),
                    downloads=_count_event_types(day_events, _DOWNLOAD_EVENTS),
                    sends=_count_event_types(day_events, _SEND_EVENTS),
                    saves=_count_event_types(day_events, _SAVE_EVENTS),
                    shares=_count_event_types(day_events, _SHARE_EVENTS),
                )
            )
        event_type_counts = Counter(event.event_type for event in current_events if _is_interaction(event))
        top_queries = _sort_query_stats(list(current_queries.values()))[:TOP_SEARCH_QUERY_LIMIT]
        return AdminAnalyticsEngagementRead(
            range=date_range.to_read(),
            metrics={
                "interactions": _metric(_count_interactions(current_events), _count_interactions(previous_events)),
                "searches": _metric(current_searches, previous_searches),
                "zero_result_searches": _metric(current_zero_results, previous_zero_results),
                "zero_result_rate": _metric(
                    _percent(current_zero_results, current_searches) or 0.0,
                    _percent(previous_zero_results, previous_searches) or 0.0,
                ),
                "average_search_latency_ms": _metric(current_average_latency_value, previous_average_latency_value),
                "detail_clicks": _metric(
                    _count_event_types(current_events, _DETAIL_CLICK_EVENTS),
                    _count_event_types(previous_events, _DETAIL_CLICK_EVENTS),
                ),
                "downloads": _metric(
                    _count_event_types(current_events, _DOWNLOAD_EVENTS),
                    _count_event_types(previous_events, _DOWNLOAD_EVENTS),
                ),
                "sends": _metric(
                    _count_event_types(current_events, _SEND_EVENTS),
                    _count_event_types(previous_events, _SEND_EVENTS),
                ),
                "saves": _metric(
                    _count_event_types(current_events, _SAVE_EVENTS),
                    _count_event_types(previous_events, _SAVE_EVENTS),
                ),
                "shares": _metric(
                    _count_event_types(current_events, _SHARE_EVENTS),
                    _count_event_types(previous_events, _SHARE_EVENTS),
                ),
            },
            activity=activity,
            interactions_by_type=[
                AdminAnalyticsBreakdownRead(key=event_type, count=count)
                for event_type, count in sorted(event_type_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            surface_mix=_surface_mix(current_events),
            top_search_queries=[item.to_read(query_key=self._public_query_key(item.query)) for item in top_queries],
        )

    async def get_audience(self, date_range: AdminAnalyticsDateRange) -> AdminAnalyticsAudienceRead:
        events = await self._events_for_comparison(date_range)
        current_events, previous_events = _split_events(events, date_range)
        created_users = await self._users_created_between(
            date_range.comparison_start_at,
            date_range.end_exclusive_at,
        )
        created_user_counts = _created_user_counts(created_users)
        current_conversions, previous_conversions, conversion_daily = await self._conversion_summary(
            date_range,
            current_events,
            previous_events,
        )
        current_guest_events = _guest_created_daily_counts(current_events)
        previous_guest_events = _guest_created_daily_counts(previous_events)
        current_full_events = _new_full_account_daily_counts(current_events)
        previous_full_events = _new_full_account_daily_counts(previous_events)
        active_account_types = await self._active_event_account_type_counts(current_events, previous_events)
        retention_cohorts = await self._retention_cohorts(date_range, current_events)

        current_guest_daily = _daily_lifecycle_with_legacy_fallback(
            current_guest_events,
            created_user_counts["guest"],
        )
        previous_guest_daily = _daily_lifecycle_with_legacy_fallback(
            previous_guest_events,
            created_user_counts["guest"],
        )
        current_full_daily = _daily_lifecycle_with_legacy_fallback(
            current_full_events,
            created_user_counts["full"],
        )
        previous_full_daily = _daily_lifecycle_with_legacy_fallback(
            previous_full_events,
            created_user_counts["full"],
        )
        current_new_guests = _sum_daily_counts(current_guest_daily, date_range.start_date, date_range.end_date)
        previous_new_guests = _sum_daily_counts(
            previous_guest_daily,
            date_range.comparison_start_date,
            date_range.comparison_end_date,
        )
        current_new_full = _sum_daily_counts(current_full_daily, date_range.start_date, date_range.end_date)
        previous_new_full = _sum_daily_counts(
            previous_full_daily,
            date_range.comparison_start_date,
            date_range.comparison_end_date,
        )
        activity = []
        for point_date in _date_sequence(date_range.start_date, date_range.end_date):
            day_events = [event for event in current_events if event.occurred_date == point_date]
            activity.append(
                AdminAnalyticsAudienceActivityPointRead(
                    date=point_date,
                    new_guests=current_guest_daily.get(point_date, 0),
                    new_full_accounts=current_full_daily.get(point_date, 0),
                    active_users=_active_user_count(day_events),
                    guest_to_full_conversions=conversion_daily.get(point_date, 0),
                )
            )
        return AdminAnalyticsAudienceRead(
            range=date_range.to_read(),
            metrics={
                "new_guests": _metric(current_new_guests, previous_new_guests),
                "new_full_accounts": _metric(current_new_full, previous_new_full),
                "active_users": _metric(_active_user_count(current_events), _active_user_count(previous_events)),
                "active_guests": _metric(active_account_types["current_guest"], active_account_types["previous_guest"]),
                "active_full_accounts": _metric(
                    active_account_types["current_full"],
                    active_account_types["previous_full"],
                ),
                "guest_to_full_conversions": _metric(current_conversions, previous_conversions),
                "guest_to_full_conversion_rate": _metric(
                    _percent(current_conversions, current_new_guests) or 0.0,
                    _percent(previous_conversions, previous_new_guests) or 0.0,
                ),
            },
            activity=activity,
            surface_mix=_surface_mix(current_events),
            retention_cohorts=retention_cohorts,
        )

    async def get_content(self, date_range: AdminAnalyticsDateRange) -> AdminAnalyticsContentRead:
        current_catalog = await self._catalog_snapshot(date_range.end_exclusive_at)
        previous_catalog = await self._catalog_snapshot(date_range.start_at)
        current_meme_daily, previous_meme_daily = await self._meme_daily_counts(date_range)
        source_current, source_previous, source_daily = await self._source_delta_summary(date_range)
        current_sources = await self._active_source_channel_count(date_range.end_exclusive_at)
        previous_sources = await self._active_source_channel_count(date_range.start_at)
        current_new_sources = await self._count_rows_between(
            SourceChannel,
            SourceChannel.created_at,
            date_range.start_at,
            date_range.end_exclusive_at,
        )
        previous_new_sources = await self._count_rows_between(
            SourceChannel,
            SourceChannel.created_at,
            date_range.comparison_start_at,
            date_range.start_at,
        )
        return AdminAnalyticsContentRead(
            range=date_range.to_read(),
            metrics={
                "catalog_memes": _metric(current_catalog["memes"], previous_catalog["memes"]),
                "new_memes": _metric(sum(current_meme_daily.values()), sum(previous_meme_daily.values())),
                "public_memes": _metric(current_catalog["public_memes"], previous_catalog["public_memes"]),
                "private_memes": _metric(current_catalog["private_memes"], previous_catalog["private_memes"]),
                "nsfw_memes": _metric(current_catalog["nsfw_memes"], previous_catalog["nsfw_memes"]),
                "seo_pages": _metric(current_catalog["seo_pages"], previous_catalog["seo_pages"]),
                "active_sources": _metric(current_sources, previous_sources),
                "new_sources": _metric(current_new_sources, previous_new_sources),
                "source_views": _metric(source_current["source_views"], source_previous["source_views"]),
                "source_reactions": _metric(
                    source_current["source_reactions"],
                    source_previous["source_reactions"],
                ),
                "source_reposts": _metric(source_current["source_reposts"], source_previous["source_reposts"]),
            },
            catalog_growth=[
                AdminAnalyticsCatalogGrowthPointRead(date=point_date, new_memes=current_meme_daily.get(point_date, 0))
                for point_date in _date_sequence(date_range.start_date, date_range.end_date)
            ],
            media_types=await self._meme_breakdown(Meme.media_type, date_range.end_exclusive_at),
            languages=await self._meme_breakdown(Meme.language, date_range.end_exclusive_at),
            visibility=[
                AdminAnalyticsBreakdownRead(key="public", count=current_catalog["public_memes"]),
                AdminAnalyticsBreakdownRead(key="private", count=current_catalog["private_memes"]),
                AdminAnalyticsBreakdownRead(key="nsfw", count=current_catalog["nsfw_memes"]),
            ],
            processing=await self._processing_breakdown(),
            source_health=await self._source_health_breakdown(),
            source_engagement=[
                AdminAnalyticsSourceEngagementPointRead(
                    date=point_date,
                    source_views=source_daily[point_date]["source_views"],
                    source_reactions=source_daily[point_date]["source_reactions"],
                    source_reposts=source_daily[point_date]["source_reposts"],
                )
                for point_date in _date_sequence(date_range.start_date, date_range.end_date)
            ],
        )

    async def get_search_queries(
        self,
        date_range: AdminAnalyticsDateRange,
        *,
        limit: int,
        offset: int,
        sort: Literal["searches", "zero_result_rate", "downloads", "niche"] = "searches",
    ) -> AdminAnalyticsSearchQueryPageRead:
        events = await self._events_between(date_range.start_at, date_range.end_exclusive_at)
        query_stats = _analyse_search_queries(events)
        values = list(query_stats.values())
        if sort == "zero_result_rate":
            values.sort(
                key=lambda item: (-(item.zero_result_rate or 0.0), -item.searches, item.query.casefold()),
            )
        elif sort == "downloads":
            values.sort(key=lambda item: (-item.downloads, -item.searches, item.query.casefold()))
        elif sort == "niche":
            values.sort(
                key=lambda item: (
                    not 1 <= item.searches <= 3,
                    item.searches,
                    item.query.casefold(),
                ),
            )
        else:
            values = _sort_query_stats(values)
        return AdminAnalyticsSearchQueryPageRead(
            range=date_range.to_read(),
            items=[
                item.to_read(query_key=self._public_query_key(item.query))
                for item in values[offset : offset + limit]
            ],
            total=len(values),
            limit=limit,
            offset=offset,
        )

    async def get_search_query_detail(
        self,
        date_range: AdminAnalyticsDateRange,
        *,
        query_key: str,
    ) -> AdminAnalyticsSearchQueryDetailRead:
        events = await self._events_between(date_range.start_at, date_range.end_exclusive_at)
        stats = next(
            (
                candidate
                for candidate in _analyse_search_queries(events).values()
                if hmac.compare_digest(self._public_query_key(candidate.query), query_key)
            ),
            None,
        )
        if stats is None:
            raise AdminAnalyticsQueryKeyNotFoundError("The selected query is not available in this reporting range.")
        outcomes = [
            AdminAnalyticsQueryMemeOutcomeRead(
                meme_id=meme_id,
                interactions=outcome.interactions,
                detail_clicks=outcome.detail_clicks,
                downloads=outcome.downloads,
                saves=outcome.saves,
                shares=outcome.shares,
            )
            for meme_id, outcome in sorted(
                stats.outcomes.items(),
                key=lambda item: (-item[1].interactions, -item[1].downloads, str(item[0])),
            )[:TOP_MEME_OUTCOME_LIMIT]
        ]
        return AdminAnalyticsSearchQueryDetailRead(
            range=date_range.to_read(),
            query=stats.query,
            query_key=query_key,
            searches=stats.searches,
            zero_result_searches=stats.zero_result_searches,
            zero_result_rate=stats.zero_result_rate,
            average_latency_ms=stats.average_latency_ms,
            meme_outcomes=outcomes,
        )

    async def _events_for_comparison(self, date_range: AdminAnalyticsDateRange) -> list[_EventRecord]:
        return await self._events_between(date_range.comparison_start_at, date_range.end_exclusive_at)

    async def _events_between(self, start_at: datetime, end_exclusive_at: datetime) -> list[_EventRecord]:
        result = await self._session.execute(
            select(
                AnalyticsEvent.event_type,
                AnalyticsEvent.payload,
                AnalyticsEvent.user_id,
                AnalyticsEvent.occurred_at,
            )
            .where(
                AnalyticsEvent.occurred_at >= start_at,
                AnalyticsEvent.occurred_at < end_exclusive_at,
            )
            .order_by(AnalyticsEvent.occurred_at.asc(), AnalyticsEvent.id.asc()),
        )
        return [
            _EventRecord(
                event_type=_enum_value(event_type),
                payload=_safe_mapping(payload),
                user_id=user_id,
                occurred_at=occurred_at,
            )
            for event_type, payload, user_id, occurred_at in result.all()
        ]

    async def _catalog_snapshot(self, cutoff: datetime) -> dict[str, int]:
        meme_row = (
            (
                await self._session.execute(
                    select(
                        func.count(Meme.id).label("memes"),
                        func.count(Meme.id).filter(Meme.is_public.is_(True)).label("public_memes"),
                        func.count(Meme.id).filter(Meme.is_public.is_(False)).label("private_memes"),
                        func.count(Meme.id).filter(Meme.is_nsfw.is_(True)).label("nsfw_memes"),
                    ).where(Meme.created_at < cutoff),
                )
            )
            .mappings()
            .one()
        )
        seo_pages = await self._session.scalar(
            select(func.count(MemeSeoPage.meme_id)).where(MemeSeoPage.generated_at < cutoff),
        )
        return {
            "memes": int(meme_row["memes"] or 0),
            "public_memes": int(meme_row["public_memes"] or 0),
            "private_memes": int(meme_row["private_memes"] or 0),
            "nsfw_memes": int(meme_row["nsfw_memes"] or 0),
            "seo_pages": int(seo_pages or 0),
        }

    async def _meme_daily_counts(
        self,
        date_range: AdminAnalyticsDateRange,
    ) -> tuple[dict[date, int], dict[date, int]]:
        result = await self._session.execute(
            select(Meme.created_at).where(
                Meme.created_at >= date_range.comparison_start_at,
                Meme.created_at < date_range.end_exclusive_at,
            ),
        )
        daily: Counter[date] = Counter(_utc_date(created_at) for created_at in result.scalars())
        return (
            {day: daily.get(day, 0) for day in _date_sequence(date_range.start_date, date_range.end_date)},
            {
                day: daily.get(day, 0)
                for day in _date_sequence(date_range.comparison_start_date, date_range.comparison_end_date)
            },
        )

    async def _source_delta_summary(
        self,
        date_range: AdminAnalyticsDateRange,
    ) -> tuple[dict[str, int], dict[str, int], dict[date, dict[str, int]]]:
        """Sum truthful snapshot-to-snapshot Telegram metric deltas by UTC day."""

        baseline_snapshots = (
            select(
                MemeSourceEngagementSnapshot.id.label("snapshot_id"),
                func.row_number()
                .over(
                    partition_by=MemeSourceEngagementSnapshot.meme_source_id,
                    order_by=(
                        MemeSourceEngagementSnapshot.captured_at.desc(),
                        MemeSourceEngagementSnapshot.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .join(MemeSource, MemeSource.id == MemeSourceEngagementSnapshot.meme_source_id)
            .where(
                MemeSource.platform == SourcePlatform.TELEGRAM,
                MemeSourceEngagementSnapshot.fetch_status == SourceEngagementFetchStatus.SUCCESS,
                MemeSourceEngagementSnapshot.captured_at < date_range.comparison_start_at,
            )
            .subquery()
        )
        baseline_ids = select(baseline_snapshots.c.snapshot_id).where(baseline_snapshots.c.rank == 1)
        result = await self._session.execute(
            select(
                MemeSourceEngagementSnapshot.meme_source_id,
                MemeSourceEngagementSnapshot.captured_at,
                MemeSourceEngagementSnapshot.view_count,
                MemeSourceEngagementSnapshot.reaction_count,
                MemeSourceEngagementSnapshot.forward_count,
            )
            .join(MemeSource, MemeSource.id == MemeSourceEngagementSnapshot.meme_source_id)
            .where(
                MemeSource.platform == SourcePlatform.TELEGRAM,
                MemeSourceEngagementSnapshot.fetch_status == SourceEngagementFetchStatus.SUCCESS,
                MemeSourceEngagementSnapshot.captured_at < date_range.end_exclusive_at,
                or_(
                    MemeSourceEngagementSnapshot.captured_at >= date_range.comparison_start_at,
                    MemeSourceEngagementSnapshot.id.in_(baseline_ids),
                ),
            )
            .order_by(
                MemeSourceEngagementSnapshot.meme_source_id.asc(),
                MemeSourceEngagementSnapshot.captured_at.asc(),
                MemeSourceEngagementSnapshot.id.asc(),
            ),
        )
        previous_values: dict[uuid.UUID, tuple[int | None, int | None, int | None]] = {}
        daily: dict[date, dict[str, int]] = defaultdict(
            lambda: {"source_views": 0, "source_reactions": 0, "source_reposts": 0},
        )
        for source_id, captured_at, views, reactions, reposts in result.all():
            previous = previous_values.get(source_id)
            current_values = (_as_int_or_none(views), _as_int_or_none(reactions), _as_int_or_none(reposts))
            previous_values[source_id] = (
                current_values
                if previous is None
                else (
                    _counter_high_watermark(current_values[0], previous[0]),
                    _counter_high_watermark(current_values[1], previous[1]),
                    _counter_high_watermark(current_values[2], previous[2]),
                )
            )
            point_date = _utc_date(captured_at)
            if previous is None or point_date < date_range.comparison_start_date:
                continue
            deltas = (
                _counter_delta(current_values[0], previous[0]),
                _counter_delta(current_values[1], previous[1]),
                _counter_delta(current_values[2], previous[2]),
            )
            row = daily[point_date]
            row["source_views"] += deltas[0]
            row["source_reactions"] += deltas[1]
            row["source_reposts"] += deltas[2]

        def sum_period(start: date, end: date) -> dict[str, int]:
            return {
                key: sum(daily[point_date][key] for point_date in _date_sequence(start, end))
                for key in ("source_views", "source_reactions", "source_reposts")
            }

        current_daily = {
            point_date: daily[point_date]
            for point_date in _date_sequence(date_range.start_date, date_range.end_date)
        }
        return (
            sum_period(date_range.start_date, date_range.end_date),
            sum_period(date_range.comparison_start_date, date_range.comparison_end_date),
            current_daily,
        )

    async def _conversion_summary(
        self,
        date_range: AdminAnalyticsDateRange,
        current_events: Sequence[_EventRecord],
        previous_events: Sequence[_EventRecord],
    ) -> tuple[int, int, dict[date, int]]:
        merge_logs = list(
            (
                await self._session.execute(
                    select(AccountMergeLog.id, AccountMergeLog.created_at).where(
                        AccountMergeLog.created_at >= date_range.comparison_start_at,
                        AccountMergeLog.created_at < date_range.end_exclusive_at,
                    ),
                )
            ).all()
        )
        events = [*previous_events, *current_events]
        event_records: list[tuple[date, str | uuid.UUID]] = []
        claimed_merge_logs: set[uuid.UUID] = set()
        seen_conversion_keys: set[str | uuid.UUID] = set()
        for event in events:
            refs = _payload_refs(event.payload)
            merge_log_id = _parse_uuid(refs.get("account_merge_log_id"))
            if merge_log_id is not None:
                claimed_merge_logs.add(merge_log_id)
            if not _is_persistent_guest_conversion_event(event):
                continue
            key = (
                _parse_uuid(refs.get("source_user_id"))
                or merge_log_id
                or f"{event.occurred_at.isoformat()}:{len(event_records)}"
            )
            if key in seen_conversion_keys:
                continue
            seen_conversion_keys.add(key)
            event_records.append((event.occurred_date, key))
        for merge_log_id, created_at in merge_logs:
            if merge_log_id not in claimed_merge_logs:
                event_records.append((_utc_date(created_at), merge_log_id))
        daily: Counter[date] = Counter(point_date for point_date, _key in event_records)
        return (
            sum(daily[point_date] for point_date in _date_sequence(date_range.start_date, date_range.end_date)),
            sum(
                daily[point_date]
                for point_date in _date_sequence(date_range.comparison_start_date, date_range.comparison_end_date)
            ),
            dict(daily),
        )

    async def _users_created_between(
        self,
        start_at: datetime,
        end_exclusive_at: datetime,
    ) -> list[tuple[datetime, str]]:
        result = await self._session.execute(
            select(User.created_at, User.account_type).where(
                User.created_at >= start_at,
                User.created_at < end_exclusive_at,
            ),
        )
        return [(created_at, _enum_value(account_type)) for created_at, account_type in result.all()]

    async def _active_event_account_type_counts(
        self,
        current_events: Sequence[_EventRecord],
        previous_events: Sequence[_EventRecord],
    ) -> dict[str, int]:
        """Classify active accounts from strict event-time state with a legacy lookup fallback."""

        current_guest_ids, current_full_ids, current_fallback_ids = _event_account_type_sets(current_events)
        previous_guest_ids, previous_full_ids, previous_fallback_ids = _event_account_type_sets(previous_events)
        fallback_user_ids = current_fallback_ids | previous_fallback_ids
        if not fallback_user_ids:
            return {
                "current_guest": len(current_guest_ids),
                "current_full": len(current_full_ids),
                "previous_guest": len(previous_guest_ids),
                "previous_full": len(previous_full_ids),
            }
        result = await self._session.execute(
            select(User.id, User.account_type).where(User.id.in_(fallback_user_ids)),
        )
        legacy_account_types = {user_id: _enum_value(account_type) for user_id, account_type in result.all()}

        def apply_legacy_fallback(
            guest_ids: set[uuid.UUID],
            full_ids: set[uuid.UUID],
            fallback_ids: set[uuid.UUID],
        ) -> None:
            for user_id in fallback_ids:
                account_type = legacy_account_types.get(user_id)
                if account_type == "guest":
                    guest_ids.add(user_id)
                elif account_type == "full":
                    full_ids.add(user_id)

        apply_legacy_fallback(current_guest_ids, current_full_ids, current_fallback_ids)
        apply_legacy_fallback(previous_guest_ids, previous_full_ids, previous_fallback_ids)
        return {
            "current_guest": len(current_guest_ids),
            "current_full": len(current_full_ids),
            "previous_guest": len(previous_guest_ids),
            "previous_full": len(previous_full_ids),
        }

    async def _retention_cohorts(
        self,
        date_range: AdminAnalyticsDateRange,
        current_events: Sequence[_EventRecord],
    ) -> list[AdminAnalyticsRetentionCohortRead]:
        """Build mature D1/D7/D30 cohorts from merge-stable creation identities."""

        today = _utc_date(utcnow())
        result = await self._session.execute(
            select(User.id, User.created_at).where(
                User.created_at >= date_range.start_at,
                User.created_at < date_range.end_exclusive_at,
            ),
        )
        created_user_rows = [(user_id, created_at) for user_id, created_at in result.all()]
        cohorts = _retention_cohort_members_by_date(
            current_events,
            created_user_rows,
            today=today,
        )
        if not cohorts:
            return []
        activity_user_ids = {
            activity_user_id
            for cohort_members in cohorts.values()
            for activity_user_id in cohort_members.values()
        }
        first_activity_at = datetime.combine(min(cohorts) + timedelta(days=1), time.min, tzinfo=UTC)
        last_needed_date = min(today, max(cohorts) + timedelta(days=30))
        activity_end_exclusive = datetime.combine(last_needed_date + timedelta(days=1), time.min, tzinfo=UTC)
        activity_result = await self._session.execute(
            select(AnalyticsEvent.user_id, AnalyticsEvent.occurred_at).where(
                AnalyticsEvent.user_id.in_(activity_user_ids),
                AnalyticsEvent.occurred_at >= first_activity_at,
                AnalyticsEvent.occurred_at < activity_end_exclusive,
            ),
        )
        active_by_date: dict[date, set[uuid.UUID]] = defaultdict(set)
        for user_id, occurred_at in activity_result.all():
            if user_id is not None:
                active_by_date[_utc_date(occurred_at)].add(user_id)

        reads: list[AdminAnalyticsRetentionCohortRead] = []
        for cohort_date, cohort_members in sorted(cohorts.items()):
            def retention(
                days: int,
                *,
                resolved_cohort_date: date = cohort_date,
                resolved_cohort_members: Mapping[uuid.UUID, uuid.UUID] = cohort_members,
            ) -> AdminAnalyticsRetentionPeriodRead | None:
                target_date = resolved_cohort_date + timedelta(days=days)
                if target_date >= today:
                    return None
                active_user_ids = active_by_date.get(target_date, set())
                retained = sum(
                    activity_user_id in active_user_ids
                    for activity_user_id in resolved_cohort_members.values()
                )
                return AdminAnalyticsRetentionPeriodRead(
                    eligible_users=len(resolved_cohort_members),
                    retained_users=retained,
                    rate=_percent(retained, len(resolved_cohort_members)),
                )

            reads.append(
                AdminAnalyticsRetentionCohortRead(
                    cohort_date=cohort_date,
                    cohort_size=len(cohort_members),
                    d1=retention(1),
                    d7=retention(7),
                    d30=retention(30),
                )
            )
        return reads

    async def _meme_breakdown(self, column: Any, cutoff: datetime) -> list[AdminAnalyticsBreakdownRead]:
        result = await self._session.execute(
            select(column, func.count(Meme.id)).where(Meme.created_at < cutoff).group_by(column),
        )
        return [
            AdminAnalyticsBreakdownRead(key=_enum_value(value), count=int(count or 0))
            for value, count in sorted(result.all(), key=lambda item: (-int(item[1] or 0), _enum_value(item[0])))
        ]

    async def _processing_breakdown(self) -> list[AdminAnalyticsBreakdownRead]:
        result = await self._session.execute(select(MemeFile.status, func.count(MemeFile.id)).group_by(MemeFile.status))
        return [
            AdminAnalyticsBreakdownRead(key=_enum_value(value), count=int(count or 0))
            for value, count in sorted(result.all(), key=lambda item: (-int(item[1] or 0), _enum_value(item[0])))
        ]

    async def _source_health_breakdown(self) -> list[AdminAnalyticsBreakdownRead]:
        result = await self._session.execute(
            select(
                SourceChannel.platform,
                SourceChannel.is_active,
                SourceChannel.is_paused,
                SourceChannel.telegram_session_id,
                SourceChannel.last_fetched_at,
            ),
        )
        now = utcnow()
        stale_before = now - SOURCE_STALE_AFTER
        counts: Counter[str] = Counter()
        for platform, is_active, is_paused, telegram_session_id, last_fetched_at in result.all():
            if not is_active:
                counts["inactive"] += 1
            elif is_paused:
                counts["paused"] += 1
            elif _enum_value(platform) == SourcePlatform.TELEGRAM.value and telegram_session_id is None:
                counts["orphaned"] += 1
            elif last_fetched_at is None or _as_utc(last_fetched_at) < stale_before:
                counts["stale"] += 1
            else:
                counts["healthy"] += 1
        return [
            AdminAnalyticsBreakdownRead(key=key, count=counts.get(key, 0))
            for key in ("healthy", "stale", "orphaned", "paused", "inactive")
        ]

    async def _active_source_channel_count(self, cutoff: datetime) -> int:
        count = await self._session.scalar(
            select(func.count(SourceChannel.id)).where(
                SourceChannel.created_at < cutoff,
                SourceChannel.is_active.is_(True),
                SourceChannel.is_paused.is_(False),
            ),
        )
        return int(count or 0)

    async def _count_rows_between(
        self,
        model: type[object],
        column: Any,
        start_at: datetime,
        end_exclusive_at: datetime,
    ) -> int:
        count = await self._session.scalar(
            select(func.count()).select_from(model).where(column >= start_at, column < end_exclusive_at),
        )
        return int(count or 0)


def _split_events(
    events: Sequence[_EventRecord],
    date_range: AdminAnalyticsDateRange,
) -> tuple[list[_EventRecord], list[_EventRecord]]:
    current = [
        event
        for event in events
        if date_range.start_date <= event.occurred_date <= date_range.end_date
    ]
    previous = [
        event
        for event in events
        if date_range.comparison_start_date <= event.occurred_date <= date_range.comparison_end_date
    ]
    return current, previous


def _analyse_search_queries(events: Sequence[_EventRecord]) -> dict[str, _QueryStats]:
    """Group raw queries and associate action events by query/request attribution."""

    stats_by_key: dict[str, _QueryStats] = {}
    request_to_query: dict[str, str] = {}
    for event in events:
        if event.event_type != _SEARCH_EVENT:
            continue
        query = _payload_query(event.payload)
        if query is None:
            continue
        key = _query_group_key(query)
        stats = stats_by_key.setdefault(key, _QueryStats(query=query))
        stats.searches += 1
        if _search_is_zero_result(event.payload):
            stats.zero_result_searches += 1
        latency = _payload_number(event.payload, "latency_ms")
        if latency is not None:
            stats.latency_total += latency
            stats.latency_count += 1
        request_id = _payload_request_id(event.payload)
        if request_id is not None:
            stats.request_ids.add(request_id)
            request_to_query[request_id] = key

    if not stats_by_key:
        return stats_by_key
    for event in events:
        if not _is_interaction(event):
            continue
        request_id = _payload_request_id(event.payload)
        key = request_to_query.get(request_id) if request_id is not None else None
        if key is None or key not in stats_by_key:
            continue
        stats = stats_by_key[key]
        if event.event_type in _DETAIL_CLICK_EVENTS:
            stats.detail_clicks += 1
        if event.event_type in _DOWNLOAD_EVENTS:
            stats.downloads += 1
        meme_id = _payload_meme_id(event.payload)
        if meme_id is None:
            continue
        outcome = stats.outcomes.setdefault(meme_id, _MemeOutcome())
        outcome.interactions += 1
        if event.event_type in _DETAIL_CLICK_EVENTS:
            outcome.detail_clicks += 1
        if event.event_type in _DOWNLOAD_EVENTS:
            outcome.downloads += 1
        if event.event_type in _SAVE_EVENTS:
            outcome.saves += 1
        if event.event_type in _SHARE_EVENTS:
            outcome.shares += 1
    return stats_by_key


def _sort_query_stats(values: Sequence[_QueryStats]) -> list[_QueryStats]:
    return sorted(values, key=lambda item: (-item.searches, -item.downloads, item.query.casefold()))


def _metric(value: int | float, previous_value: int | float) -> AdminAnalyticsMetricRead:
    change = value - previous_value
    return AdminAnalyticsMetricRead(
        value=value,
        previous_value=previous_value,
        change=change,
        change_percent=_percent(change, previous_value),
    )


def _percent(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _count_event_type(events: Sequence[_EventRecord], event_type: str) -> int:
    return sum(event.event_type == event_type for event in events)


def _count_event_types(events: Sequence[_EventRecord], event_types: frozenset[str]) -> int:
    return sum(event.event_type in event_types for event in events)


def _is_interaction(event: _EventRecord) -> bool:
    return event.event_type not in _NON_INTERACTION_EVENTS


def _count_interactions(events: Sequence[_EventRecord]) -> int:
    return sum(_is_interaction(event) for event in events)


def _active_user_count(events: Sequence[_EventRecord]) -> int:
    return len({event.user_id for event in events if event.user_id is not None})


def _surface_mix(events: Sequence[_EventRecord]) -> list[AdminAnalyticsSurfaceRead]:
    counts = Counter(
        surface
        for event in events
        if (surface := _payload_surface(event.payload)) is not None
    )
    return [
        AdminAnalyticsSurfaceRead(surface=surface, count=count)
        for surface, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _created_user_counts(created_users: Sequence[tuple[datetime, str]]) -> dict[str, Counter[date]]:
    counts: dict[str, Counter[date]] = {"guest": Counter(), "full": Counter()}
    for created_at, account_type in created_users:
        if account_type in counts:
            counts[account_type][_utc_date(created_at)] += 1
    return counts


def _event_account_type_sets(
    events: Sequence[_EventRecord],
) -> tuple[set[uuid.UUID], set[uuid.UUID], set[uuid.UUID]]:
    """Return strict guest/full sets plus user ids requiring a legacy current-state fallback."""

    guest_ids: set[uuid.UUID] = set()
    full_ids: set[uuid.UUID] = set()
    fallback_ids: set[uuid.UUID] = set()
    for event in events:
        if event.user_id is None:
            continue
        actor_account_type = _payload_actor_account_type(event.payload)
        if actor_account_type == "guest":
            guest_ids.add(event.user_id)
        elif actor_account_type == "full":
            full_ids.add(event.user_id)
        else:
            fallback_ids.add(event.user_id)
    return guest_ids, full_ids, fallback_ids


def _guest_created_daily_counts(events: Sequence[_EventRecord]) -> Counter[date]:
    return Counter(
        event.occurred_date
        for event in events
        if event.event_type == _AUTH_EVENT and _payload_action(event.payload) == "guest_created"
    )


def _new_full_account_daily_counts(events: Sequence[_EventRecord]) -> Counter[date]:
    """Count successful in-place/new account upgrades, never merges into existing accounts."""

    return Counter(
        event.occurred_date
        for event in events
        if _is_full_account_creation_event(event)
    )


def _daily_lifecycle_with_legacy_fallback(
    lifecycle_counts: Mapping[date, int],
    user_counts: Mapping[date, int],
) -> Counter[date]:
    """Use lifecycle facts per day, falling back only on days without forward telemetry."""

    return Counter(
        {
            point_date: lifecycle_counts.get(point_date, user_counts.get(point_date, 0))
            for point_date in lifecycle_counts.keys() | user_counts.keys()
        }
    )


def _retention_cohort_members_by_date(
    events: Sequence[_EventRecord],
    created_users: Sequence[tuple[uuid.UUID, datetime]],
    *,
    today: date,
) -> dict[date, dict[uuid.UUID, uuid.UUID]]:
    """Map immutable cohort identities to their current activity identities."""

    cohorts: dict[date, dict[uuid.UUID, uuid.UUID]] = defaultdict(dict)
    lifecycle_cohort_ids: set[uuid.UUID] = set()
    seen_cohort_ids: set[uuid.UUID] = set()
    for event in events:
        if event.event_type != _AUTH_EVENT or _payload_action(event.payload) != "guest_created":
            continue
        cohort_user_id = _parse_uuid(_payload_refs(event.payload).get("source_user_id"))
        if cohort_user_id is None:
            continue
        activity_user_id = event.user_id or cohort_user_id
        lifecycle_cohort_ids.add(cohort_user_id)
        if cohort_user_id in seen_cohort_ids:
            continue
        seen_cohort_ids.add(cohort_user_id)
        cohort_date = event.occurred_date
        if cohort_date + timedelta(days=1) < today:
            cohorts[cohort_date][cohort_user_id] = activity_user_id

    for user_id, created_at in created_users:
        if user_id in lifecycle_cohort_ids:
            continue
        cohort_date = _utc_date(created_at)
        if cohort_date + timedelta(days=1) < today:
            cohorts[cohort_date][user_id] = user_id
    return dict(cohorts)


def _sum_daily_counts(counts: Mapping[date, int], start_date: date, end_date: date) -> int:
    return sum(counts.get(point_date, 0) for point_date in _date_sequence(start_date, end_date))


def _payload_value(payload: Mapping[str, object], key: str) -> object | None:
    value = payload.get(key)
    if value is not None:
        return value
    properties = payload.get("properties")
    if isinstance(properties, Mapping):
        return properties.get(key)
    return None


def _payload_string(payload: Mapping[str, object], key: str) -> str | None:
    value = _payload_value(payload, key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _payload_bool(payload: Mapping[str, object], key: str) -> bool | None:
    value = _payload_value(payload, key)
    return value if isinstance(value, bool) else None


def _payload_number(payload: Mapping[str, object], key: str) -> float | None:
    value = _payload_value(payload, key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _payload_query(payload: Mapping[str, object]) -> str | None:
    return _normalize_query(_payload_string(payload, "query"))


def _payload_request_id(payload: Mapping[str, object]) -> str | None:
    return _payload_string(payload, "request_id")


def _payload_surface(payload: Mapping[str, object]) -> str | None:
    return _payload_string(payload, "surface")


def _payload_action(payload: Mapping[str, object]) -> str | None:
    return _payload_string(payload, "action")


def _payload_actor_account_type(payload: Mapping[str, object]) -> str | None:
    """Read strict event-time actor state; legacy payloads deliberately return None."""

    value = payload.get("actor_account_type")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized in {"guest", "full"} else None


def _payload_refs(payload: Mapping[str, object]) -> Mapping[str, object]:
    refs = payload.get("refs")
    return cast("Mapping[str, object]", refs) if isinstance(refs, Mapping) else {}


def _payload_meme_id(payload: Mapping[str, object]) -> uuid.UUID | None:
    flat_meme_id = _parse_uuid(payload.get("meme_id"))
    if flat_meme_id is not None:
        return flat_meme_id
    return _parse_uuid(_payload_refs(payload).get("meme_id"))


def _search_is_zero_result(payload: Mapping[str, object]) -> bool:
    explicit_value = _payload_value(payload, "zero_result")
    if isinstance(explicit_value, bool):
        return explicit_value
    explicit_value = _payload_value(payload, "zero_results")
    if isinstance(explicit_value, bool):
        return explicit_value
    for key in ("result_total", "total_results", "result_count", "returned_count"):
        count = _payload_number(payload, key)
        if count is not None:
            return count <= 0
    return False


def _is_persistent_guest_conversion_event(event: _EventRecord) -> bool:
    if event.event_type != _AUTH_EVENT or _payload_action(event.payload) not in _CONVERSION_ACTIONS:
        return False
    # Historical guest-upgrade events predate this bit and all represented a
    # persisted guest route, so retain them as conversions.
    return _payload_bool(event.payload, "guest_was_persistent") is not False


def _is_full_account_creation_event(event: _EventRecord) -> bool:
    if event.event_type != _AUTH_EVENT or _payload_action(event.payload) not in _CONVERSION_ACTIONS:
        return False
    explicit = _payload_bool(event.payload, "full_account_created")
    if explicit is not None:
        return explicit
    # Bot and historical lifecycle writes expose merge_performed; a merge has
    # no new canonical full account even though it remains a conversion.
    return _payload_bool(event.payload, "merge_performed") is not True


def _normalize_query(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _query_group_key(query: str) -> str:
    return query.casefold()


def build_admin_analytics_query_key(query: str, *, secret: str) -> str:
    """Return a stable, domain-separated opaque identifier for an admin raw query."""

    normalized_query = _normalize_query(query)
    if normalized_query is None:
        raise ValueError("query must not be blank")
    return hmac.new(
        secret.encode("utf-8"),
        _QUERY_KEY_DOMAIN + _query_group_key(normalized_query).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _safe_mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _parse_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _enum_value(value: object) -> str:
    raw_value = getattr(value, "value", value)
    return raw_value if isinstance(raw_value, str) else str(raw_value)


def _utc_date(value: datetime) -> date:
    return _as_utc(value).date()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _counter_delta(current: int | None, previous: int | None) -> int:
    if current is None or previous is None:
        return 0
    return max(current - previous, 0)


def _counter_high_watermark(current: int | None, previous: int | None) -> int | None:
    if current is None:
        return previous
    if previous is None:
        return current
    return max(current, previous)


def _date_sequence(start_date: date, end_date: date) -> list[date]:
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


__all__ = [
    "AdminAnalyticsDateRange",
    "AdminAnalyticsDateRangeError",
    "AdminAnalyticsEventVolumeError",
    "AdminAnalyticsQueryKeyNotFoundError",
    "AdminAnalyticsService",
    "DEFAULT_ANALYTICS_RANGE_DAYS",
    "MAX_ANALYTICS_RANGE_DAYS",
    "MAX_ANALYTICS_EVENT_ROWS",
    "QUERY_PAGE_LIMIT",
    "build_admin_analytics_query_key",
    "resolve_admin_analytics_date_range",
]
