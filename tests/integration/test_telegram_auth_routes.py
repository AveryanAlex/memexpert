"""Integration tests for FastAPI Telegram auth routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl, urlencode

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from memexpert.api.app import create_app
from memexpert.core.config import get_settings
from memexpert.core.database import reset_async_database_state
from memexpert.models.enums import AccountType, AnalyticsEventType
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, LoginEvent, User
from memexpert.services import UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _analytics_properties(event: AnalyticsEvent) -> dict[str, object]:
    return cast("dict[str, object]", event.payload["properties"])


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


async def test_telegram_widget_route_returns_session_and_records_login_event(
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

    payload = response.json()

    assert response.status_code == 200
    assert payload["user"]["account_type"] == "full"
    assert payload["user"]["telegram_id"] == 111222333
    assert payload["user"]["email"] is None
    assert "access_token" not in payload
    assert auth_client.cookies.get("memexpert_access_token")

    async with postgres_session_factory() as session:
        persisted_user_result = await session.execute(select(User).where(User.telegram_id == 111222333))
        persisted_user = persisted_user_result.scalar_one()
        assert persisted_user.email is None

        login_event_result = await session.execute(select(LoginEvent).where(LoginEvent.user_id == persisted_user.id))
        login_event_row = login_event_result.scalar_one()
        assert login_event_row.user_agent == "Telegram Widget Browser"


async def test_telegram_miniapp_route_first_open_creates_exactly_one_full_user_no_merge_log(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mini App first-open must produce a single full account with zero merge logs.

    The route bootstraps a throwaway guest and upgrades it in place (fresh
    telegram_id → no existing user to merge into), so the end state is one
    user row whose linked Telegram identity derives account_type=full. The
    transient bootstrap must not leak as an extra guest row, and no
    AccountMergeLog should be written because upgrade-in-place does not write
    the log.
    """

    response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        headers={"User-Agent": "Telegram Mini App"},
        json={
            "initData": build_miniapp_init_data(
                telegram_id=303030303,
                token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["account_type"] == "full"
    assert payload["user"]["telegram_id"] == 303030303
    assert "access_token" not in payload
    assert "memexpert_access_token=" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert auth_client.cookies.get("memexpert_access_token")

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        full_count_result = await session.execute(
            select(func.count()).select_from(User).where(User.account_type == AccountType.FULL)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))
        event = await session.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MINIAPP_OPEN)
        )

        assert user_count_result.scalar_one() == 1
        assert full_count_result.scalar_one() == 1
        assert merge_log_count_result.scalar_one() == 0
        assert event is not None
        properties = _analytics_properties(event)
        assert event.payload["surface"] == "telegram_miniapp_auth"
        assert properties["action"] == "auth_session_issued"
        assert "telegram_user_hash" in properties
        assert "initData" not in event.payload
        assert "init_data" not in event.payload


async def test_telegram_miniapp_route_replay_same_init_data_reissues_session_without_duplicates(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    init_data = build_miniapp_init_data(
        telegram_id=303030304,
        token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
    )

    first_response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        headers={"User-Agent": "Telegram Mini App first open"},
        json={"initData": init_data},
    )
    second_response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        headers={"User-Agent": "Telegram Mini App replay"},
        json={"initData": init_data},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_payload = first_response.json()
    second_payload = second_response.json()
    assert second_payload["user"]["id"] == first_payload["user"]["id"]
    assert second_payload["user"]["telegram_id"] == 303030304
    assert "access_token" not in second_payload
    assert "memexpert_access_token=" in second_response.headers["set-cookie"]
    assert "httponly" in second_response.headers["set-cookie"].lower()

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        telegram_user_count_result = await session.execute(
            select(func.count()).select_from(User).where(User.telegram_id == 303030304)
        )
        merge_log_count_result = await session.execute(select(func.count()).select_from(AccountMergeLog))
        login_event_result = await session.execute(
            select(LoginEvent)
            .where(LoginEvent.user_id == uuid.UUID(first_payload["user"]["id"]))
            .order_by(LoginEvent.occurred_at.asc())
        )
        miniapp_event_count_result = await session.execute(
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(AnalyticsEvent.event_type == AnalyticsEventType.MINIAPP_OPEN)
        )

        login_events = login_event_result.scalars().all()
        assert user_count_result.scalar_one() == 1
        assert telegram_user_count_result.scalar_one() == 1
        assert merge_log_count_result.scalar_one() == 0
        assert [event.user_agent for event in login_events] == [
            "Telegram Mini App first open",
            "Telegram Mini App replay",
        ]
        assert miniapp_event_count_result.scalar_one() == 2


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
    # Drop the widget session cookie so the miniapp call arrives as an
    # anonymous caller — the guest-only guard would otherwise reject a
    # full-account session attempting a second login.
    auth_client.cookies.delete("memexpert_access_token")

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
        login_event_count_result = await session.execute(
            select(func.count()).select_from(LoginEvent).where(LoginEvent.user_id == persisted_user.id)
        )
        assert login_event_count_result.scalar_one() == 2


