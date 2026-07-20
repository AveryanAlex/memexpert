"""Smoke tests for Alembic migrations against ephemeral PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from alembic import command

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
    "media_generations",
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

        # Freeze the physical 0034 contract instead of comparing a historical
        # revision with head metadata. PostgreSQL's 63-byte identifier limit
        # gives the long attempt constraint its deterministic Alembic suffix.
        expected_check_names_by_table = {
            "recovery_jobs": {
                "ck_recovery_jobs_recovery_jobs_total_count_non_negative",
                "ck_recovery_jobs_recovery_jobs_completed_count_non_negative",
                "ck_recovery_jobs_recovery_jobs_failed_count_non_negative",
                "ck_recovery_jobs_recoveryjobstatus",
                "ck_recovery_jobs_recoverycapability",
            },
            "pipeline_stage_attempts": {
                "ck_pipeline_stage_attempts_pipeline_stage_attempts_atte_9d56",
                "ck_pipeline_stage_attempts_contentpipelinestage",
                "ck_pipeline_stage_attempts_pipelineattemptoutcome",
            },
        }
        for table_name, expected_check_names in expected_check_names_by_table.items():
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


async def test_0036_seeds_locale_synonym_drafts_and_idle_sync_state(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "0035")
    await _run_alembic_command(command.upgrade, config, "0036")

    async with engine.connect() as connection:
        catalogs = (
            await connection.execute(
                text(
                    """
                    SELECT catalog.locale, revision.revision_number, revision.status,
                           revision.source_text, revision.compiler_version,
                           revision.validation ->> 'valid' AS valid,
                           revision.version
                    FROM search_synonym_catalogs AS catalog
                    JOIN search_synonym_revisions AS revision
                      ON revision.catalog_id = catalog.id
                    ORDER BY catalog.locale
                    """
                )
            )
        ).all()
        sync_state = (
            await connection.execute(
                text(
                    """
                    SELECT id, status, desired_hash, desired_revision_ids,
                           applied_revision_ids, version
                    FROM search_synonym_sync_states
                    """
                )
            )
        ).one()
        index_names = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND tablename = 'search_synonym_revisions'
                        """
                    )
                )
            ).scalars()
        )

    assert [tuple(row) for row in catalogs] == [
        ("en", 1, "draft", "", "meili_synonyms_v1", "false", 1),
        ("ru", 1, "draft", "", "meili_synonyms_v1", "false", 1),
    ]
    assert tuple(sync_state) == ("meilisearch", "idle", None, {}, {}, 1)
    assert {
        "uq_search_synonym_revisions_one_draft",
        "uq_search_synonym_revisions_one_published",
    }.issubset(index_names)

    await _run_alembic_command(command.downgrade, config, "0035")
    assert {
        "search_synonym_catalogs",
        "search_synonym_revisions",
        "search_synonym_sync_states",
    }.isdisjoint(await _get_table_names(engine))


