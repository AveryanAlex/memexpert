# ruff: noqa: E501,I001,TC003
"""telegram session registry"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


TELEGRAM_SESSION_STATUS = sa.Enum(
    "active",
    "auth_required",
    "flood_wait",
    "quarantined",
    "stopped",
    name="telegramsessionstatus",
    native_enum=False,
    create_constraint=True,
)

LEGACY_TELEGRAM_SESSION_STATUS = sa.Enum(
    "active",
    "flood_wait",
    "quarantined",
    "stopped",
    name="telegramsessionstatus",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Apply this destructive prelaunch registry rewrite."""

    op.drop_index(
        "ix_telegram_session_states_status_updated_at",
        table_name="telegram_session_states",
    )
    op.drop_table("telegram_session_states")

    op.create_table(
        "telegram_sessions",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("encrypted_string_session", sa.Text(), nullable=True),
        sa.Column("account_user_id", sa.BigInteger(), nullable=True),
        sa.Column("account_username", sa.String(length=255), nullable=True),
        sa.Column("account_phone_hint", sa.String(length=64), nullable=True),
        sa.Column("status", TELEGRAM_SESSION_STATUS, nullable=False, server_default=sa.text("'auth_required'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_error_class", sa.String(length=128), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("flood_wait_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_listener_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("catchup_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("engagement_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("max_requests_per_second", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "max_requests_per_second > 0",
            name=op.f("ck_telegram_sessions_max_requests_per_second_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_sessions")),
        sa.UniqueConstraint("name", name="uq_telegram_sessions_name"),
    )
    op.create_index(
        "ix_telegram_sessions_status_updated_at",
        "telegram_sessions",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_sessions_enabled_status",
        "telegram_sessions",
        ["enabled", "status"],
        unique=False,
    )

    op.drop_column("source_channels", "session_id")
    op.add_column("source_channels", sa.Column("telegram_session_id", sa.Uuid(), nullable=True))
    op.add_column(
        "source_channels",
        sa.Column("live_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "source_channels",
        sa.Column("engagement_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_foreign_key(
        op.f("fk_source_channels_telegram_session_id_telegram_sessions"),
        "source_channels",
        "telegram_sessions",
        ["telegram_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_source_channels_telegram_session_id",
        "source_channels",
        ["telegram_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_source_channels_session_live",
        "source_channels",
        ["telegram_session_id", "is_active", "is_paused", "live_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_source_channels_session_engagement",
        "source_channels",
        ["telegram_session_id", "engagement_enabled"],
        unique=False,
    )


def downgrade() -> None:
    """Restore the pre-registry crawler session shape without data backfill."""

    op.drop_index("ix_source_channels_session_engagement", table_name="source_channels")
    op.drop_index("ix_source_channels_session_live", table_name="source_channels")
    op.drop_index("ix_source_channels_telegram_session_id", table_name="source_channels")
    op.drop_constraint(
        op.f("fk_source_channels_telegram_session_id_telegram_sessions"),
        "source_channels",
        type_="foreignkey",
    )
    op.drop_column("source_channels", "engagement_enabled")
    op.drop_column("source_channels", "live_enabled")
    op.drop_column("source_channels", "telegram_session_id")
    op.add_column("source_channels", sa.Column("session_id", sa.String(length=255), nullable=True))

    op.drop_index("ix_telegram_sessions_enabled_status", table_name="telegram_sessions")
    op.drop_index("ix_telegram_sessions_status_updated_at", table_name="telegram_sessions")
    op.drop_table("telegram_sessions")

    op.create_table(
        "telegram_session_states",
        sa.Column("session_name", sa.String(length=64), nullable=False),
        sa.Column("status", LEGACY_TELEGRAM_SESSION_STATUS, nullable=False),
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
