"""Database engine resource and identity configuration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import memexpert.core.database as database_module
from memexpert.core.config import Settings
from memexpert.core.database import build_async_engine

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncEngine


def test_build_async_engine_applies_pool_limits_and_application_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    expected_engine = cast("AsyncEngine", object())

    def fake_create_async_engine(database_url: str, **kwargs: object) -> AsyncEngine:
        captured["database_url"] = database_url
        captured.update(kwargs)
        return expected_engine

    monkeypatch.setattr(database_module, "create_async_engine", fake_create_async_engine)
    settings = Settings(
        database_url="postgresql://worker:secret@db:5432/memexpert",
        database_connect_timeout_seconds=3.0,
        database_pool_size=2,
        database_max_overflow=1,
        database_pool_timeout_seconds=4.0,
        database_application_name="memexpert-worker-ocr",
    )

    engine = build_async_engine(settings=settings)

    assert engine is expected_engine
    assert captured["database_url"] == "postgresql+asyncpg://worker:secret@db:5432/memexpert"
    assert captured["pool_size"] == 2
    assert captured["max_overflow"] == 1
    assert captured["pool_timeout"] == 4.0
    assert captured["connect_args"] == {
        "timeout": 3.0,
        "server_settings": {"application_name": "memexpert-worker-ocr"},
    }


def test_explicit_connect_args_preserve_caller_application_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create_async_engine(_database_url: str, **kwargs: object) -> AsyncEngine:
        captured.update(kwargs)
        return cast("AsyncEngine", object())

    monkeypatch.setattr(database_module, "create_async_engine", fake_create_async_engine)

    _ = build_async_engine(
        "postgresql+asyncpg://worker:secret@db:5432/memexpert",
        application_name="default-name",
        connect_args={"server_settings": {"application_name": "caller-name"}},
    )

    assert captured["connect_args"] == {
        "timeout": 5.0,
        "server_settings": {"application_name": "caller-name"},
    }
