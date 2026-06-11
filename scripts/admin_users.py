"""Grant, revoke, and list MemeXpert admins by user UUID."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.core.database import get_async_session_factory
from memexpert.models.user import User

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage MemeXpert admin users.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    grant_parser = subparsers.add_parser("grant", help="Grant admin access to a user UUID.")
    grant_parser.add_argument("user_id", type=_parse_user_id)

    revoke_parser = subparsers.add_parser("revoke", help="Revoke admin access from a user UUID.")
    revoke_parser.add_argument("user_id", type=_parse_user_id)

    _ = subparsers.add_parser("list", help="List current admin users.")
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session_factory = get_async_session_factory()

    async with session_factory() as session:
        if args.command == "grant":
            user = await session.get(User, args.user_id)
            if user is None:
                print(f"User {args.user_id} does not exist.", file=sys.stderr)
                return 1
            user.is_admin = True
            await session.commit()
            print(f"granted {user.id}")
            return 0

        if args.command == "revoke":
            user = await session.get(User, args.user_id)
            if user is None:
                print(f"User {args.user_id} does not exist.", file=sys.stderr)
                return 1
            user.is_admin = False
            await session.commit()
            print(f"revoked {user.id}")
            return 0

        result = await session.execute(select(User).where(User.is_admin.is_(True)).order_by(User.created_at.asc()))
        for user in result.scalars().all():
            label = user.email or str(user.telegram_id or user.google_id or "no_identity")
            print(f"{user.id}\t{label}")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


def _parse_user_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid UUID") from exc


if __name__ == "__main__":
    raise SystemExit(main())
