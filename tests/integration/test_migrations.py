# ruff: noqa: I001,TC006
"""Integration tests for Alembic migrations against ephemeral PostgreSQL."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import inspect as sa_inspect, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"
EXPECTED_TABLES = {
    "account_deletion_logs",
    "account_merge_logs",
    "analytics_events",
    "channel_suggestions",
    "collection_invites",
    "collection_members",
    "collection_memes",
    "collections",
    "embedding_cache",
    "inline_usage_events",
    "login_events",
    "meme_file_ocr_results",
    "meme_file_sync_target_snapshots",
    "meme_files",
    "meme_merge_logs",
    "meme_popularity_snapshots",
    "meme_seo_pages",
    "meme_sources",
    "meme_templates",
    "memes",
    "moderation_decisions",
    "moderation_reports",
    "pinned_memes",
    "pipeline_stage_journal",
    "source_channels",
    "telegram_file_id_cache",
    "telegram_link_codes",
    "telegram_session_states",
    "users",
}
EXPECTED_MATERIALIZED_VIEWS = {
    "public_meme_trends_mv",
    "public_tag_trends_mv",
    "public_template_trends_mv",
}
ALEMBIC_TIMEOUT_SECONDS = 20.0


class PrimaryKeyConstraintInfo(TypedDict):
    constrained_columns: list[str]


class CheckConstraintInfo(TypedDict):
    name: str | None
    sqltext: str


class ForeignKeyInfo(TypedDict):
    name: str | None
    constrained_columns: list[str]
    referred_table: str


class ConstraintSnapshot(TypedDict):
    collection_members_pk: PrimaryKeyConstraintInfo
    users_fks: list[ForeignKeyInfo]
    memes_fks: list[ForeignKeyInfo]
    meme_sources_uniques: dict[str, list[str]]
    source_channels_uniques: dict[str, list[str]]
    embedding_cache_uniques: dict[str, list[str]]
    pipeline_stage_journal_uniques: dict[str, list[str]]
    pipeline_stage_journal_checks: dict[str, str]


def _build_alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


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
        raise AssertionError(f"Alembic {action.__name__} timed out after {timeout:.1f}s") from exc


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
        return {cast(str, name) for name in result.scalars()}


async def _get_current_revision(engine: AsyncEngine) -> str | None:
    table_names = await _get_table_names(engine)
    if "alembic_version" not in table_names:
        return None

    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        revision = result.scalar_one_or_none()
        return None if revision is None else cast(str, revision)


async def _get_index_definitions(engine: AsyncEngine, table_name: str) -> dict[str, str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema() AND tablename = :table_name
                ORDER BY indexname
                """
            ),
            {"table_name": table_name},
        )
        return {
            cast(str, mapping["indexname"]): cast(str, mapping["indexdef"])
            for mapping in result.mappings()
        }


