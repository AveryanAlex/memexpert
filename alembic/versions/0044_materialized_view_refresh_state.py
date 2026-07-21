# ruff: noqa: E501
"""compact trend read models and externalize refresh state

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-21
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_REFRESH_STATE_TABLE = "materialized_view_refresh_state"
_MATERIALIZED_VIEWS = (
    "public_meme_trends_mv",
    "public_tag_trends_mv",
    "public_template_trends_mv",
    "public_tag_trend_points_mv",
    "public_template_trend_points_mv",
    "public_meme_recommendation_features_mv",
)
_POINT_MATERIALIZED_VIEWS = (
    "public_tag_trend_points_mv",
    "public_template_trend_points_mv",
)
_TIMESTAMPED_VIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "public_meme_trends_mv": (
        "meme_id",
        "template_id",
        "tags",
        "media_type",
        "language",
        "is_nsfw",
        "recent_view_count",
        "previous_view_count",
        "recent_send_count",
        "previous_send_count",
        "recent_like_count",
        "previous_like_count",
        "recent_save_count",
        "previous_save_count",
        "recent_download_count",
        "previous_download_count",
        "latest_snapshot_at",
        "latest_source_views",
        "latest_source_reactions",
        "latest_source_reposts",
        "latest_platform_views",
        "latest_platform_sends",
        "latest_platform_saves",
        "latest_platform_likes",
        "latest_popularity_score",
        "engagement_24h",
        "trending_score",
    ),
    "public_tag_trends_mv": (
        "tag",
        "meme_count",
        "recent_view_count",
        "previous_view_count",
        "recent_send_count",
        "previous_send_count",
        "recent_like_count",
        "previous_like_count",
        "recent_save_count",
        "previous_save_count",
        "recent_download_count",
        "previous_download_count",
        "latest_source_views",
        "latest_source_reactions",
        "latest_source_reposts",
        "latest_platform_views",
        "latest_platform_sends",
        "latest_platform_saves",
        "latest_platform_likes",
        "latest_popularity_score",
        "latest_snapshot_at",
        "engagement_24h",
        "trending_score",
    ),
    "public_template_trends_mv": (
        "template_id",
        "template_slug",
        "template_name",
        "template_description",
        "meme_count",
        "recent_view_count",
        "previous_view_count",
        "recent_send_count",
        "previous_send_count",
        "recent_like_count",
        "previous_like_count",
        "recent_save_count",
        "previous_save_count",
        "recent_download_count",
        "previous_download_count",
        "latest_source_views",
        "latest_source_reactions",
        "latest_source_reposts",
        "latest_platform_views",
        "latest_platform_sends",
        "latest_platform_saves",
        "latest_platform_likes",
        "latest_popularity_score",
        "latest_snapshot_at",
        "engagement_24h",
        "trending_score",
    ),
    "public_meme_recommendation_features_mv": (
        "meme_id",
        "latest_published_at",
        "source_channel_ids",
        "representative_source_channel_id",
        "source_popularity_quantile",
        "source_quality_quantile",
        "technical_quality",
        "platform_response",
        "popularity_quantile",
        "trend_quantile",
        "template_id",
        "live_source_count",
        "exposure_count",
        "coverage_flags",
    ),
}


def upgrade() -> None:
    """Compact high-churn views and keep one refresh timestamp per view."""

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    op.create_table(
        _REFRESH_STATE_TABLE,
        sa.Column("view_name", sa.Text(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("view_name", name="pk_materialized_view_refresh_state"),
    )
    _rebuild_timestamped_materialized_views(
        timestamp_expression="NULL::timestamp with time zone",
        include_home_index=True,
    )
    for view_name in _POINT_MATERIALIZED_VIEWS:
        op.execute(f"REFRESH MATERIALIZED VIEW {view_name}")
    for view_name in _MATERIALIZED_VIEWS:
        op.execute(f"ANALYZE {view_name}")
    _seed_refresh_state()


def downgrade() -> None:
    """Restore per-row refresh timestamps while retaining shared statistics."""

    _rebuild_timestamped_materialized_views(
        timestamp_expression="now()",
        include_home_index=False,
    )
    for view_name in _TIMESTAMPED_VIEW_COLUMNS:
        op.execute(f"ANALYZE {view_name}")
    op.drop_table(_REFRESH_STATE_TABLE)
    # pg_stat_statements is cluster-shared and may predate this application.
    # Never remove the extension during an application-schema rollback.


def _seed_refresh_state() -> None:
    connection = op.get_bind()
    for view_name in _MATERIALIZED_VIEWS:
        connection.execute(
            sa.text(
                f"""
                INSERT INTO {_REFRESH_STATE_TABLE} (view_name, refreshed_at)
                VALUES (:view_name, statement_timestamp())
                """
            ),
            {"view_name": view_name},
        )


def _rebuild_timestamped_materialized_views(
    *,
    timestamp_expression: str,
    include_home_index: bool,
) -> None:
    definitions = {
        view_name: _materialized_view_definition(view_name)
        for view_name in _TIMESTAMPED_VIEW_COLUMNS
    }

    # PostgreSQL DDL is transactional. Existing readers either see the old
    # complete set or wait for the compact replacement set to commit.
    op.execute("DROP MATERIALIZED VIEW public_meme_recommendation_features_mv")
    op.execute("DROP MATERIALIZED VIEW public_template_trends_mv")
    op.execute("DROP MATERIALIZED VIEW public_tag_trends_mv")
    op.execute("DROP MATERIALIZED VIEW public_meme_trends_mv")

    _create_timestamped_materialized_view(
        "public_meme_trends_mv",
        definitions["public_meme_trends_mv"],
        timestamp_expression=timestamp_expression,
    )
    _create_meme_trend_indexes(include_home_index=include_home_index)

    _create_timestamped_materialized_view(
        "public_tag_trends_mv",
        definitions["public_tag_trends_mv"],
        timestamp_expression=timestamp_expression,
    )
    op.execute("CREATE UNIQUE INDEX uq_public_tag_trends_mv_tag ON public_tag_trends_mv (tag)")
    op.execute(
        "CREATE INDEX ix_public_tag_trends_mv_trending "
        "ON public_tag_trends_mv (trending_score DESC, engagement_24h DESC, tag)"
    )

    _create_timestamped_materialized_view(
        "public_template_trends_mv",
        definitions["public_template_trends_mv"],
        timestamp_expression=timestamp_expression,
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_public_template_trends_mv_template_id "
        "ON public_template_trends_mv (template_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_public_template_trends_mv_template_slug "
        "ON public_template_trends_mv (template_slug)"
    )
    op.execute(
        "CREATE INDEX ix_public_template_trends_mv_trending "
        "ON public_template_trends_mv (trending_score DESC, engagement_24h DESC, template_slug)"
    )

    _create_timestamped_materialized_view(
        "public_meme_recommendation_features_mv",
        definitions["public_meme_recommendation_features_mv"],
        timestamp_expression=timestamp_expression,
    )
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


def _materialized_view_definition(view_name: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_viewdef(CAST(:view_name AS regclass), true)"),
        {"view_name": view_name},
    )
    if not isinstance(definition, str) or not definition.strip():
        raise RuntimeError(f"Unable to read materialized view definition for {view_name}.")
    return definition.strip().removesuffix(";")


def _create_timestamped_materialized_view(
    view_name: str,
    definition: str,
    *,
    timestamp_expression: str,
) -> None:
    columns = _TIMESTAMPED_VIEW_COLUMNS[view_name]
    projection = ",\n    ".join(f"source.{column}" for column in columns)
    op.execute(
        f"""
        CREATE MATERIALIZED VIEW {view_name} AS
        SELECT
            {projection},
            {timestamp_expression} AS refreshed_at
        FROM (
            {definition}
        ) AS source
        """
    )


def _create_meme_trend_indexes(*, include_home_index: bool) -> None:
    op.execute("CREATE UNIQUE INDEX uq_public_meme_trends_mv_meme_id ON public_meme_trends_mv (meme_id)")
    op.execute(
        "CREATE INDEX ix_public_meme_trends_mv_trending "
        "ON public_meme_trends_mv (trending_score DESC, engagement_24h DESC, meme_id)"
    )
    if include_home_index:
        op.execute(
            "CREATE INDEX ix_public_meme_trends_mv_home_feed "
            "ON public_meme_trends_mv (trending_score DESC, meme_id ASC)"
        )
    op.execute(
        "CREATE INDEX ix_public_meme_trends_mv_fastest_rising ON public_meme_trends_mv "
        "(((recent_view_count + recent_send_count * 3 + recent_like_count * 5 + recent_save_count * 4) "
        "- (previous_view_count + previous_send_count * 3 + previous_like_count * 5 "
        "+ previous_save_count * 4)) DESC, trending_score DESC, meme_id)"
    )
    op.execute(
        "CREATE INDEX ix_public_meme_trends_mv_most_liked ON public_meme_trends_mv "
        "(recent_like_count DESC, latest_platform_likes DESC, trending_score DESC, meme_id)"
    )
    op.execute("CREATE INDEX ix_public_meme_trends_mv_template_id ON public_meme_trends_mv (template_id)")
    op.execute("CREATE INDEX ix_public_meme_trends_mv_tags ON public_meme_trends_mv USING gin (tags)")
    op.execute(
        "CREATE INDEX ix_public_meme_trends_mv_safe_filters "
        "ON public_meme_trends_mv (is_nsfw, media_type, language)"
    )
