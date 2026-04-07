"""Application settings loaded from environment variables and .env files."""

from __future__ import annotations

import json
from datetime import timedelta
from functools import lru_cache
from typing import ClassVar, Literal, cast

from pydantic import AnyHttpUrl, Field, SecretStr, TypeAdapter, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECURITY_DEFAULT_ALLOWED_ORIGINS = (
    "https://memexpert.net",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://web.telegram.org",
    "https://oauth.telegram.org",
)
SECURITY_DEFAULT_ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    "X-Requested-With",
)
_ALLOWED_ORIGIN_LIST_ADAPTER = TypeAdapter(tuple[AnyHttpUrl, ...])


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
    security_cors_allowed_origins: tuple[str, ...] = SECURITY_DEFAULT_ALLOWED_ORIGINS
    security_cors_allowed_origin_regex: str = r"^https://([a-z0-9-]+\.)?memexpert\.net$"
    security_cors_allowed_headers: tuple[str, ...] = SECURITY_DEFAULT_ALLOWED_HEADERS
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

    @field_validator("auth_google_client_id", "auth_google_redirect_uri", mode="before")
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
                parsed_value = cast("list[object]", json.loads(normalized_value))
            else:
                parsed_value = normalized_value.split(",")
            return tuple(str(item).strip() for item in parsed_value if str(item).strip())

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
