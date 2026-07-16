"""admin-managed search synonym catalogs

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-16
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

EMPTY_SYNONYM_HASH = "5ded1f291f1059cbadfdfe5249eb30f6282f1de841d6f50c774b0e2584fd1e62"
SYNONYM_COMPILER_VERSION = "meili_synonyms_v1"
EN_CATALOG_ID = uuid.UUID("01981a9d-0b8a-7000-8000-000000000001")
RU_CATALOG_ID = uuid.UUID("01981a9d-0b8a-7000-8000-000000000002")
EN_DRAFT_ID = uuid.UUID("01981a9d-0b8a-7000-8000-000000000011")
RU_DRAFT_ID = uuid.UUID("01981a9d-0b8a-7000-8000-000000000012")


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


SEARCH_SYNONYM_LOCALE = _enum("searchsynonymlocale", "en", "ru")
SEARCH_SYNONYM_REVISION_STATUS = _enum(
    "searchsynonymrevisionstatus",
    "draft",
    "published",
    "archived",
)
SEARCH_SYNONYM_SYNC_STATUS = _enum(
    "searchsynonymsyncstatus",
    "idle",
    "pending",
    "syncing",
    "synced",
    "failed",
)


def upgrade() -> None:
    """Create versioned locale catalogs and a durable combined sync state."""

    op.create_table(
        "search_synonym_catalogs",
        sa.Column("locale", SEARCH_SYNONYM_LOCALE, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("locale", name="uq_search_synonym_catalogs_locale"),
    )
    op.create_table(
        "search_synonym_revisions",
        sa.Column("catalog_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            SEARCH_SYNONYM_REVISION_STATUS,
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("source_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "compiled_synonyms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("compiled_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "validation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("change_note", sa.String(length=500), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("archived_by_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_search_synonym_revisions_search_synonym_revisions_revision_number_positive",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_search_synonym_revisions_search_synonym_revisions_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_admin_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["search_synonym_catalogs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["published_by_admin_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_admin_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_id",
            "revision_number",
            name="uq_search_synonym_revisions_catalog_number",
        ),
    )
    op.create_index(
        "ix_search_synonym_revisions_catalog_status_number",
        "search_synonym_revisions",
        ["catalog_id", "status", "revision_number"],
        unique=False,
    )
    op.create_index(
        "uq_search_synonym_revisions_one_draft",
        "search_synonym_revisions",
        ["catalog_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_index(
        "uq_search_synonym_revisions_one_published",
        "search_synonym_revisions",
        ["catalog_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "search_synonym_sync_states",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            SEARCH_SYNONYM_SYNC_STATUS,
            server_default=sa.text("'idle'"),
            nullable=False,
        ),
        sa.Column("desired_hash", sa.String(length=64), nullable=True),
        sa.Column("applied_hash", sa.String(length=64), nullable=True),
        sa.Column("actual_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "desired_revision_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "applied_revision_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("provider_task_uid", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_search_synonym_sync_states_search_synonym_sync_states_version_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    catalogs = sa.table(
        "search_synonym_catalogs",
        sa.column("id", sa.Uuid()),
        sa.column("locale", sa.String()),
    )
    op.bulk_insert(
        catalogs,
        [
            {"id": EN_CATALOG_ID, "locale": "en"},
            {"id": RU_CATALOG_ID, "locale": "ru"},
        ],
    )

    revisions = sa.table(
        "search_synonym_revisions",
        sa.column("id", sa.Uuid()),
        sa.column("catalog_id", sa.Uuid()),
        sa.column("revision_number", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("source_text", sa.Text()),
        sa.column("compiled_synonyms", postgresql.JSONB()),
        sa.column("compiler_version", sa.String()),
        sa.column("compiled_hash", sa.String()),
        sa.column("validation", postgresql.JSONB()),
        sa.column("stats", postgresql.JSONB()),
        sa.column("change_note", sa.String()),
        sa.column("version", sa.Integer()),
    )
    initial_validation = {
        "valid": False,
        "issues": [
            {
                "level": "error",
                "code": "catalog_requires_compiled_key",
                "message": "A published synonym catalog must compile at least one active key.",
                "line_number": None,
                "term": None,
            }
        ],
    }
    initial_stats = {
        "group_count": 0,
        "term_count": 0,
        "compiled_key_count": 0,
        "edge_count": 0,
        "target_only_term_count": 0,
        "payload_bytes": 2,
        "error_count": 1,
        "warning_count": 0,
    }
    op.bulk_insert(
        revisions,
        [
            {
                "id": EN_DRAFT_ID,
                "catalog_id": EN_CATALOG_ID,
                "revision_number": 1,
                "status": "draft",
                "source_text": "",
                "compiled_synonyms": {},
                "compiler_version": SYNONYM_COMPILER_VERSION,
                "compiled_hash": EMPTY_SYNONYM_HASH,
                "validation": initial_validation,
                "stats": initial_stats,
                "change_note": "Initial empty English draft.",
                "version": 1,
            },
            {
                "id": RU_DRAFT_ID,
                "catalog_id": RU_CATALOG_ID,
                "revision_number": 1,
                "status": "draft",
                "source_text": "",
                "compiled_synonyms": {},
                "compiler_version": SYNONYM_COMPILER_VERSION,
                "compiled_hash": EMPTY_SYNONYM_HASH,
                "validation": initial_validation,
                "stats": initial_stats,
                "change_note": "Initial empty Russian draft.",
                "version": 1,
            },
        ],
    )

    sync_states = sa.table(
        "search_synonym_sync_states",
        sa.column("id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("desired_revision_ids", postgresql.JSONB()),
        sa.column("applied_revision_ids", postgresql.JSONB()),
        sa.column("version", sa.Integer()),
    )
    op.bulk_insert(
        sync_states,
        [
            {
                "id": "meilisearch",
                "status": "idle",
                "desired_revision_ids": {},
                "applied_revision_ids": {},
                "version": 1,
            }
        ],
    )


def downgrade() -> None:
    """Remove the admin-managed synonym source of truth."""

    op.drop_table("search_synonym_sync_states")
    op.drop_index("uq_search_synonym_revisions_one_published", table_name="search_synonym_revisions")
    op.drop_index("uq_search_synonym_revisions_one_draft", table_name="search_synonym_revisions")
    op.drop_index(
        "ix_search_synonym_revisions_catalog_status_number",
        table_name="search_synonym_revisions",
    )
    op.drop_table("search_synonym_revisions")
    op.drop_table("search_synonym_catalogs")
