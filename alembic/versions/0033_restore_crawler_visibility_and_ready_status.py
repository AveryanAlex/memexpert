# ruff: noqa: E501,I001,TC003
"""restore legacy crawler visibility and post-classify readiness"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Repair state produced by legacy crawler defaults and sync-stage demotion."""

    op.execute(
        sa.text(
            """
            UPDATE memes AS meme
            SET visibility_mode = 'auto',
                is_public = TRUE,
                updated_at = now()
            WHERE meme.visibility_mode = 'force_private'
              AND EXISTS (
                  SELECT 1
                  FROM meme_files AS file
                  JOIN meme_sources AS source ON source.file_id = file.id
                  WHERE file.meme_id = meme.id
                    AND source.source_kind = 'public_crawler'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM meme_files AS file
                  WHERE file.meme_id = meme.id
                    AND file.blocked_perceptual_hash_id IS NOT NULL
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM moderation_decisions AS decision
                  WHERE decision.meme_id = meme.id
                    AND (
                        decision.action IN ('publish', 'hide', 'hide_and_mark_nsfw')
                        OR (
                            decision.action = 'override_flags'
                            AND decision.new_is_public IS DISTINCT FROM decision.previous_is_public
                        )
                    )
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE meme_files AS file
            SET status = 'ready',
                updated_at = now()
            WHERE file.status IN ('processing', 'failed')
              AND EXISTS (
                  SELECT 1
                  FROM pipeline_stage_journal AS classify
                  WHERE classify.meme_file_id = file.id
                    AND classify.stage = 'classify'
                    AND classify.status = 'succeeded'
              )
            """
        )
    )


def downgrade() -> None:
    """Keep the repaired data when moving back to the preceding schema revision."""
