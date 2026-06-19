#!/usr/bin/env python3
"""One-time interactive Telegram session authorizer for curated crawler sessions.

Usage:

  Set ``TELEGRAM_API_ID`` and ``TELEGRAM_API_HASH`` in the environment (or
  the project's ``.env`` file) before running this script, then run::

      uv run python scripts/auth_telegram_session.py --session-name primary

  The script will prompt for a phone number, one-time password, and
  optional 2FA password, then upsert a ``telegram_sessions`` row
  with status ``active`` so the crawler runtime can pick the session up.

The script exits with status ``1`` if API credentials are missing so
operators cannot accidentally auth against a half-configured environment.
It deliberately has no tests: the flow is interactive and the work it
does is already covered by the runtime's session-loading unit
tests plus the :class:`TelegramSession` model contract.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

if TYPE_CHECKING:
    from pathlib import Path

from memexpert.core.config import get_settings
from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSession
from memexpert.models.enums import TelegramSessionStatus

_EXIT_MISSING_CREDENTIALS: Final = 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="auth_telegram_session",
        description=(
            "Interactively authorize a Telethon session file for the curated "
            "Telegram crawler and mark the session as active in the database."
        ),
    )
    parser.add_argument(
        "--session-name",
        required=True,
        help="Short identifier for this session (e.g. 'primary').",
    )
    return parser.parse_args()


async def _upsert_active_session_row(session_name: str) -> None:
    """Ensure ``telegram_sessions`` has an ``active`` row for ``session_name``."""

    settings = get_settings()
    engine = build_async_engine(settings.database_url)
    session_factory = build_async_session_factory(engine)
    try:
        async with session_factory() as db_session:
            result = await db_session.execute(
                select(TelegramSession)
                .where(TelegramSession.name == session_name)
                .limit(1)
            )
            row = result.scalar_one_or_none()
            now = utcnow()
            if row is None:
                row = TelegramSession(
                    id=uuid.uuid7(),
                    name=session_name,
                    display_name=session_name,
                    status=TelegramSessionStatus.ACTIVE,
                    last_heartbeat_at=now,
                )
                db_session.add(row)
            else:
                row.status = TelegramSessionStatus.ACTIVE
                row.last_heartbeat_at = now
                row.last_error_class = None
                row.last_error_text = None
                row.quarantined_at = None
                row.flood_wait_until = None
            await db_session.commit()
    finally:
        await engine.dispose()


async def _authorize_session_file(session_name: str, session_dir: Path) -> None:
    """Build a Telethon client and run its interactive ``start()`` flow."""

    # Importing Telethon inside the function keeps the CLI entry point
    # import-safe: ``--help`` must work even in environments without the
    # SDK installed (the project ships with it, but this keeps parity
    # with the crawler-client import boundary).
    from telethon import TelegramClient  # noqa: PLC0415

    settings = get_settings()
    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if api_id is None or api_hash is None:
        sys.stderr.write(
            "error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set before "
            "running this script.\n",
        )
        raise SystemExit(_EXIT_MISSING_CREDENTIALS)

    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = session_dir / f"{session_name}.session"
    client = TelegramClient(
        session=str(session_path),
        api_id=api_id,
        api_hash=api_hash.get_secret_value(),
    )
    async with client:
        start_result: object = client.start()
        if inspect.isawaitable(start_result):
            await start_result
        me = await client.get_me()
        sys.stdout.write(
            f"Authorized session {session_name!r} as {getattr(me, 'username', None) or me!r}.\n",
        )


async def _run(session_name: str) -> None:
    settings = get_settings()
    await _authorize_session_file(session_name, settings.telegram_session_dir)
    await _upsert_active_session_row(session_name)
    sys.stdout.write(
        f"Session {session_name!r} marked active in telegram_sessions.\n",
    )


def main() -> None:
    """Entry point used by ``python scripts/auth_telegram_session.py``."""

    args = _parse_args()
    asyncio.run(_run(args.session_name))


if __name__ == "__main__":
    main()
