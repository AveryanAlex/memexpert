# ruff: noqa: TC003
"""Durable operational recovery, attempt-history, and runtime-health models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from memexpert.models.enums import (
    ContentPipelineStage,
    DependencyCircuitStatus,
    MediaGenerationCleanupStatus,
    MediaGenerationStatus,
    PipelineAttemptOutcome,
    PipelineCapacityStatus,
    RecoveryCapability,
    RecoveryDeadLetterStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryReplayScope,
    RecoveryWorkKind,
    string_enum,
)


class RecoveryJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable preview or scheduled batch of recovery work."""

    __tablename__ = "recovery_jobs"
    __table_args__ = (
        UniqueConstraint(
            "requested_by_admin_user_id",
            "request_id",
            name="uq_recovery_jobs_admin_request_id",
        ),
        CheckConstraint("total_count >= 0", name="recovery_jobs_total_count_non_negative"),
        CheckConstraint("completed_count >= 0", name="recovery_jobs_completed_count_non_negative"),
        CheckConstraint("failed_count >= 0", name="recovery_jobs_failed_count_non_negative"),
        CheckConstraint("retry_limit IN (1, 3, 5)", name="recovery_jobs_retry_limit_allowed"),
        CheckConstraint("selected_root_count >= 0", name="recovery_jobs_selected_root_count_non_negative"),
        CheckConstraint(
            "expanded_execution_count >= 0",
            name="recovery_jobs_expanded_execution_count_non_negative",
        ),
        CheckConstraint(
            "preparation_scanned_count >= 0",
            name="recovery_jobs_preparation_scanned_count_non_negative",
        ),
        CheckConstraint("excluded_count >= 0", name="recovery_jobs_excluded_count_non_negative"),
        CheckConstraint("queued_count >= 0", name="recovery_jobs_queued_count_non_negative"),
        CheckConstraint("waiting_count >= 0", name="recovery_jobs_waiting_count_non_negative"),
        CheckConstraint("dispatched_count >= 0", name="recovery_jobs_dispatched_count_non_negative"),
        CheckConstraint("succeeded_count >= 0", name="recovery_jobs_succeeded_count_non_negative"),
        CheckConstraint("stale_count >= 0", name="recovery_jobs_stale_count_non_negative"),
        CheckConstraint("skipped_count >= 0", name="recovery_jobs_skipped_count_non_negative"),
        CheckConstraint("cancelled_count >= 0", name="recovery_jobs_cancelled_count_non_negative"),
        CheckConstraint(
            "materialization_lease_generation >= 0",
            name="recovery_jobs_materialization_lease_generation_non_negative",
        ),
        Index("ix_recovery_jobs_status_created_at", "status", "created_at"),
        Index("ix_recovery_jobs_admin_created_at", "requested_by_admin_user_id", "created_at"),
        Index("ix_recovery_jobs_materialization_lease", "status", "materialization_lease_at"),
    )

    requested_by_admin_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_recovery_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[RecoveryJobStatus] = mapped_column(
        string_enum(RecoveryJobStatus),
        default=RecoveryJobStatus.PREVIEW,
        server_default=text("'preview'"),
        nullable=False,
    )
    action: Mapped[RecoveryCapability] = mapped_column(string_enum(RecoveryCapability), nullable=False)
    scope: Mapped[RecoveryReplayScope | None] = mapped_column(string_enum(RecoveryReplayScope), nullable=True)
    retry_limit: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    selection: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    total_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    completed_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    failed_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    selected_root_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    expanded_execution_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    preparation_scanned_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    excluded_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    exclusions_by_reason: Mapped[dict[str, int]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    queued_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    waiting_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    dispatched_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    stale_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    cancelled_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    selection_snapshot_at: Mapped[datetime | None] = mapped_column(nullable=True)
    materialization_cursor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    materialization_lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    materialization_lease_at: Mapped[datetime | None] = mapped_column(nullable=True)
    materialization_lease_generation: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    materialization_completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RecoveryJobItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One state/version-checked target in a recovery batch."""

    __tablename__ = "recovery_job_items"
    __table_args__ = (
        UniqueConstraint("recovery_job_id", "work_kind", "work_id", name="uq_recovery_job_items_target"),
        Index("ix_recovery_job_items_job_status", "recovery_job_id", "status"),
        Index("ix_recovery_job_items_status_created_at", "status", "created_at"),
        Index("ix_recovery_job_items_dispatch_event_id", "dispatch_event_id"),
        Index("ix_recovery_job_items_parent_status", "parent_item_id", "status"),
        Index("ix_recovery_job_items_file_stage", "meme_file_id", "stage"),
        Index(
            "uq_recovery_job_items_active_stage_reservation",
            "meme_file_id",
            "stage",
            unique=True,
            postgresql_where=text("reservation_active AND stage IS NOT NULL"),
        ),
        Index(
            "uq_recovery_job_items_active_work_reservation",
            "work_kind",
            "work_id",
            unique=True,
            postgresql_where=text("reservation_active AND stage IS NULL"),
        ),
        CheckConstraint("retry_limit IN (1, 3, 5)", name="recovery_job_items_retry_limit_allowed"),
        CheckConstraint(
            "retryable_failures_consumed >= 0",
            name="recovery_job_items_retryable_failures_non_negative",
        ),
        CheckConstraint(
            "attempt_budget_start IS NULL OR attempt_budget_start >= 1",
            name="recovery_job_items_attempt_budget_start_positive",
        ),
    )

    recovery_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    meme_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    stage: Mapped[ContentPipelineStage | None] = mapped_column(string_enum(ContentPipelineStage), nullable=True)
    is_root: Mapped[bool] = mapped_column(default=True, server_default=text("true"), nullable=False)
    work_kind: Mapped[RecoveryWorkKind] = mapped_column(string_enum(RecoveryWorkKind), nullable=False)
    work_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[RecoveryCapability] = mapped_column(string_enum(RecoveryCapability), nullable=False)
    expected_version: Mapped[str] = mapped_column(String(255), nullable=False)
    retry_limit: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"), nullable=False)
    attempt_budget_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable_failures_consumed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    preserve_ready: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    suppress_fanout: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    terminal_override_acknowledged: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    previous_stage_state: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    reservation_active: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    status: Mapped[RecoveryJobItemStatus] = mapped_column(
        string_enum(RecoveryJobItemStatus),
        default=RecoveryJobItemStatus.QUEUED,
        server_default=text("'queued'"),
        nullable=False,
    )
    dispatch_event_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    canonical_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RecoveryQuerySnapshotMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable root captured for an uncapped query preview."""

    __tablename__ = "recovery_query_snapshot_members"
    __table_args__ = (
        UniqueConstraint(
            "recovery_job_id",
            "root_key",
            name="uq_recovery_query_snapshot_members_job_root",
        ),
        CheckConstraint(
            "work_id IS NOT NULL OR is_outdated_video",
            name="recovery_query_snapshot_members_work_identity",
        ),
        Index(
            "ix_recovery_query_snapshot_members_job_id",
            "recovery_job_id",
            "id",
        ),
    )

    recovery_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    root_key: Mapped[str] = mapped_column(String(512), nullable=False)
    work_kind: Mapped[RecoveryWorkKind] = mapped_column(string_enum(RecoveryWorkKind), nullable=False)
    work_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meme_file_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    stage: Mapped[ContentPipelineStage | None] = mapped_column(string_enum(ContentPipelineStage), nullable=True)
    captured_version: Mapped[str | None] = mapped_column(String(512), nullable=True)
    captured_context_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_outdated_video: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false"),
        nullable=False,
    )


