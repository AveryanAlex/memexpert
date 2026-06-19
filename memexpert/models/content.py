# ruff: noqa: F821,TC003,UP037
"""Content, SEO, source, popularity, and cache ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    EmbeddingInputType,
    IngestFileOrigin,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    SourceAttachReason,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramMediaFormat,
    TelegramSessionStatus,
    string_enum,
)

if TYPE_CHECKING:
    from memexpert.models.collection import CollectionMeme, PinnedMeme
    from memexpert.models.user import User


class Meme(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Conceptual meme entity that can own many underlying media files."""

    __tablename__ = "memes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "primary_file_id"],
            ["meme_files.meme_id", "meme_files.id"],
            name="fk_memes_primary_file_id_meme_files",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_memes_media_type_language", "media_type", "language"),
        Index("ix_memes_visibility_popularity_created_at", "is_public", "popularity_score", "created_at"),
        Index("ix_memes_author_user_id_created_at", "author_user_id", "created_at"),
    )

    media_type: Mapped[ContentKind] = mapped_column(
        string_enum(ContentKind),
        nullable=False,
    )
    primary_file_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[ContentLanguage] = mapped_column(
        string_enum(ContentLanguage),
        default=ContentLanguage.NONE,
        nullable=False,
    )
    is_nsfw: Mapped[bool] = mapped_column(default=False, nullable=False)
    popularity_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_public: Mapped[bool] = mapped_column(default=True, nullable=False)

    primary_file: Mapped["MemeFile | None"] = relationship(
        "MemeFile",
        primaryjoin=lambda: and_(
            Meme.id == MemeFile.meme_id,
            foreign(Meme.primary_file_id) == MemeFile.id,
        ),
        back_populates="primary_for_meme",
        foreign_keys=lambda: [Meme.primary_file_id],
        uselist=False,
    )
    files: Mapped[list["MemeFile"]] = relationship(
        "MemeFile",
        primaryjoin=lambda: Meme.id == foreign(MemeFile.meme_id),
        back_populates="meme",
        foreign_keys=lambda: [MemeFile.meme_id],
        cascade="all, delete-orphan",
    )
    seo_page: Mapped["MemeSeoPage | None"] = relationship(
        "MemeSeoPage",
        back_populates="meme",
        cascade="all, delete-orphan",
        uselist=False,
    )
    template: Mapped["MemeTemplate | None"] = relationship("MemeTemplate", back_populates="memes")
    author: Mapped["User | None"] = relationship(
        "User",
        back_populates="authored_memes",
        foreign_keys=[author_user_id],
    )
    popularity_snapshots: Mapped[list["MemePopularitySnapshot"]] = relationship(
        "MemePopularitySnapshot",
        back_populates="meme",
        cascade="all, delete-orphan",
    )
    saved_in_collections: Mapped[list["CollectionMeme"]] = relationship(
        "CollectionMeme",
        back_populates="meme",
        cascade="all, delete-orphan",
    )
    pinned_by_users: Mapped[list["PinnedMeme"]] = relationship(
        "PinnedMeme",
        back_populates="meme",
        cascade="all, delete-orphan",
    )
    moderation_reports: Mapped[list["ModerationReport"]] = relationship(
        "ModerationReport",
        back_populates="meme",
        cascade="all, delete-orphan",
    )
    moderation_decisions: Mapped[list["ModerationDecision"]] = relationship(
        "ModerationDecision",
        back_populates="meme",
        cascade="all, delete-orphan",
    )


class ModerationReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable user/admin report that feeds the admin moderation queue."""

    __tablename__ = "moderation_reports"
    __table_args__ = (
        Index("ix_moderation_reports_status_created_at", "status", "created_at"),
        Index("ix_moderation_reports_meme_id_status", "meme_id", "status"),
        Index("ix_moderation_reports_reporter_user_id_created_at", "reporter_user_id", "created_at"),
    )

    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        nullable=False,
    )
    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[ModerationReportStatus] = mapped_column(
        string_enum(ModerationReportStatus),
        default=ModerationReportStatus.PENDING,
        nullable=False,
    )
    reason: Mapped[ModerationReason] = mapped_column(
        string_enum(ModerationReason),
        default=ModerationReason.OTHER,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    meme: Mapped["Meme"] = relationship("Meme", back_populates="moderation_reports")
    reporter_user: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[reporter_user_id],
        back_populates="moderation_reports_submitted",
    )
    resolved_by_admin_user: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[resolved_by_admin_user_id],
        back_populates="moderation_reports_resolved",
    )
    decisions: Mapped[list["ModerationDecision"]] = relationship(
        "ModerationDecision",
        back_populates="report",
    )


class ModerationDecision(UUIDPrimaryKeyMixin, Base):
    """Immutable admin audit entry for report resolutions and direct overrides."""

    __tablename__ = "moderation_decisions"
    __table_args__ = (
        Index("ix_moderation_decisions_meme_id_created_at", "meme_id", "created_at"),
        Index("ix_moderation_decisions_report_id_created_at", "report_id", "created_at"),
        Index("ix_moderation_decisions_admin_user_id_created_at", "admin_user_id", "created_at"),
    )

    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("moderation_reports.id", ondelete="SET NULL"),
        nullable=True,
    )
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[ModerationAction] = mapped_column(
        string_enum(ModerationAction),
        nullable=False,
    )
    reason: Mapped[ModerationReason | None] = mapped_column(
        string_enum(ModerationReason),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_is_public: Mapped[bool] = mapped_column(nullable=False)
    previous_is_nsfw: Mapped[bool] = mapped_column(nullable=False)
    new_is_public: Mapped[bool] = mapped_column(nullable=False)
    new_is_nsfw: Mapped[bool] = mapped_column(nullable=False)
    previous_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    meme: Mapped["Meme"] = relationship("Meme", back_populates="moderation_decisions")
    report: Mapped["ModerationReport | None"] = relationship("ModerationReport", back_populates="decisions")
    admin_user: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[admin_user_id],
        back_populates="moderation_decisions",
    )


class MemeFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A specific media-file instance belonging to a meme."""

    __tablename__ = "meme_files"
    __table_args__ = (
        UniqueConstraint("meme_id", "id", name="uq_meme_files_meme_id_id"),
        CheckConstraint(
            "sha256_hex IS NULL OR sha256_hex = lower(sha256_hex)",
            name="meme_files_sha256_hex_lowercase",
        ),
        CheckConstraint(
            "sha256_hex IS NULL OR sha256_hex ~ '^[0-9a-f]{64}$'",
            name="meme_files_sha256_hex_hex",
        ),
        Index("ix_meme_files_meme_id_status", "meme_id", "status"),
        Index("ix_meme_files_ingest_origin", "ingest_origin"),
        Index("ix_meme_files_matched_meme_file_id", "matched_meme_file_id"),
        Index("ix_meme_files_perceptual_hash", "perceptual_hash"),
        Index("ix_meme_files_sha256_hex", "sha256_hex"),
    )

    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ContentProcessingStatus] = mapped_column(
        string_enum(ContentProcessingStatus),
        default=ContentProcessingStatus.PENDING,
        nullable=False,
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    s3_original_key: Mapped[str] = mapped_column(Text, nullable=False)
    s3_web_video_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingest_origin: Mapped[IngestFileOrigin | None] = mapped_column(
        string_enum(IngestFileOrigin),
        nullable=True,
    )
    matched_meme_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    blocked_perceptual_hash_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blocked_perceptual_hashes.id", ondelete="SET NULL"),
        nullable=True,
    )
    quality_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    blur_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    meme: Mapped["Meme"] = relationship(
        "Meme",
        primaryjoin=lambda: foreign(MemeFile.meme_id) == Meme.id,
        back_populates="files",
        foreign_keys=lambda: [MemeFile.meme_id],
    )
    primary_for_meme: Mapped["Meme | None"] = relationship(
        "Meme",
        primaryjoin=lambda: and_(
            MemeFile.meme_id == Meme.id,
            MemeFile.id == foreign(Meme.primary_file_id),
        ),
        foreign_keys=lambda: [Meme.primary_file_id],
        uselist=False,
        viewonly=True,
    )
    sources: Mapped[list["MemeSource"]] = relationship(
        "MemeSource",
        back_populates="file",
        cascade="all, delete-orphan",
        foreign_keys=lambda: [MemeSource.file_id],
    )
    matched_meme_file: Mapped["MemeFile | None"] = relationship(
        "MemeFile",
        remote_side=lambda: [MemeFile.id],
        foreign_keys=lambda: [MemeFile.matched_meme_file_id],
    )
    telegram_file_ids: Mapped[list["TelegramFileIdCache"]] = relationship(
        "TelegramFileIdCache",
        back_populates="meme_file",
        cascade="all, delete-orphan",
    )
    embedding_cache_entries: Mapped[list["EmbeddingCache"]] = relationship(
        "EmbeddingCache",
        back_populates="source_file",
    )
    pipeline_stage_journal_entries: Mapped[list["PipelineStageJournal"]] = relationship(
        "PipelineStageJournal",
        back_populates="meme_file",
        cascade="all, delete-orphan",
    )
    ocr_result: Mapped["MemeFileOCRResult | None"] = relationship(
        "MemeFileOCRResult",
        back_populates="meme_file",
        cascade="all, delete-orphan",
        uselist=False,
    )
    sync_target_snapshots: Mapped[list["MemeFileSyncTargetSnapshot"]] = relationship(
        "MemeFileSyncTargetSnapshot",
        back_populates="meme_file",
        cascade="all, delete-orphan",
    )
    blocked_perceptual_hash: Mapped["BlockedPerceptualHash | None"] = relationship(
        "BlockedPerceptualHash",
        back_populates="matched_meme_files",
    )


