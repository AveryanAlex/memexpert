"""add idempotent public meme exposure facts

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-20
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create privacy-bounded exposure/funnel facts and backfill keyed events."""

    op.create_table(
        "meme_exposures",
        sa.Column("meme_id", sa.Uuid(), nullable=False),
        sa.Column("exposure_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("exposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("high_intent_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inline_chosen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inline_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("exposure_key <> ''", name="ck_meme_exposures_key_not_blank"),
        sa.CheckConstraint(
            "kind IN ('web_card', 'telegram_inline')",
            name="ck_meme_exposures_kind_supported",
        ),
        sa.CheckConstraint(
            "kind = 'web_card' OR detail_clicked_at IS NULL",
            name="ck_meme_exposures_detail_click_web_only",
        ),
        sa.CheckConstraint(
            "kind = 'web_card' OR high_intent_action_at IS NULL",
            name="ck_meme_exposures_high_intent_web_only",
        ),
        sa.CheckConstraint(
            "kind = 'telegram_inline' OR inline_chosen_at IS NULL",
            name="ck_meme_exposures_inline_chosen_inline_only",
        ),
        sa.CheckConstraint(
            "kind = 'telegram_inline' OR inline_sent_at IS NULL",
            name="ck_meme_exposures_inline_sent_inline_only",
        ),
        sa.ForeignKeyConstraint(["meme_id"], ["memes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_meme_exposures"),
        sa.UniqueConstraint(
            "meme_id",
            "kind",
            "exposure_key",
            name="uq_meme_exposures_meme_kind_key",
        ),
    )
    op.create_index(
        "ix_meme_exposures_meme_kind_exposed",
        "meme_exposures",
        ["meme_id", "kind", "exposed_at"],
        unique=False,
    )
    op.create_index(
        "ix_meme_exposures_meme_kind_converted",
        "meme_exposures",
        ["meme_id", "kind", "detail_clicked_at", "inline_chosen_at", "inline_sent_at"],
        unique=False,
    )

    # Only public-safe exposure tokens and meme UUIDs are copied. Actor,
    # request, query, surface, and raw property data intentionally stay in the
    # protected analytics stream and never enter this public read foundation.
    op.execute(
        sa.text(
            """
            WITH normalized AS (
                SELECT
                    ae.event_type::text AS event_type,
                    ae.occurred_at,
                    CASE
                        WHEN jsonb_typeof(ae.payload -> 'refs' -> 'meme_id') = 'string'
                         AND ae.payload -> 'refs' ->> 'meme_id'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN (ae.payload -> 'refs' ->> 'meme_id')::uuid
                        WHEN jsonb_typeof(ae.payload -> 'meme_id') = 'string'
                         AND ae.payload ->> 'meme_id'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN (ae.payload ->> 'meme_id')::uuid
                        ELSE NULL
                    END AS meme_id,
                    NULLIF(btrim(ae.payload ->> 'impression_id'), '') AS exposure_key,
                    COALESCE(ae.payload ->> 'surface', '') AS surface
                FROM analytics_events ae
                WHERE ae.event_type::text IN (
                    'impression', 'meme_impression', 'meme_detail_click', 'click',
                    'meme_download', 'meme_like', 'favorite', 'meme_save', 'save',
                    'meme_share', 'share', 'meme_send', 'inline_served',
                    'inline_chosen', 'inline_sent'
                )
            ),
            exposed AS (
                SELECT
                    meme_id,
                    exposure_key,
                    CASE
                        WHEN event_type = 'inline_served' THEN 'telegram_inline'
                        ELSE 'web_card'
                    END AS kind,
                    min(occurred_at) AS exposed_at
                FROM normalized
                WHERE meme_id IS NOT NULL
                  AND exposure_key IS NOT NULL
                  AND char_length(exposure_key) <= 255
                  AND EXISTS (SELECT 1 FROM memes WHERE memes.id = normalized.meme_id)
                  AND event_type IN ('impression', 'meme_impression', 'inline_served')
                GROUP BY meme_id, exposure_key,
                    CASE WHEN event_type = 'inline_served' THEN 'telegram_inline' ELSE 'web_card' END
            )
            INSERT INTO meme_exposures (
                id,
                meme_id,
                exposure_key,
                kind,
                exposed_at,
                detail_clicked_at,
                high_intent_action_at,
                inline_chosen_at,
                inline_sent_at,
                created_at,
                updated_at
            )
            SELECT
                md5(e.meme_id::text || ':' || e.kind || ':' || e.exposure_key)::uuid,
                e.meme_id,
                e.exposure_key,
                e.kind,
                e.exposed_at,
                min(n.occurred_at) FILTER (
                    WHERE e.kind = 'web_card'
                      AND n.event_type IN ('meme_detail_click', 'click')
                ),
                min(n.occurred_at) FILTER (
                    WHERE e.kind = 'web_card'
                      AND n.event_type IN (
                          'meme_download', 'meme_like', 'favorite', 'meme_save',
                          'save', 'meme_share', 'share', 'meme_send'
                      )
                      AND n.surface NOT LIKE 'telegram_inline%'
                ),
                min(n.occurred_at) FILTER (
                    WHERE e.kind = 'telegram_inline'
                      AND n.event_type = 'inline_chosen'
                ),
                min(n.occurred_at) FILTER (
                    WHERE e.kind = 'telegram_inline'
                      AND n.event_type = 'inline_sent'
                ),
                now(),
                now()
            FROM exposed e
            LEFT JOIN normalized n
              ON n.meme_id = e.meme_id
             AND n.exposure_key = e.exposure_key
            GROUP BY e.meme_id, e.exposure_key, e.kind, e.exposed_at
            ON CONFLICT (meme_id, kind, exposure_key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    """Drop idempotent public exposure facts."""

    op.drop_index("ix_meme_exposures_meme_kind_converted", table_name="meme_exposures")
    op.drop_index("ix_meme_exposures_meme_kind_exposed", table_name="meme_exposures")
    op.drop_table("meme_exposures")