async def test_telegram_miniapp_route_merges_guest_cookie_into_existing_telegram_account(
    auth_client: AsyncClient,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_id = 444555667
    async with postgres_session_factory() as session:
        full_user = await create_full_user_via_upgrade(
            UserService(session),
            telegram_id=telegram_id,
        )
        full_user_id = str(full_user.id)

    guest_response = await auth_client.post("/api/v1/auth/guest")
    guest_user_id = guest_response.json()["user"]["id"]

    miniapp_response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        headers={"User-Agent": "Telegram Mini App guest merge"},
        json={
            "initData": build_miniapp_init_data(
                telegram_id=telegram_id,
                token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
            )
        },
    )
    current_response = await auth_client.get("/api/v1/auth/session")

    assert miniapp_response.status_code == 200
    assert miniapp_response.json()["user"]["id"] == full_user_id
    assert miniapp_response.json()["user"]["telegram_id"] == telegram_id
    assert "memexpert_access_token=" in miniapp_response.headers["set-cookie"]
    assert "httponly" in miniapp_response.headers["set-cookie"].lower()
    assert current_response.status_code == 200
    assert current_response.json()["user"]["id"] == full_user_id
    assert current_response.json()["linked_providers"]["telegram_linked"] is True

    async with postgres_session_factory() as session:
        deleted_guest = await session.scalar(select(User).where(User.id == uuid.UUID(guest_user_id)))
        telegram_user_count_result = await session.execute(
            select(func.count()).select_from(User).where(User.telegram_id == telegram_id)
        )
        merge_log = await session.scalar(select(AccountMergeLog))
        login_event_count_result = await session.execute(
            select(func.count()).select_from(LoginEvent).where(LoginEvent.user_id == uuid.UUID(full_user_id))
        )

        assert deleted_guest is None
        assert telegram_user_count_result.scalar_one() == 1
        assert merge_log is not None
        assert str(merge_log.guest_account_id) == guest_user_id
        assert str(merge_log.target_account_id) == full_user_id
        assert login_event_count_result.scalar_one() == 1


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
    valid_miniapp_init_data = build_miniapp_init_data(
        telegram_id=777888998,
        token=auth_settings_overrides["AUTH_TELEGRAM_BOT_TOKEN"],
    )
    invalid_miniapp_response = await auth_client.post(
        "/api/v1/auth/telegram-miniapp",
        json={"initData": tamper_miniapp_init_data(valid_miniapp_init_data, username="mallory")},
    )
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
    assert invalid_miniapp_response.status_code == 401
    assert invalid_miniapp_response.json()["code"] == "provider_payload_invalid"
    assert expired_response.status_code == 401
    assert expired_response.json()["code"] == "provider_payload_expired"

    async with postgres_session_factory() as session:
        user_count_result = await session.execute(select(func.count()).select_from(User))
        login_event_count_result = await session.execute(select(func.count()).select_from(LoginEvent))
        analytics_event_count_result = await session.execute(select(func.count()).select_from(AnalyticsEvent))
        assert user_count_result.scalar_one() == 0
        assert login_event_count_result.scalar_one() == 0
        assert analytics_event_count_result.scalar_one() == 0


async def test_telegram_routes_return_provider_not_configured_when_bot_token_missing(
    postgres_async_url: str,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_async_url)
    monkeypatch.setenv("AUTH_JWT_SECRET", "route-test-auth-secret-with-32-byte-minimum")
    monkeypatch.setenv("SECURITY_RATE_LIMIT_ENABLED", "false")
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
