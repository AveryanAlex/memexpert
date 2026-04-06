"""Integration tests for Telegram provider-auth service flows."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountType, CollectionKind
from memexpert.models.user import RefreshToken, User
from memexpert.schemas.auth import TelegramWidgetAuthRequest
from memexpert.services import (
    ProviderAuthService,
    ProviderNotConfiguredError,
    ProviderPayloadExpiredError,
    ProviderPayloadInvalidError,
    UserService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

TELEGRAM_BOT_TOKEN = "123456:telegram-service-test-bot-token"
TELEGRAM_LOGIN_MAX_AGE_SECONDS = 300
TELEGRAM_MINIAPP_MAX_AGE_SECONDS = 300


def build_provider_auth_service(
    migrated_db_session: AsyncSession,
    *,
    telegram_bot_token: str | None = TELEGRAM_BOT_TOKEN,
    telegram_login_max_age_seconds: int = TELEGRAM_LOGIN_MAX_AGE_SECONDS,
    telegram_miniapp_max_age_seconds: int = TELEGRAM_MINIAPP_MAX_AGE_SECONDS,
) -> ProviderAuthService:
    return ProviderAuthService(
        migrated_db_session,
        password_hash_rounds=12,
        telegram_bot_token=telegram_bot_token,
        telegram_login_max_age_seconds=telegram_login_max_age_seconds,
        telegram_miniapp_max_age_seconds=telegram_miniapp_max_age_seconds,
    )


def sign_telegram_widget_payload(
    payload: dict[str, object],
    *,
    token: str = TELEGRAM_BOT_TOKEN,
) -> dict[str, object]:
    signed_fields = {key: value for key, value in payload.items() if value is not None}
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    data_check_string = "\n".join(f"{key}={signed_fields[key]}" for key in sorted(signed_fields))
    signature = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**signed_fields, "hash": signature}


def build_widget_request(
    *,
    telegram_id: int,
    auth_date: int | None = None,
    token: str = TELEGRAM_BOT_TOKEN,
    **overrides: object,
) -> TelegramWidgetAuthRequest:
    payload: dict[str, object] = {
        "id": telegram_id,
        "first_name": "Alice",
        "username": "alice_memexpert",
        "auth_date": auth_date if auth_date is not None else int(datetime.now(UTC).timestamp()),
    }
    payload.update(overrides)
    if "hash" not in payload:
        payload = sign_telegram_widget_payload(payload, token=token)
    return TelegramWidgetAuthRequest.model_validate(payload)


def build_miniapp_init_data(
    *,
    telegram_id: int,
    auth_date: int | None = None,
    token: str = TELEGRAM_BOT_TOKEN,
    **overrides: object,
) -> str:
    user_payload: dict[str, object] = {
        "id": telegram_id,
        "first_name": "Alice",
        "username": "alice_memexpert",
    }
    user_payload.update(overrides.pop("user", {}))

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


async def test_telegram_auth_reuses_same_account_across_widget_and_miniapp_without_email_merge(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    existing_email_user = await user_service.create_full_user(email="existing@example.com")

    widget_session = await provider_auth_service.authenticate_with_telegram_widget(
        payload=build_widget_request(telegram_id=987654321),
        device_info="Telegram Widget",
    )
    miniapp_session = await provider_auth_service.authenticate_with_telegram_miniapp(
        init_data=build_miniapp_init_data(telegram_id=987654321),
        device_info="Telegram Mini App",
    )

    assert widget_session.user.account_type is AccountType.FULL
    assert widget_session.user.telegram_id == 987654321
    assert widget_session.user.email is None
    assert widget_session.user.google_id is None
    assert miniapp_session.user.id == widget_session.user.id
    assert miniapp_session.user.telegram_id == 987654321
    assert miniapp_session.user.id != existing_email_user.id

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    assert user_count_result.scalar_one() == 2

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(
            Collection.owner_id == widget_session.user.id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    assert favorites_count_result.scalar_one() == 1

    refresh_tokens_result = await migrated_db_session.execute(
        select(RefreshToken)
        .where(RefreshToken.user_id == widget_session.user.id)
        .order_by(RefreshToken.created_at.asc())
    )
    refresh_tokens = refresh_tokens_result.scalars().all()
    assert len(refresh_tokens) == 2
    assert {row.device_info for row in refresh_tokens} == {"Telegram Widget", "Telegram Mini App"}


async def test_telegram_auth_rejects_blank_widget_hash_and_malformed_miniapp_before_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    blank_hash_request = build_widget_request(telegram_id=123456789, hash="   ")

    with pytest.raises(ProviderPayloadInvalidError, match="missing hash"):
        _ = await provider_auth_service.authenticate_with_telegram_widget(payload=blank_hash_request)

    with pytest.raises(ProviderPayloadInvalidError, match="invalid"):
        _ = await provider_auth_service.authenticate_with_telegram_miniapp(init_data="not-a-valid-query-string")

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    refresh_token_count_result = await migrated_db_session.execute(select(func.count()).select_from(RefreshToken))
    assert user_count_result.scalar_one() == 0
    assert refresh_token_count_result.scalar_one() == 0


async def test_telegram_auth_maps_tampered_and_expired_payloads_to_typed_failures(
    migrated_db_session: AsyncSession,
) -> None:
    provider_auth_service = build_provider_auth_service(migrated_db_session)
    expired_provider_auth_service = build_provider_auth_service(
        migrated_db_session,
        telegram_login_max_age_seconds=60,
        telegram_miniapp_max_age_seconds=60,
    )

    tampered_widget_payload = sign_telegram_widget_payload(
        {
            "id": 222333444,
            "first_name": "Alice",
            "auth_date": int(datetime.now(UTC).timestamp()),
        }
    )
    tampered_widget_payload["first_name"] = "Mallory"

    with pytest.raises(ProviderPayloadInvalidError, match="signature is invalid"):
        _ = await provider_auth_service.authenticate_with_telegram_widget(
            payload=TelegramWidgetAuthRequest.model_validate(tampered_widget_payload)
        )

    expired_init_data = build_miniapp_init_data(
        telegram_id=222333444,
        auth_date=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
    )
    with pytest.raises(ProviderPayloadExpiredError, match="expired"):
        _ = await expired_provider_auth_service.authenticate_with_telegram_miniapp(init_data=expired_init_data)

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    refresh_token_count_result = await migrated_db_session.execute(select(func.count()).select_from(RefreshToken))
    assert user_count_result.scalar_one() == 0
    assert refresh_token_count_result.scalar_one() == 0


async def test_telegram_auth_requires_bot_token_configuration(
    migrated_db_session: AsyncSession,
) -> None:
    provider_auth_service = build_provider_auth_service(migrated_db_session, telegram_bot_token=None)

    with pytest.raises(ProviderNotConfiguredError, match="not configured"):
        _ = await provider_auth_service.authenticate_with_telegram_widget(
            payload=build_widget_request(telegram_id=555666777),
        )

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    refresh_token_count_result = await migrated_db_session.execute(select(func.count()).select_from(RefreshToken))
    assert user_count_result.scalar_one() == 0
    assert refresh_token_count_result.scalar_one() == 0
