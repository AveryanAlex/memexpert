"""Shared pytest fixtures for API and PostgreSQL integration tests."""

from __future__ import annotations

import asyncio
import errno
import socket
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from redis import Redis
from redis.exceptions import (
    AuthenticationError,
    AuthenticationWrongNumberOfArgsError,
    AuthorizationError,
    BusyLoadingError,
    ExternalAuthProviderError,
    NoPermissionError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
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
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    from memexpert.schemas.user import UserRead
    from memexpert.services.user_service import UserService

TEST_POSTGRES_IMAGE: Final = "postgres:16"
TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS: Final = 10.0
TEST_POSTGRES_STARTUP_TIMEOUT_SECONDS: Final = 60.0
TEST_POSTGRES_RETRY_INTERVAL_SECONDS: Final = 0.5
TRANSIENT_POSTGRES_STARTUP_SQLSTATES: Final = frozenset({"53300", "57P03"})
TRANSIENT_POSTGRES_NETWORK_ERRNOS: Final = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.ETIMEDOUT,
    },
)
TEST_REDIS_IMAGE: Final = "redis:7"
TEST_REDIS_PORT: Final = 6379
TEST_REDIS_CONNECT_TIMEOUT_SECONDS: Final = 1.0
TEST_REDIS_STARTUP_TIMEOUT_SECONDS: Final = 60
TEST_REDIS_POLL_INTERVAL_SECONDS: Final = 0.5
ALEMBIC_COMMAND_TIMEOUT_SECONDS: Final = 60.0
PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
ALEMBIC_INI_PATH: Final = PROJECT_ROOT / "alembic.ini"
PUBLIC_TREND_MATERIALIZED_VIEW_REFRESH_ORDER: Final = (
    "public_meme_trends_mv",
    "public_tag_trends_mv",
    "public_template_trends_mv",
    "public_tag_trend_points_mv",
    "public_template_trend_points_mv",
)
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
AUTH_TEST_TELEGRAM_LINK_RETURN_URL: Final = "https://memexpert.test/account/telegram/complete"
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

    def __init__(
        self,
        *,
        port: int = TEST_REDIS_PORT,
        startup_timeout: int = TEST_REDIS_STARTUP_TIMEOUT_SECONDS,
        poll_interval: float = TEST_REDIS_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__()
        self._port = port
        self.with_startup_timeout(startup_timeout)
        self.with_poll_interval(poll_interval)
        self.with_transient_exceptions(OSError)

    def wait_until_ready(self, container: WaitStrategyTarget) -> None:
        """Block until the mapped Redis port accepts PING."""

        last_error: Exception | str | None = None

        def can_ping() -> bool:
            nonlocal last_error
            client = Redis(
                host=container.get_container_host_ip(),
                port=container.get_exposed_port(self._port),
                socket_connect_timeout=TEST_REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=TEST_REDIS_CONNECT_TIMEOUT_SECONDS,
            )
            try:
                if client.ping():
                    return True
                last_error = "PING returned false."
                return False
            except (
                AuthenticationError,
                AuthenticationWrongNumberOfArgsError,
                AuthorizationError,
                ExternalAuthProviderError,
                NoPermissionError,
            ):
                # Credentials and authorization configuration cannot resolve
                # through startup retries, so preserve the immediate failure.
                raise
            except (BusyLoadingError, RedisConnectionError, RedisTimeoutError, OSError) as exc:
                last_error = exc
                return False
            finally:
                client.close()

        if not self._poll(can_ping):
            diagnostic = "no Redis PING response" if last_error is None else str(last_error)
            raise TimeoutError(
                "Redis testcontainer did not become ready "
                f"after {self._startup_timeout:.1f}s. Last error: {diagnostic}",
            )


@dataclass
class MigratedDatabaseSchema:
    """Mutable schema object cache for the worker-local migrated test database."""

    head_revision: str
    table_names: tuple[str, ...]
    materialized_view_names: tuple[str, ...]


def _build_alembic_config(database_url: str) -> Config:
    """Construct an Alembic config bound to the ephemeral PostgreSQL URL."""

    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def _get_alembic_head_revision(database_url: str) -> str:
    """Return the repository's current Alembic head revision."""

    script_directory = ScriptDirectory.from_config(_build_alembic_config(database_url))
    head_revision = script_directory.get_current_head()
    if head_revision is None:  # pragma: no cover - Alembic raises first for multi-head trees.
        raise AssertionError("Alembic script directory does not define a head revision.")
    return head_revision


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


async def _wait_for_postgres_ready(
    engine: AsyncEngine,
    *,
    total_timeout: float = TEST_POSTGRES_STARTUP_TIMEOUT_SECONDS,
    retry_interval: float = TEST_POSTGRES_RETRY_INTERVAL_SECONDS,
) -> None:
    """Retry transient PostgreSQL connection failures until a fixed deadline."""

    deadline = asyncio.get_running_loop().time() + total_timeout
    attempts = 0
    last_error: DatabaseConnectionError | None = None

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break

        attempts += 1
        try:
            await verify_async_engine(
                engine,
                timeout=min(TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS, remaining),
            )
        except DatabaseConnectionError as exc:
            last_error = exc
            if not _is_retryable_postgres_startup_error(exc):
                raise
        else:
            return

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(retry_interval, remaining))

    diagnostic = "no connection attempt completed" if last_error is None else str(last_error)
    raise DatabaseConnectionError(
        "PostgreSQL testcontainer did not become ready "
        f"after {total_timeout:.1f}s across {attempts} attempts. Last error: {diagnostic}",
    ) from last_error


