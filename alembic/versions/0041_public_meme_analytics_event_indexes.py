"""index public meme analytics event lookups

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-20
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Index both supported meme-reference shapes for bounded public reads."""

    op.create_index(
        "ix_analytics_events_refs_meme_event_occurred",
        "analytics_events",
        [
            sa.text("((payload['refs']) ->> 'meme_id')"),
            "event_type",
            "occurred_at",
        ],
        unique=False,
    )
    op.create_index(
        "ix_analytics_events_legacy_meme_event_occurred",
        "analytics_events",
        [
            sa.text("(payload ->> 'meme_id')"),
            "event_type",
            "occurred_at",
        ],
        unique=False,
    )


def downgrade() -> None:
    """Remove public meme analytics event lookup indexes."""

    op.drop_index(
        "ix_analytics_events_legacy_meme_event_occurred",
        table_name="analytics_events",
    )
    op.drop_index(
        "ix_analytics_events_refs_meme_event_occurred",
        table_name="analytics_events",
    )
