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
