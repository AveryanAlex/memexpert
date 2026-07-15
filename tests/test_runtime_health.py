"""Functional tests for process-local runtime heartbeat reporting."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from memexpert.runtime_health import RuntimeHealthError, RuntimeHealthReporter, check_runtime_health

if TYPE_CHECKING:
    from pathlib import Path


async def test_runtime_health_reporter_becomes_ready_and_keeps_heartbeating(tmp_path: Path) -> None:
    health_file = tmp_path / "runtime-health.json"
    reporter = RuntimeHealthReporter(
        path=health_file,
        service="memexpert-workers",
        role="ocr",
        heartbeat_interval_seconds=0.01,
    )

    await reporter.start()
    try:
        with pytest.raises(RuntimeHealthError, match="startup readiness"):
            _ = check_runtime_health(health_file, pid_is_alive=lambda _pid: True)

        reporter.mark_ready()
        first = check_runtime_health(health_file, pid_is_alive=lambda _pid: True)
        await asyncio.sleep(0.03)
        second = check_runtime_health(health_file, pid_is_alive=lambda _pid: True)

        assert first["role"] == "ocr"
        assert cast("float", second["heartbeat_at"]) > cast("float", first["heartbeat_at"])
    finally:
        await reporter.stop()


async def test_runtime_health_fails_an_overdue_tracked_operation(tmp_path: Path) -> None:
    health_file = tmp_path / "runtime-health.json"
    reporter = RuntimeHealthReporter(path=health_file, service="worker", operation_timeout_seconds=10.0)
    await reporter.start()
    reporter.mark_ready()
    try:
        async with reporter.operation("ocr", timeout_seconds=2.0):
            payload = json.loads(health_file.read_text(encoding="utf-8"))
            deadline_at = payload["operations"][0]["deadline_at"]
            with pytest.raises(RuntimeHealthError, match="exceeded its deadline"):
                _ = check_runtime_health(
                    health_file,
                    now=deadline_at + 0.1,
                    stale_after_seconds=10.0,
                    pid_is_alive=lambda _pid: True,
                )
    finally:
        await reporter.stop()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(heartbeat_at=1.0), "heartbeat is stale"),
        (lambda payload: payload.update(pid=-1), "invalid pid"),
        (lambda payload: payload.update(schema_version=999), "schema version"),
    ],
)
def test_runtime_health_rejects_malformed_or_stale_state(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    health_file = tmp_path / "runtime-health.json"
    payload = {
        "schema_version": 1,
        "service": "worker",
        "role": "sync",
        "boot_id": "boot",
        "pid": 123,
        "ready": True,
        "heartbeat_at": 100.0,
        "last_progress_at": 100.0,
        "operations": [],
    }
    mutate(payload)
    health_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeHealthError, match=message):
        _ = check_runtime_health(
            health_file,
            now=100.0,
            stale_after_seconds=10.0,
            pid_is_alive=lambda _pid: True,
        )


def test_runtime_health_rejects_dead_process(tmp_path: Path) -> None:
    health_file = tmp_path / "runtime-health.json"
    health_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": "worker",
                "role": "media",
                "boot_id": "boot",
                "pid": 123,
                "ready": True,
                "heartbeat_at": 100.0,
                "last_progress_at": 100.0,
                "operations": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeHealthError, match="not alive"):
        _ = check_runtime_health(
            health_file,
            now=100.0,
            pid_is_alive=lambda _pid: False,
        )
