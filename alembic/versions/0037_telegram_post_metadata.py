"""capture durable Telegram post metadata

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Add versioned text, relationship, edit, and deletion metadata."""

    op.add_column(
        "source_channel_posts",
        sa.Column("first_observed_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("latest_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column(
            "first_observed_text_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column(
            "latest_text_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("media_group_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("reply_to_post_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("telegram_edited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("metadata_first_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("metadata_last_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("metadata_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "source_channel_posts",
        sa.Column("deletion_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_source_channel_posts_metadata_version_non_negative"),
        "source_channel_posts",
        "metadata_version >= 0",
    )
    op.create_index(
        "ix_source_channel_posts_channel_media_group_post",
        "source_channel_posts",
        ["source_channel_id", "media_group_id", "post_id"],
        unique=False,
        postgresql_where=sa.text("media_group_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove captured Telegram post metadata."""

    op.drop_index(
        "ix_source_channel_posts_channel_media_group_post",
        table_name="source_channel_posts",
        postgresql_where=sa.text("media_group_id IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("ck_source_channel_posts_metadata_version_non_negative"),
        "source_channel_posts",
        type_="check",
    )
    op.drop_column("source_channel_posts", "deletion_observed_at")
    op.drop_column("source_channel_posts", "is_deleted")
    op.drop_column("source_channel_posts", "metadata_version")
    op.drop_column("source_channel_posts", "metadata_last_observed_at")
    op.drop_column("source_channel_posts", "metadata_first_observed_at")
    op.drop_column("source_channel_posts", "telegram_edited_at")
    op.drop_column("source_channel_posts", "reply_to_post_id")
    op.drop_column("source_channel_posts", "media_group_id")
    op.drop_column("source_channel_posts", "latest_text_entities")
    op.drop_column("source_channel_posts", "first_observed_text_entities")
    op.drop_column("source_channel_posts", "latest_text")
    op.drop_column("source_channel_posts", "first_observed_text")
