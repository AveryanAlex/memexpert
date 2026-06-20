# ruff: noqa: E501,I001,TC003
"""telegram admin audit"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create immutable Telegram browser-admin audit history."""

    op.create_table(
        "telegram_admin_audit_logs",
        sa.Column("admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("telegram_session_id", sa.Uuid(), nullable=True),
        sa.Column("source_channel_id", sa.Uuid(), nullable=True),
        sa.Column(
            "previous_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "new_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.id"],
            name=op.f("fk_telegram_admin_audit_logs_admin_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_admin_audit_logs")),
    )
    op.create_index(
        "ix_telegram_admin_audit_logs_admin_created_at",
        "telegram_admin_audit_logs",
        ["admin_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_admin_audit_logs_action_created_at",
        "telegram_admin_audit_logs",
        ["action", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_admin_audit_logs_session_created_at",
        "telegram_admin_audit_logs",
        ["telegram_session_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_admin_audit_logs_channel_created_at",
        "telegram_admin_audit_logs",
        ["source_channel_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop Telegram browser-admin audit history."""

    op.drop_index("ix_telegram_admin_audit_logs_channel_created_at", table_name="telegram_admin_audit_logs")
    op.drop_index("ix_telegram_admin_audit_logs_session_created_at", table_name="telegram_admin_audit_logs")
    op.drop_index("ix_telegram_admin_audit_logs_action_created_at", table_name="telegram_admin_audit_logs")
    op.drop_index("ix_telegram_admin_audit_logs_admin_created_at", table_name="telegram_admin_audit_logs")
    op.drop_table("telegram_admin_audit_logs")
