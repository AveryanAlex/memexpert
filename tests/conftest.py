"""Shared pytest fixtures for API and PostgreSQL integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from testcontainers.core.container import DockerContainer  # pyright: ignore[reportMissingTypeStubs]
from testcontainers.core.waiting_utils import (  # pyright: ignore[reportMissingTypeStubs]
    WaitStrategy,
    WaitStrategyTarget,
)
from testcontainers.postgres import PostgresContainer  # pyright: ignore[reportMissingTypeStubs]

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
from memexpert.core.redis import reset_async_redis_state
from memexpert.models.enums import UserLanguage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from datetime import datetime

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from memexpert.schemas.user import UserRead
    from memexpert.services.user_service import UserService

TEST_POSTGRES_IMAGE: Final = "postgres:16"
TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS: Final = 10.0
TEST_REDIS_IMAGE: Final = "redis:7"
TEST_REDIS_PORT: Final = 6379
TEST_REDIS_CONNECT_TIMEOUT_SECONDS: Final = 1.0
ALEMBIC_COMMAND_TIMEOUT_SECONDS: Final = 60.0
PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
ALEMBIC_INI_PATH: Final = PROJECT_ROOT / "alembic.ini"
TEST_BASE_URL: Final = "https://testserver"
AUTH_TEST_JWT_SECRET: Final = "route-test-auth-secret-with-32-byte-minimum"
SECURITY_TEST_UNAVAILABLE_REDIS_URL: Final = "redis://127.0.0.1:1/0"
SECURITY_TEST_REDIS_TIMEOUT_SECONDS: Final = 0.1
BROWSER_SECURITY_ALLOWED_ORIGINS: Final = (
    "https://memexpert.net",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://web.telegram.org",
    "https://oauth.telegram.org",
)
BROWSER_SECURITY_ALLOWED_ORIGIN_REGEX: Final = r"^https://([a-z0-9-]+\.)?memexpert\.net$"
BROWSER_SECURITY_ALLOWED_METHODS: Final = "DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT"
BROWSER_SECURITY_ALLOWED_HEADERS: Final = "Authorization,Content-Type,X-Requested-With"
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


class RedisPingWaitStrategy(WaitStrategy):
    """Wait for Redis with the structured testcontainers wait-strategy API.

    ``testcontainers.redis.RedisContainer`` still relies on the deprecated
    ``@wait_container_is_ready`` decorator at import time, which emits a warning
    for every pytest run. The generic container plus this ping strategy keeps the
    fixture behavior equivalent without importing the deprecated Redis wrapper.
    """

    def __init__(self, *, port: int = TEST_REDIS_PORT) -> None:
        super().__init__()
        self._port = port
        self.with_transient_exceptions(RedisError, OSError)

    def wait_until_ready(self, container: WaitStrategyTarget) -> None:
        """Block until the mapped Redis port accepts PING."""

        def can_ping() -> bool:
            client = Redis(
                host=container.get_container_host_ip(),
                port=container.get_exposed_port(self._port),
                socket_connect_timeout=TEST_REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=TEST_REDIS_CONNECT_TIMEOUT_SECONDS,
            )
            try:
                return bool(client.ping())
            finally:
                client.close()

        if not self._poll(can_ping):
            raise TimeoutError(
                "Redis testcontainer did not become ready before the startup timeout elapsed.",
            )


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


async def reset_test_runtime_state(*, flush_redis: bool = False) -> None:
    """Clear cached settings plus global DB/Redis runtime state before env-driven app tests."""

    get_settings.cache_clear()
    await reset_async_database_state()
    await reset_async_redis_state(flushdb=flush_redis)


async def create_full_user_via_upgrade(
    user_service: UserService,
    *,
    telegram_id: int | None = None,
    google_id: str | None = None,
    email: str | None = None,
    email_verified_at: datetime | None = None,
    password_hash: str | None = None,
    language: UserLanguage = UserLanguage.ANY,
    nsfw_enabled: bool = False,
) -> UserRead:
    """Test-only seed helper: create a guest then upgrade it atomically.

    Production code owns exactly one writer path for full accounts
    (``AccountLinkService.link_guest_with_*``). Tests that need an existing
    full-account fixture — to exercise "returning user" branches of the
    writer path or of other services — use this helper instead of calling
    ``UserService.create_full_user``.

    The helper bootstraps the guest with ``commit=False`` so a validation
    failure during the upgrade rolls the guest row back atomically, matching
    the "rejected before commit" invariants that the deleted
    ``create_full_user`` method enforced.
    """

    guest = await user_service.create_guest_user(
        language=language, nsfw_enabled=nsfw_enabled, commit=False,
    )
    return await user_service.upgrade_guest_to_full_account(
        user_id=guest.id,
        telegram_id=telegram_id,
        google_id=google_id,
        email=email,
        email_verified_at=email_verified_at,
        password_hash=password_hash,
    )


@pytest_asyncio.fixture(autouse=True)
async def _reset_runtime_state_between_tests() -> AsyncIterator[None]:
    """Prevent stale settings and engine caches from leaking across tests."""

    await reset_test_runtime_state()
    yield
    await reset_test_runtime_state()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a fresh FastAPI app instance for non-security tests.

    Generic route/service tests are not about Redis-backed abuse controls, so
    they run with shared rate limiting disabled unless they opt into one of the
    dedicated security fixtures below.
    """

    monkeypatch.setenv("SECURITY_RATE_LIMIT_ENABLED", "false")

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
def auth_settings_overrides(
    postgres_async_url: str,
    redis_container_url: str,
) -> dict[str, str]:
    """Return env overrides used by route tests to prove cache resets and secure cookies."""

    return {
        "DATABASE_URL": postgres_async_url,
        "REDIS_URL": redis_container_url,
        "AUTH_JWT_SECRET": AUTH_TEST_JWT_SECRET,
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
        "SECURITY_RATE_LIMIT_REDIS_TIMEOUT_SECONDS": str(SECURITY_TEST_REDIS_TIMEOUT_SECONDS),
    }


