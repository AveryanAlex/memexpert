# ruff: noqa: E501,I001
"""canonical primary file pointer"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.drop_constraint("fk_memes_primary_file_id_meme_files", "memes", type_="foreignkey")

    # Pre-production destructive repair: a committed meme must have at least one
    # file so it can own a non-null canonical primary_file_id.
    op.execute(
        """
        DELETE FROM memes AS m
        WHERE NOT EXISTS (
            SELECT 1
            FROM meme_files AS mf
            WHERE mf.meme_id = m.id
        )
        """
    )

    # Rewrite canonical primary_file_id from the old duplicated flag when present,
    # otherwise choose the best available file by quality, creation time, then id.
    op.execute(
        """
        WITH ranked_files AS (
            SELECT
                mf.meme_id,
                mf.id,
                row_number() OVER (
                    PARTITION BY mf.meme_id
                    ORDER BY mf.is_primary DESC, mf.quality_score DESC, mf.created_at DESC, mf.id DESC
                ) AS rank
            FROM meme_files AS mf
        )
        UPDATE memes AS m
        SET primary_file_id = ranked_files.id
        FROM ranked_files
        WHERE ranked_files.meme_id = m.id
          AND ranked_files.rank = 1
        """
    )

    op.alter_column("memes", "primary_file_id", existing_type=sa.Uuid(), nullable=False)
    op.create_unique_constraint("uq_meme_files_meme_id_id", "meme_files", ["meme_id", "id"])
    op.create_foreign_key(
        "fk_memes_primary_file_id_meme_files",
        "memes",
        "meme_files",
        ["id", "primary_file_id"],
        ["meme_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_index("uq_meme_files_single_primary_per_meme", table_name="meme_files", postgresql_where=sa.text("is_primary"))
    op.drop_column("meme_files", "is_primary")


def downgrade() -> None:
    """Revert this revision."""

    op.add_column(
        "meme_files",
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        """
        UPDATE meme_files AS mf
        SET is_primary = true
        FROM memes AS m
        WHERE m.primary_file_id = mf.id
        """
    )
    op.alter_column("meme_files", "is_primary", server_default=None)
    op.drop_constraint("fk_memes_primary_file_id_meme_files", "memes", type_="foreignkey")
    op.alter_column("memes", "primary_file_id", existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint("uq_meme_files_meme_id_id", "meme_files", type_="unique")
    op.create_index(
        "uq_meme_files_single_primary_per_meme",
        "meme_files",
        ["meme_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_foreign_key(
        "fk_memes_primary_file_id_meme_files",
        "memes",
        "meme_files",
        ["primary_file_id"],
        ["id"],
        ondelete="SET NULL",
    )
