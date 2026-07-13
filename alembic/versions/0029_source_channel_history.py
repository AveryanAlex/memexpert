# ruff: noqa: E501,I001,TC003
"""source channel post inventory and older-history backfill jobs"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


SOURCE_CHANNEL_POST_STATUS = sa.Enum(
    "observed",
    "accepted",
    "unsupported",
    "failed",
    name="sourcechannelpoststatus",
    native_enum=False,
    create_constraint=True,
)

SOURCE_CHANNEL_BACKFILL_JOB_STATUS = sa.Enum(
    "queued",
    "running",
    "completed",
    "failed",
    name="sourcechannelbackfilljobstatus",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Add independent history coverage and per-post crawler truth."""

    op.execute(
        sa.text(
            "UPDATE source_channels SET catchup_message_limit = 5000 "
            "WHERE catchup_message_limit = 500"
        )
    )
    op.alter_column(
        "source_channels",
        "catchup_message_limit",
        existing_type=sa.Integer(),
        server_default=sa.text("5000"),
        existing_nullable=False,
    )
    op.add_column(
        "source_channels",
        sa.Column("oldest_observed_post_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("history_cursor_post_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("initial_catchup_completed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "source_channels",
        sa.Column("history_exhausted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "source_channel_posts",
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.String(length=255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=True),
        sa.Column("status", SOURCE_CHANNEL_POST_STATUS, server_default=sa.text("'observed'"), nullable=False),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_source_channel_posts_source_channel_posts_attempt_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name=op.f("fk_source_channel_posts_source_channel_id_source_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_channel_posts")),
        sa.UniqueConstraint(
            "source_channel_id",
            "post_id",
            name="uq_source_channel_posts_channel_post",
        ),
    )
    op.create_index(
        "ix_source_channel_posts_channel_published_at",
        "source_channel_posts",
        ["source_channel_id", "published_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_channel_posts_channel_status",
        "source_channel_posts",
        ["source_channel_id", "status"],
        unique=False,
    )

    # Preserve visibility for content accepted before this inventory existed.
    # Pipeline request ids are safe row ids because the source identity is
    # already unique and a matching legacy MemeSource insert below conflicts
    # on ``(source_channel_id, post_id)`` rather than creating a duplicate.
    op.execute(
        sa.text(
            """
            INSERT INTO source_channel_posts (
                id,
                source_channel_id,
                post_id,
                published_at,
                media_type,
                status,
                last_error_code,
                last_error_text,
                attempt_count,
                created_at,
                updated_at
            )
            SELECT
                request.id,
                channel.id,
                request.post_id,
                COALESCE(source.published_at, request.created_at),
                NULLIF(request.source_metadata ->> 'media_type', ''),
                'accepted',
                NULL,
                NULL,
                GREATEST(request.attempt_count, 1),
                request.created_at,
                request.updated_at
            FROM pipeline_ingest_requests AS request
            JOIN source_channels AS channel
              ON channel.platform = request.source_platform
             AND channel.platform_id = request.source_id
            LEFT JOIN meme_sources AS source
              ON source.platform = request.source_platform
             AND source.source_id = request.source_id
             AND source.post_id = request.post_id
            WHERE request.source_platform = 'telegram'
            ON CONFLICT (source_channel_id, post_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO source_channel_posts (
                id,
                source_channel_id,
                post_id,
                published_at,
                media_type,
                status,
                last_error_code,
                last_error_text,
                attempt_count,
                created_at,
                updated_at
            )
            SELECT
                source.id,
                channel.id,
                source.post_id,
                COALESCE(source.published_at, source.created_at),
                NULL,
                'accepted',
                NULL,
                NULL,
                1,
                source.created_at,
                source.updated_at
            FROM meme_sources AS source
            JOIN source_channels AS channel
              ON channel.platform = source.platform
             AND channel.platform_id = source.source_id
            WHERE source.platform = 'telegram'
            ON CONFLICT (source_channel_id, post_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE source_channels AS channel
            SET oldest_observed_post_id = inventory.oldest_post_id
            FROM (
                SELECT
                    source_channel_id,
                    MIN(post_id::bigint)::text AS oldest_post_id
                FROM source_channel_posts
                WHERE post_id ~ '^[0-9]+$'
                GROUP BY source_channel_id
            ) AS inventory
            WHERE inventory.source_channel_id = channel.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE source_channels
            SET history_cursor_post_id =
                CASE
                    WHEN last_read_post_id ~ '^[0-9]+$'
                        THEN (last_read_post_id::bigint + 1)::text
                    ELSE oldest_observed_post_id
                END
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE source_channels
            SET initial_catchup_completed = (history_cursor_post_id IS NOT NULL)
            """
        )
    )

    op.create_table(
        "source_channel_backfill_jobs",
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            SOURCE_CHANNEL_BACKFILL_JOB_STATUS,
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("requested_message_count", sa.Integer(), nullable=False),
        sa.Column("scanned_message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cursor_post_id", sa.String(length=255), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_owner", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "requested_message_count >= 1 AND requested_message_count <= 50000",
            name=op.f("ck_source_channel_backfill_jobs_source_channel_backfill_jobs_requested_count_bounded"),
        ),
        sa.CheckConstraint(
            "scanned_message_count >= 0 AND scanned_message_count <= requested_message_count",
            name=op.f("ck_source_channel_backfill_jobs_source_channel_backfill_jobs_scanned_count_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_admin_user_id"],
            ["users.id"],
            name=op.f("fk_source_channel_backfill_jobs_requested_by_admin_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name=op.f("fk_source_channel_backfill_jobs_source_channel_id_source_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_channel_backfill_jobs")),
    )
    op.create_index(
        "ix_source_channel_backfill_jobs_status_locked_created",
        "source_channel_backfill_jobs",
        ["status", "locked_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_channel_backfill_jobs_channel_created_id",
        "source_channel_backfill_jobs",
        ["source_channel_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_source_channel_backfill_jobs_one_active_per_channel",
        "source_channel_backfill_jobs",
        ["source_channel_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    """Remove source history inventory and crawler-owned backfill work."""

    op.drop_index(
        "uq_source_channel_backfill_jobs_one_active_per_channel",
        table_name="source_channel_backfill_jobs",
    )
    op.drop_index(
        "ix_source_channel_backfill_jobs_channel_created_id",
        table_name="source_channel_backfill_jobs",
    )
    op.drop_index(
        "ix_source_channel_backfill_jobs_status_locked_created",
        table_name="source_channel_backfill_jobs",
    )
    op.drop_table("source_channel_backfill_jobs")

    op.drop_index("ix_source_channel_posts_channel_status", table_name="source_channel_posts")
    op.drop_index("ix_source_channel_posts_channel_published_at", table_name="source_channel_posts")
    op.drop_table("source_channel_posts")

    op.drop_column("source_channels", "history_exhausted")
    op.drop_column("source_channels", "initial_catchup_completed")
    op.drop_column("source_channels", "history_cursor_post_id")
    op.drop_column("source_channels", "oldest_observed_post_id")
    op.execute(
        sa.text(
            "UPDATE source_channels SET catchup_message_limit = 500 "
            "WHERE catchup_message_limit = 5000"
        )
    )
    op.alter_column(
        "source_channels",
        "catchup_message_limit",
        existing_type=sa.Integer(),
        server_default=sa.text("500"),
        existing_nullable=False,
    )
