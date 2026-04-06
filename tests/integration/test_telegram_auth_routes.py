"""Integration tests for FastAPI Telegram auth routes."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from memexpert.api.app import create_app
from memexpert.core.config import get_settings
from memexpert.core.database import reset_async_database_state
from memexpert.models.user import RefreshToken, User

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def sign_telegram_widget_payload(
    payload: dict[str, object],
    *,
    token: str,
) -> dict[str, object]:
    signed_fields = {key: value for key, value in payload.items() if value is not None}
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    data_check_string = "\n".join(f"{key}={signed_fields[key]}" for key in sorted(signed_fields))
    signature = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**signed_fields, "hash": signature}


def build_miniapp_init_data(
    *,
    telegram_id: int,
    token: str,
    auth_date: int | None = None,
    **overrides: object,
) -> str:
    user_payload: dict[str, object] = {
        "id": telegram_id,
        "first_name": "Alice",
        "username": "alice_memexpert",
    }
    raw_user_overrides = overrides.pop("user", None)
    if raw_user_overrides is not None:
        if not isinstance(raw_user_overrides, dict):
            raise TypeError("Mini App user overrides must be a mapping.")
        typed_user_overrides: dict[str, object] = {
            str(key): value for key, value in raw_user_overrides.items()
        }
        user_payload.update(typed_user_overrides)

    fields: dict[str, str] = {
        "auth_date": str(auth_date if auth_date is not None else int(datetime.now(UTC).timestamp())),
        "user": json.dumps(user_payload, separators=(",", ":"), ensure_ascii=False),
    }
    for key, value in overrides.items():
        if isinstance(value, dict | list):
            fields[key] = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        else:
            fields[key] = str(value)

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def test_telegram_widget_route_sets_refresh_cookie_and_returns_shared_session_contract(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        headers={"User-Agent": "Telegram Widget Browser"},
        json=sign_telegram_widget_payload(
            {
                "id": 111222333,
                "first_name": "Alice",
                "username": "alice_memexpert",
                "auth_date": int(datetime.now(UTC).timestamp()),
            },
            token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
        ),
    )

    cookie_name = auth_settings_overrides["AUTH_REFRESH_COOKIE_NAME"]
    payload = response.json()

    assert response.status_code == 200
    assert payload["user"]["account_type"] == "full"
    assert payload["user"]["telegram_id"] == 111222333
    assert payload["user"]["email"] is None
    assert payload["refresh_cookie"]["name"] == cookie_name
    assert auth_client.cookies.get(cookie_name) is not None

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.telegram_id == 111222333))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.email is None

        refresh_token_result = await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == persisted_user.id)
        )
        refresh_token_row = refresh_token_result.scalar_one()
        assert refresh_token_row.device_info == "Telegram Widget Browser"
        assert refresh_token_row.token_hash != auth_client.cookies.get(cookie_name)


async def test_telegram_miniapp_route_reuses_existing_telegram_account_across_surfaces(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    widget_response = await auth_client.post(
        "/api/v1/auth/telegram",
        json=sign_telegram_widget_payload(
            {
                "id": 444555666,
                "first_name": "Alice",
                "username": "alice_memexpert",
                "auth_date": int(datetime.now(UTC).timestamp()),
            },
            token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
        ),
    )
    widget_user_id = widget_response.json()["user"]["id"]

    miniapp_response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        headers={"User-Agent": "Telegram Mini App"},
        json={
            "initData": build_miniapp_init_data(
                telegram_id=444555666,
                token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
            )
        },
    )
    miniapp_payload = miniapp_response.json()

    assert widget_response.status_code == 200
    assert miniapp_response.status_code == 200
    assert miniapp_payload["user"]["id"] == widget_user_id
    assert miniapp_payload["user"]["telegram_id"] == 444555666

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(
            select(func.count()).select_from(User).where(User.telegram_id == 444555666)
        )
        assert user_count_result.scalar_one() == 1

        persisted_user_result = await session.execute(select(User).where(User.telegram_id == 444555666))
        persisted_user = persisted_user_result.scalar_one()
        refresh_token_count_result = await session.execute(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == persisted_user.id)
        )
        assert refresh_token_count_result.scalar_one() == 2


async def test_telegram_routes_return_typed_provider_errors_for_tampered_and_expired_payloads(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tampered_payload = sign_telegram_widget_payload(
        {
            "id": 777888999,
            "first_name": "Alice",
            "auth_date": int(datetime.now(UTC).timestamp()),
        },
        token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
    )
    tampered_payload["first_name"] = "Mallory"

    invalid_response = await auth_client.post("/api/v1/auth/telegram", json=tampered_payload)
    expired_response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        json={
            "initData": build_miniapp_init_data(
                telegram_id=777888999,
                token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
                auth_date=int((datetime.now(UTC) - timedelta(minutes=10)).timestamp()),
            )
        },
    )

    assert invalid_response.status_code == 401
    assert invalid_response.json()["code"] == "provider_payload_invalid"
    assert expired_response.status_code == 401
    assert expired_response.json()["code"] == "provider_payload_expired"

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        refresh_token_count_result = await session.execute(select(func.count()).select_from(RefreshToken))
        assert user_count_result.scalar_one() == 0
        assert refresh_token_count_result.scalar_one() == 0


async def test_telegram_routes_return_provider_not_configured_when_bot_token_missing(
    postgres_async_url: str,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_async_url)
    monkeypatch.setenv("AUTH_JWT_SECRET", "route-test-auth-secret-with-32-byte-minimum")
    monkeypatch.setenv("AUTH_REFRESH_COOKIE_NAME", "route_refresh_token")
    monkeypatch.setenv("AUTH_REFRESH_COOKIE_SAMESITE", "strict")
    monkeypatch.setenv("AUTH_REFRESH_COOKIE_SECURE", "true")
    monkeypatch.delenv("AUTH_TELEGRAM_BOT_TOKEN", raising=False)

    get_settings.cache_clear()
    await reset_async_database_state()
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            json=sign_telegram_widget_payload(
                {
                    "id": 101010101,
                    "first_name": "Alice",
                    "auth_date": int(datetime.now(UTC).timestamp()),
                },
                token="123456:missing-config-signer",
            ),
        )

    await reset_async_database_state()
    get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json()["code"] == "provider_not_configured"
