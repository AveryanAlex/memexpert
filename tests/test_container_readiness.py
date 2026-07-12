"""Focused no-Docker tests for shared testcontainer readiness helpers."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, cast

import asyncpg.exceptions as asyncpg_exceptions
import pytest
from redis.exceptions import AuthenticationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import DBAPIError

import tests.conftest as test_conftest
from memexpert.core.database import DatabaseConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine
    from testcontainers.core.waiting_utils import WaitStrategyTarget  # pyright: ignore[reportMissingTypeStubs]


def _wrapped_database_connection_error(cause: BaseException) -> DatabaseConnectionError:
    try:
        raise DatabaseConnectionError("database connection failed") from cause
    except DatabaseConnectionError as error:
        return error


def _wrapped_dbapi_connection_error(original: Exception) -> DatabaseConnectionError:
    dbapi_error = DBAPIError.instance(
        statement=None,
        params=None,
        orig=original,
        dbapi_base_err=Exception,
    )
    return _wrapped_database_connection_error(dbapi_error)


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(asyncpg_exceptions.ClientCannotConnectError("connection refused"), id="sqlstate-08"),
        pytest.param(asyncpg_exceptions.CannotConnectNowError("database is starting"), id="cannot-connect-now"),
        pytest.param(asyncpg_exceptions.TooManyConnectionsError("too many connections"), id="too-many-connections"),
    ],
)
def test_postgres_readiness_recognizes_transient_dbapi_startup_errors(
    original: Exception,
) -> None:
    error = _wrapped_dbapi_connection_error(original)

    assert isinstance(error.__cause__, DBAPIError)
    assert error.__cause__.orig is original
    assert test_conftest._is_retryable_postgres_startup_error(error)


@pytest.mark.parametrize(
    "cause",
    [
        pytest.param(ConnectionRefusedError("connection refused"), id="network"),
        pytest.param(TimeoutError("connection timed out"), id="timeout"),
    ],
)
def test_postgres_readiness_recognizes_transient_direct_causes(cause: BaseException) -> None:
    assert test_conftest._is_retryable_postgres_startup_error(_wrapped_database_connection_error(cause))


async def test_postgres_readiness_retries_transient_dbapi_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[float] = []

    async def _verify(_engine: AsyncEngine, *, timeout: float) -> None:
        attempts.append(timeout)
        if len(attempts) < 3:
            raise _wrapped_dbapi_connection_error(
                asyncpg_exceptions.ClientCannotConnectError("connection refused while PostgreSQL starts"),
            )

    monkeypatch.setattr(test_conftest, "verify_async_engine", _verify)

    await test_conftest._wait_for_postgres_ready(
        cast("AsyncEngine", object()),
        total_timeout=0.1,
        retry_interval=0.0,
    )

    assert len(attempts) == 3
    assert all(0 < timeout <= 0.1 for timeout in attempts)


async def test_postgres_readiness_reports_the_last_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def _verify(_engine: AsyncEngine, *, timeout: float) -> None:
        nonlocal attempts
        _ = timeout
        attempts += 1
        raise _wrapped_dbapi_connection_error(
            asyncpg_exceptions.ClientCannotConnectError("connection refused"),
        )

    monkeypatch.setattr(test_conftest, "verify_async_engine", _verify)

    with pytest.raises(
        DatabaseConnectionError,
        match=r"PostgreSQL testcontainer.*Last error: database connection failed",
    ):
        await test_conftest._wait_for_postgres_ready(
            cast("AsyncEngine", object()),
            total_timeout=0.02,
            retry_interval=0.001,
        )

    assert attempts > 0


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(asyncpg_exceptions.InvalidPasswordError("invalid password"), id="authentication"),
        pytest.param(asyncpg_exceptions.InvalidCatalogNameError("missing database"), id="invalid-catalog"),
        pytest.param(asyncpg_exceptions.ClientConfigurationError("invalid client configuration"), id="configuration"),
        pytest.param(ssl.SSLError("TLS certificate verification failed"), id="tls"),
        pytest.param(PermissionError("permission denied"), id="unknown-os-error"),
        pytest.param(RuntimeError("unknown permanent failure"), id="unknown"),
    ],
)
async def test_postgres_readiness_does_not_retry_nontransient_dbapi_errors(
    monkeypatch: pytest.MonkeyPatch,
    original: Exception,
) -> None:
    attempts = 0

    async def _verify(_engine: AsyncEngine, *, timeout: float) -> None:
        nonlocal attempts
        _ = timeout
        attempts += 1
        raise _wrapped_dbapi_connection_error(original)

    monkeypatch.setattr(test_conftest, "verify_async_engine", _verify)

    with pytest.raises(DatabaseConnectionError, match="database connection failed") as exc_info:
        await test_conftest._wait_for_postgres_ready(
            cast("AsyncEngine", object()),
            total_timeout=0.1,
            retry_interval=0.0,
        )

    assert attempts == 1
    assert isinstance(exc_info.value.__cause__, DBAPIError)
    assert exc_info.value.__cause__.orig is original


class _FakeWaitTarget:
    def get_container_host_ip(self) -> str:
        return "127.0.0.1"

    def get_exposed_port(self, port: int) -> int:
        assert port == test_conftest.TEST_REDIS_PORT
        return port


def _fake_wait_target() -> WaitStrategyTarget:
    return cast("WaitStrategyTarget", cast("object", _FakeWaitTarget()))


def test_redis_readiness_retries_transient_ping_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ping_attempts = 0
    close_calls = 0

    class _EventuallyReadyRedis:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            nonlocal ping_attempts
            ping_attempts += 1
            if ping_attempts == 1:
                raise RedisConnectionError("connection refused while Redis starts")
            return True

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    monkeypatch.setattr(test_conftest, "Redis", _EventuallyReadyRedis)
    strategy = test_conftest.RedisPingWaitStrategy(startup_timeout=1, poll_interval=0.0)

    strategy.wait_until_ready(_fake_wait_target())

    assert ping_attempts == 2
    assert close_calls == 2


def test_redis_readiness_reports_the_last_ping_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnavailableRedis:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            raise RedisConnectionError("connection refused")

        def close(self) -> None:
            pass

    def _poll_once(check: Callable[[], bool]) -> bool:
        assert check() is False
        return False

    monkeypatch.setattr(test_conftest, "Redis", _UnavailableRedis)
    strategy = test_conftest.RedisPingWaitStrategy()
    monkeypatch.setattr(strategy, "_poll", _poll_once)

    with pytest.raises(TimeoutError, match=r"Last error: connection refused"):
        strategy.wait_until_ready(_fake_wait_target())


def test_redis_readiness_does_not_retry_authentication_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ping_attempts = 0

    class _AuthenticationFailedRedis:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ping(self) -> bool:
            nonlocal ping_attempts
            ping_attempts += 1
            raise AuthenticationError("invalid Redis password")

        def close(self) -> None:
            pass

    monkeypatch.setattr(test_conftest, "Redis", _AuthenticationFailedRedis)
    strategy = test_conftest.RedisPingWaitStrategy(startup_timeout=1, poll_interval=0.0)

    with pytest.raises(RuntimeError, match="exception while checking") as exc_info:
        strategy.wait_until_ready(_fake_wait_target())

    assert isinstance(exc_info.value.__cause__, AuthenticationError)
    assert ping_attempts == 1
