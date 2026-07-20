"""capture Telegram channel audience snapshots

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-20
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


SOURCE_CHANNEL_AUDIENCE_CAPTURE_REASON = sa.Enum(
    "initial_resolution",
    "crawler_refresh",
    "scheduled",
    name="sourcechannelaudiencecapturereason",
    native_enum=False,
    create_constraint=True,
)

SOURCE_CHANNEL_AUDIENCE_FETCH_STATUS = sa.Enum(
    "success",
    "not_exposed",
    "failed",
    name="sourcechannelaudiencefetchstatus",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Add audience observation truth and channel scheduling/cache metadata."""

    op.add_column(
        "source_channels",
        sa.Column("subscriber_count_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("last_audience_capture_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("next_audience_capture_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("audience_capture_locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("audience_capture_lock_owner", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "source_channels",
        sa.Column("audience_capture_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "source_channels",
        sa.Column("last_audience_error_code", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_source_channels_source_channels_audience_capture_attempt_count_non_negative"),
        "source_channels",
        "audience_capture_attempt_count >= 0",
    )
    op.create_index(
        "ix_source_channels_session_audience_due",
        "source_channels",
        ["telegram_session_id", "next_audience_capture_at", "audience_capture_locked_at"],
        unique=False,
    )

    op.create_table(
        "source_channel_audience_snapshots",
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_session_id", sa.Uuid(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_slot", sa.Date(), nullable=False),
        sa.Column("capture_reason", SOURCE_CHANNEL_AUDIENCE_CAPTURE_REASON, nullable=False),
        sa.Column("fetch_status", SOURCE_CHANNEL_AUDIENCE_FETCH_STATUS, nullable=False),
        sa.Column("subscriber_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "subscriber_count IS NULL OR subscriber_count >= 0",
            name=op.f(
                "ck_source_channel_audience_snapshots_"
                "source_channel_audience_snapshots_subscriber_count_non_negative"
            ),
        ),
        sa.CheckConstraint(
            "(fetch_status = 'success') = (subscriber_count IS NOT NULL)",
            name=op.f("ck_source_channel_audience_snapshots_status_count"),
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name=op.f("fk_source_channel_audience_snapshots_source_channel_id_source_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["telegram_session_id"],
            ["telegram_sessions.id"],
            name=op.f("fk_source_channel_audience_snapshots_telegram_session_id_telegram_sessions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_channel_audience_snapshots")),
        sa.UniqueConstraint(
            "source_channel_id",
            "capture_slot",
            "capture_reason",
            name="uq_source_channel_audience_snapshots_channel_slot_reason",
        ),
    )
    op.create_index(
        "ix_source_channel_audience_snapshots_channel_captured_desc",
        "source_channel_audience_snapshots",
        ["source_channel_id", sa.text("captured_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_source_channel_audience_snapshots_slot_status",
        "source_channel_audience_snapshots",
        ["capture_slot", "fetch_status"],
        unique=False,
    )
    op.create_index(
        "ix_source_channel_audience_snapshots_session_captured",
        "source_channel_audience_snapshots",
        ["telegram_session_id", "captured_at"],
        unique=False,
    )

    # Existing cached counts have no trustworthy observation time, so do not
    # synthesize snapshot history. Only distribute the first real capture over
    # the next hour to avoid a deployment-time Telegram request spike.
    op.execute(
        sa.text(
            """
            UPDATE source_channels
            SET next_audience_capture_at = CURRENT_TIMESTAMP
                + (mod(abs(hashtext(id::text)::bigint), 3600) * interval '1 second')
            WHERE platform = 'telegram'
              AND is_active = true
              AND telegram_session_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    """Remove channel audience snapshots and scheduling/cache metadata."""

    op.drop_index(
        "ix_source_channel_audience_snapshots_session_captured",
        table_name="source_channel_audience_snapshots",
    )
    op.drop_index(
        "ix_source_channel_audience_snapshots_slot_status",
        table_name="source_channel_audience_snapshots",
    )
    op.drop_index(
        "ix_source_channel_audience_snapshots_channel_captured_desc",
        table_name="source_channel_audience_snapshots",
    )
    op.drop_table("source_channel_audience_snapshots")

    op.drop_index("ix_source_channels_session_audience_due", table_name="source_channels")
    op.drop_constraint(
        op.f("ck_source_channels_source_channels_audience_capture_attempt_count_non_negative"),
        "source_channels",
        type_="check",
    )
    op.drop_column("source_channels", "last_audience_error_code")
    op.drop_column("source_channels", "audience_capture_attempt_count")
    op.drop_column("source_channels", "audience_capture_lock_owner")
    op.drop_column("source_channels", "audience_capture_locked_at")
    op.drop_column("source_channels", "next_audience_capture_at")
    op.drop_column("source_channels", "last_audience_capture_at")
    op.drop_column("source_channels", "subscriber_count_updated_at")
