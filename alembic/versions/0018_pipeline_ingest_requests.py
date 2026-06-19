# ruff: noqa: E501,I001,TC003
"""pipeline ingest requests and outbox"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


SOURCE_PLATFORM = sa.Enum(
    "reddit",
    "telegram",
    "vk",
    name="sourceplatform",
    native_enum=False,
    create_constraint=True,
)

SOURCE_ATTACH_REASON = sa.Enum(
    "new_file",
    "sha256_exact_existing_file",
    "phash_exact_new_file",
    "blocked_sha256_existing_file",
    "blocked_perceptual_hash_new_file",
    name="sourceattachreason",
    native_enum=False,
    create_constraint=True,
)

PIPELINE_INGEST_REQUEST_STATUS = sa.Enum(
    "accepted",
    "media_inspect_pending",
    "media_inspecting",
    "materialized",
    "resolved_sha_duplicate",
    "failed_invalid_media",
    "failed_blocked_phash",
    "publish_failed",
    name="pipelineingestrequeststatus",
    native_enum=False,
    create_constraint=True,
)

PIPELINE_OUTBOX_EVENT_STATUS = sa.Enum(
    "pending",
    "publishing",
    "published",
    "failed",
    name="pipelineoutboxeventstatus",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Apply this revision."""

    op.create_table(
        "pipeline_ingest_requests",
        sa.Column("source_platform", SOURCE_PLATFORM, nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("post_id", sa.String(length=255), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "user_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("declared_filename", sa.String(length=255), nullable=True),
        sa.Column("declared_content_type", sa.String(length=255), nullable=True),
        sa.Column("temp_original_object_key", sa.Text(), nullable=True),
        sa.Column("sha256_hex", sa.String(length=64), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", PIPELINE_INGEST_REQUEST_STATUS, nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("materialized_meme_id", sa.Uuid(), nullable=True),
        sa.Column("materialized_meme_file_id", sa.Uuid(), nullable=True),
        sa.Column("matched_meme_file_id", sa.Uuid(), nullable=True),
        sa.Column("source_attach_reason", SOURCE_ATTACH_REASON, nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "sha256_hex IS NULL OR sha256_hex = lower(sha256_hex)",
            name=op.f("ck_pipeline_ingest_requests_pipeline_ingest_requests_sha256_hex_lowercase"),
        ),
        sa.CheckConstraint(
            "sha256_hex IS NULL OR sha256_hex ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_pipeline_ingest_requests_pipeline_ingest_requests_sha256_hex_hex"),
        ),
        sa.CheckConstraint(
            "file_size_bytes IS NULL OR file_size_bytes >= 0",
            name=op.f("ck_pipeline_ingest_requests_pipeline_ingest_requests_file_size_non_negative"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_pipeline_ingest_requests_pipeline_ingest_requests_attempt_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_pipeline_ingest_requests_owner_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["materialized_meme_id"],
            ["memes.id"],
            name=op.f("fk_pipeline_ingest_requests_materialized_meme_id_memes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["materialized_meme_file_id"],
            ["meme_files.id"],
            name=op.f("fk_pipeline_ingest_requests_materialized_meme_file_id_meme_files"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["matched_meme_file_id"],
            ["meme_files.id"],
            name=op.f("fk_pipeline_ingest_requests_matched_meme_file_id_meme_files"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_ingest_requests")),
        sa.UniqueConstraint(
            "source_platform",
            "source_id",
            "post_id",
            name="uq_pipeline_ingest_requests_source_identity",
        ),
    )
    op.create_index("ix_pipeline_ingest_requests_sha256_hex", "pipeline_ingest_requests", ["sha256_hex"], unique=False)
    op.create_index(
        "ix_pipeline_ingest_requests_status_created_at",
        "pipeline_ingest_requests",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_ingest_requests_materialized_meme_id",
        "pipeline_ingest_requests",
        ["materialized_meme_id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_ingest_requests_materialized_meme_file_id",
        "pipeline_ingest_requests",
        ["materialized_meme_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_ingest_requests_matched_meme_file_id",
        "pipeline_ingest_requests",
        ["matched_meme_file_id"],
        unique=False,
    )

    op.create_table(
        "pipeline_outbox_events",
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("routing_key", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", PIPELINE_OUTBOX_EVENT_STATUS, nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_pipeline_outbox_events_pipeline_outbox_events_attempt_count_non_negative"),
        ),
        sa.CheckConstraint(
            "aggregate_type <> ''",
            name=op.f("ck_pipeline_outbox_events_pipeline_outbox_events_aggregate_type_not_blank"),
        ),
        sa.CheckConstraint(
            "event_type <> ''",
            name=op.f("ck_pipeline_outbox_events_pipeline_outbox_events_event_type_not_blank"),
        ),
        sa.CheckConstraint(
            "routing_key <> ''",
            name=op.f("ck_pipeline_outbox_events_pipeline_outbox_events_routing_key_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_outbox_events")),
    )
    op.create_index(
        "ix_pipeline_outbox_events_status_retry_created",
        "pipeline_outbox_events",
        ["status", "next_retry_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_outbox_events_aggregate",
        "pipeline_outbox_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_outbox_events_event_type_status",
        "pipeline_outbox_events",
        ["event_type", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_pipeline_outbox_events_event_type_status", table_name="pipeline_outbox_events")
    op.drop_index("ix_pipeline_outbox_events_aggregate", table_name="pipeline_outbox_events")
    op.drop_index("ix_pipeline_outbox_events_status_retry_created", table_name="pipeline_outbox_events")
    op.drop_table("pipeline_outbox_events")

    op.drop_index("ix_pipeline_ingest_requests_matched_meme_file_id", table_name="pipeline_ingest_requests")
    op.drop_index("ix_pipeline_ingest_requests_materialized_meme_file_id", table_name="pipeline_ingest_requests")
    op.drop_index("ix_pipeline_ingest_requests_materialized_meme_id", table_name="pipeline_ingest_requests")
    op.drop_index("ix_pipeline_ingest_requests_status_created_at", table_name="pipeline_ingest_requests")
    op.drop_index("ix_pipeline_ingest_requests_sha256_hex", table_name="pipeline_ingest_requests")
    op.drop_table("pipeline_ingest_requests")
