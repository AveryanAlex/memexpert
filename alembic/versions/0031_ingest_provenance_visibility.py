# ruff: noqa: E501,I001,TC003
"""add ingest provenance and visibility policy fields"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


INGEST_SOURCE_KIND = sa.String(length=32)
MEME_VISIBILITY_MODE = sa.String(length=32)


def upgrade() -> None:
    """Add and backfill provenance while legacy ownership columns remain available."""

    op.add_column("memes", sa.Column("visibility_mode", MEME_VISIBILITY_MODE, nullable=True))
    op.add_column("pipeline_ingest_requests", sa.Column("source_kind", INGEST_SOURCE_KIND, nullable=True))
    op.add_column("pipeline_ingest_requests", sa.Column("uploader_user_id", sa.Uuid(), nullable=True))
    op.add_column("meme_sources", sa.Column("source_kind", INGEST_SOURCE_KIND, nullable=True))
    op.add_column("meme_sources", sa.Column("uploader_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "moderation_decisions",
        sa.Column("previous_visibility_mode", MEME_VISIBILITY_MODE, nullable=True),
    )
    op.add_column(
        "moderation_decisions",
        sa.Column("new_visibility_mode", MEME_VISIBILITY_MODE, nullable=True),
    )
    op.create_check_constraint(
        "ck_memes_visibility_mode_known",
        "memes",
        "visibility_mode IN ('auto', 'force_public', 'force_private')",
    )
    op.create_check_constraint(
        "ck_pipeline_ingest_requests_source_kind_known",
        "pipeline_ingest_requests",
        "source_kind IN ('user_upload', 'public_crawler', 'operator_upload')",
    )
    op.create_check_constraint(
        "ck_meme_sources_source_kind_known",
        "meme_sources",
        "source_kind IN ('user_upload', 'public_crawler', 'operator_upload')",
    )
    op.create_check_constraint(
        "ck_moderation_decisions_visibility_modes_known",
        "moderation_decisions",
        "previous_visibility_mode IN ('auto', 'force_public', 'force_private') "
        "AND new_visibility_mode IN ('auto', 'force_public', 'force_private')",
    )

    op.create_foreign_key(
        op.f("fk_pipeline_ingest_requests_uploader_user_id_users"),
        "pipeline_ingest_requests",
        "users",
        ["uploader_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_meme_sources_uploader_user_id_users"),
        "meme_sources",
        "users",
        ["uploader_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(
        sa.text(
            """
            UPDATE pipeline_ingest_requests AS request
            SET uploader_user_id = request.owner_user_id,
                source_kind = CASE
                    WHEN request.owner_user_id IS NOT NULL THEN 'user_upload'
                    WHEN EXISTS (
                        SELECT 1
                        FROM source_channels AS channel
                        WHERE channel.platform = request.source_platform
                          AND channel.platform_id = request.source_id
                    ) THEN 'public_crawler'
                    ELSE 'operator_upload'
                END
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH resolved_source AS (
                SELECT
                    source.id,
                    COALESCE(request.uploader_user_id, meme.author_user_id) AS uploader_user_id,
                    COALESCE(
                        request.source_kind,
                        CASE
                            WHEN meme.author_user_id IS NOT NULL THEN 'user_upload'
                            WHEN channel.id IS NOT NULL THEN 'public_crawler'
                            ELSE 'operator_upload'
                        END
                    ) AS source_kind
                FROM meme_sources AS source
                JOIN meme_files AS file ON file.id = source.file_id
                JOIN memes AS meme ON meme.id = file.meme_id
                LEFT JOIN pipeline_ingest_requests AS request
                  ON request.source_platform = source.platform
                 AND request.source_id = source.source_id
                 AND request.post_id = source.post_id
                LEFT JOIN source_channels AS channel
                  ON channel.platform = source.platform
                 AND channel.platform_id = source.source_id
            )
            UPDATE meme_sources AS source
            SET uploader_user_id = resolved.uploader_user_id,
                source_kind = resolved.source_kind
            FROM resolved_source AS resolved
            WHERE source.id = resolved.id
            """
        )
    )

    # Legacy private user content must remain reachable after author-based access is removed.
    # Favorites is the stable private authority even when the user never opened a
    # collection surface before this migration.
    op.execute(
        sa.text(
            """
            INSERT INTO collections (id, owner_id, title, kind, visibility, created_at, updated_at)
            SELECT
                md5('memexpert-private-backfill-favorites:' || uploader.id::text)::uuid,
                uploader.id,
                'Favorites',
                'favorites',
                'private',
                now(),
                now()
            FROM users AS uploader
            WHERE EXISTS (
                SELECT 1
                FROM memes AS meme
                WHERE meme.author_user_id = uploader.id
                  AND meme.is_public IS FALSE
            )
              AND NOT EXISTS (
                SELECT 1
                FROM collections AS favorites
                WHERE favorites.owner_id = uploader.id
                  AND favorites.kind = 'favorites'
            )
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE collections AS favorites
            SET visibility = 'private', updated_at = now()
            WHERE favorites.kind = 'favorites'
              AND EXISTS (
                  SELECT 1
                  FROM memes AS meme
                  WHERE meme.author_user_id = favorites.owner_id
                    AND meme.is_public IS FALSE
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO collection_members (collection_id, user_id, role, joined_at)
            SELECT favorites.id, favorites.owner_id, 'owner', now()
            FROM collections AS favorites
            WHERE favorites.kind = 'favorites'
              AND EXISTS (
                  SELECT 1
                  FROM memes AS meme
                  WHERE meme.author_user_id = favorites.owner_id
                    AND meme.is_public IS FALSE
              )
            ON CONFLICT (collection_id, user_id) DO UPDATE SET role = 'owner'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE users AS uploader
            SET active_save_collection_id = favorites.id, updated_at = now()
            FROM collections AS favorites
            WHERE favorites.owner_id = uploader.id
              AND favorites.kind = 'favorites'
              AND uploader.active_save_collection_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM memes AS meme
                  WHERE meme.author_user_id = uploader.id
                    AND meme.is_public IS FALSE
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO collection_memes (collection_id, meme_id, added_by_user_id, added_at)
            SELECT
                favorites.id,
                meme.id,
                meme.author_user_id,
                meme.created_at
            FROM memes AS meme
            JOIN users AS uploader ON uploader.id = meme.author_user_id
            JOIN collections AS favorites
              ON favorites.owner_id = uploader.id
             AND favorites.kind = 'favorites'
            WHERE meme.is_public IS FALSE
            ON CONFLICT (collection_id, meme_id) DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            WITH latest_decision AS (
                SELECT DISTINCT ON (decision.meme_id)
                    decision.meme_id,
                    decision.action,
                    decision.new_is_public
                FROM moderation_decisions AS decision
                WHERE decision.action IN ('publish', 'hide', 'hide_and_mark_nsfw')
                   OR (
                       decision.action = 'override_flags'
                       AND decision.new_is_public IS DISTINCT FROM decision.previous_is_public
                   )
                ORDER BY decision.meme_id, decision.created_at DESC, decision.id DESC
            ), crawler_truth AS (
                SELECT DISTINCT file.meme_id
                FROM meme_files AS file
                JOIN meme_sources AS source ON source.file_id = file.id
                WHERE source.source_kind = 'public_crawler'
            ), blocked_truth AS (
                SELECT DISTINCT file.meme_id
                FROM meme_files AS file
                WHERE file.blocked_perceptual_hash_id IS NOT NULL
            ), resolved_mode AS (
                SELECT
                    meme.id,
                    CASE
                        WHEN latest.action = 'publish' THEN 'force_public'
                        WHEN latest.action IN ('hide', 'hide_and_mark_nsfw') THEN 'force_private'
                        WHEN latest.action = 'override_flags' THEN
                            CASE WHEN latest.new_is_public THEN 'force_public' ELSE 'force_private' END
                        WHEN blocked.meme_id IS NOT NULL THEN 'force_private'
                        WHEN meme.is_public = (crawler.meme_id IS NOT NULL) THEN 'auto'
                        WHEN meme.is_public THEN 'force_public'
                        ELSE 'force_private'
                    END AS visibility_mode
                FROM memes AS meme
                LEFT JOIN latest_decision AS latest ON latest.meme_id = meme.id
                LEFT JOIN crawler_truth AS crawler ON crawler.meme_id = meme.id
                LEFT JOIN blocked_truth AS blocked ON blocked.meme_id = meme.id
            )
            UPDATE memes AS meme
            SET visibility_mode = resolved.visibility_mode
            FROM resolved_mode AS resolved
            WHERE meme.id = resolved.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE moderation_decisions
            SET previous_visibility_mode = CASE
                    WHEN previous_is_public THEN 'force_public' ELSE 'force_private'
                END,
                new_visibility_mode = CASE
                    WHEN action = 'publish' THEN 'force_public'
                    WHEN action IN ('hide', 'hide_and_mark_nsfw') THEN 'force_private'
                    WHEN new_is_public THEN 'force_public'
                    ELSE 'force_private'
                END
            """
        )
    )

    for table_name, column_name, default in (
        ("memes", "visibility_mode", "auto"),
        ("pipeline_ingest_requests", "source_kind", "operator_upload"),
        ("meme_sources", "source_kind", "operator_upload"),
        ("moderation_decisions", "previous_visibility_mode", "auto"),
        ("moderation_decisions", "new_visibility_mode", "auto"),
    ):
        op.alter_column(
            table_name,
            column_name,
            existing_type=MEME_VISIBILITY_MODE if "visibility" in column_name else INGEST_SOURCE_KIND,
            server_default=sa.text(f"'{default}'"),
            nullable=False,
        )

    op.create_index("ix_memes_visibility_mode_created_at", "memes", ["visibility_mode", "created_at"])
    op.create_index(
        "ix_meme_sources_source_kind_uploader",
        "meme_sources",
        ["source_kind", "uploader_user_id"],
    )

    # Keep a rolling deployment safe while old writers still send owner_user_id.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION memexpert_sync_legacy_ingest_owner() RETURNS trigger AS $$
            BEGIN
                IF NEW.uploader_user_id IS NULL THEN
                    NEW.uploader_user_id := NEW.owner_user_id;
                END IF;
                IF NEW.owner_user_id IS NULL THEN
                    NEW.owner_user_id := NEW.uploader_user_id;
                END IF;
                IF NEW.uploader_user_id IS NOT NULL THEN
                    NEW.source_kind := 'user_upload';
                ELSIF EXISTS (
                    SELECT 1 FROM source_channels AS channel
                    WHERE channel.platform = NEW.source_platform
                      AND channel.platform_id = NEW.source_id
                ) THEN
                    NEW.source_kind := 'public_crawler';
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


def downgrade() -> None:
    """Remove additive provenance fields and their rolling-deploy triggers."""

    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_meme_sources_legacy_author ON meme_sources"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS memexpert_sync_legacy_meme_author()"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_pipeline_ingest_requests_legacy_owner ON pipeline_ingest_requests"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS memexpert_sync_legacy_ingest_owner()"))
    op.drop_index("ix_meme_sources_source_kind_uploader", table_name="meme_sources")
    op.drop_index("ix_memes_visibility_mode_created_at", table_name="memes")
    op.drop_constraint(op.f("fk_meme_sources_uploader_user_id_users"), "meme_sources", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_pipeline_ingest_requests_uploader_user_id_users"),
        "pipeline_ingest_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_moderation_decisions_visibility_modes_known",
        "moderation_decisions",
        type_="check",
    )
    op.drop_constraint("ck_meme_sources_source_kind_known", "meme_sources", type_="check")
    op.drop_constraint(
        "ck_pipeline_ingest_requests_source_kind_known",
        "pipeline_ingest_requests",
        type_="check",
    )
    op.drop_constraint("ck_memes_visibility_mode_known", "memes", type_="check")
    op.drop_column("moderation_decisions", "new_visibility_mode")
    op.drop_column("moderation_decisions", "previous_visibility_mode")
    op.drop_column("meme_sources", "uploader_user_id")
    op.drop_column("meme_sources", "source_kind")
    op.drop_column("pipeline_ingest_requests", "uploader_user_id")
    op.drop_column("pipeline_ingest_requests", "source_kind")
    op.drop_column("memes", "visibility_mode")
