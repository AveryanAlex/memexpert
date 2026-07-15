"""Smoke tests for Alembic migrations against ephemeral PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql

from alembic import command
from memexpert.models import metadata

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"
ALEMBIC_TIMEOUT_SECONDS = 20.0
CORE_APP_TABLES = {
    "users",
    "memes",
    "meme_files",
    "collections",
    "pipeline_stage_journal",
}
CORE_MATERIALIZED_VIEWS = {"public_meme_trends_mv"}


def _build_alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def _get_alembic_head_revision(config: Config) -> str:
    script_directory = ScriptDirectory.from_config(config)
    head_revision = script_directory.get_current_head()
    assert head_revision is not None
    return head_revision


async def _run_alembic_command(
    action: Callable[..., None],
    config: Config,
    *args: str,
    timeout: float = ALEMBIC_TIMEOUT_SECONDS,
) -> None:
    try:
        async with asyncio.timeout(timeout):
            await asyncio.to_thread(action, config, *args)
    except TimeoutError as exc:  # pragma: no cover - exercised only on failure
        action_name = getattr(action, "__name__", action.__class__.__name__)
        raise AssertionError(f"Alembic {action_name} timed out after {timeout:.1f}s") from exc


async def _reset_public_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        _ = await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        _ = await connection.execute(text("CREATE SCHEMA public"))


async def _get_table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(_get_table_names_sync)


def _get_table_names_sync(sync_connection: Connection) -> set[str]:
    return set(sa_inspect(sync_connection).get_table_names())


async def _get_materialized_view_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT matviewname
                FROM pg_matviews
                WHERE schemaname = current_schema()
                """
            )
        )
        return {cast("str", name) for name in result.scalars()}


async def _get_current_revision(engine: AsyncEngine) -> str | None:
    table_names = await _get_table_names(engine)
    if "alembic_version" not in table_names:
        return None

    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        revision = result.scalar_one_or_none()
        return None if revision is None else cast("str", revision)


@pytest_asyncio.fixture
async def empty_public_schema(
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
) -> AsyncIterator[tuple[AsyncEngine, str]]:
    await _reset_public_schema(postgres_async_engine)
    try:
        yield postgres_async_engine, postgres_async_url
    finally:
        await _reset_public_schema(postgres_async_engine)


