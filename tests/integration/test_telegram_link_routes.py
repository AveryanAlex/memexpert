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
from tests.conftest import create_full_user_via_upgrade
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_telegram_link_start_route_persists_short_hash_only_code_and_returns_deep_link(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    assert guest_response.status_code == 201
    guest_user_id = uuid.UUID(guest_response.json()["user"]["id"])

    response = await auth_client.post("/api/v1/auth/link/telegram")
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


async def test_telegram_link_in_place_upgrade_is_visible_on_next_session_read(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]
    start_response = await auth_client.post("/api/v1/auth/link/telegram")
    code = start_response.json()["code"]

    async with postgres_session_factory() as session:
        link_service = AccountLinkService.from_settings(session)
        link_result = await link_service.redeem_telegram_link_code(
            code=code,
            identity=TelegramIdentity(telegram_id=2026061201, auth_date=utcnow()),
        )

    assert str(link_result.user.id) == guest_user_id
    assert link_result.merge_performed is False
    assert link_result.user.account_type.value == "full"

    current_response = await auth_client.get("/api/v1/auth/session")
    payload = current_response.json()

    assert current_response.status_code == 200
    assert payload["user"]["id"] == guest_user_id
    assert payload["user"]["account_type"] == "full"
    assert payload["linked_providers"]["telegram_linked"] is True
    assert "access_token" not in payload


async def test_telegram_link_merge_self_heals_stale_guest_cookie_on_session_read(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_id = 2026061202
    async with postgres_session_factory() as session:
        full_user = await create_full_user_via_upgrade(
            UserService(session),
            telegram_id=telegram_id,
        )
        full_user_id = str(full_user.id)

    _ = await auth_client.post("/api/v1/auth/guest")
    start_response = await auth_client.post("/api/v1/auth/link/telegram")
    code = start_response.json()["code"]

    async with postgres_session_factory() as session:
        link_service = AccountLinkService.from_settings(session)
        link_result = await link_service.redeem_telegram_link_code(
            code=code,
            identity=TelegramIdentity(telegram_id=telegram_id, auth_date=utcnow()),
        )

    assert str(link_result.user.id) == full_user_id
    assert link_result.merge_performed is True
    assert link_result.deleted_guest_user_id is not None

    session_response = await auth_client.get("/api/v1/auth/session")
    payload = session_response.json()

    assert session_response.status_code == 200
    assert payload["user"]["id"] == full_user_id
    assert payload["user"]["account_type"] == "full"
    assert payload["linked_providers"]["telegram_linked"] is True
    assert f"{ACCESS_COOKIE_NAME}=" in session_response.headers["set-cookie"]
    assert auth_client.cookies.get(ACCESS_COOKIE_NAME) is not None

    second_response = await auth_client.get("/api/v1/auth/session")
    assert second_response.status_code == 200
    assert second_response.json()["user"]["id"] == full_user_id
    assert "set-cookie" not in second_response.headers


async def test_telegram_link_merge_self_heals_stale_guest_cookie_for_current_user_dependency(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_id = 2026061203
    async with postgres_session_factory() as session:
        full_user = await create_full_user_via_upgrade(
            UserService(session),
            telegram_id=telegram_id,
        )
        full_user_id = str(full_user.id)

    _ = await auth_client.post("/api/v1/auth/guest")
    start_response = await auth_client.post("/api/v1/auth/link/telegram")
    code = start_response.json()["code"]

    async with postgres_session_factory() as session:
        link_service = AccountLinkService.from_settings(session)
        link_result = await link_service.redeem_telegram_link_code(
            code=code,
            identity=TelegramIdentity(telegram_id=telegram_id, auth_date=utcnow()),
        )

    assert link_result.merge_performed is True

    me_response = await auth_client.get("/api/v1/auth/me")
    payload = me_response.json()

    assert me_response.status_code == 200
    assert payload["id"] == full_user_id
    assert payload["account_type"] == "full"
    assert f"{ACCESS_COOKIE_NAME}=" in me_response.headers["set-cookie"]


async def test_session_refresh_route_is_not_registered(auth_client: AsyncClient) -> None:
    legacy_path = "/api/v1/auth/" + "/".join(("session", "refresh"))
    response = await auth_client.post(legacy_path)

    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


async def test_telegram_link_start_route_rejects_full_callers_without_persisting_code(
    auth_client: AsyncClient,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        user_service = UserService(session)
        full_user = await create_full_user_via_upgrade(user_service, email="already-full@example.com")
        auth_service = AuthService.from_settings(session)
        full_session = await auth_service.issue_session_for_user(full_user)

    auth_client.cookies.set("memexpert_access_token", full_session.access_token)
    response = await auth_client.post("/api/v1/auth/link/telegram")

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
        "SECURITY_RATE_LIMIT_ENABLED": "false",
        "AUTH_TELEGRAM_BOT_USERNAME": "memexpertbot",
        "AUTH_TELEGRAM_LINK_RETURN_URL": "https://memexpert.test/account/telegram/complete",
    }

    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTH_TELEGRAM_BOT_USERNAME", raising=False)

    get_settings.cache_clear()
    await reset_async_database_state()
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        _ = await client.post("/api/v1/auth/guest")
        response = await client.post("/api/v1/auth/link/telegram")

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
        _ = await client.post("/api/v1/auth/guest")
        response = await client.post("/api/v1/auth/link/telegram")

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
    _ = await auth_client.post("/api/v1/auth/guest")

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

    response = await auth_client.post("/api/v1/auth/link/telegram")

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
            telegram_link_return_url="https://memexpert.test/account/telegram/complete",
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
        telegram_link_return_url="https://memexpert.test/account/telegram/complete",
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
        telegram_link_return_url="https://memexpert.test/account/telegram/complete",
    )

    with pytest.raises(AccountLinkInvariantError, match="invalid"):
        _ = await service.redeem_telegram_link_code(
            code="unknown_code_123",
            identity=TelegramIdentity(telegram_id=123123123, auth_date=utcnow()),
        )
