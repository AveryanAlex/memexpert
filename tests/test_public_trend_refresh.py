"""Tests for public trend materialized-view refresh helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.exc import DBAPIError

from memexpert.services.public_trends import TREND_MATERIALIZED_VIEWS, refresh_public_trend_materialized_views

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class FakeRefreshConnection:
    def __init__(self, *, failures: dict[str, BaseException] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[str] = []
        self.rollback_calls = 0

    async def execution_options(self, **kwargs: object) -> FakeRefreshConnection:
        assert kwargs == {"isolation_level": "AUTOCOMMIT"}
        return self

    async def execute(self, statement: object) -> None:
        sql = getattr(statement, "text", str(statement))
        self.calls.append(sql)
        if failure := self.failures.get(sql):
            raise failure

    async def rollback(self) -> None:
        self.rollback_calls += 1


class FakeEngine:
    def __init__(self, connection: FakeRefreshConnection) -> None:
        self.connection = connection

    def connect(self) -> object:
        return FakeConnectionContext(self.connection)


class FakeConnectionContext:
    def __init__(self, connection: FakeRefreshConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeRefreshConnection:
        return self.connection

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


async def test_public_trend_refresh_uses_dependency_order_with_concurrent_refreshes() -> None:
    connection = FakeRefreshConnection()
    engine = FakeEngine(connection)

    await refresh_public_trend_materialized_views(cast("AsyncEngine", engine), concurrently=True)

    assert connection.calls == [
        f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}" for view_name in TREND_MATERIALIZED_VIEWS
    ]
    assert connection.rollback_calls == 0


async def test_public_trend_refresh_logs_and_falls_back_when_concurrent_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view_name = TREND_MATERIALIZED_VIEWS[0]
    concurrent_sql = f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}"
    connection = FakeRefreshConnection(failures={concurrent_sql: _dbapi_error()})
    engine = FakeEngine(connection)
    warning_calls: list[dict[str, object]] = []

    def fake_warning(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        exc_info: object = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        warning_calls.append({"message": message, "extra": extra, "exc_info": exc_info})

    monkeypatch.setattr("memexpert.services.public_trends.logger.warning", fake_warning)

    await refresh_public_trend_materialized_views(cast("AsyncEngine", engine), concurrently=True)

    assert connection.calls == [
        f"REFRESH MATERIALIZED VIEW CONCURRENTLY {TREND_MATERIALIZED_VIEWS[0]}",
        f"REFRESH MATERIALIZED VIEW {TREND_MATERIALIZED_VIEWS[0]}",
        *[
            f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}"
            for view_name in TREND_MATERIALIZED_VIEWS[1:]
        ],
    ]
    assert connection.rollback_calls == 1
    assert warning_calls == [
        {
            "message": "public_trend_mv_concurrent_refresh_fallback",
            "extra": {"event": "public_trend_mv_concurrent_refresh_fallback", "view_name": view_name},
            "exc_info": True,
        }
    ]


async def test_public_trend_refresh_propagates_non_concurrent_failure() -> None:
    view_name = TREND_MATERIALIZED_VIEWS[0]
    connection = FakeRefreshConnection(
        failures={
            f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}": _dbapi_error(),
            f"REFRESH MATERIALIZED VIEW {view_name}": _dbapi_error(),
        }
    )
    engine = FakeEngine(connection)

    with pytest.raises(DBAPIError):
        await refresh_public_trend_materialized_views(cast("AsyncEngine", engine), concurrently=True)

    assert connection.calls == [
        f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view_name}",
        f"REFRESH MATERIALIZED VIEW {view_name}",
    ]
    assert connection.rollback_calls == 1


def _dbapi_error() -> DBAPIError:
    return DBAPIError("REFRESH MATERIALIZED VIEW", {}, RuntimeError("missing unique index"))
