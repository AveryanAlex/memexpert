# ruff: noqa: E501,I001
"""source engagement public read models"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this destructive prelaunch rewrite."""

    _drop_public_trend_materialized_views()
    _create_public_trend_materialized_views()
    op.execute("DROP INDEX IF EXISTS ix_memes_visibility_popularity_created_at")
    op.execute("DROP TABLE IF EXISTS meme_popularity_snapshots")
    op.drop_column("memes", "popularity_score")
    op.drop_column("meme_sources", "views")
    op.drop_column("meme_sources", "reactions")
    op.create_index("ix_memes_visibility_created_at", "memes", ["is_public", "created_at"], unique=False)


def downgrade() -> None:
    """Recreate removed legacy storage without backfilling destructive data."""

    op.drop_index("ix_memes_visibility_created_at", table_name="memes")
    op.add_column("meme_sources", sa.Column("reactions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("meme_sources", sa.Column("views", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("memes", sa.Column("popularity_score", sa.Float(), server_default=sa.text("0.0"), nullable=False))
    op.create_index("ix_memes_visibility_popularity_created_at", "memes", ["is_public", "popularity_score", "created_at"], unique=False)
    op.create_table(
        "meme_popularity_snapshots",
        sa.Column("meme_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_views", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_reactions", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_reposts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("platform_views", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("platform_sends", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("platform_saves", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("platform_likes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("popularity_score", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meme_popularity_snapshots")),
        sa.ForeignKeyConstraint(["meme_id"], ["memes.id"], name=op.f("fk_meme_popularity_snapshots_meme_id_memes"), ondelete="CASCADE"),
        sa.UniqueConstraint("meme_id", "captured_at", name="uq_meme_popularity_snapshots_meme_id_captured_at"),
    )
    op.create_index("ix_meme_popularity_snapshots_meme_id_captured_at", "meme_popularity_snapshots", ["meme_id", "captured_at"], unique=False)
    _drop_public_trend_materialized_views()
    # Older revisions own the legacy materialized-view definitions and rebuild
    # them as their downgrades run. Leaving source-engagement-backed views here
    # would block 0022 from dropping meme_source_engagement_snapshots.


def _drop_public_trend_materialized_views() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_template_trend_points_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_tag_trend_points_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_template_trends_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_tag_trends_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_meme_trends_mv")


def _create_public_trend_materialized_views() -> None:
    op.execute(_PUBLIC_MEME_TRENDS_MV_SQL)
    op.execute("CREATE UNIQUE INDEX uq_public_meme_trends_mv_meme_id ON public_meme_trends_mv (meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_trending ON public_meme_trends_mv (trending_score DESC, engagement_24h DESC, meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_fastest_rising ON public_meme_trends_mv (((recent_view_count + recent_send_count * 3 + recent_like_count * 5 + recent_save_count * 4 + recent_download_count * 2) - (previous_view_count + previous_send_count * 3 + previous_like_count * 5 + previous_save_count * 4 + previous_download_count * 2)) DESC, trending_score DESC, meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_most_liked ON public_meme_trends_mv (recent_like_count DESC, latest_platform_likes DESC, trending_score DESC, meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_template_id ON public_meme_trends_mv (template_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_tags ON public_meme_trends_mv USING gin (tags)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_safe_filters ON public_meme_trends_mv (is_nsfw, media_type, language)")

    op.execute(_PUBLIC_TAG_TRENDS_MV_SQL)
    op.execute("CREATE UNIQUE INDEX uq_public_tag_trends_mv_tag ON public_tag_trends_mv (tag)")
    op.execute("CREATE INDEX ix_public_tag_trends_mv_trending ON public_tag_trends_mv (trending_score DESC, engagement_24h DESC, tag)")

    op.execute(_PUBLIC_TEMPLATE_TRENDS_MV_SQL)
    op.execute("CREATE UNIQUE INDEX uq_public_template_trends_mv_template_id ON public_template_trends_mv (template_id)")
    op.execute("CREATE UNIQUE INDEX uq_public_template_trends_mv_template_slug ON public_template_trends_mv (template_slug)")
    op.execute("CREATE INDEX ix_public_template_trends_mv_trending ON public_template_trends_mv (trending_score DESC, engagement_24h DESC, template_slug)")

    op.execute(_PUBLIC_TAG_TREND_POINTS_MV_SQL)
    op.execute("CREATE UNIQUE INDEX uq_public_tag_trend_points_mv_tag_observed_at ON public_tag_trend_points_mv (tag, observed_at)")
    op.execute("CREATE INDEX ix_public_tag_trend_points_mv_observed_at ON public_tag_trend_points_mv (observed_at DESC, tag)")
    op.execute("CREATE INDEX ix_public_tag_trend_points_mv_value ON public_tag_trend_points_mv (aggregate_value DESC, observed_at DESC, tag)")

    op.execute(_PUBLIC_TEMPLATE_TREND_POINTS_MV_SQL)
    op.execute("CREATE UNIQUE INDEX uq_public_template_trend_points_mv_template_observed_at ON public_template_trend_points_mv (template_id, observed_at)")
    op.execute("CREATE INDEX ix_public_template_trend_points_mv_slug_observed_at ON public_template_trend_points_mv (template_slug, observed_at)")
    op.execute("CREATE INDEX ix_public_template_trend_points_mv_observed_at ON public_template_trend_points_mv (observed_at DESC, template_slug)")
    op.execute("CREATE INDEX ix_public_template_trend_points_mv_value ON public_template_trend_points_mv (aggregate_value DESC, observed_at DESC, template_slug)")


_SOURCE_SCORE_EXPR = """
    ln(1.0 + GREATEST(COALESCE(latest_source_views, 0), 0)) * 1.0
    + ln(1.0 + GREATEST(COALESCE(latest_source_reactions, 0), 0)) * 2.0
    + ln(1.0 + GREATEST(COALESCE(latest_source_reposts, 0), 0)) * 3.0
    + ln(1.0 + GREATEST(COALESCE(latest_platform_views, 0), 0)) * 1.0
    + ln(1.0 + GREATEST(COALESCE(latest_platform_sends, 0), 0)) * 3.0
    + ln(1.0 + GREATEST(COALESCE(latest_platform_saves, 0), 0)) * 4.0
    + ln(1.0 + GREATEST(COALESCE(latest_platform_likes, 0), 0)) * 5.0
"""

_POINT_SCORE_EXPR = """
    ln(1.0 + GREATEST(COALESCE(source_views, 0), 0)) * 1.0
    + ln(1.0 + GREATEST(COALESCE(source_reactions, 0), 0)) * 2.0
    + ln(1.0 + GREATEST(COALESCE(source_reposts, 0), 0)) * 3.0
    + ln(1.0 + GREATEST(COALESCE(platform_views, 0), 0)) * 1.0
    + ln(1.0 + GREATEST(COALESCE(platform_sends, 0), 0)) * 3.0
    + ln(1.0 + GREATEST(COALESCE(platform_saves, 0), 0)) * 4.0
    + ln(1.0 + GREATEST(COALESCE(platform_likes, 0), 0)) * 5.0
"""

_PUBLIC_MEME_TRENDS_MV_SQL = f"""
CREATE MATERIALIZED VIEW public_meme_trends_mv AS
WITH safe_events AS (
    SELECT
        CASE
            WHEN jsonb_typeof(payload -> 'meme_id') = 'string'
            AND payload ->> 'meme_id' ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
            THEN (payload ->> 'meme_id')::uuid
            WHEN jsonb_typeof(payload -> 'refs' -> 'meme_id') = 'string'
            AND payload -> 'refs' ->> 'meme_id' ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
            THEN (payload -> 'refs' ->> 'meme_id')::uuid
            ELSE NULL
        END AS meme_id,
        event_type::text AS event_type,
        occurred_at
    FROM analytics_events
    WHERE event_type::text IN (
        'meme_view', 'view', 'meme_send', 'share', 'meme_share', 'inline_sent',
        'meme_like', 'favorite', 'meme_save', 'save', 'meme_download'
    )
),
event_counts AS (
    SELECT
        meme_id,
        count(*) FILTER (WHERE event_type IN ('meme_view', 'view') AND occurred_at >= now() - interval '7 days')::integer AS recent_platform_view_count,
        count(*) FILTER (WHERE event_type IN ('meme_view', 'view') AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_platform_view_count,
        count(*) FILTER (WHERE event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent') AND occurred_at >= now() - interval '7 days')::integer AS recent_platform_send_count,
        count(*) FILTER (WHERE event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent') AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_platform_send_count,
        count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite') AND occurred_at >= now() - interval '7 days')::integer AS recent_platform_like_count,
        count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite') AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_platform_like_count,
        count(*) FILTER (WHERE event_type IN ('meme_save', 'save') AND occurred_at >= now() - interval '7 days')::integer AS recent_platform_save_count,
        count(*) FILTER (WHERE event_type IN ('meme_save', 'save') AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_platform_save_count,
        count(*) FILTER (WHERE event_type = 'meme_download' AND occurred_at >= now() - interval '7 days')::integer AS recent_download_count,
        count(*) FILTER (WHERE event_type = 'meme_download' AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_download_count,
        count(*) FILTER (WHERE event_type IN ('meme_view', 'view') AND occurred_at >= now() - interval '24 hours')::integer AS platform_view_count_24h,
        count(*) FILTER (WHERE event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent') AND occurred_at >= now() - interval '24 hours')::integer AS platform_send_count_24h,
        count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite') AND occurred_at >= now() - interval '24 hours')::integer AS platform_like_count_24h,
        count(*) FILTER (WHERE event_type IN ('meme_save', 'save') AND occurred_at >= now() - interval '24 hours')::integer AS platform_save_count_24h,
        count(*) FILTER (WHERE event_type = 'meme_download' AND occurred_at >= now() - interval '24 hours')::integer AS download_count_24h,
        count(*) FILTER (WHERE event_type IN ('meme_view', 'view'))::integer AS latest_platform_views,
        count(*) FILTER (WHERE event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent'))::integer AS latest_platform_sends,
        count(*) FILTER (WHERE event_type IN ('meme_save', 'save'))::integer AS latest_platform_saves,
        count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite'))::integer AS latest_platform_likes
    FROM safe_events
    WHERE meme_id IS NOT NULL
    GROUP BY meme_id
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
        lag(ses.view_count) OVER (PARTITION BY ses.meme_source_id ORDER BY ses.captured_at, ses.id) AS previous_view_count,
        lag(ses.reaction_count) OVER (PARTITION BY ses.meme_source_id ORDER BY ses.captured_at, ses.id) AS previous_reaction_count,
        lag(ses.forward_count) OVER (PARTITION BY ses.meme_source_id ORDER BY ses.captured_at, ses.id) AS previous_forward_count
    FROM meme_source_engagement_snapshots ses
    JOIN meme_sources ms ON ms.id = ses.meme_source_id
    JOIN meme_files mf ON mf.id = ms.file_id
    WHERE ses.fetch_status::text = 'success'
),
source_deltas AS (
    SELECT
        meme_id,
        captured_at,
        CASE WHEN previous_view_count IS NULL OR view_count IS NULL THEN 0 ELSE GREATEST(view_count - previous_view_count, 0) END::integer AS view_delta,
        CASE WHEN previous_reaction_count IS NULL OR reaction_count IS NULL THEN 0 ELSE GREATEST(reaction_count - previous_reaction_count, 0) END::integer AS reaction_delta,
        CASE WHEN previous_forward_count IS NULL OR forward_count IS NULL THEN 0 ELSE GREATEST(forward_count - previous_forward_count, 0) END::integer AS forward_delta
    FROM successful_source_snapshots
),
source_delta_counts AS (
    SELECT
        meme_id,
        sum(view_delta) FILTER (WHERE captured_at >= now() - interval '7 days')::integer AS recent_source_view_count,
        sum(view_delta) FILTER (WHERE captured_at >= now() - interval '14 days' AND captured_at < now() - interval '7 days')::integer AS previous_source_view_count,
        sum(reaction_delta) FILTER (WHERE captured_at >= now() - interval '7 days')::integer AS recent_source_reaction_count,
        sum(reaction_delta) FILTER (WHERE captured_at >= now() - interval '14 days' AND captured_at < now() - interval '7 days')::integer AS previous_source_reaction_count,
        sum(forward_delta) FILTER (WHERE captured_at >= now() - interval '7 days')::integer AS recent_source_forward_count,
        sum(forward_delta) FILTER (WHERE captured_at >= now() - interval '14 days' AND captured_at < now() - interval '7 days')::integer AS previous_source_forward_count,
        sum(view_delta) FILTER (WHERE captured_at >= now() - interval '24 hours')::integer AS source_view_count_24h,
        sum(reaction_delta) FILTER (WHERE captured_at >= now() - interval '24 hours')::integer AS source_reaction_count_24h,
        sum(forward_delta) FILTER (WHERE captured_at >= now() - interval '24 hours')::integer AS source_forward_count_24h
    FROM source_deltas
    GROUP BY meme_id
),
latest_source_snapshots AS (
    SELECT DISTINCT ON (meme_source_id)
        meme_id,
        meme_source_id,
        captured_at,
        view_count,
        reaction_count,
        forward_count
    FROM successful_source_snapshots
    ORDER BY meme_source_id, captured_at DESC, id DESC
),
source_totals AS (
    SELECT
        meme_id,
        max(captured_at) AS latest_snapshot_at,
        sum(COALESCE(view_count, 0))::integer AS latest_source_views,
        sum(COALESCE(reaction_count, 0))::integer AS latest_source_reactions,
        sum(COALESCE(forward_count, 0))::integer AS latest_source_reposts
    FROM latest_source_snapshots
    GROUP BY meme_id
),
metric_inputs AS (
    SELECT
        m.id AS meme_id,
        m.template_id,
        m.tags,
        m.media_type::text AS media_type,
        m.language::text AS language,
        m.is_nsfw,
        COALESCE(ec.recent_platform_view_count, 0) + COALESCE(sdc.recent_source_view_count, 0) AS recent_view_count,
        COALESCE(ec.previous_platform_view_count, 0) + COALESCE(sdc.previous_source_view_count, 0) AS previous_view_count,
        COALESCE(ec.recent_platform_send_count, 0) + COALESCE(sdc.recent_source_forward_count, 0) AS recent_send_count,
        COALESCE(ec.previous_platform_send_count, 0) + COALESCE(sdc.previous_source_forward_count, 0) AS previous_send_count,
        COALESCE(ec.recent_platform_like_count, 0) + COALESCE(sdc.recent_source_reaction_count, 0) AS recent_like_count,
        COALESCE(ec.previous_platform_like_count, 0) + COALESCE(sdc.previous_source_reaction_count, 0) AS previous_like_count,
        COALESCE(ec.recent_platform_save_count, 0) AS recent_save_count,
        COALESCE(ec.previous_platform_save_count, 0) AS previous_save_count,
        COALESCE(ec.recent_download_count, 0) AS recent_download_count,
        COALESCE(ec.previous_download_count, 0) AS previous_download_count,
        st.latest_snapshot_at,
        COALESCE(st.latest_source_views, 0) AS latest_source_views,
        COALESCE(st.latest_source_reactions, 0) AS latest_source_reactions,
        COALESCE(st.latest_source_reposts, 0) AS latest_source_reposts,
        COALESCE(ec.latest_platform_views, 0) AS latest_platform_views,
        COALESCE(ec.latest_platform_sends, 0) AS latest_platform_sends,
        COALESCE(ec.latest_platform_saves, 0) AS latest_platform_saves,
        COALESCE(ec.latest_platform_likes, 0) AS latest_platform_likes,
        (
            COALESCE(ec.platform_view_count_24h, 0) + COALESCE(sdc.source_view_count_24h, 0)
            + (COALESCE(ec.platform_send_count_24h, 0) + COALESCE(sdc.source_forward_count_24h, 0)) * 3
            + (COALESCE(ec.platform_like_count_24h, 0) + COALESCE(sdc.source_reaction_count_24h, 0)) * 5
            + COALESCE(ec.platform_save_count_24h, 0) * 4
            + COALESCE(ec.download_count_24h, 0) * 2
        )::double precision AS engagement_24h,
        (
            COALESCE(ec.recent_platform_view_count, 0) + COALESCE(sdc.recent_source_view_count, 0)
            + (COALESCE(ec.recent_platform_send_count, 0) + COALESCE(sdc.recent_source_forward_count, 0)) * 3
            + (COALESCE(ec.recent_platform_like_count, 0) + COALESCE(sdc.recent_source_reaction_count, 0)) * 5
            + COALESCE(ec.recent_platform_save_count, 0) * 4
            + COALESCE(ec.recent_download_count, 0) * 2
        )::double precision AS recent_engagement,
        (
            COALESCE(ec.previous_platform_view_count, 0) + COALESCE(sdc.previous_source_view_count, 0)
            + (COALESCE(ec.previous_platform_send_count, 0) + COALESCE(sdc.previous_source_forward_count, 0)) * 3
            + (COALESCE(ec.previous_platform_like_count, 0) + COALESCE(sdc.previous_source_reaction_count, 0)) * 5
            + COALESCE(ec.previous_platform_save_count, 0) * 4
            + COALESCE(ec.previous_download_count, 0) * 2
        )::double precision AS previous_engagement
    FROM memes m
    LEFT JOIN event_counts ec ON ec.meme_id = m.id
    LEFT JOIN source_delta_counts sdc ON sdc.meme_id = m.id
    LEFT JOIN source_totals st ON st.meme_id = m.id
    WHERE m.is_public IS TRUE
),
scored AS (
    SELECT
        *,
        ({_SOURCE_SCORE_EXPR})::double precision AS latest_popularity_score
    FROM metric_inputs
)
SELECT
    meme_id,
    template_id,
    tags,
    media_type,
    language,
    is_nsfw,
    recent_view_count,
    previous_view_count,
    recent_send_count,
    previous_send_count,
    recent_like_count,
    previous_like_count,
    recent_save_count,
    previous_save_count,
    recent_download_count,
    previous_download_count,
    latest_snapshot_at,
    latest_source_views,
    latest_source_reactions,
    latest_source_reposts,
    latest_platform_views,
    latest_platform_sends,
    latest_platform_saves,
    latest_platform_likes,
    latest_popularity_score,
    engagement_24h,
    (
        recent_engagement
        + engagement_24h * 0.5
        + GREATEST(recent_engagement - previous_engagement, 0) * 0.75
        + latest_popularity_score * 0.25
    )::double precision AS trending_score,
    now() AS refreshed_at
FROM scored
"""

_PUBLIC_TAG_TRENDS_MV_SQL = """
CREATE MATERIALIZED VIEW public_tag_trends_mv AS
SELECT
    tag AS tag,
    count(*)::integer AS meme_count,
    sum(recent_view_count)::integer AS recent_view_count,
    sum(previous_view_count)::integer AS previous_view_count,
    sum(recent_send_count)::integer AS recent_send_count,
    sum(previous_send_count)::integer AS previous_send_count,
    sum(recent_like_count)::integer AS recent_like_count,
    sum(previous_like_count)::integer AS previous_like_count,
    sum(recent_save_count)::integer AS recent_save_count,
    sum(previous_save_count)::integer AS previous_save_count,
    sum(recent_download_count)::integer AS recent_download_count,
    sum(previous_download_count)::integer AS previous_download_count,
    sum(latest_source_views)::integer AS latest_source_views,
    sum(latest_source_reactions)::integer AS latest_source_reactions,
    sum(latest_source_reposts)::integer AS latest_source_reposts,
    sum(latest_platform_views)::integer AS latest_platform_views,
    sum(latest_platform_sends)::integer AS latest_platform_sends,
    sum(latest_platform_saves)::integer AS latest_platform_saves,
    sum(latest_platform_likes)::integer AS latest_platform_likes,
    max(latest_popularity_score)::double precision AS latest_popularity_score,
    max(latest_snapshot_at) AS latest_snapshot_at,
    sum(engagement_24h)::double precision AS engagement_24h,
    sum(trending_score)::double precision AS trending_score,
    max(refreshed_at) AS refreshed_at
FROM public_meme_trends_mv mt
CROSS JOIN LATERAL unnest(mt.tags) AS tag
WHERE mt.is_nsfw IS FALSE
GROUP BY tag
"""

_PUBLIC_TEMPLATE_TRENDS_MV_SQL = """
CREATE MATERIALIZED VIEW public_template_trends_mv AS
SELECT
    mt.template_id,
    t.slug AS template_slug,
    t.name AS template_name,
    t.description AS template_description,
    count(*)::integer AS meme_count,
    sum(mt.recent_view_count)::integer AS recent_view_count,
    sum(mt.previous_view_count)::integer AS previous_view_count,
    sum(mt.recent_send_count)::integer AS recent_send_count,
    sum(mt.previous_send_count)::integer AS previous_send_count,
    sum(mt.recent_like_count)::integer AS recent_like_count,
    sum(mt.previous_like_count)::integer AS previous_like_count,
    sum(mt.recent_save_count)::integer AS recent_save_count,
    sum(mt.previous_save_count)::integer AS previous_save_count,
    sum(mt.recent_download_count)::integer AS recent_download_count,
    sum(mt.previous_download_count)::integer AS previous_download_count,
    sum(mt.latest_source_views)::integer AS latest_source_views,
    sum(mt.latest_source_reactions)::integer AS latest_source_reactions,
    sum(mt.latest_source_reposts)::integer AS latest_source_reposts,
    sum(mt.latest_platform_views)::integer AS latest_platform_views,
    sum(mt.latest_platform_sends)::integer AS latest_platform_sends,
    sum(mt.latest_platform_saves)::integer AS latest_platform_saves,
    sum(mt.latest_platform_likes)::integer AS latest_platform_likes,
    max(mt.latest_popularity_score)::double precision AS latest_popularity_score,
    max(mt.latest_snapshot_at) AS latest_snapshot_at,
    sum(mt.engagement_24h)::double precision AS engagement_24h,
    sum(mt.trending_score)::double precision AS trending_score,
    max(mt.refreshed_at) AS refreshed_at
FROM public_meme_trends_mv mt
JOIN meme_templates t ON t.id = mt.template_id
WHERE mt.template_id IS NOT NULL AND mt.is_nsfw IS FALSE
GROUP BY mt.template_id, t.slug, t.name, t.description
"""

_DAILY_POINT_CTES = """
WITH safe_events AS (
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
    WHERE event_type::text IN ('meme_view', 'view', 'meme_send', 'share', 'meme_share', 'inline_sent', 'meme_like', 'favorite', 'meme_save', 'save')
),
event_daily AS (
    SELECT
        meme_id,
        date_trunc('day', occurred_at) AS observed_at,
        count(*) FILTER (WHERE event_type IN ('meme_view', 'view'))::integer AS platform_views,
        count(*) FILTER (WHERE event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent'))::integer AS platform_sends,
        count(*) FILTER (WHERE event_type IN ('meme_save', 'save'))::integer AS platform_saves,
        count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite'))::integer AS platform_likes
    FROM safe_events
    WHERE meme_id IS NOT NULL
    GROUP BY meme_id, date_trunc('day', occurred_at)
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
        lag(ses.view_count) OVER (PARTITION BY ses.meme_source_id ORDER BY ses.captured_at, ses.id) AS previous_view_count,
        lag(ses.reaction_count) OVER (PARTITION BY ses.meme_source_id ORDER BY ses.captured_at, ses.id) AS previous_reaction_count,
        lag(ses.forward_count) OVER (PARTITION BY ses.meme_source_id ORDER BY ses.captured_at, ses.id) AS previous_forward_count
    FROM meme_source_engagement_snapshots ses
    JOIN meme_sources ms ON ms.id = ses.meme_source_id
    JOIN meme_files mf ON mf.id = ms.file_id
    WHERE ses.fetch_status::text = 'success'
),
source_daily AS (
    SELECT
        meme_id,
        date_trunc('day', captured_at) AS observed_at,
        count(*)::integer AS snapshot_count,
        sum(CASE WHEN previous_view_count IS NULL OR view_count IS NULL THEN 0 ELSE GREATEST(view_count - previous_view_count, 0) END)::integer AS source_views,
        sum(CASE WHEN previous_reaction_count IS NULL OR reaction_count IS NULL THEN 0 ELSE GREATEST(reaction_count - previous_reaction_count, 0) END)::integer AS source_reactions,
        sum(CASE WHEN previous_forward_count IS NULL OR forward_count IS NULL THEN 0 ELSE GREATEST(forward_count - previous_forward_count, 0) END)::integer AS source_reposts
    FROM successful_source_snapshots
    GROUP BY meme_id, date_trunc('day', captured_at)
),
meme_daily AS (
    SELECT
        COALESCE(sd.meme_id, ed.meme_id) AS meme_id,
        COALESCE(sd.observed_at, ed.observed_at) AS observed_at,
        COALESCE(sd.snapshot_count, 0)::integer AS snapshot_count,
        COALESCE(sd.source_views, 0)::integer AS source_views,
        COALESCE(sd.source_reactions, 0)::integer AS source_reactions,
        COALESCE(sd.source_reposts, 0)::integer AS source_reposts,
        COALESCE(ed.platform_views, 0)::integer AS platform_views,
        COALESCE(ed.platform_sends, 0)::integer AS platform_sends,
        COALESCE(ed.platform_saves, 0)::integer AS platform_saves,
        COALESCE(ed.platform_likes, 0)::integer AS platform_likes
    FROM source_daily sd
    FULL OUTER JOIN event_daily ed ON ed.meme_id = sd.meme_id AND ed.observed_at = sd.observed_at
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

_PUBLIC_TAG_TREND_POINTS_MV_SQL = f"""
CREATE MATERIALIZED VIEW public_tag_trend_points_mv AS
{_DAILY_POINT_CTES}
SELECT
    normalized_tags.tag,
    d.observed_at,
    count(DISTINCT d.meme_id)::integer AS meme_count,
    sum(d.snapshot_count)::integer AS snapshot_count,
    sum(d.source_views)::integer AS source_views,
    sum(d.source_reactions)::integer AS source_reactions,
    sum(d.source_reposts)::integer AS source_reposts,
    sum(d.platform_views)::integer AS platform_views,
    sum(d.platform_sends)::integer AS platform_sends,
    sum(d.platform_saves)::integer AS platform_saves,
    sum(d.platform_likes)::integer AS platform_likes,
    sum(d.popularity_score)::double precision AS aggregate_value
FROM scored_meme_daily d
JOIN memes m ON m.id = d.meme_id
CROSS JOIN LATERAL (
    SELECT DISTINCT lower(btrim(raw_tags.tag))::varchar(64) AS tag
    FROM unnest(m.tags) AS raw_tags(tag)
    WHERE btrim(raw_tags.tag) <> ''
) normalized_tags
WHERE m.is_public IS TRUE AND m.is_nsfw IS FALSE
GROUP BY normalized_tags.tag, d.observed_at
"""

_PUBLIC_TEMPLATE_TREND_POINTS_MV_SQL = f"""
CREATE MATERIALIZED VIEW public_template_trend_points_mv AS
{_DAILY_POINT_CTES}
SELECT
    t.id AS template_id,
    t.slug AS template_slug,
    t.name AS template_name,
    t.description AS template_description,
    d.observed_at,
    count(DISTINCT d.meme_id)::integer AS meme_count,
    sum(d.snapshot_count)::integer AS snapshot_count,
    sum(d.source_views)::integer AS source_views,
    sum(d.source_reactions)::integer AS source_reactions,
    sum(d.source_reposts)::integer AS source_reposts,
    sum(d.platform_views)::integer AS platform_views,
    sum(d.platform_sends)::integer AS platform_sends,
    sum(d.platform_saves)::integer AS platform_saves,
    sum(d.platform_likes)::integer AS platform_likes,
    sum(d.popularity_score)::double precision AS aggregate_value
FROM scored_meme_daily d
JOIN memes m ON m.id = d.meme_id
JOIN meme_templates t ON t.id = m.template_id
WHERE m.is_public IS TRUE AND m.is_nsfw IS FALSE
GROUP BY t.id, t.slug, t.name, t.description, d.observed_at
"""
