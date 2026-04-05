# ruff: noqa: I001
"""Alembic environment wired to MemeXpert async PostgreSQL settings and metadata."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING, Final

from alembic import context
from alembic.util.exc import CommandError
from sqlalchemy import MetaData, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

from memexpert.core.config import get_settings
from memexpert.core.database import (
    DatabaseConfigurationError,
    get_database_url,
    normalize_async_database_url,
)
from memexpert.models import metadata as model_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

REQUIRED_TABLES: Final[frozenset[str]] = frozenset({"users", "collections", "memes"})
target_metadata = model_metadata


def _validated_target_metadata() -> MetaData:
    table_names = set(target_metadata.tables)
    if not table_names:
        raise CommandError(
            "Alembic target metadata is empty; ensure memexpert.models is imported before migrations run.",
        )

    missing_tables = sorted(REQUIRED_TABLES - table_names)
    if missing_tables:
        raise CommandError(
            "Alembic target metadata is missing required tables: " + ", ".join(missing_tables),
        )

    return target_metadata


def _resolve_database_url() -> str:
    x_arguments = context.get_x_argument(as_dictionary=True)
    explicit_url = (
        config.attributes.get("database_url")
        or x_arguments.get("database_url")
        or x_arguments.get("sqlalchemy_url")
        or config.get_main_option("sqlalchemy.url")
    )

    try:
        if explicit_url:
            return normalize_async_database_url(str(explicit_url))

        get_settings.cache_clear()
        return get_database_url()
    except DatabaseConfigurationError as exc:
        raise CommandError(f"Unable to resolve the Alembic database URL: {exc}") from exc


def _configure_database_url() -> str:
    database_url = _resolve_database_url()
    config.set_main_option("sqlalchemy.url", database_url)
    return database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""

    database_url = _configure_database_url()
    context.configure(
        url=database_url,
        target_metadata=_validated_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations using a provided synchronous SQLAlchemy connection."""

    context.configure(
        connection=connection,
        target_metadata=_validated_target_metadata(),
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a sync callback."""

    _ = _configure_database_url()
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
