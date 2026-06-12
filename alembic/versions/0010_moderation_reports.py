# ruff: noqa: E501,I001,TC003
"""moderation reports and decision audit"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "moderation_reports",
        sa.Column("meme_id", sa.Uuid(), nullable=False),
        sa.Column("reporter_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "in_review",
                "resolved",
                "dismissed",
                name="moderationreportstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
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
        sa.Column("resolved_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["meme_id"], ["memes.id"], name=op.f("fk_moderation_reports_meme_id_memes"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reporter_user_id"], ["users.id"], name=op.f("fk_moderation_reports_reporter_user_id_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_admin_user_id"], ["users.id"], name=op.f("fk_moderation_reports_resolved_by_admin_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_reports")),
    )
    op.create_index("ix_moderation_reports_meme_id_status", "moderation_reports", ["meme_id", "status"], unique=False)
    op.create_index("ix_moderation_reports_reporter_user_id_created_at", "moderation_reports", ["reporter_user_id", "created_at"], unique=False)
    op.create_index("ix_moderation_reports_status_created_at", "moderation_reports", ["status", "created_at"], unique=False)

    op.create_table(
        "moderation_decisions",
        sa.Column("meme_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("admin_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "hide",
                "hide_and_mark_nsfw",
                "mark_nsfw",
                "mark_sfw",
                "no_action",
                "override_flags",
                "publish",
                name="moderationaction",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
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
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("previous_is_public", sa.Boolean(), nullable=False),
        sa.Column("previous_is_nsfw", sa.Boolean(), nullable=False),
        sa.Column("new_is_public", sa.Boolean(), nullable=False),
        sa.Column("new_is_nsfw", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"], name=op.f("fk_moderation_decisions_admin_user_id_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["meme_id"], ["memes.id"], name=op.f("fk_moderation_decisions_meme_id_memes"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["moderation_reports.id"], name=op.f("fk_moderation_decisions_report_id_moderation_reports"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_decisions")),
    )
    op.create_index("ix_moderation_decisions_admin_user_id_created_at", "moderation_decisions", ["admin_user_id", "created_at"], unique=False)
    op.create_index("ix_moderation_decisions_meme_id_created_at", "moderation_decisions", ["meme_id", "created_at"], unique=False)
    op.create_index("ix_moderation_decisions_report_id_created_at", "moderation_decisions", ["report_id", "created_at"], unique=False)


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_moderation_decisions_report_id_created_at", table_name="moderation_decisions")
    op.drop_index("ix_moderation_decisions_meme_id_created_at", table_name="moderation_decisions")
    op.drop_index("ix_moderation_decisions_admin_user_id_created_at", table_name="moderation_decisions")
    op.drop_table("moderation_decisions")
    op.drop_index("ix_moderation_reports_status_created_at", table_name="moderation_reports")
    op.drop_index("ix_moderation_reports_reporter_user_id_created_at", table_name="moderation_reports")
    op.drop_index("ix_moderation_reports_meme_id_status", table_name="moderation_reports")
    op.drop_table("moderation_reports")
