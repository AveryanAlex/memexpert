"""DB-backed Telethon factory tests with no Telegram network calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from telethon.tl.types import (
    Message as TelethonMessage,
)
from telethon.tl.types import (
    MessageActionChatEditPhoto,
    MessageEntityBold,
    MessageService,
    PeerChannel,
    Photo,
)

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionNotRunnableError,
    TelegramMessageEditedEvent,
    TelegramMessagesDeletedEvent,
    TelegramNewMessageEvent,
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
    connect_delay_seconds: ClassVar[float] = 0.0

    def __init__(
        self,
        *,
        session: object,
        api_id: int,
        api_hash: str,
        request_retries: int,
        connection_retries: int,
        raise_last_call_error: bool,
        auto_reconnect: bool,
    ) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.request_retries = request_retries
        self.connection_retries = connection_retries
        self.raise_last_call_error = raise_last_call_error
        self.auto_reconnect = auto_reconnect
        self.connected = False
        self.disconnected = False
        self.instances.append(self)

    async def connect(self) -> None:
        if self.connect_delay_seconds:
            await asyncio.sleep(self.connect_delay_seconds)
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


@dataclass(frozen=True, slots=True)
class _FakeChannel:
    id: int = 123
    title: str = "Channel"
    username: str | None = None


@dataclass(slots=True)
class _FakeSingleMessageClient:
    message: object

    async def get_messages(self, _entity: object, *, ids: int) -> object:
        assert ids == 11
        return self.message


@dataclass(slots=True)
class _FakeBatchMessageClient:
    messages: object
    requested_ids: list[int] | None = None

    async def get_messages(self, _entity: object, *, ids: list[int]) -> object:
        self.requested_ids = ids
        return self.messages


class _FakeLiveClient:
    def __init__(self) -> None:
        self.handlers: list[tuple[Any, object]] = []
        self.removed_handlers: list[Any] = []
        self.disconnected: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def add_event_handler(self, handler: Any, event_builder: object) -> None:
        self.handlers.append((handler, event_builder))

    def remove_event_handler(self, handler: Any) -> None:
        self.removed_handlers.append(handler)


@dataclass(slots=True)
class _FakeSingleMessageFactory:
    client: _FakeSingleMessageClient
    settings: Settings
    max_requests_per_second: float | None = None

    async def get_client(self) -> _FakeSingleMessageClient:
        return self.client


@dataclass(slots=True)
class _FakeAdapterFactory:
    client: object
    settings: Settings
    session_name: str = "test-session"
    max_requests_per_second: float | None = None

    async def get_client(self) -> object:
        return self.client


@dataclass(slots=True)
class _FailingSingleMessageClient:
    error: Exception

    async def get_messages(self, _entity: object, *, ids: int) -> object:
        _ = ids
        raise self.error


@dataclass(slots=True)
class _EvictingSingleMessageFactory:
    client: _FailingSingleMessageClient
    settings: Settings
    session_name: str = "test-session"
    max_requests_per_second: float | None = None
    invalidated_clients: list[object] = field(default_factory=list)

    async def get_client(self) -> _FailingSingleMessageClient:
        return self.client

    async def invalidate_client(self, *, client: object | None = None) -> None:
        self.invalidated_clients.append(client)


@pytest.fixture(autouse=True)
def _reset_fake_telethon_client() -> None:
    _FakeTelegramClient.instances = []
    _FakeTelegramClient.authorized = True
    _FakeTelegramClient.account = _FakeAccount()
    _FakeTelegramClient.connect_delay_seconds = 0.0


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
        encrypted_string_session = (
            TelegramStringSessionCipher(settings.telegram_session_encryption_secret)
            .encrypt(
                string_session,
            )
            .get_secret_value()
        )
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
    assert client.request_retries == 2
    assert client.connection_retries == 2
    assert client.raise_last_call_error is True
    assert client.auto_reconnect is False
    assert factory.max_requests_per_second == 2.5

    async with postgres_session_factory() as verify_session:
        row = await verify_session.scalar(select(TelegramSession).where(TelegramSession.name == "primary"))
    assert row is not None
    assert row.last_heartbeat_at is not None
    assert row.account_user_id == 123456
    assert row.account_username == "primary_account"
    assert row.account_phone_hint == "ending-4567"


@pytest.mark.parametrize("authorized", [True, False], ids=["authorized", "auth-rejected"])
async def test_telethon_factory_can_disable_all_session_state_persistence(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    authorized: bool,
) -> None:
    settings = _settings()
    await _insert_telegram_session(migrated_db_session, settings=settings)
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "StringSession", _FakeStringSession)
    monkeypatch.setattr(adapter_module, "TelegramClient", _FakeTelegramClient)
    _FakeTelegramClient.authorized = authorized
    factory = TelethonClientFactory(
        settings=settings,
        session_name="primary",
        session_factory=postgres_session_factory,
        persist_session_state=False,
    )

    if authorized:
        _ = await factory.get_client()
    else:
        with pytest.raises(PipelineTelegramSessionAuthRequiredError):
            _ = await factory.get_client()

    async with postgres_session_factory() as verify_session:
        row = await verify_session.scalar(select(TelegramSession).where(TelegramSession.name == "primary"))
    assert row is not None
    assert row.status is TelegramSessionStatus.ACTIVE
    assert row.last_heartbeat_at is None
    assert row.last_error_class is None
    assert row.last_error_text is None
    assert row.account_user_id is None
    assert row.account_username is None
    assert row.account_phone_hint is None
    assert row.encrypted_string_session is not None


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


async def test_telethon_factory_times_out_evicts_and_reconnects_with_fresh_client(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings().model_copy(update={"crawler_telegram_connect_timeout_seconds": 0.01})
    await _insert_telegram_session(migrated_db_session, settings=settings)
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "StringSession", _FakeStringSession)
    monkeypatch.setattr(adapter_module, "TelegramClient", _FakeTelegramClient)
    _FakeTelegramClient.connect_delay_seconds = 1.0
    factory = TelethonClientFactory(
        settings=settings,
        session_name="primary",
        session_factory=postgres_session_factory,
    )

    with pytest.raises(PipelineTelegramProviderUnavailableError, match="TimeoutError"):
        _ = await factory.get_client()

    first_client = _FakeTelegramClient.instances[0]
    assert first_client.disconnected is True
    assert factory._client is None  # noqa: SLF001 - verifies poisoned client eviction.

    _FakeTelegramClient.connect_delay_seconds = 0.0
    reconnected = await factory.get_client()

    assert reconnected is _FakeTelegramClient.instances[1]
    assert reconnected is not first_client


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


async def test_telethon_fetch_single_message_accepts_service_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    client = PipelineTelethonClient(
        factory=cast(
            "Any",
            _FakeSingleMessageFactory(
                client=_FakeSingleMessageClient(message=service_message),
                settings=_settings(),
            ),
        ),
        rate_limiter=_RateLimiter(max_requests_per_second=10.0),
    )

    async def _resolve_entity(_self: PipelineTelethonClient, _channel_id: str) -> _FakeChannel:
        return _FakeChannel()

    monkeypatch.setattr(PipelineTelethonClient, "_resolve_entity", _resolve_entity)

    normalized = await client.fetch_single_message(channel_id="123", post_id="11")

    assert normalized.message_id == "11"
    assert normalized.media_type == "unsupported"
    assert normalized.raw_payload is service_message


async def test_telethon_fetch_messages_normalizes_batch_and_preserves_missing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
    first_message = TelethonMessage(
        id=10,
        peer_id=PeerChannel(channel_id=123),
        date=observed_at,
        message="first batch caption",
        entities=[MessageEntityBold(offset=0, length=5)],
        grouped_id=777,
    )
    third_message = TelethonMessage(
        id=12,
        peer_id=PeerChannel(channel_id=123),
        date=observed_at,
        message="",
    )
    underlying_client = _FakeBatchMessageClient(
        # Telethon documents one positional result per requested integer id;
        # an explicit None is the only safe missing-message signal.
        messages=(first_message, None, third_message),
    )
    client = PipelineTelethonClient(
        factory=cast(
            "Any",
            _FakeAdapterFactory(
                client=underlying_client,
                settings=_settings(),
            ),
        ),
        rate_limiter=_RateLimiter(max_requests_per_second=10.0),
    )

    async def _resolve_entity(_self: PipelineTelethonClient, _channel_id: str) -> _FakeChannel:
        return _FakeChannel()

    monkeypatch.setattr(PipelineTelethonClient, "_resolve_entity", _resolve_entity)

    fetched = await client.fetch_messages(
        channel_id="tracked-channel",
        post_ids=("10", "11", "12"),
    )

    assert underlying_client.requested_ids == [10, 11, 12]
    assert list(fetched) == ["10", "11", "12"]
    assert fetched["11"] is None
    assert fetched["10"] is not None
    assert fetched["10"].telegram_post.text == "first batch caption"
    assert fetched["10"].telegram_post.entity_json() == [
        {"type": "bold", "offset": 0, "length": 5}
    ]
    assert fetched["10"].telegram_post.media_group_id == "777"
    assert fetched["12"] is not None
    assert fetched["12"].telegram_post.text is None

    underlying_client.messages = (object(),)
    with pytest.raises(PipelineTelegramMalformedMessageError, match="unexpected item"):
        _ = await client.fetch_messages(
            channel_id="tracked-channel",
            post_ids=("10",),
        )
    underlying_client.messages = (
        TelethonMessage(
            id=99,
            peer_id=PeerChannel(channel_id=123),
            date=observed_at,
            message="unrequested",
        ),
    )
    with pytest.raises(PipelineTelegramMalformedMessageError, match="unrequested message id"):
        _ = await client.fetch_messages(
            channel_id="tracked-channel",
            post_ids=("10",),
        )
    underlying_client.messages = (first_message,)
    incomplete = await client.fetch_messages(
        channel_id="tracked-channel",
        post_ids=("10", "11"),
    )
    assert incomplete == {}
    with pytest.raises(PipelineTelegramMalformedMessageError, match="must be numeric"):
        _ = await client.fetch_messages(
            channel_id="tracked-channel",
            post_ids=("not-numeric",),
        )
    with pytest.raises(ValueError, match="at most 100"):
        _ = await client.fetch_messages(
            channel_id="tracked-channel",
            post_ids=tuple(str(post_id) for post_id in range(101)),
        )


async def test_telethon_live_handlers_emit_new_edit_and_delete_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    underlying_client = _FakeLiveClient()
    client = PipelineTelethonClient(
        factory=cast(
            "Any",
            _FakeAdapterFactory(
                client=underlying_client,
                settings=_settings(),
            ),
        ),
        rate_limiter=_RateLimiter(max_requests_per_second=10.0),
    )

    async def _resolve_entity(_self: PipelineTelethonClient, _channel_id: str) -> _FakeChannel:
        return _FakeChannel(id=123, title="Tracked Channel", username="tracked")

    monkeypatch.setattr(PipelineTelethonClient, "_resolve_entity", _resolve_entity)
    monkeypatch.setattr(adapter_module, "get_peer_id", lambda _entity: -1_000_000_000_123)

    ready_event = asyncio.Event()
    stream = client.listen_live(
        channel_ids=("tracked-channel",),
        ready_event=ready_event,
    )
    first_event_task = asyncio.ensure_future(anext(stream))
    await asyncio.wait_for(ready_event.wait(), timeout=1.0)
    assert len(underlying_client.handlers) == 3
    new_handler, edited_handler, deleted_handler = [
        handler for handler, _event_builder in underlying_client.handlers
    ]

    observed_at = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
    new_message = TelethonMessage(
        id=20,
        peer_id=PeerChannel(channel_id=123),
        date=observed_at,
        message="new post",
    )
    edited_at = datetime(2024, 5, 1, 13, 30, tzinfo=UTC)
    edited_message = TelethonMessage(
        id=20,
        peer_id=PeerChannel(channel_id=123),
        date=observed_at,
        message="edited post",
        edit_date=edited_at,
    )

    try:
        await new_handler(SimpleNamespace(message=new_message))
        new_event = await asyncio.wait_for(first_event_task, timeout=1.0)
        assert isinstance(new_event, TelegramNewMessageEvent)
        assert new_event.message.channel_id == "tracked-channel"
        assert new_event.message.telegram_post.text == "new post"

        edited_event_task = asyncio.ensure_future(anext(stream))
        await edited_handler(SimpleNamespace(message=edited_message))
        edited_event = await asyncio.wait_for(edited_event_task, timeout=1.0)
        assert isinstance(edited_event, TelegramMessageEditedEvent)
        assert edited_event.message.telegram_post.text == "edited post"
        assert edited_event.message.telegram_post.edited_at == edited_at

        deleted_event_task = asyncio.ensure_future(anext(stream))
        await deleted_handler(
            SimpleNamespace(
                chat_id=-1_000_000_000_123,
                deleted_ids=[20, 21],
            )
        )
        deleted_event = await asyncio.wait_for(deleted_event_task, timeout=1.0)
        assert deleted_event == TelegramMessagesDeletedEvent(
            channel_id="tracked-channel",
            post_ids=("20", "21"),
        )
    finally:
        await cast("Any", stream).aclose()

    assert underlying_client.removed_handlers == [
        new_handler,
        edited_handler,
        deleted_handler,
    ]
    assert client._live_handlers == []
    assert client._live_queue is None


async def test_telethon_exhausted_request_error_is_retryable_and_evicts_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    underlying_client = _FailingSingleMessageClient(
        error=RuntimeError("Request was unsuccessful 6 time(s)"),
    )
    factory = _EvictingSingleMessageFactory(
        client=underlying_client,
        settings=_settings(),
    )
    client = PipelineTelethonClient(
        factory=cast("Any", factory),
        rate_limiter=_RateLimiter(max_requests_per_second=10.0),
    )

    async def _resolve_entity(_self: PipelineTelethonClient, _channel_id: str) -> _FakeChannel:
        return _FakeChannel()

    monkeypatch.setattr(PipelineTelethonClient, "_resolve_entity", _resolve_entity)

    with pytest.raises(PipelineTelegramProviderUnavailableError, match="exhausted"):
        _ = await client.fetch_single_message(channel_id="123", post_id="11")

    assert factory.invalidated_clients == [underlying_client]
