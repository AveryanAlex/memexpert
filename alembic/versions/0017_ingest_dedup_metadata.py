# ruff: noqa: E501,I001
"""ingest dedup metadata"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


INGEST_FILE_ORIGIN = sa.Enum(
    "new_meme",
    "phash_exact_existing_meme",
    "blocked_perceptual_hash",
    name="ingestfileorigin",
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


def upgrade() -> None:
    """Apply this revision."""

    op.add_column("meme_files", sa.Column("sha256_hex", sa.String(length=64), nullable=True))
    op.add_column("meme_files", sa.Column("ingest_origin", INGEST_FILE_ORIGIN, nullable=True))
    op.add_column("meme_files", sa.Column("matched_meme_file_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        op.f("ck_meme_files_meme_files_sha256_hex_lowercase"),
        "meme_files",
        "sha256_hex IS NULL OR sha256_hex = lower(sha256_hex)",
    )
    op.create_check_constraint(
        op.f("ck_meme_files_meme_files_sha256_hex_hex"),
        "meme_files",
        "sha256_hex IS NULL OR sha256_hex ~ '^[0-9a-f]{64}$'",
    )
    op.create_unique_constraint("uq_meme_files_sha256_hex", "meme_files", ["sha256_hex"])
    op.create_foreign_key(
        op.f("fk_meme_files_matched_meme_file_id_meme_files"),
        "meme_files",
        "meme_files",
        ["matched_meme_file_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_meme_files_ingest_origin", "meme_files", ["ingest_origin"], unique=False)
    op.create_index("ix_meme_files_matched_meme_file_id", "meme_files", ["matched_meme_file_id"], unique=False)

    op.add_column("meme_sources", sa.Column("attach_reason", SOURCE_ATTACH_REASON, nullable=True))
    op.add_column("meme_sources", sa.Column("matched_meme_file_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_meme_sources_matched_meme_file_id_meme_files"),
        "meme_sources",
        "meme_files",
        ["matched_meme_file_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_meme_sources_attach_reason", "meme_sources", ["attach_reason"], unique=False)
    op.create_index("ix_meme_sources_matched_meme_file_id", "meme_sources", ["matched_meme_file_id"], unique=False)


def downgrade() -> None:
    """Revert this revision."""

    op.drop_index("ix_meme_sources_matched_meme_file_id", table_name="meme_sources")
    op.drop_index("ix_meme_sources_attach_reason", table_name="meme_sources")
    op.drop_constraint(op.f("fk_meme_sources_matched_meme_file_id_meme_files"), "meme_sources", type_="foreignkey")
    op.drop_column("meme_sources", "matched_meme_file_id")
    op.drop_column("meme_sources", "attach_reason")

    op.drop_index("ix_meme_files_matched_meme_file_id", table_name="meme_files")
    op.drop_index("ix_meme_files_ingest_origin", table_name="meme_files")
    op.drop_constraint(op.f("fk_meme_files_matched_meme_file_id_meme_files"), "meme_files", type_="foreignkey")
    op.drop_constraint("uq_meme_files_sha256_hex", "meme_files", type_="unique")
    op.drop_constraint(op.f("ck_meme_files_meme_files_sha256_hex_hex"), "meme_files", type_="check")
    op.drop_constraint(op.f("ck_meme_files_meme_files_sha256_hex_lowercase"), "meme_files", type_="check")
    op.drop_column("meme_files", "matched_meme_file_id")
    op.drop_column("meme_files", "ingest_origin")
    op.drop_column("meme_files", "sha256_hex")
