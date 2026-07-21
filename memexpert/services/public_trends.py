# ruff: noqa: TC001
"""MV-backed public trend analytics reads and refresh helpers."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import String, TextClause, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload

from memexpert.models.content import Meme, MemeSeoPage
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import (
    MemeResultAttributionFiltersRead,
    MemeResultAttributionRead,
    PublicMemePopularityPointRead,
    PublicMemePopularitySummaryRead,
    PublicMemeTrendPageRead,
    PublicMemeTrendRead,
    PublicTrendAggregatePointRead,
    PublicTrendComparisonPointRead,
    PublicTrendComparisonRead,
    PublicTrendComparisonSeriesRead,
    PublicTrendCountsRead,
    PublicTrendMetricsRead,
    PublicTrendSummaryRead,
    PublicTrendTimelineMemeRead,
    PublicTrendTimelinePageRead,
    PublicTrendTimelinePeriodRead,
    new_discovery_impression_id,
    new_discovery_request_id,
)
from memexpert.services.media_render_urls import MediaRenderUrlService
from memexpert.services.meme_search import _DERIVED_POPULARITY_ATTR, _to_public_card_read
from memexpert.services.recommendations.attribution import sign_result_attribution

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

TREND_MATERIALIZED_VIEWS = (
    "public_meme_trends_mv",
    "public_tag_trends_mv",
    "public_template_trends_mv",
    "public_tag_trend_points_mv",
    "public_template_trend_points_mv",
    "public_meme_recommendation_features_mv",
)
_REFRESH_STATE_TABLE = "materialized_view_refresh_state"
_RECORD_REFRESH_SQL = f"""
INSERT INTO {_REFRESH_STATE_TABLE} (view_name, refreshed_at)
VALUES (:view_name, statement_timestamp())
ON CONFLICT (view_name) DO UPDATE
SET refreshed_at = EXCLUDED.refreshed_at
"""

type PublicTrendRanking = Literal["trending", "fastest_rising", "most_liked"]
type PublicTrendTimelineGranularity = Literal["month", "year"]

MAX_COMPARE_ITEMS = 6
TIMELINE_TOP_MEMES_PER_PERIOD = 5
PUBLIC_TRENDS_ALGORITHM_VERSION = "public_trends_mv_v2"


class PublicTrendsService:
    """Read public trend projections without scanning raw analytics events."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        media_render_service: MediaRenderUrlService | None = None,
    ) -> None:
        self._session = session
        self._media_render_service = media_render_service or MediaRenderUrlService()

    async def rank_memes(
        self,
        *,
        ranking: PublicTrendRanking = "trending",
        language: ContentLanguage | None = None,
        media_type: ContentKind | None = None,
        include_nsfw: bool = False,
        tags: tuple[str, ...] = (),
        limit: int = 20,
        offset: int = 0,
        surface: str = "public_api_trends",
        viewer_user_id: uuid.UUID | None = None,
    ) -> PublicMemeTrendPageRead:
        """Return MV-ranked public memes for the requested ranking mode."""

        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        request_id = new_discovery_request_id()
        normalized_tags = tuple(tag.strip().lower() for tag in tags if tag.strip())
        where_sql = _ranking_filters_sql()
        params = {
            "language": language.value if language is not None else None,
            "media_type": media_type.value if media_type is not None else None,
            "include_nsfw": include_nsfw,
            "tags": list(normalized_tags),
            "tag_count": len(normalized_tags),
            "limit": resolved_limit,
            "offset": resolved_offset,
        }

        total = await self._session.scalar(
            _typed_text(
                f"""
                SELECT count(*)
                FROM public_meme_trends_mv mt
                JOIN memes m ON m.id = mt.meme_id
                WHERE {where_sql}
                """
            ),
            params,
        )
        result = await self._session.execute(
            _typed_text(
                f"""
                SELECT
                    mt.*,
                    {_refresh_timestamp_sql("public_meme_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_meme_trends_mv mt
                JOIN memes m ON m.id = mt.meme_id
                WHERE {where_sql}
                ORDER BY {_ranking_order_sql(ranking)}
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        rows = [dict(row) for row in result.mappings()]
        memes_by_id = await self._load_public_memes(tuple(cast("uuid.UUID", row["meme_id"]) for row in rows))
        _attach_row_popularity_scores(memes_by_id, rows)
        filters = MemeResultAttributionFiltersRead(
            language=language,
            media_type=media_type,
            include_nsfw=include_nsfw,
            tags=list(normalized_tags),
            scope="public",
        )
        items = []
        for rank, row in enumerate(rows, start=resolved_offset + 1):
            meme = memes_by_id.get(cast("uuid.UUID", row["meme_id"]))
            if meme is None:
                continue
            items.append(
                PublicMemeTrendRead(
                    meme=_to_public_card_read(meme, media_render_service=self._media_render_service),
                    trend=_trend_metrics_from_row(row),
                    attribution=sign_result_attribution(
                        MemeResultAttributionRead(
                            request_id=request_id,
                            impression_id=new_discovery_impression_id(),
                            surface=surface,
                            source_algorithm=f"public_trends_mv_{ranking}",
                            rank=rank,
                            query=None,
                            filters=filters,
                            collection_scope="public",
                            algorithm_version=PUBLIC_TRENDS_ALGORITHM_VERSION,
                            score=_trend_score_from_row(row, ranking=ranking),
                            score_components=_trend_score_components_from_row(row, ranking=ranking),
                            reason=ranking,
                        ),
                        meme_id=meme.id,
                        viewer_user_id=viewer_user_id,
                    ),
                )
            )
        return PublicMemeTrendPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total or 0,
            has_more=resolved_offset + resolved_limit < (total or 0),
            request_id=request_id,
        )

    async def meme_popularity_summary(
        self,
        meme_id: uuid.UUID,
        *,
        include_nsfw: bool = False,
        snapshot_limit: int = 30,
    ) -> PublicMemePopularitySummaryRead | None:
        """Return public trend metrics plus real snapshot points for one public meme."""

        meme = await self._session.get(Meme, meme_id)
        if meme is None or not meme.is_public or (meme.is_nsfw and not include_nsfw):
            return None

        trend_row = await self._session.execute(
            text(
                f"""
                SELECT
                    trend.*,
                    {_refresh_timestamp_sql("public_meme_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_meme_trends_mv trend
                WHERE trend.meme_id = :meme_id
                """
            ),
            {"meme_id": meme_id},
        )
        trend = trend_row.mappings().first()
        snapshots = await self._session.execute(
            _meme_daily_points_text(
                f"""
                {_MEME_DAILY_POINT_CTES}
                SELECT
                    captured_at,
                    source_views,
                    source_reactions,
                    source_reposts,
                    platform_views,
                    platform_sends,
                    platform_saves,
                    platform_likes,
                    popularity_score
                FROM scored_meme_daily
                ORDER BY captured_at DESC
                LIMIT :limit
                """
            ),
            {"meme_ids": [meme_id], "meme_id_count": 1, "limit": max(1, min(120, snapshot_limit))},
        )
        points = [
            PublicMemePopularityPointRead(**dict(row))
            for row in reversed(list(snapshots.mappings()))
        ]
        return PublicMemePopularitySummaryRead(
            meme_id=meme_id,
            trend=_trend_metrics_from_row(dict(trend)) if trend is not None else None,
            sparkline=points,
        )

    async def tag_summaries(self, *, limit: int = 20, offset: int = 0) -> list[PublicTrendSummaryRead]:
        """Return top safe public tag trend summaries."""

        result = await self._session.execute(
            text(
                f"""
                SELECT
                    tag_trend.*,
                    {_refresh_timestamp_sql("public_tag_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_tag_trends_mv tag_trend
                ORDER BY trending_score DESC, engagement_24h DESC, tag ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": _clamp_limit(limit), "offset": max(0, offset)},
        )
        rows = [dict(row) for row in result.mappings()]
        points_by_tag = await self._load_tag_history_points(tuple(str(row["tag"]) for row in rows))
        return [
            _tag_summary_from_row(row, points=points_by_tag.get(str(row["tag"]).strip().lower(), []))
            for row in rows
        ]

    async def template_summaries(self, *, limit: int = 20, offset: int = 0) -> list[PublicTrendSummaryRead]:
        """Return top safe public template trend summaries."""

        result = await self._session.execute(
            text(
                f"""
                SELECT
                    template_trend.*,
                    {_refresh_timestamp_sql("public_template_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_template_trends_mv template_trend
                ORDER BY trending_score DESC, engagement_24h DESC, template_slug ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": _clamp_limit(limit), "offset": max(0, offset)},
        )
        rows = [dict(row) for row in result.mappings()]
        points_by_slug = await self._load_template_history_points(
            tuple(str(row["template_slug"]) for row in rows)
        )
        return [
            _template_summary_from_row(
                row,
                points=points_by_slug.get(str(row["template_slug"]).strip().lower(), []),
            )
            for row in rows
        ]

    async def tag_summary(self, tag: str) -> PublicTrendSummaryRead | None:
        """Return one safe public tag trend summary."""

        normalized_tag = tag.strip().lower()
        if not normalized_tag:
            return None
        result = await self._session.execute(
            text(
                f"""
                SELECT
                    tag_trend.*,
                    {_refresh_timestamp_sql("public_tag_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_tag_trends_mv tag_trend
                WHERE tag_trend.tag = :tag
                """
            ),
            {"tag": normalized_tag},
        )
        row = result.mappings().first()
        if row is None:
            return None
        points_by_tag = await self._load_tag_history_points((normalized_tag,))
        return _tag_summary_from_row(dict(row), points=points_by_tag.get(normalized_tag, []))

    async def template_summary(self, template_slug: str) -> PublicTrendSummaryRead | None:
        """Return one safe public template trend summary."""

        normalized_slug = template_slug.strip().lower()
        if not normalized_slug:
            return None
        result = await self._session.execute(
            text(
                f"""
                SELECT
                    template_trend.*,
                    {_refresh_timestamp_sql("public_template_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_template_trends_mv template_trend
                WHERE template_trend.template_slug = :template_slug
                """
            ),
            {"template_slug": normalized_slug},
        )
        row = result.mappings().first()
        if row is None:
            return None
        points_by_slug = await self._load_template_history_points((normalized_slug,))
        return _template_summary_from_row(dict(row), points=points_by_slug.get(normalized_slug, []))

    async def compare_items(
        self,
        items: tuple[str, ...],
        *,
        include_nsfw: bool = False,
        snapshot_limit: int = 120,
    ) -> PublicTrendComparisonRead:
        """Compare public meme/template/tag trend items without inventing missing history."""

        requested_items = [item.strip() for item in items if item.strip()][:MAX_COMPARE_ITEMS]
        series: list[PublicTrendComparisonSeriesRead] = []
        seen: set[str] = set()

        for raw_item in requested_items:
            parsed = _parse_comparison_item(raw_item)
            if parsed is None:
                series.append(
                    PublicTrendComparisonSeriesRead(
                        kind="unknown",
                        value=raw_item,
                        title=raw_item,
                        no_data_reason="Use item specs like meme:<uuid-or-slug>, tag:<slug>, or template:<slug>.",
                    )
                )
                continue
            kind, value = parsed
            dedupe_key = f"{kind}:{value}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if kind == "meme":
                series.append(await self._compare_meme(value, include_nsfw=include_nsfw, snapshot_limit=snapshot_limit))
            elif kind == "tag":
                series.append(await self._compare_tag(value))
            else:
                series.append(await self._compare_template(value))

        return PublicTrendComparisonRead(items=series, requested_items=requested_items, max_items=MAX_COMPARE_ITEMS)

    async def timeline_periods(
        self,
        *,
        granularity: PublicTrendTimelineGranularity = "month",
        include_nsfw: bool = False,
        limit: int = 12,
        offset: int = 0,
    ) -> PublicTrendTimelinePageRead:
        """Return month/year timeline periods from derived source/platform points."""

        resolved_granularity = granularity if granularity in ("month", "year") else "month"
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        period_expr = f"date_trunc('{resolved_granularity}', d.captured_at, 'UTC')"
        total = await self._session.scalar(
            _meme_daily_points_text(
                f"""
                {_MEME_DAILY_POINT_CTES}
                SELECT count(*)
                FROM (
                    SELECT {period_expr} AS period_start
                    FROM scored_meme_daily d
                    JOIN memes m ON m.id = d.meme_id
                    WHERE m.is_public IS TRUE AND (:include_nsfw OR m.is_nsfw IS FALSE)
                    GROUP BY period_start
                ) periods
                """
            ),
            {"include_nsfw": include_nsfw, "meme_ids": [], "meme_id_count": 0},
        )
        result = await self._session.execute(
            _meme_daily_points_text(
                f"""
                {_MEME_DAILY_POINT_CTES}
                , period_page AS (
                    SELECT {period_expr} AS period_start
                    FROM scored_meme_daily d
                    JOIN memes m ON m.id = d.meme_id
                    WHERE m.is_public IS TRUE AND (:include_nsfw OR m.is_nsfw IS FALSE)
                    GROUP BY period_start
                    ORDER BY period_start DESC
                    LIMIT :limit OFFSET :offset
                ),
                period_meme AS (
                    SELECT
                        pp.period_start,
                        d.meme_id,
                        sum(d.popularity_score)::double precision AS popularity_score,
                        sum(d.snapshot_count)::integer AS snapshot_count,
                        min(d.captured_at) AS first_captured_at,
                        max(d.captured_at) AS last_captured_at,
                        sum(d.source_views)::integer AS source_views,
                        sum(d.source_reactions)::integer AS source_reactions,
                        sum(d.source_reposts)::integer AS source_reposts,
                        sum(d.platform_views)::integer AS platform_views,
                        sum(d.platform_sends)::integer AS platform_sends,
                        sum(d.platform_saves)::integer AS platform_saves,
                        sum(d.platform_likes)::integer AS platform_likes
                    FROM period_page pp
                    JOIN scored_meme_daily d ON {period_expr} = pp.period_start
                    JOIN memes m ON m.id = d.meme_id
                    WHERE m.is_public IS TRUE AND (:include_nsfw OR m.is_nsfw IS FALSE)
                    GROUP BY pp.period_start, d.meme_id
                ),
                period_totals AS (
                    SELECT
                        period_start,
                        count(*)::integer AS meme_count,
                        sum(snapshot_count)::integer AS period_snapshot_count
                    FROM period_meme
                    GROUP BY period_start
                ),
                ranked AS (
                    SELECT
                        period_meme.*,
                        row_number() OVER (
                            PARTITION BY period_start
                            ORDER BY popularity_score DESC, source_views DESC, platform_views DESC, meme_id DESC
                        ) AS rank
                    FROM period_meme
                )
                SELECT
                    ranked.*,
                    period_totals.meme_count,
                    period_totals.period_snapshot_count
                FROM ranked
                JOIN period_totals ON period_totals.period_start = ranked.period_start
                WHERE ranked.rank <= :top_limit
                ORDER BY ranked.period_start DESC, ranked.rank ASC
                """
            ),
            {
                "include_nsfw": include_nsfw,
                "meme_ids": [],
                "meme_id_count": 0,
                "limit": resolved_limit,
                "offset": resolved_offset,
                "top_limit": TIMELINE_TOP_MEMES_PER_PERIOD,
            },
        )
        rows = [dict(row) for row in result.mappings()]
        memes_by_id = await self._load_public_memes(tuple(cast("uuid.UUID", row["meme_id"]) for row in rows))
        _attach_period_popularity_scores(memes_by_id, rows)
        period_order: list[datetime] = []
        period_rows: dict[datetime, list[dict[str, object]]] = {}
        for row in rows:
            period_start = cast("datetime", row["period_start"])
            if period_start not in period_rows:
                period_order.append(period_start)
                period_rows[period_start] = []
            period_rows[period_start].append(row)

        periods: list[PublicTrendTimelinePeriodRead] = []
        for period_start in period_order:
            rows_for_period = period_rows[period_start]
            top_memes: list[PublicTrendTimelineMemeRead] = []
            for row in rows_for_period:
                meme = memes_by_id.get(cast("uuid.UUID", row["meme_id"]))
                if meme is None:
                    continue
                top_memes.append(
                    PublicTrendTimelineMemeRead(
                        meme=_to_public_card_read(meme, media_render_service=self._media_render_service),
                        popularity_score=_float(row.get("popularity_score")),
                        snapshot_count=_int(row.get("snapshot_count")),
                        first_captured_at=cast("datetime", row["first_captured_at"]),
                        last_captured_at=cast("datetime", row["last_captured_at"]),
                        source_views=_int(row.get("source_views")),
                        source_reactions=_int(row.get("source_reactions")),
                        source_reposts=_int(row.get("source_reposts")),
                        platform_views=_int(row.get("platform_views")),
                        platform_sends=_int(row.get("platform_sends")),
                        platform_saves=_int(row.get("platform_saves")),
                        platform_likes=_int(row.get("platform_likes")),
                    )
                )
            periods.append(
                PublicTrendTimelinePeriodRead(
                    period=_format_period(period_start, resolved_granularity),
                    period_start=period_start,
                    top_memes=top_memes,
                    meme_count=_int(rows_for_period[0].get("meme_count")),
                    snapshot_count=_int(rows_for_period[0].get("period_snapshot_count")),
                )
            )

        return PublicTrendTimelinePageRead(
            granularity=resolved_granularity,
            periods=periods,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total or 0,
            has_more=resolved_offset + resolved_limit < (total or 0),
        )

    async def _load_tag_history_points(
        self,
        tags: tuple[str, ...],
    ) -> dict[str, list[PublicTrendAggregatePointRead]]:
        normalized_tags = tuple(dict.fromkeys(tag.strip().lower() for tag in tags if tag.strip()))
        if not normalized_tags:
            return {}

        result = await self._session.execute(
            _tag_history_text(
                """
                SELECT
                    tag,
                    observed_at,
                    meme_count,
                    snapshot_count,
                    source_views,
                    source_reactions,
                    source_reposts,
                    platform_views,
                    platform_sends,
                    platform_saves,
                    platform_likes,
                    aggregate_value
                FROM public_tag_trend_points_mv
                WHERE tag = ANY(CAST(:tags AS varchar[]))
                ORDER BY tag ASC, observed_at ASC
                """
            ),
            {"tags": list(normalized_tags)},
        )
        points_by_tag: dict[str, list[PublicTrendAggregatePointRead]] = {}
        for row in result.mappings():
            row_dict = dict(row)
            points_by_tag.setdefault(str(row_dict["tag"]), []).append(_aggregate_point_from_row(row_dict))
        return points_by_tag

    async def _load_template_history_points(
        self,
        template_slugs: tuple[str, ...],
    ) -> dict[str, list[PublicTrendAggregatePointRead]]:
        normalized_slugs = tuple(dict.fromkeys(slug.strip().lower() for slug in template_slugs if slug.strip()))
        if not normalized_slugs:
            return {}

        result = await self._session.execute(
            _template_history_text(
                """
                SELECT
                    template_slug,
                    observed_at,
                    meme_count,
                    snapshot_count,
                    source_views,
                    source_reactions,
                    source_reposts,
                    platform_views,
                    platform_sends,
                    platform_saves,
                    platform_likes,
                    aggregate_value
                FROM public_template_trend_points_mv
                WHERE template_slug = ANY(CAST(:template_slugs AS varchar[]))
                ORDER BY template_slug ASC, observed_at ASC
                """
            ),
            {"template_slugs": list(normalized_slugs)},
        )
        points_by_slug: dict[str, list[PublicTrendAggregatePointRead]] = {}
        for row in result.mappings():
            row_dict = dict(row)
            points_by_slug.setdefault(str(row_dict["template_slug"]), []).append(
                _aggregate_point_from_row(row_dict)
            )
        return points_by_slug

    async def _load_public_memes(self, meme_ids: tuple[uuid.UUID, ...]) -> dict[uuid.UUID, Meme]:
        if not meme_ids:
            return {}
        orm_result = await self._session.execute(
            select(Meme)
            .options(selectinload(Meme.primary_file), selectinload(Meme.files), selectinload(Meme.seo_page))
            .where(Meme.id.in_(meme_ids), Meme.is_public.is_(True))
        )
        return {meme.id: meme for meme in orm_result.scalars().all()}

    async def _compare_meme(
        self,
        value: str,
        *,
        include_nsfw: bool,
        snapshot_limit: int,
    ) -> PublicTrendComparisonSeriesRead:
        meme = await self._resolve_public_meme(value, include_nsfw=include_nsfw)
        if meme is None:
            return PublicTrendComparisonSeriesRead(
                kind="meme",
                value=value,
                title=value,
                no_data_reason="No visible public meme matched this UUID or slug.",
            )
        trend_row = await self._session.execute(
            text(
                f"""
                SELECT
                    trend.*,
                    {_refresh_timestamp_sql("public_meme_trends_mv")}
                        AS materialized_view_refreshed_at
                FROM public_meme_trends_mv trend
                WHERE trend.meme_id = :meme_id
                """
            ),
            {"meme_id": meme.id},
        )
        trend = trend_row.mappings().first()
        if trend is not None:
            setattr(meme, _DERIVED_POPULARITY_ATTR, _float(dict(trend).get("latest_popularity_score")))
        snapshots = await self._session.execute(
            _meme_daily_points_text(
                f"""
                {_MEME_DAILY_POINT_CTES}
                SELECT
                    captured_at,
                    snapshot_count,
                    source_views,
                    source_reactions,
                    source_reposts,
                    platform_views,
                    platform_sends,
                    platform_saves,
                    platform_likes,
                    popularity_score
                FROM scored_meme_daily
                ORDER BY captured_at DESC
                LIMIT :limit
                """
            ),
            {"meme_ids": [meme.id], "meme_id_count": 1, "limit": max(1, min(365, snapshot_limit))},
        )
        snapshot_rows = list(reversed(list(snapshots.mappings())))
        card = _to_public_card_read(meme, media_render_service=self._media_render_service)
        return PublicTrendComparisonSeriesRead(
            kind="meme",
            value=value,
            title=(
                meme.seo_page.page_title
                if meme.seo_page is not None
                else (card.caption or f"Meme {str(meme.id)[:8]}")
            ),
            description="Derived popularity score from source engagement deltas and platform events.",
            meme=card,
            trend=_trend_metrics_from_row(dict(trend)) if trend is not None else None,
            points=[
                PublicTrendComparisonPointRead(
                    observed_at=cast("datetime", row["captured_at"]),
                    value=_float(row.get("popularity_score")),
                    metric="popularity_score",
                    label="Popularity score",
                    meme_count=1,
                    snapshot_count=_int(row.get("snapshot_count")),
                    source_views=_int(row.get("source_views")),
                    source_reactions=_int(row.get("source_reactions")),
                    source_reposts=_int(row.get("source_reposts")),
                    platform_views=_int(row.get("platform_views")),
                    platform_sends=_int(row.get("platform_sends")),
                    platform_saves=_int(row.get("platform_saves")),
                    platform_likes=_int(row.get("platform_likes")),
                )
                for row in snapshot_rows
            ],
            insufficient_history=len(snapshot_rows) < 2,
            no_data_reason=(
                "No source engagement or platform event points exist for this meme yet." if not snapshot_rows else None
            ),
        )

    async def _compare_tag(self, value: str) -> PublicTrendComparisonSeriesRead:
        summary = await self.tag_summary(value)
        if summary is None:
            return PublicTrendComparisonSeriesRead(
                kind="tag",
                value=value,
                title=f"#{value}",
                no_data_reason="No public tag trend aggregate exists for this tag.",
            )
        return _summary_comparison_series(summary, value=value)

    async def _compare_template(self, value: str) -> PublicTrendComparisonSeriesRead:
        summary = await self.template_summary(value)
        if summary is None:
            return PublicTrendComparisonSeriesRead(
                kind="template",
                value=value,
                title=value,
                no_data_reason="No public template trend aggregate exists for this template.",
            )
        return _summary_comparison_series(summary, value=value)

    async def _resolve_public_meme(self, value: str, *, include_nsfw: bool) -> Meme | None:
        meme_id = _parse_uuid(value)
        statement = select(Meme).options(
            selectinload(Meme.primary_file),
            selectinload(Meme.files),
            selectinload(Meme.seo_page),
        )
        if meme_id is not None:
            statement = statement.where(Meme.id == meme_id)
        else:
            statement = statement.join(MemeSeoPage).where(MemeSeoPage.slug == value.strip().lower())
        statement = statement.where(Meme.is_public.is_(True))
        if not include_nsfw:
            statement = statement.where(Meme.is_nsfw.is_(False))
        result = await self._session.execute(statement)
        return result.scalars().first()


async def refresh_public_trend_materialized_views(engine: AsyncEngine, *, concurrently: bool = True) -> None:
    """Refresh trend MVs in dependency order, preferring concurrent refreshes."""

    async with engine.connect() as connection:
        refresh_connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for view_name in TREND_MATERIALIZED_VIEWS:
            refreshed = False
            if concurrently:
                try:
                    await refresh_connection.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}"))
                except DBAPIError:
                    logger.warning(
                        "public_trend_mv_concurrent_refresh_fallback",
                        extra={
                            "event": "public_trend_mv_concurrent_refresh_fallback",
                            "view_name": view_name,
                        },
                        exc_info=True,
                    )
                    await refresh_connection.rollback()
                else:
                    refreshed = True
            if not refreshed:
                await refresh_connection.execute(text(f"REFRESH MATERIALIZED VIEW {view_name}"))
            await refresh_connection.execute(text(_RECORD_REFRESH_SQL), {"view_name": view_name})


def _refresh_timestamp_sql(view_name: str) -> str:
    """Return a fixed-name scalar read for a materialized view's completion time."""

    if view_name not in TREND_MATERIALIZED_VIEWS:
        raise ValueError(f"Unsupported materialized view refresh state: {view_name}")
    return (
        f"(SELECT refresh_state.refreshed_at FROM {_REFRESH_STATE_TABLE} refresh_state "
        f"WHERE refresh_state.view_name = '{view_name}')"
    )


def _ranking_filters_sql() -> str:
    return """
        m.is_public IS TRUE
        AND (:include_nsfw OR mt.is_nsfw IS FALSE)
        AND (CAST(:language AS text) IS NULL OR mt.language = CAST(:language AS text))
        AND (CAST(:media_type AS text) IS NULL OR mt.media_type = CAST(:media_type AS text))
        AND (:tag_count = 0 OR mt.tags @> CAST(:tags AS varchar[]))
    """


def _ranking_order_sql(ranking: PublicTrendRanking) -> str:
    if ranking == "fastest_rising":
        return """
            (
                (
                    mt.recent_view_count
                    + mt.recent_send_count * 3
                    + mt.recent_like_count * 5
                    + mt.recent_save_count * 4
                )
                - (
                    mt.previous_view_count
                    + mt.previous_send_count * 3
                    + mt.previous_like_count * 5
                    + mt.previous_save_count * 4
                )
            ) DESC,
            mt.trending_score DESC,
            mt.engagement_24h DESC,
            mt.meme_id DESC
        """
    if ranking == "most_liked":
        return """
            mt.recent_like_count DESC,
            mt.latest_platform_likes DESC,
            mt.trending_score DESC,
            mt.meme_id DESC
        """
    return "mt.trending_score DESC, mt.engagement_24h DESC, mt.latest_popularity_score DESC, mt.meme_id DESC"


def _typed_text(sql: str) -> TextClause:
    return text(sql).bindparams(bindparam("tags", type_=ARRAY(String)))


def _tag_history_text(sql: str) -> TextClause:
    return text(sql).bindparams(bindparam("tags", type_=ARRAY(String)))


def _template_history_text(sql: str) -> TextClause:
    return text(sql).bindparams(bindparam("template_slugs", type_=ARRAY(String)))


def _meme_daily_points_text(sql: str) -> TextClause:
    return text(sql).bindparams(bindparam("meme_ids", type_=ARRAY(PGUUID(as_uuid=True))))


def _attach_row_popularity_scores(memes_by_id: dict[uuid.UUID, Meme], rows: list[dict[str, object]]) -> None:
    for row in rows:
        meme = memes_by_id.get(cast("uuid.UUID", row.get("meme_id")))
        if meme is not None:
            setattr(meme, _DERIVED_POPULARITY_ATTR, _float(row.get("latest_popularity_score")))


def _attach_period_popularity_scores(memes_by_id: dict[uuid.UUID, Meme], rows: list[dict[str, object]]) -> None:
    scores: dict[uuid.UUID, float] = {}
    for row in rows:
        meme_id = cast("uuid.UUID", row.get("meme_id"))
        scores[meme_id] = max(scores.get(meme_id, 0.0), _float(row.get("popularity_score")))
    for meme_id, score in scores.items():
        if meme := memes_by_id.get(meme_id):
            setattr(meme, _DERIVED_POPULARITY_ATTR, score)


_POINT_SCORE_EXPR = """
    ln(1.0 + GREATEST(COALESCE(source_views, 0), 0)) * 1.0
    + ln(1.0 + GREATEST(COALESCE(source_reactions, 0), 0)) * 2.0
    + ln(1.0 + GREATEST(COALESCE(source_reposts, 0), 0)) * 3.0
    + ln(1.0 + GREATEST(COALESCE(platform_views, 0), 0)) * 1.0
    + ln(1.0 + GREATEST(COALESCE(platform_sends, 0), 0)) * 3.0
    + ln(1.0 + GREATEST(COALESCE(platform_saves, 0), 0)) * 4.0
    + ln(1.0 + GREATEST(COALESCE(platform_likes, 0), 0)) * 5.0
"""


_MEME_DAILY_POINT_CTES = """
WITH requested AS (
    SELECT unnest(CAST(:meme_ids AS uuid[])) AS meme_id
),
safe_events AS (
    SELECT
        CASE
            WHEN jsonb_typeof(payload -> 'meme_id') = 'string'
            AND payload ->> 'meme_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (payload ->> 'meme_id')::uuid
            WHEN jsonb_typeof(payload -> 'refs' -> 'meme_id') = 'string'
            AND payload -> 'refs' ->> 'meme_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (payload -> 'refs' ->> 'meme_id')::uuid
            ELSE NULL
        END AS meme_id,
        event_type::text AS event_type,
        occurred_at
    FROM analytics_events
    WHERE event_type::text IN (
        'meme_view',
        'view',
        'meme_send',
        'share',
        'meme_share',
        'meme_like',
        'favorite',
        'meme_save',
        'save'
    )
),
event_daily AS (
    SELECT
        se.meme_id,
        date_trunc('day', se.occurred_at, 'UTC') AS captured_at,
        count(*) FILTER (WHERE se.event_type IN ('meme_view', 'view'))::integer AS platform_views,
        count(*) FILTER (
            WHERE se.event_type IN ('meme_send', 'share', 'meme_share')
        )::integer AS platform_sends,
        count(*) FILTER (WHERE se.event_type IN ('meme_save', 'save'))::integer AS platform_saves,
        count(*) FILTER (WHERE se.event_type IN ('meme_like', 'favorite'))::integer AS platform_likes
    FROM safe_events se
    LEFT JOIN requested r ON r.meme_id = se.meme_id
    WHERE se.meme_id IS NOT NULL
      AND (:meme_id_count = 0 OR r.meme_id IS NOT NULL)
    GROUP BY se.meme_id, date_trunc('day', se.occurred_at, 'UTC')
),
successful_source_snapshots AS (
    SELECT
        mf.meme_id,
        ses.meme_source_id,
        ses.id,
        ses.captured_at,
        ses.view_count,
        ses.reaction_count,
        ses.forward_count,
        max(ses.view_count) OVER (
            PARTITION BY ses.meme_source_id
            ORDER BY ses.captured_at, ses.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS previous_view_high,
        max(ses.reaction_count) OVER (
            PARTITION BY ses.meme_source_id
            ORDER BY ses.captured_at, ses.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS previous_reaction_high,
        max(ses.forward_count) OVER (
            PARTITION BY ses.meme_source_id
            ORDER BY ses.captured_at, ses.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS previous_forward_high
    FROM meme_source_engagement_snapshots ses
    JOIN meme_sources ms ON ms.id = ses.meme_source_id
    JOIN meme_files mf ON mf.id = ms.file_id
    LEFT JOIN requested r ON r.meme_id = mf.meme_id
    WHERE ses.fetch_status::text = 'success'
      AND (:meme_id_count = 0 OR r.meme_id IS NOT NULL)
),
source_daily AS (
    SELECT
        meme_id,
        date_trunc('day', captured_at, 'UTC') AS captured_at,
        count(*)::integer AS snapshot_count,
        sum(
            CASE
                WHEN previous_view_high IS NULL OR view_count IS NULL THEN 0
                ELSE GREATEST(view_count - previous_view_high, 0)
            END
        )::integer AS source_views,
        sum(
            CASE
                WHEN previous_reaction_high IS NULL OR reaction_count IS NULL THEN 0
                ELSE GREATEST(reaction_count - previous_reaction_high, 0)
            END
        )::integer AS source_reactions,
        sum(
            CASE
                WHEN previous_forward_high IS NULL OR forward_count IS NULL THEN 0
                ELSE GREATEST(forward_count - previous_forward_high, 0)
            END
        )::integer AS source_reposts
    FROM successful_source_snapshots
    GROUP BY meme_id, date_trunc('day', captured_at, 'UTC')
),
meme_daily AS (
    SELECT
        COALESCE(sd.meme_id, ed.meme_id) AS meme_id,
        COALESCE(sd.captured_at, ed.captured_at) AS captured_at,
        COALESCE(sd.snapshot_count, 0)::integer AS snapshot_count,
        COALESCE(sd.source_views, 0)::integer AS source_views,
        COALESCE(sd.source_reactions, 0)::integer AS source_reactions,
        COALESCE(sd.source_reposts, 0)::integer AS source_reposts,
        COALESCE(ed.platform_views, 0)::integer AS platform_views,
        COALESCE(ed.platform_sends, 0)::integer AS platform_sends,
        COALESCE(ed.platform_saves, 0)::integer AS platform_saves,
        COALESCE(ed.platform_likes, 0)::integer AS platform_likes
    FROM source_daily sd
    FULL OUTER JOIN event_daily ed ON ed.meme_id = sd.meme_id AND ed.captured_at = sd.captured_at
),
scored_meme_daily AS (
    SELECT
        *,
        (__POINT_SCORE_EXPR__)::double precision AS popularity_score
    FROM meme_daily
    WHERE source_views > 0
       OR source_reactions > 0
       OR source_reposts > 0
       OR platform_views > 0
       OR platform_sends > 0
       OR platform_saves > 0
       OR platform_likes > 0
)
""".replace("__POINT_SCORE_EXPR__", _POINT_SCORE_EXPR)


def _trend_metrics_from_row(row: dict[str, object]) -> PublicTrendMetricsRead:
    return PublicTrendMetricsRead(
        recent=PublicTrendCountsRead(
            views=_int(row.get("recent_view_count")),
            sends=_int(row.get("recent_send_count")),
            likes=_int(row.get("recent_like_count")),
            saves=_int(row.get("recent_save_count")),
            downloads=_int(row.get("recent_download_count")),
        ),
        previous=PublicTrendCountsRead(
            views=_int(row.get("previous_view_count")),
            sends=_int(row.get("previous_send_count")),
            likes=_int(row.get("previous_like_count")),
            saves=_int(row.get("previous_save_count")),
            downloads=_int(row.get("previous_download_count")),
        ),
        latest_snapshot_at=cast("datetime | None", row.get("latest_snapshot_at")),
        latest_source_views=_int(row.get("latest_source_views")),
        latest_source_reactions=_int(row.get("latest_source_reactions")),
        latest_source_reposts=_int(row.get("latest_source_reposts")),
        latest_platform_views=_int(row.get("latest_platform_views")),
        latest_platform_sends=_int(row.get("latest_platform_sends")),
        latest_platform_saves=_int(row.get("latest_platform_saves")),
        latest_platform_likes=_int(row.get("latest_platform_likes")),
        latest_popularity_score=_float(row.get("latest_popularity_score")),
        engagement_24h=_float(row.get("engagement_24h")),
        trending_score=_float(row.get("trending_score")),
        refreshed_at=cast(
            "datetime | None",
            row.get("materialized_view_refreshed_at") or row.get("refreshed_at"),
        ),
    )


def _trend_score_from_row(row: dict[str, object], *, ranking: PublicTrendRanking) -> float:
    components = _trend_score_components_from_row(row, ranking=ranking)
    if ranking == "fastest_rising":
        return components["delta"]
    if ranking == "most_liked":
        return components["recent_likes"]
    return components["trending"]


def _trend_score_components_from_row(row: dict[str, object], *, ranking: PublicTrendRanking) -> dict[str, float]:
    trending = _float(row.get("trending_score"))
    engagement = _float(row.get("engagement_24h"))
    latest_popularity = _float(row.get("latest_popularity_score"))
    if ranking == "fastest_rising":
        recent_weighted = _weighted_trend_event_count(row, prefix="recent")
        previous_weighted = _weighted_trend_event_count(row, prefix="previous")
        return {
            "recent_weighted": recent_weighted,
            "previous_weighted": previous_weighted,
            "delta": recent_weighted - previous_weighted,
            "trending": trending,
            "engagement_24h": engagement,
        }
    if ranking == "most_liked":
        return {
            "recent_likes": float(_int(row.get("recent_like_count"))),
            "latest_platform_likes": float(_int(row.get("latest_platform_likes"))),
            "trending": trending,
        }
    return {
        "trending": trending,
        "engagement_24h": engagement,
        "latest_popularity": latest_popularity,
    }


def _weighted_trend_event_count(row: dict[str, object], *, prefix: Literal["recent", "previous"]) -> float:
    return (
        _float(row.get(f"{prefix}_view_count"))
        + _float(row.get(f"{prefix}_send_count")) * 3.0
        + _float(row.get(f"{prefix}_like_count")) * 5.0
        + _float(row.get(f"{prefix}_save_count")) * 4.0
    )


def _aggregate_point_from_row(row: dict[str, object]) -> PublicTrendAggregatePointRead:
    return PublicTrendAggregatePointRead(
        observed_at=cast("datetime", row["observed_at"]),
        value=_float(row.get("aggregate_value")),
        metric="aggregate_popularity_score",
        label="Aggregate popularity score",
        meme_count=_int(row.get("meme_count")),
        snapshot_count=_int(row.get("snapshot_count")),
        source_views=_int(row.get("source_views")),
        source_reactions=_int(row.get("source_reactions")),
        source_reposts=_int(row.get("source_reposts")),
        platform_views=_int(row.get("platform_views")),
        platform_sends=_int(row.get("platform_sends")),
        platform_saves=_int(row.get("platform_saves")),
        platform_likes=_int(row.get("platform_likes")),
    )


def _tag_summary_from_row(
    row: dict[str, object],
    *,
    points: list[PublicTrendAggregatePointRead],
) -> PublicTrendSummaryRead:
    tag = str(row["tag"])
    insufficient_history, current_only_reason = _summary_history_state(points)
    return PublicTrendSummaryRead(
        kind="tag",
        slug=tag,
        title=f"{tag.replace('-', ' ').title()} memes",
        description=f"Aggregate public trend activity for memes tagged {tag}.",
        meme_count=_int(row.get("meme_count")),
        trend=_trend_metrics_from_row(row),
        points=points,
        insufficient_history=insufficient_history,
        current_only_reason=current_only_reason,
    )


def _template_summary_from_row(
    row: dict[str, object],
    *,
    points: list[PublicTrendAggregatePointRead],
) -> PublicTrendSummaryRead:
    insufficient_history, current_only_reason = _summary_history_state(points)
    return PublicTrendSummaryRead(
        kind="template",
        slug=str(row["template_slug"]),
        title=f"{row['template_name']} memes",
        description=cast("str | None", row.get("template_description")),
        meme_count=_int(row.get("meme_count")),
        trend=_trend_metrics_from_row(row),
        points=points,
        insufficient_history=insufficient_history,
        current_only_reason=current_only_reason,
    )


def _summary_history_state(points: list[PublicTrendAggregatePointRead]) -> tuple[bool, str | None]:
    if len(points) >= 2:
        return False, None
    if points:
        return True, None
    return True, "No historical aggregate snapshot points exist; using the current public trend window only."


def _current_window_point_from_summary(summary: PublicTrendSummaryRead) -> PublicTrendComparisonPointRead:
    return PublicTrendComparisonPointRead(
        observed_at=summary.trend.refreshed_at or summary.trend.latest_snapshot_at,
        value=summary.trend.trending_score,
        metric="trending_score",
        label="Current public trend window",
        meme_count=summary.meme_count,
        source_views=summary.trend.latest_source_views,
        source_reactions=summary.trend.latest_source_reactions,
        source_reposts=summary.trend.latest_source_reposts,
        platform_views=summary.trend.latest_platform_views,
        platform_sends=summary.trend.latest_platform_sends,
        platform_saves=summary.trend.latest_platform_saves,
        platform_likes=summary.trend.latest_platform_likes,
    )


def _comparison_point_from_aggregate_point(point: PublicTrendAggregatePointRead) -> PublicTrendComparisonPointRead:
    return PublicTrendComparisonPointRead(**point.model_dump())


def _summary_comparison_series(summary: PublicTrendSummaryRead, *, value: str) -> PublicTrendComparisonSeriesRead:
    points = [_comparison_point_from_aggregate_point(point) for point in summary.points]
    current_only_reason = summary.current_only_reason
    description = summary.description
    if not points:
        points = [_current_window_point_from_summary(summary)]
        current_only_reason = current_only_reason or (
            "No historical aggregate snapshot points exist; using the current public trend window only."
        )
        description = f"{summary.description or ''} Current-window aggregate only.".strip()
    return PublicTrendComparisonSeriesRead(
        kind=summary.kind,
        value=value,
        title=summary.title,
        description=description,
        trend=summary.trend,
        points=points,
        insufficient_history=len(summary.points) < 2,
        current_only_reason=current_only_reason,
    )


def _parse_comparison_item(raw_item: str) -> tuple[Literal["meme", "tag", "template"], str] | None:
    kind, separator, value = raw_item.strip().partition(":")
    if separator != ":":
        return None
    normalized_kind = kind.strip().lower()
    normalized_value = value.strip().lower()
    if normalized_kind not in {"meme", "tag", "template"} or not normalized_value:
        return None
    return cast("tuple[Literal['meme', 'tag', 'template'], str]", (normalized_kind, normalized_value))


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _format_period(period_start: datetime, granularity: PublicTrendTimelineGranularity) -> str:
    if granularity == "year":
        return period_start.strftime("%Y")
    return period_start.strftime("%Y-%m")


def _int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str | bytes | bytearray):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if value is None:
        return 0
    raise TypeError(f"Expected numeric trend value, got {type(value).__name__}")


def _float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str | bytes | bytearray):
        return float(value)
    if value is None:
        return 0.0
    raise TypeError(f"Expected numeric trend value, got {type(value).__name__}")


def _clamp_limit(limit: int) -> int:
    return min(100, max(1, limit))


__all__ = [
    "PublicTrendRanking",
    "PublicTrendTimelineGranularity",
    "PublicTrendsService",
    "TREND_MATERIALIZED_VIEWS",
    "refresh_public_trend_materialized_views",
]
