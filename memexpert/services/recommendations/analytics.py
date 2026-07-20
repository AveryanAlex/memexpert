# ruff: noqa: TC003
"""Bounded, idempotent daily recommendation analytics rollups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, select, text

from memexpert.models.recommendation import RecommendationDailyAggregate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class RecommendationAnalyticsRollupResult:
    """Summary emitted by the scheduler without exposing event payloads."""

    start_date: date
    end_date: date
    aggregate_rows: int


async def rollup_recommendation_daily_analytics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    through_date: date | None = None,
    lookback_days: int = 2,
    impression_cooldown_hours: int = 72,
    strong_positive_cooldown_hours: int = 168,
) -> RecommendationAnalyticsRollupResult:
    """Replace a small UTC date window of dashboard rows atomically.

    Recomputing today and yesterday makes late keepalive retries visible while
    bounding every scheduler run. Raw interaction rows remain authoritative.
    """

    resolved_end_date = through_date or datetime.now(UTC).date()
    resolved_lookback = max(1, min(31, lookback_days))
    resolved_impression_cooldown = max(1, min(720, impression_cooldown_hours))
    resolved_strong_positive_cooldown = max(1, min(2160, strong_positive_cooldown_hours))
    resolved_start_date = resolved_end_date - timedelta(days=resolved_lookback - 1)
    window_start = datetime.combine(resolved_start_date, time.min, tzinfo=UTC)
    window_end = datetime.combine(resolved_end_date + timedelta(days=1), time.min, tzinfo=UTC)
    history_start = window_start - timedelta(
        hours=max(resolved_impression_cooldown, resolved_strong_positive_cooldown)
    )

    async with session_factory() as session:
        try:
            await session.execute(
                delete(RecommendationDailyAggregate).where(
                    RecommendationDailyAggregate.metric_date >= resolved_start_date,
                    RecommendationDailyAggregate.metric_date <= resolved_end_date,
                )
            )
            result = await session.execute(
                text(_ROLLUP_SQL),
                {
                    "history_start": history_start,
                    "window_start": window_start,
                    "window_end": window_end,
                    "impression_cooldown_hours": resolved_impression_cooldown,
                    "strong_positive_cooldown_hours": resolved_strong_positive_cooldown,
                },
            )
            rows = list(result)
            session.add_all(
                [
                    RecommendationDailyAggregate(
                        metric_date=cast("date", row.metric_date),
                        surface=str(row.surface),
                        algorithm_version=str(row.algorithm_version),
                        profile_version=str(row.profile_version),
                        candidate_source=str(row.candidate_source),
                        impression_count=int(row.impression_count),
                        strong_action_count=int(row.strong_action_count),
                        attributed_send_count=int(row.attributed_send_count),
                        result_count=int(row.result_count),
                        exploration_count=int(row.exploration_count),
                        # Cursor expiry is request-path structured telemetry,
                        # not an AnalyticsEvent fact. Keep the reserved schema
                        # column explicit rather than synthesizing event rows.
                        cache_expiry_count=0,
                        fallback_count=int(row.fallback_count),
                        metrics=cast("dict[str, object]", row.metrics),
                    )
                    for row in rows
                ]
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    return RecommendationAnalyticsRollupResult(
        start_date=resolved_start_date,
        end_date=resolved_end_date,
        aggregate_rows=len(rows),
    )


async def load_recommendation_daily_analytics(
    session: AsyncSession,
    *,
    start_date: date,
    end_date: date,
) -> tuple[RecommendationDailyAggregate, ...]:
    """Load bounded dashboard rows in stable dimensional order."""

    rows = await session.scalars(
        select(RecommendationDailyAggregate)
        .where(
            RecommendationDailyAggregate.metric_date >= start_date,
            RecommendationDailyAggregate.metric_date <= end_date,
        )
        .order_by(
            RecommendationDailyAggregate.metric_date,
            RecommendationDailyAggregate.surface,
            RecommendationDailyAggregate.algorithm_version,
            RecommendationDailyAggregate.profile_version,
            RecommendationDailyAggregate.candidate_source,
        )
    )
    return tuple(rows)


_ROLLUP_SQL = r"""
WITH normalized AS (
    SELECT
        ae.id,
        ae.user_id,
        ae.event_type::text AS event_type,
        ae.occurred_at,
        ae.payload -> 'properties' @> '{"attribution_trusted": true}'::jsonb
            AS attribution_trusted,
        timezone('UTC', ae.occurred_at)::date AS metric_date,
        left(COALESCE(NULLIF(btrim(ae.payload ->> 'surface'), ''), 'unknown'), 120) AS surface,
        left(
            CASE
                WHEN ae.payload -> 'properties' @> '{"attribution_trusted": true}'::jsonb
                THEN COALESCE(
                    NULLIF(btrim(ae.payload -> 'properties' ->> 'algorithm_version'), ''),
                    'unknown'
                )
                ELSE 'untrusted'
            END,
            120
        ) AS algorithm_version,
        left(
            CASE
                WHEN ae.payload -> 'properties' @> '{"attribution_trusted": true}'::jsonb
                THEN COALESCE(
                    NULLIF(btrim(ae.payload -> 'properties' ->> 'profile_version'), ''),
                    'none'
                )
                ELSE 'untrusted'
            END,
            120
        ) AS profile_version,
        CASE
            WHEN jsonb_typeof(ae.payload -> 'refs' -> 'meme_id') = 'string'
             AND ae.payload -> 'refs' ->> 'meme_id'
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (ae.payload -> 'refs' ->> 'meme_id')::uuid
            ELSE NULL
        END AS meme_id,
        NULLIF(btrim(ae.payload ->> 'impression_id'), '') AS impression_id,
        lower(COALESCE(ae.payload -> 'properties' ->> 'action', '')) AS action,
        COALESCE(ae.payload ->> 'reason', '') AS reason,
        CASE
            WHEN ae.payload -> 'properties' @> '{"attribution_trusted": true}'::jsonb
             AND jsonb_typeof(ae.payload -> 'properties' -> 'candidate_sources') = 'array'
            THEN ae.payload -> 'properties' -> 'candidate_sources'
            ELSE '[]'::jsonb
        END AS candidate_sources
    FROM analytics_events ae
    WHERE ae.occurred_at >= :history_start
      AND ae.occurred_at < :window_end
      AND ae.event_type::text IN (
          'impression', 'meme_impression', 'inline_served',
          'favorite', 'meme_like', 'meme_save', 'save', 'meme_pin',
          'meme_download', 'meme_send', 'meme_share', 'share',
          'inline_chosen', 'inline_sent'
      )
),
canonical_impressions AS (
    SELECT DISTINCT ON (
        COALESCE(user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        meme_id,
        COALESCE(impression_id, 'event:' || id::text)
    )
        normalized.*,
        COALESCE(impression_id, 'event:' || id::text) AS exposure_key
    FROM normalized
    WHERE event_type IN ('impression', 'meme_impression', 'inline_served')
    ORDER BY
        COALESCE(user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        meme_id,
        COALESCE(impression_id, 'event:' || id::text),
        occurred_at,
        id
),
impressions AS (
    SELECT
        canonical_impressions.*,
        COALESCE(source.candidate_source, 'unknown') AS candidate_source
    FROM canonical_impressions
    LEFT JOIN LATERAL (
        -- Several long-term clusters may contribute to one item, but the
        -- public attribution contract intentionally exposes only their typed
        -- source. Count that keyed exposure once for the typed source.
        SELECT DISTINCT
            left(COALESCE(NULLIF(value ->> 'source', ''), 'unknown'), 120)
                AS candidate_source
        FROM jsonb_array_elements(canonical_impressions.candidate_sources) source(value)
    ) source ON TRUE
),
actions AS (
    SELECT
        normalized.*,
        (
            normalized.event_type IN (
                'meme_download', 'meme_send', 'meme_share', 'share',
                'inline_chosen', 'inline_sent'
            )
            OR (
                normalized.event_type IN ('favorite', 'meme_like', 'meme_save', 'save', 'meme_pin')
                AND normalized.action NOT IN (
                    'delete', 'deleted', 'remove', 'removed', 'remove_save',
                    'reorder', 'reorder_pin', 'unfavorite', 'unlike', 'unpin', 'unsave'
                )
            )
        ) AS is_strong_action,
        normalized.event_type IN ('meme_send', 'inline_sent') AS is_attributed_send
    FROM normalized
    WHERE normalized.event_type NOT IN ('impression', 'meme_impression', 'inline_served')
),
attributed_impressions AS (
    SELECT
        impressions.id,
        impressions.user_id,
        impressions.occurred_at,
        impressions.metric_date,
        impressions.surface,
        impressions.algorithm_version,
        impressions.profile_version,
        impressions.meme_id,
        impressions.impression_id,
        impressions.exposure_key,
        impressions.reason,
        impressions.candidate_source,
        bool_or(COALESCE(actions.is_strong_action, false)) AS has_strong_action,
        bool_or(COALESCE(actions.is_attributed_send, false)) AS has_attributed_send,
        (
            impressions.user_id IS NOT NULL
            AND (
                EXISTS (
                    SELECT 1
                    FROM canonical_impressions prior
                    WHERE prior.user_id = impressions.user_id
                      AND prior.meme_id = impressions.meme_id
                      AND (prior.occurred_at, prior.id) < (impressions.occurred_at, impressions.id)
                      AND prior.occurred_at >= impressions.occurred_at
                          - :impression_cooldown_hours * interval '1 hour'
                )
                OR EXISTS (
                    SELECT 1
                    FROM actions prior_action
                    WHERE prior_action.user_id = impressions.user_id
                      AND prior_action.meme_id = impressions.meme_id
                      AND prior_action.is_strong_action
                      AND (prior_action.occurred_at, prior_action.id)
                          < (impressions.occurred_at, impressions.id)
                      AND prior_action.occurred_at >= impressions.occurred_at
                          - :strong_positive_cooldown_hours * interval '1 hour'
                )
            )
        ) AS is_repeat_within_cooldown
    FROM impressions
    LEFT JOIN actions
      ON actions.impression_id IS NOT NULL
     AND actions.impression_id = impressions.impression_id
     AND actions.meme_id = impressions.meme_id
     AND actions.user_id IS NOT DISTINCT FROM impressions.user_id
     AND actions.attribution_trusted = impressions.attribution_trusted
     AND (actions.occurred_at, actions.id) >= (impressions.occurred_at, impressions.id)
    GROUP BY
        impressions.id,
        impressions.user_id,
        impressions.occurred_at,
        impressions.metric_date,
        impressions.surface,
        impressions.algorithm_version,
        impressions.profile_version,
        impressions.meme_id,
        impressions.impression_id,
        impressions.exposure_key,
        impressions.reason,
        impressions.candidate_source,
        impressions.attribution_trusted
),
target_impressions AS (
    SELECT *
    FROM attributed_impressions
    WHERE occurred_at >= :window_start
      AND occurred_at < :window_end
),
counts AS (
    SELECT
        metric_date,
        surface,
        algorithm_version,
        profile_version,
        candidate_source,
        count(*)::integer AS impression_count,
        count(*) FILTER (WHERE has_strong_action)::integer AS strong_action_count,
        count(*) FILTER (WHERE has_attributed_send)::integer AS attributed_send_count,
        count(*)::integer AS result_count,
        count(*) FILTER (WHERE is_repeat_within_cooldown)::integer
            AS repeat_within_cooldown_count,
        count(*) FILTER (WHERE reason = 'quality_exploration')::integer AS exploration_count,
        count(*) FILTER (
            WHERE (
                  algorithm_version = 'public_trending_keyset_v1'
                  OR reason LIKE '%fallback%'
              )
        )::integer AS fallback_count,
        count(DISTINCT meme_id)::integer AS unique_meme_count,
        count(*) FILTER (
            WHERE has_strong_action AND reason = 'quality_exploration'
        )::integer AS exploration_action_count
    FROM target_impressions
    GROUP BY metric_date, surface, algorithm_version, profile_version, candidate_source
),
impression_items AS (
    SELECT
        target_impressions.metric_date,
        target_impressions.surface,
        target_impressions.algorithm_version,
        target_impressions.profile_version,
        target_impressions.candidate_source,
        target_impressions.meme_id,
        feature.representative_source_channel_id,
        feature.template_id,
        feature.popularity_quantile,
        count(*)::double precision AS impression_count
    FROM target_impressions
    LEFT JOIN public_meme_recommendation_features_mv feature
      ON feature.meme_id = target_impressions.meme_id
    WHERE target_impressions.meme_id IS NOT NULL
    GROUP BY
        target_impressions.metric_date,
        target_impressions.surface,
        target_impressions.algorithm_version,
        target_impressions.profile_version,
        target_impressions.candidate_source,
        target_impressions.meme_id,
        feature.representative_source_channel_id,
        feature.template_id,
        feature.popularity_quantile
),
source_counts AS (
    SELECT
        metric_date, surface, algorithm_version, profile_version, candidate_source,
        representative_source_channel_id,
        sum(impression_count) AS item_count
    FROM impression_items
    WHERE representative_source_channel_id IS NOT NULL
    GROUP BY
        metric_date, surface, algorithm_version, profile_version, candidate_source,
        representative_source_channel_id
),
source_hhi AS (
    SELECT
        metric_date, surface, algorithm_version, profile_version, candidate_source,
        sum(item_count * item_count) / NULLIF(sum(item_count) * sum(item_count), 0.0) AS concentration
    FROM source_counts
    GROUP BY metric_date, surface, algorithm_version, profile_version, candidate_source
),
template_counts AS (
    SELECT
        metric_date, surface, algorithm_version, profile_version, candidate_source,
        template_id,
        sum(impression_count) AS item_count
    FROM impression_items
    WHERE template_id IS NOT NULL
    GROUP BY
        metric_date, surface, algorithm_version, profile_version, candidate_source, template_id
),
template_hhi AS (
    SELECT
        metric_date, surface, algorithm_version, profile_version, candidate_source,
        sum(item_count * item_count) / NULLIF(sum(item_count) * sum(item_count), 0.0) AS concentration
    FROM template_counts
    GROUP BY metric_date, surface, algorithm_version, profile_version, candidate_source
),
long_tail AS (
    SELECT
        metric_date, surface, algorithm_version, profile_version, candidate_source,
        count(DISTINCT meme_id) FILTER (WHERE popularity_quantile < 0.8)::integer AS meme_count
    FROM impression_items
    GROUP BY metric_date, surface, algorithm_version, profile_version, candidate_source
),
catalog AS (
    SELECT GREATEST(count(*), 1)::double precision AS meme_count
    FROM public_meme_recommendation_features_mv
)
SELECT
    counts.metric_date,
    counts.surface,
    counts.algorithm_version,
    counts.profile_version,
    counts.candidate_source,
    counts.impression_count,
    counts.strong_action_count,
    counts.attributed_send_count,
    counts.result_count,
    counts.exploration_count,
    counts.fallback_count,
    jsonb_build_object(
        'strong_action_rate',
            counts.strong_action_count::double precision / NULLIF(counts.impression_count, 0),
        'attributed_send_rate',
            counts.attributed_send_count::double precision / NULLIF(counts.impression_count, 0),
        'repeat_within_cooldown_count', counts.repeat_within_cooldown_count,
        'repeat_within_cooldown_rate',
            counts.repeat_within_cooldown_count::double precision
                / NULLIF(counts.impression_count, 0),
        'fallback_rate',
            counts.fallback_count::double precision / NULLIF(counts.impression_count, 0),
        'catalog_coverage', counts.unique_meme_count::double precision / catalog.meme_count,
        'long_tail_coverage', COALESCE(long_tail.meme_count, 0)::double precision / catalog.meme_count,
        'source_concentration', COALESCE(source_hhi.concentration, 0.0),
        'template_concentration', COALESCE(template_hhi.concentration, 0.0),
        'exploration_share',
            counts.exploration_count::double precision / NULLIF(counts.impression_count, 0),
        'exploration_conversion',
            counts.exploration_action_count::double precision / NULLIF(counts.exploration_count, 0),
        'unique_meme_count', counts.unique_meme_count
    ) AS metrics
FROM counts
CROSS JOIN catalog
LEFT JOIN source_hhi USING (
    metric_date, surface, algorithm_version, profile_version, candidate_source
)
LEFT JOIN template_hhi USING (
    metric_date, surface, algorithm_version, profile_version, candidate_source
)
LEFT JOIN long_tail USING (
    metric_date, surface, algorithm_version, profile_version, candidate_source
)
ORDER BY
    counts.metric_date,
    counts.surface,
    counts.algorithm_version,
    counts.profile_version,
    counts.candidate_source
"""


__all__ = [
    "RecommendationAnalyticsRollupResult",
    "load_recommendation_daily_analytics",
    "rollup_recommendation_daily_analytics",
]
