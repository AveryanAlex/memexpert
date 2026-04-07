"""Tests for configuration loading, validation, and caching."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import RedisConfigurationError, normalize_redis_url

if TYPE_CHECKING:
    from pathlib import Path


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
    assert settings.auth_refresh_cookie_path == "/api/v1/auth/refresh"


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


def test_get_settings_returns_cached_instance() -> None:
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()

    assert first is second
