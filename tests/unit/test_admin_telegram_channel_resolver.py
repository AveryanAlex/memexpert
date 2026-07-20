"""Unit coverage for bounded public Telegram channel resolution."""

from __future__ import annotations

import asyncio
import traceback
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import SecretStr

import memexpert.services.admin_telegram_channel_resolver as resolver_module
from memexpert.crawlers.telegram.client import FakeTelegramClient, RawTelegramChannel
from memexpert.services.admin_telegram_channel_resolver import (
    AdminTelegramChannelResolverError,
    normalize_public_telegram_reference,
    resolve_admin_telegram_channel,
)

if TYPE_CHECKING:
    from memexpert.core.config import Settings


@pytest.fixture(autouse=True)
def _fake_full_channel_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver_module, "_telegram_full_channel_request", lambda entity: entity)


@pytest.mark.parametrize(
    ("reference", "username"),
    [
        ("@Public_Channel", "public_channel"),
        ("Public_Channel", "public_channel"),
        ("https://t.me/Public_Channel", "public_channel"),
        ("telegram.me/Public_Channel", "public_channel"),
    ],
)
def test_normalize_public_telegram_reference_accepts_only_public_forms(reference: str, username: str) -> None:
    normalized = normalize_public_telegram_reference(reference)

    assert normalized.username == username
    assert normalized.canonical_url == f"https://t.me/{username}"


@pytest.mark.parametrize(
    "reference",
    [
        "https://t.me/+invite",
        "https://t.me/joinchat/invite",
        "https://t.me/c/123/4",
        "https://t.me/Public_Channel/42",
        "https://t.me/Public_Channel?start=secret",
        "https://example.com/Public_Channel",
        "https://t.me:invalid/Public_Channel",
        "tg://resolve?domain=Public_Channel",
        "+15551234567",
        "tiny",
    ],
)
def test_normalize_public_telegram_reference_rejects_invite_private_and_non_telegram_inputs(
    reference: str,
) -> None:
    with pytest.raises(AdminTelegramChannelResolverError):
        normalize_public_telegram_reference(reference)


@pytest.mark.asyncio
async def test_resolver_stores_lowercase_public_username_that_existing_crawler_client_resolves(monkeypatch) -> None:
    secret = SecretStr("server-only-string-session")
    entity = _FakeChannel(
        id=123456789,
        username="Canonical_Channel",
        title="Canonical channel",
        participants_count=456,
    )
    client = _FakeClient(entity=entity)
    captured: dict[str, object] = {}

    def fake_build_telegram_client(*, string_session: SecretStr, api_id: int, api_hash: SecretStr):
        captured.update(string_session=string_session, api_id=api_id, api_hash=api_hash)
        return client

    monkeypatch.setattr(resolver_module, "_build_telegram_client", fake_build_telegram_client)
    monkeypatch.setattr(resolver_module, "_telegram_channel_type", lambda: _FakeChannel)

    resolved = await resolve_admin_telegram_channel(
        settings=_settings(),
        string_session=secret,
        reference="@canonical_channel",
    )

    assert resolved.platform_id == "canonical_channel"
    assert resolved.username == "canonical_channel"
    assert resolved.title == "Canonical channel"
    assert resolved.subscriber_count == 456
    assert client.references == ["canonical_channel"]
    assert client.full_channel_requests == [entity]
    assert client.disconnected is True
    assert captured["string_session"] is secret
    assert "server-only-string-session" not in repr(resolved)

    crawler_channel = RawTelegramChannel(
        channel_id="123456789",
        username=resolved.username,
        title=resolved.title,
        subscriber_count=resolved.subscriber_count,
    )
    crawler_client = FakeTelegramClient(canned_channels={resolved.platform_id: crawler_channel})
    assert await crawler_client.resolve_channel(resolved.platform_id) == crawler_channel


@pytest.mark.asyncio
async def test_resolver_accepts_requested_handle_from_active_multi_username_list(monkeypatch) -> None:
    entity = _FakeChannel(
        id=123456789,
        username=None,
        usernames=[
            SimpleNamespace(username="memach", active=True),
            SimpleNamespace(username="notmemes", active=True),
            SimpleNamespace(username="old_memach", active=False),
        ],
        title="Not Memes",
    )
    client = _FakeClient(entity=entity)
    monkeypatch.setattr(resolver_module, "_build_telegram_client", lambda **_kwargs: client)
    monkeypatch.setattr(resolver_module, "_telegram_channel_type", lambda: _FakeChannel)

    resolved = await resolve_admin_telegram_channel(
        settings=_settings(),
        string_session=SecretStr("opaque"),
        reference="@MeMaCh",
    )

    assert resolved.platform_id == "memach"
    assert resolved.username == "memach"
    assert resolved.title == "Not Memes"
    assert client.references == ["memach"]


