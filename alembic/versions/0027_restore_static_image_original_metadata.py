# ruff: noqa: E501,TC003
"""restore static image original metadata"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Repair rows where static image originals were marked as video/mp4 derivatives."""

    op.execute(
        """
        UPDATE meme_files AS f
        SET
            mime_type = CASE
                WHEN lower(f.s3_original_key) LIKE '%.jpg' THEN 'image/jpeg'
                WHEN lower(f.s3_original_key) LIKE '%.jpeg' THEN 'image/jpeg'
                WHEN lower(f.s3_original_key) LIKE '%.png' THEN 'image/png'
                WHEN lower(f.s3_original_key) LIKE '%.webp' THEN 'image/webp'
                ELSE f.mime_type
            END,
            s3_web_video_key = NULL
        FROM memes AS m
        WHERE m.id = f.meme_id
          AND m.media_type = 'image'
          AND (
              lower(f.s3_original_key) LIKE '%.jpg'
              OR lower(f.s3_original_key) LIKE '%.jpeg'
              OR lower(f.s3_original_key) LIKE '%.png'
              OR lower(f.s3_original_key) LIKE '%.webp'
          )
        """
    )


def downgrade() -> None:
    """Do not re-corrupt original MIME metadata on downgrade."""

    return None