class BlockedPerceptualHash(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Admin-managed perceptual-hash pattern consumed by real ingest paths."""

    __tablename__ = "blocked_perceptual_hashes"
    __table_args__ = (
        UniqueConstraint(
            "hash_algorithm",
            "hash_size",
            "perceptual_hash",
            name="uq_blocked_perceptual_hashes_algorithm_size_hash",
        ),
        CheckConstraint("hash_size > 0", name="blocked_perceptual_hashes_hash_size_positive"),
        CheckConstraint(
            "max_hamming_distance >= 0 AND max_hamming_distance <= hash_size",
            name="blocked_perceptual_hashes_distance_within_hash_size",
        ),
        CheckConstraint(
            "perceptual_hash = lower(perceptual_hash)",
            name="blocked_perceptual_hashes_hash_lowercase",
        ),
        CheckConstraint(
            "perceptual_hash ~ '^[0-9a-f]+$'",
            name="blocked_perceptual_hashes_hash_hex",
        ),
        CheckConstraint(
            "hash_size = char_length(perceptual_hash) * 4",
            name="blocked_perceptual_hashes_hash_size_matches_hash",
        ),
        Index("ix_blocked_perceptual_hashes_active_algorithm", "is_active", "hash_algorithm"),
        Index("ix_blocked_perceptual_hashes_created_by_created_at", "created_by_admin_user_id", "created_at"),
    )

    perceptual_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_algorithm: Mapped[str] = mapped_column(String(32), default="phash", nullable=False)
    hash_size: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hamming_distance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason: Mapped[ModerationReason] = mapped_column(
        string_enum(ModerationReason),
        default=ModerationReason.OTHER,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    matched_meme_files: Mapped[list["MemeFile"]] = relationship(
        "MemeFile",
        back_populates="blocked_perceptual_hash",
    )


class BlockedPerceptualHashAuditLog(UUIDPrimaryKeyMixin, Base):
    """Immutable admin history for blocked perceptual-hash lifecycle changes."""

    __tablename__ = "blocked_perceptual_hash_audit_logs"
    __table_args__ = (
        Index("ix_blocked_perceptual_hash_audit_logs_hash_created_at", "blocked_perceptual_hash_id", "created_at"),
        Index("ix_blocked_perceptual_hash_audit_logs_admin_created_at", "admin_user_id", "created_at"),
        Index("ix_blocked_perceptual_hash_audit_logs_action_created_at", "action", "created_at"),
    )

    blocked_perceptual_hash_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_values: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    new_values: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class PipelineStageJournal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Latest per-stage truth for a meme file as it moves through the pipeline."""

    __tablename__ = "pipeline_stage_journal"
    __table_args__ = (
        UniqueConstraint("meme_file_id", "stage", name="uq_pipeline_stage_journal_meme_file_id_stage"),
        CheckConstraint("attempt_count >= 0", name="pipeline_stage_journal_attempt_count_non_negative"),
        Index("ix_pipeline_stage_journal_stage_status", "stage", "status"),
        Index("ix_pipeline_stage_journal_meme_file_id_updated_at", "meme_file_id", "updated_at"),
        Index("ix_pipeline_stage_journal_last_event_id", "last_event_id"),
        Index("ix_pipeline_stage_journal_status_retry_after", "status", "retry_after"),
    )

    meme_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[ContentPipelineStage] = mapped_column(
        string_enum(ContentPipelineStage),
        nullable=False,
    )
    status: Mapped[ContentPipelineStageStatus] = mapped_column(
        string_enum(ContentPipelineStageStatus),
        default=ContentPipelineStageStatus.PENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    normalized_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_retryable: Mapped[bool] = mapped_column(default=False, nullable=False)
    retry_after: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    meme_file: Mapped["MemeFile"] = relationship("MemeFile", back_populates="pipeline_stage_journal_entries")


class PipelineIngestRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Raw upload/source request accepted before worker media materialization."""

    __tablename__ = "pipeline_ingest_requests"
    __table_args__ = (
        UniqueConstraint(
            "source_platform",
            "source_id",
            "post_id",
            name="uq_pipeline_ingest_requests_source_identity",
        ),
        CheckConstraint(
            "sha256_hex IS NULL OR sha256_hex = lower(sha256_hex)",
            name="pipeline_ingest_requests_sha256_hex_lowercase",
        ),
        CheckConstraint(
            "sha256_hex IS NULL OR sha256_hex ~ '^[0-9a-f]{64}$'",
            name="pipeline_ingest_requests_sha256_hex_hex",
        ),
        CheckConstraint(
            "file_size_bytes IS NULL OR file_size_bytes >= 0",
            name="pipeline_ingest_requests_file_size_non_negative",
        ),
        CheckConstraint("attempt_count >= 0", name="pipeline_ingest_requests_attempt_count_non_negative"),
        Index("ix_pipeline_ingest_requests_sha256_hex", "sha256_hex"),
        Index("ix_pipeline_ingest_requests_status_created_at", "status", "created_at"),
        Index("ix_pipeline_ingest_requests_materialized_meme_id", "materialized_meme_id"),
        Index("ix_pipeline_ingest_requests_materialized_meme_file_id", "materialized_meme_file_id"),
        Index("ix_pipeline_ingest_requests_matched_meme_file_id", "matched_meme_file_id"),
    )

    source_platform: Mapped[SourcePlatform] = mapped_column(
        string_enum(SourcePlatform),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    source_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    declared_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declared_content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    temp_original_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[PipelineIngestRequestStatus] = mapped_column(
        string_enum(PipelineIngestRequestStatus),
        default=PipelineIngestRequestStatus.ACCEPTED,
        nullable=False,
    )
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    materialized_meme_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memes.id", ondelete="SET NULL"),
        nullable=True,
    )
    materialized_meme_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    matched_meme_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_attach_reason: Mapped[SourceAttachReason | None] = mapped_column(
        string_enum(SourceAttachReason),
        nullable=True,
    )

    materialized_meme: Mapped["Meme | None"] = relationship("Meme", foreign_keys=[materialized_meme_id])
    materialized_meme_file: Mapped["MemeFile | None"] = relationship(
        "MemeFile",
        foreign_keys=[materialized_meme_file_id],
    )
    matched_meme_file: Mapped["MemeFile | None"] = relationship("MemeFile", foreign_keys=[matched_meme_file_id])


class RabbitMQOutboxMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable RabbitMQ publish intent written with business state changes."""

    __tablename__ = "rabbitmq_outbox_messages"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_rabbitmq_outbox_messages_message_id"),
        CheckConstraint("attempt_count >= 0", name="rabbitmq_outbox_messages_attempt_count_non_negative"),
        CheckConstraint("aggregate_id <> ''", name="rabbitmq_outbox_messages_aggregate_id_not_blank"),
        CheckConstraint("aggregate_type <> ''", name="rabbitmq_outbox_messages_aggregate_type_not_blank"),
        CheckConstraint("content_type <> ''", name="rabbitmq_outbox_messages_content_type_not_blank"),
        CheckConstraint("event_type <> ''", name="rabbitmq_outbox_messages_event_type_not_blank"),
        CheckConstraint("exchange <> ''", name="rabbitmq_outbox_messages_exchange_not_blank"),
        CheckConstraint("message_id <> ''", name="rabbitmq_outbox_messages_message_id_not_blank"),
        CheckConstraint("routing_key <> ''", name="rabbitmq_outbox_messages_routing_key_not_blank"),
        Index("ix_rabbitmq_outbox_messages_status_retry_created", "status", "next_retry_at", "created_at"),
        Index("ix_rabbitmq_outbox_messages_aggregate", "aggregate_type", "aggregate_id", "created_at"),
        Index("ix_rabbitmq_outbox_messages_event_status", "event_type", "status"),
        Index("ix_rabbitmq_outbox_messages_lease", "status", "locked_at"),
        Index("ix_rabbitmq_outbox_messages_ordering_key_created", "ordering_key", "created_at"),
    )

    exchange: Mapped[str] = mapped_column(String(255), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    headers: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), default="application/json", nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordering_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[RabbitMQOutboxMessageStatus] = mapped_column(
        string_enum(RabbitMQOutboxMessageStatus),
        default=RabbitMQOutboxMessageStatus.PENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    lock_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemeFileSyncTargetSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable per-target sync truth for Qdrant and Meilisearch.

    Each row tracks one ``(meme_file_id, sync_target)`` pair and captures the
    latest success/failure timestamps, the normalized failure reason, the event
    id that produced this state, and a bounded JSON preview of the payload that
    the target last received. T02 (Qdrant) and T03 (Meilisearch) populate this
    row as they execute; T04 and the reporting layer read from it without
    re-deriving truth from stage-journal semantics.
    """

    __tablename__ = "meme_file_sync_target_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "meme_file_id",
            "sync_target",
            name="uq_meme_file_sync_target_snapshots_meme_file_id_sync_target",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="meme_file_sync_target_snapshots_attempt_count_non_negative",
        ),
        Index(
            "ix_meme_file_sync_target_snapshots_target_updated_at",
            "sync_target",
            "updated_at",
        ),
    )

    meme_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    sync_target: Mapped[SyncTargetKind] = mapped_column(
        string_enum(SyncTargetKind),
        nullable=False,
    )
    status: Mapped[SyncTargetStatus] = mapped_column(
        string_enum(SyncTargetStatus),
        default=SyncTargetStatus.PENDING,
        nullable=False,
    )
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    normalized_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_payload_preview: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    meme_file: Mapped["MemeFile"] = relationship(
        "MemeFile",
        back_populates="sync_target_snapshots",
    )


class MemeFileOCRResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable per-file OCR provenance, confidence, and extracted text state."""

    __tablename__ = "meme_file_ocr_results"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="meme_file_ocr_results_confidence_between_zero_and_one",
        ),
        Index("uq_meme_file_ocr_results_meme_file_id", "meme_file_id", unique=True),
        Index("ix_meme_file_ocr_results_engine_updated_at", "engine", "updated_at"),
        Index("ix_meme_file_ocr_results_low_confidence_updated_at", "low_confidence", "updated_at"),
    )

    meme_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    fallback_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(default=False, nullable=False)
    low_confidence: Mapped[bool] = mapped_column(default=False, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[ContentLanguage] = mapped_column(
        string_enum(ContentLanguage),
        default=ContentLanguage.NONE,
        nullable=False,
    )
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    meme_file: Mapped["MemeFile"] = relationship("MemeFile", back_populates="ocr_result")


class MemeSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Source provenance for a specific meme file."""

    __tablename__ = "meme_sources"
    __table_args__ = (
        UniqueConstraint("platform", "source_id", "post_id", name="uq_meme_sources_platform_source_post"),
        CheckConstraint(
            "engagement_check_attempt_count >= 0",
            name="meme_sources_engagement_check_attempt_count_non_negative",
        ),
        Index("ix_meme_sources_file_id_platform", "file_id", "platform"),
        Index("ix_meme_sources_attach_reason", "attach_reason"),
        Index("ix_meme_sources_matched_meme_file_id", "matched_meme_file_id"),
        Index("ix_meme_sources_engagement_due_lease", "next_engagement_check_at", "engagement_check_locked_at"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SourcePlatform] = mapped_column(
        string_enum(SourcePlatform),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reactions: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict, nullable=False)
    is_first_source: Mapped[bool] = mapped_column(default=False, nullable=False)
    source_alive: Mapped[bool] = mapped_column(default=True, nullable=False)
    # The Telegram publish timestamp captured by the crawler; left NULL for
    # operator uploads and backfilled rows because they have no upstream
    # publish time the freshness SLO could measure against.
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_engagement_check_at: Mapped[datetime | None] = mapped_column(nullable=True)
    next_engagement_check_at: Mapped[datetime | None] = mapped_column(nullable=True)
    engagement_check_locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    engagement_check_lock_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engagement_check_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_engagement_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Forward-chain attribution: when the crawler saw a reposter channel, the
    # ``source_id``/``post_id`` pair still records the channel where the post
    # was seen. These two columns preserve the original author pair so the
    # product surface can attribute the original poster. Both are NULL when
    # ``forward`` was absent on the underlying Telegram message.
    forwarded_from_source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    forwarded_from_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attach_reason: Mapped[SourceAttachReason | None] = mapped_column(
        string_enum(SourceAttachReason),
        nullable=True,
    )
    matched_meme_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )

    file: Mapped["MemeFile"] = relationship(
        "MemeFile",
        back_populates="sources",
        foreign_keys=[file_id],
    )
    matched_meme_file: Mapped["MemeFile | None"] = relationship(
        "MemeFile",
        foreign_keys=[matched_meme_file_id],
    )
    engagement_snapshots: Mapped[list["MemeSourceEngagementSnapshot"]] = relationship(
        "MemeSourceEngagementSnapshot",
        back_populates="source",
        cascade="all, delete-orphan",
    )

    @property
    def is_forwarded(self) -> bool:
        """Return ``True`` when this source row points at a forwarded Telegram post.

        Small ergonomic helper so callers do not have to re-derive "this row
        is a repost" from the two forward columns every time. Kept as a
        computed property (no DB column) so the ORM contract stays small.
        """

        return self.forwarded_from_source_id is not None


class MemeSourceEngagementSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only upstream engagement metrics captured for one source post."""

    __tablename__ = "meme_source_engagement_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "meme_source_id",
            "captured_at",
            name="uq_meme_source_engagement_snapshots_source_captured_at",
        ),
        UniqueConstraint(
            "meme_source_id",
            "scheduled_for",
            "schedule_label",
            name="uq_meme_source_engagement_snapshots_source_schedule",
        ),
        CheckConstraint(
            "view_count IS NULL OR view_count >= 0",
            name="meme_source_engagement_snapshots_view_count_non_negative",
        ),
        CheckConstraint(
            "reaction_count IS NULL OR reaction_count >= 0",
            name="meme_source_engagement_snapshots_reaction_count_non_negative",
        ),
        CheckConstraint(
            "comment_count IS NULL OR comment_count >= 0",
            name="meme_source_engagement_snapshots_comment_count_non_negative",
        ),
        CheckConstraint(
            "forward_count IS NULL OR forward_count >= 0",
            name="meme_source_engagement_snapshots_forward_count_non_negative",
        ),
        Index(
            "ix_meme_source_engagement_snapshots_label_status_captured",
            "schedule_label",
            "fetch_status",
            "captured_at",
        ),
    )

    meme_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    captured_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(nullable=True)
    capture_reason: Mapped[SourceEngagementCaptureReason] = mapped_column(
        string_enum(SourceEngagementCaptureReason),
        nullable=False,
    )
    schedule_label: Mapped[SourceEngagementScheduleLabel | None] = mapped_column(
        string_enum(SourceEngagementScheduleLabel),
        nullable=True,
    )
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reactions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    reaction_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forward_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments_state: Mapped[SourceEngagementCommentsState] = mapped_column(
        string_enum(SourceEngagementCommentsState),
        default=SourceEngagementCommentsState.UNKNOWN,
        nullable=False,
    )
    fetch_status: Mapped[SourceEngagementFetchStatus] = mapped_column(
        string_enum(SourceEngagementFetchStatus),
        nullable=False,
    )
    source_alive: Mapped[bool] = mapped_column(default=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_metrics: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)

    source: Mapped["MemeSource"] = relationship("MemeSource", back_populates="engagement_snapshots")