async def test_0037_adds_versioned_telegram_post_metadata(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    channel_id = uuid.uuid7()
    post_row_id = uuid.uuid7()

    await _run_alembic_command(command.upgrade, config, "0036")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO source_channels (id, platform, platform_id, title, is_active)
                VALUES (:channel_id, 'telegram', 'metadata-legacy', 'Metadata legacy', true)
                """
            ),
            {"channel_id": channel_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channel_posts (
                    id, source_channel_id, post_id, published_at, status, attempt_count
                ) VALUES (
                    :post_row_id, :channel_id, '42', now(), 'accepted', 1
                )
                """
            ),
            {"post_row_id": post_row_id, "channel_id": channel_id},
        )

    await _run_alembic_command(command.upgrade, config, "0037")

    async with engine.connect() as connection:
        legacy_metadata = (
            await connection.execute(
                text(
                    """
                    SELECT first_observed_text,
                           latest_text,
                           first_observed_text_entities,
                           latest_text_entities,
                           media_group_id,
                           reply_to_post_id,
                           telegram_edited_at,
                           metadata_first_observed_at,
                           metadata_last_observed_at,
                           metadata_version,
                           is_deleted,
                           deletion_observed_at
                    FROM source_channel_posts
                    WHERE id = :post_row_id
                    """
                ),
                {"post_row_id": post_row_id},
            )
        ).one()
        index_definition = (
            await connection.execute(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'source_channel_posts'
                      AND indexname = 'ix_source_channel_posts_channel_media_group_post'
                    """
                )
            )
        ).scalar_one()
        metadata_constraint = (
            await connection.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conname = 'ck_source_channel_posts_metadata_version_non_negative'
                    """
                )
            )
        ).scalar_one()

    assert tuple(legacy_metadata) == (
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
        False,
        None,
    )
    assert "WHERE (media_group_id IS NOT NULL)" in index_definition
    assert "metadata_version >= 0" in metadata_constraint

    await _run_alembic_command(command.downgrade, config, "0036")
    async with engine.connect() as connection:
        remaining_metadata_columns = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'source_channel_posts'
                          AND column_name IN (
                              'first_observed_text',
                              'latest_text',
                              'first_observed_text_entities',
                              'latest_text_entities',
                              'media_group_id',
                              'reply_to_post_id',
                              'telegram_edited_at',
                              'metadata_first_observed_at',
                              'metadata_last_observed_at',
                              'metadata_version',
                              'is_deleted',
                              'deletion_observed_at'
                          )
                        """
                    )
                )
            ).scalars()
        )
    assert remaining_metadata_columns == set()


async def test_0038_adds_audience_schedule_without_synthetic_history(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    telegram_session_id = uuid.uuid7()
    source_channel_id = uuid.uuid7()

    await _run_alembic_command(command.upgrade, config, "0037")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO telegram_sessions (id, name, display_name, status, enabled)
                VALUES (:session_id, 'audience-session', 'Audience session', 'active', true)
                """
            ),
            {"session_id": telegram_session_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO source_channels (
                    id, platform, platform_id, title, subscriber_count,
                    is_active, telegram_session_id
                ) VALUES (
                    :channel_id, 'telegram', 'audience_channel', 'Audience channel',
                    1234, true, :session_id
                )
                """
            ),
            {"channel_id": source_channel_id, "session_id": telegram_session_id},
        )

    await _run_alembic_command(command.upgrade, config, "0038")

    async with engine.connect() as connection:
        channel_state = (
            await connection.execute(
                text(
                    """
                    SELECT subscriber_count,
                           subscriber_count_updated_at,
                           next_audience_capture_at,
                           audience_capture_attempt_count
                    FROM source_channels
                    WHERE id = :channel_id
                    """
                ),
                {"channel_id": source_channel_id},
            )
        ).one()
        snapshot_count = await connection.scalar(
            text("SELECT count(*) FROM source_channel_audience_snapshots")
        )
        status_count_constraint = await connection.scalar(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'ck_source_channel_audience_snapshots_status_count'
                """
            )
        )

    assert channel_state.subscriber_count == 1234
    assert channel_state.subscriber_count_updated_at is None
    assert channel_state.next_audience_capture_at is not None
    assert channel_state.audience_capture_attempt_count == 0
    assert snapshot_count == 0
    assert status_count_constraint is not None
    assert "fetch_status" in status_count_constraint
    assert "subscriber_count IS NOT NULL" in status_count_constraint

    await _run_alembic_command(command.downgrade, config, "0037")
    assert "source_channel_audience_snapshots" not in await _get_table_names(engine)