def _is_retryable_postgres_startup_error(error: DatabaseConnectionError) -> bool:
    """Return whether a wrapped PostgreSQL failure can resolve during container startup."""

    nested_errors = tuple(_iter_nested_exceptions(error))
    sqlstates = tuple(
        sqlstate
        for nested_error in nested_errors
        if (sqlstate := _postgres_sqlstate(nested_error)) is not None
    )

    if any(sqlstate.startswith(("28", "3D")) for sqlstate in sqlstates):
        return False
    if any(
        isinstance(nested_error, (DatabaseConfigurationError, ValueError, TypeError, ssl.SSLError))
        for nested_error in nested_errors
    ):
        return False

    if any(sqlstate.startswith("08") or sqlstate in TRANSIENT_POSTGRES_STARTUP_SQLSTATES for sqlstate in sqlstates):
        return True
    if any(getattr(nested_error, "connection_invalidated", False) for nested_error in nested_errors):
        return True
    if any(isinstance(nested_error, TimeoutError) for nested_error in nested_errors):
        return True
    return any(_is_transient_postgres_network_error(nested_error) for nested_error in nested_errors)


def _is_transient_postgres_network_error(error: BaseException) -> bool:
    """Return whether a direct OS error is a recognized retryable network failure."""

    if isinstance(error, ConnectionError):
        return True
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, OSError) and error.errno in TRANSIENT_POSTGRES_NETWORK_ERRNOS


def _iter_nested_exceptions(error: BaseException) -> Iterator[BaseException]:
    """Yield an exception's cause/context chain plus SQLAlchemy DBAPI ``orig`` errors."""

    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for attribute in ("__cause__", "__context__", "orig"):
            nested_error = getattr(current, attribute, None)
            if isinstance(nested_error, BaseException):
                pending.append(nested_error)


def _postgres_sqlstate(error: BaseException) -> str | None:
    """Extract a PostgreSQL SQLSTATE from asyncpg or DBAPI-compatible exceptions."""

    for attribute in ("sqlstate", "pgcode"):
        sqlstate = getattr(error, attribute, None)
        if isinstance(sqlstate, str) and len(sqlstate) == 5:
            return sqlstate.upper()
    return None


async def _reset_public_schema(engine: AsyncEngine) -> None:
    """Drop and recreate the public schema for migration-backed integration tests."""

    async with engine.begin() as connection:
        _ = await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        _ = await connection.execute(text("CREATE SCHEMA public"))


async def _get_current_alembic_revision(engine: AsyncEngine) -> str | None:
    """Return the public schema's Alembic revision, or None when no migrated schema exists."""

    async with engine.connect() as connection:
        version_table = await connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if version_table is None:
            return None
        revision = await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
        return None if revision is None else str(revision)


