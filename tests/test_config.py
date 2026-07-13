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


def test_settings_parse_security_origins_and_preserve_csrf_header_default() -> None:
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
    assert broker_settings.source_engagement_capture_queue == "pipeline.source_engagement_capture"
    assert broker_settings.source_engagement_capture_routing_key == "pipeline.source_engagement_capture"
    assert broker_settings.source_engagement_capture_binding_key == "pipeline.source_engagement_capture.#"
    assert broker_settings.source_engagement_capture_queue_for_session("session-a.abc123") == (
        "pipeline.source_engagement_capture.session-a.abc123"
    )
    assert broker_settings.source_engagement_capture_retry_queue_for_session("session-a.abc123") == (
        "pipeline.source_engagement_capture.session-a.abc123.retry"
    )
    assert broker_settings.source_engagement_capture_binding_key_for_session("session-a.abc123") == (
        "pipeline.source_engagement_capture.session-a.abc123"
    )
    assert broker_settings.source_engagement_capture_retry_request_routing_key_for_session("session-a.abc123") == (
        "pipeline.retry.source_engagement_capture.session-a.abc123"
    )
    assert broker_settings.source_engagement_capture_retry_routing_key_for_session("session-a.abc123") == (
        "pipeline.source_engagement_capture_retry.session-a.abc123"
    )
    assert broker_settings.dead_letter_routing_key == "pipeline.dead_letter"


def test_settings_ocr_defaults_are_honest_about_missing_fallback() -> None:
    settings = Settings()

    assert settings.pipeline_transcode_timeout_seconds == 180.0
    assert settings.pipeline_ocr_primary_engine == "paddleocr"
    assert settings.pipeline_ocr_paddle_command is None
    assert settings.pipeline_ocr_fallback_engine is None
    assert settings.pipeline_ocr_fallback_command is None
    assert settings.pipeline_ocr_timeout_seconds == 120.0


def test_settings_pipeline_worker_prefetch_count_defaults_and_bounds() -> None:
    assert Settings().pipeline_worker_prefetch_count == 1
    assert Settings(pipeline_worker_prefetch_count=8).pipeline_worker_prefetch_count == 8

    with pytest.raises(ValidationError):
        _ = Settings(pipeline_worker_prefetch_count=0)
    with pytest.raises(ValidationError):
        _ = Settings(pipeline_worker_prefetch_count=513)


def test_settings_include_telegram_session_encryption_secret_without_session_dir() -> None:
    settings = Settings.model_validate(
        {"telegram_session_encryption_secret": "  test-telegram-session-encryption-secret  "},
    )

    assert settings.telegram_session_encryption_secret.get_secret_value() == "test-telegram-session-encryption-secret"
    assert not hasattr(settings, "telegram_session_dir")
    with pytest.raises(ValidationError, match="telegram_session_encryption_secret"):
        _ = Settings.model_validate({"telegram_session_encryption_secret": "  "})


def test_settings_crawler_reconcile_interval_defaults_and_requires_positive_value() -> None:
    settings = Settings()

    assert settings.crawler_reconcile_interval_seconds == 10.0
    assert settings.crawler_default_catchup_message_limit == 5000
    overridden = Settings.model_validate({"crawler_reconcile_interval_seconds": 2.5})
    assert overridden.crawler_reconcile_interval_seconds == 2.5
    with pytest.raises(ValidationError):
        _ = Settings.model_validate({"crawler_reconcile_interval_seconds": 0})


def test_settings_normalize_blank_ocr_command_settings_to_none() -> None:
    settings = Settings.model_validate(
        {
            "pipeline_ocr_paddle_command": "  ",
            "pipeline_ocr_fallback_engine": "  ",
            "pipeline_ocr_fallback_command": "  ",
        }
    )

    assert settings.pipeline_ocr_paddle_command is None
    assert settings.pipeline_ocr_fallback_engine is None
    assert settings.pipeline_ocr_fallback_command is None


def test_settings_seo_image_byte_cap_defaults_and_requires_positive_value() -> None:
    settings = Settings()

    assert settings.pipeline_seo_image_max_bytes == 5 * 1024 * 1024
    with pytest.raises(ValidationError):
        _ = Settings.model_validate({"pipeline_seo_image_max_bytes": 0})


