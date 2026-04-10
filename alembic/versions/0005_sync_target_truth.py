# ruff: noqa: E501,I001,TC003
"""sync target truth"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "meme_file_sync_target_snapshots",
        sa.Column("meme_file_id", sa.Uuid(), nullable=False),
        sa.Column(
            "sync_target",
            sa.Enum(
                "qdrant",
                "meilisearch",
                name="synctargetkind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "synced",
                "failed",
                name="synctargetstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("normalized_reason", sa.String(length=128), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column(
            "last_payload_preview",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_meme_file_sync_target_snapshots_meme_file_sync_target_snapshots_attempt_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["meme_file_id"],
            ["meme_files.id"],
            name=op.f("fk_meme_file_sync_target_snapshots_meme_file_id_meme_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meme_file_sync_target_snapshots")),
        sa.UniqueConstraint(
            "meme_file_id",
            "sync_target",
            name="uq_meme_file_sync_target_snapshots_meme_file_id_sync_target",
        ),
    )
    op.create_index(
        "ix_meme_file_sync_target_snapshots_target_updated_at",
        "meme_file_sync_target_snapshots",
        ["sync_target", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index(
        "ix_meme_file_sync_target_snapshots_target_updated_at",
        table_name="meme_file_sync_target_snapshots",
    )
    op.drop_table("meme_file_sync_target_snapshots")
