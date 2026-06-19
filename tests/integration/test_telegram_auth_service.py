"""Integration tests for Telegram provider-auth service flows."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode

import pytest
from sqlalchemy import func, select

from memexpert.models.collection import Collection
from memexpert.models.enums import AccountType, CollectionKind
from memexpert.models.user import AccountMergeLog, LoginEvent, User
from memexpert.schemas.auth import TelegramWidgetAuthRequest
from memexpert.services import (
    AccountLinkResult,
    AccountLinkService,
    AuthService,
    AuthSession,
    ProviderAuthService,
    ProviderNotConfiguredError,
    ProviderPayloadExpiredError,
    ProviderPayloadInvalidError,
    UserService,
)
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

TELEGRAM_BOT_TOKEN = "123456:telegram-service-test-bot-token"
TELEGRAM_LOGIN_MAX_AGE_SECONDS = 300
TELEGRAM_MINIAPP_MAX_AGE_SECONDS = 300
JWT_SECRET = "telegram-service-test-jwt-secret-with-32-byte-minimum"


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


def build_account_link_service(
    migrated_db_session: AsyncSession,
    *,
    telegram_bot_token: str | None = TELEGRAM_BOT_TOKEN,
    telegram_login_max_age_seconds: int = TELEGRAM_LOGIN_MAX_AGE_SECONDS,
    telegram_miniapp_max_age_seconds: int = TELEGRAM_MINIAPP_MAX_AGE_SECONDS,
) -> AccountLinkService:
    return AccountLinkService(
        migrated_db_session,
        provider_auth_service=build_provider_auth_service(
            migrated_db_session,
            telegram_bot_token=telegram_bot_token,
            telegram_login_max_age_seconds=telegram_login_max_age_seconds,
            telegram_miniapp_max_age_seconds=telegram_miniapp_max_age_seconds,
        ),
    )


def build_auth_service(session: AsyncSession) -> AuthService:
    return AuthService(
        session,
        jwt_secret=JWT_SECRET,
        access_token_ttl=timedelta(minutes=15),
    )


async def _issue_session_for(
    session: AsyncSession,
    link_result: AccountLinkResult,
    *,
    user_agent: str | None = None,
) -> AuthSession:
    auth_service = build_auth_service(session)
    return await auth_service.issue_session_for_user(
        link_result.user,
        user_agent=user_agent,
        reload_user=False,
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
    raw_user_overrides = overrides.pop("user", None)
    if raw_user_overrides is not None:
        if not isinstance(raw_user_overrides, dict):
            raise TypeError("Mini App user overrides must be a mapping.")
        typed_user_overrides: dict[str, object] = {str(key): value for key, value in raw_user_overrides.items()}
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


def tamper_miniapp_init_data(init_data: str, **user_overrides: object) -> str:
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    user_payload = json.loads(fields["user"])
    if not isinstance(user_payload, dict):
        raise TypeError("Mini App user payload must be a mapping.")

    user_payload.update(user_overrides)
    fields["user"] = json.dumps(user_payload, separators=(",", ":"), ensure_ascii=False)
    return urlencode(fields)


async def test_telegram_auth_reuses_same_account_across_widget_and_miniapp_without_email_merge(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    link_service = build_account_link_service(migrated_db_session)
    existing_email_user = await create_full_user_via_upgrade(user_service, email="existing@example.com")

    widget_link = await link_service.link_guest_with_telegram_widget(
        guest_user_id=None,
        payload=build_widget_request(telegram_id=987654321),
    )
    widget_session = await _issue_session_for(
        migrated_db_session,
        widget_link,
        user_agent="Telegram Widget",
    )
    miniapp_link = await link_service.link_guest_with_telegram_miniapp(
        guest_user_id=None,
        init_data=build_miniapp_init_data(telegram_id=987654321),
    )
    miniapp_session = await _issue_session_for(
        migrated_db_session,
        miniapp_link,
        user_agent="Telegram Mini App",
    )

    assert widget_session.user.account_type is AccountType.FULL
    assert widget_session.user.telegram_id == 987654321
    assert widget_session.user.email is None
    assert widget_session.user.google_id is None
    assert miniapp_session.user.id == widget_session.user.id
    assert miniapp_session.user.telegram_id == 987654321
    assert miniapp_session.user.id != existing_email_user.id

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    # Two users: the email account seeded by the test and the Telegram
    # account created by the first widget call. The miniapp call resolved
    # into the existing Telegram account via merge, deleting its
    # transient bootstrap guest.
    assert user_count_result.scalar_one() == 2

    favorites_count_result = await migrated_db_session.execute(
        select(func.count())
        .select_from(Collection)
        .where(
            Collection.owner_id == widget_session.user.id,
            Collection.kind == CollectionKind.FAVORITES,
        )
    )
    # Lazy Favorites: neither upgrade-in-place nor merge pre-allocates
    # Favorites for the canonical Telegram account.
    assert favorites_count_result.scalar_one() == 0

    refresh_tokens_result = await migrated_db_session.execute(
        select(LoginEvent).where(LoginEvent.user_id == widget_session.user.id).order_by(LoginEvent.created_at.asc())
    )
    refresh_tokens = refresh_tokens_result.scalars().all()
    assert len(refresh_tokens) == 2
    assert {row.user_agent for row in refresh_tokens} == {"Telegram Widget", "Telegram Mini App"}

    # The miniapp call produced a merge log because its transient guest
    # was merged into the existing Telegram account rather than upgraded
    # in place.
    merge_log_count_result = await migrated_db_session.execute(select(func.count()).select_from(AccountMergeLog))
    assert merge_log_count_result.scalar_one() == 1


async def test_telegram_auth_rejects_blank_widget_hash_and_malformed_miniapp_before_side_effects(
    migrated_db_session: AsyncSession,
) -> None:
    link_service = build_account_link_service(migrated_db_session)
    blank_hash_request = build_widget_request(telegram_id=123456789, hash="   ")

    with pytest.raises(ProviderPayloadInvalidError, match="missing hash"):
        _ = await link_service.link_guest_with_telegram_widget(
            guest_user_id=None,
            payload=blank_hash_request,
        )

    with pytest.raises(ProviderPayloadInvalidError, match="invalid"):
        _ = await link_service.link_guest_with_telegram_miniapp(
            guest_user_id=None,
            init_data="not-a-valid-query-string",
        )

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))
    # Payload validation runs before the bootstrap (the delegators
    # resolve the identity first), so nothing is persisted.
    assert user_count_result.scalar_one() == 0
    assert login_event_count_result.scalar_one() == 0


async def test_telegram_auth_maps_tampered_and_expired_payloads_to_typed_failures(
    migrated_db_session: AsyncSession,
) -> None:
    link_service = build_account_link_service(migrated_db_session)
    expired_link_service = build_account_link_service(
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
        _ = await link_service.link_guest_with_telegram_widget(
            guest_user_id=None,
            payload=TelegramWidgetAuthRequest.model_validate(tampered_widget_payload),
        )

    tampered_init_data = tamper_miniapp_init_data(
        build_miniapp_init_data(telegram_id=222333443),
        username="mallory",
    )
    with pytest.raises(ProviderPayloadInvalidError, match="invalid"):
        _ = await link_service.link_guest_with_telegram_miniapp(
            guest_user_id=None,
            init_data=tampered_init_data,
        )

    expired_init_data = build_miniapp_init_data(
        telegram_id=222333444,
        auth_date=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
    )
    with pytest.raises(ProviderPayloadExpiredError, match="expired"):
        _ = await expired_link_service.link_guest_with_telegram_miniapp(
            guest_user_id=None,
            init_data=expired_init_data,
        )

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))
    assert user_count_result.scalar_one() == 0
    assert login_event_count_result.scalar_one() == 0


async def test_telegram_auth_requires_bot_token_configuration(
    migrated_db_session: AsyncSession,
) -> None:
    link_service = build_account_link_service(migrated_db_session, telegram_bot_token=None)

    with pytest.raises(ProviderNotConfiguredError, match="not configured"):
        _ = await link_service.link_guest_with_telegram_widget(
            guest_user_id=None,
            payload=build_widget_request(telegram_id=555666777),
        )

    user_count_result = await migrated_db_session.execute(select(func.count()).select_from(User))
    login_event_count_result = await migrated_db_session.execute(select(func.count()).select_from(LoginEvent))
    assert user_count_result.scalar_one() == 0
    assert login_event_count_result.scalar_one() == 0
