# ruff: noqa: E501,I001,TC003
"""operational recovery control plane and durable attempt history"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


RECOVERY_JOB_STATUS = _enum(
    "recoveryjobstatus",
    "preview",
    "queued",
    "running",
    "completed",
    "completed_with_failures",
    "cancelled",
    "expired",
)
RECOVERY_ITEM_STATUS = _enum(
    "recoveryjobitemstatus",
    "queued",
    "waiting_capacity",
    "dispatched",
    "succeeded",
    "failed",
    "skipped_stale",
    "cancelled",
)
RECOVERY_CAPABILITY = _enum(
    "recoverycapability",
    "resume_backfill",
    "replay_source_post",
    "reinspect_ingest",
    "retry_stage",
    "resync_target",
    "rebuild_outbox",
    "recover_dead_letter",
    "archive_dead_letter",
)
RECOVERY_WORK_KIND = _enum(
    "recoveryworkkind",
    "backfill",
    "source_post",
    "ingest_request",
    "pipeline_stage",
    "sync_target",
    "outbox",
    "dead_letter",
)
ATTEMPT_OUTCOME = _enum(
    "pipelineattemptoutcome",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "skipped",
)
DEAD_LETTER_STATUS = _enum(
    "recoverydeadletterstatus",
    "unresolved",
    "recovery_queued",
    "resolved",
    "archived",
)
CAPACITY_STATUS = _enum("pipelinecapacitystatus", "open", "closed")
CIRCUIT_STATUS = _enum("dependencycircuitstatus", "closed", "open", "half_open")
PIPELINE_STAGE = _enum(
    "contentpipelinestage",
    "ingest",
    "transcode",
    "ocr",
    "embed",
    "classify",
    "sync_qdrant",
    "sync_meili",
)


def upgrade() -> None:
    """Add durable recovery, attempts, DLQ, capacity, circuit, and runtime state."""

    op.add_column(
        "source_channel_posts", sa.Column("is_retryable", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("source_channel_posts", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("source_channel_posts", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("source_channel_posts", sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_index("uq_source_channel_backfill_jobs_one_active_per_channel", table_name="source_channel_backfill_jobs")
    op.execute("ALTER TABLE source_channel_backfill_jobs DROP CONSTRAINT IF EXISTS sourcechannelbackfilljobstatus")
    op.execute(
        "ALTER TABLE source_channel_backfill_jobs DROP CONSTRAINT IF EXISTS ck_source_channel_backfill_jobs_sourcechannelbackfilljobstatus"
    )
    op.alter_column(
        "source_channel_backfill_jobs",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=23),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'::character varying"),
    )
    op.create_check_constraint(
        "sourcechannelbackfilljobstatus",
        "source_channel_backfill_jobs",
        "status IN ('queued','running','waiting_retry','waiting_capacity','completed','completed_with_failures','failed','cancelled')",
    )
    op.add_column(
        "source_channel_backfill_jobs",
        sa.Column("quarantined_message_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "source_channel_backfill_jobs", sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("source_channel_backfill_jobs", sa.Column("last_error_code", sa.String(length=128), nullable=True))
    op.add_column("source_channel_backfill_jobs", sa.Column("last_error_class", sa.String(length=128), nullable=True))
    op.add_column("source_channel_backfill_jobs", sa.Column("failed_post_id", sa.String(length=255), nullable=True))
    op.add_column(
        "source_channel_backfill_jobs",
        sa.Column("is_retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "source_channel_backfill_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "source_channel_backfill_jobs", sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "source_channel_backfill_jobs", sa.Column("lease_generation", sa.Integer(), server_default="0", nullable=False)
    )
    op.create_check_constraint(
        "source_channel_backfill_jobs_quarantined_count_bounded",
        "source_channel_backfill_jobs",
        "quarantined_message_count >= 0 AND quarantined_message_count <= scanned_message_count",
    )
    op.create_check_constraint(
        "source_channel_backfill_jobs_attempt_count_non_negative",
        "source_channel_backfill_jobs",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "source_channel_backfill_jobs_lease_generation_non_negative",
        "source_channel_backfill_jobs",
        "lease_generation >= 0",
    )
    op.create_index(
        "uq_source_channel_backfill_jobs_one_active_per_channel",
        "source_channel_backfill_jobs",
        ["source_channel_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting_retry', 'waiting_capacity')"),
    )

    # Pre-control-plane failures have no explicit retryability classification.
    # Keep them operator-driven, but expose them as eligible for an admin-
    # scheduled replay instead of silently classifying them as blocked.
    op.execute(
        sa.text(
            """
            UPDATE source_channel_posts
            SET is_retryable = true,
                last_attempt_at = updated_at
            WHERE status = 'failed'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE source_channel_backfill_jobs
            SET is_retryable = (status = 'failed'),
                attempt_count = CASE WHEN status = 'queued' THEN 0 ELSE 1 END,
                last_progress_at = COALESCE(completed_at, locked_at, started_at, updated_at)
            """
        )
    )

    op.create_table(
        "recovery_jobs",
        sa.Column("requested_by_admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("status", RECOVERY_JOB_STATUS, server_default="preview", nullable=False),
        sa.Column("action", RECOVERY_CAPABILITY, nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "selection", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("total_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "total_count >= 0",
            name=op.f("ck_recovery_jobs_recovery_jobs_total_count_non_negative"),
        ),
        sa.CheckConstraint(
            "completed_count >= 0",
            name=op.f("ck_recovery_jobs_recovery_jobs_completed_count_non_negative"),
        ),
        sa.CheckConstraint(
            "failed_count >= 0",
            name=op.f("ck_recovery_jobs_recovery_jobs_failed_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(["requested_by_admin_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("requested_by_admin_user_id", "request_id", name="uq_recovery_jobs_admin_request_id"),
    )
    op.create_index("ix_recovery_jobs_status_created_at", "recovery_jobs", ["status", "created_at"])
    op.create_index("ix_recovery_jobs_admin_created_at", "recovery_jobs", ["requested_by_admin_user_id", "created_at"])

    op.create_table(
        "recovery_job_items",
        sa.Column("recovery_job_id", sa.Uuid(), nullable=False),
        sa.Column("work_kind", RECOVERY_WORK_KIND, nullable=False),
        sa.Column("work_id", sa.String(length=255), nullable=False),
        sa.Column("action", RECOVERY_CAPABILITY, nullable=False),
        sa.Column("expected_version", sa.String(length=255), nullable=False),
        sa.Column("status", RECOVERY_ITEM_STATUS, server_default="queued", nullable=False),
        sa.Column("dispatch_event_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_version", sa.String(length=255), nullable=True),
        sa.Column("normalized_reason", sa.String(length=128), nullable=True),
        sa.Column("safe_error_text", sa.Text(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["recovery_job_id"], ["recovery_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recovery_job_id", "work_kind", "work_id", name="uq_recovery_job_items_target"),
    )
    op.create_index("ix_recovery_job_items_job_status", "recovery_job_items", ["recovery_job_id", "status"])
    op.create_index("ix_recovery_job_items_status_created_at", "recovery_job_items", ["status", "created_at"])
    op.create_index("ix_recovery_job_items_dispatch_event_id", "recovery_job_items", ["dispatch_event_id"])

    op.create_table(
        "operational_audit_logs",
        sa.Column("admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=255), nullable=False),
        sa.Column(
            "previous_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "new_values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("note", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operational_audit_logs_admin_created", "operational_audit_logs", ["admin_user_id", "created_at"]
    )
    op.create_index(
        "ix_operational_audit_logs_target_created", "operational_audit_logs", ["target_kind", "target_id", "created_at"]
    )
    op.create_index("ix_operational_audit_logs_action_created", "operational_audit_logs", ["action", "created_at"])

    op.create_table(
        "pipeline_stage_attempts",
        sa.Column("meme_file_id", sa.Uuid(), nullable=False),
        sa.Column("stage", PIPELINE_STAGE, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", ATTEMPT_OUTCOME, server_default="processing", nullable=False),
        sa.Column("recovery_item_id", sa.Uuid(), nullable=True),
        sa.Column("worker_role", sa.String(length=32), nullable=True),
        sa.Column("worker_instance_id", sa.String(length=128), nullable=True),
        sa.Column("normalized_reason", sa.String(length=128), nullable=True),
        sa.Column("safe_error_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_pipeline_stage_attempts_pipeline_stage_attempts_attempt_positive"),
        ),
        sa.ForeignKeyConstraint(["meme_file_id"], ["meme_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recovery_item_id"], ["recovery_job_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "meme_file_id",
            "stage",
            "event_id",
            "attempt_number",
            name="uq_pipeline_stage_attempts_file_stage_event_attempt",
        ),
    )
    op.create_index(
        "ix_pipeline_stage_attempts_file_stage_started",
        "pipeline_stage_attempts",
        ["meme_file_id", "stage", "started_at"],
    )
    op.create_index(
        "ix_pipeline_stage_attempts_reason_started", "pipeline_stage_attempts", ["normalized_reason", "started_at"]
    )
    op.create_index("ix_pipeline_stage_attempts_recovery_item", "pipeline_stage_attempts", ["recovery_item_id"])

    op.create_table(
        "source_channel_backfill_attempts",
        sa.Column("backfill_job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("telegram_session_id", sa.Uuid(), nullable=True),
        sa.Column("recovery_item_id", sa.Uuid(), nullable=True),
        sa.Column("worker_instance_id", sa.String(length=128), nullable=True),
        sa.Column("normalized_reason", sa.String(length=128), nullable=True),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("safe_error_text", sa.Text(), nullable=True),
        sa.Column("failed_post_id", sa.String(length=255), nullable=True),
        sa.Column("is_retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["backfill_job_id"], ["source_channel_backfill_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["telegram_session_id"], ["telegram_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recovery_item_id"], ["recovery_job_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("backfill_job_id", "attempt_number", name="uq_backfill_attempts_job_attempt"),
    )
    op.create_index(
        "ix_backfill_attempts_job_started", "source_channel_backfill_attempts", ["backfill_job_id", "started_at"]
    )

    op.create_table(
        "pipeline_dead_letters",
        sa.Column("deduplication_key", sa.String(length=128), nullable=False),
        sa.Column("broker_message_id", sa.String(length=255), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=True),
        sa.Column("work_kind", RECOVERY_WORK_KIND, nullable=True),
        sa.Column("work_id", sa.String(length=255), nullable=True),
        sa.Column("normalized_reason", sa.String(length=128), nullable=False),
        sa.Column("death_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "safe_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "safe_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", DEAD_LETTER_STATUS, server_default="unresolved", nullable=False),
        sa.Column("recovery_item_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["recovery_item_id"], ["recovery_job_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key", name="uq_pipeline_dead_letters_deduplication_key"),
    )
    op.create_index("ix_pipeline_dead_letters_status_created", "pipeline_dead_letters", ["status", "created_at"])
    op.create_index("ix_pipeline_dead_letters_work", "pipeline_dead_letters", ["work_kind", "work_id"])
    op.create_index("ix_pipeline_dead_letters_message_id", "pipeline_dead_letters", ["broker_message_id"])

    op.create_table(
        "pipeline_capacity_states",
        sa.Column("stage", PIPELINE_STAGE, nullable=False),
        sa.Column("status", CAPACITY_STATUS, server_default="open", nullable=False),
        sa.Column("pending_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("oldest_pending_age_seconds", sa.Float(), server_default="0", nullable=False),
        sa.Column("throughput_per_minute_15m", sa.Float(), server_default="0", nullable=False),
        sa.Column("drain_eta_seconds", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stage", name="uq_pipeline_capacity_states_stage"),
    )

    op.create_table(
        "dependency_circuit_states",
        sa.Column("dependency", sa.String(length=64), nullable=False),
        sa.Column("status", CIRCUIT_STATUS, server_default="closed", nullable=False),
        sa.Column("error_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_owner", sa.String(length=128), nullable=True),
        sa.Column("probe_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dependency", name="uq_dependency_circuit_states_dependency"),
    )

    op.create_table(
        "runtime_heartbeats",
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=64), server_default="", nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("build_revision", sa.String(length=128), nullable=True),
        sa.Column("ready", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="starting", nullable=False),
        sa.Column("current_operation", sa.String(length=128), nullable=True),
        sa.Column("operation_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_name", "role", "instance_id", name="uq_runtime_heartbeats_instance"),
    )
    op.create_index(
        "ix_runtime_heartbeats_service_role_updated", "runtime_heartbeats", ["service_name", "role", "updated_at"]
    )


def downgrade() -> None:
    """Remove the operational control-plane schema."""

    op.drop_index("ix_runtime_heartbeats_service_role_updated", table_name="runtime_heartbeats")
    op.drop_table("runtime_heartbeats")
    op.drop_table("dependency_circuit_states")
    op.drop_table("pipeline_capacity_states")
    op.drop_index("ix_pipeline_dead_letters_message_id", table_name="pipeline_dead_letters")
    op.drop_index("ix_pipeline_dead_letters_work", table_name="pipeline_dead_letters")
    op.drop_index("ix_pipeline_dead_letters_status_created", table_name="pipeline_dead_letters")
    op.drop_table("pipeline_dead_letters")
    op.drop_index("ix_backfill_attempts_job_started", table_name="source_channel_backfill_attempts")
    op.drop_table("source_channel_backfill_attempts")
    op.drop_index("ix_pipeline_stage_attempts_recovery_item", table_name="pipeline_stage_attempts")
    op.drop_index("ix_pipeline_stage_attempts_reason_started", table_name="pipeline_stage_attempts")
    op.drop_index("ix_pipeline_stage_attempts_file_stage_started", table_name="pipeline_stage_attempts")
    op.drop_table("pipeline_stage_attempts")
    op.drop_index("ix_operational_audit_logs_action_created", table_name="operational_audit_logs")
    op.drop_index("ix_operational_audit_logs_target_created", table_name="operational_audit_logs")
    op.drop_index("ix_operational_audit_logs_admin_created", table_name="operational_audit_logs")
    op.drop_table("operational_audit_logs")
    op.drop_index("ix_recovery_job_items_dispatch_event_id", table_name="recovery_job_items")
    op.drop_index("ix_recovery_job_items_status_created_at", table_name="recovery_job_items")
    op.drop_index("ix_recovery_job_items_job_status", table_name="recovery_job_items")
    op.drop_table("recovery_job_items")
    op.drop_index("ix_recovery_jobs_admin_created_at", table_name="recovery_jobs")
    op.drop_index("ix_recovery_jobs_status_created_at", table_name="recovery_jobs")
    op.drop_table("recovery_jobs")

    op.drop_index("uq_source_channel_backfill_jobs_one_active_per_channel", table_name="source_channel_backfill_jobs")
    op.execute(
        sa.text(
            """
            UPDATE source_channel_backfill_jobs
            SET status = CASE status
                WHEN 'waiting_retry' THEN 'queued'
                WHEN 'waiting_capacity' THEN 'queued'
                WHEN 'completed_with_failures' THEN 'completed'
                WHEN 'cancelled' THEN 'failed'
                ELSE status
            END
            """
        )
    )
    op.drop_constraint(
        "source_channel_backfill_jobs_lease_generation_non_negative", "source_channel_backfill_jobs", type_="check"
    )
    op.drop_constraint(
        "source_channel_backfill_jobs_attempt_count_non_negative", "source_channel_backfill_jobs", type_="check"
    )
    op.drop_constraint(
        "source_channel_backfill_jobs_quarantined_count_bounded", "source_channel_backfill_jobs", type_="check"
    )
    for column in (
        "lease_generation",
        "last_progress_at",
        "next_attempt_at",
        "is_retryable",
        "failed_post_id",
        "last_error_class",
        "last_error_code",
        "attempt_count",
        "quarantined_message_count",
    ):
        op.drop_column("source_channel_backfill_jobs", column)
    op.drop_constraint("sourcechannelbackfilljobstatus", "source_channel_backfill_jobs", type_="check")
    op.alter_column(
        "source_channel_backfill_jobs",
        "status",
        existing_type=sa.String(length=23),
        type_=sa.String(length=9),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'::character varying"),
    )
    op.create_check_constraint(
        "sourcechannelbackfilljobstatus",
        "source_channel_backfill_jobs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_index(
        "uq_source_channel_backfill_jobs_one_active_per_channel",
        "source_channel_backfill_jobs",
        ["source_channel_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.drop_column("source_channel_posts", "quarantined_at")
    op.drop_column("source_channel_posts", "last_attempt_at")
    op.drop_column("source_channel_posts", "next_attempt_at")
    op.drop_column("source_channel_posts", "is_retryable")
