# ruff: noqa: E501,I001
"""public trend aggregate history points"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    _drop_public_trend_point_materialized_views()
    _drop_public_trend_materialized_views()
    _create_current_public_trend_materialized_views(include_alias_events=True)
    _create_public_trend_point_materialized_views()


def downgrade() -> None:
    """Revert this revision."""

    _drop_public_trend_point_materialized_views()
    _drop_public_trend_materialized_views()
    _create_current_public_trend_materialized_views(include_alias_events=False)


def _drop_public_trend_point_materialized_views() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_template_trend_points_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_tag_trend_points_mv")


def _drop_public_trend_materialized_views() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_template_trends_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_tag_trends_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public_meme_trends_mv")


def _create_current_public_trend_materialized_views(*, include_alias_events: bool) -> None:
    safe_event_values = [
        "'meme_view'",
        "'meme_send'",
        "'meme_like'",
        "'meme_save'",
        "'save'",
        "'favorite'",
        "'meme_download'",
    ]
    if include_alias_events:
        safe_event_values.extend(["'view'", "'share'", "'meme_share'", "'inline_sent'"])
        view_event_sql = "event_type IN ('meme_view', 'view')"
        send_event_sql = "event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent')"
    else:
        view_event_sql = "event_type = 'meme_view'"
        send_event_sql = "event_type = 'meme_send'"
    safe_events_sql = ",\n                ".join(safe_event_values)
    op.execute(
        f"""
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
                {safe_events_sql}
            )
        ),
        event_counts AS (
            SELECT
                meme_id,
                count(*) FILTER (WHERE {view_event_sql} AND occurred_at >= now() - interval '7 days')::integer AS recent_view_count,
                count(*) FILTER (WHERE {view_event_sql} AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_view_count,
                count(*) FILTER (WHERE {send_event_sql} AND occurred_at >= now() - interval '7 days')::integer AS recent_send_count,
                count(*) FILTER (WHERE {send_event_sql} AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_send_count,
                count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite') AND occurred_at >= now() - interval '7 days')::integer AS recent_like_count,
                count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite') AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_like_count,
                count(*) FILTER (WHERE event_type IN ('meme_save', 'save') AND occurred_at >= now() - interval '7 days')::integer AS recent_save_count,
                count(*) FILTER (WHERE event_type IN ('meme_save', 'save') AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_save_count,
                count(*) FILTER (WHERE event_type = 'meme_download' AND occurred_at >= now() - interval '7 days')::integer AS recent_download_count,
                count(*) FILTER (WHERE event_type = 'meme_download' AND occurred_at >= now() - interval '14 days' AND occurred_at < now() - interval '7 days')::integer AS previous_download_count,
                count(*) FILTER (WHERE {view_event_sql} AND occurred_at >= now() - interval '24 hours')::integer AS view_count_24h,
                count(*) FILTER (WHERE {send_event_sql} AND occurred_at >= now() - interval '24 hours')::integer AS send_count_24h,
                count(*) FILTER (WHERE event_type IN ('meme_like', 'favorite') AND occurred_at >= now() - interval '24 hours')::integer AS like_count_24h,
                count(*) FILTER (WHERE event_type IN ('meme_save', 'save') AND occurred_at >= now() - interval '24 hours')::integer AS save_count_24h,
                count(*) FILTER (WHERE event_type = 'meme_download' AND occurred_at >= now() - interval '24 hours')::integer AS download_count_24h
            FROM safe_events
            WHERE meme_id IS NOT NULL
            GROUP BY meme_id
        ),
        latest_snapshots AS (
            SELECT DISTINCT ON (meme_id)
                meme_id,
                captured_at AS latest_snapshot_at,
                source_views AS latest_source_views,
                source_reactions AS latest_source_reactions,
                source_reposts AS latest_source_reposts,
                platform_views AS latest_platform_views,
                platform_sends AS latest_platform_sends,
                platform_saves AS latest_platform_saves,
                platform_likes AS latest_platform_likes,
                popularity_score AS latest_popularity_score
            FROM meme_popularity_snapshots
            ORDER BY meme_id, captured_at DESC
        ),
        scored AS (
            SELECT
                m.id AS meme_id,
                m.template_id,
                m.tags,
                m.media_type::text AS media_type,
                m.language::text AS language,
                m.is_nsfw,
                COALESCE(ec.recent_view_count, 0) AS recent_view_count,
                COALESCE(ec.previous_view_count, 0) AS previous_view_count,
                COALESCE(ec.recent_send_count, 0) AS recent_send_count,
                COALESCE(ec.previous_send_count, 0) AS previous_send_count,
                COALESCE(ec.recent_like_count, 0) AS recent_like_count,
                COALESCE(ec.previous_like_count, 0) AS previous_like_count,
                COALESCE(ec.recent_save_count, 0) AS recent_save_count,
                COALESCE(ec.previous_save_count, 0) AS previous_save_count,
                COALESCE(ec.recent_download_count, 0) AS recent_download_count,
                COALESCE(ec.previous_download_count, 0) AS previous_download_count,
                ls.latest_snapshot_at,
                COALESCE(ls.latest_source_views, 0) AS latest_source_views,
                COALESCE(ls.latest_source_reactions, 0) AS latest_source_reactions,
                COALESCE(ls.latest_source_reposts, 0) AS latest_source_reposts,
                COALESCE(ls.latest_platform_views, 0) AS latest_platform_views,
                COALESCE(ls.latest_platform_sends, 0) AS latest_platform_sends,
                COALESCE(ls.latest_platform_saves, 0) AS latest_platform_saves,
                COALESCE(ls.latest_platform_likes, 0) AS latest_platform_likes,
                COALESCE(ls.latest_popularity_score, m.popularity_score, 0.0) AS latest_popularity_score,
                (
                    COALESCE(ec.view_count_24h, 0)
                    + COALESCE(ec.send_count_24h, 0) * 3
                    + COALESCE(ec.like_count_24h, 0) * 5
                    + COALESCE(ec.save_count_24h, 0) * 4
                    + COALESCE(ec.download_count_24h, 0) * 2
                )::double precision AS engagement_24h,
                (
                    COALESCE(ec.recent_view_count, 0)
                    + COALESCE(ec.recent_send_count, 0) * 3
                    + COALESCE(ec.recent_like_count, 0) * 5
                    + COALESCE(ec.recent_save_count, 0) * 4
                    + COALESCE(ec.recent_download_count, 0) * 2
                )::double precision AS recent_engagement,
                (
                    COALESCE(ec.previous_view_count, 0)
                    + COALESCE(ec.previous_send_count, 0) * 3
                    + COALESCE(ec.previous_like_count, 0) * 5
                    + COALESCE(ec.previous_save_count, 0) * 4
                    + COALESCE(ec.previous_download_count, 0) * 2
                )::double precision AS previous_engagement
            FROM memes m
            LEFT JOIN event_counts ec ON ec.meme_id = m.id
            LEFT JOIN latest_snapshots ls ON ls.meme_id = m.id
            WHERE m.is_public IS TRUE
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
    )
    op.execute("CREATE UNIQUE INDEX uq_public_meme_trends_mv_meme_id ON public_meme_trends_mv (meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_trending ON public_meme_trends_mv (trending_score DESC, engagement_24h DESC, meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_fastest_rising ON public_meme_trends_mv (((recent_view_count + recent_send_count * 3 + recent_like_count * 5 + recent_save_count * 4 + recent_download_count * 2) - (previous_view_count + previous_send_count * 3 + previous_like_count * 5 + previous_save_count * 4 + previous_download_count * 2)) DESC, trending_score DESC, meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_most_liked ON public_meme_trends_mv (recent_like_count DESC, latest_platform_likes DESC, trending_score DESC, meme_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_template_id ON public_meme_trends_mv (template_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_tags ON public_meme_trends_mv USING gin (tags)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_safe_filters ON public_meme_trends_mv (is_nsfw, media_type, language)")

    op.execute(
        """
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
    )
    op.execute("CREATE UNIQUE INDEX uq_public_tag_trends_mv_tag ON public_tag_trends_mv (tag)")
    op.execute("CREATE INDEX ix_public_tag_trends_mv_trending ON public_tag_trends_mv (trending_score DESC, engagement_24h DESC, tag)")

    op.execute(
        """
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
    )
    op.execute("CREATE UNIQUE INDEX uq_public_template_trends_mv_template_id ON public_template_trends_mv (template_id)")
    op.execute("CREATE UNIQUE INDEX uq_public_template_trends_mv_template_slug ON public_template_trends_mv (template_slug)")
    op.execute("CREATE INDEX ix_public_template_trends_mv_trending ON public_template_trends_mv (trending_score DESC, engagement_24h DESC, template_slug)")


def _create_public_trend_point_materialized_views() -> None:
    op.execute(
        """
        CREATE MATERIALIZED VIEW public_tag_trend_points_mv AS
        SELECT
            normalized_tags.tag,
            date_trunc('day', s.captured_at) AS observed_at,
            count(DISTINCT s.meme_id)::integer AS meme_count,
            count(*)::integer AS snapshot_count,
            sum(s.source_views)::integer AS source_views,
            sum(s.source_reactions)::integer AS source_reactions,
            sum(s.source_reposts)::integer AS source_reposts,
            sum(s.platform_views)::integer AS platform_views,
            sum(s.platform_sends)::integer AS platform_sends,
            sum(s.platform_saves)::integer AS platform_saves,
            sum(s.platform_likes)::integer AS platform_likes,
            sum(s.popularity_score)::double precision AS aggregate_value
        FROM meme_popularity_snapshots s
        JOIN memes m ON m.id = s.meme_id
        CROSS JOIN LATERAL (
            SELECT DISTINCT lower(btrim(raw_tags.tag))::varchar(64) AS tag
            FROM unnest(m.tags) AS raw_tags(tag)
            WHERE btrim(raw_tags.tag) <> ''
        ) normalized_tags
        WHERE m.is_public IS TRUE AND m.is_nsfw IS FALSE
        GROUP BY normalized_tags.tag, date_trunc('day', s.captured_at)
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_public_tag_trend_points_mv_tag_observed_at ON public_tag_trend_points_mv (tag, observed_at)")
    op.execute("CREATE INDEX ix_public_tag_trend_points_mv_observed_at ON public_tag_trend_points_mv (observed_at DESC, tag)")
    op.execute("CREATE INDEX ix_public_tag_trend_points_mv_value ON public_tag_trend_points_mv (aggregate_value DESC, observed_at DESC, tag)")

    op.execute(
        """
        CREATE MATERIALIZED VIEW public_template_trend_points_mv AS
        SELECT
            t.id AS template_id,
            t.slug AS template_slug,
            t.name AS template_name,
            t.description AS template_description,
            date_trunc('day', s.captured_at) AS observed_at,
            count(DISTINCT s.meme_id)::integer AS meme_count,
            count(*)::integer AS snapshot_count,
            sum(s.source_views)::integer AS source_views,
            sum(s.source_reactions)::integer AS source_reactions,
            sum(s.source_reposts)::integer AS source_reposts,
            sum(s.platform_views)::integer AS platform_views,
            sum(s.platform_sends)::integer AS platform_sends,
            sum(s.platform_saves)::integer AS platform_saves,
            sum(s.platform_likes)::integer AS platform_likes,
            sum(s.popularity_score)::double precision AS aggregate_value
        FROM meme_popularity_snapshots s
        JOIN memes m ON m.id = s.meme_id
        JOIN meme_templates t ON t.id = m.template_id
        WHERE m.is_public IS TRUE AND m.is_nsfw IS FALSE
        GROUP BY t.id, t.slug, t.name, t.description, date_trunc('day', s.captured_at)
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_public_template_trend_points_mv_template_observed_at ON public_template_trend_points_mv (template_id, observed_at)")
    op.execute("CREATE INDEX ix_public_template_trend_points_mv_slug_observed_at ON public_template_trend_points_mv (template_slug, observed_at)")
    op.execute("CREATE INDEX ix_public_template_trend_points_mv_observed_at ON public_template_trend_points_mv (observed_at DESC, template_slug)")
    op.execute("CREATE INDEX ix_public_template_trend_points_mv_value ON public_template_trend_points_mv (aggregate_value DESC, observed_at DESC, template_slug)")
