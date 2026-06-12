# ruff: noqa: E501,I001
"""blocked perceptual hashes"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "blocked_perceptual_hashes",
        sa.Column("perceptual_hash", sa.String(length=64), nullable=False),
        sa.Column("hash_algorithm", sa.String(length=32), nullable=False),
        sa.Column("hash_size", sa.Integer(), nullable=False),
        sa.Column("max_hamming_distance", sa.Integer(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "copyright",
                "harassment",
                "illegal",
                "nsfw",
                "other",
                "spam",
                name="moderationreason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("hash_size > 0", name=op.f("ck_blocked_perceptual_hashes_blocked_perceptual_hashes_hash_size_positive")),
        sa.CheckConstraint(
            "max_hamming_distance >= 0 AND max_hamming_distance <= hash_size",
            name=op.f("ck_blocked_perceptual_hashes_blocked_perceptual_hashes_distance_within_hash_size"),
        ),
        sa.CheckConstraint(
            "perceptual_hash = lower(perceptual_hash)",
            name=op.f("ck_blocked_perceptual_hashes_blocked_perceptual_hashes_hash_lowercase"),
        ),
        sa.CheckConstraint(
            "perceptual_hash ~ '^[0-9a-f]+$'",
            name=op.f("ck_blocked_perceptual_hashes_blocked_perceptual_hashes_hash_hex"),
        ),
        sa.CheckConstraint(
            "hash_size = char_length(perceptual_hash) * 4",
            name=op.f("ck_blocked_perceptual_hashes_blocked_perceptual_hashes_hash_size_matches_hash"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_user_id"],
            ["users.id"],
            name=op.f("fk_blocked_perceptual_hashes_created_by_admin_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocked_perceptual_hashes")),
        sa.UniqueConstraint(
            "hash_algorithm",
            "hash_size",
            "perceptual_hash",
            name="uq_blocked_perceptual_hashes_algorithm_size_hash",
        ),
    )
    op.create_index(
        "ix_blocked_perceptual_hashes_active_algorithm",
        "blocked_perceptual_hashes",
        ["is_active", "hash_algorithm"],
        unique=False,
    )
    op.create_index(
        "ix_blocked_perceptual_hashes_created_by_created_at",
        "blocked_perceptual_hashes",
        ["created_by_admin_user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "blocked_perceptual_hash_audit_logs",
        sa.Column("blocked_perceptual_hash_id", sa.Uuid(), nullable=False),
        sa.Column("admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("previous_values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("new_values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.id"],
            name=op.f("fk_blocked_perceptual_hash_audit_logs_admin_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocked_perceptual_hash_audit_logs")),
    )
    op.create_index(
        "ix_blocked_perceptual_hash_audit_logs_action_created_at",
        "blocked_perceptual_hash_audit_logs",
        ["action", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_blocked_perceptual_hash_audit_logs_admin_created_at",
        "blocked_perceptual_hash_audit_logs",
        ["admin_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_blocked_perceptual_hash_audit_logs_hash_created_at",
        "blocked_perceptual_hash_audit_logs",
        ["blocked_perceptual_hash_id", "created_at"],
        unique=False,
    )
    op.alter_column("blocked_perceptual_hash_audit_logs", "previous_values", server_default=None)
    op.alter_column("blocked_perceptual_hash_audit_logs", "new_values", server_default=None)

    op.add_column("meme_files", sa.Column("blocked_perceptual_hash_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_meme_files_blocked_perceptual_hash_id_blocked_perceptual_hashes"),
        "meme_files",
        "blocked_perceptual_hashes",
        ["blocked_perceptual_hash_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_constraint(
        op.f("fk_meme_files_blocked_perceptual_hash_id_blocked_perceptual_hashes"),
        "meme_files",
        type_="foreignkey",
    )
    op.drop_column("meme_files", "blocked_perceptual_hash_id")

    op.drop_index("ix_blocked_perceptual_hash_audit_logs_hash_created_at", table_name="blocked_perceptual_hash_audit_logs")
    op.drop_index("ix_blocked_perceptual_hash_audit_logs_admin_created_at", table_name="blocked_perceptual_hash_audit_logs")
    op.drop_index("ix_blocked_perceptual_hash_audit_logs_action_created_at", table_name="blocked_perceptual_hash_audit_logs")
    op.drop_table("blocked_perceptual_hash_audit_logs")

    op.drop_index("ix_blocked_perceptual_hashes_created_by_created_at", table_name="blocked_perceptual_hashes")
    op.drop_index("ix_blocked_perceptual_hashes_active_algorithm", table_name="blocked_perceptual_hashes")
    op.drop_table("blocked_perceptual_hashes")