@pytest.mark.asyncio
async def test_resolver_rejects_inactive_multi_username_without_legacy_handle(monkeypatch) -> None:
    client = _FakeClient(
        entity=_FakeChannel(
            username=None,
            usernames=[SimpleNamespace(username="public_channel", active=False)],
        ),
    )
    monkeypatch.setattr(resolver_module, "_build_telegram_client", lambda **_kwargs: client)
    monkeypatch.setattr(resolver_module, "_telegram_channel_type", lambda: _FakeChannel)

    with pytest.raises(AdminTelegramChannelResolverError, match="with a handle"):
        await resolve_admin_telegram_channel(
            settings=_settings(),
            string_session=SecretStr("opaque"),
            reference="@public_channel",
        )


@pytest.mark.asyncio
async def test_resolver_requires_authorization_and_channel_entity(monkeypatch) -> None:
    unauthorized = _FakeClient(entity=_FakeChannel(), authorized=False)
    monkeypatch.setattr(resolver_module, "_build_telegram_client", lambda **_kwargs: unauthorized)
    monkeypatch.setattr(resolver_module, "_telegram_channel_type", lambda: _FakeChannel)

    with pytest.raises(AdminTelegramChannelResolverError, match="no longer authorized"):
        await resolve_admin_telegram_channel(
            settings=_settings(),
            string_session=SecretStr("opaque"),
            reference="@public_channel",
        )

    not_a_channel = _FakeClient(entity=object())
    monkeypatch.setattr(resolver_module, "_build_telegram_client", lambda **_kwargs: not_a_channel)
    with pytest.raises(AdminTelegramChannelResolverError, match="not a channel"):
        await resolve_admin_telegram_channel(
            settings=_settings(),
            string_session=SecretStr("opaque"),
            reference="@public_channel",
        )


@pytest.mark.asyncio
async def test_resolver_applies_finite_timeout_and_returns_safe_provider_error(monkeypatch) -> None:
    slow_client = _FakeClient(entity=_FakeChannel(), delay_seconds=1)
    monkeypatch.setattr(resolver_module, "_build_telegram_client", lambda **_kwargs: slow_client)
    monkeypatch.setattr(resolver_module, "_telegram_channel_type", lambda: _FakeChannel)

    with pytest.raises(AdminTelegramChannelResolverError, match="did not respond in time") as timeout_error:
        await resolve_admin_telegram_channel(
            settings=_settings(),
            string_session=SecretStr("must-not-leak"),
            reference="@public_channel",
            timeout_seconds=0.01,
        )
    assert "must-not-leak" not in str(timeout_error.value)
    assert slow_client.disconnected is True

    failing_client = _FakeClient(entity=_FakeChannel(), error=RuntimeError("phone=+15551234567 password=hunter2"))
    monkeypatch.setattr(resolver_module, "_build_telegram_client", lambda **_kwargs: failing_client)
    with pytest.raises(AdminTelegramChannelResolverError, match="could not resolve") as provider_error:
        await resolve_admin_telegram_channel(
            settings=_settings(),
            string_session=SecretStr("must-not-leak"),
            reference="@public_channel",
        )
    assert "+15551234567" not in str(provider_error.value)
    assert "hunter2" not in str(provider_error.value)
    formatted_error = "".join(
        traceback.format_exception(
            provider_error.type,
            provider_error.value,
            provider_error.tb,
        ),
    )
    assert "+15551234567" not in formatted_error
    assert "hunter2" not in formatted_error
    assert provider_error.value.__cause__ is None


class _FakeChannel:
    def __init__(
        self,
        *,
        id: int = 123,
        username: str | None = "public_channel",
        usernames: list[object] | None = None,
        title: str = "Public channel",
        participants_count: int | None = None,
    ) -> None:
        self.id = id
        self.username = username
        self.usernames = usernames
        self.title = title
        self.participants_count = participants_count


class _FakeClient:
    def __init__(
        self,
        *,
        entity: object,
        authorized: bool = True,
        delay_seconds: float = 0,
        error: Exception | None = None,
    ) -> None:
        self.entity = entity
        self.authorized = authorized
        self.delay_seconds = delay_seconds
        self.error = error
        self.references: list[str] = []
        self.full_channel_requests: list[object] = []
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_entity(self, reference: str) -> object:
        self.references.append(reference)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return self.entity

    async def __call__(self, request: object) -> object:
        self.full_channel_requests.append(request)
        return SimpleNamespace(
            full_chat=SimpleNamespace(participants_count=getattr(self.entity, "participants_count", None)),
        )

    async def disconnect(self) -> None:
        self.disconnected = True


def _settings() -> Settings:
    return cast(
        "Settings",
        cast("object", SimpleNamespace(
            telegram_api_id=12345,
            telegram_api_hash=SecretStr("api-hash"),
        )),
    )