async def test_alembic_upgrade_downgrade_and_reupgrade_smoke(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    head_revision = _get_alembic_head_revision(config)

    await _run_alembic_command(command.upgrade, config, "head")

    assert await _get_current_revision(engine) == head_revision
    assert ({"alembic_version"} | CORE_APP_TABLES).issubset(await _get_table_names(engine))
    assert CORE_MATERIALIZED_VIEWS.issubset(await _get_materialized_view_names(engine))

    await _run_alembic_command(command.downgrade, config, "base")

    assert await _get_current_revision(engine) is None
    assert CORE_APP_TABLES.isdisjoint(await _get_table_names(engine))
    assert not await _get_materialized_view_names(engine)

    await _run_alembic_command(command.upgrade, config, "head")

    assert await _get_current_revision(engine) == head_revision
    assert ({"alembic_version"} | CORE_APP_TABLES).issubset(await _get_table_names(engine))


async def test_0034_adds_operator_recovery_state_and_downgrades_new_backfill_statuses(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    admin_user_id = uuid.uuid7()
    channel_ids = [uuid.uuid7() for _ in range(4)]
    post_id = uuid.uuid7()
    backfill_ids = [uuid.uuid7() for _ in range(4)]

    await _run_alembic_command(command.upgrade, config, "0033")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO users (id, status, email, nsfw_enabled, language)
                VALUES (:user_id, 'active', 'recovery-admin@example.com', false, 'any')
                """
            ),
            {"user_id": admin_user_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channels (id, platform, platform_id, title, is_active)
                VALUES
                    (:channel_0, 'telegram', 'legacy-failed', 'Legacy failed', true),
                    (:channel_1, 'telegram', 'waiting-capacity', 'Waiting capacity', true),
                    (:channel_2, 'telegram', 'partial-complete', 'Partial complete', true),
                    (:channel_3, 'telegram', 'cancelled', 'Cancelled', true)
                """
            ),
            {f"channel_{index}": channel_id for index, channel_id in enumerate(channel_ids)},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channel_posts (
                    id, source_channel_id, post_id, published_at, status,
                    last_error_code, last_error_text, attempt_count
                ) VALUES (
                    :post_row_id, :channel_id, '42', now(), 'failed',
                    'telegram_request_failed', 'legacy provider failure', 6
                )
                """
            ),
            {"post_row_id": post_id, "channel_id": channel_ids[0]},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channel_backfill_jobs (
                    id, source_channel_id, requested_by_admin_user_id, status,
                    requested_message_count, scanned_message_count, last_error_text,
                    locked_at, started_at
                ) VALUES (
                    :job_id, :channel_id, :admin_user_id, 'failed',
                    100, 12, 'legacy provider failure', now(), now()
                )
                """
            ),
            {
                "job_id": backfill_ids[0],
                "channel_id": channel_ids[0],
                "admin_user_id": admin_user_id,
            },
        )

    await _run_alembic_command(command.upgrade, config, "0034")

    async with engine.begin() as connection:
        historical_post = (
            await connection.execute(
                text(
                    """
                    SELECT is_retryable, last_attempt_at IS NOT NULL AS has_last_attempt
                    FROM source_channel_posts
                    WHERE id = :post_id
                    """
                ),
                {"post_id": post_id},
            )
        ).one()
        historical_backfill = (
            await connection.execute(
                text(
                    """
                    SELECT is_retryable, attempt_count,
                           last_progress_at IS NOT NULL AS has_last_progress
                    FROM source_channel_backfill_jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": backfill_ids[0]},
            )
        ).one()

        assert historical_post == (True, True)
        assert historical_backfill == (True, 1, True)

        await connection.execute(
            text(
                """
                UPDATE source_channel_backfill_jobs
                SET status = 'waiting_retry'
                WHERE id = :job_id
                """
            ),
            {"job_id": backfill_ids[0]},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channel_backfill_jobs (
                    id, source_channel_id, status,
                    requested_message_count, scanned_message_count
                ) VALUES
                    (:job_1, :channel_1, 'waiting_capacity', 100, 0),
                    (:job_2, :channel_2, 'completed_with_failures', 100, 100),
                    (:job_3, :channel_3, 'cancelled', 100, 0)
                """
            ),
            {
                "job_1": backfill_ids[1],
                "channel_1": channel_ids[1],
                "job_2": backfill_ids[2],
                "channel_2": channel_ids[2],
                "job_3": backfill_ids[3],
                "channel_3": channel_ids[3],
            },
        )

        for table_name in ("recovery_jobs", "pipeline_stage_attempts"):
            actual_check_names = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid = CAST(:table_name AS regclass)
                              AND contype = 'c'
                            """
                        ),
                        {"table_name": table_name},
                    )
                ).scalars()
            )
            preparer = postgresql.dialect().identifier_preparer
            expected_check_names = {
                preparer.format_constraint(constraint)
                for constraint in metadata.tables[table_name].constraints
                if isinstance(constraint, CheckConstraint)
            }
            assert actual_check_names == expected_check_names

        attempt_unique_definition = (
            await connection.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'pipeline_stage_attempts'::regclass
                      AND conname = 'uq_pipeline_stage_attempts_file_stage_event_attempt'
                    """
                )
            )
        ).scalar_one()
        assert attempt_unique_definition == ("UNIQUE (meme_file_id, stage, event_id, attempt_number)")

    operational_tables = {
        "dependency_circuit_states",
        "operational_audit_logs",
        "pipeline_capacity_states",
        "pipeline_dead_letters",
        "pipeline_stage_attempts",
        "recovery_job_items",
        "recovery_jobs",
        "runtime_heartbeats",
        "source_channel_backfill_attempts",
    }
    assert operational_tables.issubset(await _get_table_names(engine))

    await _run_alembic_command(command.downgrade, config, "0033")

    async with engine.connect() as connection:
        status_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, status
                    FROM source_channel_backfill_jobs
                    WHERE id = ANY(:job_ids)
                    """
                ),
                {"job_ids": backfill_ids},
            )
        ).all()
        post_columns = {
            row.column_name
            for row in (
                await connection.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'source_channel_posts'
                        """
                    )
                )
            )
        }

    assert {row.id: row.status for row in status_rows} == {
        backfill_ids[0]: "queued",
        backfill_ids[1]: "queued",
        backfill_ids[2]: "completed",
        backfill_ids[3]: "failed",
    }
    assert operational_tables.isdisjoint(await _get_table_names(engine))
    assert {"is_retryable", "next_attempt_at", "last_attempt_at", "quarantined_at"}.isdisjoint(post_columns)


