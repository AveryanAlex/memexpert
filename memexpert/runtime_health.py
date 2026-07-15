"""Process-local progress heartbeat and container health-check command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from memexpert.core.config import Settings

HEALTH_SCHEMA_VERSION: Final = 1
DEFAULT_HEALTH_FILE: Final = Path("/tmp/memexpert-runtime-health.json")
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 10.0
DEFAULT_STALE_AFTER_SECONDS: Final = 45.0
DEFAULT_OPERATION_TIMEOUT_SECONDS: Final = 900.0
MAX_CLOCK_SKEW_SECONDS: Final = 5.0


class RuntimeHealthError(RuntimeError):
    """Raised when a process heartbeat is missing, malformed, or unhealthy."""


@dataclass(frozen=True, slots=True)
class RuntimeOperation:
    """One bounded operation currently owned by the runtime event loop."""

    name: str
    started_at: float
    deadline_at: float


@dataclass(slots=True)
class RuntimeHealthReporter:
    """Write an atomic heartbeat that proves the runtime event loop is progressing."""

    path: Path
    service: str
    role: str | None = None
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS
    boot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    pid: int = field(default_factory=os.getpid)
    _ready: bool = field(default=False, init=False)
    _last_progress_at: float = field(default_factory=time.time, init=False)
    _operations: dict[str, RuntimeOperation] = field(default_factory=dict, init=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        service: str,
        role: str | None = None,
    ) -> RuntimeHealthReporter:
        """Build a reporter from shared runtime-health settings."""

        return cls(
            path=settings.runtime_health_file,
            service=service,
            role=role,
            heartbeat_interval_seconds=settings.runtime_health_interval_seconds,
            operation_timeout_seconds=settings.runtime_health_operation_timeout_seconds,
        )

    async def start(self) -> None:
        """Publish the startup state and begin periodic event-loop heartbeats."""

        if self._heartbeat_task is not None:
            return
        self._write()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"{self.service}-runtime-health",
        )

    async def stop(self) -> None:
        """Publish a terminal not-ready state and stop the heartbeat task."""

        self._ready = False
        self._operations.clear()
        self._write()
        heartbeat_task = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    def mark_ready(self) -> None:
        """Mark startup complete and make the process eligible to become healthy."""

        self._ready = True
        self._last_progress_at = time.time()
        self._write()

    def mark_progress(self) -> None:
        """Record completion of useful control-loop or message work."""

        self._last_progress_at = time.time()

    @asynccontextmanager
    async def operation(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        """Track a bounded operation so an overdue but live loop becomes unhealthy."""

        resolved_timeout = timeout_seconds or self.operation_timeout_seconds
        if resolved_timeout <= 0:
            raise ValueError("runtime health operation timeout must be positive")
        operation_id = str(uuid.uuid4())
        started_at = time.time()
        self._operations[operation_id] = RuntimeOperation(
            name=name,
            started_at=started_at,
            deadline_at=started_at + resolved_timeout,
        )
        self._write()
        try:
            yield
        finally:
            self._operations.pop(operation_id, None)
            self.mark_progress()
            self._write()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            self._write()

    def _write(self) -> None:
        now = time.time()
        payload = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "service": self.service,
            "role": self.role,
            "boot_id": self.boot_id,
            "pid": self.pid,
            "ready": self._ready,
            "heartbeat_at": now,
            "last_progress_at": self._last_progress_at,
            "operations": [
                {
                    "name": operation.name,
                    "started_at": operation.started_at,
                    "deadline_at": operation.deadline_at,
                }
                for operation in sorted(self._operations.values(), key=lambda item: item.deadline_at)
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(payload, temporary_file, separators=(",", ":"), sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            temporary_path.chmod(0o600)
            temporary_path.replace(self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def check_runtime_health(
    path: Path,
    *,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    now: float | None = None,
    pid_is_alive: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Validate one heartbeat file and return its decoded payload."""

    if stale_after_seconds <= 0:
        raise ValueError("runtime health stale threshold must be positive")
    try:
        raw_payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeHealthError(f"runtime health file {path} is unavailable: {exc}") from exc
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise RuntimeHealthError(f"runtime health file {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeHealthError("runtime health payload must be a JSON object")
    if payload.get("schema_version") != HEALTH_SCHEMA_VERSION:
        raise RuntimeHealthError("runtime health payload has an unsupported schema version")
    if payload.get("ready") is not True:
        raise RuntimeHealthError("runtime has not completed startup readiness")

    checked_at = time.time() if now is None else now
    heartbeat_at = _require_number(payload, "heartbeat_at")
    if heartbeat_at > checked_at + MAX_CLOCK_SKEW_SECONDS:
        raise RuntimeHealthError("runtime heartbeat timestamp is in the future")
    heartbeat_age = checked_at - heartbeat_at
    if heartbeat_age > stale_after_seconds:
        raise RuntimeHealthError(
            f"runtime heartbeat is stale by {heartbeat_age:.1f}s (limit {stale_after_seconds:.1f}s)"
        )

    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeHealthError("runtime health payload has an invalid pid")
    resolved_pid_is_alive = pid_is_alive or _pid_is_alive
    if not resolved_pid_is_alive(pid):
        raise RuntimeHealthError(f"runtime process {pid} is not alive")

    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise RuntimeHealthError("runtime health payload has invalid operations")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise RuntimeHealthError(f"runtime operation {index} must be an object")
        operation_payload = cast("dict[str, Any]", operation)
        name = operation_payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeHealthError(f"runtime operation {index} has an invalid name")
        deadline_at = _require_number(operation_payload, "deadline_at", prefix=f"runtime operation {index}")
        if checked_at > deadline_at:
            raise RuntimeHealthError(f"runtime operation {name!r} exceeded its deadline")
    return payload


def _require_number(payload: dict[str, Any], key: str, *, prefix: str = "runtime health payload") -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeHealthError(f"{prefix} has an invalid {key}")
    return float(value)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> None:
    """Validate the local runtime heartbeat for Podman or Docker."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(os.environ.get("RUNTIME_HEALTH_FILE", DEFAULT_HEALTH_FILE)),
        help="heartbeat file written by the long-running process",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=float(os.environ.get("RUNTIME_HEALTH_STALE_AFTER_SECONDS", DEFAULT_STALE_AFTER_SECONDS)),
        help="maximum accepted heartbeat age",
    )
    args = parser.parse_args()
    try:
        payload = check_runtime_health(args.file, stale_after_seconds=args.stale_after_seconds)
    except (RuntimeHealthError, ValueError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    role_suffix = f" role={payload['role']}" if payload.get("role") is not None else ""
    print(f"healthy: service={payload['service']}{role_suffix} boot_id={payload['boot_id']}")


__all__ = [
    "DEFAULT_HEALTH_FILE",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_OPERATION_TIMEOUT_SECONDS",
    "DEFAULT_STALE_AFTER_SECONDS",
    "RuntimeHealthError",
    "RuntimeHealthReporter",
    "check_runtime_health",
    "main",
]


if __name__ == "__main__":
    main()
