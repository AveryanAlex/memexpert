# ruff: noqa: E501,I001,TC003
"""telegram session login attempts"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create short-lived Telegram login attempt persistence."""

    op.create_table(
        "telegram_session_login_attempts",
        sa.Column("telegram_session_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("encrypted_temp_string_session", sa.Text(), nullable=True),
        sa.Column("phone_number_hint", sa.String(length=64), nullable=True),
        sa.Column("phone_code_hash", sa.String(length=255), nullable=True),
        sa.Column("qr_url", sa.Text(), nullable=True),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "method IN ('qr', 'phone')",
            name=op.f("ck_telegram_session_login_attempts_method_known"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'password_required', 'completed', 'failed', 'expired')",
            name=op.f("ck_telegram_session_login_attempts_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["telegram_session_id"],
            ["telegram_sessions.id"],
            name=op.f("fk_telegram_session_login_attempts_telegram_session_id_telegram_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_session_login_attempts")),
    )
    op.create_index(
        "ix_telegram_session_login_attempts_session_created_at",
        "telegram_session_login_attempts",
        ["telegram_session_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_session_login_attempts_expires_at",
        "telegram_session_login_attempts",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop short-lived Telegram login attempt persistence."""

    op.drop_index("ix_telegram_session_login_attempts_expires_at", table_name="telegram_session_login_attempts")
    op.drop_index(
        "ix_telegram_session_login_attempts_session_created_at",
        table_name="telegram_session_login_attempts",
    )
    op.drop_table("telegram_session_login_attempts")
