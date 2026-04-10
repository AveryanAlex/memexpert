"""Application settings loaded from environment variables and .env files."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, ClassVar, Literal, cast

from pydantic import AnyHttpUrl, Field, SecretStr, TypeAdapter, ValidationError, field_validator
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
    pipeline_operator_token: SecretStr = SecretStr("memexpert-dev-pipeline-operator-token-min-32")
    pipeline_image_upload_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    pipeline_gif_upload_max_bytes: int = Field(default=40 * 1024 * 1024, gt=0)
    pipeline_video_upload_max_bytes: int = Field(default=150 * 1024 * 1024, gt=0)
    pipeline_allowed_mime_types: Annotated[tuple[str, ...], NoDecode] = PIPELINE_DEFAULT_ALLOWED_MIME_TYPES
    pipeline_phash_size: int = Field(default=16, ge=4, le=64)
    pipeline_image_max_pixels: int = Field(default=45_000_000, gt=0)
    pipeline_transcode_timeout_seconds: float = Field(default=45.0, gt=0.0, le=900.0)
    pipeline_ffmpeg_binary: str = "ffmpeg"
    pipeline_ffprobe_binary: str = "ffprobe"
    pipeline_s3_original_prefix: str = "pipeline/originals"
    pipeline_s3_derivative_prefix: str = "pipeline/derived"
    pipeline_broker_exchange: str = "memexpert.pipeline"
    pipeline_broker_routing_key_prefix: str = "pipeline"
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
    pipeline_storage_connection_timeout_seconds: float = Field(default=5.0, gt=0.0)
    pipeline_ocr_primary_engine: str = "paddleocr"
    pipeline_ocr_fallback_engine: str = "qwen2.5-vl-2b"
    pipeline_ocr_fallback_command: str | None = None
    pipeline_ocr_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    pipeline_ocr_low_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
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
    pipeline_merge_similarity_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    pipeline_classification_api_url: str | None = None
    pipeline_classification_api_key: SecretStr | None = None
    pipeline_classification_model: str = "memexpert-nsfw-v1"
    pipeline_classification_timeout_seconds: float = Field(default=15.0, gt=0.0, le=600.0)
    pipeline_classification_nsfw_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    pipeline_worker_fail_transcode_for_meme_file_id: str | None = None
    pipeline_worker_fail_embed_for_meme_file_id: str | None = None
    pipeline_worker_fail_classify_for_meme_file_id: str | None = None
    pipeline_worker_fail_sync_qdrant_for_meme_file_id: str | None = None
    pipeline_worker_fail_sync_meili_for_meme_file_id: str | None = None
    auth_jwt_secret: SecretStr = SecretStr("memexpert-dev-jwt-secret-with-32-byte-minimum")
    auth_access_token_algorithm: Literal["HS256"] = "HS256"
    auth_access_token_ttl_seconds: int = 900
    auth_refresh_token_ttl_days: int = 30
    auth_refresh_cookie_name: str = "memexpert_refresh_token"
    auth_refresh_cookie_secure: bool = True
    auth_refresh_cookie_httponly: bool = True
    auth_refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    auth_refresh_cookie_path: str = "/api/v1/auth/refresh"
    auth_refresh_cookie_domain: str | None = None
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

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("rabbitmq_url", "s3_endpoint", "s3_access_key", "s3_secret_key", "s3_region", mode="before")
    @classmethod
    def _normalize_required_runtime_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("runtime settings must not be blank.")
        return normalized_value

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

    @field_validator("pipeline_s3_original_prefix", "pipeline_s3_derivative_prefix", mode="before")
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
        "pipeline_ocr_fallback_engine",
        "pipeline_ffmpeg_binary",
        "pipeline_ffprobe_binary",
        "pipeline_voyage_model",
        "pipeline_voyage_api_url",
        "pipeline_qdrant_collection_name",
        "pipeline_meilisearch_index_name",
        "pipeline_classification_model",
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

    @field_validator(
        "auth_google_client_id",
        "auth_google_redirect_uri",
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
        normalized_methods = tuple(
            dict.fromkeys(method.strip().upper() for method in raw_methods if method.strip())
        )
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
    def auth_access_token_ttl(self) -> timedelta:
        """Return the configured access-token lifetime as a timedelta."""

        return timedelta(seconds=self.auth_access_token_ttl_seconds)

    @property
    def auth_refresh_token_ttl(self) -> timedelta:
        """Return the configured refresh-token lifetime as a timedelta."""

        return timedelta(days=self.auth_refresh_token_ttl_days)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for process-wide reuse."""

    return Settings()


__all__ = ["Settings", "get_settings"]
