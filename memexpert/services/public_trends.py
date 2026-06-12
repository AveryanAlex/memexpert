# ruff: noqa: TC001
"""MV-backed public trend analytics reads and refresh helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import String, TextClause, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload

from memexpert.models.content import Meme
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.schemas.meme import (
    PublicMemePopularityPointRead,
    PublicMemePopularitySummaryRead,
    PublicMemeTrendPageRead,
    PublicMemeTrendRead,
    PublicTrendCountsRead,
    PublicTrendMetricsRead,
    PublicTrendSummaryRead,
)
from memexpert.services.media_render_urls import MediaRenderUrlService
from memexpert.services.meme_search import _to_public_card_read

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

TREND_MATERIALIZED_VIEWS = (
    "public_meme_trends_mv",
    "public_tag_trends_mv",
    "public_template_trends_mv",
)

type PublicTrendRanking = Literal["trending", "fastest_rising", "most_liked"]


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
    ) -> PublicMemeTrendPageRead:
        """Return MV-ranked public memes for the requested ranking mode."""

        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
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
                SELECT mt.*
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
        items = [
            PublicMemeTrendRead(
                meme=_to_public_card_read(meme, media_render_service=self._media_render_service),
                trend=_trend_metrics_from_row(row),
            )
            for row in rows
            if (meme := memes_by_id.get(cast("uuid.UUID", row["meme_id"]))) is not None
        ]
        return PublicMemeTrendPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total or 0,
            has_more=resolved_offset + resolved_limit < (total or 0),
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
            text("SELECT * FROM public_meme_trends_mv WHERE meme_id = :meme_id"),
            {"meme_id": meme_id},
        )
        trend = trend_row.mappings().first()
        snapshots = await self._session.execute(
            text(
                """
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
                FROM meme_popularity_snapshots
                WHERE meme_id = :meme_id
                ORDER BY captured_at DESC
                LIMIT :limit
                """
            ),
            {"meme_id": meme_id, "limit": max(1, min(120, snapshot_limit))},
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
                """
                SELECT *
                FROM public_tag_trends_mv
                ORDER BY trending_score DESC, engagement_24h DESC, tag ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": _clamp_limit(limit), "offset": max(0, offset)},
        )
        return [_tag_summary_from_row(dict(row)) for row in result.mappings()]

    async def template_summaries(self, *, limit: int = 20, offset: int = 0) -> list[PublicTrendSummaryRead]:
        """Return top safe public template trend summaries."""

        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM public_template_trends_mv
                ORDER BY trending_score DESC, engagement_24h DESC, template_slug ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": _clamp_limit(limit), "offset": max(0, offset)},
        )
        return [_template_summary_from_row(dict(row)) for row in result.mappings()]

    async def tag_summary(self, tag: str) -> PublicTrendSummaryRead | None:
        """Return one safe public tag trend summary."""

        normalized_tag = tag.strip().lower()
        if not normalized_tag:
            return None
        result = await self._session.execute(
            text("SELECT * FROM public_tag_trends_mv WHERE tag = :tag"),
            {"tag": normalized_tag},
        )
        row = result.mappings().first()
        return _tag_summary_from_row(dict(row)) if row is not None else None

    async def template_summary(self, template_slug: str) -> PublicTrendSummaryRead | None:
        """Return one safe public template trend summary."""

        normalized_slug = template_slug.strip().lower()
        if not normalized_slug:
            return None
        result = await self._session.execute(
            text("SELECT * FROM public_template_trends_mv WHERE template_slug = :template_slug"),
            {"template_slug": normalized_slug},
        )
        row = result.mappings().first()
        return _template_summary_from_row(dict(row)) if row is not None else None

    async def _load_public_memes(self, meme_ids: tuple[uuid.UUID, ...]) -> dict[uuid.UUID, Meme]:
        if not meme_ids:
            return {}
        orm_result = await self._session.execute(
            select(Meme)
            .options(selectinload(Meme.primary_file), selectinload(Meme.files), selectinload(Meme.seo_page))
            .where(Meme.id.in_(meme_ids), Meme.is_public.is_(True))
        )
        return {meme.id: meme for meme in orm_result.scalars().all()}


async def refresh_public_trend_materialized_views(engine: AsyncEngine, *, concurrently: bool = True) -> None:
    """Refresh trend MVs in dependency order, preferring concurrent refreshes."""

    async with engine.connect() as connection:
        refresh_connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for view_name in TREND_MATERIALIZED_VIEWS:
            if concurrently:
                try:
                    await refresh_connection.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}"))
                    continue
                except DBAPIError:
                    logger.exception("Concurrent refresh failed for %s; retrying without CONCURRENTLY.", view_name)
                    await refresh_connection.rollback()
            await refresh_connection.execute(text(f"REFRESH MATERIALIZED VIEW {view_name}"))


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
                    + mt.recent_download_count * 2
                )
                - (
                    mt.previous_view_count
                    + mt.previous_send_count * 3
                    + mt.previous_like_count * 5
                    + mt.previous_save_count * 4
                    + mt.previous_download_count * 2
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
        refreshed_at=cast("datetime | None", row.get("refreshed_at")),
    )


def _tag_summary_from_row(row: dict[str, object]) -> PublicTrendSummaryRead:
    tag = str(row["tag"])
    return PublicTrendSummaryRead(
        kind="tag",
        slug=tag,
        title=f"{tag.replace('-', ' ').title()} memes",
        description=f"Aggregate public trend activity for memes tagged {tag}.",
        meme_count=_int(row.get("meme_count")),
        trend=_trend_metrics_from_row(row),
    )


def _template_summary_from_row(row: dict[str, object]) -> PublicTrendSummaryRead:
    return PublicTrendSummaryRead(
        kind="template",
        slug=str(row["template_slug"]),
        title=f"{row['template_name']} memes",
        description=cast("str | None", row.get("template_description")),
        meme_count=_int(row.get("meme_count")),
        trend=_trend_metrics_from_row(row),
    )


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
    "PublicTrendsService",
    "TREND_MATERIALIZED_VIEWS",
    "refresh_public_trend_materialized_views",
]
