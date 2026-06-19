# ruff: noqa: E501,I001
"""allow owner-scoped private upload sha duplicates"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.drop_constraint("uq_meme_files_sha256_hex", "meme_files", type_="unique")
    op.create_index("ix_meme_files_sha256_hex", "meme_files", ["sha256_hex"], unique=False)


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_meme_files_sha256_hex", table_name="meme_files")
    op.create_unique_constraint("uq_meme_files_sha256_hex", "meme_files", ["sha256_hex"])
