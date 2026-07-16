# ruff: noqa: TC003
"""Versioned PostgreSQL source of truth for Meilisearch synonyms."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from memexpert.models.enums import (
    SearchSynonymLocale,
    SearchSynonymRevisionStatus,
    SearchSynonymSyncStatus,
    string_enum,
)

if TYPE_CHECKING:
    from memexpert.models.user import User


class SearchSynonymCatalog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A locale-specific synonym catalog with one mutable draft."""

    __tablename__ = "search_synonym_catalogs"
    __table_args__ = (UniqueConstraint("locale", name="uq_search_synonym_catalogs_locale"),)

    locale: Mapped[SearchSynonymLocale] = mapped_column(
        string_enum(SearchSynonymLocale),
        nullable=False,
    )

    revisions: Mapped[list[SearchSynonymRevision]] = relationship(
        back_populates="catalog",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SearchSynonymRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One draft or immutable published/archived synonym snapshot."""

    __tablename__ = "search_synonym_revisions"
    __table_args__ = (
        UniqueConstraint(
            "catalog_id",
            "revision_number",
            name="uq_search_synonym_revisions_catalog_number",
        ),
        CheckConstraint(
            "revision_number >= 1",
            name="search_synonym_revisions_revision_number_positive",
        ),
        CheckConstraint("version >= 1", name="search_synonym_revisions_version_positive"),
        Index(
            "uq_search_synonym_revisions_one_draft",
            "catalog_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
        Index(
            "uq_search_synonym_revisions_one_published",
            "catalog_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        Index(
            "ix_search_synonym_revisions_catalog_status_number",
            "catalog_id",
            "status",
            "revision_number",
        ),
    )

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_synonym_catalogs.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SearchSynonymRevisionStatus] = mapped_column(
        string_enum(SearchSynonymRevisionStatus),
        default=SearchSynonymRevisionStatus.DRAFT,
        server_default=text("'draft'"),
        nullable=False,
    )
    source_text: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    compiled_synonyms: Mapped[dict[str, list[str]]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    compiled_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    stats: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    change_note: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    created_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    published_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    archived_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)

    catalog: Mapped[SearchSynonymCatalog] = relationship(back_populates="revisions")
    created_by_admin_user: Mapped[User | None] = relationship(foreign_keys=[created_by_admin_user_id])
    updated_by_admin_user: Mapped[User | None] = relationship(foreign_keys=[updated_by_admin_user_id])
    published_by_admin_user: Mapped[User | None] = relationship(foreign_keys=[published_by_admin_user_id])
    archived_by_admin_user: Mapped[User | None] = relationship(foreign_keys=[archived_by_admin_user_id])


class SearchSynonymSyncState(TimestampMixin, Base):
    """Singleton durable state for the combined Meilisearch synonym map."""

    __tablename__ = "search_synonym_sync_states"
    __table_args__ = (
        CheckConstraint("version >= 1", name="search_synonym_sync_states_version_positive"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[SearchSynonymSyncStatus] = mapped_column(
        string_enum(SearchSynonymSyncStatus),
        default=SearchSynonymSyncStatus.IDLE,
        server_default=text("'idle'"),
        nullable=False,
    )
    desired_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applied_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_revision_ids: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    applied_revision_ids: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    provider_task_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)


__all__ = [
    "SearchSynonymCatalog",
    "SearchSynonymRevision",
    "SearchSynonymSyncState",
]
