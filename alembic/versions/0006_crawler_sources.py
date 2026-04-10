# ruff: noqa: E501,I001,TC003
"""crawler sources"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    # --- meme_sources: Telegram publish time + forward attribution ----------
    # ``published_at`` captures the Telegram message publish timestamp so the
    # S04 freshness SLO can measure ``published_at → both_targets_synced``.
    # Operator uploads and backfilled rows leave it NULL because they have no
    # upstream publish time.
    op.add_column(
        "meme_sources",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Forward-chain attribution: when a reposter channel is ingested, the
    # ``source_id`` + ``post_id`` pair still points at the channel where we
    # SAW the post. These two columns preserve the original author so the
    # product surface can render "originally posted by X".
    op.add_column(
        "meme_sources",
        sa.Column("forwarded_from_source_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "meme_sources",
        sa.Column("forwarded_from_post_id", sa.String(length=255), nullable=True),
    )

    # --- source_channels: crawler checkpoint + catch-up controls -----------
    # ``catchup_message_limit`` bounds the initial catch-up window so a brand
    # new channel cannot drown the crawler by fetching years of history on
    # first run. ``catchup_enabled`` lets operators freeze catch-up without
    # disabling the live listener, and ``is_paused`` lets them freeze both
    # without deleting the channel row. ``last_fetched_at`` documents the
    # most recent successful crawler poll so the operator surface can show
    # staleness without walking the stage journal.
    op.add_column(
        "source_channels",
        sa.Column(
            "catchup_message_limit",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("500"),
        ),
    )
    op.add_column(
        "source_channels",
        sa.Column(
            "catchup_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "source_channels",
        sa.Column(
            "is_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "source_channels",
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- telegram_session_states: durable Telethon session health ----------
    op.create_table(
        "telegram_session_states",
        sa.Column("session_name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "flood_wait",
                "quarantined",
                "stopped",
                name="telegramsessionstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("last_error_class", sa.String(length=128), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("flood_wait_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_listener_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_session_states")),
        sa.UniqueConstraint("session_name", name="uq_telegram_session_states_session_name"),
    )
    op.create_index(
        "ix_telegram_session_states_status_updated_at",
        "telegram_session_states",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index(
        "ix_telegram_session_states_status_updated_at",
        table_name="telegram_session_states",
    )
    op.drop_table("telegram_session_states")

    op.drop_column("source_channels", "last_fetched_at")
    op.drop_column("source_channels", "is_paused")
    op.drop_column("source_channels", "catchup_enabled")
    op.drop_column("source_channels", "catchup_message_limit")

    op.drop_column("meme_sources", "forwarded_from_post_id")
    op.drop_column("meme_sources", "forwarded_from_source_id")
    op.drop_column("meme_sources", "published_at")
