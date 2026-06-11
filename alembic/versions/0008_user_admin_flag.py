# ruff: noqa: E501,I001
"""user admin flag"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index("ix_users_is_admin", "users", ["is_admin"], unique=False)


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_users_is_admin", table_name="users")
    op.drop_column("users", "is_admin")
