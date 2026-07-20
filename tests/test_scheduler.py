"""Tests for scheduler job definitions and wrappers."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast

import pytest

from memexpert.scheduler.logging import configure_scheduler_logging

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

from memexpert.core.config import Settings
from memexpert.messaging.rabbitmq_outbox_runtime import RabbitMQOutboxPublisherBatchResult
from memexpert.models.enums import ContentKind, ContentLanguage, SearchSynonymSyncStatus
from memexpert.scheduler.jobs import (
    JOB_ID_MATERIALIZED_VIEW_REFRESH,
    JOB_ID_MEDIA_GENERATION_GC,
    JOB_ID_MEILISEARCH_SETTINGS_RECONCILE,
    JOB_ID_MOTD,
    JOB_ID_PIPELINE_CAPACITY_REFRESH,
    JOB_ID_RABBITMQ_OUTBOX_PUBLISHER,
    JOB_ID_RECOMMENDATION_ANALYTICS_ROLLUP,
    JOB_ID_RECOMMENDATION_PROFILE_REBUILD,
    JOB_ID_RECOVERY_DISPATCH,
    JOB_ID_SEARCH_INDEX_SYNC,
    JOB_ID_SEO_BACKLOG_BATCHES,
    JOB_ID_SOURCE_CHANNEL_AUDIENCE_CAPTURE,
    JOB_ID_SOURCE_ENGAGEMENT_CAPTURE,
    JOB_ID_TELEGRAM_LOGIN_CLEANUP,
    build_scheduler_job_definitions,
    enabled_scheduler_jobs,
    run_logged_job,
)
from memexpert.scheduler.locking import (
    PostgresAdvisorySchedulerLock,
    SchedulerInstanceLockError,
)
from memexpert.scheduler.runtime import run_scheduler_runtime
from memexpert.schemas.meme import PublicMemeCardRead, PublicMemeOfTheDayRead
from memexpert.services.admin_telegram_login import TelegramLoginCleanupBatchResult
from memexpert.services.meilisearch_settings_reconcile import MeilisearchSettingsReconcileResult
from memexpert.services.recommendations.analytics import RecommendationAnalyticsRollupResult
from memexpert.services.recommendations.profile_store import ProfileRebuildResult
from memexpert.services.scheduler_batch_jobs import SearchIndexBatchJobResult, SeoBacklogBatchJobResult
from memexpert.services.source_channel_audience_scheduler import SourceChannelAudienceCaptureSchedulerResult
from memexpert.services.source_engagement_scheduler import SourceEngagementCaptureSchedulerResult


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
        record.event_type = "search_query"
        record.payload_key_count = 2
        record.payload_keys = ["result_count", "safe_context"]
        record.language = "en"
        record.media_type = "image"
        record.expired = 2
        record.cleaned = 3
        record.attempt_id = "019f5c1a-5fd6-7000-8000-000000000001"
        record.changed = True
        record.desired_hash = "a" * 64
        record.actual_hash = "b" * 64
        record.provider_task_uid = 42
        record.revision_count = 2

        payload = json.loads(handler.format(record))
        assert payload["event"] == "popularity_snapshot_capture_succeeded"
        assert payload["event_type"] == "search_query"
        assert payload["captured_at"] == "2026-03-01T12:00:00+00:00"
        assert payload["public_meme_count"] == 7
        assert payload["snapshot_count"] == 7
        assert payload["updated_meme_count"] == 7
        assert payload["view_name"] == "public_meme_trends_mv"
        assert payload["payload_key_count"] == 2
        assert payload["payload_keys"] == ["result_count", "safe_context"]
        assert payload["language"] == "en"
        assert payload["media_type"] == "image"
        assert payload["expired"] == 2
        assert payload["cleaned"] == 3
        assert payload["attempt_id"] == "019f5c1a-5fd6-7000-8000-000000000001"
        assert payload["changed"] is True
        assert payload["desired_hash"] == "a" * 64
        assert payload["actual_hash"] == "b" * 64
        assert payload["provider_task_uid"] == 42
        assert payload["revision_count"] == 2
    finally:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.NOTSET)


def test_scheduler_job_definitions_register_expected_ids() -> None:
    settings = Settings()
    definitions = build_scheduler_job_definitions(settings, engine=cast("AsyncEngine", object()))

    assert [definition.id for definition in definitions] == [
        JOB_ID_MATERIALIZED_VIEW_REFRESH,
        JOB_ID_SOURCE_ENGAGEMENT_CAPTURE,
        JOB_ID_MOTD,
        JOB_ID_SEARCH_INDEX_SYNC,
        JOB_ID_MEILISEARCH_SETTINGS_RECONCILE,
        JOB_ID_SEO_BACKLOG_BATCHES,
        JOB_ID_RABBITMQ_OUTBOX_PUBLISHER,
        JOB_ID_RECOVERY_DISPATCH,
        JOB_ID_MEDIA_GENERATION_GC,
        JOB_ID_PIPELINE_CAPACITY_REFRESH,
        JOB_ID_TELEGRAM_LOGIN_CLEANUP,
        JOB_ID_RECOMMENDATION_PROFILE_REBUILD,
        JOB_ID_RECOMMENDATION_ANALYTICS_ROLLUP,
        JOB_ID_SOURCE_CHANNEL_AUDIENCE_CAPTURE,
    ]
    reconcile_definition = next(
        definition for definition in definitions if definition.id == JOB_ID_MEILISEARCH_SETTINGS_RECONCILE
    )
    assert reconcile_definition.enabled is True
    assert reconcile_definition.trigger_seconds == 60.0
    assert reconcile_definition.run_on_startup is True


def test_enabled_scheduler_jobs_filters_disabled_jobs() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": False,
            "scheduler_source_engagement_capture_enabled": True,
            "scheduler_source_channel_audience_capture_enabled": False,
            "scheduler_motd_enabled": False,
            "scheduler_search_index_sync_enabled": True,
            "scheduler_meilisearch_settings_reconcile_enabled": False,
            "scheduler_seo_backlog_batches_enabled": False,
            "scheduler_rabbitmq_outbox_publisher_enabled": False,
            "scheduler_recovery_dispatch_enabled": False,
            "scheduler_media_generation_gc_enabled": False,
            "scheduler_pipeline_capacity_refresh_enabled": False,
            "scheduler_telegram_login_cleanup_enabled": False,
            "scheduler_recommendation_profile_rebuild_enabled": False,
            "scheduler_recommendation_analytics_rollup_enabled": False,
        }
    )

    enabled_job_ids = [
        definition.id for definition in enabled_scheduler_jobs(settings, engine=cast("AsyncEngine", object()))
    ]

    assert enabled_job_ids == [JOB_ID_SOURCE_ENGAGEMENT_CAPTURE, JOB_ID_SEARCH_INDEX_SYNC]


@pytest.mark.asyncio
async def test_recommendation_profile_rebuild_job_calls_bounded_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"scheduler_recommendation_profile_rebuild_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_rebuild(session_factory_arg: object, *, settings: Settings) -> ProfileRebuildResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return ProfileRebuildResult(claimed_users=3, rebuilt_users=2, failed_users=1)

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.rebuild_dirty_recommendation_profiles", fake_rebuild)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = next(
        item
        for item in build_scheduler_job_definitions(settings, engine=engine)
        if item.id == JOB_ID_RECOMMENDATION_PROFILE_REBUILD
    )
    await definition.action()

    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_RECOMMENDATION_PROFILE_REBUILD,
                "status": "completed",
                "degraded_mode": True,
                "claimed_users": 3,
                "rebuilt_users": 2,
                "failed_users": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_recommendation_analytics_rollup_job_recomputes_bounded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "scheduler_recommendation_analytics_rollup_enabled": True,
            "scheduler_recommendation_analytics_rollup_lookback_days": 3,
        }
    )
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_rollup(
        session_factory_arg: object,
        *,
        lookback_days: int,
        impression_cooldown_hours: int,
        strong_positive_cooldown_hours: int,
    ) -> RecommendationAnalyticsRollupResult:
        called["session_factory"] = session_factory_arg
        called["lookback_days"] = lookback_days
        called["impression_cooldown_hours"] = impression_cooldown_hours
        called["strong_positive_cooldown_hours"] = strong_positive_cooldown_hours
        return RecommendationAnalyticsRollupResult(
            start_date=date(2026, 7, 18),
            end_date=date(2026, 7, 20),
            aggregate_rows=7,
        )

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.rollup_recommendation_daily_analytics", fake_rollup)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = next(
        item
        for item in build_scheduler_job_definitions(settings, engine=engine)
        if item.id == JOB_ID_RECOMMENDATION_ANALYTICS_ROLLUP
    )
    await definition.action()

    assert called == {
        "engine": engine,
        "session_factory": session_factory,
        "lookback_days": 3,
        "impression_cooldown_hours": 72,
        "strong_positive_cooldown_hours": 168,
    }
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_RECOMMENDATION_ANALYTICS_ROLLUP,
                "status": "completed",
                "degraded_mode": False,
                "start_date": "2026-07-18",
                "end_date": "2026-07-20",
                "aggregate_rows": 7,
            },
        )
    ]


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
async def test_source_engagement_capture_job_calls_batch_service_and_logs_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"scheduler_source_engagement_capture_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_run_batch(
        session_factory_arg: object,
        *,
        settings: Settings,
    ) -> SourceEngagementCaptureSchedulerResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return SourceEngagementCaptureSchedulerResult(
            claimed=2,
            enqueued=2,
            meme_source_ids=(uuid.UUID("00000000-0000-0000-0000-000000000001"),),
            outbox_message_ids=(uuid.UUID("00000000-0000-0000-0000-000000000002"),),
            duration_seconds=0.25,
        )

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_scheduler_source_engagement_capture_batch", fake_run_batch)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = build_scheduler_job_definitions(settings, engine=engine)[1]
    await definition.action()

    assert definition.id == JOB_ID_SOURCE_ENGAGEMENT_CAPTURE
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_SOURCE_ENGAGEMENT_CAPTURE,
                "status": "completed",
                "degraded_mode": False,
                "claimed": 2,
                "enqueued": 2,
                "meme_source_ids": ["00000000-0000-0000-0000-000000000001"],
                "outbox_message_ids": ["00000000-0000-0000-0000-000000000002"],
                "duration_seconds": 0.25,
            },
        )
    ]


@pytest.mark.asyncio
async def test_source_channel_audience_capture_job_calls_batch_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"scheduler_source_channel_audience_capture_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    monkeypatch.setattr(
        "memexpert.scheduler.jobs.build_async_session_factory",
        fake_build_session_factory,
    )

    async def fake_run_batch(
        session_factory_arg: object,
        *,
        settings: Settings,
    ) -> SourceChannelAudienceCaptureSchedulerResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return SourceChannelAudienceCaptureSchedulerResult(
            claimed=1,
            enqueued=1,
            source_channel_ids=(uuid.UUID("00000000-0000-0000-0000-000000000001"),),
            outbox_message_ids=(uuid.UUID("00000000-0000-0000-0000-000000000002"),),
            duration_seconds=0.1,
        )

    monkeypatch.setattr(
        "memexpert.scheduler.jobs.run_scheduler_source_channel_audience_capture_batch",
        fake_run_batch,
    )
    definition = build_scheduler_job_definitions(settings, engine=engine)[-1]

    await definition.action()

    assert definition.id == JOB_ID_SOURCE_CHANNEL_AUDIENCE_CAPTURE
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}


@pytest.mark.asyncio
async def test_motd_job_calls_refresh_service_and_logs_result(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate({"scheduler_motd_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    selected_meme_id = uuid.UUID("00000000-0000-0000-0000-000000000101")
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_refresh(session_factory_arg: object, *, settings: Settings) -> PublicMemeOfTheDayRead:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return PublicMemeOfTheDayRead(
            meme=PublicMemeCardRead(
                id=selected_meme_id,
                media_type=ContentKind.IMAGE,
                language=ContentLanguage.EN,
                is_nsfw=False,
                popularity_score=1.0,
                like_count=0,
                primary_file=None,
                caption=None,
                created_at=datetime(2026, 6, 20, 11, 0, tzinfo=UTC),
                updated_at=datetime(2026, 6, 20, 11, 0, tzinfo=UTC),
            ),
            selected_for=date(2026, 6, 20),
            refreshed_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
            algorithm_version="motd_v1",
            score=1.0,
            score_components={"total": 1.0},
            reason="selected",
            candidate_count=3,
        )

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_scheduler_meme_of_the_day_refresh", fake_refresh)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = build_scheduler_job_definitions(settings, engine=engine)[2]
    await definition.action()

    assert definition.id == JOB_ID_MOTD
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_MOTD,
                "candidate_count": 3,
                "selected_meme_id": str(selected_meme_id),
                "reason": "selected",
                "algorithm_version": "motd_v1",
                "refreshed_at": "2026-06-20T12:00:00+00:00",
            },
        )
    ]


@pytest.mark.asyncio
async def test_search_index_sync_job_calls_batch_service_and_logs_result(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate({"scheduler_search_index_sync_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_run_batch(session_factory_arg: object, *, settings: Settings) -> SearchIndexBatchJobResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return SearchIndexBatchJobResult(
            scanned=3,
            updated=2,
            failed=1,
            skipped=0,
            duration_seconds=0.25,
            index_sync_unsynced_count=7,
            index_sync_failed_count=2,
            index_sync_processing_count=1,
            index_sync_oldest_lag_seconds=120.0,
        )

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_scheduler_search_index_sync_batch", fake_run_batch)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = build_scheduler_job_definitions(settings, engine=engine)[3]
    await definition.action()

    assert definition.id == JOB_ID_SEARCH_INDEX_SYNC
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_SEARCH_INDEX_SYNC,
                "status": "completed",
                "degraded_mode": True,
                "scanned": 3,
                "updated": 2,
                "failed": 1,
                "skipped": 0,
                "duration_seconds": 0.25,
                "index_sync_unsynced_count": 7,
                "index_sync_failed_count": 2,
                "index_sync_processing_count": 1,
                "index_sync_oldest_lag_seconds": 120.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_meilisearch_settings_reconcile_job_calls_service_and_logs_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"scheduler_meilisearch_settings_reconcile_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_reconcile(
        session_factory_arg: object,
        *,
        settings: Settings,
    ) -> MeilisearchSettingsReconcileResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return MeilisearchSettingsReconcileResult(
            status=SearchSynonymSyncStatus.SYNCED,
            reason="applied",
            changed=True,
            desired_hash="a" * 64,
            actual_hash="a" * 64,
            provider_task_uid=42,
            revision_count=2,
            duration_seconds=0.5,
        )

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_meilisearch_settings_reconcile", fake_reconcile)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = build_scheduler_job_definitions(settings, engine=engine)[4]
    await definition.action()

    assert definition.id == JOB_ID_MEILISEARCH_SETTINGS_RECONCILE
    assert definition.run_on_startup is True
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_MEILISEARCH_SETTINGS_RECONCILE,
                "status": "synced",
                "degraded_mode": False,
                "reason": "applied",
                "changed": True,
                "desired_hash": "a" * 64,
                "actual_hash": "a" * 64,
                "provider_task_uid": 42,
                "revision_count": 2,
                "duration_seconds": 0.5,
            },
        )
    ]


@pytest.mark.asyncio
async def test_seo_backlog_job_calls_batch_service_and_logs_result(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate({"scheduler_seo_backlog_batches_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_run_batch(session_factory_arg: object, *, settings: Settings) -> SeoBacklogBatchJobResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return SeoBacklogBatchJobResult(scanned=4, updated=3, failed=0, skipped=1, duration_seconds=0.5)

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_scheduler_seo_backlog_batch", fake_run_batch)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = build_scheduler_job_definitions(settings, engine=engine)[5]
    await definition.action()

    assert definition.id == JOB_ID_SEO_BACKLOG_BATCHES
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_SEO_BACKLOG_BATCHES,
                "status": "completed",
                "degraded_mode": False,
                "scanned": 4,
                "updated": 3,
                "failed": 0,
                "skipped": 1,
                "duration_seconds": 0.5,
            },
        )
    ]


@pytest.mark.asyncio
async def test_rabbitmq_outbox_publisher_job_calls_runtime_and_logs_result(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate({"scheduler_rabbitmq_outbox_publisher_enabled": True})
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_run_batch(
        session_factory_arg: object,
        *,
        settings: Settings,
    ) -> RabbitMQOutboxPublisherBatchResult:
        called["session_factory"] = session_factory_arg
        called["settings"] = settings
        return RabbitMQOutboxPublisherBatchResult(
            recovered=1,
            claimed=3,
            published=2,
            failed=1,
            duration_seconds=0.75,
            outbox_due_count=5,
            outbox_pending_count=4,
            outbox_failed_count=1,
            outbox_publishing_count=2,
            outbox_oldest_due_age_seconds=30.0,
        )

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_rabbitmq_outbox_publisher_batch", fake_run_batch)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = build_scheduler_job_definitions(settings, engine=engine)[6]
    await definition.action()

    assert definition.id == JOB_ID_RABBITMQ_OUTBOX_PUBLISHER
    assert called == {"engine": engine, "session_factory": session_factory, "settings": settings}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_RABBITMQ_OUTBOX_PUBLISHER,
                "status": "completed",
                "degraded_mode": True,
                "recovered": 1,
                "claimed": 3,
                "published": 2,
                "failed": 1,
                "duration_seconds": 0.75,
                "outbox_due_count": 5,
                "outbox_pending_count": 4,
                "outbox_failed_count": 1,
                "outbox_publishing_count": 2,
                "outbox_oldest_due_age_seconds": 30.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_telegram_login_cleanup_job_calls_batch_service_and_logs_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "scheduler_telegram_login_cleanup_enabled": True,
            "scheduler_telegram_login_cleanup_batch_size": 17,
        }
    )
    engine = cast("AsyncEngine", object())
    session_factory = object()
    called: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_build_session_factory(bound_engine: object) -> object:
        called["engine"] = bound_engine
        return session_factory

    async def fake_cleanup(session_factory_arg: object, *, batch_size: int) -> TelegramLoginCleanupBatchResult:
        called["session_factory"] = session_factory_arg
        called["batch_size"] = batch_size
        return TelegramLoginCleanupBatchResult(scanned=4, expired=2, cleaned=3, failed=1)

    def fake_info(message: str, *args: object, extra: dict[str, object] | None = None, **kwargs: object) -> None:
        del args, kwargs
        info_calls.append((message, extra))

    monkeypatch.setattr("memexpert.scheduler.jobs.build_async_session_factory", fake_build_session_factory)
    monkeypatch.setattr("memexpert.scheduler.jobs.run_telegram_login_cleanup_batch", fake_cleanup)
    monkeypatch.setattr("memexpert.scheduler.jobs.logger.info", fake_info)

    definition = next(
        definition
        for definition in build_scheduler_job_definitions(settings, engine=engine)
        if definition.id == JOB_ID_TELEGRAM_LOGIN_CLEANUP
    )
    await definition.action()

    assert definition.id == JOB_ID_TELEGRAM_LOGIN_CLEANUP
    assert called == {"engine": engine, "session_factory": session_factory, "batch_size": 17}
    assert info_calls == [
        (
            "scheduler_job_batch_result",
            {
                "event": "scheduler_job_batch_result",
                "job_id": JOB_ID_TELEGRAM_LOGIN_CLEANUP,
                "status": "completed",
                "degraded_mode": True,
                "scanned": 4,
                "expired": 2,
                "cleaned": 3,
                "failed": 1,
            },
        )
    ]


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
    assert extra.get("status") == "failed"
    assert extra.get("degraded_mode") is True
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
            "scheduler_source_engagement_capture_enabled": False,
            "scheduler_source_channel_audience_capture_enabled": False,
            "scheduler_motd_enabled": True,
            "scheduler_search_index_sync_enabled": False,
            "scheduler_meilisearch_settings_reconcile_enabled": True,
            "scheduler_seo_backlog_batches_enabled": True,
            "scheduler_rabbitmq_outbox_publisher_enabled": False,
            "scheduler_recovery_dispatch_enabled": False,
            "scheduler_media_generation_gc_enabled": False,
            "scheduler_pipeline_capacity_refresh_enabled": False,
            "scheduler_recommendation_profile_rebuild_enabled": False,
            "scheduler_recommendation_analytics_rollup_enabled": False,
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
        JOB_ID_MEILISEARCH_SETTINGS_RECONCILE,
        JOB_ID_SEO_BACKLOG_BATCHES,
        JOB_ID_TELEGRAM_LOGIN_CLEANUP,
    ]
    reconcile_job = next(
        job for job in scheduler.jobs if job["id"] == JOB_ID_MEILISEARCH_SETTINGS_RECONCILE
    )
    assert reconcile_job["max_instances"] == 1
    assert isinstance(reconcile_job["next_run_time"], datetime)
    assert reconcile_job["next_run_time"].tzinfo is not None
    assert all(
        "next_run_time" not in job
        for job in scheduler.jobs
        if job["id"] != JOB_ID_MEILISEARCH_SETTINGS_RECONCILE
    )
    assert engine.dispose_calls == 0


@pytest.mark.asyncio
async def test_scheduler_runtime_skips_disabled_jobs() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": False,
            "scheduler_source_engagement_capture_enabled": False,
            "scheduler_source_channel_audience_capture_enabled": False,
            "scheduler_motd_enabled": False,
            "scheduler_search_index_sync_enabled": False,
            "scheduler_meilisearch_settings_reconcile_enabled": False,
            "scheduler_seo_backlog_batches_enabled": False,
            "scheduler_rabbitmq_outbox_publisher_enabled": False,
            "scheduler_recovery_dispatch_enabled": False,
            "scheduler_media_generation_gc_enabled": False,
            "scheduler_pipeline_capacity_refresh_enabled": False,
            "scheduler_telegram_login_cleanup_enabled": False,
            "scheduler_recommendation_profile_rebuild_enabled": False,
            "scheduler_recommendation_analytics_rollup_enabled": False,
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