def test_settings_parse_scheduler_contracts() -> None:
    settings = Settings.model_validate(
        {
            "scheduler_materialized_view_refresh_enabled": False,
            "scheduler_source_engagement_capture_enabled": False,
            "scheduler_source_engagement_capture_interval_seconds": 120.0,
            "scheduler_source_engagement_capture_batch_size": 7,
            "scheduler_source_engagement_capture_per_session_batch_size": 3,
            "scheduler_source_engagement_capture_lease_timeout_seconds": 45.0,
            "scheduler_motd_interval_seconds": 300.0,
            "motd_algorithm_version": " motd_test_v2 ",
            "motd_candidate_lookback_days": 14,
            "motd_candidate_limit": 25,
            "motd_min_quality_score": 0.75,
            "motd_popularity_weight": 0.4,
            "motd_trending_growth_weight": 0.3,
            "motd_novelty_weight": 0.2,
            "motd_quality_weight": 0.1,
            "scheduler_search_index_sync_interval_seconds": 180.0,
            "scheduler_search_index_sync_batch_size": 7,
            "scheduler_search_index_sync_processing_timeout_seconds": 45.0,
            "scheduler_seo_backlog_batches_interval_seconds": 240.0,
            "scheduler_seo_backlog_batch_size": 9,
            "scheduler_telegram_login_cleanup_enabled": False,
            "scheduler_telegram_login_cleanup_interval_seconds": 30.0,
            "scheduler_telegram_login_cleanup_batch_size": 17,
            "scheduler_advisory_lock_enabled": False,
            "scheduler_advisory_lock_key": "123, 456",
        }
    )

    assert settings.scheduler_materialized_view_refresh_enabled is False
    assert settings.scheduler_source_engagement_capture_enabled is False
    assert settings.scheduler_source_engagement_capture_interval_seconds == 120.0
    assert settings.scheduler_source_engagement_capture_batch_size == 7
    assert settings.scheduler_source_engagement_capture_per_session_batch_size == 3
    assert settings.scheduler_source_engagement_capture_lease_timeout_seconds == 45.0
    assert settings.scheduler_motd_interval_seconds == 300.0
    assert settings.motd_algorithm_version == "motd_test_v2"
    assert settings.motd_candidate_lookback_days == 14
    assert settings.motd_candidate_limit == 25
    assert settings.motd_min_quality_score == 0.75
    assert settings.motd_popularity_weight == 0.4
    assert settings.motd_trending_growth_weight == 0.3
    assert settings.motd_novelty_weight == 0.2
    assert settings.motd_quality_weight == 0.1
    assert settings.scheduler_search_index_sync_interval_seconds == 180.0
    assert settings.scheduler_search_index_sync_batch_size == 7
    assert settings.scheduler_search_index_sync_processing_timeout_seconds == 45.0
    assert settings.scheduler_seo_backlog_batches_interval_seconds == 240.0
    assert settings.scheduler_seo_backlog_batch_size == 9
    assert settings.scheduler_telegram_login_cleanup_enabled is False
    assert settings.scheduler_telegram_login_cleanup_interval_seconds == 30.0
    assert settings.scheduler_telegram_login_cleanup_batch_size == 17
    assert settings.scheduler_advisory_lock_enabled is False
    assert settings.scheduler_advisory_lock_key == (123, 456)


def test_settings_scheduler_source_engagement_defaults_match_design() -> None:
    settings = Settings()

    assert settings.scheduler_source_engagement_capture_enabled is True
    assert settings.scheduler_source_engagement_capture_interval_seconds == 21600.0
    assert settings.scheduler_source_engagement_capture_batch_size == 100
    assert settings.scheduler_source_engagement_capture_per_session_batch_size == 20
    assert settings.scheduler_source_engagement_capture_lease_timeout_seconds == 1800.0


def test_settings_scheduler_batch_job_defaults_match_design() -> None:
    settings = Settings()

    assert settings.motd_algorithm_version == "motd_v1"
    assert settings.motd_candidate_lookback_days == 30
    assert settings.motd_candidate_limit == 50
    assert settings.motd_min_quality_score == 0.5
    assert settings.motd_popularity_weight == 0.35
    assert settings.motd_trending_growth_weight == 0.30
    assert settings.motd_novelty_weight == 0.20
    assert settings.motd_quality_weight == 0.15
    assert settings.scheduler_search_index_sync_batch_size == 50
    assert settings.scheduler_search_index_sync_processing_timeout_seconds == 900.0
    assert settings.scheduler_seo_backlog_batch_size == 25


