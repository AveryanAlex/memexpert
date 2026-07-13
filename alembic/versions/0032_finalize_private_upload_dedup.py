# ruff: noqa: E501,I001,TC003
"""restore global SHA uniqueness and remove singular ownership"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Finalize the model after the resumable SHA reconciliation has completed."""

    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM meme_files
                    WHERE sha256_hex IS NOT NULL
                    GROUP BY sha256_hex
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'duplicate meme_files.sha256_hex values remain; run memexpert-reconcile-sha-duplicates before upgrading to 0032';
                END IF;
            END;
            $$
            """
        )
    )

    op.drop_index("ix_meme_files_sha256_hex", table_name="meme_files")
    op.create_index(
        "uq_meme_files_sha256_hex_not_null",
        "meme_files",
        ["sha256_hex"],
        unique=True,
        postgresql_where=sa.text("sha256_hex IS NOT NULL"),
    )

    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_meme_sources_legacy_author ON meme_sources"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS memexpert_sync_legacy_meme_author()"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_pipeline_ingest_requests_legacy_owner ON pipeline_ingest_requests"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS memexpert_sync_legacy_ingest_owner()"))

    op.drop_index("ix_memes_author_user_id_created_at", table_name="memes")
    op.drop_constraint(op.f("fk_memes_author_user_id_users"), "memes", type_="foreignkey")
    op.drop_column("memes", "author_user_id")
    op.drop_constraint(
        op.f("fk_pipeline_ingest_requests_owner_user_id_users"),
        "pipeline_ingest_requests",
        type_="foreignkey",
    )
    op.drop_column("pipeline_ingest_requests", "owner_user_id")


def downgrade() -> None:
    """Restore nullable compatibility ownership columns and non-unique SHA lookup."""

    op.add_column("pipeline_ingest_requests", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_pipeline_ingest_requests_owner_user_id_users"),
        "pipeline_ingest_requests",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(sa.text("UPDATE pipeline_ingest_requests SET owner_user_id = uploader_user_id"))

    op.add_column("memes", sa.Column("author_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_memes_author_user_id_users"),
        "memes",
        "users",
        ["author_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_memes_author_user_id_created_at", "memes", ["author_user_id", "created_at"])
    op.execute(
        sa.text(
            """
            UPDATE memes AS meme
            SET author_user_id = uploader.user_id
            FROM (
                SELECT file.meme_id, min(source.uploader_user_id::text)::uuid AS user_id
                FROM meme_files AS file
                JOIN meme_sources AS source ON source.file_id = file.id
                WHERE source.uploader_user_id IS NOT NULL
                GROUP BY file.meme_id
                HAVING count(DISTINCT source.uploader_user_id) = 1
            ) AS uploader
            WHERE uploader.meme_id = meme.id
            """
        )
    )

    op.drop_index("uq_meme_files_sha256_hex_not_null", table_name="meme_files")
    op.create_index("ix_meme_files_sha256_hex", "meme_files", ["sha256_hex"])

    op.execute(
        sa.text(
            """
            CREATE FUNCTION memexpert_sync_legacy_ingest_owner() RETURNS trigger AS $$
            BEGIN
                IF NEW.uploader_user_id IS NULL THEN NEW.uploader_user_id := NEW.owner_user_id; END IF;
                IF NEW.owner_user_id IS NULL THEN NEW.owner_user_id := NEW.uploader_user_id; END IF;
                IF NEW.uploader_user_id IS NOT NULL THEN NEW.source_kind := 'user_upload'; END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_pipeline_ingest_requests_legacy_owner
            BEFORE INSERT OR UPDATE OF owner_user_id, uploader_user_id, source_kind
            ON pipeline_ingest_requests
            FOR EACH ROW EXECUTE FUNCTION memexpert_sync_legacy_ingest_owner()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION memexpert_sync_legacy_meme_author() RETURNS trigger AS $$
            BEGIN
                IF NEW.source_kind = 'user_upload' AND NEW.uploader_user_id IS NOT NULL THEN
                    UPDATE memes AS meme
                    SET author_user_id = COALESCE(meme.author_user_id, NEW.uploader_user_id)
                    FROM meme_files AS file
                    WHERE file.id = NEW.file_id AND meme.id = file.meme_id;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_meme_sources_legacy_author
            AFTER INSERT OR UPDATE OF uploader_user_id, source_kind
            ON meme_sources
            FOR EACH ROW EXECUTE FUNCTION memexpert_sync_legacy_meme_author()
            """
        )
    )
