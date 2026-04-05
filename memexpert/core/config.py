"""Application settings loaded from environment variables and .env files."""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import ClassVar, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
