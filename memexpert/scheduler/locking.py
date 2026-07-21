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
        try:
            result = await self._connection.execute(
                text("SELECT pg_try_advisory_lock(:key1, :key2)"),
                {"key1": self._key[0], "key2": self._key[1]},
            )
            self._acquired = bool(result.scalar())
            # This is a session-level lock, so committing clears autobegin's
            # transaction and backend_xmin without releasing the lock.
            await self._connection.commit()
        except BaseException:
            await self._connection.rollback()
            raise

        if not self._acquired:
            raise SchedulerInstanceLockError("Unable to acquire scheduler advisory lock.")

    async def release(self) -> None:
        if not self._acquired:
            return

        try:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:key1, :key2)"),
                {"key1": self._key[0], "key2": self._key[1]},
            )
            await self._connection.commit()
        except BaseException:
            await self._connection.rollback()
            raise

        self._acquired = False

__all__ = [
    "PostgresAdvisorySchedulerLock",
    "SchedulerAdvisoryLockKey",
    "SchedulerInstanceLockError",
]