Index(
    "ix_meme_source_engagement_snapshots_source_captured_desc",
    MemeSourceEngagementSnapshot.__table__.c.meme_source_id,
    MemeSourceEngagementSnapshot.__table__.c.captured_at.desc(),
)


class MemeSeoPage(Base):
    """LLM-authored SEO page data attached 1:1 to a meme."""

    __tablename__ = "meme_seo_pages"
    __table_args__ = (
        Index("uq_meme_seo_pages_slug", "slug", unique=True),
        Index("ix_meme_seo_pages_generated_at", "generated_at"),
    )

    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    page_title: Mapped[str] = mapped_column(String(255), nullable=False)
    meta_description: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list, nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(nullable=True)

    meme: Mapped["Meme"] = relationship("Meme", back_populates="seo_page")


class MemeTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Template taxonomy ready for future editor features."""

    __tablename__ = "meme_templates"
    __table_args__ = (
        Index("uq_meme_templates_slug", "slug", unique=True),
        Index("ix_meme_templates_is_curated", "is_curated"),
    )

    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_curated: Mapped[bool] = mapped_column(default=False, nullable=False)
    base_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_regions: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)

    memes: Mapped[list["Meme"]] = relationship("Meme", back_populates="template")


class MemePopularitySnapshot(UUIDPrimaryKeyMixin, Base):
    """Historical popularity metrics captured on a schedule."""

    __tablename__ = "meme_popularity_snapshots"
    __table_args__ = (
        UniqueConstraint("meme_id", "captured_at", name="uq_meme_popularity_snapshots_meme_id_captured_at"),
        Index("ix_meme_popularity_snapshots_meme_id_captured_at", "meme_id", "captured_at"),
    )

    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        nullable=False,
    )
    captured_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    source_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_reactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_reposts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platform_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platform_sends: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platform_saves: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platform_likes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    popularity_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    meme: Mapped["Meme"] = relationship("Meme", back_populates="popularity_snapshots")


class SourceChannel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Tracked source channels and crawler checkpoint state."""

    __tablename__ = "source_channels"
    __table_args__ = (
        UniqueConstraint("platform", "platform_id", name="uq_source_channels_platform_platform_id"),
        Index("ix_source_channels_is_active_platform", "is_active", "platform"),
    )

    platform: Mapped[SourcePlatform] = mapped_column(
        string_enum(SourcePlatform),
        nullable=False,
    )
    platform_id: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subscriber_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_read_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Bounded number of messages to consume during initial catch-up. Prevents
    # a brand new curated channel from drowning the crawler by fetching years
    # of history the first time it is added.
    catchup_message_limit: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    # When ``True`` the crawler runs catch-up sweeps for this channel on
    # startup and after reconnects. Operators can freeze catch-up without
    # disabling the live listener by setting this to ``False``.
    catchup_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    # When ``True`` the crawler skips this channel entirely (catch-up AND
    # live listener). Operators use this to pause a problematic channel
    # without losing its checkpoint state.
    is_paused: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Most recent successful crawler poll for this channel. Distinct from
    # ``last_read_post_id`` because a poll can return zero new messages;
    # tracking this separately lets operators see staleness.
    last_fetched_at: Mapped[datetime | None] = mapped_column(nullable=True)