async def _get_column_names(engine: AsyncEngine, table_name: str) -> set[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = :table_name
                ORDER BY ordinal_position
                """
            ),
            {"table_name": table_name},
        )
        return {cast(str, column_name) for column_name in result.scalars()}


async def _inspect_constraints(engine: AsyncEngine) -> ConstraintSnapshot:
    async with engine.connect() as connection:
        return await connection.run_sync(_inspect_constraints_sync)


def _inspect_constraints_sync(sync_connection: Connection) -> ConstraintSnapshot:
    inspector = sa_inspect(sync_connection)
    meme_sources_constraints = cast(list[dict[str, object]], inspector.get_unique_constraints("meme_sources"))
    source_channels_constraints = cast(list[dict[str, object]], inspector.get_unique_constraints("source_channels"))
    embedding_cache_constraints = cast(list[dict[str, object]], inspector.get_unique_constraints("embedding_cache"))
    pipeline_stage_journal_uniques = cast(
        list[dict[str, object]],
        inspector.get_unique_constraints("pipeline_stage_journal"),
    )
    pipeline_stage_journal_checks = cast(
        list[CheckConstraintInfo],
        inspector.get_check_constraints("pipeline_stage_journal"),
    )

    return {
        "collection_members_pk": cast(
            PrimaryKeyConstraintInfo,
            inspector.get_pk_constraint("collection_members"),
        ),
        "users_fks": cast(list[ForeignKeyInfo], inspector.get_foreign_keys("users")),
        "memes_fks": cast(list[ForeignKeyInfo], inspector.get_foreign_keys("memes")),
        "meme_sources_uniques": {
            cast(str, constraint["name"]): cast(list[str], constraint["column_names"])
            for constraint in meme_sources_constraints
        },
        "source_channels_uniques": {
            cast(str, constraint["name"]): cast(list[str], constraint["column_names"])
            for constraint in source_channels_constraints
        },
        "embedding_cache_uniques": {
            cast(str, constraint["name"]): cast(list[str], constraint["column_names"])
            for constraint in embedding_cache_constraints
        },
        "pipeline_stage_journal_uniques": {
            cast(str, constraint["name"]): cast(list[str], constraint["column_names"])
            for constraint in pipeline_stage_journal_uniques
        },
        "pipeline_stage_journal_checks": {
            str(constraint["name"]): constraint["sqltext"]
            for constraint in pipeline_stage_journal_checks
        },
    }


@pytest_asyncio.fixture
async def migrated_database(
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
) -> AsyncIterator[tuple[AsyncEngine, str]]:
    await _reset_public_schema(postgres_async_engine)
    try:
        yield postgres_async_engine, postgres_async_url
    finally:
        await _reset_public_schema(postgres_async_engine)


def test_initial_revision_metadata_is_present() -> None:
    config = _build_alembic_config()
    script_directory = ScriptDirectory.from_config(config)
    revision = script_directory.get_revision("head")

    assert revision is not None
    assert revision.revision == "0010"
    assert revision.down_revision == "0009"
    assert revision.doc == "moderation reports and decision audit"


async def test_upgrade_head_creates_expected_schema_and_constraints(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = migrated_database
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "head")

    table_names = await _get_table_names(engine)
    assert table_names == EXPECTED_TABLES | {"alembic_version"}
    assert await _get_current_revision(engine) == "0010"
    assert await _get_materialized_view_names(engine) == EXPECTED_MATERIALIZED_VIEWS

    users_indexes = await _get_index_definitions(engine, "users")
    collections_indexes = await _get_index_definitions(engine, "collections")
    meme_files_indexes = await _get_index_definitions(engine, "meme_files")
    meme_file_ocr_result_indexes = await _get_index_definitions(engine, "meme_file_ocr_results")
    meme_merge_log_indexes = await _get_index_definitions(engine, "meme_merge_logs")
    pipeline_stage_journal_indexes = await _get_index_definitions(engine, "pipeline_stage_journal")
    telegram_indexes = await _get_index_definitions(engine, "telegram_file_id_cache")
    telegram_link_indexes = await _get_index_definitions(engine, "telegram_link_codes")
    telegram_link_columns = await _get_column_names(engine, "telegram_link_codes")
    public_meme_trends_indexes = await _get_index_definitions(engine, "public_meme_trends_mv")
    public_tag_trends_indexes = await _get_index_definitions(engine, "public_tag_trends_mv")
    public_template_trends_indexes = await _get_index_definitions(engine, "public_template_trends_mv")
    meme_file_ocr_result_columns = await _get_column_names(engine, "meme_file_ocr_results")
    meme_merge_log_columns = await _get_column_names(engine, "meme_merge_logs")
    pipeline_stage_journal_columns = await _get_column_names(engine, "pipeline_stage_journal")
    constraints = await _inspect_constraints(engine)

    assert "uq_users_email_not_null" in users_indexes
    assert "email IS NOT NULL" in users_indexes["uq_users_email_not_null"]
    assert "uq_users_google_id_not_null" in users_indexes
    assert "google_id IS NOT NULL" in users_indexes["uq_users_google_id_not_null"]
    assert "uq_users_telegram_id_not_null" in users_indexes
    assert "telegram_id IS NOT NULL" in users_indexes["uq_users_telegram_id_not_null"]

    assert "uq_collections_one_favorites_per_owner" in collections_indexes
    favorites_index = collections_indexes["uq_collections_one_favorites_per_owner"]
    assert "owner_id" in favorites_index
    assert "favorites" in favorites_index

    assert constraints["collection_members_pk"]["constrained_columns"] == ["collection_id", "user_id"]
    assert any(
        foreign_key["name"] == "fk_users_active_save_collection_id_collections"
        and foreign_key["constrained_columns"] == ["active_save_collection_id"]
        and foreign_key["referred_table"] == "collections"
        for foreign_key in constraints["users_fks"]
    )

    assert "uq_meme_files_single_primary_per_meme" in meme_files_indexes
    assert "is_primary" in meme_files_indexes["uq_meme_files_single_primary_per_meme"]
    assert any(
        foreign_key["name"] == "fk_memes_primary_file_id_meme_files"
        and foreign_key["constrained_columns"] == ["primary_file_id"]
        and foreign_key["referred_table"] == "meme_files"
        for foreign_key in constraints["memes_fks"]
    )
    assert constraints["meme_sources_uniques"] == {
        "uq_meme_sources_platform_source_post": ["platform", "source_id", "post_id"],
    }
    assert constraints["source_channels_uniques"] == {
        "uq_source_channels_platform_platform_id": ["platform", "platform_id"],
    }
    assert constraints["embedding_cache_uniques"] == {
        "uq_embedding_cache_input_hash_model_version_input_type": ["input_hash", "model_version", "input_type"],
    }
    assert constraints["pipeline_stage_journal_uniques"] == {
        "uq_pipeline_stage_journal_meme_file_id_stage": ["meme_file_id", "stage"],
    }
    pipeline_stage_journal_checks = " ".join(constraints["pipeline_stage_journal_checks"].values()).lower()
    assert "attempt_count >= 0" in pipeline_stage_journal_checks
    assert "ingest" in pipeline_stage_journal_checks
    assert "transcode" in pipeline_stage_journal_checks
    assert "ocr" in pipeline_stage_journal_checks
    assert "embed" in pipeline_stage_journal_checks
    assert "classify" in pipeline_stage_journal_checks
    assert "sync_qdrant" in pipeline_stage_journal_checks
    assert "sync_meili" in pipeline_stage_journal_checks
    assert "duplicate" in pipeline_stage_journal_checks
    assert "ix_pipeline_stage_journal_last_event_id" in pipeline_stage_journal_indexes
    assert "last_event_id" in pipeline_stage_journal_indexes["ix_pipeline_stage_journal_last_event_id"]
    assert "ix_pipeline_stage_journal_meme_file_id_updated_at" in pipeline_stage_journal_indexes
    assert "meme_file_id" in pipeline_stage_journal_indexes["ix_pipeline_stage_journal_meme_file_id_updated_at"]
    assert "updated_at" in pipeline_stage_journal_indexes["ix_pipeline_stage_journal_meme_file_id_updated_at"]
    assert "ix_pipeline_stage_journal_stage_status" in pipeline_stage_journal_indexes
    assert "stage" in pipeline_stage_journal_indexes["ix_pipeline_stage_journal_stage_status"]
    assert "status" in pipeline_stage_journal_indexes["ix_pipeline_stage_journal_stage_status"]
    assert "ix_pipeline_stage_journal_status_retry_after" in pipeline_stage_journal_indexes
    assert "retry_after" in pipeline_stage_journal_indexes["ix_pipeline_stage_journal_status_retry_after"]
    assert pipeline_stage_journal_columns == {
        "attempt_count",
        "created_at",
        "finished_at",
        "id",
        "is_retryable",
        "last_error_text",
        "last_event_id",
        "meme_file_id",
        "normalized_reason",
        "retry_after",
        "stage",
        "started_at",
        "status",
        "updated_at",
    }
    assert "uq_meme_file_ocr_results_meme_file_id" in meme_file_ocr_result_indexes
    assert "UNIQUE INDEX uq_meme_file_ocr_results_meme_file_id" in meme_file_ocr_result_indexes[
        "uq_meme_file_ocr_results_meme_file_id"
    ]
    assert "ix_meme_file_ocr_results_engine_updated_at" in meme_file_ocr_result_indexes
    assert "engine" in meme_file_ocr_result_indexes["ix_meme_file_ocr_results_engine_updated_at"]
    assert "ix_meme_file_ocr_results_low_confidence_updated_at" in meme_file_ocr_result_indexes
    assert "low_confidence" in meme_file_ocr_result_indexes[
        "ix_meme_file_ocr_results_low_confidence_updated_at"
    ]
    assert meme_file_ocr_result_columns == {
        "confidence",
        "created_at",
        "engine",
        "extracted_text",
        "fallback_engine",
        "fallback_used",
        "id",
        "language",
        "last_event_id",
        "low_confidence",
        "meme_file_id",
        "source_object_key",
        "updated_at",
    }
    assert "ix_meme_merge_logs_source_meme_file_id" in meme_merge_log_indexes
    assert "source_meme_file_id" in meme_merge_log_indexes["ix_meme_merge_logs_source_meme_file_id"]
    assert "ix_meme_merge_logs_source_meme_id_created_at" in meme_merge_log_indexes
    assert "source_meme_id" in meme_merge_log_indexes["ix_meme_merge_logs_source_meme_id_created_at"]
    assert "ix_meme_merge_logs_target_meme_id_created_at" in meme_merge_log_indexes
    assert "target_meme_id" in meme_merge_log_indexes["ix_meme_merge_logs_target_meme_id_created_at"]
    assert meme_merge_log_columns == {
        "created_at",
        "details",
        "id",
        "merge_reason",
        "similarity_score",
        "source_meme_file_id",
        "source_meme_id",
        "target_meme_id",
        "target_primary_file_id",
    }
    assert "uq_telegram_file_id_cache_file_format_scope" in telegram_indexes
    assert "UNIQUE INDEX uq_telegram_file_id_cache_file_format_scope" in telegram_indexes[
        "uq_telegram_file_id_cache_file_format_scope"
    ]
    assert telegram_link_columns == {
        "guest_user_id",
        "code_hash",
        "expires_at",
        "redeemed_at",
        "redeemed_by_telegram_id",
        "id",
        "created_at",
        "updated_at",
    }
    assert "uq_telegram_link_codes_code_hash" in telegram_link_indexes
    assert "UNIQUE INDEX uq_telegram_link_codes_code_hash" in telegram_link_indexes[
        "uq_telegram_link_codes_code_hash"
    ]
    assert "ix_telegram_link_codes_guest_user_id_expires_at" in telegram_link_indexes
    assert "guest_user_id" in telegram_link_indexes["ix_telegram_link_codes_guest_user_id_expires_at"]
    assert "expires_at" in telegram_link_indexes["ix_telegram_link_codes_guest_user_id_expires_at"]
    assert "ix_telegram_link_codes_redeemed_by_telegram_id_redeemed_at" in telegram_link_indexes
    assert "uq_public_meme_trends_mv_meme_id" in public_meme_trends_indexes
    assert "UNIQUE" in public_meme_trends_indexes["uq_public_meme_trends_mv_meme_id"].upper()
    assert "ix_public_meme_trends_mv_trending" in public_meme_trends_indexes
    assert "ix_public_meme_trends_mv_fastest_rising" in public_meme_trends_indexes
    assert "ix_public_meme_trends_mv_most_liked" in public_meme_trends_indexes
    assert "ix_public_meme_trends_mv_template_id" in public_meme_trends_indexes
    assert "ix_public_meme_trends_mv_tags" in public_meme_trends_indexes
    assert "USING gin" in public_meme_trends_indexes["ix_public_meme_trends_mv_tags"]
    assert "uq_public_tag_trends_mv_tag" in public_tag_trends_indexes
    assert "ix_public_tag_trends_mv_trending" in public_tag_trends_indexes
    assert "uq_public_template_trends_mv_template_id" in public_template_trends_indexes
    assert "uq_public_template_trends_mv_template_slug" in public_template_trends_indexes
    assert "ix_public_template_trends_mv_trending" in public_template_trends_indexes


async def test_sync_target_snapshots_schema_is_created_with_expected_shape(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = migrated_database
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "head")

    snapshot_indexes = await _get_index_definitions(engine, "meme_file_sync_target_snapshots")
    snapshot_columns = await _get_column_names(engine, "meme_file_sync_target_snapshots")

    assert snapshot_columns == {
        "attempt_count",
        "created_at",
        "id",
        "last_attempt_at",
        "last_error_text",
        "last_event_id",
        "last_payload_preview",
        "last_success_at",
        "meme_file_id",
        "normalized_reason",
        "status",
        "sync_target",
        "updated_at",
    }
    assert "uq_meme_file_sync_target_snapshots_meme_file_id_sync_target" in snapshot_indexes
    unique_index_definition = snapshot_indexes[
        "uq_meme_file_sync_target_snapshots_meme_file_id_sync_target"
    ]
    assert "meme_file_id" in unique_index_definition
    assert "sync_target" in unique_index_definition
    assert "UNIQUE" in unique_index_definition.upper()
    assert "ix_meme_file_sync_target_snapshots_target_updated_at" in snapshot_indexes
    target_updated_index_definition = snapshot_indexes[
        "ix_meme_file_sync_target_snapshots_target_updated_at"
    ]
    assert "sync_target" in target_updated_index_definition
    assert "updated_at" in target_updated_index_definition


async def test_downgrade_0005_removes_sync_target_snapshots_table(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = migrated_database
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "head")
    assert "meme_file_sync_target_snapshots" in await _get_table_names(engine)

    # Downgrade past 0006 first so the test can assert 0005 in isolation.
    await _run_alembic_command(command.downgrade, config, "0005")
    await _run_alembic_command(command.downgrade, config, "0004")

    remaining_tables = await _get_table_names(engine)
    assert "meme_file_sync_target_snapshots" not in remaining_tables
    assert await _get_current_revision(engine) == "0004"


async def test_crawler_sources_migration_applies_and_reverses(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = migrated_database
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "head")
    assert await _get_current_revision(engine) == "0010"

    meme_sources_columns = await _get_column_names(engine, "meme_sources")
    source_channels_columns = await _get_column_names(engine, "source_channels")
    telegram_session_states_columns = await _get_column_names(
        engine,
        "telegram_session_states",
    )

    assert {
        "is_admin",
        "token_nonce",
    }.issubset(await _get_column_names(engine, "users"))
    assert {
        "published_at",
        "forwarded_from_source_id",
        "forwarded_from_post_id",
    }.issubset(meme_sources_columns)
    assert {
        "catchup_message_limit",
        "catchup_enabled",
        "is_paused",
        "last_fetched_at",
    }.issubset(source_channels_columns)
    assert telegram_session_states_columns == {
        "id",
        "session_name",
        "status",
        "last_error_class",
        "last_error_text",
        "flood_wait_until",
        "live_listener_started_at",
        "last_heartbeat_at",
        "quarantined_at",
        "created_at",
        "updated_at",
    }

    telegram_session_indexes = await _get_index_definitions(
        engine,
        "telegram_session_states",
    )
    assert "uq_telegram_session_states_session_name" in telegram_session_indexes
    assert "session_name" in telegram_session_indexes[
        "uq_telegram_session_states_session_name"
    ]
    assert "UNIQUE" in telegram_session_indexes[
        "uq_telegram_session_states_session_name"
    ].upper()
    assert "ix_telegram_session_states_status_updated_at" in telegram_session_indexes
    status_updated_index = telegram_session_indexes[
        "ix_telegram_session_states_status_updated_at"
    ]
    assert "status" in status_updated_index
    assert "updated_at" in status_updated_index

    await _run_alembic_command(command.downgrade, config, "0005")
    assert await _get_current_revision(engine) == "0005"

    downgraded_tables = await _get_table_names(engine)
    assert "telegram_session_states" not in downgraded_tables

    downgraded_meme_sources_columns = await _get_column_names(engine, "meme_sources")
    downgraded_source_channels_columns = await _get_column_names(engine, "source_channels")
    assert "published_at" not in downgraded_meme_sources_columns
    assert "forwarded_from_source_id" not in downgraded_meme_sources_columns
    assert "forwarded_from_post_id" not in downgraded_meme_sources_columns
    assert "catchup_message_limit" not in downgraded_source_channels_columns
    assert "catchup_enabled" not in downgraded_source_channels_columns
    assert "is_paused" not in downgraded_source_channels_columns
    assert "last_fetched_at" not in downgraded_source_channels_columns


async def test_downgrade_base_reverses_the_schema_cleanly(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = migrated_database
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "head")
    await _run_alembic_command(command.downgrade, config, "base")

    assert await _get_table_names(engine) == {"alembic_version"}
    assert await _get_current_revision(engine) is None


async def test_repeated_fresh_database_upgrades_work_after_a_full_downgrade(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, database_url = migrated_database
    config = _build_alembic_config(database_url)

    await _run_alembic_command(command.upgrade, config, "head")
    await _run_alembic_command(command.downgrade, config, "base")
    await _run_alembic_command(command.upgrade, config, "head")

    assert await _get_current_revision(engine) == "0010"
    assert EXPECTED_TABLES.issubset(await _get_table_names(engine))


async def test_invalid_database_urls_fail_before_any_schema_is_created(
    migrated_database: tuple[AsyncEngine, str],
) -> None:
    engine, _database_url = migrated_database
    config = _build_alembic_config("sqlite:///memexpert.db")

    with pytest.raises(CommandError, match="Unable to resolve the Alembic database URL"):
        await _run_alembic_command(command.upgrade, config, "head")

    assert await _get_table_names(engine) == set()
