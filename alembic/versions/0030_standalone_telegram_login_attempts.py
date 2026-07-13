# ruff: noqa: E501,I001,TC003
"""standalone Telegram login attempts"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Decouple login attempts from persistent crawler sessions."""

    op.add_column(
        "telegram_session_login_attempts",
        sa.Column("created_by_admin_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "telegram_session_login_attempts",
        sa.Column(
            "cleanup_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "telegram_session_login_attempts",
        sa.Column("cleanup_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "telegram_session_login_attempts",
        sa.Column("cleanup_error_class", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "telegram_session_login_attempts",
        sa.Column("cleanup_error_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "telegram_session_login_attempts",
        sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.drop_constraint(
        op.f("fk_telegram_session_login_attempts_telegram_session_id_telegram_sessions"),
        "telegram_session_login_attempts",
        type_="foreignkey",
    )
    op.alter_column(
        "telegram_session_login_attempts",
        "telegram_session_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.create_foreign_key(
        op.f("fk_telegram_session_login_attempts_telegram_session_id_telegram_sessions"),
        "telegram_session_login_attempts",
        "telegram_sessions",
        ["telegram_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_telegram_session_login_attempts_created_by_admin_user_id_users"),
        "telegram_session_login_attempts",
        "users",
        ["created_by_admin_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint(
        op.f("ck_telegram_session_login_attempts_status_known"),
        "telegram_session_login_attempts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_telegram_session_login_attempts_status_known"),
        "telegram_session_login_attempts",
        "status IN ('pending', 'password_required', 'completed', 'failed', 'expired', 'cancelled')",
    )
    op.create_check_constraint(
        op.f("ck_telegram_session_login_attempts_cleanup_status_known"),
        "telegram_session_login_attempts",
        "cleanup_status IN ('pending', 'promoted', 'discarded', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_telegram_session_login_attempts_cleanup_attempts_nonnegative"),
        "telegram_session_login_attempts",
        "cleanup_attempts >= 0",
    )

    op.create_index(
        "ix_telegram_session_login_attempts_cleanup_status_expires_at",
        "telegram_session_login_attempts",
        ["cleanup_status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_session_login_attempts_created_by_created_at",
        "telegram_session_login_attempts",
        ["created_by_admin_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_telegram_sessions_account_user_id_not_null",
        "telegram_sessions",
        ["account_user_id"],
        unique=True,
        postgresql_where=sa.text("account_user_id IS NOT NULL"),
    )

    # Completed attempts have already promoted their StringSession to the
    # canonical registry. Their temporary transport fields are no longer
    # useful and should not survive the migration.
    op.execute(
        sa.text(
            """
            UPDATE telegram_session_login_attempts
            SET cleanup_status = 'promoted',
                cleanup_completed_at = COALESCE(completed_at, updated_at, now()),
                encrypted_temp_string_session = NULL,
                phone_code_hash = NULL,
                qr_url = NULL
            WHERE status = 'completed'
            """,
        ),
    )

    # Old UI flows created persistent pending_telegram_* shells before auth.
    # Expire their attempts before removing the shell. Attempts that still
    # carry encrypted temporary material remain cleanup-pending so the runtime
    # scheduler can connect and revoke an already-authorized Telegram auth key.
    op.execute(
        sa.text(
            """
            UPDATE telegram_session_login_attempts AS attempt
            SET status = CASE
                    WHEN attempt.status IN ('pending', 'password_required') THEN 'expired'
                    ELSE attempt.status
                END,
                error_class = CASE
                    WHEN attempt.status IN ('pending', 'password_required')
                        THEN COALESCE(attempt.error_class, 'TelegramLoginAttemptMigrated')
                    ELSE attempt.error_class
                END,
                error_text = CASE
                    WHEN attempt.status IN ('pending', 'password_required')
                        THEN COALESCE(attempt.error_text, 'Legacy provisional Telegram login attempt expired during migration.')
                    ELSE attempt.error_text
                END,
                completed_at = COALESCE(attempt.completed_at, now()),
                cleanup_status = CASE
                    WHEN attempt.encrypted_temp_string_session IS NULL THEN 'discarded'
                    ELSE 'pending'
                END,
                cleanup_completed_at = CASE
                    WHEN attempt.encrypted_temp_string_session IS NULL THEN now()
                    ELSE NULL
                END,
                phone_code_hash = NULL,
                qr_url = NULL
            FROM telegram_sessions AS telegram_session
            WHERE attempt.telegram_session_id = telegram_session.id
              AND left(telegram_session.name, 17) = 'pending_telegram_'
              AND telegram_session.encrypted_string_session IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM source_channels AS source_channel
                  WHERE source_channel.telegram_session_id = telegram_session.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM telegram_session_login_attempts AS completed_attempt
                  WHERE completed_attempt.telegram_session_id = telegram_session.id
                    AND completed_attempt.status = 'completed'
              )
            """,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE telegram_session_login_attempts AS attempt
            SET telegram_session_id = NULL
            FROM telegram_sessions AS telegram_session
            WHERE attempt.telegram_session_id = telegram_session.id
              AND left(telegram_session.name, 17) = 'pending_telegram_'
              AND telegram_session.encrypted_string_session IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM source_channels AS source_channel
                  WHERE source_channel.telegram_session_id = telegram_session.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM telegram_session_login_attempts AS completed_attempt
                  WHERE completed_attempt.telegram_session_id = telegram_session.id
                    AND completed_attempt.status = 'completed'
              )
            """,
        ),
    )
    op.execute(
        sa.text(
            """
            DELETE FROM telegram_sessions AS telegram_session
            WHERE left(telegram_session.name, 17) = 'pending_telegram_'
              AND telegram_session.encrypted_string_session IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM source_channels AS source_channel
                  WHERE source_channel.telegram_session_id = telegram_session.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM telegram_session_login_attempts AS completed_attempt
                  WHERE completed_attempt.telegram_session_id = telegram_session.id
                    AND completed_attempt.status = 'completed'
              )
            """,
        ),
    )


def downgrade() -> None:
    """Restore session-owned login attempts."""

    op.execute(
        sa.text(
            "DELETE FROM telegram_session_login_attempts WHERE telegram_session_id IS NULL",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE telegram_session_login_attempts SET status = 'failed' WHERE status = 'cancelled'",
        ),
    )

    op.drop_index(
        "uq_telegram_sessions_account_user_id_not_null",
        table_name="telegram_sessions",
        postgresql_where=sa.text("account_user_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_telegram_session_login_attempts_created_by_created_at",
        table_name="telegram_session_login_attempts",
    )
    op.drop_index(
        "ix_telegram_session_login_attempts_cleanup_status_expires_at",
        table_name="telegram_session_login_attempts",
    )

    op.drop_constraint(
        op.f("ck_telegram_session_login_attempts_cleanup_attempts_nonnegative"),
        "telegram_session_login_attempts",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_telegram_session_login_attempts_cleanup_status_known"),
        "telegram_session_login_attempts",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_telegram_session_login_attempts_status_known"),
        "telegram_session_login_attempts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_telegram_session_login_attempts_status_known"),
        "telegram_session_login_attempts",
        "status IN ('pending', 'password_required', 'completed', 'failed', 'expired')",
    )

    op.drop_constraint(
        op.f("fk_telegram_session_login_attempts_created_by_admin_user_id_users"),
        "telegram_session_login_attempts",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_telegram_session_login_attempts_telegram_session_id_telegram_sessions"),
        "telegram_session_login_attempts",
        type_="foreignkey",
    )
    op.alter_column(
        "telegram_session_login_attempts",
        "telegram_session_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.create_foreign_key(
        op.f("fk_telegram_session_login_attempts_telegram_session_id_telegram_sessions"),
        "telegram_session_login_attempts",
        "telegram_sessions",
        ["telegram_session_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_column("telegram_session_login_attempts", "cleanup_completed_at")
    op.drop_column("telegram_session_login_attempts", "cleanup_error_text")
    op.drop_column("telegram_session_login_attempts", "cleanup_error_class")
    op.drop_column("telegram_session_login_attempts", "cleanup_attempts")
    op.drop_column("telegram_session_login_attempts", "cleanup_status")
    op.drop_column("telegram_session_login_attempts", "created_by_admin_user_id")