async def test_0029_backfills_existing_telegram_requests_into_source_post_inventory(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    channel_id = uuid.uuid7()
    request_id = uuid.uuid7()
    fallback_channel_id = uuid.uuid7()
    fallback_request_id = uuid.uuid7()

    await _run_alembic_command(command.upgrade, config, "0028")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO source_channels (
                    id, platform, platform_id, title, is_active, last_read_post_id
                ) VALUES (
                    :channel_id, 'telegram', 'legacy_channel', 'Legacy channel', true, '511'
                )
                """
            ),
            {"channel_id": channel_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO pipeline_ingest_requests (
                    id,
                    source_platform,
                    source_id,
                    post_id,
                    source_metadata,
                    status
                ) VALUES (
                    :request_id,
                    'telegram',
                    'legacy_channel',
                    '42',
                    '{"media_type": "photo"}'::jsonb,
                    'media_inspect_pending'
                )
                """
            ),
            {"request_id": request_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channels (
                    id, platform, platform_id, title, is_active, last_read_post_id
                ) VALUES (
                    :channel_id, 'telegram', 'fallback_channel', 'Fallback channel', true, 'opaque'
                )
                """
            ),
            {"channel_id": fallback_channel_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO pipeline_ingest_requests (
                    id,
                    source_platform,
                    source_id,
                    post_id,
                    source_metadata,
                    status
                ) VALUES (
                    :request_id,
                    'telegram',
                    'fallback_channel',
                    '84',
                    '{"media_type": "photo"}'::jsonb,
                    'media_inspect_pending'
                )
                """
            ),
            {"request_id": fallback_request_id},
        )

    await _run_alembic_command(command.upgrade, config, "head")

    async with engine.connect() as connection:
        inventory_row = (
            await connection.execute(
                text(
                    """
                    SELECT id, post_id, media_type, status
                    FROM source_channel_posts
                    WHERE source_channel_id = :channel_id
                    """
                ),
                {"channel_id": channel_id},
            )
        ).one()
        channel_row = (
            await connection.execute(
                text(
                    """
                    SELECT oldest_observed_post_id,
                           history_cursor_post_id,
                           initial_catchup_completed,
                           history_exhausted,
                           catchup_message_limit
                    FROM source_channels
                    WHERE id = :channel_id
                    """
                ),
                {"channel_id": channel_id},
            )
        ).one()
        fallback_channel_row = (
            await connection.execute(
                text(
                    """
                    SELECT oldest_observed_post_id, history_cursor_post_id, initial_catchup_completed
                    FROM source_channels
                    WHERE id = :channel_id
                    """
                ),
                {"channel_id": fallback_channel_id},
            )
        ).one()

    assert inventory_row == (request_id, "42", "photo", "accepted")
    assert channel_row == ("42", "512", True, False, 5000)
    assert fallback_channel_row == ("84", "84", True)


