# ruff: noqa: F821,TC003,UP037
"""Content, SEO, source, popularity, and cache ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    EmbeddingInputType,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramMediaFormat,
    string_enum,
)

if TYPE_CHECKING:
    from memexpert.models.collection import CollectionMeme, PinnedMeme
    from memexpert.models.user import User


class Meme(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Conceptual meme entity that can own many underlying media files."""

    __tablename__ = "memes"
    __table_args__ = (
        Index("ix_memes_media_type_language", "media_type", "language"),
        Index("ix_memes_visibility_popularity_created_at", "is_public", "popularity_score", "created_at"),
        Index("ix_memes_author_user_id_created_at", "author_user_id", "created_at"),
    )

    media_type: Mapped[ContentKind] = mapped_column(
        string_enum(ContentKind),
        nullable=False,
    )
    primary_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meme_files.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
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
        back_populates="primary_for_meme",
        foreign_keys=[primary_file_id],
        post_update=True,
        uselist=False,
    )
    files: Mapped[list["MemeFile"]] = relationship(
        "MemeFile",
        back_populates="meme",
        foreign_keys="MemeFile.meme_id",
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


class MemeFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A specific media-file instance belonging to a meme."""

    __tablename__ = "meme_files"
    __table_args__ = (
        Index("ix_meme_files_meme_id_status", "meme_id", "status"),
        Index("ix_meme_files_perceptual_hash", "perceptual_hash"),
        Index(
            "uq_meme_files_single_primary_per_meme",
            "meme_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
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
    quality_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    blur_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)

    meme: Mapped["Meme"] = relationship(
        "Meme",
        back_populates="files",
        foreign_keys=[meme_id],
    )
    primary_for_meme: Mapped["Meme | None"] = relationship(
        "Meme",
        foreign_keys="Meme.primary_file_id",
        uselist=False,
        viewonly=True,
    )
    sources: Mapped[list["MemeSource"]] = relationship(
        "MemeSource",
        back_populates="file",
        cascade="all, delete-orphan",
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
        Index("ix_meme_sources_file_id_platform", "file_id", "platform"),
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

    file: Mapped["MemeFile"] = relationship("MemeFile", back_populates="sources")


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
    "EmbeddingCache",
    "Meme",
    "MemeFile",
    "MemeFileOCRResult",
    "MemeFileSyncTargetSnapshot",
    "MemeMergeLog",
    "MemePopularitySnapshot",
    "MemeSeoPage",
    "MemeSource",
    "MemeTemplate",
    "PipelineStageJournal",
    "SourceChannel",
    "TelegramFileIdCache",
]
