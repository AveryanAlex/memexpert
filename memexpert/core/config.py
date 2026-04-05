"""Application settings loaded from environment variables and .env files."""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar

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

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for process-wide reuse."""

    return Settings()


__all__ = ["Settings", "get_settings"]
