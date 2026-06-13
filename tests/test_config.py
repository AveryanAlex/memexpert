"""Tests for configuration loading, validation, and lazy runtime helpers."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError

from memexpert.core.broker import (
    BrokerConfigurationError,
    BrokerConnectionError,
    build_pipeline_broker,
    ensure_pipeline_broker_started,
    get_pipeline_broker_settings,
    normalize_rabbitmq_url,
    verify_pipeline_broker,
)
from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import RedisConfigurationError, normalize_redis_url
from memexpert.core.storage import (
    StorageConfigurationError,
    StorageConnectionError,
    build_original_object_key,
    build_s3_client,
    build_web_video_object_key,
    normalize_s3_bucket_name,
    normalize_s3_endpoint,
    verify_s3_storage,
)

if TYPE_CHECKING:
    from pathlib import Path


class StartableBroker:
    """FastStream-like broker double that records ping/start behavior."""

    def __init__(self) -> None:
        self.started = False
        self.start_calls = 0
        self.ping_timeouts: list[float | None] = []

    async def ping(self, timeout: float | None) -> bool:
        self.ping_timeouts.append(timeout)
        return self.started

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True


def test_settings_load_from_env_file_and_ignore_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "REDIS_URL=redis://cache.example:6379/9\nEXTRA_FIELD=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.redis_url == "redis://cache.example:6379/9"


def test_settings_parse_security_origins_and_preserve_refresh_cookie_path() -> None:
    settings = Settings.model_validate(
        {
            "security_cors_allowed_origins": "https://memexpert.net, http://localhost:3000",
            "security_cors_allowed_methods": "GET, post , OPTIONS",
        }
    )

    assert settings.security_cors_allowed_origins == (
        "https://memexpert.net",
        "http://localhost:3000",
    )
    assert settings.security_cors_allowed_methods == ("GET", "POST", "OPTIONS")
    assert settings.security_csrf_header_name == "X-Requested-With"


def test_settings_parse_pipeline_contract_and_normalize_object_prefixes() -> None:
    settings = Settings.model_validate(
        {
            "pipeline_allowed_mime_types": "image/jpeg, image/png , image/webp",
            "pipeline_s3_original_prefix": "/uploads/originals/",
            "pipeline_s3_derivative_prefix": "uploads/derived/",
            "s3_bucket": "MemExpert-Uploads",
        }
    )

    assert settings.pipeline_allowed_mime_types == (
        "image/jpeg",
        "image/png",
        "image/webp",
    )
    assert settings.pipeline_s3_original_prefix == "uploads/originals"
    assert settings.pipeline_s3_derivative_prefix == "uploads/derived"
    assert settings.s3_bucket == "memexpert-uploads"

    broker_settings = get_pipeline_broker_settings(settings)
    assert broker_settings.meme_created_routing_key == "pipeline.transcode"
    assert broker_settings.ocr_queue == "pipeline.ocr"
    assert broker_settings.dead_letter_routing_key == "pipeline.dead_letter"


def test_settings_parse_scheduler_contracts() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": False,
            "scheduler_popularity_snapshots_interval_seconds": 120.0,
            "scheduler_motd_interval_seconds": 300.0,
            "scheduler_search_index_sync_interval_seconds": 180.0,
            "scheduler_seo_backlog_batches_interval_seconds": 240.0,
            "scheduler_advisory_lock_enabled": False,
            "scheduler_advisory_lock_key": "123, 456",
        }
    )

    assert settings.scheduler_materialized_view_refresh_enabled is False
    assert settings.scheduler_popularity_snapshots_interval_seconds == 120.0
    assert settings.scheduler_motd_interval_seconds == 300.0
    assert settings.scheduler_search_index_sync_interval_seconds == 180.0
    assert settings.scheduler_seo_backlog_batches_interval_seconds == 240.0
    assert settings.scheduler_advisory_lock_enabled is False
    assert settings.scheduler_advisory_lock_key == (123, 456)


def test_settings_require_imgproxy_key_and_salt_together() -> None:
    with pytest.raises(ValidationError, match="imgproxy_key and imgproxy_salt"):
        _ = Settings.model_validate({"imgproxy_key": "001122"})


def test_settings_default_cors_origin_policy_matches_memexpert_net_but_not_other_tlds() -> None:
    settings = Settings()

    assert re.fullmatch(settings.security_cors_allowed_origin_regex, "https://app.memexpert.net")
    assert re.fullmatch(settings.security_cors_allowed_origin_regex, "https://memexpert.net")
    assert re.fullmatch(settings.security_cors_allowed_origin_regex, "https://app.memexpert.com") is None
    assert re.fullmatch(settings.security_cors_allowed_origin_regex, "https://memexpert.net.evil.example") is None


def test_settings_reject_blank_security_cors_allowed_origins() -> None:
    with pytest.raises(ValidationError):
        _ = Settings.model_validate({"security_cors_allowed_origins": "   "})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"pipeline_operator_token": "   "}, "pipeline_operator_token"),
        ({"pipeline_broker_transcode_queue": "queue with spaces"}, "pipeline broker topology names"),
        ({"pipeline_s3_original_prefix": "../escape"}, "object-key prefixes"),
        ({"pipeline_allowed_mime_types": "jpeg"}, "MIME types"),
        ({"s3_bucket": "BAD_BUCKET"}, "s3_bucket"),
        ({"pipeline_broker_retry_max_attempts": 0}, "greater than or equal to 1"),
        ({"pipeline_broker_retry_backoff_seconds": 0}, "greater than 0"),
    ],
)
def test_settings_reject_invalid_pipeline_contracts(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _ = Settings.model_validate(payload)


@pytest.mark.parametrize(
    "redis_url",
    [
        "",
        "   ",
        "http://cache.example:6379/0",
        "not-a-redis-url",
    ],
)
def test_normalize_redis_url_rejects_invalid_values(redis_url: str) -> None:
    with pytest.raises(RedisConfigurationError):
        _ = normalize_redis_url(redis_url)


@pytest.mark.parametrize(
    "rabbitmq_url",
    [
        "",
        "   ",
        "http://broker.example:5672/",
        "amqp://:badport/",
    ],
)
def test_normalize_rabbitmq_url_rejects_invalid_values(rabbitmq_url: str) -> None:
    with pytest.raises(BrokerConfigurationError):
        _ = normalize_rabbitmq_url(rabbitmq_url)


@pytest.mark.parametrize(
    "s3_endpoint",
    [
        "",
        "   ",
        "amqp://storage.example:9000",
        "http://:9000",
    ],
)
def test_normalize_s3_endpoint_rejects_invalid_values(s3_endpoint: str) -> None:
    with pytest.raises(StorageConfigurationError):
        _ = normalize_s3_endpoint(s3_endpoint)


@pytest.mark.parametrize(
    "bucket_name",
    [
        "",
        "   ",
        "invalid_bucket",
        "ab",
        "192.168.1.10",
    ],
)
def test_normalize_s3_bucket_name_rejects_invalid_values(bucket_name: str) -> None:
    with pytest.raises(StorageConfigurationError):
        _ = normalize_s3_bucket_name(bucket_name)


def test_pipeline_runtime_helpers_are_lazy_until_verified() -> None:
    settings = Settings.model_validate(
        {
            "rabbitmq_url": "amqp://guest:guest@127.0.0.1:9/",
            "s3_endpoint": "http://127.0.0.1:9",
            "s3_access_key": "test-access",
            "s3_secret_key": "test-secret",
            "s3_bucket": "memexpert-runtime",
        }
    )

    broker = build_pipeline_broker(settings)
    storage_client = cast("object", build_s3_client(settings))

    assert broker is not None
    assert storage_client is not None


async def test_ensure_pipeline_broker_started_starts_an_unconnected_broker_once() -> None:
    settings = Settings.model_validate(
        {
            "rabbitmq_url": "amqp://guest:guest@127.0.0.1:5672/",
            "pipeline_broker_connection_timeout_seconds": 0.2,
        }
    )
    broker = StartableBroker()

    first = await ensure_pipeline_broker_started(cast("Any", broker), settings=settings)
    second = await ensure_pipeline_broker_started(cast("Any", broker), settings=settings)

    assert cast("Any", first) is broker
    assert cast("Any", second) is broker
    assert broker.start_calls == 1
    assert broker.ping_timeouts == [0.2, 0.2, 0.2]


async def test_verify_pipeline_broker_reports_unreachable_runtime() -> None:
    settings = Settings.model_validate(
        {
            "rabbitmq_url": "amqp://guest:guest@127.0.0.1:9/",
            "pipeline_broker_connection_timeout_seconds": 0.2,
        }
    )

    with pytest.raises(BrokerConnectionError, match=r"(Unable to verify|Timed out)"):
        _ = await verify_pipeline_broker(settings, timeout=0.5)


async def test_verify_s3_storage_reports_unreachable_runtime() -> None:
    settings = Settings.model_validate(
        {
            "s3_endpoint": "http://127.0.0.1:9",
            "s3_access_key": "test-access",
            "s3_secret_key": "test-secret",
            "s3_bucket": "memexpert-runtime",
            "pipeline_storage_connection_timeout_seconds": 0.2,
        }
    )

    client = cast("object", build_s3_client(settings))

    with pytest.raises(StorageConnectionError, match=r"(Unable to verify|Timed out)"):
        _ = await verify_s3_storage(client, settings, timeout=0.5)


def test_pipeline_object_key_builders_follow_a_stable_contract() -> None:
    meme_file_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    settings = Settings.model_validate(
        {
            "pipeline_s3_original_prefix": "pipeline/originals",
            "pipeline_s3_derivative_prefix": "pipeline/derived",
        }
    )

    original_key = build_original_object_key(meme_file_id, "../strange name.JPEG", settings=settings)
    web_video_key = build_web_video_object_key(meme_file_id, extension="MP4", settings=settings)

    assert original_key == f"pipeline/originals/{meme_file_id}/original.jpeg"
    assert web_video_key == f"pipeline/derived/{meme_file_id}/web.mp4"


def test_get_settings_returns_cached_instance() -> None:
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()

    assert first is second
