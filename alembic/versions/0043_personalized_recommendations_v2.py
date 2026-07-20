"""personalized recommendation state, profiles, and item features

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-20
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_PREVIOUS_ANALYTICS_EVENT_TYPES = (
    "account_merge",
    "auth_event",
    "channel_suggest",
    "click",
    "collection_action",
    "favorite",
    "impression",
    "inline_chosen",
    "inline_query",
    "inline_sent",
    "inline_served",
    "meme_detail_click",
    "meme_download",
    "meme_impression",
    "meme_like",
    "meme_pin",
    "meme_report",
    "meme_save",
    "meme_send",
    "meme_share",
    "meme_view",
    "miniapp_open",
    "page_view",
    "save",
    "search_query",
    "share",
    "view",
)
_ANALYTICS_EVENT_TYPES = (*_PREVIOUS_ANALYTICS_EVENT_TYPES, "meme_engaged_view")


def upgrade() -> None:
    """Add exact cooldown state, bounded profiles, and recommendation features."""

    _replace_analytics_event_type_constraint(_ANALYTICS_EVENT_TYPES)
    _create_recommendation_tables()
    _backfill_recommendation_state()
    op.execute(_PUBLIC_MEME_RECOMMENDATION_FEATURES_MV_SQL)
    op.execute(
        "CREATE UNIQUE INDEX uq_public_meme_recommendation_features_mv_meme_id "
        "ON public_meme_recommendation_features_mv (meme_id)"
    )
    op.execute(
        "CREATE INDEX ix_public_meme_recommendation_features_mv_exploration "
        "ON public_meme_recommendation_features_mv "
        "(latest_published_at DESC, source_quality_quantile DESC, technical_quality DESC, meme_id)"
    )
    op.execute(
        "CREATE INDEX ix_public_meme_recommendation_features_mv_representative_source "
        "ON public_meme_recommendation_features_mv (representative_source_channel_id)"
    )


def downgrade() -> None:
    """Remove v2 projections without deleting indefinitely retained raw events."""

    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_meme_recommendation_features_mv")
    op.drop_table("recommendation_daily_aggregates")
    op.drop_table("user_recommendation_profile_signals")
    op.drop_table("user_recommendation_profiles")
    op.drop_table("user_recommendation_profile_status")
    op.drop_table("user_meme_recommendation_state")
    # Keep the additive event value valid so a schema rollback never destroys
    # raw engaged-view history. Older application code can ignore the row.
    _replace_analytics_event_type_constraint(_ANALYTICS_EVENT_TYPES)


def _create_recommendation_tables() -> None:
    op.create_table(
        "user_meme_recommendation_state",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("meme_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_impression_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_engaged_view_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_strong_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("impression_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "impression_count >= 0",
            name="ck_user_meme_recommendation_state_recommendation_state_impression_count_non_negative",
        ),
        sa.ForeignKeyConstraint(["meme_id"], ["memes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "meme_id", name="pk_user_meme_recommendation_state"),
    )
    op.create_index(
        "ix_user_meme_recommendation_state_user_impression",
        "user_meme_recommendation_state",
        ["user_id", "latest_impression_at"],
    )
    op.create_index(
        "ix_user_meme_recommendation_state_user_strong_action",
        "user_meme_recommendation_state",
        ["user_id", "latest_strong_action_at"],
    )

    op.create_table(
        "user_recommendation_profile_status",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("dirty_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rebuilt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_recommendation_profile_status"),
    )

    op.create_table(
        "user_recommendation_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("profile_slot", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(length=255), nullable=False),
        sa.Column("profile_version", sa.String(length=64), nullable=False),
        sa.Column("signal_count", sa.Integer(), nullable=False),
        sa.Column("total_weight", sa.Float(), nullable=False),
        sa.Column("event_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vector", postgresql.BYTEA(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "model_version <> ''",
            name="ck_user_recommendation_profiles_recommendation_profile_model_version_not_blank",
        ),
        sa.CheckConstraint(
            "profile_version <> ''",
            name="ck_user_recommendation_profiles_recommendation_profile_version_not_blank",
        ),
        sa.CheckConstraint(
            "profile_slot >= 0 AND profile_slot <= 4",
            name="ck_user_recommendation_profiles_recommendation_profile_slot_range",
        ),
        sa.CheckConstraint(
            "signal_count >= 0",
            name="ck_user_recommendation_profiles_recommendation_profile_signal_count_non_negative",
        ),
        sa.CheckConstraint(
            "total_weight >= 0",
            name="ck_user_recommendation_profiles_recommendation_profile_weight_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_user_recommendation_profiles"),
        sa.UniqueConstraint(
            "user_id",
            "profile_slot",
            name="uq_user_recommendation_profiles_user_slot",
        ),
    )
    op.create_index(
        "ix_user_recommendation_profiles_user_generated",
        "user_recommendation_profiles",
        ["user_id", "generated_at"],
    )

    op.create_table(
        "user_recommendation_profile_signals",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("meme_id", sa.Uuid(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_strong_positive", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "weight > 0",
            name="ck_user_recommendation_profile_signals_recommendation_profile_signal_weight_positive",
        ),
        sa.ForeignKeyConstraint(["meme_id"], ["memes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "meme_id", name="pk_user_recommendation_profile_signals"),
    )
    op.create_index(
        "ix_user_recommendation_profile_signals_user_weight",
        "user_recommendation_profile_signals",
        ["user_id", "weight"],
    )

    op.create_table(
        "recommendation_daily_aggregates",
        sa.Column("metric_date", sa.Date(), server_default=sa.func.current_date(), nullable=False),
        sa.Column("surface", sa.String(length=120), nullable=False),
        sa.Column("algorithm_version", sa.String(length=120), nullable=False),
        sa.Column("profile_version", sa.String(length=120), server_default="none", nullable=False),
        sa.Column("candidate_source", sa.String(length=120), server_default="unknown", nullable=False),
        sa.Column("impression_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("strong_action_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attributed_send_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exploration_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_expiry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fallback_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "impression_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_impressions_non_negative",
        ),
        sa.CheckConstraint(
            "strong_action_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_actions_non_negative",
        ),
        sa.CheckConstraint(
            "attributed_send_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_sends_non_negative",
        ),
        sa.CheckConstraint(
            "result_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_results_non_negative",
        ),
        sa.CheckConstraint(
            "exploration_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_exploration_non_negative",
        ),
        sa.CheckConstraint(
            "cache_expiry_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_cache_expiry_non_negative",
        ),
        sa.CheckConstraint(
            "fallback_count >= 0",
            name="ck_recommendation_daily_aggregates_recommendation_daily_fallback_non_negative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recommendation_daily_aggregates"),
        sa.UniqueConstraint(
            "metric_date",
            "surface",
            "algorithm_version",
            "profile_version",
            "candidate_source",
            name="uq_recommendation_daily_aggregate_dimensions",
        ),
    )
    op.create_index(
        "ix_recommendation_daily_aggregates_date_surface",
        "recommendation_daily_aggregates",
        ["metric_date", "surface"],
    )


def _backfill_recommendation_state() -> None:
    # Exact state is intentionally independent of bounded candidate reads. It
    # can therefore suppress the 81st, 800th, or 8,000th recent impression.
    op.execute(
        sa.text(
            """
            WITH normalized AS (
                SELECT
                    ae.user_id,
                    CASE
                        WHEN jsonb_typeof(ae.payload -> 'refs' -> 'meme_id') = 'string'
                         AND ae.payload -> 'refs' ->> 'meme_id'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN (ae.payload -> 'refs' ->> 'meme_id')::uuid
                        WHEN jsonb_typeof(ae.payload -> 'meme_id') = 'string'
                         AND ae.payload ->> 'meme_id'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN (ae.payload ->> 'meme_id')::uuid
                        ELSE NULL
                    END AS meme_id,
                    ae.event_type::text AS event_type,
                    lower(COALESCE(ae.payload -> 'properties' ->> 'action', '')) AS action,
                    ae.occurred_at
                FROM analytics_events ae
                WHERE ae.user_id IS NOT NULL
            ),
            aggregated AS (
                SELECT
                    user_id,
                    meme_id,
                    min(occurred_at) AS first_seen_at,
                    max(occurred_at) FILTER (
                        WHERE event_type IN ('impression', 'meme_impression', 'inline_served')
                    ) AS latest_impression_at,
                    max(occurred_at) FILTER (
                        WHERE event_type = 'meme_engaged_view'
                    ) AS latest_engaged_view_at,
                    max(occurred_at) FILTER (
                        WHERE event_type IN (
                            'favorite', 'meme_like', 'meme_save', 'save', 'meme_pin',
                            'meme_download', 'meme_send', 'meme_share', 'share',
                            'inline_chosen', 'inline_sent'
                        )
                        AND action NOT IN (
                            'remove', 'delete', 'unfavorite', 'unlike', 'unsave',
                            'unpin', 'remove_save', 'reorder', 'reorder_pin'
                        )
                    ) AS latest_strong_action_at,
                    count(*) FILTER (
                        WHERE event_type IN ('impression', 'meme_impression', 'inline_served')
                    )::integer AS impression_count
                FROM normalized
                WHERE meme_id IS NOT NULL
                  AND EXISTS (SELECT 1 FROM memes WHERE memes.id = normalized.meme_id)
                GROUP BY user_id, meme_id
            )
            INSERT INTO user_meme_recommendation_state (
                user_id, meme_id, first_seen_at, latest_impression_at,
                latest_engaged_view_at, latest_strong_action_at, impression_count,
                created_at, updated_at
            )
            SELECT
                user_id, meme_id, first_seen_at, latest_impression_at,
                latest_engaged_view_at, latest_strong_action_at, impression_count,
                now(), now()
            FROM aggregated
            ON CONFLICT (user_id, meme_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH durable AS (
                SELECT
                    COALESCE(cm.added_by_user_id, c.owner_id) AS user_id,
                    cm.meme_id,
                    cm.added_at AS signal_at
                FROM collection_memes cm
                JOIN collections c ON c.id = cm.collection_id
                UNION ALL
                SELECT pm.user_id, pm.meme_id, pm.pinned_at
                FROM pinned_memes pm
            ),
            aggregated AS (
                SELECT user_id, meme_id, min(signal_at) AS first_seen_at, max(signal_at) AS strong_at
                FROM durable
                WHERE user_id IS NOT NULL
                GROUP BY user_id, meme_id
            )
            INSERT INTO user_meme_recommendation_state (
                user_id, meme_id, first_seen_at, latest_impression_at,
                latest_engaged_view_at, latest_strong_action_at, impression_count,
                created_at, updated_at
            )
            SELECT
                user_id, meme_id, first_seen_at, NULL, NULL, strong_at, 0, now(), now()
            FROM aggregated
            ON CONFLICT (user_id, meme_id) DO UPDATE SET
                first_seen_at = LEAST(
                    user_meme_recommendation_state.first_seen_at,
                    EXCLUDED.first_seen_at
                ),
                latest_strong_action_at = GREATEST(
                    user_meme_recommendation_state.latest_strong_action_at,
                    EXCLUDED.latest_strong_action_at
                ),
                updated_at = now()
            """
        )
    )
    op.execute(
        """
        INSERT INTO user_recommendation_profile_status (
            user_id, dirty_since, last_rebuilt_at, event_watermark, created_at, updated_at
        )
        SELECT user_id, now(), NULL, NULL, now(), now()
        FROM user_meme_recommendation_state
        GROUP BY user_id
        ON CONFLICT (user_id) DO NOTHING
        """
    )


def _replace_analytics_event_type_constraint(event_types: tuple[str, ...]) -> None:
    joined_values = ", ".join(f"'{event_type}'" for event_type in event_types)
    op.execute("ALTER TABLE analytics_events DROP CONSTRAINT IF EXISTS analyticseventtype")
    op.execute("ALTER TABLE analytics_events DROP CONSTRAINT IF EXISTS ck_analytics_events_analyticseventtype")
    op.execute(
        f"ALTER TABLE analytics_events ADD CONSTRAINT analyticseventtype CHECK (event_type IN ({joined_values}))"
    )


_PUBLIC_MEME_RECOMMENDATION_FEATURES_MV_SQL = """
CREATE MATERIALIZED VIEW public_meme_recommendation_features_mv AS
WITH live_sources AS (
    SELECT
        ms.id AS meme_source_id,
        mf.meme_id,
        sc.id AS source_channel_id,
        COALESCE(sc.id::text, ms.platform::text || ':' || ms.source_id) AS source_cohort_key,
        ms.published_at,
        snapshot.id AS snapshot_id,
        snapshot.view_count::double precision AS views,
        snapshot.reaction_count::double precision AS reactions,
        snapshot.forward_count::double precision AS forwards,
        snapshot.comment_count::double precision AS comments,
        CASE
            WHEN ms.published_at >= now() - interval '1 day' THEN '0-1d'
            WHEN ms.published_at >= now() - interval '7 days' THEN '2-7d'
            WHEN ms.published_at >= now() - interval '30 days' THEN '8-30d'
            WHEN ms.published_at >= now() - interval '180 days' THEN '31-180d'
            ELSE 'older'
        END AS age_cohort
    FROM meme_sources ms
    JOIN meme_files mf ON mf.id = ms.file_id
    LEFT JOIN source_channels sc
      ON sc.platform = ms.platform
     AND sc.platform_id = ms.source_id
    LEFT JOIN LATERAL (
        SELECT
            ses.id,
            ses.view_count,
            ses.reaction_count,
            ses.forward_count,
            ses.comment_count
        FROM meme_source_engagement_snapshots ses
        WHERE ses.meme_source_id = ms.id
          AND ses.fetch_status = 'success'
          AND ses.source_alive IS TRUE
        ORDER BY ses.captured_at DESC, ses.id DESC
        LIMIT 1
    ) snapshot ON TRUE
    WHERE ms.source_alive IS TRUE
),
source_provenance AS (
    SELECT
        meme_id,
        max(published_at) AS latest_published_at,
        array_agg(DISTINCT source_channel_id ORDER BY source_channel_id)
            FILTER (WHERE source_channel_id IS NOT NULL) AS source_channel_ids,
        (array_agg(source_channel_id ORDER BY source_channel_id)
            FILTER (WHERE source_channel_id IS NOT NULL))[1]
            AS fallback_representative_source_channel_id,
        count(*)::integer AS live_source_count
    FROM live_sources
    GROUP BY meme_id
),
source_base AS (
    SELECT
        source.*,
        CASE
            WHEN source.views > 0
            THEN (
                COALESCE(source.reactions, 0.0)
                + 3.0 * COALESCE(source.forwards, 0.0)
                + 0.5 * COALESCE(source.comments, 0.0)
            ) / source.views
            ELSE 0.0
        END AS raw_quality_rate
    FROM live_sources source
    WHERE source.snapshot_id IS NOT NULL
      AND source.views IS NOT NULL
),
cohort_priors AS (
    SELECT
        source_cohort_key,
        age_cohort,
        avg(raw_quality_rate)::double precision AS mean_quality_rate
    FROM source_base
    GROUP BY source_cohort_key, age_cohort
),
source_smoothed AS (
    SELECT
        source.*,
        (
            COALESCE(source.reactions, 0.0)
            + 3.0 * COALESCE(source.forwards, 0.0)
            + 0.5 * COALESCE(source.comments, 0.0)
            + 100.0 * prior.mean_quality_rate
        ) / (source.views + 100.0) AS smoothed_quality_rate
    FROM source_base source
    JOIN cohort_priors prior USING (source_cohort_key, age_cohort)
),
source_ranked AS (
    SELECT
        source.*,
        percent_rank() OVER (
            PARTITION BY source.source_cohort_key, source.age_cohort
            ORDER BY ln(1.0 + source.views)
        )::double precision AS popularity_quantile,
        percent_rank() OVER (
            PARTITION BY source.source_cohort_key, source.age_cohort
            ORDER BY source.smoothed_quality_rate
        )::double precision AS quality_quantile
    FROM source_smoothed source
),
source_per_meme AS (
    SELECT
        meme_id,
        (array_agg(
            source_channel_id
            ORDER BY quality_quantile DESC, popularity_quantile DESC, source_channel_id
        ) FILTER (WHERE source_channel_id IS NOT NULL))[1]
            AS measured_representative_source_channel_id,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY popularity_quantile)::double precision
            AS source_popularity_quantile,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY quality_quantile)::double precision
            AS source_quality_quantile
    FROM source_ranked
    GROUP BY meme_id
),
technical_quality_coverage AS (
    SELECT journal.meme_file_id
    FROM pipeline_stage_journal journal
    WHERE journal.stage = 'transcode'
      AND journal.status = 'succeeded'
    UNION
    SELECT attempt.meme_file_id
    FROM pipeline_stage_attempts attempt
    WHERE attempt.stage = 'transcode'
      AND attempt.outcome = 'succeeded'
),
exposure_per_meme AS (
    SELECT
        meme_id,
        count(*) FILTER (WHERE exposed_at IS NOT NULL)::integer AS exposure_count,
        count(*) FILTER (
            WHERE exposed_at IS NOT NULL
              AND (high_intent_action_at IS NOT NULL OR inline_sent_at IS NOT NULL)
        )::integer AS strong_action_count
    FROM meme_exposures
    GROUP BY meme_id
),
surface_response AS (
    SELECT
        COALESCE(
            sum(strong_action_count)::double precision / NULLIF(sum(exposure_count), 0),
            0.5
        ) AS mean_response
    FROM exposure_per_meme
),
trend_ranked AS (
    SELECT
        trend.meme_id,
        percent_rank() OVER (
            ORDER BY trend.latest_popularity_score
        )::double precision AS popularity_quantile,
        percent_rank() OVER (
            ORDER BY trend.trending_score
        )::double precision AS trend_quantile
    FROM public_meme_trends_mv trend
)
SELECT
    meme.id AS meme_id,
    COALESCE(provenance.latest_published_at, meme.created_at) AS latest_published_at,
    COALESCE(provenance.source_channel_ids, ARRAY[]::uuid[]) AS source_channel_ids,
    COALESCE(
        source.measured_representative_source_channel_id,
        provenance.fallback_representative_source_channel_id
    ) AS representative_source_channel_id,
    COALESCE(source.source_popularity_quantile, 0.5)::double precision AS source_popularity_quantile,
    COALESCE(source.source_quality_quantile, 0.5)::double precision AS source_quality_quantile,
    CASE
        WHEN technical.meme_file_id IS NOT NULL
        THEN LEAST(GREATEST(primary_file.quality_score, 0.0), 1.0)::double precision
        ELSE 0.5::double precision
    END AS technical_quality,
    (
        COALESCE(exposure.strong_action_count, 0)
        + 20.0 * response.mean_response
    ) / (COALESCE(exposure.exposure_count, 0) + 20.0) AS platform_response,
    COALESCE(trend.popularity_quantile, 0.5)::double precision AS popularity_quantile,
    COALESCE(trend.trend_quantile, 0.5)::double precision AS trend_quantile,
    meme.template_id,
    COALESCE(provenance.live_source_count, 0)::integer AS live_source_count,
    COALESCE(exposure.exposure_count, 0)::integer AS exposure_count,
    jsonb_build_object(
        'provenance', provenance.latest_published_at IS NOT NULL,
        'source_popularity', source.source_popularity_quantile IS NOT NULL,
        'source_quality', source.source_quality_quantile IS NOT NULL,
        'technical_quality', technical.meme_file_id IS NOT NULL,
        'platform_response', COALESCE(exposure.exposure_count, 0) > 0,
        'popularity', trend.meme_id IS NOT NULL
    ) AS coverage_flags,
    now() AS refreshed_at
FROM memes meme
JOIN meme_files primary_file ON primary_file.id = meme.primary_file_id
LEFT JOIN source_provenance provenance ON provenance.meme_id = meme.id
LEFT JOIN source_per_meme source ON source.meme_id = meme.id
LEFT JOIN technical_quality_coverage technical ON technical.meme_file_id = primary_file.id
LEFT JOIN exposure_per_meme exposure ON exposure.meme_id = meme.id
CROSS JOIN surface_response response
LEFT JOIN trend_ranked trend ON trend.meme_id = meme.id
WHERE meme.is_public IS TRUE
"""