async def test_0040_rebuilds_all_public_trend_views_with_high_water_send_semantics(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    expected_views = {
        "public_meme_trends_mv",
        "public_tag_trends_mv",
        "public_template_trends_mv",
        "public_tag_trend_points_mv",
        "public_template_trend_points_mv",
    }
    directly_aggregated_views = {
        "public_meme_trends_mv",
        "public_tag_trend_points_mv",
        "public_template_trend_points_mv",
    }

    await _run_alembic_command(command.upgrade, config, "0039")
    await _run_alembic_command(command.upgrade, config, "0040")


    assert expected_views <= await _get_materialized_view_names(engine)
    async with engine.connect() as connection:
        corrected_definitions = {
            view_name: cast(
                "str",
                await connection.scalar(
                    text("SELECT pg_get_viewdef(CAST(:view_name AS regclass), true)"),
                    {"view_name": view_name},
                ),
            )
            for view_name in directly_aggregated_views
        }
        corrected_fastest_rising_index = cast(
            "str",
            await connection.scalar(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = 'ix_public_meme_trends_mv_fastest_rising'
                    """
                )
            ),
        )
    for definition in corrected_definitions.values():
        assert "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING" in definition
        assert "inline_sent" not in definition
    assert "download" not in corrected_fastest_rising_index
    corrected_meme_definition = corrected_definitions["public_meme_trends_mv"]
    assert "COALESCE(ec.download_count_24h, 0) * 2" not in corrected_meme_definition
    assert "COALESCE(ec.recent_download_count, 0) * 2" not in corrected_meme_definition
    assert "COALESCE(ec.previous_download_count, 0) * 2" not in corrected_meme_definition

    await _run_alembic_command(command.downgrade, config, "0039")
    assert expected_views <= await _get_materialized_view_names(engine)
    async with engine.connect() as connection:
        legacy_definition = cast(
            "str",
            await connection.scalar(
                text("SELECT pg_get_viewdef('public_meme_trends_mv'::regclass, true)")
            ),
        )
        legacy_fastest_rising_index = cast(
            "str",
            await connection.scalar(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = 'ix_public_meme_trends_mv_fastest_rising'
                    """
                )
            ),
        )
    assert "lag(" in legacy_definition
    assert "inline_sent" in legacy_definition
    assert "COALESCE(ec.download_count_24h, 0) * 2" in legacy_definition
    assert "download" in legacy_fastest_rising_index

    await _run_alembic_command(command.upgrade, config, "0040")


async def test_0041_indexes_both_public_meme_analytics_reference_shapes(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    expected_indexes = {
        "ix_analytics_events_refs_meme_event_occurred",
        "ix_analytics_events_legacy_meme_event_occurred",
    }

    await _run_alembic_command(command.upgrade, config, "0041")
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = ANY(:index_names)
                    """
                ),
                {"index_names": sorted(expected_indexes)},
            )
        ).all()

    definitions = {row.indexname: row.indexdef for row in rows}
    assert definitions.keys() == expected_indexes
    for definition in definitions.values():
        assert "payload" in definition
        assert "meme_id" in definition
        assert "event_type" in definition
        assert "occurred_at" in definition

    await _run_alembic_command(command.downgrade, config, "0040")
    async with engine.connect() as connection:
        remaining = await connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname = ANY(:index_names)
                """
            ),
            {"index_names": sorted(expected_indexes)},
        )
    assert remaining == 0


