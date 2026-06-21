"""Smoke tests for Alembic migrations against ephemeral PostgreSQL."""

from __future__ import annotations

import asyncio
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