@pytest.fixture
def security_settings_overrides(
    auth_settings_overrides: dict[str, str],
    redis_container_url: str,
) -> dict[str, str]:
    """Return auth-route overrides plus Redis and browser-cookie defaults for security tests."""

    return {
        **auth_settings_overrides,
        "REDIS_URL": redis_container_url,
        "SECURITY_RATE_LIMIT_REDIS_TIMEOUT_SECONDS": str(SECURITY_TEST_REDIS_TIMEOUT_SECONDS),
    }


@pytest.fixture
def unavailable_security_settings_overrides(
    security_settings_overrides: dict[str, str],
) -> dict[str, str]:
    """Return security overrides with a deliberately unreachable Redis backend."""

    return {
        **security_settings_overrides,
        "REDIS_URL": SECURITY_TEST_UNAVAILABLE_REDIS_URL,
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

    await reset_test_runtime_state(flush_redis=True)
    yield create_app()
    await reset_test_runtime_state(flush_redis=True)


@pytest_asyncio.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPS client for auth-route integration tests with truthful secure cookies."""

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def security_app(
    migrated_db_session: AsyncSession,
    security_settings_overrides: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build an app wired to migrated PostgreSQL plus a real Redis backend for security tests."""

    _ = migrated_db_session
    for key, value in security_settings_overrides.items():
        monkeypatch.setenv(key, value)

    await reset_test_runtime_state(flush_redis=True)
    yield create_app()
    await reset_test_runtime_state(flush_redis=True)


@pytest_asyncio.fixture
async def security_client(security_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPS client for security tests with truthful SameSite=Lax cookies."""

    transport = ASGITransport(app=security_app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as async_client:
        yield async_client


@pytest.fixture
def browser_security_settings_overrides(
    auth_settings_overrides: dict[str, str],
    redis_container_url: str,
) -> dict[str, str]:
    """Return dedicated browser-security overrides with explicit origins and SameSite=Lax cookies."""

    return {
        **auth_settings_overrides,
        "REDIS_URL": redis_container_url,
        "SECURITY_CORS_ALLOWED_ORIGINS": ",".join(BROWSER_SECURITY_ALLOWED_ORIGINS),
        "SECURITY_CORS_ALLOWED_ORIGIN_REGEX": BROWSER_SECURITY_ALLOWED_ORIGIN_REGEX,
        "SECURITY_CORS_ALLOWED_METHODS": BROWSER_SECURITY_ALLOWED_METHODS,
        "SECURITY_CORS_ALLOWED_HEADERS": BROWSER_SECURITY_ALLOWED_HEADERS,
        "SECURITY_RATE_LIMIT_REDIS_TIMEOUT_SECONDS": str(SECURITY_TEST_REDIS_TIMEOUT_SECONDS),
        "SECURITY_RATE_LIMIT_AUTH_WRITE_MAX_REQUESTS": "10",
        "SECURITY_RATE_LIMIT_AUTH_WRITE_WINDOW_SECONDS": "60",
    }


@pytest_asyncio.fixture
async def browser_security_app(
    migrated_db_session: AsyncSession,
    browser_security_settings_overrides: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build an app for browser-targeted security tests with real Redis and explicit CORS config."""

    _ = migrated_db_session
    for key, value in browser_security_settings_overrides.items():
        monkeypatch.setenv(key, value)

    await reset_test_runtime_state(flush_redis=True)
    yield create_app()
    await reset_test_runtime_state(flush_redis=True)


@pytest_asyncio.fixture
async def browser_security_client(browser_security_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPS client for browser-targeted CORS/CSRF assertions with SameSite=Lax cookies."""

    transport = ASGITransport(app=browser_security_app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def unavailable_security_app(
    migrated_db_session: AsyncSession,
    unavailable_security_settings_overrides: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build an app with the same auth contract but a deliberately unavailable Redis backend."""

    _ = migrated_db_session
    for key, value in unavailable_security_settings_overrides.items():
        monkeypatch.setenv(key, value)

    await reset_test_runtime_state()
    yield create_app()
    await reset_test_runtime_state()


@pytest_asyncio.fixture
async def unavailable_security_client(unavailable_security_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPS client for degraded-mode security tests."""

    transport = ASGITransport(app=unavailable_security_app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL) as async_client:
        yield async_client


def _build_redis_url(container: DockerContainer) -> str:
    """Construct a redis:// URL from a started Redis testcontainer."""

    host = container.get_container_host_ip()
    port = container.get_exposed_port(TEST_REDIS_PORT)
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def redis_container_url() -> Iterator[str]:
    """Yield a Redis connection URL for future integration tests."""

    with DockerContainer(TEST_REDIS_IMAGE).with_exposed_ports(TEST_REDIS_PORT).waiting_for(
        RedisPingWaitStrategy(),
    ) as redis_container:
        yield _build_redis_url(redis_container)