class MediaGeneration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable ledger for one immutable moving-media derivative generation."""

    __tablename__ = "media_generations"
    __table_args__ = (
        UniqueConstraint("web_video_object_key", name="uq_media_generations_web_video_object_key"),
        UniqueConstraint("preview_image_object_key", name="uq_media_generations_preview_image_object_key"),
        CheckConstraint("retry_limit IN (1, 3, 5)", name="media_generations_retry_limit_allowed"),
        CheckConstraint("attempt_count >= 0", name="media_generations_attempt_count_non_negative"),
        CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="media_generations_cleanup_attempt_count_non_negative",
        ),
        Index("ix_media_generations_file_created", "meme_file_id", "created_at"),
        Index("ix_media_generations_status_superseded", "status", "superseded_at"),
        Index("ix_media_generations_cleanup_status_created", "cleanup_status", "created_at"),
        Index("ix_media_generations_recovery_item", "recovery_item_id"),
    )

    meme_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    recovery_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    expected_web_video_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    web_video_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    preview_image_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(128), nullable=False)
    retry_limit: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    status: Mapped[MediaGenerationStatus] = mapped_column(
        string_enum(MediaGenerationStatus),
        default=MediaGenerationStatus.GENERATING,
        server_default=text("'generating'"),
        nullable=False,
    )
    previous_file_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_stage_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_stage_observations: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    source_observations: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    output_observations: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    source_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_frame_rate_numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_frame_rate_denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_has_audio: Mapped[bool | None] = mapped_column(nullable=True)
    output_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_frame_rate_numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_frame_rate_denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_video_bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_has_audio: Mapped[bool | None] = mapped_column(nullable=True)
    safe_failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_failure_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cleanup_status: Mapped[MediaGenerationCleanupStatus] = mapped_column(
        string_enum(MediaGenerationCleanupStatus),
        default=MediaGenerationCleanupStatus.NOT_ELIGIBLE,
        server_default=text("'not_eligible'"),
        nullable=False,
    )
    cleanup_attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    cleanup_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_at: Mapped[datetime | None] = mapped_column(nullable=True)


class OperationalAuditLog(UUIDPrimaryKeyMixin, Base):
    """Immutable audit record for operational recovery mutations."""

    __tablename__ = "operational_audit_logs"
    __table_args__ = (
        Index("ix_operational_audit_logs_admin_created", "admin_user_id", "created_at"),
        Index("ix_operational_audit_logs_target_created", "target_kind", "target_id", "created_at"),
        Index("ix_operational_audit_logs_action_created", "action", "created_at"),
    )

    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_values: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    new_values: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    note: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class PipelineStageAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Attempt history preserved independently from the latest stage journal row."""

    __tablename__ = "pipeline_stage_attempts"
    __table_args__ = (
        UniqueConstraint(
            "meme_file_id",
            "stage",
            "event_id",
            "attempt_number",
            name="uq_pipeline_stage_attempts_file_stage_event_attempt",
        ),
        CheckConstraint("attempt_number >= 1", name="pipeline_stage_attempts_attempt_positive"),
        Index("ix_pipeline_stage_attempts_file_stage_started", "meme_file_id", "stage", "started_at"),
        Index("ix_pipeline_stage_attempts_reason_started", "normalized_reason", "started_at"),
        Index("ix_pipeline_stage_attempts_recovery_item", "recovery_item_id"),
    )

    meme_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[ContentPipelineStage] = mapped_column(string_enum(ContentPipelineStage), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[PipelineAttemptOutcome] = mapped_column(
        string_enum(PipelineAttemptOutcome),
        default=PipelineAttemptOutcome.PROCESSING,
        server_default=text("'processing'"),
        nullable=False,
    )
    recovery_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    worker_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    worker_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    normalized_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SourceChannelBackfillAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Attempt history and lease fencing for one logical backfill job."""

    __tablename__ = "source_channel_backfill_attempts"
    __table_args__ = (
        UniqueConstraint("backfill_job_id", "attempt_number", name="uq_backfill_attempts_job_attempt"),
        Index("ix_backfill_attempts_job_started", "backfill_job_id", "started_at"),
    )

    backfill_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_channel_backfill_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("telegram_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    recovery_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    worker_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    normalized_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_retryable: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PipelineDeadLetter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """PostgreSQL ledger for final broker deliveries and imported legacy DLQ messages."""

    __tablename__ = "pipeline_dead_letters"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_pipeline_dead_letters_deduplication_key"),
        Index("ix_pipeline_dead_letters_status_created", "status", "created_at"),
        Index("ix_pipeline_dead_letters_work", "work_kind", "work_id"),
        Index("ix_pipeline_dead_letters_message_id", "broker_message_id"),
    )

    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    broker_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    work_kind: Mapped[RecoveryWorkKind | None] = mapped_column(string_enum(RecoveryWorkKind), nullable=True)
    work_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    death_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )
    safe_payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    safe_headers: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    status: Mapped[RecoveryDeadLetterStatus] = mapped_column(
        string_enum(RecoveryDeadLetterStatus),
        default=RecoveryDeadLetterStatus.UNRESOLVED,
        server_default=text("'unresolved'"),
        nullable=False,
    )
    recovery_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PipelineCapacityState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Hysteretic admission state derived from canonical stage backlog."""

    __tablename__ = "pipeline_capacity_states"
    __table_args__ = (UniqueConstraint("stage", name="uq_pipeline_capacity_states_stage"),)

    stage: Mapped[ContentPipelineStage] = mapped_column(string_enum(ContentPipelineStage), nullable=False)
    status: Mapped[PipelineCapacityStatus] = mapped_column(
        string_enum(PipelineCapacityStatus),
        default=PipelineCapacityStatus.OPEN,
        server_default=text("'open'"),
        nullable=False,
    )
    pending_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    oldest_pending_age_seconds: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        server_default=text("0"),
        nullable=False,
    )
    throughput_per_minute_15m: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        server_default=text("0"),
        nullable=False,
    )
    drain_eta_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class DependencyCircuitState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable provider circuit state shared across worker restarts."""

    __tablename__ = "dependency_circuit_states"
    __table_args__ = (UniqueConstraint("dependency", name="uq_dependency_circuit_states_dependency"),)

    dependency: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DependencyCircuitStatus] = mapped_column(
        string_enum(DependencyCircuitStatus),
        default=DependencyCircuitStatus.CLOSED,
        server_default=text("'closed'"),
        nullable=False,
    )
    error_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    opened_at: Mapped[datetime | None] = mapped_column(nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(nullable=True)
    probe_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    probe_generation: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )


class RuntimeHeartbeat(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Latest durable runtime state for operational diagnosis."""

    __tablename__ = "runtime_heartbeats"
    __table_args__ = (
        UniqueConstraint("service_name", "role", "instance_id", name="uq_runtime_heartbeats_instance"),
        Index("ix_runtime_heartbeats_service_role_updated", "service_name", "role", "updated_at"),
    )

    service_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(
        String(64),
        default="",
        server_default=text("''"),
        nullable=False,
    )
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ready: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(32),
        default="starting",
        server_default=text("'starting'"),
        nullable=False,
    )
    current_operation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_deadline_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(nullable=True)
    safe_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )


__all__ = [
    "DependencyCircuitState",
    "MediaGeneration",
    "OperationalAuditLog",
    "PipelineCapacityState",
    "PipelineDeadLetter",
    "PipelineStageAttempt",
    "RecoveryJob",
    "RecoveryJobItem",
    "RecoveryQuerySnapshotMember",
    "RuntimeHeartbeat",
    "SourceChannelBackfillAttempt",
]