async def test_0031_backfills_provenance_visibility_and_private_collection_access(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    user_id = uuid.uuid7()
    channel_id = uuid.uuid7()
    private_meme_id, private_file_id = uuid.uuid7(), uuid.uuid7()
    crawler_meme_id, crawler_file_id = uuid.uuid7(), uuid.uuid7()
    operator_meme_id, operator_file_id = uuid.uuid7(), uuid.uuid7()
    hidden_crawler_meme_id, hidden_crawler_file_id = uuid.uuid7(), uuid.uuid7()
    request_ids = [uuid.uuid7() for _ in range(4)]
    source_ids = [uuid.uuid7() for _ in range(4)]
    decision_id = uuid.uuid7()
    non_visibility_decision_id = uuid.uuid7()

    await _run_alembic_command(command.upgrade, config, "0030")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO users (
                    id, status, email, nsfw_enabled, language
                ) VALUES (
                    :user_id, 'active', 'legacy-private@example.com', false, 'any'
                )
                """
            ),
            {"user_id": user_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channels (
                    id, platform, platform_id, title, is_active
                ) VALUES (
                    :channel_id, 'telegram', 'public-crawler', 'Public crawler', true
                )
                """
            ),
            {"channel_id": channel_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO memes (
                    id, media_type, primary_file_id, language, is_nsfw,
                    like_count, tags, author_user_id, is_public, created_at, updated_at
                ) VALUES
                    (
                        :private_meme_id, 'image', :private_file_id, 'none', false,
                        0, '{}', :user_id, false, now() - interval '4 hours', now()
                    ),
                    (
                        :crawler_meme_id, 'image', :crawler_file_id, 'none', false,
                        0, '{}', NULL, true, now() - interval '3 hours', now()
                    ),
                    (
                        :operator_meme_id, 'image', :operator_file_id, 'none', false,
                        0, '{}', NULL, true, now() - interval '2 hours', now()
                    ),
                    (
                        :hidden_crawler_meme_id, 'image', :hidden_crawler_file_id, 'none', false,
                        0, '{}', NULL, false, now() - interval '1 hour', now()
                    )
                """
            ),
            {
                "private_meme_id": private_meme_id,
                "private_file_id": private_file_id,
                "crawler_meme_id": crawler_meme_id,
                "crawler_file_id": crawler_file_id,
                "operator_meme_id": operator_meme_id,
                "operator_file_id": operator_file_id,
                "hidden_crawler_meme_id": hidden_crawler_meme_id,
                "hidden_crawler_file_id": hidden_crawler_file_id,
                "user_id": user_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO meme_files (
                    id, meme_id, status, s3_original_key, sha256_hex, quality_score
                ) VALUES
                    (:private_file_id, :private_meme_id, 'ready', 'legacy/private.jpg', :private_sha, 1.0),
                    (:crawler_file_id, :crawler_meme_id, 'ready', 'legacy/crawler.jpg', :crawler_sha, 1.0),
                    (:operator_file_id, :operator_meme_id, 'ready', 'legacy/operator.jpg', :operator_sha, 1.0),
                    (
                        :hidden_crawler_file_id, :hidden_crawler_meme_id, 'ready',
                        'legacy/hidden-crawler.jpg', :hidden_sha, 1.0
                    )
                """
            ),
            {
                "private_file_id": private_file_id,
                "private_meme_id": private_meme_id,
                "private_sha": "1" * 64,
                "crawler_file_id": crawler_file_id,
                "crawler_meme_id": crawler_meme_id,
                "crawler_sha": "2" * 64,
                "operator_file_id": operator_file_id,
                "operator_meme_id": operator_meme_id,
                "operator_sha": "3" * 64,
                "hidden_crawler_file_id": hidden_crawler_file_id,
                "hidden_crawler_meme_id": hidden_crawler_meme_id,
                "hidden_sha": "4" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO pipeline_ingest_requests (
                    id, source_platform, source_id, post_id, owner_user_id,
                    status, materialized_meme_id, materialized_meme_file_id
                ) VALUES
                    (
                        :private_request_id, 'telegram', 'private-upload', '1', :user_id,
                        'materialized', :private_meme_id, :private_file_id
                    ),
                    (
                        :crawler_request_id, 'telegram', 'public-crawler', '2', NULL,
                        'materialized', :crawler_meme_id, :crawler_file_id
                    ),
                    (
                        :operator_request_id, 'telegram', 'manual-operator', '3', NULL,
                        'materialized', :operator_meme_id, :operator_file_id
                    ),
                    (
                        :hidden_request_id, 'telegram', 'public-crawler', '4', NULL,
                        'materialized', :hidden_meme_id, :hidden_file_id
                    )
                """
            ),
            {
                "private_request_id": request_ids[0],
                "crawler_request_id": request_ids[1],
                "operator_request_id": request_ids[2],
                "hidden_request_id": request_ids[3],
                "user_id": user_id,
                "private_meme_id": private_meme_id,
                "private_file_id": private_file_id,
                "crawler_meme_id": crawler_meme_id,
                "crawler_file_id": crawler_file_id,
                "operator_meme_id": operator_meme_id,
                "operator_file_id": operator_file_id,
                "hidden_meme_id": hidden_crawler_meme_id,
                "hidden_file_id": hidden_crawler_file_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO meme_sources (
                    id, file_id, platform, source_id, post_id, is_first_source, source_alive
                ) VALUES
                    (:private_source_id, :private_file_id, 'telegram', 'private-upload', '1', true, true),
                    (:crawler_source_id, :crawler_file_id, 'telegram', 'public-crawler', '2', true, true),
                    (:operator_source_id, :operator_file_id, 'telegram', 'manual-operator', '3', true, true),
                    (:hidden_source_id, :hidden_file_id, 'telegram', 'public-crawler', '4', true, true)
                """
            ),
            {
                "private_source_id": source_ids[0],
                "crawler_source_id": source_ids[1],
                "operator_source_id": source_ids[2],
                "hidden_source_id": source_ids[3],
                "private_file_id": private_file_id,
                "crawler_file_id": crawler_file_id,
                "operator_file_id": operator_file_id,
                "hidden_file_id": hidden_crawler_file_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO moderation_decisions (
                    id, meme_id, action, reason, previous_is_public, previous_is_nsfw,
                    new_is_public, new_is_nsfw, created_at
                ) VALUES
                    (
                        :decision_id, :meme_id, 'hide', 'other', true, false,
                        false, false, now() - interval '1 minute'
                    ),
                    (
                        :non_visibility_decision_id, :meme_id, 'mark_sfw', 'other', false, true,
                        false, false, now()
                    )
                """
            ),
            {
                "decision_id": decision_id,
                "non_visibility_decision_id": non_visibility_decision_id,
                "meme_id": hidden_crawler_meme_id,
            },
        )

    await _run_alembic_command(command.upgrade, config, "0031")

    async with engine.connect() as connection:
        request_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, source_kind, uploader_user_id
                    FROM pipeline_ingest_requests
                    ORDER BY id
                    """
                )
            )
        ).all()
        source_rows = (
            await connection.execute(text("SELECT id, source_kind, uploader_user_id FROM meme_sources ORDER BY id"))
        ).all()
        visibility_result = await connection.execute(text("SELECT id, visibility_mode FROM memes"))
        visibility_rows = dict(visibility_result.tuples().all())
        favorites_row = (
            await connection.execute(
                text(
                    """
                    SELECT collection.id, collection.visibility, member.role,
                           saved.meme_id, uploader.active_save_collection_id
                    FROM collections AS collection
                    JOIN collection_members AS member
                      ON member.collection_id = collection.id
                     AND member.user_id = :user_id
                    JOIN collection_memes AS saved
                      ON saved.collection_id = collection.id
                     AND saved.meme_id = :meme_id
                    JOIN users AS uploader ON uploader.id = :user_id
                    WHERE collection.owner_id = :user_id
                      AND collection.kind = 'favorites'
                    """
                ),
                {"user_id": user_id, "meme_id": private_meme_id},
            )
        ).one()
        decision_modes = (
            await connection.execute(
                text(
                    """
                    SELECT previous_visibility_mode, new_visibility_mode
                    FROM moderation_decisions
                    WHERE id = :decision_id
                    """
                ),
                {"decision_id": decision_id},
            )
        ).one()

    expected_requests = {
        request_ids[0]: ("user_upload", user_id),
        request_ids[1]: ("public_crawler", None),
        request_ids[2]: ("operator_upload", None),
        request_ids[3]: ("public_crawler", None),
    }
    assert {row.id: (row.source_kind, row.uploader_user_id) for row in request_rows} == expected_requests
    expected_sources = {
        source_ids[0]: ("user_upload", user_id),
        source_ids[1]: ("public_crawler", None),
        source_ids[2]: ("operator_upload", None),
        source_ids[3]: ("public_crawler", None),
    }
    assert {row.id: (row.source_kind, row.uploader_user_id) for row in source_rows} == expected_sources
    assert visibility_rows == {
        private_meme_id: "auto",
        crawler_meme_id: "auto",
        operator_meme_id: "force_public",
        hidden_crawler_meme_id: "force_private",
    }
    assert favorites_row.visibility == "private"
    assert favorites_row.role == "owner"
    assert favorites_row.meme_id == private_meme_id
    assert favorites_row.active_save_collection_id == favorites_row.id
    assert decision_modes == ("force_public", "force_private")


