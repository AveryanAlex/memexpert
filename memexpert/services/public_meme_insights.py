"""Privacy-bounded public source provenance and professional meme analytics."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from memexpert.models.base import utcnow
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
    IngestSourceKind,
    SourceChannelAudienceFetchStatus,
    SourceEngagementFetchStatus,
    SourcePlatform,
)
from memexpert.models.user import AnalyticsEvent, MemeExposure
from memexpert.schemas.meme import (
    PublicMemeActivityCountsRead,
    PublicMemeActivityPointRead,
    PublicMemeAnalyticsGranularity,
    PublicMemeAnalyticsMomentumRead,
    PublicMemeAnalyticsPeakRead,
    PublicMemeAnalyticsRead,
    PublicMemeAnalyticsSummaryRead,
    PublicMemeAnalyticsWindow,
    PublicMemeChannelAudienceChangeRead,
    PublicMemeExposureFunnelsRead,
    PublicMemeInlineExposureFunnelRead,
    PublicMemeMetricCoverageRead,
    PublicMemeObservedSourcePointRead,
    PublicMemeObservedSourceSeriesRead,
    PublicMemeSourceAudienceRead,
    PublicMemeSourceAudienceSummaryRead,
    PublicMemeSourceCoverageRead,
    PublicMemeSourcePageRead,
    PublicMemeSourcePostRead,
    PublicMemeSourceRateRead,
    PublicMemeSourceRatesRead,
    PublicMemeSourceSort,
    PublicMemeSourceSummaryRead,
    PublicMemeSourceTotalsRead,
    PublicMemeWebExposureFunnelRead,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

_TELEGRAM_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_TELEGRAM_POST_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
_PUBLISH_AUDIENCE_MAX_AGE = timedelta(hours=48)
_FIXED_WINDOW_DAYS: dict[PublicMemeAnalyticsWindow, int] = {
    PublicMemeAnalyticsWindow.SEVEN_DAYS: 7,
    PublicMemeAnalyticsWindow.THIRTY_DAYS: 30,
    PublicMemeAnalyticsWindow.NINETY_DAYS: 90,
}
_MEMEEXPERT_VIEW_EVENTS = frozenset({AnalyticsEventType.MEME_VIEW, AnalyticsEventType.VIEW})
_MEMEEXPERT_SEND_EVENTS = frozenset(
    {
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.SHARE,
    }
)
_MEMEEXPERT_SAVE_EVENTS = frozenset({AnalyticsEventType.MEME_SAVE, AnalyticsEventType.SAVE})
_MEMEEXPERT_FAVORITE_EVENTS = frozenset({AnalyticsEventType.MEME_LIKE, AnalyticsEventType.FAVORITE})
_ACTIVITY_EVENT_TYPES = frozenset(
    {
        *_MEMEEXPERT_VIEW_EVENTS,
        *_MEMEEXPERT_SEND_EVENTS,
        *_MEMEEXPERT_SAVE_EVENTS,
        *_MEMEEXPERT_FAVORITE_EVENTS,
        AnalyticsEventType.MEME_DOWNLOAD,
    }
)
_WEB_HIGH_INTENT_EVENTS = frozenset(
    {
        AnalyticsEventType.FAVORITE,
        AnalyticsEventType.MEME_DOWNLOAD,
        AnalyticsEventType.MEME_LIKE,
        AnalyticsEventType.MEME_SAVE,
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.SAVE,
        AnalyticsEventType.SHARE,
    }
)
_FUNNEL_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.IMPRESSION,
        AnalyticsEventType.MEME_IMPRESSION,
        AnalyticsEventType.CLICK,
        AnalyticsEventType.MEME_DETAIL_CLICK,
        AnalyticsEventType.INLINE_SERVED,
        AnalyticsEventType.INLINE_CHOSEN,
        AnalyticsEventType.INLINE_SENT,
        *_WEB_HIGH_INTENT_EVENTS,
    }
)


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    id: uuid.UUID
    source_key: str
    post_id: str
    source_alive: bool
    published_at: datetime | None
    created_at: datetime
    channel_id: uuid.UUID | None
    channel_title: str
    channel_username: str | None


@dataclass(frozen=True, slots=True)
class _EngagementObservation:
    source_id: uuid.UUID
    captured_at: datetime
    views: int | None
    reactions: int | None
    comments: int | None
    reposts: int | None


@dataclass(frozen=True, slots=True)
class _AudienceObservation:
    channel_id: uuid.UUID
    captured_at: datetime
    subscribers: int


@dataclass(frozen=True, slots=True)
class _SourceMetric:
    source: _SourceRecord
    latest: _EngagementObservation | None
    audience_at_publish: int | None
    current_audience: int | None


@dataclass(slots=True)
class _ActivityCounts:
    source_views: int = 0
    source_reactions: int = 0
    source_reposts: int = 0
    memeexpert_views: int = 0
    memeexpert_sends: int = 0
    memeexpert_saves: int = 0
    memeexpert_favorites: int = 0
    downloads: int = 0

    @property
    def recorded_activity(self) -> int:
        return (
            self.source_views
            + self.source_reactions
            + self.source_reposts
            + self.memeexpert_views
            + self.memeexpert_sends
            + self.memeexpert_saves
            + self.memeexpert_favorites
        )

    def add(self, other: _ActivityCounts) -> None:
        self.source_views += other.source_views
        self.source_reactions += other.source_reactions
        self.source_reposts += other.source_reposts
        self.memeexpert_views += other.memeexpert_views
        self.memeexpert_sends += other.memeexpert_sends
        self.memeexpert_saves += other.memeexpert_saves
        self.memeexpert_favorites += other.memeexpert_favorites
        self.downloads += other.downloads

    def to_read(self) -> PublicMemeActivityCountsRead:
        return PublicMemeActivityCountsRead(
            source_views=self.source_views,
            source_reactions=self.source_reactions,
            source_reposts=self.source_reposts,
            memeexpert_views=self.memeexpert_views,
            memeexpert_sends=self.memeexpert_sends,
            memeexpert_saves=self.memeexpert_saves,
            memeexpert_favorites=self.memeexpert_favorites,
            downloads=self.downloads,
            recorded_activity=self.recorded_activity,
        )


@dataclass(frozen=True, slots=True)
class _TimedActivity:
    observed_at: datetime
    counts: _ActivityCounts


@dataclass(frozen=True, slots=True)
class _Bucket:
    start: datetime
    end: datetime
    granularity: PublicMemeAnalyticsGranularity


class PublicMemeInsightsService:
    """Build source and analytics DTOs without exposing ingestion internals."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def source_page(
        self,
        meme_id: uuid.UUID,
        *,
        sort: PublicMemeSourceSort = PublicMemeSourceSort.VIEWS_DESC,
        limit: int = 20,
        offset: int = 0,
        snapshot_at: datetime | None = None,
        include_nsfw: bool = False,
    ) -> PublicMemeSourcePageRead | None:
        """Return latest public Telegram observations at one stable cutoff."""

        meme = await self._load_visible_public_meme(meme_id, include_nsfw=include_nsfw)
        if meme is None:
            return None

        resolved_snapshot_at = min(_normalize_utc(snapshot_at), utcnow())
        sources = await self._load_sources(meme_id, cutoff=resolved_snapshot_at)
        engagement = await self._load_latest_engagement_observations(sources, cutoff=resolved_snapshot_at)
        audience, audience_at_publish = await self._load_source_page_audience(
            meme_id,
            sources,
            cutoff=resolved_snapshot_at,
        )
        metrics = _build_source_metrics(
            sources,
            engagement=engagement,
            audience=audience,
            audience_at_publish_by_source=audience_at_publish,
        )
        sorted_metrics = sorted(metrics, key=lambda metric: _source_sort_key(metric, sort=sort))
        resolved_limit = max(1, min(limit, 100))
        resolved_offset = max(0, offset)
        items = [
            _source_post_read(metric)
            for metric in sorted_metrics[resolved_offset : resolved_offset + resolved_limit]
        ]
        return PublicMemeSourcePageRead(
            meme_id=meme_id,
            snapshot_at=resolved_snapshot_at,
            sort=sort,
            items=items,
            summary=_source_summary(metrics),
            limit=resolved_limit,
            offset=resolved_offset,
            total=len(metrics),
            has_more=resolved_offset + resolved_limit < len(metrics),
        )

    async def analytics(
        self,
        meme_id: uuid.UUID,
        *,
        window: PublicMemeAnalyticsWindow = PublicMemeAnalyticsWindow.THIRTY_DAYS,
        include_nsfw: bool = False,
    ) -> PublicMemeAnalyticsRead | None:
        """Return exact activity, absolute source counters, and matched-token funnels."""

        meme = await self._load_visible_public_meme(meme_id, include_nsfw=include_nsfw)
        if meme is None:
            return None

        refreshed_at = utcnow()
        sources = await self._load_sources(meme_id, cutoff=refreshed_at)
        engagement = await self._load_engagement_observations(sources, cutoff=refreshed_at)
        audience = await self._load_audience_observations(sources, cutoff=refreshed_at)
        event_history_start, event_history_end = await self._load_event_history_bounds(meme_id, end_at=refreshed_at)
        source_history_start = engagement[0].captured_at if engagement else None
        source_history_end = engagement[-1].captured_at if engagement else None
        history_start = _minimum_datetime(event_history_start, source_history_start)
        history_end = _maximum_datetime(event_history_end, source_history_end)
        start_at = _analytics_start(window, now=refreshed_at, history_start=history_start)
        momentum_start = _midnight_utc(refreshed_at) - timedelta(days=13)
        activity_load_start = min(start_at, momentum_start)
        event_daily = await self._load_event_daily_activity(
            meme_id,
            start_at=activity_load_start,
            end_at=refreshed_at,
        )
        timed_activity = [
            *_source_timed_activity(engagement, start_at=activity_load_start),
            *event_daily,
        ]
        timed_activity.sort(key=lambda item: item.observed_at)
        buckets = _analytics_buckets(start_at, refreshed_at, window=window, now=refreshed_at)
        activity_points = _activity_points(buckets, timed_activity, window=window, now=refreshed_at)
        totals = _sum_activity_points(activity_points)
        recent_start = _midnight_utc(refreshed_at) - timedelta(days=6)
        previous_start = recent_start - timedelta(days=7)
        recent_activity = _activity_in_interval(timed_activity, recent_start, refreshed_at)
        previous_activity = _activity_in_interval(timed_activity, previous_start, recent_start)
        momentum = _momentum_read(recent_activity, previous_activity)
        peak = _peak_read(activity_points)
        duration_days = max((refreshed_at - start_at).total_seconds() / 86400.0, 1.0)
        source_metrics = _build_source_metrics(sources, engagement=engagement, audience=audience)
        observed_source = _observed_source_series(
            sources,
            engagement,
            start_at=start_at,
            end_at=refreshed_at,
            window=window,
            now=refreshed_at,
        )
        audience_change = _audience_change(
            sources,
            audience,
            start_at=start_at,
            end_at=refreshed_at,
        )
        funnel = await self._exposure_funnels(meme_id, start_at=start_at, end_at=refreshed_at)
        usable_activity_points = sum(point.recorded_activity > 0 for point in activity_points)
        usable_source_points = len(observed_source.points) + int(
            any(
                value is not None
                for value in (
                    observed_source.opening_baseline.views,
                    observed_source.opening_baseline.reactions,
                    observed_source.opening_baseline.comments,
                    observed_source.opening_baseline.reposts,
                )
            )
        )
        return PublicMemeAnalyticsRead(
            meme_id=meme_id,
            window=window,
            start_at=start_at,
            end_at=refreshed_at,
            granularity=(
                PublicMemeAnalyticsGranularity.ADAPTIVE
                if window is PublicMemeAnalyticsWindow.ALL
                else PublicMemeAnalyticsGranularity.DAY
            ),
            history_start_at=history_start,
            history_end_at=history_end,
            refreshed_at=refreshed_at,
            insufficient_history=max(usable_activity_points, usable_source_points) < 2,
            summary=PublicMemeAnalyticsSummaryRead(
                totals=totals,
                average_recorded_activity_per_day=totals.recorded_activity / duration_days,
                current_favorites=max(meme.like_count, 0),
                momentum=momentum,
                peak=peak,
            ),
            activity_points=activity_points,
            observed_source=observed_source,
            source_performance=_source_summary(source_metrics),
            audience_change=audience_change,
            exposure_funnels=funnel,
        )

    async def _load_visible_public_meme(self, meme_id: uuid.UUID, *, include_nsfw: bool) -> Meme | None:
        statement = select(Meme).where(Meme.id == meme_id, Meme.is_public.is_(True))
        if not include_nsfw:
            statement = statement.where(Meme.is_nsfw.is_(False))
        return await self._session.scalar(statement)

    async def _load_sources(self, meme_id: uuid.UUID, *, cutoff: datetime) -> list[_SourceRecord]:
        result = await self._session.execute(
            select(
                MemeSource.id,
                MemeSource.source_id,
                MemeSource.post_id,
                MemeSource.source_alive,
                MemeSource.published_at,
                MemeSource.created_at,
                SourceChannel.id,
                SourceChannel.title,
                SourceChannel.username,
            )
            .join(MemeFile, MemeFile.id == MemeSource.file_id)
            .outerjoin(
                SourceChannel,
                and_(
                    SourceChannel.platform == MemeSource.platform,
                    SourceChannel.platform_id == MemeSource.source_id,
                ),
            )
            .where(
                MemeFile.meme_id == meme_id,
                MemeSource.platform == SourcePlatform.TELEGRAM,
                MemeSource.source_kind == IngestSourceKind.PUBLIC_CRAWLER,
                MemeSource.created_at <= cutoff,
            )
            .order_by(MemeSource.created_at.asc(), MemeSource.id.asc())
        )
        return [
            _SourceRecord(
                id=row[0],
                source_key=row[1],
                post_id=row[2],
                source_alive=row[3],
                published_at=row[4],
                created_at=row[5],
                channel_id=row[6],
                channel_title=(row[7] or "").strip() or "Telegram channel",
                channel_username=_safe_telegram_username(row[8]),
            )
            for row in result.all()
        ]

    async def _load_engagement_observations(
        self,
        sources: Sequence[_SourceRecord],
        *,
        cutoff: datetime,
    ) -> list[_EngagementObservation]:
        source_ids = [source.id for source in sources]
        if not source_ids:
            return []
        result = await self._session.execute(
            select(
                MemeSourceEngagementSnapshot.meme_source_id,
                MemeSourceEngagementSnapshot.captured_at,
                MemeSourceEngagementSnapshot.view_count,
                MemeSourceEngagementSnapshot.reaction_count,
                MemeSourceEngagementSnapshot.comment_count,
                MemeSourceEngagementSnapshot.forward_count,
            )
            .where(
                MemeSourceEngagementSnapshot.meme_source_id.in_(source_ids),
                MemeSourceEngagementSnapshot.fetch_status == SourceEngagementFetchStatus.SUCCESS,
                MemeSourceEngagementSnapshot.captured_at <= cutoff,
            )
            .order_by(
                MemeSourceEngagementSnapshot.captured_at.asc(),
                MemeSourceEngagementSnapshot.id.asc(),
            )
        )
        return [
            _EngagementObservation(
                source_id=row[0],
                captured_at=row[1],
                views=row[2],
                reactions=row[3],
                comments=row[4],
                reposts=row[5],
            )
            for row in result.all()
        ]

    async def _load_latest_engagement_observations(
        self,
        sources: Sequence[_SourceRecord],
        *,
        cutoff: datetime,
    ) -> list[_EngagementObservation]:
        """Load one latest-success row per source for the paginated source read."""

        source_ids = [source.id for source in sources]
        if not source_ids:
            return []
        ranked = (
            select(
                MemeSourceEngagementSnapshot.meme_source_id.label("source_id"),
                MemeSourceEngagementSnapshot.captured_at.label("captured_at"),
                MemeSourceEngagementSnapshot.view_count.label("views"),
                MemeSourceEngagementSnapshot.reaction_count.label("reactions"),
                MemeSourceEngagementSnapshot.comment_count.label("comments"),
                MemeSourceEngagementSnapshot.forward_count.label("reposts"),
                func.row_number()
                .over(
                    partition_by=MemeSourceEngagementSnapshot.meme_source_id,
                    order_by=(
                        MemeSourceEngagementSnapshot.captured_at.desc(),
                        MemeSourceEngagementSnapshot.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .where(
                MemeSourceEngagementSnapshot.meme_source_id.in_(source_ids),
                MemeSourceEngagementSnapshot.fetch_status == SourceEngagementFetchStatus.SUCCESS,
                MemeSourceEngagementSnapshot.captured_at <= cutoff,
            )
            .subquery()
        )
        result = await self._session.execute(
            select(
                ranked.c.source_id,
                ranked.c.captured_at,
                ranked.c.views,
                ranked.c.reactions,
                ranked.c.comments,
                ranked.c.reposts,
            )
            .where(ranked.c.row_number == 1)
            .order_by(ranked.c.captured_at.asc(), ranked.c.source_id.asc())
        )
        return [
            _EngagementObservation(
                source_id=row[0],
                captured_at=row[1],
                views=row[2],
                reactions=row[3],
                comments=row[4],
                reposts=row[5],
            )
            for row in result.all()
        ]

    async def _load_audience_observations(
        self,
        sources: Sequence[_SourceRecord],
        *,
        cutoff: datetime,
    ) -> list[_AudienceObservation]:
        channel_ids = list(dict.fromkeys(source.channel_id for source in sources if source.channel_id is not None))
        if not channel_ids:
            return []
        result = await self._session.execute(
            select(
                SourceChannelAudienceSnapshot.source_channel_id,
                SourceChannelAudienceSnapshot.captured_at,
                SourceChannelAudienceSnapshot.subscriber_count,
            )
            .where(
                SourceChannelAudienceSnapshot.source_channel_id.in_(channel_ids),
                SourceChannelAudienceSnapshot.fetch_status == SourceChannelAudienceFetchStatus.SUCCESS,
                SourceChannelAudienceSnapshot.subscriber_count.is_not(None),
                SourceChannelAudienceSnapshot.captured_at <= cutoff,
            )
            .order_by(
                SourceChannelAudienceSnapshot.captured_at.asc(),
                SourceChannelAudienceSnapshot.id.asc(),
            )
        )
        return [
            _AudienceObservation(channel_id=row[0], captured_at=row[1], subscribers=row[2])
            for row in result.all()
            if row[2] is not None
        ]

    async def _load_source_page_audience(
        self,
        meme_id: uuid.UUID,
        sources: Sequence[_SourceRecord],
        *,
        cutoff: datetime,
    ) -> tuple[list[_AudienceObservation], dict[uuid.UUID, int]]:
        """Load only current and per-post publish-time audience facts for source pages."""

        channel_ids = list(dict.fromkeys(source.channel_id for source in sources if source.channel_id is not None))
        if not channel_ids:
            return [], {}

        ranked_current = (
            select(
                SourceChannelAudienceSnapshot.source_channel_id.label("channel_id"),
                SourceChannelAudienceSnapshot.captured_at.label("captured_at"),
                SourceChannelAudienceSnapshot.subscriber_count.label("subscribers"),
                func.row_number()
                .over(
                    partition_by=SourceChannelAudienceSnapshot.source_channel_id,
                    order_by=(
                        SourceChannelAudienceSnapshot.captured_at.desc(),
                        SourceChannelAudienceSnapshot.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .where(
                SourceChannelAudienceSnapshot.source_channel_id.in_(channel_ids),
                SourceChannelAudienceSnapshot.fetch_status == SourceChannelAudienceFetchStatus.SUCCESS,
                SourceChannelAudienceSnapshot.subscriber_count.is_not(None),
                SourceChannelAudienceSnapshot.captured_at <= cutoff,
            )
            .subquery()
        )
        current_result = await self._session.execute(
            select(
                ranked_current.c.channel_id,
                ranked_current.c.captured_at,
                ranked_current.c.subscribers,
            )
            .where(ranked_current.c.row_number == 1)
            .order_by(ranked_current.c.captured_at.asc(), ranked_current.c.channel_id.asc())
        )
        current = [
            _AudienceObservation(channel_id=row[0], captured_at=row[1], subscribers=row[2])
            for row in current_result.all()
        ]

        eligible_sources = (
            select(
                MemeSource.id.label("source_id"),
                SourceChannel.id.label("channel_id"),
                MemeSource.published_at.label("published_at"),
            )
            .join(MemeFile, MemeFile.id == MemeSource.file_id)
            .join(
                SourceChannel,
                and_(
                    SourceChannel.platform == MemeSource.platform,
                    SourceChannel.platform_id == MemeSource.source_id,
                ),
            )
            .where(
                MemeFile.meme_id == meme_id,
                MemeSource.platform == SourcePlatform.TELEGRAM,
                MemeSource.source_kind == IngestSourceKind.PUBLIC_CRAWLER,
                MemeSource.created_at <= cutoff,
                MemeSource.published_at.is_not(None),
            )
            .subquery()
        )
        ranked_publish = (
            select(
                eligible_sources.c.source_id,
                SourceChannelAudienceSnapshot.subscriber_count.label("subscribers"),
                func.row_number()
                .over(
                    partition_by=eligible_sources.c.source_id,
                    order_by=(
                        SourceChannelAudienceSnapshot.captured_at.desc(),
                        SourceChannelAudienceSnapshot.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .join(
                SourceChannelAudienceSnapshot,
                SourceChannelAudienceSnapshot.source_channel_id == eligible_sources.c.channel_id,
            )
            .where(
                SourceChannelAudienceSnapshot.fetch_status == SourceChannelAudienceFetchStatus.SUCCESS,
                SourceChannelAudienceSnapshot.subscriber_count.is_not(None),
                SourceChannelAudienceSnapshot.captured_at <= cutoff,
                SourceChannelAudienceSnapshot.captured_at <= eligible_sources.c.published_at,
                SourceChannelAudienceSnapshot.captured_at
                >= eligible_sources.c.published_at - _PUBLISH_AUDIENCE_MAX_AGE,
            )
            .subquery()
        )
        publish_result = await self._session.execute(
            select(ranked_publish.c.source_id, ranked_publish.c.subscribers).where(
                ranked_publish.c.row_number == 1
            )
        )
        return current, {row[0]: row[1] for row in publish_result.all()}

    async def _load_event_history_bounds(
        self,
        meme_id: uuid.UUID,
        *,
        end_at: datetime,
    ) -> tuple[datetime | None, datetime | None]:
        result = await self._session.execute(
            select(func.min(AnalyticsEvent.occurred_at), func.max(AnalyticsEvent.occurred_at)).where(
                _event_meme_filter(meme_id),
                AnalyticsEvent.occurred_at <= end_at,
                AnalyticsEvent.event_type.in_([*list(_ACTIVITY_EVENT_TYPES), *list(_FUNNEL_EVENT_TYPES)]),
            )
        )
        row = result.one()
        return row[0], row[1]

    async def _load_event_daily_activity(
        self,
        meme_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[_TimedActivity]:
        day = func.date_trunc("day", AnalyticsEvent.occurred_at, "UTC").label("observed_at")
        result = await self._session.execute(
            select(day, AnalyticsEvent.event_type, func.count())
            .where(
                _event_meme_filter(meme_id),
                AnalyticsEvent.event_type.in_(list(_ACTIVITY_EVENT_TYPES)),
                AnalyticsEvent.occurred_at >= start_at,
                AnalyticsEvent.occurred_at <= end_at,
            )
            .group_by(day, AnalyticsEvent.event_type)
            .order_by(day.asc(), AnalyticsEvent.event_type.asc())
        )
        return [
            _TimedActivity(observed_at=_normalize_utc(row[0]), counts=_event_activity(row[1], row[2]))
            for row in result.all()
        ]

    async def _exposure_funnels(
        self,
        meme_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> PublicMemeExposureFunnelsRead:
        exposure_result = await self._session.execute(
            select(
                func.count(MemeExposure.id)
                .filter(MemeExposure.kind == "web_card")
                .label("web_exposures"),
                func.count(MemeExposure.id)
                .filter(
                    MemeExposure.kind == "web_card",
                    MemeExposure.detail_clicked_at >= start_at,
                    MemeExposure.detail_clicked_at <= end_at,
                )
                .label("web_detail_clicks"),
                func.count(MemeExposure.id)
                .filter(
                    MemeExposure.kind == "web_card",
                    MemeExposure.high_intent_action_at >= start_at,
                    MemeExposure.high_intent_action_at <= end_at,
                )
                .label("web_high_intent_actions"),
                func.count(MemeExposure.id)
                .filter(MemeExposure.kind == "telegram_inline")
                .label("inline_exposures"),
                func.count(MemeExposure.id)
                .filter(
                    MemeExposure.kind == "telegram_inline",
                    MemeExposure.inline_chosen_at >= start_at,
                    MemeExposure.inline_chosen_at <= end_at,
                )
                .label("inline_chosen"),
                func.count(MemeExposure.id)
                .filter(
                    MemeExposure.kind == "telegram_inline",
                    MemeExposure.inline_sent_at >= start_at,
                    MemeExposure.inline_sent_at <= end_at,
                )
                .label("inline_sent"),
            ).where(
                MemeExposure.meme_id == meme_id,
                MemeExposure.exposed_at >= start_at,
                MemeExposure.exposed_at <= end_at,
            )
        )
        exposure_counts = exposure_result.one()
        web_exposure_count = int(exposure_counts.web_exposures or 0)
        web_detail = int(exposure_counts.web_detail_clicks or 0)
        web_high_intent = int(exposure_counts.web_high_intent_actions or 0)
        inline_exposure_count = int(exposure_counts.inline_exposures or 0)
        inline_chosen = int(exposure_counts.inline_chosen or 0)
        inline_sent = int(exposure_counts.inline_sent or 0)

        # Keep legacy/unattributed observations visible as lower-confidence totals,
        # but never place them in a funnel denominator or conversion rate.
        impression_id = AnalyticsEvent.payload["impression_id"].astext
        unkeyed_result = await self._session.execute(
            select(AnalyticsEvent.event_type, func.count())
            .where(
                _event_meme_filter(meme_id),
                AnalyticsEvent.event_type.in_(
                    [
                        AnalyticsEventType.IMPRESSION,
                        AnalyticsEventType.MEME_IMPRESSION,
                        AnalyticsEventType.INLINE_SERVED,
                    ]
                ),
                AnalyticsEvent.occurred_at >= start_at,
                AnalyticsEvent.occurred_at <= end_at,
                or_(impression_id.is_(None), impression_id == ""),
            )
            .group_by(AnalyticsEvent.event_type)
        )
        unkeyed_counts = {event_type: count for event_type, count in unkeyed_result.all()}
        return PublicMemeExposureFunnelsRead(
            web=PublicMemeWebExposureFunnelRead(
                recorded_card_impressions=(
                    web_exposure_count
                    + unkeyed_counts.get(AnalyticsEventType.IMPRESSION, 0)
                    + unkeyed_counts.get(AnalyticsEventType.MEME_IMPRESSION, 0)
                ),
                attributed_impressions=web_exposure_count,
                matched_detail_clicks=web_detail,
                matched_high_intent_actions=web_high_intent,
                detail_click_rate=_safe_ratio(web_detail, web_exposure_count),
                high_intent_rate=_safe_ratio(web_high_intent, web_exposure_count),
            ),
            telegram_inline=PublicMemeInlineExposureFunnelRead(
                inline_results_served=(
                    inline_exposure_count + unkeyed_counts.get(AnalyticsEventType.INLINE_SERVED, 0)
                ),
                attributed_results_served=inline_exposure_count,
                matched_chosen=inline_chosen,
                matched_sent=inline_sent,
                chosen_rate=_safe_ratio(inline_chosen, inline_exposure_count),
                sent_rate=_safe_ratio(inline_sent, inline_exposure_count),
            ),
        )


def _event_meme_filter(meme_id: uuid.UUID) -> ColumnElement[bool]:
    meme_id_text = str(meme_id)
    return or_(
        AnalyticsEvent.payload["refs"]["meme_id"].astext == meme_id_text,
        AnalyticsEvent.payload["meme_id"].astext == meme_id_text,
    )


def _normalize_utc(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _midnight_utc(value: datetime) -> datetime:
    return datetime.combine(_normalize_utc(value).date(), time.min, tzinfo=UTC)


def _minimum_datetime(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _maximum_datetime(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _safe_telegram_username(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().removeprefix("@")
    if not _TELEGRAM_USERNAME_PATTERN.fullmatch(normalized):
        return None
    return normalized


def _telegram_channel_url(username: str | None) -> str | None:
    return f"https://t.me/{username}" if username is not None else None


def _telegram_post_url(username: str | None, post_id: str) -> str | None:
    if username is None or not _TELEGRAM_POST_ID_PATTERN.fullmatch(post_id):
        return None
    return f"https://t.me/{username}/{post_id}"


def _observations_by_source(
    observations: Iterable[_EngagementObservation],
) -> dict[uuid.UUID, list[_EngagementObservation]]:
    grouped: dict[uuid.UUID, list[_EngagementObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.source_id].append(observation)
    return grouped


def _audience_by_channel(
    observations: Iterable[_AudienceObservation],
) -> dict[uuid.UUID, list[_AudienceObservation]]:
    grouped: dict[uuid.UUID, list[_AudienceObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.channel_id].append(observation)
    return grouped


def _build_source_metrics(
    sources: Sequence[_SourceRecord],
    *,
    engagement: Sequence[_EngagementObservation],
    audience: Sequence[_AudienceObservation],
    audience_at_publish_by_source: Mapping[uuid.UUID, int] | None = None,
) -> list[_SourceMetric]:
    engagement_by_source = _observations_by_source(engagement)
    audience_by_channel = _audience_by_channel(audience)
    metrics: list[_SourceMetric] = []
    for source in sources:
        source_observations = engagement_by_source.get(source.id, [])
        channel_observations = audience_by_channel.get(source.channel_id, []) if source.channel_id else []
        current_audience = channel_observations[-1].subscribers if channel_observations else None
        audience_at_publish = (
            audience_at_publish_by_source.get(source.id)
            if audience_at_publish_by_source is not None
            else _audience_at_publish(channel_observations, source.published_at)
        )
        metrics.append(
            _SourceMetric(
                source=source,
                latest=source_observations[-1] if source_observations else None,
                audience_at_publish=audience_at_publish,
                current_audience=current_audience,
            )
        )
    return metrics


def _audience_at_publish(
    observations: Sequence[_AudienceObservation],
    published_at: datetime | None,
) -> int | None:
    if published_at is None:
        return None
    normalized_published_at = _normalize_utc(published_at)
    for observation in reversed(observations):
        if observation.captured_at > normalized_published_at:
            continue
        if normalized_published_at - observation.captured_at <= _PUBLISH_AUDIENCE_MAX_AGE:
            return observation.subscribers
        break
    return None


def _metric_value(metric: _SourceMetric, name: str) -> int | None:
    return getattr(metric.latest, name) if metric.latest is not None else None


def _item_rates(metric: _SourceMetric) -> PublicMemeSourceRatesRead:
    views = _metric_value(metric, "views")
    reactions = _metric_value(metric, "reactions")
    comments = _metric_value(metric, "comments")
    reposts = _metric_value(metric, "reposts")
    return PublicMemeSourceRatesRead(
        reactions=_single_views_rate(reactions, views),
        comments=_single_views_rate(comments, views),
        reposts=_single_views_rate(reposts, views),
        interactions=_single_views_rate(
            reactions + comments + reposts
            if reactions is not None and comments is not None and reposts is not None
            else None,
            views,
        ),
    )


def _single_views_rate(numerator: int | None, views: int | None) -> PublicMemeSourceRateRead:
    eligible = numerator is not None and views is not None and views > 0
    return PublicMemeSourceRateRead(
        value=numerator / views if eligible else None,
        numerator=numerator if eligible else None,
        denominator=views if eligible else None,
        eligible_posts=int(eligible),
        total_posts=1,
    )


def _source_audience_read(metric: _SourceMetric) -> PublicMemeSourceAudienceRead:
    views = _metric_value(metric, "views")
    reactions = _metric_value(metric, "reactions")
    comments = _metric_value(metric, "comments")
    reposts = _metric_value(metric, "reposts")
    audience = metric.audience_at_publish
    interactions = (
        reactions + comments + reposts
        if reactions is not None and comments is not None and reposts is not None
        else None
    )
    return PublicMemeSourceAudienceRead(
        audience_at_publish=audience,
        current_audience=metric.current_audience,
        views_per_1000_subscribers=(views / audience * 1000 if views is not None and audience else None),
        interactions_per_1000_subscribers=(
            interactions / audience * 1000 if interactions is not None and audience else None
        ),
    )


def _source_post_read(metric: _SourceMetric) -> PublicMemeSourcePostRead:
    source = metric.source
    latest = metric.latest
    return PublicMemeSourcePostRead(
        channel_title=source.channel_title,
        channel_username=source.channel_username,
        channel_url=_telegram_channel_url(source.channel_username),
        post_url=_telegram_post_url(source.channel_username, source.post_id),
        published_at=source.published_at,
        available=source.source_alive,
        captured_at=latest.captured_at if latest else None,
        views=latest.views if latest else None,
        reactions=latest.reactions if latest else None,
        comments=latest.comments if latest else None,
        reposts=latest.reposts if latest else None,
        rates=_item_rates(metric),
        audience=_source_audience_read(metric),
    )


def _source_sort_key(metric: _SourceMetric, *, sort: PublicMemeSourceSort) -> tuple[object, ...]:
    source = metric.source
    # The stable page token freezes observations, not mutable channel metadata.
    # Use only the immutable source primary key to break equal metric/date ties.
    stable = (str(source.id),)
    if sort is PublicMemeSourceSort.NEWEST:
        published_at = _normalize_utc(source.published_at) if source.published_at else None
        return (published_at is None, -(published_at.timestamp() if published_at else 0), *stable)
    if sort is PublicMemeSourceSort.OLDEST:
        published_at = _normalize_utc(source.published_at) if source.published_at else None
        return (published_at is None, published_at.timestamp() if published_at else 0, *stable)
    if sort is PublicMemeSourceSort.REACTIONS_DESC:
        value = _metric_value(metric, "reactions")
    elif sort is PublicMemeSourceSort.REPOSTS_DESC:
        value = _metric_value(metric, "reposts")
    elif sort is PublicMemeSourceSort.INTERACTION_RATE_DESC:
        value = _item_rates(metric).interactions.value
    else:
        value = _metric_value(metric, "views")
    return (value is None, -(value or 0), *stable)


def _coverage(measured: int, total: int) -> PublicMemeMetricCoverageRead:
    return PublicMemeMetricCoverageRead(
        measured_posts=measured,
        total_posts=total,
        ratio=measured / total if total else 0.0,
    )


def _source_coverage(metrics: Sequence[_SourceMetric]) -> PublicMemeSourceCoverageRead:
    total = len(metrics)
    return PublicMemeSourceCoverageRead(
        views=_coverage(sum(_metric_value(metric, "views") is not None for metric in metrics), total),
        reactions=_coverage(sum(_metric_value(metric, "reactions") is not None for metric in metrics), total),
        comments=_coverage(sum(_metric_value(metric, "comments") is not None for metric in metrics), total),
        reposts=_coverage(sum(_metric_value(metric, "reposts") is not None for metric in metrics), total),
    )


def _known_total(metrics: Sequence[_SourceMetric], name: str) -> int | None:
    values = [value for metric in metrics if (value := _metric_value(metric, name)) is not None]
    return sum(values) if values else None


def _aggregate_views_rate(metrics: Sequence[_SourceMetric], name: str) -> PublicMemeSourceRateRead:
    eligible: list[tuple[int, int]] = []
    for metric in metrics:
        numerator = _metric_value(metric, name)
        views = _metric_value(metric, "views")
        if numerator is not None and views is not None and views > 0:
            eligible.append((numerator, views))
    numerator_total = sum(item[0] for item in eligible)
    denominator_total = sum(item[1] for item in eligible)
    return PublicMemeSourceRateRead(
        value=numerator_total / denominator_total if denominator_total else None,
        numerator=numerator_total if eligible else None,
        denominator=denominator_total if eligible else None,
        eligible_posts=len(eligible),
        total_posts=len(metrics),
    )


def _aggregate_interaction_rate(metrics: Sequence[_SourceMetric]) -> PublicMemeSourceRateRead:
    eligible: list[tuple[int, int]] = []
    for metric in metrics:
        views = _metric_value(metric, "views")
        reactions = _metric_value(metric, "reactions")
        comments = _metric_value(metric, "comments")
        reposts = _metric_value(metric, "reposts")
        if (
            views is not None
            and views > 0
            and reactions is not None
            and comments is not None
            and reposts is not None
        ):
            eligible.append((reactions + comments + reposts, views))
    numerator_total = sum(item[0] for item in eligible)
    denominator_total = sum(item[1] for item in eligible)
    return PublicMemeSourceRateRead(
        value=numerator_total / denominator_total if denominator_total else None,
        numerator=numerator_total if eligible else None,
        denominator=denominator_total if eligible else None,
        eligible_posts=len(eligible),
        total_posts=len(metrics),
    )


def _source_rates(metrics: Sequence[_SourceMetric]) -> PublicMemeSourceRatesRead:
    return PublicMemeSourceRatesRead(
        reactions=_aggregate_views_rate(metrics, "reactions"),
        comments=_aggregate_views_rate(metrics, "comments"),
        reposts=_aggregate_views_rate(metrics, "reposts"),
        interactions=_aggregate_interaction_rate(metrics),
    )


def _audience_normalized_rate(
    metrics: Sequence[_SourceMetric],
    *,
    interactions: bool,
) -> PublicMemeSourceRateRead:
    eligible: list[tuple[int, int]] = []
    for metric in metrics:
        audience = metric.audience_at_publish
        if audience is None or audience <= 0:
            continue
        if interactions:
            values = (
                _metric_value(metric, "reactions"),
                _metric_value(metric, "comments"),
                _metric_value(metric, "reposts"),
            )
            reactions, comments, reposts = values
            if reactions is None or comments is None or reposts is None:
                continue
            numerator = reactions + comments + reposts
        else:
            views = _metric_value(metric, "views")
            if views is None:
                continue
            numerator = views
        eligible.append((numerator, audience))
    numerator_total = sum(item[0] for item in eligible)
    denominator_total = sum(item[1] for item in eligible)
    return PublicMemeSourceRateRead(
        value=numerator_total / denominator_total * 1000 if denominator_total else None,
        numerator=numerator_total if eligible else None,
        denominator=denominator_total if eligible else None,
        eligible_posts=len(eligible),
        total_posts=len(metrics),
    )


def _source_audience_summary(metrics: Sequence[_SourceMetric]) -> PublicMemeSourceAudienceSummaryRead:
    channel_keys = {
        metric.source.channel_id or metric.source.source_key
        for metric in metrics
    }
    known_channel_keys = {
        metric.source.channel_id or metric.source.source_key
        for metric in metrics
        if metric.current_audience is not None
    }
    return PublicMemeSourceAudienceSummaryRead(
        current_known_channels=len(known_channel_keys),
        total_channels=len(channel_keys),
        publish_time_eligible_posts=sum(
            metric.audience_at_publish is not None and metric.audience_at_publish > 0 for metric in metrics
        ),
        total_posts=len(metrics),
        views_per_1000_subscribers=_audience_normalized_rate(metrics, interactions=False),
        interactions_per_1000_subscribers=_audience_normalized_rate(metrics, interactions=True),
    )


def _source_summary(metrics: Sequence[_SourceMetric]) -> PublicMemeSourceSummaryRead:
    published = [metric.source.published_at for metric in metrics if metric.source.published_at is not None]
    captured = [metric.latest.captured_at for metric in metrics if metric.latest is not None]
    channel_keys = {metric.source.channel_id or metric.source.source_key for metric in metrics}
    return PublicMemeSourceSummaryRead(
        total_posts=len(metrics),
        available_posts=sum(metric.source.source_alive for metric in metrics),
        distinct_channels=len(channel_keys),
        earliest_published_at=min(published) if published else None,
        latest_published_at=max(published) if published else None,
        latest_captured_at=max(captured) if captured else None,
        totals=PublicMemeSourceTotalsRead(
            views=_known_total(metrics, "views"),
            reactions=_known_total(metrics, "reactions"),
            comments=_known_total(metrics, "comments"),
            reposts=_known_total(metrics, "reposts"),
        ),
        coverage=_source_coverage(metrics),
        rates=_source_rates(metrics),
        audience=_source_audience_summary(metrics),
    )


def _analytics_start(
    window: PublicMemeAnalyticsWindow,
    *,
    now: datetime,
    history_start: datetime | None,
) -> datetime:
    if window is not PublicMemeAnalyticsWindow.ALL:
        return _midnight_utc(now) - timedelta(days=_FIXED_WINDOW_DAYS[window] - 1)
    if history_start is None:
        return _midnight_utc(now)
    return _bucket_for(history_start, window=window, now=now, end_at=now).start


def _start_of_week(value: datetime) -> datetime:
    midnight = _midnight_utc(value)
    return midnight - timedelta(days=midnight.weekday())


def _start_of_month(value: datetime) -> datetime:
    normalized = _normalize_utc(value)
    return datetime(normalized.year, normalized.month, 1, tzinfo=UTC)


def _next_month(value: datetime) -> datetime:
    return datetime(value.year + int(value.month == 12), value.month % 12 + 1, 1, tzinfo=UTC)


def _adaptive_cutoffs(now: datetime) -> tuple[datetime, datetime]:
    daily_start = _midnight_utc(now - timedelta(days=119))
    weekly_start = _start_of_week(now - timedelta(days=729))
    return daily_start, weekly_start


def _bucket_for(
    observed_at: datetime,
    *,
    window: PublicMemeAnalyticsWindow,
    now: datetime,
    end_at: datetime,
) -> _Bucket:
    observed_at = _normalize_utc(observed_at)
    if window is not PublicMemeAnalyticsWindow.ALL:
        start = _midnight_utc(observed_at)
        return _Bucket(
            start=start,
            end=min(start + timedelta(days=1), end_at),
            granularity=PublicMemeAnalyticsGranularity.DAY,
        )
    daily_start, weekly_start = _adaptive_cutoffs(now)
    if observed_at >= daily_start:
        start = _midnight_utc(observed_at)
        return _Bucket(
            start=start,
            end=min(start + timedelta(days=1), end_at),
            granularity=PublicMemeAnalyticsGranularity.DAY,
        )
    if observed_at >= weekly_start:
        start = _start_of_week(observed_at)
        return _Bucket(
            start=start,
            end=min(start + timedelta(days=7), daily_start, end_at),
            granularity=PublicMemeAnalyticsGranularity.WEEK,
        )
    start = _start_of_month(observed_at)
    return _Bucket(
        start=start,
        end=min(_next_month(start), weekly_start, end_at),
        granularity=PublicMemeAnalyticsGranularity.MONTH,
    )


def _analytics_buckets(
    start_at: datetime,
    end_at: datetime,
    *,
    window: PublicMemeAnalyticsWindow,
    now: datetime,
) -> list[_Bucket]:
    buckets: list[_Bucket] = []
    cursor = start_at
    while cursor < end_at:
        bucket = _bucket_for(cursor, window=window, now=now, end_at=end_at)
        if bucket.end <= cursor:
            break
        buckets.append(_Bucket(start=cursor, end=bucket.end, granularity=bucket.granularity))
        cursor = bucket.end
    return buckets


def _source_timed_activity(
    observations: Sequence[_EngagementObservation],
    *,
    start_at: datetime,
) -> list[_TimedActivity]:
    high_watermarks: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    activity: list[_TimedActivity] = []
    for observation in observations:
        counts = _ActivityCounts()
        for metric_name, activity_name in (
            ("views", "source_views"),
            ("reactions", "source_reactions"),
            ("reposts", "source_reposts"),
        ):
            value = getattr(observation, metric_name)
            if value is None:
                continue
            previous_high = high_watermarks[observation.source_id].get(metric_name)
            if previous_high is not None and value > previous_high:
                setattr(counts, activity_name, value - previous_high)
            high_watermarks[observation.source_id][metric_name] = max(value, previous_high or 0)
        if observation.captured_at >= start_at and counts.recorded_activity:
            activity.append(_TimedActivity(observed_at=observation.captured_at, counts=counts))
    return activity


def _event_activity(event_type: AnalyticsEventType, count: int) -> _ActivityCounts:
    counts = _ActivityCounts()
    if event_type in _MEMEEXPERT_VIEW_EVENTS:
        counts.memeexpert_views = count
    elif event_type in _MEMEEXPERT_SEND_EVENTS:
        counts.memeexpert_sends = count
    elif event_type in _MEMEEXPERT_SAVE_EVENTS:
        counts.memeexpert_saves = count
    elif event_type in _MEMEEXPERT_FAVORITE_EVENTS:
        counts.memeexpert_favorites = count
    elif event_type is AnalyticsEventType.MEME_DOWNLOAD:
        counts.downloads = count
    return counts


def _activity_points(
    buckets: Sequence[_Bucket],
    activity: Sequence[_TimedActivity],
    *,
    window: PublicMemeAnalyticsWindow,
    now: datetime,
) -> list[PublicMemeActivityPointRead]:
    by_start: dict[datetime, _ActivityCounts] = defaultdict(_ActivityCounts)
    for item in activity:
        bucket = _bucket_for(item.observed_at, window=window, now=now, end_at=buckets[-1].end if buckets else now)
        by_start[bucket.start].add(item.counts)
    return [
        PublicMemeActivityPointRead(
            bucket_start=bucket.start,
            bucket_end=bucket.end,
            granularity=bucket.granularity,
            **by_start[bucket.start].to_read().model_dump(),
        )
        for bucket in buckets
    ]


def _sum_activity_points(points: Sequence[PublicMemeActivityPointRead]) -> PublicMemeActivityCountsRead:
    counts = _ActivityCounts()
    for point in points:
        counts.add(
            _ActivityCounts(
                source_views=point.source_views,
                source_reactions=point.source_reactions,
                source_reposts=point.source_reposts,
                memeexpert_views=point.memeexpert_views,
                memeexpert_sends=point.memeexpert_sends,
                memeexpert_saves=point.memeexpert_saves,
                memeexpert_favorites=point.memeexpert_favorites,
                downloads=point.downloads,
            )
        )
    return counts.to_read()


def _activity_in_interval(
    activity: Sequence[_TimedActivity],
    start_at: datetime,
    end_at: datetime,
) -> _ActivityCounts:
    counts = _ActivityCounts()
    for item in activity:
        if start_at <= item.observed_at < end_at:
            counts.add(item.counts)
    return counts


def _momentum_read(recent: _ActivityCounts, previous: _ActivityCounts) -> PublicMemeAnalyticsMomentumRead:
    change = recent.recorded_activity - previous.recorded_activity
    return PublicMemeAnalyticsMomentumRead(
        recent_recorded_activity=recent.recorded_activity,
        previous_recorded_activity=previous.recorded_activity,
        change=change,
        change_rate=change / previous.recorded_activity if previous.recorded_activity else None,
    )


def _peak_read(points: Sequence[PublicMemeActivityPointRead]) -> PublicMemeAnalyticsPeakRead | None:
    nonzero = [point for point in points if point.recorded_activity > 0]
    if not nonzero:
        return None
    peak = max(nonzero, key=lambda point: point.recorded_activity)
    return PublicMemeAnalyticsPeakRead(
        bucket_start=peak.bucket_start,
        bucket_end=peak.bucket_end,
        granularity=peak.granularity,
        recorded_activity=peak.recorded_activity,
    )


def _observed_totals(
    state: dict[uuid.UUID, _EngagementObservation],
    *,
    total_posts: int,
    observed_at: datetime,
) -> PublicMemeObservedSourcePointRead:
    observations = list(state.values())
    totals: dict[str, int | None] = {}
    coverage: dict[str, PublicMemeMetricCoverageRead] = {}
    for name in ("views", "reactions", "comments", "reposts"):
        values = [value for observation in observations if (value := getattr(observation, name)) is not None]
        totals[name] = sum(values) if values else None
        coverage[name] = _coverage(len(values), total_posts)
    return PublicMemeObservedSourcePointRead(
        observed_at=observed_at,
        **totals,
        coverage=PublicMemeSourceCoverageRead(**coverage),
    )


def _observed_source_series(
    sources: Sequence[_SourceRecord],
    observations: Sequence[_EngagementObservation],
    *,
    start_at: datetime,
    end_at: datetime,
    window: PublicMemeAnalyticsWindow,
    now: datetime,
) -> PublicMemeObservedSourceSeriesRead:
    state: dict[uuid.UUID, _EngagementObservation] = {}
    for observation in observations:
        if observation.captured_at >= start_at:
            break
        state[observation.source_id] = observation
    opening_total = sum(source.created_at < start_at for source in sources)
    opening = _observed_totals(state, total_posts=opening_total, observed_at=start_at)
    by_bucket: dict[datetime, list[_EngagementObservation]] = defaultdict(list)
    for observation in observations:
        if not (start_at <= observation.captured_at <= end_at):
            continue
        bucket = _bucket_for(observation.captured_at, window=window, now=now, end_at=end_at)
        by_bucket[bucket.start].append(observation)
    points: list[PublicMemeObservedSourcePointRead] = []
    for bucket_start in sorted(by_bucket):
        bucket_observations = by_bucket[bucket_start]
        for observation in bucket_observations:
            state[observation.source_id] = observation
        # Preserve the last real capture boundary represented by this bucket;
        # never label an observation with an artificial future bucket end.
        observed_at = max(observation.captured_at for observation in bucket_observations)
        total_posts = sum(source.created_at <= observed_at for source in sources)
        points.append(_observed_totals(state, total_posts=total_posts, observed_at=observed_at))
    return PublicMemeObservedSourceSeriesRead(opening_baseline=opening, points=points)


def _audience_change(
    sources: Sequence[_SourceRecord],
    observations: Sequence[_AudienceObservation],
    *,
    start_at: datetime,
    end_at: datetime,
) -> PublicMemeChannelAudienceChangeRead:
    channel_ids = {source.channel_id for source in sources if source.channel_id is not None}
    by_channel = _audience_by_channel(observations)
    current: dict[uuid.UUID, int] = {}
    baseline: dict[uuid.UUID, int] = {}
    for channel_id in channel_ids:
        for observation in by_channel.get(channel_id, []):
            if observation.captured_at < start_at:
                baseline[channel_id] = observation.subscribers
            if observation.captured_at <= end_at:
                current[channel_id] = observation.subscribers
    comparable = channel_ids & current.keys() & baseline.keys()
    return PublicMemeChannelAudienceChangeRead(
        total_channels=len(channel_ids),
        current_known_channels=len(channel_ids & current.keys()),
        comparable_channels=len(comparable),
        net_known_subscriber_change=(
            sum(current[channel_id] - baseline[channel_id] for channel_id in comparable)
            if comparable
            else None
        ),
    )


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


__all__ = ["PublicMemeInsightsService"]
