"""Real-PostgreSQL coverage for the scheduler's session advisory lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from memexpert.scheduler.locking import PostgresAdvisorySchedulerLock, SchedulerInstanceLockError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@pytest.mark.asyncio
async def test_scheduler_lock_does_not_pin_a_postgres_transaction(
    postgres_async_engine: AsyncEngine,
) -> None:
    key = (1_298_362_451, 1_815_167_091)
    async with (
        postgres_async_engine.connect() as lock_connection,
        postgres_async_engine.connect() as observer_connection,
        postgres_async_engine.connect() as contender_connection,
    ):
        backend_pid = int(await lock_connection.scalar(text("SELECT pg_backend_pid()")))
        await lock_connection.commit()
        lock = PostgresAdvisorySchedulerLock(lock_connection, key)
        contender = PostgresAdvisorySchedulerLock(contender_connection, key)

        await lock.acquire()

        assert not lock_connection.in_transaction()
        assert await _transaction_state(observer_connection, backend_pid) == ("idle", None, None)
        with pytest.raises(SchedulerInstanceLockError):
            await contender.acquire()
        assert not contender_connection.in_transaction()

        await lock.release()

        assert not lock_connection.in_transaction()
        assert await _transaction_state(observer_connection, backend_pid) == ("idle", None, None)
        await contender.acquire()
        assert not contender_connection.in_transaction()
        await contender.release()
        assert not contender_connection.in_transaction()


async def _transaction_state(
    observer_connection: AsyncConnection,
    backend_pid: int,
) -> tuple[str, object, object]:
    row = (
        await observer_connection.execute(
            text(
                """
                SELECT state, xact_start, backend_xmin
                FROM pg_stat_activity
                WHERE pid = :backend_pid
                """
            ),
            {"backend_pid": backend_pid},
        )
    ).one()
    await observer_connection.commit()
    return str(row.state), row.xact_start, row.backend_xmin
