"""Shared async SQLAlchemy engine and session helpers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from memexpert.core.config import Settings, get_settings

POSTGRES_ASYNC_DRIVERNAME: Final = "postgresql+asyncpg"
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final = 5.0

type AsyncSessionFactory = async_sessionmaker[AsyncSession]


class DatabaseConfigurationError(ValueError):
    """Raised when the configured database URL cannot produce an async engine."""


class DatabaseConnectionError(RuntimeError):
    """Raised when an async engine cannot establish a real database connection."""


_engine: AsyncEngine | None = None
_session_factory: AsyncSessionFactory | None = None


def normalize_async_database_url(database_url: str) -> str:
    """Normalize PostgreSQL URLs so SQLAlchemy always receives an asyncpg driver."""

    normalized_input = database_url.strip()
    if not normalized_input:
        raise DatabaseConfigurationError(
            "Database URL is required before constructing the async engine.",
        )

    try:
        parsed_url = make_url(normalized_input)
    except ArgumentError as exc:
        raise DatabaseConfigurationError(
            "Database URL must be a valid SQLAlchemy URL.",
        ) from exc

    drivername = parsed_url.drivername
    is_postgres_url = drivername in {
        "postgres",
        "postgresql",
        "postgres+asyncpg",
        POSTGRES_ASYNC_DRIVERNAME,
    } or drivername.startswith(("postgres+", "postgresql+"))
    if not is_postgres_url:
        raise DatabaseConfigurationError(
            "Database URL must use a PostgreSQL dialect supported by asyncpg.",
        )

    if drivername != POSTGRES_ASYNC_DRIVERNAME:
        parsed_url = parsed_url.set(drivername=POSTGRES_ASYNC_DRIVERNAME)

    return parsed_url.render_as_string(hide_password=False)


def get_database_url(settings: Settings | None = None) -> str:
    """Return the configured database URL after async-driver normalization."""

    resolved_settings = settings or get_settings()
    return normalize_async_database_url(resolved_settings.database_url)


def _build_connect_args(
    database_url: str,
    *,
    connect_timeout: float,
    connect_args: Mapping[str, object] | None = None,
) -> dict[str, object]:
    resolved_connect_args = dict(connect_args or {})
    if database_url.startswith(f"{POSTGRES_ASYNC_DRIVERNAME}://"):
        _ = resolved_connect_args.setdefault("timeout", connect_timeout)
    return resolved_connect_args


def build_async_engine(
    database_url: str | None = None,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    pool_pre_ping: bool = True,
    connect_args: Mapping[str, object] | None = None,
    **engine_options: object,
) -> AsyncEngine:
    """Build a lazy async SQLAlchemy engine from the configured database URL."""

    resolved_database_url = (
        normalize_async_database_url(database_url)
        if database_url is not None
        else get_database_url()
    )
    resolved_connect_args = _build_connect_args(
        resolved_database_url,
        connect_timeout=connect_timeout,
        connect_args=connect_args,
    )

    try:
        return create_async_engine(
            resolved_database_url,
            pool_pre_ping=pool_pre_ping,
            connect_args=resolved_connect_args,
            **engine_options,
        )
    except (ArgumentError, TypeError, ValueError) as exc:
        raise DatabaseConfigurationError(
            "Unable to construct the async SQLAlchemy engine from the configured database URL.",
        ) from exc


def build_async_session_factory(engine: AsyncEngine | None = None) -> AsyncSessionFactory:
    """Build an async SQLAlchemy session factory bound to the provided engine."""

    return async_sessionmaker(
        bind=engine or get_async_engine(),
        autoflush=False,
        expire_on_commit=False,
    )


def get_async_engine() -> AsyncEngine:
    """Return the process-wide async SQLAlchemy engine, creating it lazily."""

    global _engine
    if _engine is None:
        _engine = build_async_engine()
    return _engine


def get_async_session_factory() -> AsyncSessionFactory:
    """Return the cached async session factory for services and API dependencies."""

    global _session_factory
    if _session_factory is None:
        _session_factory = build_async_session_factory()
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield an async SQLAlchemy session for FastAPI dependencies and services."""

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        yield session


async def verify_async_engine(
    engine: AsyncEngine,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> None:
    """Open and validate a real database connection with a bounded timeout."""

    redacted_url = engine.url.render_as_string(hide_password=True)

    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as connection:
                _ = await connection.execute(text("SELECT 1"))
    except TimeoutError as exc:
        raise DatabaseConnectionError(
            f"Timed out after {timeout:.1f}s while connecting to database {redacted_url}.",
        ) from exc
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Unable to connect to database {redacted_url}: {exc}",
        ) from exc


async def reset_async_database_state() -> None:
    """Dispose the cached engine and clear cached session helpers."""

    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()

    _engine = None
    _session_factory = None


__all__ = [
    "AsyncSessionFactory",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "POSTGRES_ASYNC_DRIVERNAME",
    "build_async_engine",
    "build_async_session_factory",
    "get_async_engine",
    "get_async_session_factory",
    "get_database_url",
    "get_db_session",
    "normalize_async_database_url",
    "reset_async_database_state",
    "verify_async_engine",
]
