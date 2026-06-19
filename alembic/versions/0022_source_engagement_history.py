# ruff: noqa: E501,I001
"""source engagement history"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


SOURCE_ENGAGEMENT_CAPTURE_REASON = sa.Enum(
    "ingest_initial",
    "scheduled",
    "manual_refresh",
    name="sourceengagementcapturereason",
    native_enum=False,
    create_constraint=True,
)

SOURCE_ENGAGEMENT_SCHEDULE_LABEL = sa.Enum(
    "ingest_initial",
    "plus_1h",
    "plus_3h",
    "plus_12h",
    "plus_1d",
    "plus_3d",
    "plus_7d",
    "plus_1month",
    "monthly",
    name="sourceengagementschedulelabel",
    native_enum=False,
    create_constraint=True,
)

SOURCE_ENGAGEMENT_COMMENTS_STATE = sa.Enum(
    "unknown",
    "enabled",
    "disabled",
    "not_exposed",
    name="sourceengagementcommentsstate",
    native_enum=False,
    create_constraint=True,
)

SOURCE_ENGAGEMENT_FETCH_STATUS = sa.Enum(
    "success",
    "not_found",
    "not_accessible",
    "failed",
    name="sourceengagementfetchstatus",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Apply this revision."""

    op.add_column("meme_sources", sa.Column("last_engagement_check_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meme_sources", sa.Column("next_engagement_check_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meme_sources", sa.Column("engagement_check_locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meme_sources", sa.Column("engagement_check_lock_owner", sa.String(length=255), nullable=True))
    op.add_column(
        "meme_sources",
        sa.Column(
            "engagement_check_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("meme_sources", sa.Column("last_engagement_error_code", sa.String(length=128), nullable=True))
    op.create_check_constraint(
        op.f("ck_meme_sources_meme_sources_engagement_check_attempt_count_non_negative"),
        "meme_sources",
        "engagement_check_attempt_count >= 0",
    )
    op.create_index(
        "ix_meme_sources_engagement_due_lease",
        "meme_sources",
        ["next_engagement_check_at", "engagement_check_locked_at"],
        unique=False,
    )

    op.create_table(
        "meme_source_engagement_snapshots",
        sa.Column("meme_source_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capture_reason", SOURCE_ENGAGEMENT_CAPTURE_REASON, nullable=False),
        sa.Column("schedule_label", SOURCE_ENGAGEMENT_SCHEDULE_LABEL, nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=True),
        sa.Column("reactions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reaction_count", sa.Integer(), nullable=True),
        sa.Column("comment_count", sa.Integer(), nullable=True),
        sa.Column("forward_count", sa.Integer(), nullable=True),
        sa.Column("comments_state", SOURCE_ENGAGEMENT_COMMENTS_STATE, nullable=False),
        sa.Column("fetch_status", SOURCE_ENGAGEMENT_FETCH_STATUS, nullable=False),
        sa.Column("source_alive", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "raw_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "comment_count IS NULL OR comment_count >= 0",
            name=op.f("ck_meme_source_engagement_snapshots_meme_source_engagement_snapshots_comment_count_non_negative"),
        ),
        sa.CheckConstraint(
            "forward_count IS NULL OR forward_count >= 0",
            name=op.f("ck_meme_source_engagement_snapshots_meme_source_engagement_snapshots_forward_count_non_negative"),
        ),
        sa.CheckConstraint(
            "reaction_count IS NULL OR reaction_count >= 0",
            name=op.f("ck_meme_source_engagement_snapshots_meme_source_engagement_snapshots_reaction_count_non_negative"),
        ),
        sa.CheckConstraint(
            "view_count IS NULL OR view_count >= 0",
            name=op.f("ck_meme_source_engagement_snapshots_meme_source_engagement_snapshots_view_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["meme_source_id"],
            ["meme_sources.id"],
            name=op.f("fk_meme_source_engagement_snapshots_meme_source_id_meme_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meme_source_engagement_snapshots")),
        sa.UniqueConstraint(
            "meme_source_id",
            "captured_at",
            name="uq_meme_source_engagement_snapshots_source_captured_at",
        ),
    )
    op.execute(
        "CREATE INDEX ix_meme_source_engagement_snapshots_source_captured_desc "
        "ON meme_source_engagement_snapshots (meme_source_id, captured_at DESC)"
    )
    op.create_index(
        "ix_meme_source_engagement_snapshots_label_status_captured",
        "meme_source_engagement_snapshots",
        ["schedule_label", "fetch_status", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_meme_source_engagement_snapshots_label_status_captured", table_name="meme_source_engagement_snapshots")
    op.drop_index("ix_meme_source_engagement_snapshots_source_captured_desc", table_name="meme_source_engagement_snapshots")
    op.drop_table("meme_source_engagement_snapshots")

    op.drop_index("ix_meme_sources_engagement_due_lease", table_name="meme_sources")
    op.drop_constraint(
        op.f("ck_meme_sources_meme_sources_engagement_check_attempt_count_non_negative"),
        "meme_sources",
        type_="check",
    )
    op.drop_column("meme_sources", "last_engagement_error_code")
    op.drop_column("meme_sources", "engagement_check_attempt_count")
    op.drop_column("meme_sources", "engagement_check_lock_owner")
    op.drop_column("meme_sources", "engagement_check_locked_at")
    op.drop_column("meme_sources", "next_engagement_check_at")
    op.drop_column("meme_sources", "last_engagement_check_at")
