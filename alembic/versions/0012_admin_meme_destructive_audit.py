# ruff: noqa: E501,I001
"""admin meme destructive audit"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "admin_meme_destructive_audit_logs",
        sa.Column("admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("source_meme_id", sa.Uuid(), nullable=False),
        sa.Column("target_meme_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("affected_snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.id"],
            name=op.f("fk_admin_meme_destructive_audit_logs_admin_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_meme_destructive_audit_logs")),
    )
    op.create_index(
        "ix_admin_meme_destructive_audit_logs_action_created_at",
        "admin_meme_destructive_audit_logs",
        ["action", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_meme_destructive_audit_logs_admin_created_at",
        "admin_meme_destructive_audit_logs",
        ["admin_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_meme_destructive_audit_logs_source_created_at",
        "admin_meme_destructive_audit_logs",
        ["source_meme_id", "created_at"],
        unique=False,
    )
    op.alter_column("admin_meme_destructive_audit_logs", "affected_snapshot", server_default=None)


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_admin_meme_destructive_audit_logs_source_created_at", table_name="admin_meme_destructive_audit_logs")
    op.drop_index("ix_admin_meme_destructive_audit_logs_admin_created_at", table_name="admin_meme_destructive_audit_logs")
    op.drop_index("ix_admin_meme_destructive_audit_logs_action_created_at", table_name="admin_meme_destructive_audit_logs")
    op.drop_table("admin_meme_destructive_audit_logs")
