"""DB-backed Telethon factory tests with no Telegram network calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from telethon.tl.types import MessageActionChatEditPhoto, MessageService, PeerChannel, Photo

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionNotRunnableError,
)
from memexpert.crawlers.telegram.session_crypto import TelegramStringSessionCipher
from memexpert.crawlers.telegram.telethon_adapter import (
    PipelineTelethonClient,
    TelethonClientFactory,
    _RateLimiter,
)
from memexpert.crawlers.telegram.telethon_mapper import TelethonMessageNormalizer
from memexpert.models.content import TelegramSession
from memexpert.models.enums import TelegramSessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_TEST_SESSION_SECRET = SecretStr("telethon-adapter-test-secret")
_TEST_STRING_SESSION = SecretStr("test-telethon-string-session")


class _FakeStringSession:
    def __init__(self, value: str) -> None:
        self.value = value


@dataclass(frozen=True, slots=True)
class _FakeAccount:
    id: int = 123456
    username: str = "primary_account"
    phone: str = "+15551234567"


class _FakeTelegramClient:
    instances: ClassVar[list[_FakeTelegramClient]] = []
    authorized: ClassVar[bool] = True
    account: ClassVar[_FakeAccount | None] = _FakeAccount()

    def __init__(self, *, session: object, api_id: int, api_hash: str) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.connected = False
        self.disconnected = False
        self.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected and not self.disconnected

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> _FakeAccount | None:
        return self.account

    def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False


@pytest.fixture(autouse=True)
def _reset_fake_telethon_client() -> None:
    _FakeTelegramClient.instances = []
    _FakeTelegramClient.authorized = True
    _FakeTelegramClient.account = _FakeAccount()


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "telegram_api_id": 12345,
            "telegram_api_hash": "test-api-hash",
            "telegram_session_encryption_secret": _TEST_SESSION_SECRET.get_secret_value(),
            "crawler_max_requests_per_second": 10.0,
        }
    )


async def _insert_telegram_session(
    session: AsyncSession,
    *,
    settings: Settings,
    name: str = "primary",
    status: TelegramSessionStatus = TelegramSessionStatus.ACTIVE,
    enabled: bool = True,
    string_session: SecretStr | None = _TEST_STRING_SESSION,
    max_requests_per_second: float = 2.5,
) -> None:
    encrypted_string_session = None
    if string_session is not None:
        encrypted_string_session = TelegramStringSessionCipher(settings.telegram_session_encryption_secret).encrypt(
            string_session,
        ).get_secret_value()
    session.add(
        TelegramSession(
            name=name,
            display_name="Primary",
            status=status,
            enabled=enabled,
            encrypted_string_session=encrypted_string_session,
            max_requests_per_second=max_requests_per_second,
        )
    )
    await session.commit()


async def test_telethon_factory_passes_db_string_session_to_telegram_client(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    await _insert_telegram_session(migrated_db_session, settings=settings)
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "StringSession", _FakeStringSession)
    monkeypatch.setattr(adapter_module, "TelegramClient", _FakeTelegramClient)
    factory = TelethonClientFactory(
        settings=settings,
        session_name="primary",
        session_factory=postgres_session_factory,
    )

    client = await factory.get_client()

    assert client is _FakeTelegramClient.instances[0]
    assert isinstance(client.session, _FakeStringSession)
    assert client.session.value == _TEST_STRING_SESSION.get_secret_value()
    assert not isinstance(client.session, str)
    assert client.api_id == settings.telegram_api_id
    assert settings.telegram_api_hash is not None
    assert client.api_hash == settings.telegram_api_hash.get_secret_value()
    assert factory.max_requests_per_second == 2.5

    async with postgres_session_factory() as verify_session:
        row = await verify_session.scalar(select(TelegramSession).where(TelegramSession.name == "primary"))
    assert row is not None
    assert row.last_heartbeat_at is not None
    assert row.account_user_id == 123456
    assert row.account_username == "primary_account"
    assert row.account_phone_hint == "ending-4567"


async def test_telethon_client_updates_limiter_from_loaded_db_session(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    await _insert_telegram_session(migrated_db_session, settings=settings, max_requests_per_second=4.0)
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "StringSession", _FakeStringSession)
    monkeypatch.setattr(adapter_module, "TelegramClient", _FakeTelegramClient)
    client = PipelineTelethonClient(
        factory=TelethonClientFactory(
            settings=settings,
            session_name="primary",
            session_factory=postgres_session_factory,
        ),
        rate_limiter=_RateLimiter(max_requests_per_second=settings.crawler_max_requests_per_second),
    )

    _ = await client._get_client()

    assert client.rate_limiter._min_interval_seconds == 0.25


async def test_telethon_factory_rejects_missing_or_unusable_db_session(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _ = migrated_db_session
    settings = _settings()
    factory = TelethonClientFactory(
        settings=settings,
        session_name="missing",
        session_factory=postgres_session_factory,
    )

    with pytest.raises(PipelineTelegramSessionNotRunnableError):
        _ = await factory.get_client()


async def test_telethon_factory_rejects_missing_string_session_material(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    await _insert_telegram_session(migrated_db_session, settings=settings, string_session=None)
    factory = TelethonClientFactory(
        settings=settings,
        session_name="primary",
        session_factory=postgres_session_factory,
    )

    with pytest.raises(PipelineTelegramSessionAuthRequiredError):
        _ = await factory.get_client()


async def test_telethon_factory_marks_unauthorized_string_session_auth_required(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    await _insert_telegram_session(migrated_db_session, settings=settings)
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "StringSession", _FakeStringSession)
    monkeypatch.setattr(adapter_module, "TelegramClient", _FakeTelegramClient)
    _FakeTelegramClient.authorized = False
    factory = TelethonClientFactory(
        settings=settings,
        session_name="primary",
        session_factory=postgres_session_factory,
    )

    with pytest.raises(PipelineTelegramSessionAuthRequiredError):
        _ = await factory.get_client()

    async with postgres_session_factory() as verify_session:
        row = await verify_session.scalar(select(TelegramSession).where(TelegramSession.name == "primary"))
    assert row is not None
    assert row.status is TelegramSessionStatus.AUTH_REQUIRED
    assert row.last_error_class == PipelineTelegramSessionAuthRequiredError.__name__
    assert row.encrypted_string_session is not None


def test_telethon_factory_requires_api_credentials() -> None:
    factory = TelethonClientFactory(
        settings=Settings.model_validate({"telegram_session_encryption_secret": "test-secret"}),
        session_name="primary",
        session_factory=None,
    )

    with pytest.raises(PipelineTelegramProviderUnavailableError) as exc_info:
        _ = factory._build_client(_TEST_STRING_SESSION)

    assert "TELEGRAM_API_ID" in str(exc_info.value)


def test_telethon_channel_photo_service_event_is_not_downloadable_post_media() -> None:
    observed_at = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
    service_message = MessageService(
        id=11,
        peer_id=PeerChannel(channel_id=123),
        date=observed_at,
        action=MessageActionChatEditPhoto(
            photo=Photo(
                id=1,
                access_hash=2,
                file_reference=b"",
                date=observed_at,
                sizes=[],
                dc_id=4,
            ),
        ),
    )

    normalized = TelethonMessageNormalizer.build(
        message=cast("Any", service_message),
        channel_id="123",
        channel_title="Channel",
        channel_username=None,
    )

    assert normalized.media_type == "unsupported"
    assert normalized.raw_payload is service_message