async def test_0033_repairs_legacy_crawler_visibility_and_post_classify_readiness(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    crawler_meme_id, crawler_file_id = uuid.uuid7(), uuid.uuid7()
    hidden_meme_id, hidden_file_id = uuid.uuid7(), uuid.uuid7()

    await _run_alembic_command(command.upgrade, config, "0032")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO memes (
                    id, media_type, primary_file_id, language, is_nsfw,
                    like_count, tags, visibility_mode, is_public, created_at, updated_at
                ) VALUES
                    (
                        :crawler_meme_id, 'image', :crawler_file_id, 'none', false,
                        0, '{}', 'force_private', false, now(), now()
                    ),
                    (
                        :hidden_meme_id, 'image', :hidden_file_id, 'none', false,
                        0, '{}', 'force_private', false, now(), now()
                    )
                """
            ),
            {
                "crawler_meme_id": crawler_meme_id,
                "crawler_file_id": crawler_file_id,
                "hidden_meme_id": hidden_meme_id,
                "hidden_file_id": hidden_file_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO meme_files (
                    id, meme_id, status, s3_original_key, sha256_hex, quality_score,
                    created_at, updated_at
                ) VALUES
                    (
                        :crawler_file_id, :crawler_meme_id, 'processing',
                        'legacy/crawler-ready.jpg', :crawler_sha, 1.0, now(), now()
                    ),
                    (
                        :hidden_file_id, :hidden_meme_id, 'processing',
                        'legacy/crawler-hidden.jpg', :hidden_sha, 1.0, now(), now()
                    )
                """
            ),
            {
                "crawler_file_id": crawler_file_id,
                "crawler_meme_id": crawler_meme_id,
                "crawler_sha": "5" * 64,
                "hidden_file_id": hidden_file_id,
                "hidden_meme_id": hidden_meme_id,
                "hidden_sha": "6" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO meme_sources (
                    id, file_id, platform, source_id, post_id, source_kind,
                    uploader_user_id, is_first_source, source_alive, created_at, updated_at
                ) VALUES
                    (
                        :crawler_source_id, :crawler_file_id, 'telegram', 'legacy-public', '1',
                        'public_crawler', NULL, true, true, now(), now()
                    ),
                    (
                        :hidden_source_id, :hidden_file_id, 'telegram', 'legacy-hidden', '2',
                        'public_crawler', NULL, true, true, now(), now()
                    )
                """
            ),
            {
                "crawler_source_id": uuid.uuid7(),
                "crawler_file_id": crawler_file_id,
                "hidden_source_id": uuid.uuid7(),
                "hidden_file_id": hidden_file_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO pipeline_stage_journal (
                    id, meme_file_id, stage, status, attempt_count, last_event_id,
                    is_retryable, started_at, finished_at, created_at, updated_at
                ) VALUES
                    (
                        :crawler_stage_id, :crawler_file_id, 'classify', 'succeeded', 1, NULL,
                        false, now(), now(), now(), now()
                    ),
                    (
                        :hidden_stage_id, :hidden_file_id, 'classify', 'succeeded', 1, NULL,
                        false, now(), now(), now(), now()
                    )
                """
            ),
            {
                "crawler_stage_id": uuid.uuid7(),
                "crawler_file_id": crawler_file_id,
                "hidden_stage_id": uuid.uuid7(),
                "hidden_file_id": hidden_file_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO moderation_decisions (
                    id, meme_id, action, reason,
                    previous_is_public, previous_visibility_mode, previous_is_nsfw,
                    new_is_public, new_visibility_mode, new_is_nsfw, created_at
                ) VALUES (
                    :decision_id, :hidden_meme_id, 'hide', 'other',
                    true, 'auto', false, false, 'force_private', false, now()
                )
                """
            ),
            {"decision_id": uuid.uuid7(), "hidden_meme_id": hidden_meme_id},
        )

    await _run_alembic_command(command.upgrade, config, "0033")

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT meme.id, meme.visibility_mode, meme.is_public, file.status
                    FROM memes AS meme
                    JOIN meme_files AS file ON file.id = meme.primary_file_id
                    ORDER BY meme.id
                    """
                )
            )
        ).all()

    repaired = {row.id: (row.visibility_mode, row.is_public, row.status) for row in rows}
    assert repaired[crawler_meme_id] == ("auto", True, "ready")
    assert repaired[hidden_meme_id] == ("force_private", False, "ready")