async def test_0042_adds_and_removes_immutable_media_generation_contract(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "0042")
    assert "media_generations" in await _get_table_names(engine)
    assert "recovery_query_snapshot_members" in await _get_table_names(engine)

    async with engine.connect() as connection:
        generation_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in sa_inspect(sync_connection).get_columns("media_generations")
            }
        )
        meme_file_columns = await connection.run_sync(
            lambda sync_connection: {column["name"] for column in sa_inspect(sync_connection).get_columns("meme_files")}
        )
        generation_indexes = await connection.run_sync(
            lambda sync_connection: {
                index["name"] for index in sa_inspect(sync_connection).get_indexes("media_generations")
            }
        )
        recovery_item_indexes = await connection.run_sync(
            lambda sync_connection: {
                index["name"] for index in sa_inspect(sync_connection).get_indexes("recovery_job_items")
            }
        )
        snapshot_member_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in sa_inspect(sync_connection).get_columns("recovery_query_snapshot_members")
            }
        )
        snapshot_member_indexes = await connection.run_sync(
            lambda sync_connection: {
                index["name"]
                for index in sa_inspect(sync_connection).get_indexes("recovery_query_snapshot_members")
            }
        )
        snapshot_member_foreign_keys = await connection.run_sync(
            lambda sync_connection: sa_inspect(sync_connection).get_foreign_keys(
                "recovery_query_snapshot_members"
            )
        )
        generation_checks = await connection.run_sync(
            lambda sync_connection: {
                constraint["name"]: constraint["sqltext"]
                for constraint in sa_inspect(sync_connection).get_check_constraints("media_generations")
            }
        )
        generation_foreign_keys = await connection.run_sync(
            lambda sync_connection: sa_inspect(sync_connection).get_foreign_keys("media_generations")
        )
        meme_file_foreign_keys = await connection.run_sync(
            lambda sync_connection: sa_inspect(sync_connection).get_foreign_keys("meme_files")
        )

    assert {
        "meme_file_id",
        "recovery_item_id",
        "expected_web_video_object_key",
        "web_video_object_key",
        "preview_image_object_key",
        "profile",
        "retry_limit",
        "attempt_count",
        "status",
        "source_observations",
        "output_observations",
        "source_width",
        "source_height",
        "source_frame_rate_numerator",
        "source_frame_rate_denominator",
        "source_duration_seconds",
        "source_has_audio",
        "output_width",
        "output_height",
        "output_frame_rate_numerator",
        "output_frame_rate_denominator",
        "output_duration_seconds",
        "output_video_bitrate",
        "output_byte_size",
        "output_video_codec",
        "output_audio_codec",
        "output_has_audio",
        "safe_failure_reason",
        "safe_failure_text",
        "verified_at",
        "uploaded_at",
        "activated_at",
        "superseded_at",
        "cleanup_status",
        "cleanup_attempt_count",
        "cleanup_error_text",
        "cleanup_at",
    }.issubset(generation_columns)
    assert {
        "active_media_generation_id",
        "source_has_audio",
        "web_video_has_audio",
        "web_video_profile",
        "web_video_verified_at",
    }.issubset(meme_file_columns)
    assert {
        "ix_media_generations_cleanup_status_created",
        "ix_media_generations_file_created",
        "ix_media_generations_recovery_item",
        "ix_media_generations_status_superseded",
    }.issubset(generation_indexes)
    assert {
        "uq_recovery_job_items_active_stage_reservation",
        "uq_recovery_job_items_active_work_reservation",
    }.issubset(recovery_item_indexes)
    assert {
        "recovery_job_id",
        "root_key",
        "work_kind",
        "work_id",
        "meme_file_id",
        "stage",
        "captured_version",
        "captured_context_fingerprint",
        "is_outdated_video",
    }.issubset(snapshot_member_columns)
    assert "ix_recovery_query_snapshot_members_job_id" in snapshot_member_indexes
    assert len(snapshot_member_foreign_keys) == 1
    assert snapshot_member_foreign_keys[0]["referred_table"] == "recovery_jobs"
    assert snapshot_member_foreign_keys[0]["options"]["ondelete"] == "CASCADE"
    assert any("retry_limit" in sqltext and "1" in sqltext and "5" in sqltext for sqltext in generation_checks.values())
    assert any("attempt_count >= 0" in sqltext for sqltext in generation_checks.values())
    assert {foreign_key["referred_table"] for foreign_key in generation_foreign_keys} == {
        "meme_files",
        "recovery_job_items",
    }
    active_generation_fk = next(
        foreign_key
        for foreign_key in meme_file_foreign_keys
        if foreign_key["constrained_columns"] == ["active_media_generation_id"]
    )
    assert active_generation_fk["referred_table"] == "media_generations"
    assert active_generation_fk["options"]["ondelete"] == "SET NULL"

    await _run_alembic_command(command.downgrade, config, "0041")
    assert "media_generations" not in await _get_table_names(engine)
    assert "recovery_query_snapshot_members" not in await _get_table_names(engine)
    async with engine.connect() as connection:
        downgraded_meme_file_columns = await connection.run_sync(
            lambda sync_connection: {column["name"] for column in sa_inspect(sync_connection).get_columns("meme_files")}
        )
        downgraded_recovery_item_indexes = await connection.run_sync(
            lambda sync_connection: {
                index["name"] for index in sa_inspect(sync_connection).get_indexes("recovery_job_items")
            }
        )
    assert {
        "active_media_generation_id",
        "source_has_audio",
        "web_video_has_audio",
        "web_video_profile",
        "web_video_verified_at",
    }.isdisjoint(downgraded_meme_file_columns)
    assert "uq_recovery_job_items_active_work_reservation" not in downgraded_recovery_item_indexes


