# ruff: noqa: E501,I001,TC003
"""telegram link codes"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "telegram_link_codes",
        sa.Column("guest_user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_link_codes")),
    )
    op.create_index(
        "ix_telegram_link_codes_guest_user_id_expires_at",
        "telegram_link_codes",
        ["guest_user_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_link_codes_redeemed_by_telegram_id_redeemed_at",
        "telegram_link_codes",
        ["redeemed_by_telegram_id", "redeemed_at"],
        unique=False,
    )
    op.create_index(
        "uq_telegram_link_codes_code_hash",
        "telegram_link_codes",
        ["code_hash"],
        unique=True,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("uq_telegram_link_codes_code_hash", table_name="telegram_link_codes")
    op.drop_index(
        "ix_telegram_link_codes_redeemed_by_telegram_id_redeemed_at",
        table_name="telegram_link_codes",
    )
    op.drop_index("ix_telegram_link_codes_guest_user_id_expires_at", table_name="telegram_link_codes")
    op.drop_table("telegram_link_codes")
