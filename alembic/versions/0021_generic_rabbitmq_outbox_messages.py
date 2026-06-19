# ruff: noqa: E501,I001
"""generic RabbitMQ outbox messages"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


RABBITMQ_OUTBOX_MESSAGE_STATUS = sa.Enum(
    "pending",
    "publishing",
    "published",
    "failed",
    name="rabbitmqoutboxmessagestatus",
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

    op.drop_index("ix_pipeline_outbox_events_event_type_status", table_name="pipeline_outbox_events")
    op.drop_index("ix_pipeline_outbox_events_aggregate", table_name="pipeline_outbox_events")
    op.drop_index("ix_pipeline_outbox_events_status_retry_created", table_name="pipeline_outbox_events")
    op.drop_table("pipeline_outbox_events")

    op.create_table(
        "rabbitmq_outbox_messages",
        sa.Column("exchange", sa.String(length=255), nullable=False),
        sa.Column("routing_key", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("content_type", sa.String(length=128), server_default="application/json", nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("ordering_key", sa.String(length=255), nullable=True),
        sa.Column("status", RABBITMQ_OUTBOX_MESSAGE_STATUS, nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_owner", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_text", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_attempt_count_non_negative"),
        ),
        sa.CheckConstraint(
            "aggregate_id <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_aggregate_id_not_blank"),
        ),
        sa.CheckConstraint(
            "aggregate_type <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_aggregate_type_not_blank"),
        ),
        sa.CheckConstraint(
            "content_type <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_content_type_not_blank"),
        ),
        sa.CheckConstraint(
            "event_type <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_event_type_not_blank"),
        ),
        sa.CheckConstraint(
            "exchange <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_exchange_not_blank"),
        ),
        sa.CheckConstraint(
            "message_id <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_message_id_not_blank"),
        ),
        sa.CheckConstraint(
            "routing_key <> ''",
            name=op.f("ck_rabbitmq_outbox_messages_rabbitmq_outbox_messages_routing_key_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rabbitmq_outbox_messages")),
        sa.UniqueConstraint("message_id", name=op.f("uq_rabbitmq_outbox_messages_message_id")),
    )
    op.create_index(
        "ix_rabbitmq_outbox_messages_status_retry_created",
        "rabbitmq_outbox_messages",
        ["status", "next_retry_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rabbitmq_outbox_messages_aggregate",
        "rabbitmq_outbox_messages",
        ["aggregate_type", "aggregate_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rabbitmq_outbox_messages_event_status",
        "rabbitmq_outbox_messages",
        ["event_type", "status"],
        unique=False,
    )
    op.create_index(
        "ix_rabbitmq_outbox_messages_lease",
        "rabbitmq_outbox_messages",
        ["status", "locked_at"],
        unique=False,
    )
    op.create_index(
        "ix_rabbitmq_outbox_messages_ordering_key_created",
        "rabbitmq_outbox_messages",
        ["ordering_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_rabbitmq_outbox_messages_ordering_key_created", table_name="rabbitmq_outbox_messages")
    op.drop_index("ix_rabbitmq_outbox_messages_lease", table_name="rabbitmq_outbox_messages")
    op.drop_index("ix_rabbitmq_outbox_messages_event_status", table_name="rabbitmq_outbox_messages")
    op.drop_index("ix_rabbitmq_outbox_messages_aggregate", table_name="rabbitmq_outbox_messages")
    op.drop_index("ix_rabbitmq_outbox_messages_status_retry_created", table_name="rabbitmq_outbox_messages")
    op.drop_table("rabbitmq_outbox_messages")

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
