# ruff: noqa: E501,I001,TC003
"""pipeline heavy contracts"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD_PIPELINE_STAGE_VALUES: tuple[str, ...] = (
    "ingest",
    "transcode",
    "sync_qdrant",
    "sync_meili",
)
_NEW_PIPELINE_STAGE_VALUES: tuple[str, ...] = (
    "ingest",
    "transcode",
    "ocr",
    "embed",
    "classify",
    "sync_qdrant",
    "sync_meili",
)


def _replace_pipeline_stage_constraint(stage_values: tuple[str, ...]) -> None:
    allowed_values = ", ".join(f"'{value}'" for value in stage_values)
    op.drop_constraint("contentpipelinestage", "pipeline_stage_journal", type_="check")
    op.create_check_constraint(
        "contentpipelinestage",
        "pipeline_stage_journal",
        f"stage IN ({allowed_values})",
    )


def upgrade() -> None:
    """Apply this revision."""

    _replace_pipeline_stage_constraint(_NEW_PIPELINE_STAGE_VALUES)

    op.create_table(
        "meme_file_ocr_results",
        sa.Column("meme_file_id", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.String(length=64), nullable=False),
        sa.Column("fallback_engine", sa.String(length=64), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("low_confidence", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "language",
            sa.Enum(
                "en",
                "mixed",
                "none",
                "ru",
                name="contentlanguage",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("source_object_key", sa.Text(), nullable=True),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_meme_file_ocr_results_meme_file_ocr_results_confidence_between_zero_and_one"),
        ),
        sa.ForeignKeyConstraint(
            ["meme_file_id"],
            ["meme_files.id"],
            name=op.f("fk_meme_file_ocr_results_meme_file_id_meme_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meme_file_ocr_results")),
    )
    op.create_index(
        "ix_meme_file_ocr_results_engine_updated_at",
        "meme_file_ocr_results",
        ["engine", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_meme_file_ocr_results_low_confidence_updated_at",
        "meme_file_ocr_results",
        ["low_confidence", "updated_at"],
        unique=False,
    )
    op.create_index(
        "uq_meme_file_ocr_results_meme_file_id",
        "meme_file_ocr_results",
        ["meme_file_id"],
        unique=True,
    )

    op.create_table(
        "meme_merge_logs",
        sa.Column("source_meme_id", sa.Uuid(), nullable=False),
        sa.Column("source_meme_file_id", sa.Uuid(), nullable=True),
        sa.Column("target_meme_id", sa.Uuid(), nullable=False),
        sa.Column("target_primary_file_id", sa.Uuid(), nullable=True),
        sa.Column("similarity_score", sa.Float(), nullable=True),
        sa.Column("merge_reason", sa.String(length=128), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meme_merge_logs")),
    )
    op.create_index(
        "ix_meme_merge_logs_source_meme_file_id",
        "meme_merge_logs",
        ["source_meme_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_meme_merge_logs_source_meme_id_created_at",
        "meme_merge_logs",
        ["source_meme_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_meme_merge_logs_target_meme_id_created_at",
        "meme_merge_logs",
        ["target_meme_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_meme_merge_logs_target_meme_id_created_at", table_name="meme_merge_logs")
    op.drop_index("ix_meme_merge_logs_source_meme_id_created_at", table_name="meme_merge_logs")
    op.drop_index("ix_meme_merge_logs_source_meme_file_id", table_name="meme_merge_logs")
    op.drop_table("meme_merge_logs")

    op.drop_index("uq_meme_file_ocr_results_meme_file_id", table_name="meme_file_ocr_results")
    op.drop_index("ix_meme_file_ocr_results_low_confidence_updated_at", table_name="meme_file_ocr_results")
    op.drop_index("ix_meme_file_ocr_results_engine_updated_at", table_name="meme_file_ocr_results")
    op.drop_table("meme_file_ocr_results")

    _replace_pipeline_stage_constraint(_OLD_PIPELINE_STAGE_VALUES)
