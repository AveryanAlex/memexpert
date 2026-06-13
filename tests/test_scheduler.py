"""Tests for scheduler job definitions and wrappers."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, cast

import pytest

from memexpert.scheduler.logging import configure_scheduler_logging

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

from memexpert.core.config import Settings
from memexpert.scheduler.jobs import (
    JOB_ID_MATERIALIZED_VIEW_REFRESH,
    JOB_ID_MOTD,
    JOB_ID_POPULARITY_SNAPSHOTS,
    JOB_ID_SEARCH_INDEX_SYNC,
    JOB_ID_SEO_BACKLOG_BATCHES,
    build_scheduler_job_definitions,
    enabled_scheduler_jobs,
    run_logged_job,
)
from memexpert.scheduler.locking import (
    PostgresAdvisorySchedulerLock,
    SchedulerInstanceLockError,
)
from memexpert.scheduler.runtime import run_scheduler_runtime


class FakeResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar(self) -> bool:
        return self.value

    def scalar_one(self) -> bool:
        return self.value


class FakeAsyncConnection:
    def __init__(self, results: list[bool]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, int]]] = []
        self.close_calls = 0

    async def execute(self, statement: object, params: dict[str, int]) -> FakeResult:
        sql = getattr(statement, "text", str(statement))
        self.calls.append((sql, params))
        return FakeResult(self.results.pop(0))

    async def close(self) -> None:
        self.close_calls += 1


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []
        self.started = False
        self.shutdown_waits: list[bool] = []

    def add_job(self, func: object, **kwargs: object) -> None:
        record = dict(kwargs)
        record["func"] = func
        self.jobs.append(record)

    def start(self) -> None:
        self.started = True

    def shutdown(self, *, wait: bool) -> None:
        self.shutdown_waits.append(wait)


class FakeLock:
    def __init__(self) -> None:
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> None:
        self.acquire_calls += 1

    async def release(self) -> None:
        self.release_calls += 1


class FakeSessionContext:
    def __init__(self, session: object) -> None:
        self.session = session
        self.enter_calls = 0
        self.exit_calls = 0

    async def __aenter__(self) -> object:
        self.enter_calls += 1
        return self.session

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exit_calls += 1


class FailingLock:
    def __init__(self) -> None:
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> None:
        self.acquire_calls += 1
        raise SchedulerInstanceLockError("duplicate scheduler")

    async def release(self) -> None:
        self.release_calls += 1


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0
        self.connect_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1

    def connect(self) -> object:
        self.connect_calls += 1
        return _FakeConnectionContext()


class _FakeConnectionContext:
    async def __aenter__(self) -> FakeAsyncConnection:
        return FakeAsyncConnection([True, True])

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def test_configure_scheduler_logging_uses_stdout_when_bootstrapping(monkeypatch: pytest.MonkeyPatch) -> None:
    root_logger = logging.getLogger("memexpert-test-scheduler-root")
    original_get_logger = logging.getLogger

    def fake_get_logger(name: str | None = None) -> logging.Logger:
        return root_logger if name is None else original_get_logger(name)

    monkeypatch.setattr("logging.getLogger", fake_get_logger)

    root_logger.handlers.clear()
    root_logger.setLevel(logging.NOTSET)

    try:
        configure_scheduler_logging()
        assert len(root_logger.handlers) == 1
        handler = root_logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout
        assert root_logger.level == logging.INFO

        record = logging.LogRecord(
            name="memexpert.scheduler.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg="scheduler structured field test",
            args=(),
            exc_info=None,
        )
        record.event = "popularity_snapshot_capture_succeeded"
        record.captured_at = "2026-03-01T12:00:00+00:00"
        record.public_meme_count = 7
        record.snapshot_count = 7
        record.updated_meme_count = 7
        record.view_name = "public_meme_trends_mv"

        payload = json.loads(handler.format(record))
        assert payload["event"] == "popularity_snapshot_capture_succeeded"
        assert payload["captured_at"] == "2026-03-01T12:00:00+00:00"
        assert payload["public_meme_count"] == 7
        assert payload["snapshot_count"] == 7
        assert payload["updated_meme_count"] == 7
        assert payload["view_name"] == "public_meme_trends_mv"
    finally:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.NOTSET)


def test_scheduler_job_definitions_register_expected_ids() -> None:
    settings = Settings()
    definitions = build_scheduler_job_definitions(settings, engine=cast("AsyncEngine", object()))

    assert [definition.id for definition in definitions] == [
        JOB_ID_MATERIALIZED_VIEW_REFRESH,
        JOB_ID_POPULARITY_SNAPSHOTS,
        JOB_ID_MOTD,
        JOB_ID_SEARCH_INDEX_SYNC,
        JOB_ID_SEO_BACKLOG_BATCHES,
    ]


def test_enabled_scheduler_jobs_filters_disabled_jobs() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": False,
            "scheduler_popularity_snapshots_enabled": True,
            "scheduler_motd_enabled": False,
            "scheduler_search_index_sync_enabled": True,
            "scheduler_seo_backlog_batches_enabled": False,
        }
    )

    enabled_job_ids = [
        definition.id
        for definition in enabled_scheduler_jobs(settings, engine=cast("AsyncEngine", object()))
    ]

    assert enabled_job_ids == [JOB_ID_POPULARITY_SNAPSHOTS, JOB_ID_SEARCH_INDEX_SYNC]


@pytest.mark.asyncio
async def test_materialized_view_job_calls_refresh_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate({"scheduler_materialized_view_refresh_enabled": True})
    called: dict[str, object] = {}

    async def fake_refresh(engine: object, *, concurrently: bool = True) -> None:
        called["engine"] = engine
        called["concurrently"] = concurrently

    monkeypatch.setattr("memexpert.scheduler.jobs.refresh_public_trend_materialized_views", fake_refresh)

    sentinel = cast("AsyncEngine", object())
    definition = build_scheduler_job_definitions(settings, engine=sentinel)[0]
    await definition.action()

    assert called == {"engine": sentinel, "concurrently": True}


@pytest.mark.asyncio
async def test_popularity_snapshots_job_uses_scheduler_engine_session_and_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"scheduler_popularity_snapshots_enabled": True})
    engine = cast("AsyncEngine", object())
    session = object()
    session_context = FakeSessionContext(session)
    called: dict[str, object] = {}

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return lambda: session_context

    async def fake_capture(session_arg: object, *, settings: Settings) -> object:
        called["session"] = session_arg
        called["settings"] = settings
        return object()

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.capture_popularity_snapshots", fake_capture)

    definition = build_scheduler_job_definitions(settings, engine=engine)[1]
    await definition.action()

    assert definition.id == JOB_ID_POPULARITY_SNAPSHOTS
    assert called == {"engine": engine, "session": session, "settings": settings}
    assert session_context.enter_calls == 1
    assert session_context.exit_calls == 1


@pytest.mark.asyncio
async def test_run_logged_job_catches_and_logs_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    exception_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_exception(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        exception_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.logger.exception", fake_exception)

    async def failing_action() -> None:
        raise RuntimeError("boom")

    await run_logged_job("test-job", failing_action)

    assert len(exception_calls) == 1
    message, extra = exception_calls[0]
    assert message == "scheduler_job_failed"
    assert extra is not None
    assert extra.get("event") == "scheduler_job_failed"
    assert extra.get("job_id") == "test-job"
    duration_seconds = extra.get("duration_seconds")
    assert isinstance(duration_seconds, float)
    assert duration_seconds >= 0


@pytest.mark.asyncio
async def test_postgres_advisory_scheduler_lock_acquire_success_and_release_once() -> None:
    connection = FakeAsyncConnection([True, True])
    lock = PostgresAdvisorySchedulerLock(connection, (11, 22))

    await lock.acquire()
    await lock.release()
    await lock.release()

    assert connection.calls == [
        ("SELECT pg_try_advisory_lock(:key1, :key2)", {"key1": 11, "key2": 22}),
        ("SELECT pg_advisory_unlock(:key1, :key2)", {"key1": 11, "key2": 22}),
    ]


@pytest.mark.asyncio
async def test_postgres_advisory_scheduler_lock_acquire_failure_raises() -> None:
    connection = FakeAsyncConnection([False])
    lock = PostgresAdvisorySchedulerLock(connection, (33, 44))

    with pytest.raises(SchedulerInstanceLockError):
        await lock.acquire()

    await lock.release()

    assert connection.calls == [
        ("SELECT pg_try_advisory_lock(:key1, :key2)", {"key1": 33, "key2": 44}),
    ]


@pytest.mark.asyncio
async def test_postgres_advisory_scheduler_lock_release_is_idempotent_when_never_acquired() -> None:
    connection = FakeAsyncConnection([])
    lock = PostgresAdvisorySchedulerLock(connection, (55, 66))

    await lock.release()

    assert connection.calls == []


@pytest.mark.asyncio
async def test_scheduler_runtime_registers_enabled_jobs_and_shuts_down_gracefully() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": True,
            "scheduler_popularity_snapshots_enabled": False,
            "scheduler_motd_enabled": True,
            "scheduler_search_index_sync_enabled": False,
            "scheduler_seo_backlog_batches_enabled": True,
            "scheduler_advisory_lock_enabled": False,
        }
    )
    scheduler = FakeScheduler()
    engine = FakeEngine()
    stop_waiter_called = False

    async def stop_waiter() -> None:
        nonlocal stop_waiter_called
        stop_waiter_called = True

    await run_scheduler_runtime(
        settings=settings,
        engine=cast("AsyncEngine", engine),
        scheduler=scheduler,
        stop_waiter=stop_waiter,
    )

    assert stop_waiter_called is True
    assert scheduler.started is True
    assert scheduler.shutdown_waits == [True]
    assert [job["id"] for job in scheduler.jobs] == [
        JOB_ID_MATERIALIZED_VIEW_REFRESH,
        JOB_ID_MOTD,
        JOB_ID_SEO_BACKLOG_BATCHES,
    ]
    assert engine.dispose_calls == 0


@pytest.mark.asyncio
async def test_scheduler_runtime_skips_disabled_jobs() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": False,
            "scheduler_popularity_snapshots_enabled": False,
            "scheduler_motd_enabled": False,
            "scheduler_search_index_sync_enabled": False,
            "scheduler_seo_backlog_batches_enabled": False,
            "scheduler_advisory_lock_enabled": False,
        }
    )
    scheduler = FakeScheduler()

    await run_scheduler_runtime(
        settings=settings,
        engine=cast("AsyncEngine", FakeEngine()),
        scheduler=scheduler,
        stop_waiter=lambda: None,
    )

    assert scheduler.jobs == []


@pytest.mark.asyncio
async def test_scheduler_runtime_releases_lock_and_disposes_owned_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate({"scheduler_advisory_lock_enabled": True})
    scheduler = FakeScheduler()
    engine = FakeEngine()
    lock = FakeLock()

    monkeypatch.setattr("memexpert.scheduler.runtime.build_async_engine", lambda: engine)

    await run_scheduler_runtime(
        settings=settings,
        scheduler=scheduler,
        stop_waiter=lambda: None,
        lock=lock,
    )

    assert lock.acquire_calls == 1
    assert lock.release_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_scheduler_runtime_opens_managed_connection_when_lock_seam_missing() -> None:
    settings = Settings.model_validate({"scheduler_advisory_lock_enabled": True})
    scheduler = FakeScheduler()
    connection = FakeAsyncConnection([True, True])

    class EngineWithConnection:
        def __init__(self, conn: FakeAsyncConnection) -> None:
            self.conn = conn
            self.dispose_calls = 0
            self.connect_calls = 0

        def connect(self) -> FakeAsyncConnection:
            self.connect_calls += 1
            return self.conn

        async def dispose(self) -> None:
            self.dispose_calls += 1

    engine = EngineWithConnection(connection)

    await run_scheduler_runtime(
        settings=settings,
        engine=cast("AsyncEngine", engine),
        scheduler=scheduler,
        stop_waiter=lambda: None,
    )

    assert engine.connect_calls == 1
    key1, key2 = settings.scheduler_advisory_lock_key
    assert connection.calls == [
        ("SELECT pg_try_advisory_lock(:key1, :key2)", {"key1": key1, "key2": key2}),
        ("SELECT pg_advisory_unlock(:key1, :key2)", {"key1": key1, "key2": key2}),
    ]
    assert connection.close_calls == 1
    assert engine.dispose_calls == 0


@pytest.mark.asyncio
async def test_scheduler_runtime_logs_lock_conflict_and_disposes_owned_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"scheduler_advisory_lock_enabled": True})
    scheduler = FakeScheduler()
    engine = FakeEngine()
    lock = FailingLock()
    error_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_error(
        message: str,
        *args: object,
        extra: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        error_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.runtime.build_async_engine", lambda: engine)
    monkeypatch.setattr("memexpert.scheduler.runtime.logger.error", fake_error)

    with pytest.raises(SchedulerInstanceLockError, match="duplicate scheduler"):
        await run_scheduler_runtime(
            settings=settings,
            scheduler=scheduler,
            stop_waiter=lambda: None,
            lock=lock,
        )

    assert len(error_calls) == 1
    message, extra = error_calls[0]
    assert message == "scheduler_instance_lock_unavailable"
    assert extra is not None
    assert extra.get("event") == "scheduler_instance_lock_unavailable"
    assert extra.get("advisory_lock_key") == settings.scheduler_advisory_lock_key
    assert scheduler.started is False
    assert scheduler.shutdown_waits == []
    assert lock.acquire_calls == 1
    assert lock.release_calls == 1
    assert engine.dispose_calls == 1
