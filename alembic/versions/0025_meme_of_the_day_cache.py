# ruff: noqa: E501,I001,TC003
"""meme of the day cache"""

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
    """Apply this revision."""

    op.create_table(
        "meme_of_the_day_selections",
        sa.Column("selected_for", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.Column("meme_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "score_components",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "algorithm_version <> ''",
            name=op.f("ck_meme_of_the_day_selections_meme_of_the_day_algorithm_version_not_blank"),
        ),
        sa.CheckConstraint(
            "candidate_count >= 0",
            name=op.f("ck_meme_of_the_day_selections_meme_of_the_day_candidate_count_non_negative"),
        ),
        sa.CheckConstraint(
            "reason <> ''",
            name=op.f("ck_meme_of_the_day_selections_meme_of_the_day_reason_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["meme_id"],
            ["memes.id"],
            name=op.f("fk_meme_of_the_day_selections_meme_id_memes"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meme_of_the_day_selections")),
        sa.UniqueConstraint("selected_for", "algorithm_version", name="uq_motd_selected_for_algorithm_version"),
    )
    op.create_index(
        "ix_meme_of_the_day_selections_selected_for",
        "meme_of_the_day_selections",
        ["selected_for"],
        unique=False,
    )
    op.create_index(
        "ix_meme_of_the_day_selections_meme_id",
        "meme_of_the_day_selections",
        ["meme_id"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_meme_of_the_day_selections_meme_id", table_name="meme_of_the_day_selections")
    op.drop_index("ix_meme_of_the_day_selections_selected_for", table_name="meme_of_the_day_selections")
    op.drop_table("meme_of_the_day_selections")
