"""Shared pytest fixtures for API and PostgreSQL integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer  # pyright: ignore[reportMissingTypeStubs]
from testcontainers.redis import RedisContainer  # pyright: ignore[reportMissingTypeStubs]

from memexpert.api.app import create_app
from memexpert.core.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    build_async_engine,
    build_async_session_factory,
    normalize_async_database_url,
    verify_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

TEST_POSTGRES_IMAGE: Final = "postgres:16"
TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS: Final = 10.0


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh FastAPI app instance for each test."""

    return create_app()


@pytest_asyncio.fixture(loop_scope="session")
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client bound directly to the ASGI app."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _build_redis_url(container: RedisContainer) -> str:
    """Construct a redis:// URL from a started Redis testcontainer."""

    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def postgres_container_url() -> Iterator[str]:
    """Yield an asyncpg-compatible PostgreSQL URL for integration tests."""

    with PostgresContainer(TEST_POSTGRES_IMAGE) as postgres:
        raw_database_url = postgres.get_connection_url()
        try:
            normalized_database_url = normalize_async_database_url(raw_database_url)
        except DatabaseConfigurationError as exc:
            pytest.fail(f"PostgreSQL testcontainer returned an unusable database URL: {exc}")

        yield normalized_database_url


@pytest.fixture(scope="session")
def postgres_async_url(postgres_container_url: str) -> str:
    """Expose the normalized async PostgreSQL URL under an explicit fixture name."""

    return postgres_container_url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgres_async_engine(postgres_async_url: str) -> AsyncIterator[AsyncEngine]:
    """Yield a reusable async SQLAlchemy engine bound to the testcontainer."""

    try:
        engine = build_async_engine(
            postgres_async_url,
            connect_timeout=TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS,
        )
    except DatabaseConfigurationError as exc:
        pytest.fail(f"Unable to build the async PostgreSQL test engine: {exc}")

    try:
        await verify_async_engine(
            engine,
            timeout=TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS,
        )
    except DatabaseConnectionError as exc:
        await engine.dispose()
        pytest.fail(f"Unable to initialize the async PostgreSQL test engine: {exc}")

    yield engine

    await engine.dispose()


@pytest.fixture(scope="session")
def postgres_session_factory(
    postgres_async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Yield a reusable async SQLAlchemy sessionmaker for integration tests."""

    return build_async_session_factory(postgres_async_engine)


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a short-lived async database session for integration tests."""

    async with postgres_session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture(scope="session")
def redis_container_url() -> Iterator[str]:
    """Yield a Redis connection URL for future integration tests."""

    with RedisContainer("redis:7") as redis:
        yield _build_redis_url(redis)