@pytest.mark.parametrize(
    "field_name,bad_value",
    [
        ("scheduler_search_index_sync_batch_size", 0),
        ("scheduler_source_engagement_capture_per_session_batch_size", 0),
        ("scheduler_search_index_sync_processing_timeout_seconds", 0.0),
        ("scheduler_seo_backlog_batch_size", 0),
        ("motd_candidate_lookback_days", 0),
        ("motd_candidate_limit", 0),
        ("motd_min_quality_score", -0.1),
        ("motd_popularity_weight", -0.1),
    ],
)
def test_settings_reject_invalid_scheduler_batch_job_settings(field_name: str, bad_value: object) -> None:
    with pytest.raises(ValidationError):
        _ = Settings.model_validate({field_name: bad_value})


def test_settings_reject_blank_motd_algorithm_version() -> None:
    with pytest.raises(ValidationError, match="motd_algorithm_version"):
        _ = Settings.model_validate({"motd_algorithm_version": "   "})


def test_settings_require_imgproxy_key_and_salt_together() -> None:
    with pytest.raises(ValidationError, match="imgproxy_key and imgproxy_salt"):
        _ = Settings.model_validate({"imgproxy_key": "001122"})


def test_settings_allows_signed_imgproxy_internal_base_with_public_override() -> None:
    settings = Settings.model_validate(
        {
            "imgproxy_base_url": "http://imgproxy:8080",
            "imgproxy_public_base_url": " https://img.memexpert.net/ ",
            "imgproxy_key": "00112233445566778899aabbccddeeff",
            "imgproxy_salt": "ffeeddccbbaa99887766554433221100",
        }
    )

    assert settings.imgproxy_base_url == "http://imgproxy:8080"
    assert settings.imgproxy_public_base_url == "https://img.memexpert.net"
    assert settings.imgproxy_render_base_url == "https://img.memexpert.net"


@pytest.mark.parametrize(
    "imgproxy_base_url",
    [
        "http://imgproxy:8080",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://0.0.0.0:8080",
        "http://192.168.1.10:8080",
        "http://169.254.1.1:8080",
        "img.memexpert.net",
    ],
)
def test_settings_reject_signed_imgproxy_without_browser_reachable_base(imgproxy_base_url: str) -> None:
    with pytest.raises(ValidationError, match="browser-reachable"):
        _ = Settings.model_validate(
            {
                "imgproxy_base_url": imgproxy_base_url,
                "imgproxy_key": "00112233445566778899aabbccddeeff",
                "imgproxy_salt": "ffeeddccbbaa99887766554433221100",
            }
        )


def test_settings_trim_and_validate_auth_access_cookie_fields() -> None:
    settings = Settings.model_validate(
        {
            "auth_access_cookie_name": "  memexpert_session  ",
            "auth_access_cookie_path": "  /auth  ",
        }
    )

    assert settings.auth_access_cookie_name == "memexpert_session"
    assert settings.auth_access_cookie_path == "/auth"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"auth_access_cookie_name": "   "}, "auth_access_cookie_name must not be blank"),
        ({"auth_access_cookie_path": "   "}, "auth_access_cookie_path must not be blank"),
        ({"auth_access_cookie_path": "auth"}, "auth_access_cookie_path must start with '/'"),
    ],
)
def test_settings_reject_invalid_auth_access_cookie_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _ = Settings.model_validate(payload)


def test_settings_normalize_blank_auth_access_cookie_domain_to_none() -> None:
    settings = Settings.model_validate({"auth_access_cookie_domain": "   "})

    assert settings.auth_access_cookie_domain is None


def test_settings_require_secure_cookies_for_samesite_none() -> None:
    with pytest.raises(
        ValidationError,
        match="auth_access_cookie_samesite='none' requires auth_access_cookie_secure=true",
    ):
        _ = Settings.model_validate(
            {
                "auth_access_cookie_samesite": "none",
                "auth_access_cookie_secure": False,
            }
        )


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
    ("field_name", "value"),
    [
        ("security_rate_limit_auth_write_max_requests", 0),
        ("security_rate_limit_auth_write_window_seconds", 0),
        ("security_rate_limit_search_feed_max_requests", 0),
        ("security_rate_limit_search_feed_window_seconds", 0),
        ("security_rate_limit_write_max_requests", 0),
        ("security_rate_limit_write_window_seconds", 0),
        ("security_rate_limit_upload_max_requests", 0),
        ("security_rate_limit_upload_window_seconds", 0),
        ("security_rate_limit_admin_max_requests", 0),
        ("security_rate_limit_admin_window_seconds", 0),
    ],
)
def test_security_rate_limit_settings_reject_non_positive_values(
    field_name: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        _ = Settings.model_validate({field_name: value})


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
