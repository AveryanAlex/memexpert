# ruff: noqa: E501
"""add replay orchestration and immutable media generations

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-20
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


MEDIA_GENERATION_STATUS = _enum(
    "mediagenerationstatus",
    "generating",
    "verified",
    "uploaded",
    "active",
    "superseded",
    "failed",
    "stale",
)
MEDIA_GENERATION_CLEANUP_STATUS = _enum(
    "mediagenerationcleanupstatus",
    "not_eligible",
    "pending",
    "deleted",
    "retained_referenced",
    "failed",
)


def _drop_check(table_name: str, constraint_name: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{table_name}" DROP CONSTRAINT IF EXISTS "{constraint_name}"'))


def _replace_recovery_checks(*, expanded: bool) -> None:
    for table_name, enum_name in (
        ("recovery_jobs", "recoveryjobstatus"),
        ("recovery_jobs", "recoverycapability"),
        ("recovery_job_items", "recoveryjobitemstatus"),
        ("recovery_job_items", "recoverycapability"),
    ):
        _drop_check(table_name, enum_name)
        _drop_check(table_name, f"ck_{table_name}_{enum_name}")

    job_statuses = [
        "preview",
        "queued",
        "running",
        "completed",
        "completed_with_failures",
        "cancelled",
        "expired",
    ]
    item_statuses = [
        "queued",
        "waiting_capacity",
        "dispatched",
        "succeeded",
        "failed",
        "skipped_stale",
        "cancelled",
    ]
    capabilities = [
        "resume_backfill",
        "replay_source_post",
        "reinspect_ingest",
        "retry_stage",
        "resync_target",
        "rebuild_outbox",
        "recover_dead_letter",
        "archive_dead_letter",
    ]
    if expanded:
        job_statuses[0:0] = ["preparing"]
        job_statuses.insert(4, "cancelling")
        item_statuses.insert(1, "waiting_dependency")
        item_statuses.insert(-1, "skipped_dependency")
        capabilities[4:4] = ["replay_stage", "regenerate_derivatives"]

    def quoted(values: list[str]) -> str:
        return ",".join(f"'{value}'" for value in values)

    op.create_check_constraint(
        "recoveryjobstatus",
        "recovery_jobs",
        f"status IN ({quoted(job_statuses)})",
    )
    op.create_check_constraint(
        "recoveryjobitemstatus",
        "recovery_job_items",
        f"status IN ({quoted(item_statuses)})",
    )
    for table_name in ("recovery_jobs", "recovery_job_items"):
        op.create_check_constraint(
            "recoverycapability",
            table_name,
            f"action IN ({quoted(capabilities)})",
        )


def _backfill_legacy_recovery_state() -> None:
    """Fence recovery rows created before replay orchestration existed.

    Revisions 0034-0041 could leave recovery work queued or dispatched without
    the reservation, dependency, and retry-budget fields introduced here.  It
    is not safe to infer ownership for those deliveries after the fact.  Valid
    previews, on the other hand, have not executed yet and can be retained once
    their exact materialized selection and stage metadata are made explicit.
    """

    op.execute(
        sa.text(
            """
            UPDATE recovery_jobs
            SET assigned_admin_user_id = requested_by_admin_user_id,
                scope = 'stage_only',
                retry_limit = 3,
                selection_snapshot_at = COALESCE(selection_snapshot_at, created_at),
                materialization_cursor = NULL,
                materialization_lease_owner = NULL,
                materialization_lease_at = NULL,
                materialization_lease_generation = 0,
                materialization_completed_at = COALESCE(materialization_completed_at, created_at),
                exclusions_by_reason = '{}'::jsonb,
                excluded_count = 0,
                preparation_scanned_count = 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET parent_item_id = NULL,
                source_item_id = NULL,
                meme_file_id = NULL,
                stage = NULL,
                is_root = true,
                retry_limit = job.retry_limit,
                attempt_budget_start = NULL,
                retryable_failures_consumed = 0,
                preserve_ready = false,
                suppress_fanout = false,
                terminal_override_acknowledged = false,
                previous_stage_state = '{}'::jsonb,
                reservation_active = false,
                updated_at = CURRENT_TIMESTAMP
            FROM recovery_jobs AS job
            WHERE job.id = item.recovery_job_id
            """
        )
    )

    # Legacy work ids for pipeline and sync targets were canonical row UUIDs.
    # Compare their text forms so malformed historical ids simply remain
    # unhydrated instead of aborting the migration with a UUID cast failure.
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET meme_file_id = journal.meme_file_id,
                stage = journal.stage,
                preserve_ready = (meme_file.status = 'ready'),
                suppress_fanout = true
            FROM pipeline_stage_journal AS journal
            JOIN meme_files AS meme_file ON meme_file.id = journal.meme_file_id
            WHERE item.work_kind = 'pipeline_stage'
              AND item.work_id = journal.id::text
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET meme_file_id = snapshot.meme_file_id,
                stage = CASE snapshot.sync_target
                    WHEN 'qdrant' THEN 'sync_qdrant'
                    WHEN 'meilisearch' THEN 'sync_meili'
                END,
                preserve_ready = (meme_file.status = 'ready'),
                suppress_fanout = true
            FROM meme_file_sync_target_snapshots AS snapshot
            JOIN meme_files AS meme_file ON meme_file.id = snapshot.meme_file_id
            WHERE item.work_kind = 'sync_target'
              AND item.work_id = snapshot.id::text
            """
        )
    )

    # Stage-only replay of moving-media Transcode now means derivative-only,
    # atomic regeneration.  Align retained previews with the execution topology
    # that schedule-time revalidation will rebuild.
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET action = 'regenerate_derivatives'
            FROM recovery_jobs AS job, meme_files AS meme_file
            WHERE job.id = item.recovery_job_id
              AND meme_file.id = item.meme_file_id
              AND job.status = 'preview'
              AND item.action = 'retry_stage'
              AND item.stage = 'transcode'
              AND lower(COALESCE(meme_file.mime_type, '')) IN (
                  'image/gif', 'video/mp4', 'video/quicktime', 'video/webm'
              )
            """
        )
    )

    # A preview with evidence of dispatch is not a preview we can safely make
    # schedulable.  Expire it and terminalize any non-terminal children.
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET status = 'cancelled',
                normalized_reason = 'legacy_preview_incompatible',
                safe_error_text =
                    'Legacy preview contained non-preview work and was safely terminalized during upgrade.',
                dispatch_event_id = NULL,
                canonical_version = NULL,
                dispatched_at = NULL,
                finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                reservation_active = false
            WHERE item.recovery_job_id IN (
                SELECT DISTINCT job.id
                FROM recovery_jobs AS job
                JOIN recovery_job_items AS candidate
                  ON candidate.recovery_job_id = job.id
                WHERE job.status = 'preview'
                  AND candidate.status <> 'queued'
            )
              AND item.status IN ('queued', 'waiting_dependency', 'waiting_capacity', 'dispatched')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_jobs AS job
            SET status = 'expired',
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
            WHERE job.status = 'preview'
              AND EXISTS (
                  SELECT 1
                  FROM recovery_job_items AS item
                  WHERE item.recovery_job_id = job.id
                    AND item.status <> 'queued'
              )
            """
        )
    )

    # Rebuild retained preview selectors from the durable items rather than
    # trusting the older JSON shape.  This keeps the reviewed versions and the
    # rows revalidated at scheduling time exactly aligned.
    op.execute(
        sa.text(
            """
            UPDATE recovery_jobs AS job
            SET selection = jsonb_build_object(
                'selector', jsonb_build_object(
                    'type', 'explicit',
                    'items', COALESCE(
                        (
                            SELECT jsonb_agg(
                                jsonb_build_object(
                                    'kind', item.work_kind,
                                    'id', item.work_id,
                                    'version', item.expected_version
                                )
                                ORDER BY item.created_at, item.id
                            )
                            FROM recovery_job_items AS item
                            WHERE item.recovery_job_id = job.id
                              AND item.is_root
                        ),
                        '[]'::jsonb
                    )
                ),
                'scope', 'stage_only',
                'retry_limit', 3,
                'acknowledgements', '[]'::jsonb
            )
            WHERE job.status = 'preview'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET canonical_version = NULL,
                dispatch_event_id = NULL,
                dispatched_at = NULL,
                finished_at = NULL,
                normalized_reason = NULL,
                safe_error_text = NULL,
                reservation_active = false
            FROM recovery_jobs AS job
            WHERE job.id = item.recovery_job_id
              AND job.status = 'preview'
            """
        )
    )

    # Queued/running legacy deliveries predate reservation and attempt fencing.
    # Never admit them into the new runtime as if they were owned replay steps.
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items AS item
            SET status = 'cancelled',
                normalized_reason = 'legacy_recovery_terminalized',
                safe_error_text =
                    'Legacy recovery delivery was safely terminalized during upgrade; create a fresh job to retry it.',
                dispatch_event_id = NULL,
                canonical_version = NULL,
                dispatched_at = NULL,
                finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                reservation_active = false
            FROM recovery_jobs AS job
            WHERE job.id = item.recovery_job_id
              AND job.status IN ('queued', 'running')
              AND item.status IN ('queued', 'waiting_dependency', 'waiting_capacity', 'dispatched')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_jobs
            SET status = 'cancelled',
                selection = selection || jsonb_build_object(
                    'scope', 'stage_only',
                    'retry_limit', 3,
                    'acknowledgements', '[]'::jsonb,
                    'migration_terminalized', true
                ),
                cancelled_at = COALESCE(cancelled_at, CURRENT_TIMESTAMP),
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
            WHERE status IN ('queued', 'running')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_jobs
            SET selection = selection || jsonb_build_object(
                'scope', 'stage_only',
                'retry_limit', 3,
                'acknowledgements', COALESCE(selection->'acknowledgements', '[]'::jsonb)
            )
            WHERE status <> 'preview'
            """
        )
    )

    # Treat the durable item rows as authoritative for every legacy count.
    op.execute(
        sa.text(
            """
            WITH item_counts AS (
                SELECT job.id AS recovery_job_id,
                       count(item.id)::integer AS item_count,
                       count(item.id) FILTER (WHERE item.is_root)::integer AS root_count,
                       count(item.id) FILTER (WHERE item.status = 'queued')::integer AS queued_count,
                       count(item.id) FILTER (
                           WHERE item.status IN ('waiting_dependency', 'waiting_capacity')
                       )::integer AS waiting_count,
                       count(item.id) FILTER (WHERE item.status = 'dispatched')::integer AS dispatched_count,
                       count(item.id) FILTER (WHERE item.status = 'succeeded')::integer AS succeeded_count,
                       count(item.id) FILTER (WHERE item.status = 'failed')::integer AS failed_count,
                       count(item.id) FILTER (WHERE item.status = 'skipped_stale')::integer AS stale_count,
                       count(item.id) FILTER (WHERE item.status = 'skipped_dependency')::integer AS skipped_count,
                       count(item.id) FILTER (WHERE item.status = 'cancelled')::integer AS cancelled_count,
                       count(item.id) FILTER (
                           WHERE item.status IN (
                               'succeeded', 'failed', 'skipped_stale', 'skipped_dependency', 'cancelled'
                           )
                       )::integer AS completed_count
                FROM recovery_jobs AS job
                LEFT JOIN recovery_job_items AS item ON item.recovery_job_id = job.id
                GROUP BY job.id
            )
            UPDATE recovery_jobs AS job
            SET total_count = counts.item_count,
                selected_root_count = counts.root_count,
                expanded_execution_count = counts.item_count,
                completed_count = counts.completed_count,
                failed_count = counts.failed_count,
                queued_count = counts.queued_count,
                waiting_count = counts.waiting_count,
                dispatched_count = counts.dispatched_count,
                succeeded_count = counts.succeeded_count,
                stale_count = counts.stale_count,
                skipped_count = counts.skipped_count,
                cancelled_count = counts.cancelled_count,
                updated_at = CURRENT_TIMESTAMP
            FROM item_counts AS counts
            WHERE counts.recovery_job_id = job.id
            """
        )
    )


def upgrade() -> None:
    """Add exact replay jobs, generation-safe media activation, and cleanup state."""

    _replace_recovery_checks(expanded=True)
    op.alter_column(
        "recovery_jobs",
        "action",
        existing_type=sa.String(length=19),
        type_=sa.String(length=22),
        existing_nullable=False,
    )
    op.alter_column(
        "recovery_job_items",
        "action",
        existing_type=sa.String(length=19),
        type_=sa.String(length=22),
        existing_nullable=False,
    )
    op.alter_column(
        "recovery_job_items",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=18),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'::character varying"),
    )

    op.add_column("recovery_jobs", sa.Column("assigned_admin_user_id", sa.Uuid(), nullable=True))
    op.add_column("recovery_jobs", sa.Column("source_recovery_job_id", sa.Uuid(), nullable=True))
    op.add_column("recovery_jobs", sa.Column("scope", sa.String(length=20), nullable=True))
    op.add_column("recovery_jobs", sa.Column("retry_limit", sa.Integer(), server_default="3", nullable=False))
    for column_name in (
        "selected_root_count",
        "expanded_execution_count",
        "preparation_scanned_count",
        "excluded_count",
        "queued_count",
        "waiting_count",
        "dispatched_count",
        "succeeded_count",
        "stale_count",
        "skipped_count",
        "cancelled_count",
    ):
        op.add_column(
            "recovery_jobs",
            sa.Column(column_name, sa.Integer(), server_default="0", nullable=False),
        )
        op.create_check_constraint(
            f"recovery_jobs_{column_name}_non_negative",
            "recovery_jobs",
            f"{column_name} >= 0",
        )
    op.add_column(
        "recovery_jobs",
        sa.Column(
            "exclusions_by_reason",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("recovery_jobs", sa.Column("selection_snapshot_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("recovery_jobs", sa.Column("materialization_cursor", sa.String(length=512), nullable=True))
    op.add_column("recovery_jobs", sa.Column("materialization_lease_owner", sa.String(length=128), nullable=True))
    op.add_column("recovery_jobs", sa.Column("materialization_lease_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "recovery_jobs",
        sa.Column("materialization_lease_generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "recovery_jobs",
        sa.Column("materialization_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_recovery_jobs_assigned_admin_user_id_users",
        "recovery_jobs",
        "users",
        ["assigned_admin_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_recovery_jobs_source_recovery_job_id_recovery_jobs",
        "recovery_jobs",
        "recovery_jobs",
        ["source_recovery_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint("recoveryreplayscope", "recovery_jobs", "scope IN ('stage_only','stage_and_dependents')")
    op.create_check_constraint("recovery_jobs_retry_limit_allowed", "recovery_jobs", "retry_limit IN (1, 3, 5)")
    op.create_check_constraint(
        "recovery_jobs_materialization_lease_generation_non_negative",
        "recovery_jobs",
        "materialization_lease_generation >= 0",
    )
    op.create_index(
        "ix_recovery_jobs_materialization_lease",
        "recovery_jobs",
        ["status", "materialization_lease_at"],
    )

    op.add_column("recovery_job_items", sa.Column("parent_item_id", sa.Uuid(), nullable=True))
    op.add_column("recovery_job_items", sa.Column("source_item_id", sa.Uuid(), nullable=True))
    op.add_column("recovery_job_items", sa.Column("meme_file_id", sa.Uuid(), nullable=True))
    op.add_column("recovery_job_items", sa.Column("stage", sa.String(length=12), nullable=True))
    op.add_column(
        "recovery_job_items",
        sa.Column("is_root", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "recovery_job_items",
        sa.Column("retry_limit", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column("recovery_job_items", sa.Column("attempt_budget_start", sa.Integer(), nullable=True))
    op.add_column(
        "recovery_job_items",
        sa.Column("retryable_failures_consumed", sa.Integer(), server_default="0", nullable=False),
    )
    for column_name in (
        "preserve_ready",
        "suppress_fanout",
        "terminal_override_acknowledged",
        "reservation_active",
    ):
        op.add_column(
            "recovery_job_items",
            sa.Column(column_name, sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    op.add_column(
        "recovery_job_items",
        sa.Column(
            "previous_stage_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_recovery_job_items_parent_item_id_recovery_job_items",
        "recovery_job_items",
        "recovery_job_items",
        ["parent_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_recovery_job_items_source_item_id_recovery_job_items",
        "recovery_job_items",
        "recovery_job_items",
        ["source_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_recovery_job_items_meme_file_id_meme_files",
        "recovery_job_items",
        "meme_files",
        ["meme_file_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "contentpipelinestage",
        "recovery_job_items",
        "stage IN ('ingest','transcode','ocr','embed','classify','sync_qdrant','sync_meili')",
    )
    op.create_check_constraint(
        "recovery_job_items_retry_limit_allowed",
        "recovery_job_items",
        "retry_limit IN (1, 3, 5)",
    )
    op.create_check_constraint(
        "recovery_job_items_retryable_failures_non_negative",
        "recovery_job_items",
        "retryable_failures_consumed >= 0",
    )
    op.create_check_constraint(
        "recovery_job_items_attempt_budget_start_positive",
        "recovery_job_items",
        "attempt_budget_start IS NULL OR attempt_budget_start >= 1",
    )
    _backfill_legacy_recovery_state()
    op.create_index(
        "ix_recovery_job_items_parent_status",
        "recovery_job_items",
        ["parent_item_id", "status"],
    )
    op.create_index(
        "ix_recovery_job_items_file_stage",
        "recovery_job_items",
        ["meme_file_id", "stage"],
    )
    op.create_index(
        "uq_recovery_job_items_active_stage_reservation",
        "recovery_job_items",
        ["meme_file_id", "stage"],
        unique=True,
        postgresql_where=sa.text("reservation_active AND stage IS NOT NULL"),
    )
    op.create_index(
        "uq_recovery_job_items_active_work_reservation",
        "recovery_job_items",
        ["work_kind", "work_id"],
        unique=True,
        postgresql_where=sa.text("reservation_active AND stage IS NULL"),
    )

    op.create_table(
        "recovery_query_snapshot_members",
        sa.Column("recovery_job_id", sa.Uuid(), nullable=False),
        sa.Column("root_key", sa.String(length=512), nullable=False),
        sa.Column("work_kind", sa.String(length=14), nullable=False),
        sa.Column("work_id", sa.String(length=255), nullable=True),
        sa.Column("meme_file_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(length=12), nullable=True),
        sa.Column("captured_version", sa.String(length=512), nullable=True),
        sa.Column("captured_context_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("is_outdated_video", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "work_kind IN ("
            "'backfill','source_post','ingest_request','pipeline_stage','sync_target','outbox','dead_letter'"
            ")",
            name=op.f("ck_recovery_query_snapshot_members_recoveryworkkind"),
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN ("
            "'ingest','transcode','ocr','embed','classify','sync_qdrant','sync_meili'"
            ")",
            name=op.f("ck_recovery_query_snapshot_members_contentpipelinestage"),
        ),
        sa.CheckConstraint(
            "work_id IS NOT NULL OR is_outdated_video",
            name=op.f("ck_recovery_query_snapshot_members_recovery_query_snapshot_members_work_identity"),
        ),
        sa.ForeignKeyConstraint(["recovery_job_id"], ["recovery_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recovery_job_id",
            "root_key",
            name="uq_recovery_query_snapshot_members_job_root",
        ),
    )
    op.create_index(
        "ix_recovery_query_snapshot_members_job_id",
        "recovery_query_snapshot_members",
        ["recovery_job_id", "id"],
    )

    op.create_table(
        "media_generations",
        sa.Column("meme_file_id", sa.Uuid(), nullable=True),
        sa.Column("recovery_item_id", sa.Uuid(), nullable=True),
        sa.Column("expected_web_video_object_key", sa.Text(), nullable=True),
        sa.Column("web_video_object_key", sa.Text(), nullable=False),
        sa.Column("preview_image_object_key", sa.Text(), nullable=False),
        sa.Column("profile", sa.String(length=128), nullable=False),
        sa.Column("retry_limit", sa.Integer(), server_default="3", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", MEDIA_GENERATION_STATUS, server_default="generating", nullable=False),
        sa.Column("previous_file_status", sa.String(length=32), nullable=True),
        sa.Column("previous_stage_status", sa.String(length=32), nullable=True),
        sa.Column(
            "previous_stage_observations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_observations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "output_observations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_width", sa.Integer(), nullable=True),
        sa.Column("source_height", sa.Integer(), nullable=True),
        sa.Column("source_frame_rate_numerator", sa.Integer(), nullable=True),
        sa.Column("source_frame_rate_denominator", sa.Integer(), nullable=True),
        sa.Column("source_duration_seconds", sa.Float(), nullable=True),
        sa.Column("source_has_audio", sa.Boolean(), nullable=True),
        sa.Column("output_width", sa.Integer(), nullable=True),
        sa.Column("output_height", sa.Integer(), nullable=True),
        sa.Column("output_frame_rate_numerator", sa.Integer(), nullable=True),
        sa.Column("output_frame_rate_denominator", sa.Integer(), nullable=True),
        sa.Column("output_duration_seconds", sa.Float(), nullable=True),
        sa.Column("output_video_bitrate", sa.Integer(), nullable=True),
        sa.Column("output_byte_size", sa.Integer(), nullable=True),
        sa.Column("output_video_codec", sa.String(length=64), nullable=True),
        sa.Column("output_audio_codec", sa.String(length=64), nullable=True),
        sa.Column("output_has_audio", sa.Boolean(), nullable=True),
        sa.Column("safe_failure_reason", sa.String(length=128), nullable=True),
        sa.Column("safe_failure_text", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_status", MEDIA_GENERATION_CLEANUP_STATUS, server_default="not_eligible", nullable=False),
        sa.Column("cleanup_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cleanup_error_text", sa.Text(), nullable=True),
        sa.Column("cleanup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("retry_limit IN (1, 3, 5)", name=op.f("ck_media_generations_media_generations_retry_limit_allowed")),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_media_generations_media_generations_attempt_count_non_negative")),
        sa.CheckConstraint(
            "cleanup_attempt_count >= 0",
            name=op.f("ck_media_generations_media_generations_cleanup_attempt_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(["meme_file_id"], ["meme_files.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recovery_item_id"], ["recovery_job_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("web_video_object_key", name="uq_media_generations_web_video_object_key"),
        sa.UniqueConstraint("preview_image_object_key", name="uq_media_generations_preview_image_object_key"),
    )
    op.create_index("ix_media_generations_file_created", "media_generations", ["meme_file_id", "created_at"])
    op.create_index("ix_media_generations_status_superseded", "media_generations", ["status", "superseded_at"])
    op.create_index("ix_media_generations_cleanup_status_created", "media_generations", ["cleanup_status", "created_at"])
    op.create_index("ix_media_generations_recovery_item", "media_generations", ["recovery_item_id"])

    op.add_column("meme_files", sa.Column("active_media_generation_id", sa.Uuid(), nullable=True))
    op.add_column("meme_files", sa.Column("source_has_audio", sa.Boolean(), nullable=True))
    op.add_column("meme_files", sa.Column("web_video_has_audio", sa.Boolean(), nullable=True))
    op.add_column("meme_files", sa.Column("web_video_profile", sa.String(length=128), nullable=True))
    op.add_column("meme_files", sa.Column("web_video_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_meme_files_active_media_generation_id",
        "meme_files",
        "media_generations",
        ["active_media_generation_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )
    op.create_index(
        "ix_meme_files_web_video_profile_verified",
        "meme_files",
        ["web_video_profile", "web_video_verified_at"],
        postgresql_where=sa.text("s3_web_video_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove replay orchestration and immutable generation state."""

    op.drop_index("ix_meme_files_web_video_profile_verified", table_name="meme_files")
    op.drop_constraint("fk_meme_files_active_media_generation_id", "meme_files", type_="foreignkey")
    for column_name in (
        "web_video_verified_at",
        "web_video_profile",
        "web_video_has_audio",
        "source_has_audio",
        "active_media_generation_id",
    ):
        op.drop_column("meme_files", column_name)

    op.drop_index("ix_media_generations_recovery_item", table_name="media_generations")
    op.drop_index("ix_media_generations_cleanup_status_created", table_name="media_generations")
    op.drop_index("ix_media_generations_status_superseded", table_name="media_generations")
    op.drop_index("ix_media_generations_file_created", table_name="media_generations")
    op.drop_table("media_generations")

    op.drop_index(
        "ix_recovery_query_snapshot_members_job_id",
        table_name="recovery_query_snapshot_members",
    )
    op.drop_table("recovery_query_snapshot_members")

    op.drop_index("uq_recovery_job_items_active_work_reservation", table_name="recovery_job_items")
    op.drop_index("uq_recovery_job_items_active_stage_reservation", table_name="recovery_job_items")
    op.drop_index("ix_recovery_job_items_file_stage", table_name="recovery_job_items")
    op.drop_index("ix_recovery_job_items_parent_status", table_name="recovery_job_items")
    for constraint_name in (
        "recovery_job_items_attempt_budget_start_positive",
        "recovery_job_items_retryable_failures_non_negative",
        "recovery_job_items_retry_limit_allowed",
        "contentpipelinestage",
    ):
        _drop_check("recovery_job_items", constraint_name)
        _drop_check("recovery_job_items", f"ck_recovery_job_items_{constraint_name}")
    op.drop_constraint("fk_recovery_job_items_meme_file_id_meme_files", "recovery_job_items", type_="foreignkey")
    op.drop_constraint(
        "fk_recovery_job_items_source_item_id_recovery_job_items",
        "recovery_job_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_recovery_job_items_parent_item_id_recovery_job_items",
        "recovery_job_items",
        type_="foreignkey",
    )
    for column_name in (
        "previous_stage_state",
        "reservation_active",
        "terminal_override_acknowledged",
        "suppress_fanout",
        "preserve_ready",
        "retryable_failures_consumed",
        "attempt_budget_start",
        "retry_limit",
        "is_root",
        "stage",
        "meme_file_id",
        "source_item_id",
        "parent_item_id",
    ):
        op.drop_column("recovery_job_items", column_name)

    op.drop_index("ix_recovery_jobs_materialization_lease", table_name="recovery_jobs")
    for constraint_name in (
        "recovery_jobs_materialization_lease_generation_non_negative",
        "recovery_jobs_retry_limit_allowed",
        "recoveryreplayscope",
    ):
        _drop_check("recovery_jobs", constraint_name)
        _drop_check("recovery_jobs", f"ck_recovery_jobs_{constraint_name}")
    op.drop_constraint(
        "fk_recovery_jobs_source_recovery_job_id_recovery_jobs",
        "recovery_jobs",
        type_="foreignkey",
    )
    op.drop_constraint("fk_recovery_jobs_assigned_admin_user_id_users", "recovery_jobs", type_="foreignkey")
    for column_name in (
        "materialization_completed_at",
        "materialization_lease_generation",
        "materialization_lease_at",
        "materialization_lease_owner",
        "materialization_cursor",
        "selection_snapshot_at",
        "exclusions_by_reason",
        "cancelled_count",
        "skipped_count",
        "stale_count",
        "succeeded_count",
        "dispatched_count",
        "waiting_count",
        "queued_count",
        "excluded_count",
        "preparation_scanned_count",
        "expanded_execution_count",
        "selected_root_count",
        "retry_limit",
        "scope",
        "source_recovery_job_id",
        "assigned_admin_user_id",
    ):
        if column_name.endswith("_count"):
            constraint_name = f"recovery_jobs_{column_name}_non_negative"
            _drop_check("recovery_jobs", constraint_name)
            _drop_check("recovery_jobs", f"ck_recovery_jobs_{constraint_name}")
        op.drop_column("recovery_jobs", column_name)

    op.execute(
        sa.text(
            """
            UPDATE recovery_jobs
            SET status = CASE status
                WHEN 'preparing' THEN 'preview'
                WHEN 'cancelling' THEN 'cancelled'
                ELSE status
            END,
            action = CASE action
                WHEN 'replay_stage' THEN 'retry_stage'
                WHEN 'regenerate_derivatives' THEN 'retry_stage'
                ELSE action
            END
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE recovery_job_items
            SET status = CASE status
                WHEN 'waiting_dependency' THEN 'queued'
                WHEN 'skipped_dependency' THEN 'cancelled'
                ELSE status
            END,
            action = CASE action
                WHEN 'replay_stage' THEN 'retry_stage'
                WHEN 'regenerate_derivatives' THEN 'retry_stage'
                ELSE action
            END
            """
        )
    )
    _replace_recovery_checks(expanded=False)
    op.alter_column(
        "recovery_job_items",
        "status",
        existing_type=sa.String(length=18),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'::character varying"),
    )
    op.alter_column(
        "recovery_job_items",
        "action",
        existing_type=sa.String(length=22),
        type_=sa.String(length=19),
        existing_nullable=False,
    )
    op.alter_column(
        "recovery_jobs",
        "action",
        existing_type=sa.String(length=22),
        type_=sa.String(length=19),
        existing_nullable=False,
    )