async def test_0042_backfills_and_safely_terminalizes_legacy_recovery_jobs(
    empty_public_schema: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = empty_public_schema
    config = _build_alembic_config(database_url)
    admin_user_id = uuid.uuid7()
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    stage_id = uuid.uuid7()
    sync_target_id = uuid.uuid7()
    preview_stage_job_id = uuid.uuid7()
    queued_stage_job_id = uuid.uuid7()
    running_sync_job_id = uuid.uuid7()
    preview_non_stage_job_id = uuid.uuid7()
    preview_stage_item_id = uuid.uuid7()
    queued_stage_item_id = uuid.uuid7()
    running_sync_item_id = uuid.uuid7()
    preview_non_stage_item_id = uuid.uuid7()
    dispatch_event_id = uuid.uuid7()
    outbox_id = uuid.uuid7()

    await _run_alembic_command(command.upgrade, config, "0041")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO users (id, status, email, nsfw_enabled, language)
                VALUES (:user_id, 'active', 'legacy-recovery@example.com', false, 'any')
                """
            ),
            {"user_id": admin_user_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO memes (
                    id, media_type, primary_file_id, language, is_nsfw,
                    like_count, tags, visibility_mode, is_public
                ) VALUES (
                    :meme_id, 'video', :meme_file_id, 'none', false,
                    0, '{}', 'auto', true
                )
                """
            ),
            {"meme_id": meme_id, "meme_file_id": meme_file_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO meme_files (
                    id, meme_id, status, mime_type, s3_original_key,
                    s3_web_video_key, sha256_hex, quality_score
                ) VALUES (
                    :meme_file_id, :meme_id, 'ready', 'video/webm',
                    'legacy/original.webm', 'legacy/web.mp4', :sha256_hex, 1.0
                )
                """
            ),
            {
                "meme_file_id": meme_file_id,
                "meme_id": meme_id,
                "sha256_hex": "7" * 64,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO pipeline_stage_journal (
                    id, meme_file_id, stage, status, attempt_count,
                    normalized_reason, is_retryable, finished_at
                ) VALUES (
                    :stage_id, :meme_file_id, 'transcode', 'failed', 2,
                    'legacy_transcode_failure', true, now()
                )
                """
            ),
            {"stage_id": stage_id, "meme_file_id": meme_file_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO meme_file_sync_target_snapshots (
                    id, meme_file_id, sync_target, status, normalized_reason,
                    last_payload_preview, attempt_count, last_attempt_at
                ) VALUES (
                    :sync_target_id, :meme_file_id, 'qdrant', 'failed',
                    'legacy_sync_failure', '{}', 1, now()
                )
                """
            ),
            {"sync_target_id": sync_target_id, "meme_file_id": meme_file_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO recovery_jobs (
                    id, requested_by_admin_user_id, request_id, status, action,
                    reason, selection, total_count, completed_count, failed_count,
                    scheduled_at
                ) VALUES
                    (
                        :preview_stage_job_id, :admin_user_id, :preview_stage_request_id,
                        'preview', 'retry_stage', 'Review legacy stage',
                        '{"items": [{"kind": "pipeline_stage", "id": "stale", "version": "stale"}]}',
                        99, 7, 4, NULL
                    ),
                    (
                        :queued_stage_job_id, :admin_user_id, :queued_stage_request_id,
                        'queued', 'retry_stage', 'Run legacy stage',
                        '{"kind": "pipeline_stage", "id": "legacy"}',
                        99, 7, 4, now()
                    ),
                    (
                        :running_sync_job_id, :admin_user_id, :running_sync_request_id,
                        'running', 'resync_target', 'Run legacy sync',
                        '{"kind": "sync_target", "id": "legacy"}',
                        99, 7, 4, now()
                    ),
                    (
                        :preview_non_stage_job_id, :admin_user_id, :preview_non_stage_request_id,
                        'preview', 'rebuild_outbox', 'Review legacy outbox',
                        '{"items": []}', 99, 7, 4, NULL
                    )
                """
            ),
            {
                "admin_user_id": admin_user_id,
                "preview_stage_job_id": preview_stage_job_id,
                "preview_stage_request_id": uuid.uuid7(),
                "queued_stage_job_id": queued_stage_job_id,
                "queued_stage_request_id": uuid.uuid7(),
                "running_sync_job_id": running_sync_job_id,
                "running_sync_request_id": uuid.uuid7(),
                "preview_non_stage_job_id": preview_non_stage_job_id,
                "preview_non_stage_request_id": uuid.uuid7(),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO recovery_job_items (
                    id, recovery_job_id, work_kind, work_id, action,
                    expected_version, status, dispatch_event_id, canonical_version,
                    dispatched_at
                ) VALUES
                    (
                        :preview_stage_item_id, :preview_stage_job_id, 'pipeline_stage',
                        :stage_id, 'retry_stage', 'stage-version-reviewed', 'queued',
                        NULL, 'legacy-preview-canonical', NULL
                    ),
                    (
                        :queued_stage_item_id, :queued_stage_job_id, 'pipeline_stage',
                        :stage_id, 'retry_stage', 'stage-version-active', 'waiting_capacity',
                        NULL, NULL, NULL
                    ),
                    (
                        :running_sync_item_id, :running_sync_job_id, 'sync_target',
                        :sync_target_id, 'resync_target', 'sync-version-active', 'dispatched',
                        :dispatch_event_id, 'sync-canonical', now()
                    ),
                    (
                        :preview_non_stage_item_id, :preview_non_stage_job_id, 'outbox',
                        :outbox_id, 'rebuild_outbox', 'outbox-version-reviewed', 'queued',
                        NULL, NULL, NULL
                    )
                """
            ),
            {
                "preview_stage_item_id": preview_stage_item_id,
                "preview_stage_job_id": preview_stage_job_id,
                "queued_stage_item_id": queued_stage_item_id,
                "queued_stage_job_id": queued_stage_job_id,
                "running_sync_item_id": running_sync_item_id,
                "running_sync_job_id": running_sync_job_id,
                "preview_non_stage_item_id": preview_non_stage_item_id,
                "preview_non_stage_job_id": preview_non_stage_job_id,
                "stage_id": str(stage_id),
                "sync_target_id": str(sync_target_id),
                "dispatch_event_id": dispatch_event_id,
                "outbox_id": str(outbox_id),
            },
        )

    await _run_alembic_command(command.upgrade, config, "0042")

    async with engine.connect() as connection:
        job_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, requested_by_admin_user_id, assigned_admin_user_id,
                           status, scope, retry_limit, selection,
                           selected_root_count, expanded_execution_count,
                           total_count, completed_count, failed_count,
                           queued_count, waiting_count, dispatched_count,
                           succeeded_count, stale_count, skipped_count, cancelled_count,
                           selection_snapshot_at, materialization_completed_at,
                           cancelled_at, completed_at
                    FROM recovery_jobs
                    WHERE id = ANY(:job_ids)
                    """
                ),
                {
                    "job_ids": [
                        preview_stage_job_id,
                        queued_stage_job_id,
                        running_sync_job_id,
                        preview_non_stage_job_id,
                    ]
                },
            )
        ).all()
        item_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, status, action, meme_file_id, stage, is_root,
                           retry_limit, attempt_budget_start, retryable_failures_consumed,
                           preserve_ready, suppress_fanout, reservation_active,
                           dispatch_event_id, canonical_version, normalized_reason,
                           safe_error_text, dispatched_at, finished_at
                    FROM recovery_job_items
                    WHERE id = ANY(:item_ids)
                    """
                ),
                {
                    "item_ids": [
                        preview_stage_item_id,
                        queued_stage_item_id,
                        running_sync_item_id,
                        preview_non_stage_item_id,
                    ]
                },
            )
        ).all()
        reservation_index_definitions = dict(
            (
                await connection.execute(
                    text(
                        """
                        SELECT indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND indexname IN (
                              'uq_recovery_job_items_active_stage_reservation',
                              'uq_recovery_job_items_active_work_reservation'
                          )
                        """
                    )
                )
            ).tuples().all()
        )

    jobs = {row.id: row for row in job_rows}
    items = {row.id: row for row in item_rows}

    stage_preview = jobs[preview_stage_job_id]
    assert stage_preview.requested_by_admin_user_id == admin_user_id
    assert stage_preview.assigned_admin_user_id == admin_user_id
    assert (stage_preview.status, stage_preview.scope, stage_preview.retry_limit) == (
        "preview",
        "stage_only",
        3,
    )
    assert (
        stage_preview.selected_root_count,
        stage_preview.expanded_execution_count,
        stage_preview.total_count,
        stage_preview.completed_count,
        stage_preview.failed_count,
        stage_preview.queued_count,
    ) == (1, 1, 1, 0, 0, 1)
    assert stage_preview.selection_snapshot_at is not None
    assert stage_preview.materialization_completed_at is not None
    assert stage_preview.selection == {
        "selector": {
            "type": "explicit",
            "items": [
                {
                    "kind": "pipeline_stage",
                    "id": str(stage_id),
                    "version": "stage-version-reviewed",
                }
            ],
        },
        "scope": "stage_only",
        "retry_limit": 3,
        "acknowledgements": [],
    }

    stage_preview_item = items[preview_stage_item_id]
    assert (stage_preview_item.status, stage_preview_item.action) == (
        "queued",
        "regenerate_derivatives",
    )
    assert (stage_preview_item.meme_file_id, stage_preview_item.stage) == (
        meme_file_id,
        "transcode",
    )
    assert stage_preview_item.is_root is True
    assert stage_preview_item.retry_limit == 3
    assert stage_preview_item.attempt_budget_start is None
    assert stage_preview_item.retryable_failures_consumed == 0
    assert stage_preview_item.preserve_ready is True
    assert stage_preview_item.suppress_fanout is True
    assert stage_preview_item.reservation_active is False
    assert stage_preview_item.canonical_version is None

    for terminalized_job_id in (queued_stage_job_id, running_sync_job_id):
        terminalized = jobs[terminalized_job_id]
        assert terminalized.status == "cancelled"
        assert terminalized.selection["migration_terminalized"] is True
        assert terminalized.completed_count == 1
        assert terminalized.cancelled_count == 1
        assert terminalized.queued_count == 0
        assert terminalized.waiting_count == 0
        assert terminalized.dispatched_count == 0
        assert terminalized.cancelled_at is not None
        assert terminalized.completed_at is not None

    queued_stage_item = items[queued_stage_item_id]
    assert queued_stage_item.status == "cancelled"
    assert queued_stage_item.normalized_reason == "legacy_recovery_terminalized"
    assert queued_stage_item.safe_error_text
    assert queued_stage_item.finished_at is not None
    assert queued_stage_item.reservation_active is False
    assert (queued_stage_item.meme_file_id, queued_stage_item.stage) == (
        meme_file_id,
        "transcode",
    )

    running_sync_item = items[running_sync_item_id]
    assert running_sync_item.status == "cancelled"
    assert (running_sync_item.meme_file_id, running_sync_item.stage) == (
        meme_file_id,
        "sync_qdrant",
    )
    assert running_sync_item.preserve_ready is True
    assert running_sync_item.suppress_fanout is True
    assert running_sync_item.reservation_active is False
    # A pre-upgrade broker delivery may still arrive after this job is
    # terminalized. It must no longer resolve as recovery-owned work.
    assert running_sync_item.dispatch_event_id is None
    assert running_sync_item.canonical_version is None
    assert running_sync_item.dispatched_at is None

    non_stage_preview = jobs[preview_non_stage_job_id]
    non_stage_item = items[preview_non_stage_item_id]
    assert non_stage_preview.status == "preview"
    assert non_stage_preview.selection["selector"]["items"] == [
        {
            "kind": "outbox",
            "id": str(outbox_id),
            "version": "outbox-version-reviewed",
        }
    ]
    assert non_stage_item.meme_file_id is None
    assert non_stage_item.stage is None
    assert non_stage_item.preserve_ready is False
    assert non_stage_item.suppress_fanout is False
    assert non_stage_item.reservation_active is False

    stage_index = reservation_index_definitions["uq_recovery_job_items_active_stage_reservation"].lower()
    work_index = reservation_index_definitions["uq_recovery_job_items_active_work_reservation"].lower()
    assert "reservation_active" in stage_index and "stage is not null" in stage_index
    assert "reservation_active" in work_index and "stage is null" in work_index

    await _run_alembic_command(command.downgrade, config, "0041")
    async with engine.connect() as connection:
        downgraded_rows = (
            await connection.execute(
                text(
                    """
                        SELECT job.id,
                               job.status AS job_status,
                               item.action AS item_action,
                               item.status AS item_status
                    FROM recovery_jobs AS job
                    JOIN recovery_job_items AS item ON item.recovery_job_id = job.id
                    WHERE item.id IN (:preview_item_id, :queued_item_id, :running_item_id)
                    """
                ),
                {
                    "preview_item_id": preview_stage_item_id,
                    "queued_item_id": queued_stage_item_id,
                    "running_item_id": running_sync_item_id,
                },
            )
        ).all()
    downgraded = {row.id: row for row in downgraded_rows}
    assert downgraded[preview_stage_job_id].job_status == "preview"
    assert downgraded[preview_stage_job_id].item_action == "retry_stage"
    assert downgraded[queued_stage_job_id].job_status == "cancelled"
    assert downgraded[running_sync_job_id].job_status == "cancelled"
