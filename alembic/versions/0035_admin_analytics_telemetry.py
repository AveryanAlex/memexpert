"""admin analytics telemetry indexes and page views

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-15
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_PREVIOUS_ANALYTICS_EVENT_TYPES = (
    "account_merge",
    "auth_event",
    "channel_suggest",
    "click",
    "collection_action",
    "favorite",
    "impression",
    "inline_chosen",
    "inline_query",
    "inline_sent",
    "inline_served",
    "meme_detail_click",
    "meme_download",
    "meme_impression",
    "meme_like",
    "meme_pin",
    "meme_report",
    "meme_save",
    "meme_send",
    "meme_share",
    "meme_view",
    "miniapp_open",
    "save",
    "search_query",
    "share",
    "view",
)
_ANALYTICS_EVENT_TYPES = (*_PREVIOUS_ANALYTICS_EVENT_TYPES, "page_view")


def upgrade() -> None:
    """Allow privacy-bounded page views and speed bounded analytics reads."""

    _replace_analytics_event_type_constraint(_ANALYTICS_EVENT_TYPES)
    op.create_index("ix_analytics_events_occurred_at", "analytics_events", ["occurred_at"], unique=False)


def downgrade() -> None:
    """Remove page-view support and the bounded-range timestamp index."""

    op.drop_index("ix_analytics_events_occurred_at", table_name="analytics_events")
    op.execute("DELETE FROM analytics_events WHERE event_type = 'page_view'")
    _replace_analytics_event_type_constraint(_PREVIOUS_ANALYTICS_EVENT_TYPES)


def _replace_analytics_event_type_constraint(event_types: tuple[str, ...]) -> None:
    joined_values = ", ".join(f"'{event_type}'" for event_type in event_types)
    op.execute("ALTER TABLE analytics_events DROP CONSTRAINT IF EXISTS analyticseventtype")
    op.execute("ALTER TABLE analytics_events DROP CONSTRAINT IF EXISTS ck_analytics_events_analyticseventtype")
    op.execute(
        f"ALTER TABLE analytics_events ADD CONSTRAINT analyticseventtype CHECK (event_type IN ({joined_values}))"
    )