async def _list_truncatable_public_tables(engine: AsyncEngine) -> tuple[str, ...]:
    """Return quoted regular public tables whose rows should be cleared between tests."""

    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT quote_ident(tablename)
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename <> 'alembic_version'
                ORDER BY tablename
                """
            )
        )
        return tuple(str(table_name) for table_name in result.scalars())


async def _list_public_materialized_views(engine: AsyncEngine) -> tuple[str, ...]:
    """Return quoted public materialized views in dependency-safe refresh order."""

    ordered_view_cases = "\n".join(
        f"WHEN {view_name!r} THEN {position}"
        for position, view_name in enumerate(PUBLIC_TREND_MATERIALIZED_VIEW_REFRESH_ORDER, start=1)
    )
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                f"""
                SELECT quote_ident(matviewname)
                FROM pg_matviews
                WHERE schemaname = 'public'
                ORDER BY CASE matviewname
                    {ordered_view_cases}
                    ELSE 1000
                END, matviewname
                """
            )
        )
        return tuple(str(view_name) for view_name in result.scalars())


async def _truncate_migrated_database(engine: AsyncEngine, schema: MigratedDatabaseSchema) -> None:
    """Clear mutable migrated database state without dropping migrated schema objects."""

    async with engine.begin() as connection:
        if schema.table_names:
            _ = await connection.execute(
                text(f"TRUNCATE TABLE {', '.join(schema.table_names)} RESTART IDENTITY CASCADE"),
            )

        should_refresh_views = False
        for view_name in schema.materialized_view_names:
            view_has_rows = await connection.scalar(text(f"SELECT EXISTS (SELECT 1 FROM {view_name} LIMIT 1)"))
            should_refresh_views = should_refresh_views or bool(view_has_rows)

        if should_refresh_views:
            for view_name in schema.materialized_view_names:
                _ = await connection.execute(text(f"REFRESH MATERIALIZED VIEW {view_name}"))


async def _reset_and_migrate_public_schema(engine: AsyncEngine, database_url: str) -> MigratedDatabaseSchema:
    """Reset the public schema, apply Alembic once, and cache mutable object names."""

    head_revision = _get_alembic_head_revision(database_url)
    await _reset_public_schema(engine)
    await _run_alembic_upgrade(database_url)
    return MigratedDatabaseSchema(
        head_revision=head_revision,
        table_names=await _list_truncatable_public_tables(engine),
        materialized_view_names=await _list_public_materialized_views(engine),
    )


async def _ensure_migrated_database_schema(
    engine: AsyncEngine,
    database_url: str,
    schema: MigratedDatabaseSchema,
) -> None:
    """Rebuild the worker schema if another isolated migration test reset it."""

    if await _get_current_alembic_revision(engine) == schema.head_revision:
        return

    rebuilt_schema = await _reset_and_migrate_public_schema(engine, database_url)
    schema.head_revision = rebuilt_schema.head_revision
    schema.table_names = rebuilt_schema.table_names
    schema.materialized_view_names = rebuilt_schema.materialized_view_names


@asynccontextmanager
async def _transactional_migrated_session(
    engine: AsyncEngine,
    database_url: str,
    schema: MigratedDatabaseSchema,
) -> AsyncIterator[AsyncSession]:
    """Open a migrated DB session wrapped in an outer rollback-only transaction."""

    await _ensure_migrated_database_schema(engine, database_url, schema)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            autoflush=False,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            try:
                yield session
            finally:
                await session.close()

        if transaction.is_active:
            await transaction.rollback()


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
        await _wait_for_postgres_ready(engine)
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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated_db_schema(
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
) -> AsyncIterator[MigratedDatabaseSchema]:
    """Apply Alembic once per pytest worker and preserve migrated schema objects."""

    schema = await _reset_and_migrate_public_schema(postgres_async_engine, postgres_async_url)
    await _truncate_migrated_database(postgres_async_engine, schema)

    try:
        yield schema
    finally:
        await _reset_public_schema(postgres_async_engine)


@pytest_asyncio.fixture(loop_scope="session")
async def migrated_db_session(
    request: pytest.FixtureRequest,
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    migrated_db_schema: MigratedDatabaseSchema,
) -> AsyncIterator[AsyncSession]:
    """Yield a session bound to the worker-local migrated PostgreSQL schema."""

    if request.node.get_closest_marker("transactional_db") is not None:
        async with _transactional_migrated_session(
            postgres_async_engine,
            postgres_async_url,
            migrated_db_schema,
        ) as session:
            yield session
        return

    await _ensure_migrated_database_schema(
        postgres_async_engine,
        postgres_async_url,
        migrated_db_schema,
    )

    async with postgres_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()

    await _truncate_migrated_database(postgres_async_engine, migrated_db_schema)


@pytest_asyncio.fixture(loop_scope="session")
async def transactional_migrated_db_session(
    postgres_async_engine: AsyncEngine,
    postgres_async_url: str,
    migrated_db_schema: MigratedDatabaseSchema,
) -> AsyncIterator[AsyncSession]:
    """Yield a migrated DB session whose writes roll back with one outer transaction."""

    async with _transactional_migrated_session(
        postgres_async_engine,
        postgres_async_url,
        migrated_db_schema,
    ) as session:
        yield session


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
