"""Tests for the plain argparse admin-user management script."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memexpert.models.user import User
from memexpert.services import UserService
from scripts import admin_users
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from pytest import CaptureFixture, MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_admin_users_cli_grants_lists_and_revokes_admins(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    user = await create_full_user_via_upgrade(UserService(migrated_db_session), email="cli-admin@example.com")
    monkeypatch.setattr(admin_users, "get_async_session_factory", lambda: postgres_session_factory)

    grant_status = await admin_users.run(["grant", str(user.id)])
    list_status = await admin_users.run(["list"])
    revoke_status = await admin_users.run(["revoke", str(user.id)])

    output = capsys.readouterr().out
    assert grant_status == 0
    assert list_status == 0
    assert revoke_status == 0
    assert f"granted {user.id}" in output
    assert f"{user.id}\tcli-admin@example.com" in output
    assert f"revoked {user.id}" in output

    async with postgres_session_factory() as session:
        persisted = await session.get(User, user.id)
        assert persisted is not None
        assert persisted.is_admin is False
