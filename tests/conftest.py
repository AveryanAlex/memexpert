"""Shared pytest fixtures for API and PostgreSQL integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer  # pyright: ignore[reportMissingTypeStubs]
from testcontainers.redis import RedisContainer  # pyright: ignore[reportMissingTypeStubs]

from alembic import command
from memexpert.api.app import create_app
from memexpert.core.config import get_settings
from memexpert.core.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    build_async_engine,
    build_async_session_factory,
    normalize_async_database_url,
    reset_async_database_state,
    verify_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

TEST_POSTGRES_IMAGE: Final = "postgres:16"
TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS: Final = 10.0
ALEMBIC_COMMAND_TIMEOUT_SECONDS: Final = 20.0
PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
ALEMBIC_INI_PATH: Final = PROJECT_ROOT / "alembic.ini"
TEST_BASE_URL: Final = "https://testserver"
AUTH_TEST_JWT_SECRET: Final = "route-test-auth-secret-with-32-byte-minimum"
AUTH_TEST_REFRESH_COOKIE_NAME: Final = "route_refresh_token"
AUTH_TEST_REFRESH_COOKIE_SAMESITE: Final = "strict"
AUTH_TEST_TELEGRAM_BOT_TOKEN: Final = "123456:telegram-route-test-bot-token"
AUTH_TEST_TELEGRAM_BOT_USERNAME: Final = "memexpertbot"
AUTH_TEST_TELEGRAM_LOGIN_MAX_AGE_SECONDS: Final = 300
AUTH_TEST_TELEGRAM_MINIAPP_MAX_AGE_SECONDS: Final = 300
AUTH_TEST_TELEGRAM_LINK_CODE_TTL_SECONDS: Final = 600
AUTH_TEST_TELEGRAM_LINK_RETURN_URL: Final = "https://memexpert.test/link/telegram/complete"
AUTH_TEST_GOOGLE_CLIENT_ID: Final = "route-test-google-client-id"
AUTH_TEST_GOOGLE_CLIENT_SECRET: Final = "route-test-google-client-secret"
AUTH_TEST_GOOGLE_REDIRECT_URI: Final = "https://testserver/auth/google/callback"
AUTH_TEST_GOOGLE_TOKEN_URL: Final = "https://google.test/token"
AUTH_TEST_GOOGLE_USERINFO_URL: Final = "https://google.test/userinfo"
AUTH_TEST_GOOGLE_TIMEOUT_SECONDS: Final = 5.0


def _build_alembic_config(database_url: str) -> Config:
    """Construct an Alembic config bound to the ephemeral PostgreSQL URL."""

    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


async def _run_alembic_upgrade(database_url: str) -> None:
    """Apply the project's Alembic head revision with a bounded timeout."""

    config = _build_alembic_config(database_url)
    try:
        async with asyncio.timeout(ALEMBIC_COMMAND_TIMEOUT_SECONDS):
            await asyncio.to_thread(command.upgrade, config, "head")
    except TimeoutError as exc:  # pragma: no cover - exercised only on failure
        raise AssertionError(
            f"Alembic upgrade timed out after {ALEMBIC_COMMAND_TIMEOUT_SECONDS:.1f}s",
        ) from exc


async def _reset_public_schema(engine: AsyncEngine) -> None:
    """Drop and recreate the public schema for migration-backed integration tests."""

    async with engine.begin() as connection:
        _ = await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        _ = await connection.execute(text("CREATE SCHEMA public"))


async def reset_test_runtime_state() -> None:
    """Clear cached settings and global async engine state before env-driven app tests."""

    get_settings.cache_clear()
    await reset_async_database_state()


@pytest_asyncio.fixture(autouse=True)
async def _reset_runtime_state_between_tests() -> AsyncIterator[None]:
    """Prevent stale settings and engine caches from leaking across tests."""

    await reset_test_runtime_state()
    yield
    await reset_test_runtime_state()


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh FastAPI app instance for each test."""

    return create_app()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPS async HTTP client bound directly to the ASGI app."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as async_client:
        yield async_client


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


@pytest_asyncio.fixture(loop_scope="session")
async def migrated_db_session(
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session bound to a freshly migrated PostgreSQL schema."""

    await _reset_public_schema(postgres_async_engine)
    await _run_alembic_upgrade(postgres_async_url)

    async with postgres_session_factory() as session:
        yield session
        await session.rollback()

    await _reset_public_schema(postgres_async_engine)


@pytest.fixture
def auth_settings_overrides(postgres_async_url: str) -> dict[str, str]:
    """Return env overrides used by route tests to prove cache resets and secure cookies."""

    return {
        "DATABASE_URL": postgres_async_url,
        "AUTH_JWT_SECRET": AUTH_TEST_JWT_SECRET,
        "AUTH_REFRESH_COOKIE_NAME": AUTH_TEST_REFRESH_COOKIE_NAME,
        "AUTH_REFRESH_COOKIE_SAMESITE": AUTH_TEST_REFRESH_COOKIE_SAMESITE,
        "AUTH_REFRESH_COOKIE_SECURE": "true",
        "AUTH_TELEGRAM_BOT_TOKEN": AUTH_TEST_TELEGRAM_BOT_TOKEN,
        "AUTH_TELEGRAM_BOT_USERNAME": AUTH_TEST_TELEGRAM_BOT_USERNAME,
        "AUTH_TELEGRAM_LOGIN_MAX_AGE_SECONDS": str(AUTH_TEST_TELEGRAM_LOGIN_MAX_AGE_SECONDS),
        "AUTH_TELEGRAM_MINIAPP_MAX_AGE_SECONDS": str(AUTH_TEST_TELEGRAM_MINIAPP_MAX_AGE_SECONDS),
        "AUTH_TELEGRAM_LINK_CODE_TTL_SECONDS": str(AUTH_TEST_TELEGRAM_LINK_CODE_TTL_SECONDS),
        "AUTH_TELEGRAM_LINK_RETURN_URL": AUTH_TEST_TELEGRAM_LINK_RETURN_URL,
        "AUTH_GOOGLE_CLIENT_ID": AUTH_TEST_GOOGLE_CLIENT_ID,
        "AUTH_GOOGLE_CLIENT_SECRET": AUTH_TEST_GOOGLE_CLIENT_SECRET,
        "AUTH_GOOGLE_REDIRECT_URI": AUTH_TEST_GOOGLE_REDIRECT_URI,
        "AUTH_GOOGLE_TOKEN_URL": AUTH_TEST_GOOGLE_TOKEN_URL,
        "AUTH_GOOGLE_USERINFO_URL": AUTH_TEST_GOOGLE_USERINFO_URL,
        "AUTH_GOOGLE_TIMEOUT_SECONDS": str(AUTH_TEST_GOOGLE_TIMEOUT_SECONDS),
    }


@pytest_asyncio.fixture
async def auth_app(
    migrated_db_session: AsyncSession,
    auth_settings_overrides: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build an app wired to the migrated PostgreSQL test DB and auth-specific env overrides."""

    _ = migrated_db_session
    for key, value in auth_settings_overrides.items():
        monkeypatch.setenv(key, value)

    await reset_test_runtime_state()
    yield create_app()
    await reset_test_runtime_state()


@pytest_asyncio.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPS client for auth-route integration tests with truthful secure cookies."""

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as async_client:
        yield async_client


def _build_redis_url(container: RedisContainer) -> str:
    """Construct a redis:// URL from a started Redis testcontainer."""

    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def redis_container_url() -> Iterator[str]:
    """Yield a Redis connection URL for future integration tests."""

    with RedisContainer("redis:7") as redis:
        yield _build_redis_url(redis)
