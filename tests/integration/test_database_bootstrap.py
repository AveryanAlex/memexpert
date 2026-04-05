"""Integration tests for the shared async database bootstrap layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from memexpert.api.app import create_app
from memexpert.core.config import get_settings
from memexpert.core.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    build_async_engine,
    normalize_async_database_url,
    verify_async_engine,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def test_create_app_stays_side_effect_free_with_invalid_database_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "not a valid database url")
    get_settings.cache_clear()

    try:
        app = create_app()
    finally:
        get_settings.cache_clear()

    assert app.title == "MemeXpert API"
    assert app.version == "0.1.0"


def test_normalize_async_database_url_promotes_plain_postgres_urls() -> None:
    database_url = "postgresql://memexpert:secret@example.com:5432/memexpert"

    assert normalize_async_database_url(database_url) == (
        "postgresql+asyncpg://memexpert:secret@example.com:5432/memexpert"
    )


def test_normalize_async_database_url_converts_sync_postgres_drivers() -> None:
    database_url = "postgresql+psycopg://memexpert:secret@example.com:5432/memexpert"

    assert normalize_async_database_url(database_url) == (
        "postgresql+asyncpg://memexpert:secret@example.com:5432/memexpert"
    )


def test_normalize_async_database_url_rejects_non_postgres_urls() -> None:
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL dialect"):
        _ = normalize_async_database_url("sqlite:///memexpert.db")


def test_build_async_engine_rejects_invalid_database_urls() -> None:
    with pytest.raises(DatabaseConfigurationError, match="valid SQLAlchemy URL"):
        _ = build_async_engine("not a valid database url")


def test_postgres_container_fixture_exposes_asyncpg_url(
    postgres_container_url: str,
) -> None:
    assert postgres_container_url.startswith("postgresql+asyncpg://")


async def test_verify_async_engine_reports_unreachable_databases() -> None:
    engine = build_async_engine(
        "postgresql+asyncpg://memexpert:memexpert@127.0.0.1:9/memexpert",
        connect_timeout=0.2,
    )

    try:
        with pytest.raises(DatabaseConnectionError, match=r"(Unable to connect|Timed out)"):
            await verify_async_engine(engine, timeout=0.5)
    finally:
        await engine.dispose()


async def test_postgres_session_factory_opens_real_sessions_without_eager_connect_regressions(
    postgres_async_engine: AsyncEngine,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert postgres_async_engine.url.drivername == "postgresql+asyncpg"

    for _ in range(3):
        async with postgres_session_factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
