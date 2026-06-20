#!/usr/bin/env python3
"""Import an existing Telethon StringSession into the Telegram session registry.

Examples:

    uv run python scripts/auth_telegram_session.py --session-name primary \
        --string-session-file /run/secrets/telegram_string_session

    TELEGRAM_STRING_SESSION=... uv run python scripts/auth_telegram_session.py \
        --session-name primary --display-name "Primary crawler"

The helper validates the provided StringSession against Telegram, then writes
only encrypted StringSession material plus account projection fields to the
``telegram_sessions`` table. It never creates Telethon filesystem sessions and
never prints the provided StringSession value.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
import uuid
from pathlib import Path
from typing import Final

from pydantic import SecretStr
from sqlalchemy import select

from memexpert.core.config import get_settings
from memexpert.core.database import build_async_engine, build_async_session_factory
from memexpert.crawlers.telegram.session_crypto import TelegramStringSessionCipher
from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSession
from memexpert.models.enums import TelegramSessionStatus

_EXIT_MISSING_CREDENTIALS: Final = 1
_EXIT_MISSING_STRING_SESSION: Final = 2
_EXIT_INVALID_STRING_SESSION: Final = 3
_EXIT_INVALID_INPUT: Final = 4


class _ValidatedTelegramAccount:
    def __init__(
        self,
        *,
        user_id: int | None,
        username: str | None,
        phone_hint: str | None,
    ) -> None:
        self.user_id = user_id
        self.username = username
        self.phone_hint = phone_hint


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="auth_telegram_session",
        description="Validate and import a Telethon StringSession into telegram_sessions.",
    )
    parser.add_argument(
        "--session-name",
        required=True,
        help="Short registry identifier for this session (for example, 'primary').",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Human-readable display name. Defaults to the existing display name or session name.",
    )
    parser.add_argument(
        "--string-session",
        default=None,
        help="Existing Telethon StringSession value. Prefer env/file mechanisms in shared shells.",
    )
    parser.add_argument(
        "--string-session-file",
        type=Path,
        default=None,
        help="Path to a text file containing the existing Telethon StringSession value.",
    )
    return parser.parse_args()


def _normalize_required_text(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        sys.stderr.write(f"error: {label} must not be blank.\n")
        raise SystemExit(_EXIT_INVALID_INPUT)
    return normalized


def _load_string_session(args: argparse.Namespace) -> SecretStr:
    provided_sources = [
        source
        for source in (
            args.string_session,
            args.string_session_file,
            os.environ.get("TELEGRAM_STRING_SESSION"),
        )
        if source is not None
    ]
    if len(provided_sources) > 1:
        sys.stderr.write(
            "error: provide StringSession material through exactly one of "
            "--string-session, --string-session-file, or TELEGRAM_STRING_SESSION.\n",
        )
        raise SystemExit(_EXIT_INVALID_INPUT)
    if args.string_session is not None:
        raw_value = args.string_session
    elif args.string_session_file is not None:
        try:
            raw_value = args.string_session_file.read_text(encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"error: unable to read --string-session-file ({type(exc).__name__}).\n")
            raise SystemExit(_EXIT_INVALID_INPUT) from exc
    else:
        raw_value = os.environ.get("TELEGRAM_STRING_SESSION")
    if raw_value is None or not raw_value.strip():
        sys.stderr.write(
            "error: provide an existing Telethon StringSession via --string-session, "
            "--string-session-file, or TELEGRAM_STRING_SESSION.\n",
        )
        raise SystemExit(_EXIT_MISSING_STRING_SESSION)
    return SecretStr(raw_value.strip())


async def _validate_string_session(string_session: SecretStr) -> _ValidatedTelegramAccount:
    settings = get_settings()
    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if api_id is None or api_hash is None:
        sys.stderr.write(
            "error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set before importing a session.\n",
        )
        raise SystemExit(_EXIT_MISSING_CREDENTIALS)

    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    try:
        client = TelegramClient(
            StringSession(string_session.get_secret_value()),
            api_id,
            api_hash.get_secret_value(),
        )
    except Exception:
        sys.stderr.write("error: provided Telegram StringSession is invalid.\n")
        raise SystemExit(_EXIT_INVALID_STRING_SESSION) from None

    try:
        await client.connect()
        if not await client.is_user_authorized():
            sys.stderr.write("error: provided Telegram StringSession is not authorized.\n")
            raise SystemExit(_EXIT_INVALID_STRING_SESSION)
        me = await client.get_me()
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(f"error: unable to validate Telegram StringSession ({type(exc).__name__}).\n")
        raise SystemExit(_EXIT_INVALID_STRING_SESSION) from exc
    finally:
        disconnect_result = client.disconnect()
        if inspect.isawaitable(disconnect_result):
            await disconnect_result

    user_id = getattr(me, "id", None)
    username = getattr(me, "username", None)
    phone = getattr(me, "phone", None)
    return _ValidatedTelegramAccount(
        user_id=user_id if isinstance(user_id, int) else None,
        username=username.strip() if isinstance(username, str) and username.strip() else None,
        phone_hint=_phone_hint(phone if isinstance(phone, str) else None),
    )


async def _upsert_session_row(
    *,
    session_name: str,
    display_name: str | None,
    string_session: SecretStr,
    account: _ValidatedTelegramAccount,
) -> None:
    settings = get_settings()
    encrypted_string_session = TelegramStringSessionCipher(settings.telegram_session_encryption_secret).encrypt(
        string_session,
    )
    engine = build_async_engine(settings.database_url)
    session_factory = build_async_session_factory(engine)
    try:
        async with session_factory() as db_session:
            row = await db_session.scalar(
                select(TelegramSession)
                .where(TelegramSession.name == session_name)
                .limit(1),
            )
            now = utcnow()
            if row is None:
                row = TelegramSession(
                    id=uuid.uuid7(),
                    name=session_name,
                    display_name=display_name or session_name,
                    status=TelegramSessionStatus.ACTIVE,
                    enabled=True,
                    encrypted_string_session=encrypted_string_session.get_secret_value(),
                    account_user_id=account.user_id,
                    account_username=account.username,
                    account_phone_hint=account.phone_hint,
                    last_heartbeat_at=now,
                )
                db_session.add(row)
            else:
                row.display_name = display_name or row.display_name or session_name
                row.status = TelegramSessionStatus.ACTIVE
                row.enabled = True
                row.encrypted_string_session = encrypted_string_session.get_secret_value()
                row.account_user_id = account.user_id
                row.account_username = account.username
                row.account_phone_hint = account.phone_hint
                row.last_heartbeat_at = now
                row.last_error_class = None
                row.last_error_text = None
                row.quarantined_at = None
                row.flood_wait_until = None
            await db_session.commit()
    finally:
        await engine.dispose()


def _phone_hint(phone: str | None) -> str | None:
    if phone is None:
        return None
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 4:
        return None
    return f"ending-{digits[-4:]}"


async def _run(args: argparse.Namespace) -> None:
    session_name = _normalize_required_text(args.session_name, label="session-name")
    display_name = (
        None
        if args.display_name is None
        else _normalize_required_text(args.display_name, label="display-name")
    )
    string_session = _load_string_session(args)
    account = await _validate_string_session(string_session)
    await _upsert_session_row(
        session_name=session_name,
        display_name=display_name,
        string_session=string_session,
        account=account,
    )
    account_label = f" user_id={account.user_id}" if account.user_id is not None else ""
    username_label = f" username=@{account.username}" if account.username else ""
    sys.stdout.write(
        f"Imported Telegram session {session_name!r}{account_label}{username_label}.\n",
    )


def main() -> None:
    """Entry point used by ``python scripts/auth_telegram_session.py``."""

    asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    main()
