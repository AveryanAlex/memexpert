"""Tests for configuration loading and caching."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memexpert.core.config import Settings, get_settings

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


def test_get_settings_returns_cached_instance() -> None:
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()

    assert first is second
