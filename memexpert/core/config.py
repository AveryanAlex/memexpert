"""Application settings loaded from environment variables and .env files."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, ClassVar, Literal, cast
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

SECURITY_DEFAULT_ALLOWED_ORIGINS = (
    "https://memexpert.net",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://web.telegram.org",
    "https://oauth.telegram.org",
)
SECURITY_DEFAULT_ALLOWED_METHODS = (
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
)
SECURITY_DEFAULT_ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    "X-Requested-With",
)
PIPELINE_DEFAULT_ALLOWED_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/quicktime",
    "video/webm",
)
_ALLOWED_ORIGIN_LIST_ADAPTER = TypeAdapter(tuple[AnyHttpUrl, ...])
_PIPELINE_TOPOLOGY_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PIPELINE_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class Settings(BaseSettings):
    """Runtime configuration for local development and deployed environments."""

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = "postgresql+asyncpg://memexpert:memexpert@localhost:5432/memexpert"
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    database_application_name: str = "memexpert"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://memexpert:memexpert@localhost:5672/"
    qdrant_url: str = "http://localhost:6333"
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_master_key: str | None = "memexpert-dev-key"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "memexpert"
    s3_secret_key: str = "memexpert-secret"
    s3_bucket: str = "memexpert"
    s3_region: str = "us-east-1"
    imgproxy_base_url: str = "http://localhost:8080"
    imgproxy_public_base_url: str | None = None
    imgproxy_key: SecretStr | None = None
    imgproxy_salt: SecretStr | None = None
    media_public_base_url: str | None = None
    pipeline_operator_token: SecretStr = SecretStr("memexpert-dev-pipeline-operator-token-min-32")
    pipeline_image_upload_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    pipeline_gif_upload_max_bytes: int = Field(default=40 * 1024 * 1024, gt=0)
    pipeline_video_upload_max_bytes: int = Field(default=150 * 1024 * 1024, gt=0)
    pipeline_allowed_mime_types: Annotated[tuple[str, ...], NoDecode] = PIPELINE_DEFAULT_ALLOWED_MIME_TYPES
    pipeline_phash_size: int = Field(default=16, ge=4, le=64)
    pipeline_image_max_pixels: int = Field(default=45_000_000, gt=0)
    pipeline_transcode_timeout_seconds: float = Field(default=180.0, gt=0.0, le=900.0)
    pipeline_ffmpeg_binary: str = "ffmpeg"
    pipeline_ffprobe_binary: str = "ffprobe"
    pipeline_s3_original_prefix: str = "pipeline/originals"
    pipeline_s3_temp_original_prefix: str = "pipeline/temp-originals"
    pipeline_s3_derivative_prefix: str = "pipeline/derived"
    pipeline_broker_exchange: str = "memexpert.pipeline"
    pipeline_broker_routing_key_prefix: str = "pipeline"
    pipeline_broker_media_inspect_queue: str = "pipeline.media_inspect"
    pipeline_broker_source_engagement_capture_queue: str = "pipeline.source_engagement_capture"
    pipeline_broker_transcode_queue: str = "pipeline.transcode"
    pipeline_broker_ocr_queue: str = "pipeline.ocr"
    pipeline_broker_embed_queue: str = "pipeline.embed"
    pipeline_broker_classify_queue: str = "pipeline.classify"
    pipeline_broker_sync_qdrant_queue: str = "pipeline.sync_qdrant"
    pipeline_broker_sync_meili_queue: str = "pipeline.sync_meili"
    pipeline_broker_retry_exchange: str = "memexpert.pipeline.retry"
    pipeline_broker_retry_queue: str = "pipeline.retry"
    pipeline_broker_dead_letter_exchange: str = "memexpert.pipeline.dlx"
    pipeline_broker_dead_letter_queue: str = "pipeline.dlq"
    pipeline_broker_retry_max_attempts: int = Field(default=5, ge=1, le=32)
    pipeline_broker_retry_backoff_seconds: float = Field(default=5.0, gt=0.0, le=3600.0)
    pipeline_broker_connection_timeout_seconds: float = Field(default=5.0, gt=0.0)
    pipeline_worker_prefetch_count: int = Field(default=1, ge=1, le=512)
    pipeline_worker_graceful_shutdown_timeout_seconds: float = Field(default=210.0, gt=0.0, le=900.0)
    pipeline_capacity_close_pending_count: int = Field(default=1000, ge=10, le=1_000_000)
    pipeline_capacity_reopen_pending_count: int = Field(default=500, ge=0, le=999_999)
    pipeline_capacity_close_oldest_age_seconds: float = Field(default=3600.0, gt=0.0, le=604800.0)
    pipeline_capacity_reopen_oldest_age_seconds: float = Field(default=900.0, ge=0.0, le=604799.0)
    pipeline_circuit_failure_threshold: int = Field(default=3, ge=1, le=32)
    pipeline_circuit_cooldown_seconds: float = Field(default=30.0, gt=0.0, le=3600.0)
    pipeline_stuck_reclaim_after_seconds: float = Field(default=900.0, gt=60.0, le=86400.0)
    recovery_telegram_poll_interval_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    recovery_telegram_batch_size: int = Field(default=10, ge=1, le=100)
    runtime_health_file: Path = Path("/tmp/memexpert-runtime-health.json")
    runtime_health_interval_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    runtime_health_stale_after_seconds: float = Field(default=45.0, gt=0.0, le=900.0)
    runtime_health_operation_timeout_seconds: float = Field(default=900.0, gt=0.0, le=7200.0)
    pipeline_storage_connection_timeout_seconds: float = Field(default=5.0, gt=0.0)
    pipeline_ocr_primary_engine: str = "paddleocr"
    pipeline_ocr_paddle_command: str | None = None
    pipeline_ocr_fallback_engine: str | None = None
    pipeline_ocr_fallback_command: str | None = None
    pipeline_ocr_provider_mode: Literal["live", "fake"] = "live"
    pipeline_fake_ocr_text: str = "cat e2e smoke fake ocr text"
    pipeline_ocr_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    pipeline_ocr_low_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    pipeline_voyage_provider_mode: Literal["live", "fake"] = "live"
    pipeline_voyage_model: str = "voyage-multimodal-3.5"
    pipeline_voyage_output_dimensions: int = Field(default=1024, ge=1)
    pipeline_voyage_api_url: str = "https://api.voyageai.com/v1/multimodalembeddings"
    pipeline_voyage_api_key: SecretStr | None = None
    pipeline_voyage_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    pipeline_qdrant_collection_name: str = "memexpert-memes"
    pipeline_qdrant_search_top_k: int = Field(default=5, ge=1, le=100)
    pipeline_qdrant_timeout_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    pipeline_meilisearch_index_name: str = "memexpert-memes"
    pipeline_meilisearch_timeout_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    meilisearch_settings_task_timeout_seconds: float = Field(default=600.0, gt=0.0, le=3600.0)
    search_candidate_pool_limit_per_source: int = Field(default=200, ge=100, le=500)
    recommendation_positive_lookback_hours: int = Field(default=168, ge=1, le=2160)
    recommendation_impression_lookback_hours: int = Field(default=72, ge=1, le=720)
    recommendation_positive_signal_limit: int = Field(default=50, ge=1, le=500)
    recommendation_qdrant_candidate_limit: int = Field(default=80, ge=1, le=500)
    # Tunable personalized-recommendation inputs. These are conservative starter
    # weights for short-term positive signals, not final product truth.
    recommendation_signal_favorite_weight: float = Field(default=4.0, ge=0.0, le=1000.0)
    recommendation_signal_like_weight: float = Field(default=5.0, ge=0.0, le=1000.0)
    recommendation_signal_save_weight: float = Field(default=4.0, ge=0.0, le=1000.0)
    recommendation_signal_pin_weight: float = Field(default=5.0, ge=0.0, le=1000.0)
    recommendation_signal_download_weight: float = Field(default=2.0, ge=0.0, le=1000.0)
    recommendation_signal_telegram_send_weight: float = Field(default=3.0, ge=0.0, le=1000.0)
    recommendation_signal_telegram_chosen_inline_weight: float = Field(default=3.0, ge=0.0, le=1000.0)
    recommendation_signal_telegram_sent_weight: float = Field(default=3.0, ge=0.0, le=1000.0)
    recommendation_signal_detail_view_weight: float = Field(default=1.5, ge=0.0, le=1000.0)
    recommendation_signal_view_weight: float = Field(default=0.75, ge=0.0, le=1000.0)
    recommendation_signal_collection_add_weight: float = Field(default=3.0, ge=0.0, le=1000.0)
    recommendation_signal_durable_pin_weight: float = Field(default=4.0, ge=0.0, le=1000.0)
    recommendation_signal_durable_collection_weight: float = Field(default=2.5, ge=0.0, le=1000.0)
    pipeline_merge_similarity_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    pipeline_classification_api_url: str | None = None
    pipeline_classification_api_key: SecretStr | None = None
    pipeline_classification_provider_mode: Literal["live", "fake"] = "live"
    pipeline_classification_model: str = "memexpert-nsfw-v1"
    pipeline_fake_classification_nsfw_score: float = Field(default=0.0, ge=0.0, le=1.0)
    pipeline_classification_timeout_seconds: float = Field(default=15.0, gt=0.0, le=600.0)
    pipeline_classification_nsfw_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    pipeline_seo_provider_mode: Literal["static", "live"] = "static"
    pipeline_seo_model: str = Field(default="gpt-5-mini", min_length=1, max_length=255)
    pipeline_seo_api_base_url: str | None = None
    pipeline_seo_api_key: SecretStr | None = None
    pipeline_seo_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    pipeline_seo_max_attempts: int = Field(default=2, ge=1, le=16)
    pipeline_seo_image_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    pipeline_seo_prompt_version: str = Field(default="meme-seo-v1", min_length=1, max_length=64)
    pipeline_worker_fail_transcode_for_meme_file_id: str | None = None
    pipeline_worker_fail_embed_for_meme_file_id: str | None = None
    pipeline_worker_fail_classify_for_meme_file_id: str | None = None
    pipeline_worker_fail_sync_qdrant_for_meme_file_id: str | None = None
    pipeline_worker_fail_sync_meili_for_meme_file_id: str | None = None
    scheduler_materialized_view_refresh_enabled: bool = True
    scheduler_materialized_view_refresh_interval_seconds: float = Field(default=300.0, gt=0.0)
    scheduler_source_engagement_capture_enabled: bool = True
    scheduler_source_engagement_capture_interval_seconds: float = Field(default=21600.0, gt=0.0)
    scheduler_source_engagement_capture_batch_size: int = Field(default=100, ge=1, le=1000)
    scheduler_source_engagement_capture_per_session_batch_size: int = Field(default=20, ge=1, le=1000)
    scheduler_source_engagement_capture_lease_timeout_seconds: float = Field(default=1800.0, gt=0.0)
    scheduler_motd_enabled: bool = True
    scheduler_motd_interval_seconds: float = Field(default=86400.0, gt=0.0)
    motd_algorithm_version: str = Field(default="motd_v1", min_length=1, max_length=64)
    motd_candidate_lookback_days: int = Field(default=30, ge=1, le=365)
    motd_candidate_limit: int = Field(default=50, ge=1, le=500)
    motd_min_quality_score: float = Field(default=0.5, ge=0.0, le=1.0)
    motd_popularity_weight: float = Field(default=0.35, ge=0.0, le=1000.0)
    motd_trending_growth_weight: float = Field(default=0.30, ge=0.0, le=1000.0)
    motd_novelty_weight: float = Field(default=0.20, ge=0.0, le=1000.0)
    motd_quality_weight: float = Field(default=0.15, ge=0.0, le=1000.0)
    scheduler_search_index_sync_enabled: bool = True
    scheduler_search_index_sync_interval_seconds: float = Field(default=600.0, gt=0.0)
    scheduler_search_index_sync_batch_size: int = Field(default=50, ge=1, le=1000)
    scheduler_search_index_sync_processing_timeout_seconds: float = Field(default=900.0, gt=0.0)
    scheduler_meilisearch_settings_reconcile_enabled: bool = True
    scheduler_meilisearch_settings_reconcile_interval_seconds: float = Field(default=60.0, gt=0.0)
    scheduler_seo_backlog_batches_enabled: bool = True
    scheduler_seo_backlog_batches_interval_seconds: float = Field(default=900.0, gt=0.0)
    scheduler_seo_backlog_batch_size: int = Field(default=25, ge=1, le=500)
    scheduler_rabbitmq_outbox_publisher_enabled: bool = True
    scheduler_rabbitmq_outbox_publisher_interval_seconds: float = Field(default=5.0, gt=0.0)
    scheduler_rabbitmq_outbox_publisher_batch_size: int = Field(default=100, ge=1, le=1000)
    scheduler_rabbitmq_outbox_publisher_stale_timeout_seconds: float = Field(default=300.0, gt=0.0)
    scheduler_recovery_dispatch_enabled: bool = True
    scheduler_recovery_dispatch_interval_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    scheduler_recovery_dispatch_batch_size: int = Field(default=50, ge=1, le=1000)
    scheduler_pipeline_capacity_refresh_enabled: bool = True
    scheduler_pipeline_capacity_refresh_interval_seconds: float = Field(default=15.0, gt=0.0, le=300.0)
    scheduler_telegram_login_cleanup_enabled: bool = True
    scheduler_telegram_login_cleanup_interval_seconds: float = Field(default=60.0, gt=0.0)
    scheduler_telegram_login_cleanup_batch_size: int = Field(default=100, ge=1, le=1000)
    scheduler_advisory_lock_enabled: bool = True
    scheduler_advisory_lock_key: Annotated[tuple[int, int], NoDecode] = (0, 0)
    # --- S04: curated Telethon crawler + freshness SLO -----------------
    # ``telegram_api_id`` / ``telegram_api_hash`` are deliberately optional so
    # the pipeline service and the ``FakeTelegramClient`` stay side-effect free;
    # the real Telethon adapter validates them only when a DB-backed session is
    # loaded.
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_session_encryption_secret: SecretStr = SecretStr(
        "memexpert-dev-telegram-session-encryption-secret-change-me",
    )
    # Conservative crawler rate: the Telethon docs and the tech design both
    # cap user-bot sessions at 30 req/s. The real adapter enforces this limit
    # and freshness snapshots expose its downstream effect.
    crawler_max_requests_per_second: float = Field(default=15.0, gt=0, le=30)
    crawler_live_mode_enabled: bool = True
    crawler_reconcile_interval_seconds: float = Field(default=10.0, gt=0.0, le=3600.0)
    # Telethon has its own retry loop, but every logical crawler operation
    # still needs a caller-owned deadline so a bad socket or cross-DC request
    # cannot pin an account forever.  Keep the retry counts deliberately low:
    # the durable crawler/backfill layers own retries across logical attempts.
    crawler_telegram_request_retries: int = Field(default=2, ge=0, le=10)
    crawler_telegram_connection_retries: int = Field(default=2, ge=0, le=10)
    crawler_telegram_connect_timeout_seconds: float = Field(default=20.0, gt=0.0, le=300.0)
    crawler_telegram_resolve_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    crawler_telegram_history_page_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    crawler_telegram_single_message_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    crawler_telegram_media_download_timeout_seconds: float = Field(default=120.0, gt=0.0, le=1800.0)
    # Freshness SLO budgets (publish → both sync targets synced) in seconds.
    # Runtime code surfaces measured p50/p95 against these numbers through the
    # operator inspect surface so drift is visible.
    crawler_freshness_slo_p50_seconds: float = Field(default=60.0, gt=0)
    crawler_freshness_slo_p95_seconds: float = Field(default=180.0, gt=0)
    crawler_default_catchup_message_limit: int = Field(default=5000, gt=0, le=10000)
    auth_jwt_secret: SecretStr = SecretStr("memexpert-dev-jwt-secret-with-32-byte-minimum")
    auth_access_token_algorithm: Literal["HS256"] = "HS256"
    # Access tokens are long-lived because revocation is handled by the
    # per-user ``token_nonce`` counter: logout-everywhere bumps the nonce
    # and every outstanding JWT is instantly invalid on the next request.
    # A 30-day TTL keeps natural hygiene for abandoned sessions.
    auth_access_token_ttl_seconds: int = Field(default=30 * 24 * 60 * 60, gt=0)
    # Cookie-only transport for the access token. The JWT lives
    # exclusively in ``memexpert_access_token`` — no ``Authorization:
    # Bearer`` fallback, no ``access_token`` in response bodies.
    auth_access_cookie_name: str = "memexpert_access_token"
    auth_access_cookie_secure: bool = True
    auth_access_cookie_httponly: bool = True
    auth_access_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    auth_access_cookie_path: str = "/"
    auth_access_cookie_domain: str | None = None
    auth_bcrypt_rounds: int = Field(default=12, ge=4, le=31)
    auth_telegram_bot_token: SecretStr | None = None
    auth_telegram_bot_username: str | None = None
    auth_telegram_login_max_age_seconds: int = Field(default=300, gt=0)
    auth_telegram_miniapp_max_age_seconds: int = Field(default=300, gt=0)
    auth_telegram_link_code_ttl_seconds: int = Field(default=600, gt=0)
    auth_telegram_link_return_url: AnyHttpUrl | None = None
    auth_google_client_id: str | None = None
    auth_google_client_secret: SecretStr | None = None
    auth_google_redirect_uri: str | None = None
    auth_google_token_url: str = "https://oauth2.googleapis.com/token"
    auth_google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    auth_google_timeout_seconds: float = Field(default=10.0, gt=0.0)
    security_cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = SECURITY_DEFAULT_ALLOWED_ORIGINS
    security_cors_allowed_origin_regex: str = r"^https://([a-z0-9-]+\.)?memexpert\.net$"
    security_cors_allowed_methods: Annotated[tuple[str, ...], NoDecode] = SECURITY_DEFAULT_ALLOWED_METHODS
    security_cors_allowed_headers: Annotated[tuple[str, ...], NoDecode] = SECURITY_DEFAULT_ALLOWED_HEADERS
    security_csrf_header_name: str = "X-Requested-With"
    security_rate_limit_enabled: bool = True
    security_rate_limit_fail_closed: bool = True
    security_rate_limit_redis_timeout_seconds: float = Field(default=0.5, gt=0.0)
    security_rate_limit_auth_write_max_requests: int = Field(default=10, gt=0)
    security_rate_limit_auth_write_window_seconds: int = Field(default=60, gt=0)
    security_rate_limit_search_feed_max_requests: int = Field(default=30, gt=0)
    security_rate_limit_search_feed_window_seconds: int = Field(default=60, gt=0)
    security_rate_limit_analytics_write_max_requests: int = Field(default=6000, gt=0)
    security_rate_limit_analytics_write_window_seconds: int = Field(default=60, gt=0)
    security_rate_limit_write_max_requests: int = Field(default=60, gt=0)
    security_rate_limit_write_window_seconds: int = Field(default=60, gt=0)
    security_rate_limit_upload_max_requests: int = Field(default=10, gt=0)
    security_rate_limit_upload_window_seconds: int = Field(default=60, gt=0)
    security_rate_limit_admin_max_requests: int = Field(default=120, gt=0)
    security_rate_limit_admin_window_seconds: int = Field(default=60, gt=0)

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator(
        "database_application_name",
        "rabbitmq_url",
        "s3_endpoint",
        "s3_access_key",
        "s3_secret_key",
        "s3_region",
        mode="before",
    )
    @classmethod
    def _normalize_required_runtime_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("runtime settings must not be blank.")
        return normalized_value

    @field_validator("imgproxy_base_url", mode="before")
    @classmethod
    def _normalize_imgproxy_base_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip().rstrip("/")
        if not normalized_value:
            raise ValueError("imgproxy_base_url must not be blank.")
        return normalized_value

    @field_validator("imgproxy_key", "imgproxy_salt", "pipeline_seo_api_key", mode="before")
    @classmethod
    def _normalize_optional_secret_text(cls, value: object) -> object:
        if value is None:
            return None
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw_value, str):
            return value
        normalized_value = raw_value.strip()
        return normalized_value or None

    @field_validator("telegram_session_encryption_secret", mode="before")
    @classmethod
    def _normalize_required_secret_text(cls, value: object) -> object:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw_value, str):
            return value

        normalized_value = raw_value.strip()
        if not normalized_value:
            raise ValueError("telegram_session_encryption_secret must not be blank.")
        return normalized_value

    @field_validator("imgproxy_public_base_url", "media_public_base_url", "pipeline_seo_api_base_url", mode="before")
    @classmethod
    def _normalize_optional_base_url_or_path(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        normalized_value = value.strip().rstrip("/")
        return normalized_value or None

    @model_validator(mode="after")
    def _validate_imgproxy_signature_pair(self) -> Settings:
        if (self.imgproxy_key is None) != (self.imgproxy_salt is None):
            raise ValueError("imgproxy_key and imgproxy_salt must be configured together.")
        if self.imgproxy_key is not None and self.imgproxy_salt is not None:
            _validate_browser_reachable_imgproxy_base_url(self.imgproxy_render_base_url)
        return self

    @model_validator(mode="after")
    def _validate_pipeline_capacity_hysteresis(self) -> Settings:
        if self.pipeline_capacity_reopen_pending_count >= self.pipeline_capacity_close_pending_count:
            raise ValueError(
                "pipeline_capacity_reopen_pending_count must be below pipeline_capacity_close_pending_count."
            )
        if self.pipeline_capacity_reopen_oldest_age_seconds >= self.pipeline_capacity_close_oldest_age_seconds:
            raise ValueError(
                "pipeline_capacity_reopen_oldest_age_seconds must be below pipeline_capacity_close_oldest_age_seconds."
            )
        return self

    @field_validator("pipeline_operator_token", mode="before")
    @classmethod
    def _normalize_pipeline_operator_token(cls, value: object) -> object:
        if value is None:
            return value

        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value

        if not isinstance(raw_value, str):
            return value

        normalized_value = raw_value.strip()
        if not normalized_value:
            raise ValueError("pipeline_operator_token must not be blank.")
        return normalized_value

    @field_validator(
        "pipeline_broker_exchange",
        "pipeline_broker_routing_key_prefix",
        "pipeline_broker_media_inspect_queue",
        "pipeline_broker_source_engagement_capture_queue",
        "pipeline_broker_transcode_queue",
        "pipeline_broker_ocr_queue",
        "pipeline_broker_embed_queue",
        "pipeline_broker_classify_queue",
        "pipeline_broker_sync_qdrant_queue",
        "pipeline_broker_sync_meili_queue",
        "pipeline_broker_retry_exchange",
        "pipeline_broker_retry_queue",
        "pipeline_broker_dead_letter_exchange",
        "pipeline_broker_dead_letter_queue",
        mode="before",
    )
    @classmethod
    def _normalize_pipeline_broker_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("pipeline broker topology names must not be blank.")
        if _PIPELINE_TOPOLOGY_NAME_RE.fullmatch(normalized_value) is None:
            raise ValueError(
                "pipeline broker topology names may contain only letters, numbers, dots, underscores, and hyphens.",
            )
        return normalized_value

    @field_validator(
        "pipeline_s3_original_prefix",
        "pipeline_s3_temp_original_prefix",
        "pipeline_s3_derivative_prefix",
        mode="before",
    )
    @classmethod
    def _normalize_pipeline_object_prefix(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip().strip("/")
        if not normalized_value:
            raise ValueError("pipeline object-key prefixes must not be blank.")

        segments = tuple(segment for segment in normalized_value.split("/") if segment)
        if not segments or any(segment in {".", ".."} for segment in segments):
            raise ValueError("pipeline object-key prefixes must not contain empty, '.', or '..' path segments.")
        return "/".join(segments)

    @field_validator(
        "pipeline_ocr_primary_engine",
        "pipeline_ffmpeg_binary",
        "pipeline_ffprobe_binary",
        "pipeline_voyage_model",
        "pipeline_voyage_api_url",
        "pipeline_qdrant_collection_name",
        "pipeline_meilisearch_index_name",
        "pipeline_classification_model",
        "pipeline_seo_model",
        "pipeline_seo_prompt_version",
        mode="before",
    )
    @classmethod
    def _normalize_required_pipeline_runtime_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("pipeline runtime text settings must not be blank.")
        return normalized_value

    @field_validator("s3_bucket", mode="before")
    @classmethod
    def _normalize_s3_bucket(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip().lower()
        if not normalized_value:
            raise ValueError("s3_bucket must not be blank.")
        if len(normalized_value) < 3 or len(normalized_value) > 63:
            raise ValueError("s3_bucket must be between 3 and 63 characters long.")
        if _PIPELINE_BUCKET_NAME_RE.fullmatch(normalized_value) is None:
            raise ValueError("s3_bucket may contain only lowercase letters, numbers, dots, and hyphens.")
        if normalized_value.count(".") == 3 and all(part.isdigit() for part in normalized_value.split(".")):
            raise ValueError("s3_bucket must not be formatted like an IP address.")
        return normalized_value

    @field_validator("pipeline_allowed_mime_types", mode="before")
    @classmethod
    def _normalize_pipeline_allowed_mime_types(cls, value: object) -> object:
        if value is None:
            return value

        raw_mime_types = cls._coerce_env_sequence(value)
        normalized_mime_types = tuple(
            dict.fromkeys(mime_type.strip().lower() for mime_type in raw_mime_types if mime_type.strip())
        )
        if not normalized_mime_types:
            raise ValueError("pipeline_allowed_mime_types must include at least one MIME type.")
        if any(
            "/" not in mime_type or mime_type.startswith("/") or mime_type.endswith("/")
            for mime_type in normalized_mime_types
        ):
            raise ValueError("pipeline_allowed_mime_types must contain MIME types like image/jpeg.")
        return normalized_mime_types

    @field_validator(
        "pipeline_worker_fail_transcode_for_meme_file_id",
        "pipeline_worker_fail_embed_for_meme_file_id",
        "pipeline_worker_fail_classify_for_meme_file_id",
        "pipeline_worker_fail_sync_qdrant_for_meme_file_id",
        "pipeline_worker_fail_sync_meili_for_meme_file_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_pipeline_worker_uuid(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            return None

        try:
            uuid.UUID(normalized_value)
        except ValueError as exc:
            raise ValueError("pipeline worker failure-injection UUID settings must be a UUID.") from exc
        return normalized_value

    @field_validator("scheduler_advisory_lock_key", mode="before")
    @classmethod
    def _normalize_scheduler_advisory_lock_key(cls, value: object) -> object:
        if value is None:
            return value

        raw_parts = cls._coerce_env_sequence(value)
        if len(raw_parts) != 2:
            raise ValueError("scheduler_advisory_lock_key must contain exactly two integers.")

        try:
            normalized_key = tuple(int(part) for part in raw_parts)
        except ValueError as exc:
            raise ValueError("scheduler_advisory_lock_key must contain only integers.") from exc

        return normalized_key

    @field_validator("motd_algorithm_version", mode="before")
    @classmethod
    def _normalize_motd_algorithm_version(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("motd_algorithm_version must not be blank.")
        return normalized_value

    @field_validator("auth_telegram_bot_username", mode="before")
    @classmethod
    def _normalize_optional_bot_username(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        normalized_value = value.strip().lstrip("@")
        if not normalized_value:
            return None
        if " " in normalized_value:
            raise ValueError("auth_telegram_bot_username must not contain spaces.")
        return normalized_value

    @field_validator("auth_access_cookie_name", mode="before")
    @classmethod
    def _normalize_auth_access_cookie_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("auth_access_cookie_name must not be blank.")
        return normalized_value

    @field_validator("auth_access_cookie_path", mode="before")
    @classmethod
    def _normalize_auth_access_cookie_path(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("auth_access_cookie_path must not be blank.")
        if not normalized_value.startswith("/"):
            raise ValueError("auth_access_cookie_path must start with '/'.")
        return normalized_value

    @field_validator("auth_access_cookie_domain", mode="before")
    @classmethod
    def _normalize_auth_access_cookie_domain(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value

        normalized_value = value.strip()
        return normalized_value or None

    @field_validator(
        "auth_google_client_id",
        "auth_google_redirect_uri",
        "pipeline_ocr_paddle_command",
        "pipeline_ocr_fallback_engine",
        "pipeline_ocr_fallback_command",
        "pipeline_classification_api_url",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        return normalized_value or None

    @field_validator("auth_telegram_link_return_url", mode="before")
    @classmethod
    def _normalize_optional_url(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        return normalized_value or None

    @model_validator(mode="after")
    def _validate_auth_access_cookie_security(self) -> Settings:
        if self.auth_access_cookie_samesite == "none" and not self.auth_access_cookie_secure:
            raise ValueError("auth_access_cookie_samesite='none' requires auth_access_cookie_secure=true.")
        return self

    @field_validator("security_cors_allowed_origins", mode="before")
    @classmethod
    def _normalize_security_cors_allowed_origins(cls, value: object) -> object:
        if value is None:
            return value

        raw_origins = cls._coerce_env_sequence(value)
        try:
            validated_origins = _ALLOWED_ORIGIN_LIST_ADAPTER.validate_python(raw_origins)
        except ValidationError as exc:
            raise ValueError(
                "security_cors_allowed_origins must contain valid HTTP origins.",
            ) from exc

        normalized_origins = tuple(dict.fromkeys(str(origin).rstrip("/") for origin in validated_origins))
        if not normalized_origins:
            raise ValueError("security_cors_allowed_origins must include at least one origin.")
        return normalized_origins

    @field_validator("security_cors_allowed_methods", mode="before")
    @classmethod
    def _normalize_security_cors_allowed_methods(cls, value: object) -> object:
        if value is None:
            return value

        raw_methods = cls._coerce_env_sequence(value)
        normalized_methods = tuple(dict.fromkeys(method.strip().upper() for method in raw_methods if method.strip()))
        if not normalized_methods:
            raise ValueError("security_cors_allowed_methods must include at least one HTTP method.")
        return normalized_methods

    @field_validator("security_cors_allowed_headers", mode="before")
    @classmethod
    def _normalize_security_cors_allowed_headers(cls, value: object) -> object:
        if value is None:
            return value

        raw_headers = cls._coerce_env_sequence(value)
        normalized_headers = tuple(dict.fromkeys(header.strip() for header in raw_headers if header.strip()))
        if not normalized_headers:
            raise ValueError("security_cors_allowed_headers must include at least one header name.")
        return normalized_headers

    @field_validator("security_cors_allowed_origin_regex", "security_csrf_header_name", mode="before")
    @classmethod
    def _normalize_required_security_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("security text settings must not be blank.")
        return normalized_value

    @classmethod
    def _coerce_env_sequence(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            normalized_value = value.strip()
            if not normalized_value:
                raise ValueError("security sequence settings must not be blank.")

            if normalized_value.startswith("["):
                parsed_json = cast("object", json.loads(normalized_value))
                if not isinstance(parsed_json, list):
                    raise ValueError("security sequence settings JSON text must decode to a list.")
                normalized_items = tuple(str(item).strip() for item in parsed_json if str(item).strip())
            else:
                normalized_items = tuple(str(item).strip() for item in normalized_value.split(",") if str(item).strip())

            if not normalized_items:
                raise ValueError("security sequence settings must not be empty.")
            return normalized_items

        if isinstance(value, (list, tuple, set, frozenset)):
            normalized_items = tuple(str(item).strip() for item in value if str(item).strip())
            if not normalized_items:
                raise ValueError("security sequence settings must not be empty.")
            return normalized_items

        raise TypeError("security sequence settings must be provided as text or a sequence.")

    @property
    def imgproxy_render_base_url(self) -> str:
        """Return the public imgproxy URL base used in rendered image contracts."""

        return self.imgproxy_public_base_url or self.imgproxy_base_url

    @property
    def auth_access_token_ttl(self) -> timedelta:
        """Return the configured access-token lifetime as a timedelta."""

        return timedelta(seconds=self.auth_access_token_ttl_seconds)


def _validate_browser_reachable_imgproxy_base_url(value: str) -> None:
    error_message = (
        "IMGPROXY_PUBLIC_BASE_URL or IMGPROXY_BASE_URL must be an absolute browser-reachable http(s) URL "
        "when IMGPROXY_KEY and IMGPROXY_SALT are configured."
    )
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(error_message) from exc

    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError(error_message)

    hostname = parsed.hostname.strip().lower().rstrip(".")
    if not hostname:
        raise ValueError(error_message)

    if hostname == "localhost" or hostname.startswith("localhost.") or hostname.endswith(".localhost"):
        raise ValueError(error_message)

    try:
        host_ip = ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(labels) < 2 or any(not label or label == "*" for label in labels):
            raise ValueError(error_message) from None
        return

    if host_ip.is_loopback or host_ip.is_unspecified or host_ip.is_private or host_ip.is_link_local:
        raise ValueError(error_message)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for process-wide reuse."""

    return Settings()


__all__ = ["Settings", "get_settings"]
