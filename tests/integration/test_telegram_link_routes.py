"""Integration tests for guest-only Telegram deep-link issuance and code redemption."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from memexpert.api.app import create_app
from memexpert.core.config import get_settings
from memexpert.core.database import reset_async_database_state
from memexpert.models.base import utcnow
from memexpert.models.user import TelegramLinkCode
from memexpert.services import (
    AccountLinkInvariantError,
    AccountLinkService,
    AuthConfigurationError,
    AuthService,
    UserService,
)
from memexpert.services.provider_auth_service import TelegramIdentity

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_telegram_link_start_route_persists_short_hash_only_code_and_returns_deep_link(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_payload = guest_response.json()
    guest_access_token = guest_payload["access_token"]
    guest_user_id = uuid.UUID(guest_payload["user"]["id"])

    response = await auth_client.post(
        "/api/v1/auth/link/telegram",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )
    payload = response.json()
    start_parameter = f"link_{payload['code']}"

    assert response.status_code == 201
    assert 0 < len(payload["code"]) <= 59
    assert payload == {
        "code": payload["code"],
        "deep_link_url": f"https://t.me/{auth_settings_overrides['AUTH_TELEGRAM_BOT_USERNAME']}?start={start_parameter}",
        "expires_at": payload["expires_at"],
        "expires_in_seconds": int(auth_settings_overrides["AUTH_TELEGRAM_LINK_CODE_TTL_SECONDS"]),
        "return_url": auth_settings_overrides["AUTH_TELEGRAM_LINK_RETURN_URL"],
    }
    assert len(start_parameter) <= 64
    assert str(guest_user_id) not in payload["deep_link_url"]
    assert auth_settings_overrides["AUTH_TELEGRAM_LINK_RETURN_URL"] not in payload["deep_link_url"]

    async with postgres_session_factory() as session:
        persisted_rows_result = await session.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.guest_user_id == guest_user_id)
        )
        persisted_row = persisted_rows_result.scalar_one()

        assert persisted_row.code_hash == hashlib.sha256(payload["code"].encode("utf-8")).hexdigest()
        assert persisted_row.code_hash != payload["code"]
        assert persisted_row.expires_at > utcnow()
        assert persisted_row.redeemed_at is None
        assert persisted_row.redeemed_by_telegram_id is None


async def test_telegram_link_start_route_rejects_full_callers_without_persisting_code(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await user_service.create_full_user(email="already-full@example.com")
        auth_service = AuthService.from_settings(session)
        full_session = await auth_service.issue_session_for_user(full_user)

    response = await auth_client.post(
        "/api/v1/auth/link/telegram",
        headers={"Authorization": f"Bearer {full_session.access_token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "guest_account_required"

    async with postgres_session_factory() as session:
        telegram_link_count_result = await session.execute(select(func.count()).select_from(TelegramLinkCode))
        assert telegram_link_count_result.scalar_one() == 0


async def test_telegram_link_start_route_returns_typed_config_errors_before_persisting_codes(
    migrated_db_session: AsyncSession,
    postgres_async_url: str,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    _ = migrated_db_session
    base_env = {
        "DATABASE_URL": postgres_async_url,
        "AUTH_JWT_SECRET": "route-test-auth-secret-with-32-byte-minimum",
        "AUTH_REFRESH_COOKIE_NAME": "route_refresh_token",
        "AUTH_REFRESH_COOKIE_SAMESITE": "strict",
        "AUTH_REFRESH_COOKIE_SECURE": "true",
        "AUTH_TELEGRAM_BOT_USERNAME": "memexpertbot",
        "AUTH_TELEGRAM_LINK_RETURN_URL": "https://memexpert.test/link/telegram/complete",
    }

    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTH_TELEGRAM_BOT_USERNAME", raising=False)

    get_settings.cache_clear()
    await reset_async_database_state()
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        guest_response = await client.post("/api/v1/auth/guest")
        guest_access_token = guest_response.json()["access_token"]
        response = await client.post(
            "/api/v1/auth/link/telegram",
            headers={"Authorization": f"Bearer {guest_access_token}"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "provider_not_configured"

    async with postgres_session_factory() as session:
        telegram_link_count_result = await session.execute(select(func.count()).select_from(TelegramLinkCode))
        assert telegram_link_count_result.scalar_one() == 0

    monkeypatch.setenv("AUTH_TELEGRAM_BOT_USERNAME", "memexpertbot")
    monkeypatch.delenv("AUTH_TELEGRAM_LINK_RETURN_URL", raising=False)
    get_settings.cache_clear()
    await reset_async_database_state()
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        guest_response = await client.post("/api/v1/auth/guest")
        guest_access_token = guest_response.json()["access_token"]
        response = await client.post(
            "/api/v1/auth/link/telegram",
            headers={"Authorization": f"Bearer {guest_access_token}"},
        )

    await reset_async_database_state()
    get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json()["code"] == "auth_configuration_error"

    async with postgres_session_factory() as session:
        telegram_link_count_result = await session.execute(select(func.count()).select_from(TelegramLinkCode))
        assert telegram_link_count_result.scalar_one() == 0


async def test_telegram_link_start_route_surfaces_duplicate_hash_collisions_explicitly(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    forced_code = "collision_code_123"
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_access_token = guest_response.json()["access_token"]

    async with postgres_session_factory() as session:
        session.add(
            TelegramLinkCode(
                guest_user_id=uuid.uuid7(),
                code_hash=hashlib.sha256(forced_code.encode("utf-8")).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        await session.commit()

    monkeypatch.setattr(
        AccountLinkService,
        "_generate_telegram_link_code",
        staticmethod(lambda: forced_code),
    )

    response = await auth_client.post(
        "/api/v1/auth/link/telegram",
        headers={"Authorization": f"Bearer {guest_access_token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "account_link_invariant_error"
    assert "collision" in response.json()["detail"].lower()

    async with postgres_session_factory() as session:
        telegram_link_count_result = await session.execute(select(func.count()).select_from(TelegramLinkCode))
        assert telegram_link_count_result.scalar_one() == 1


async def test_account_link_service_rejects_invalid_telegram_link_ttl_before_issuance(
    migrated_db_session: AsyncSession,
) -> None:
    with pytest.raises(AuthConfigurationError, match="greater than zero"):
        _ = AccountLinkService(
            migrated_db_session,
            telegram_link_bot_username="memexpertbot",
            telegram_link_code_ttl_seconds=0,
            telegram_link_return_url="https://memexpert.test/link/telegram/complete",
        )


async def test_account_link_service_redeem_rejects_expired_and_replayed_codes_explicitly(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    guest_user = await user_service.create_guest_user()
    service = AccountLinkService(
        migrated_db_session,
        telegram_link_bot_username="memexpertbot",
        telegram_link_code_ttl_seconds=600,
        telegram_link_return_url="https://memexpert.test/link/telegram/complete",
    )
    identity = TelegramIdentity(telegram_id=987654321, auth_date=utcnow())

    expired_code = "expired_code_123"
    replayed_code = "replayed_code_123"
    migrated_db_session.add_all(
        [
            TelegramLinkCode(
                guest_user_id=guest_user.id,
                code_hash=hashlib.sha256(expired_code.encode("utf-8")).hexdigest(),
                expires_at=utcnow() - timedelta(minutes=1),
            ),
            TelegramLinkCode(
                guest_user_id=guest_user.id,
                code_hash=hashlib.sha256(replayed_code.encode("utf-8")).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=10),
                redeemed_at=utcnow(),
                redeemed_by_telegram_id=123456789,
            ),
        ]
    )
    await migrated_db_session.commit()

    with pytest.raises(AccountLinkInvariantError, match="expired"):
        _ = await service.redeem_telegram_link_code(code=expired_code, identity=identity)

    with pytest.raises(AccountLinkInvariantError, match="already been redeemed"):
        _ = await service.redeem_telegram_link_code(code=replayed_code, identity=identity)


async def test_account_link_service_redeem_rejects_unknown_codes_explicitly(
    migrated_db_session: AsyncSession,
) -> None:
    service = AccountLinkService(
        migrated_db_session,
        telegram_link_bot_username="memexpertbot",
        telegram_link_code_ttl_seconds=600,
        telegram_link_return_url="https://memexpert.test/link/telegram/complete",
    )

    with pytest.raises(AccountLinkInvariantError, match="invalid"):
        _ = await service.redeem_telegram_link_code(
            code="unknown_code_123",
            identity=TelegramIdentity(telegram_id=123123123, auth_date=utcnow()),
        )
