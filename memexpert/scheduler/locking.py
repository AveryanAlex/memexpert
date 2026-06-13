"""Advisory-lock helpers for the scheduler runtime."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

SchedulerAdvisoryLockKey = tuple[int, int]


class SchedulerInstanceLockError(RuntimeError):
    """Raised when the scheduler cannot acquire its instance lock."""


class PostgresAdvisorySchedulerLock:
    """Acquire and release a PostgreSQL advisory lock for one scheduler instance."""

    def __init__(self, connection: Any, key: SchedulerAdvisoryLockKey) -> None:
        self._connection = connection
        self._key = key
        self._acquired = False

    async def acquire(self) -> None:
        result = await self._connection.execute(
            text("SELECT pg_try_advisory_lock(:key1, :key2)"),
            {"key1": self._key[0], "key2": self._key[1]},
        )
        if not result.scalar():
            raise SchedulerInstanceLockError("Unable to acquire scheduler advisory lock.")

        self._acquired = True

    async def release(self) -> None:
        if not self._acquired:
            return

        self._acquired = False
        await self._connection.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": self._key[0], "key2": self._key[1]},
        )

__all__ = [
    "PostgresAdvisorySchedulerLock",
    "SchedulerAdvisoryLockKey",
    "SchedulerInstanceLockError",
]