class TelegramSessionState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable per-session health and cooldown truth for Telethon userbot sessions.

    T02 populates this row when a real Telethon client connects; T01 creates
    the contract so the crawler runtime, operator surfaces, and the session
    distributor all agree on the same persisted shape before any real network
    work lands. Operators see at most one row per ``session_name`` so the
    table doubles as a session registry. ``status`` uses
    :class:`TelegramSessionStatus` (separate from sync-target statuses) so
    session health never collides with any pipeline-stage lifecycle.
    """

    __tablename__ = "telegram_session_states"
    __table_args__ = (
        UniqueConstraint("session_name", name="uq_telegram_session_states_session_name"),
        Index("ix_telegram_session_states_status_updated_at", "status", "updated_at"),
    )

    session_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[TelegramSessionStatus] = mapped_column(
        string_enum(TelegramSessionStatus),
        default=TelegramSessionStatus.STOPPED,
        nullable=False,
    )
    last_error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    flood_wait_until: Mapped[datetime | None] = mapped_column(nullable=True)
    live_listener_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(nullable=True)


class TelegramFileIdCache(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Telegram Bot API file_id cache scoped by bot token and delivery format."""

    __tablename__ = "telegram_file_id_cache"
    __table_args__ = (
        Index(
            "uq_telegram_file_id_cache_file_format_scope",
            "meme_file_id",
            "media_format",
            "bot_scope",
            unique=True,
        ),
    )

    meme_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meme_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    bot_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    media_format: Mapped[TelegramMediaFormat] = mapped_column(
        string_enum(TelegramMediaFormat),
        nullable=False,
    )
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False)

    meme_file: Mapped["MemeFile"] = relationship("MemeFile", back_populates="telegram_file_ids")


