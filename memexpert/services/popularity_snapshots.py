"""Scheduled popularity snapshot computation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import log1p
from typing import TYPE_CHECKING

from sqlalchemy import text

from memexpert.core.config import Settings, get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_UUID_TEXT_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_SCORE_COMPONENTS = (
    ("source_views", "source_view"),
    ("source_reactions", "source_reaction"),
    ("source_reposts", "source_repost"),
    ("platform_views", "platform_view"),
    ("platform_sends", "platform_send"),
    ("platform_saves", "platform_save"),
    ("platform_likes", "platform_like"),
)


@dataclass(frozen=True, slots=True)
class PopularitySnapshotWeights:
    """Tunable weights for the static log-scaled popularity formula."""

    source_view: float
    source_reaction: float
    source_repost: float
    platform_view: float
    platform_send: float
    platform_save: float
    platform_like: float

    @classmethod
    def from_settings(cls, settings: Settings) -> PopularitySnapshotWeights:
        return cls(
            source_view=settings.scheduler_popularity_source_view_weight,
            source_reaction=settings.scheduler_popularity_source_reaction_weight,
            source_repost=settings.scheduler_popularity_source_repost_weight,
            platform_view=settings.scheduler_popularity_platform_view_weight,
            platform_send=settings.scheduler_popularity_platform_send_weight,
            platform_save=settings.scheduler_popularity_platform_save_weight,
            platform_like=settings.scheduler_popularity_platform_like_weight,
        )

    def sql_params(self) -> dict[str, float]:
        return {
            "source_view_weight": self.source_view,
            "source_reaction_weight": self.source_reaction,
            "source_repost_weight": self.source_repost,
            "platform_view_weight": self.platform_view,
            "platform_send_weight": self.platform_send,
            "platform_save_weight": self.platform_save,
            "platform_like_weight": self.platform_like,
        }


@dataclass(frozen=True, slots=True)
class PopularitySnapshotCaptureResult:
    """Summary of one popularity snapshot capture run."""

    captured_at: datetime
    public_meme_count: int
    snapshot_count: int
    updated_meme_count: int


def calculate_popularity_score(
    *,
    source_views: int,
    source_reactions: int,
    source_reposts: int,
    platform_views: int,
    platform_sends: int,
    platform_saves: int,
    platform_likes: int,
    weights: PopularitySnapshotWeights,
) -> float:
    """Return the static log-scaled popularity score for the supplied metrics."""

    return (
        log1p(max(0, source_views)) * weights.source_view
        + log1p(max(0, source_reactions)) * weights.source_reaction
        + log1p(max(0, source_reposts)) * weights.source_repost
        + log1p(max(0, platform_views)) * weights.platform_view
        + log1p(max(0, platform_sends)) * weights.platform_send
        + log1p(max(0, platform_saves)) * weights.platform_save
        + log1p(max(0, platform_likes)) * weights.platform_like
    )


class PopularitySnapshotService:
    """Capture cumulative public meme popularity snapshots from persisted aggregates/events."""

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._weights = PopularitySnapshotWeights.from_settings(self._settings)

    async def capture(
        self,
        *,
        captured_at: datetime | None = None,
        commit: bool = True,
    ) -> PopularitySnapshotCaptureResult:
        resolved_captured_at = captured_at or datetime.now(UTC)
        logger.info(
            "popularity_snapshot_capture_started",
            extra={
                "event": "popularity_snapshot_capture_started",
                "captured_at": resolved_captured_at.isoformat(),
            },
        )

        params = {
            "captured_at": resolved_captured_at,
            "uuid_text_re": _UUID_TEXT_RE,
            **self._weights.sql_params(),
        }
        try:
            result = await self._session.execute(_CAPTURE_POPULARITY_SNAPSHOTS_SQL, params)
            row = result.mappings().one()
            capture_result = PopularitySnapshotCaptureResult(
                captured_at=resolved_captured_at,
                public_meme_count=int(row["public_meme_count"] or 0),
                snapshot_count=int(row["snapshot_count"] or 0),
                updated_meme_count=int(row["updated_meme_count"] or 0),
            )
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except Exception:
            if commit:
                await self._session.rollback()
            raise

        logger.info(
            "popularity_snapshot_capture_succeeded",
            extra={
                "event": "popularity_snapshot_capture_succeeded",
                "captured_at": capture_result.captured_at.isoformat(),
                "public_meme_count": capture_result.public_meme_count,
                "snapshot_count": capture_result.snapshot_count,
                "updated_meme_count": capture_result.updated_meme_count,
            },
        )
        return capture_result


async def capture_popularity_snapshots(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    captured_at: datetime | None = None,
    commit: bool = True,
) -> PopularitySnapshotCaptureResult:
    """Capture one scheduled popularity snapshot batch."""

    service = PopularitySnapshotService(session, settings=settings)
    return await service.capture(captured_at=captured_at, commit=commit)


def _popularity_score_sql() -> str:
    return "\n                    + ".join(
        f"ln(1.0 + GREATEST(COALESCE({metric_name}, 0), 0)) * :{weight_name}_weight"
        for metric_name, weight_name in _SCORE_COMPONENTS
    )


_CAPTURE_POPULARITY_SNAPSHOTS_SQL = text(
    f"""
    WITH public_memes AS (
        SELECT id
        FROM memes
        WHERE is_public IS TRUE
    ),
    source_metrics AS (
        SELECT
            mf.meme_id,
            COALESCE(sum(ms.views), 0)::integer AS source_views,
            COALESCE(sum(reaction_totals.reaction_count), 0)::integer AS source_reactions,
            count(*) FILTER (
                WHERE ms.forwarded_from_source_id IS NOT NULL
                OR ms.forwarded_from_post_id IS NOT NULL
            )::integer AS source_reposts
        FROM meme_files mf
        JOIN public_memes pm ON pm.id = mf.meme_id
        JOIN meme_sources ms ON ms.file_id = mf.id
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                sum(
                    CASE
                        WHEN reaction_value ~ '^[0-9]+$' THEN reaction_value::integer
                        ELSE 0
                    END
                ),
                0
            )::integer AS reaction_count
            FROM jsonb_each_text(COALESCE(ms.reactions, '{{}}'::jsonb)) AS reactions(reaction_name, reaction_value)
        ) reaction_totals ON TRUE
        GROUP BY mf.meme_id
    ),
    safe_events AS (
        SELECT
            CASE
                WHEN jsonb_typeof(payload -> 'meme_id') = 'string'
                AND payload ->> 'meme_id' ~* :uuid_text_re
                THEN (payload ->> 'meme_id')::uuid
                WHEN jsonb_typeof(payload -> 'refs' -> 'meme_id') = 'string'
                AND payload -> 'refs' ->> 'meme_id' ~* :uuid_text_re
                THEN (payload -> 'refs' ->> 'meme_id')::uuid
                ELSE NULL
            END AS meme_id,
            event_type::text AS event_type
        FROM analytics_events
        WHERE event_type::text IN (
            'meme_view',
            'view',
            'meme_send',
            'share',
            'meme_save',
            'save',
            'meme_like',
            'favorite'
        )
    ),
    event_metrics AS (
        SELECT
            se.meme_id,
            count(*) FILTER (WHERE se.event_type IN ('meme_view', 'view'))::integer AS platform_views,
            count(*) FILTER (WHERE se.event_type IN ('meme_send', 'share'))::integer AS platform_sends,
            count(*) FILTER (WHERE se.event_type IN ('meme_save', 'save'))::integer AS platform_saves,
            count(*) FILTER (WHERE se.event_type IN ('meme_like', 'favorite'))::integer AS platform_likes
        FROM safe_events se
        JOIN public_memes pm ON pm.id = se.meme_id
        GROUP BY se.meme_id
    ),
    metric_inputs AS (
        SELECT
            pm.id AS meme_id,
            COALESCE(sm.source_views, 0)::integer AS source_views,
            COALESCE(sm.source_reactions, 0)::integer AS source_reactions,
            COALESCE(sm.source_reposts, 0)::integer AS source_reposts,
            COALESCE(em.platform_views, 0)::integer AS platform_views,
            COALESCE(em.platform_sends, 0)::integer AS platform_sends,
            COALESCE(em.platform_saves, 0)::integer AS platform_saves,
            COALESCE(em.platform_likes, 0)::integer AS platform_likes
        FROM public_memes pm
        LEFT JOIN source_metrics sm ON sm.meme_id = pm.id
        LEFT JOIN event_metrics em ON em.meme_id = pm.id
    ),
    scored_metrics AS (
        SELECT
            meme_id,
            CAST(:captured_at AS timestamp with time zone) AS captured_at,
            source_views,
            source_reactions,
            source_reposts,
            platform_views,
            platform_sends,
            platform_saves,
            platform_likes,
            (
                {_popularity_score_sql()}
            )::double precision AS popularity_score
        FROM metric_inputs
    ),
    upserted AS (
        INSERT INTO meme_popularity_snapshots (
            id,
            meme_id,
            captured_at,
            source_views,
            source_reactions,
            source_reposts,
            platform_views,
            platform_sends,
            platform_saves,
            platform_likes,
            popularity_score
        )
        SELECT
            gen_random_uuid(),
            meme_id,
            captured_at,
            source_views,
            source_reactions,
            source_reposts,
            platform_views,
            platform_sends,
            platform_saves,
            platform_likes,
            popularity_score
        FROM scored_metrics
        ON CONFLICT (meme_id, captured_at) DO UPDATE SET
            source_views = EXCLUDED.source_views,
            source_reactions = EXCLUDED.source_reactions,
            source_reposts = EXCLUDED.source_reposts,
            platform_views = EXCLUDED.platform_views,
            platform_sends = EXCLUDED.platform_sends,
            platform_saves = EXCLUDED.platform_saves,
            platform_likes = EXCLUDED.platform_likes,
            popularity_score = EXCLUDED.popularity_score
        RETURNING meme_id, popularity_score
    ),
    updated_memes AS (
        UPDATE memes m
        SET popularity_score = u.popularity_score
        FROM upserted u
        WHERE m.id = u.meme_id
        RETURNING m.id
    )
    SELECT
        (SELECT count(*) FROM metric_inputs)::integer AS public_meme_count,
        (SELECT count(*) FROM upserted)::integer AS snapshot_count,
        (SELECT count(*) FROM updated_memes)::integer AS updated_meme_count
    """
)


__all__ = [
    "PopularitySnapshotCaptureResult",
    "PopularitySnapshotService",
    "PopularitySnapshotWeights",
    "calculate_popularity_score",
    "capture_popularity_snapshots",
]
