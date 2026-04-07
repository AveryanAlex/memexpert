# ruff: noqa: E501,I001,TC003
"""pipeline stage journal"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "pipeline_stage_journal",
        sa.Column("meme_file_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Enum(
                "ingest",
                "transcode",
                "sync_qdrant",
                "sync_meili",
                name="contentpipelinestage",
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
                "succeeded",
                "failed",
                "duplicate",
                name="contentpipelinestagestatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("normalized_reason", sa.String(length=128), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("is_retryable", sa.Boolean(), nullable=False),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_pipeline_stage_journal_pipeline_stage_journal_attempt_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["meme_file_id"],
            ["meme_files.id"],
            name=op.f("fk_pipeline_stage_journal_meme_file_id_meme_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_stage_journal")),
        sa.UniqueConstraint("meme_file_id", "stage", name="uq_pipeline_stage_journal_meme_file_id_stage"),
    )
    op.create_index(
        "ix_pipeline_stage_journal_last_event_id",
        "pipeline_stage_journal",
        ["last_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_stage_journal_meme_file_id_updated_at",
        "pipeline_stage_journal",
        ["meme_file_id", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_stage_journal_stage_status",
        "pipeline_stage_journal",
        ["stage", "status"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_stage_journal_status_retry_after",
        "pipeline_stage_journal",
        ["status", "retry_after"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_pipeline_stage_journal_status_retry_after", table_name="pipeline_stage_journal")
    op.drop_index("ix_pipeline_stage_journal_stage_status", table_name="pipeline_stage_journal")
    op.drop_index("ix_pipeline_stage_journal_meme_file_id_updated_at", table_name="pipeline_stage_journal")
    op.drop_index("ix_pipeline_stage_journal_last_event_id", table_name="pipeline_stage_journal")
    op.drop_table("pipeline_stage_journal")