class EmbeddingCache(UUIDPrimaryKeyMixin, Base):
    """Persistent embedding cache used as the source of truth before Qdrant sync."""

    __tablename__ = "embedding_cache"
    __table_args__ = (
        UniqueConstraint(
            "input_hash",
            "model_version",
            "input_type",
            name="uq_embedding_cache_input_hash_model_version_input_type",
        ),
        Index("ix_embedding_cache_source_file_id", "source_file_id"),
    )

    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_type: Mapped[EmbeddingInputType] = mapped_column(
        string_enum(EmbeddingInputType),
        nullable=False,
    )
    embedding: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    model_version: Mapped[str] = mapped_column(String(255), nullable=False)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    source_file: Mapped["MemeFile | None"] = relationship("MemeFile", back_populates="embedding_cache_entries")


class AdminMemeDestructiveAuditLog(UUIDPrimaryKeyMixin, Base):
    """Durable admin audit for meme destructive actions that outlives meme deletion."""

    __tablename__ = "admin_meme_destructive_audit_logs"
    __table_args__ = (
        Index("ix_admin_meme_destructive_audit_logs_source_created_at", "source_meme_id", "created_at"),
        Index("ix_admin_meme_destructive_audit_logs_admin_created_at", "admin_user_id", "created_at"),
        Index("ix_admin_meme_destructive_audit_logs_action_created_at", "action", "created_at"),
    )

    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_meme_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_meme_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    affected_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class MemeMergeLog(UUIDPrimaryKeyMixin, Base):
    """Audit records for canonical meme merge decisions and moved lineage."""

    __tablename__ = "meme_merge_logs"
    __table_args__ = (
        Index("ix_meme_merge_logs_source_meme_id_created_at", "source_meme_id", "created_at"),
        Index("ix_meme_merge_logs_target_meme_id_created_at", "target_meme_id", "created_at"),
        Index("ix_meme_merge_logs_source_meme_file_id", "source_meme_file_id"),
    )

    source_meme_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_meme_file_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    target_meme_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_primary_file_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    merge_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


__all__ = [
    "AdminMemeDestructiveAuditLog",
    "BlockedPerceptualHash",
    "BlockedPerceptualHashAuditLog",
    "EmbeddingCache",
    "Meme",
    "MemeFile",
    "MemeFileOCRResult",
    "MemeFileSyncTargetSnapshot",
    "MemeMergeLog",
    "MemePopularitySnapshot",
    "MemeSeoPage",
    "MemeSource",
    "MemeSourceEngagementSnapshot",
    "MemeTemplate",
    "ModerationDecision",
    "ModerationReport",
    "PipelineStageJournal",
    "SourceChannel",
    "TelegramFileIdCache",
    "TelegramSessionState",
]
