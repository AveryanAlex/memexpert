# ruff: noqa: E501,I001
"""derive account type from login identities"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ACCOUNT_TYPE_ENUM = sa.Enum(
    "guest",
    "full",
    name="accounttype",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Apply this revision."""

    op.drop_index("ix_users_account_type_status", table_name="users")
    op.drop_column("users", "account_type")


def downgrade() -> None:
    """Revert this revision."""

    op.add_column(
        "users",
        sa.Column("account_type", _ACCOUNT_TYPE_ENUM, nullable=True),
    )
    op.execute(
        """
        UPDATE users
        SET account_type = CASE
            WHEN telegram_id IS NOT NULL
              OR google_id IS NOT NULL
              OR email IS NOT NULL
              OR length(trim(password_hash)) > 0
            THEN 'full'
            ELSE 'guest'
        END
        """
    )
    op.alter_column("users", "account_type", nullable=False)
    op.create_index("ix_users_account_type_status", "users", ["account_type", "status"], unique=False)
