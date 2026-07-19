"""Integration tests for the multi-session TelegramSessionManager."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
    RawTelegramMessage,
    TelegramLiveEvent,
    TelegramNewMessageEvent,
)
from memexpert.crawlers.telegram.manager import TelegramSessionManager
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.models.content import PipelineIngestRequest, SourceChannel, SourceChannelBackfillJob, TelegramSession
from memexpert.models.enums import (
    SourceChannelBackfillJobStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.models.operations import SourceChannelBackfillAttempt
from memexpert.pipeline.events import SourceEngagementCaptureRequestedEvent, build_source_engagement_session_key
from memexpert.schemas.content_pipeline import CrawlerIngestOutcome
from memexpert.services import CrawlerSessionNotRunnableError
from tests.integration.test_ingest_accept_service import FakeStorageClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_LIVE_LISTENER_TEST_TIMEOUT_SECONDS = 10.0


async def _await_live_listener_events_or_completion(
    *,
    events: tuple[asyncio.Event, ...],
    listener_tasks: tuple[asyncio.Task[None], ...],
) -> None:
    """Wait for fake lifecycle events while surfacing premature listener failures."""

    async def _wait_for_all_events() -> None:
        await asyncio.gather(*(event.wait() for event in events))

    event_task = asyncio.create_task(_wait_for_all_events())
    try:
        done, _ = await asyncio.wait(
            (*listener_tasks, event_task),
            timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        completed_listener_tasks = tuple(task for task in done if task is not event_task)
        if completed_listener_tasks:
            results = await asyncio.gather(*completed_listener_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            raise AssertionError("Live listener completed before the fake lifecycle event.")
        if event_task in done:
            await event_task
            return
        raise AssertionError("Live listener did not reach the fake lifecycle event before the watchdog elapsed.")
    finally:
        if not event_task.done():
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)


async def test_live_listener_event_wait_surfaces_task_error_before_watchdog() -> None:
    event = asyncio.Event()

    async def _fails() -> None:
        raise RuntimeError("listener failed")

    task = asyncio.create_task(_fails())

    with pytest.raises(RuntimeError, match="listener failed"):
        await _await_live_listener_events_or_completion(
            events=(event,),
            listener_tasks=(task,),
        )


async def test_live_listener_event_wait_rejects_early_normal_completion() -> None:
    event = asyncio.Event()

    async def _completes() -> None:
        return None

    task = asyncio.create_task(_completes())

    with pytest.raises(AssertionError, match="completed before the fake lifecycle event"):
        await _await_live_listener_events_or_completion(
            events=(event,),
            listener_tasks=(task,),
        )


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _build_photo_message(*, message_id: str, channel_id: str) -> RawTelegramMessage:
    return RawTelegramMessage(
        message_id=message_id,
        channel_id=channel_id,
        channel_username=None,
        channel_title=f"{channel_id} title",
        published_at=_now(),
        media_type="photo",
        views=7,
        reactions={},
        forward=None,
    )


async def _seed_session(
    session: AsyncSession,
    *,
    session_name: str,
    status: TelegramSessionStatus = TelegramSessionStatus.ACTIVE,
    enabled: bool = True,
    encrypted_string_session: str | None = "encrypted-string-session",
    flood_wait_until: datetime | None = None,
    catchup_enabled: bool = True,
    live_enabled: bool = True,
    engagement_enabled: bool = True,
) -> TelegramSession:
    row = TelegramSession(
        name=session_name,
        display_name=session_name.title(),
        status=status,
        enabled=enabled,
        encrypted_string_session=encrypted_string_session,
        flood_wait_until=flood_wait_until,
        catchup_enabled=catchup_enabled,
        live_enabled=live_enabled,
        engagement_enabled=engagement_enabled,
        last_heartbeat_at=_now(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _seed_channel(
    session: AsyncSession,
    *,
    platform_id: str,
    session_name: str | None,
    is_active: bool = True,
    is_paused: bool = False,
    catchup_enabled: bool = True,
    live_enabled: bool = True,
    last_read_post_id: str | None = None,
) -> SourceChannel:
    telegram_session_id = None
    if session_name is not None:
        telegram_session_id = await session.scalar(
            select(TelegramSession.id).where(TelegramSession.name == session_name),
        )
        assert telegram_session_id is not None
    row = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id=platform_id,
        title=f"{platform_id} title",
        is_active=is_active,
        is_paused=is_paused,
        catchup_enabled=catchup_enabled,
        live_enabled=live_enabled,
        telegram_session_id=telegram_session_id,
        last_read_post_id=last_read_post_id,
        initial_catchup_completed=last_read_post_id is not None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _build_manager(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clients_by_name: dict[str, FakeTelegramClient],
    created_for: list[str] | None = None,
    storage_client: FakeStorageClient | None = None,
) -> TelegramSessionManager:
    storage = storage_client or FakeStorageClient()

    def _client_factory(row: TelegramSession) -> FakeTelegramClient:
        if created_for is not None:
            created_for.append(row.name)
        return clients_by_name[row.name]

    return TelegramSessionManager(
        settings=Settings(),
        session_factory=session_factory,
        telegram_client_factory=_client_factory,
        ingest_service_factory=lambda db_session: PipelineCrawlerIngestService.from_settings(
            db_session,
            settings=Settings(),
            storage_client=storage,
        ),
    )


def _source_engagement_event_for_session(
    row: TelegramSession,
    *,
    session_name: str | None = None,
) -> SourceEngagementCaptureRequestedEvent:
    resolved_session_name = session_name or row.name
    return SourceEngagementCaptureRequestedEvent(
        event_id=uuid.uuid7(),
        event_type="source_engagement_capture_requested",
        meme_source_id=uuid.uuid7(),
        source_platform=SourcePlatform.TELEGRAM,
        source_id="stats-channel",
        post_id="100",
        scheduled_for=_now(),
        schedule_label=SourceEngagementScheduleLabel.PLUS_1H,
        telegram_session_id=row.id,
        session_name=resolved_session_name,
        session_key=build_source_engagement_session_key(row.id, resolved_session_name),
        created_at=_now(),
    )


async def test_manager_catch_up_all_uses_one_cached_client_per_runnable_session(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="alpha")
    await _seed_session(migrated_db_session, session_name="beta")
    await _seed_session(migrated_db_session, session_name="stopped", status=TelegramSessionStatus.STOPPED)
    await _seed_session(migrated_db_session, session_name="no_secret", encrypted_string_session="   ")
    await _seed_session(migrated_db_session, session_name="disabled", enabled=False)
    await _seed_session(
        migrated_db_session,
        session_name="future_flood",
        flood_wait_until=_now() + timedelta(minutes=10),
    )
    await _seed_channel(migrated_db_session, platform_id="alpha_one", session_name="alpha")
    await _seed_channel(migrated_db_session, platform_id="alpha_two", session_name="alpha")
    await _seed_channel(migrated_db_session, platform_id="beta_one", session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="alpha_paused", session_name="alpha", is_paused=True)
    await _seed_channel(migrated_db_session, platform_id="alpha_cold", session_name="alpha", catchup_enabled=False)
    await _seed_channel(migrated_db_session, platform_id="orphan", session_name=None)
    await _seed_channel(migrated_db_session, platform_id="stopped_channel", session_name="stopped")
    await _seed_channel(migrated_db_session, platform_id="no_secret_channel", session_name="no_secret")
    await _seed_channel(migrated_db_session, platform_id="disabled_channel", session_name="disabled")
    await _seed_channel(migrated_db_session, platform_id="future_flood_channel", session_name="future_flood")

    alpha_messages = {
        "alpha_one": [_build_photo_message(message_id="101", channel_id="alpha_one")],
        "alpha_two": [_build_photo_message(message_id="201", channel_id="alpha_two")],
        "alpha_paused": [_build_photo_message(message_id="301", channel_id="alpha_paused")],
        "alpha_cold": [_build_photo_message(message_id="401", channel_id="alpha_cold")],
    }
    beta_messages = {"beta_one": [_build_photo_message(message_id="501", channel_id="beta_one")]}
    alpha = FakeTelegramClient(
        canned_messages=alpha_messages,
        media_by_message={"101": b"alpha-one", "201": b"alpha-two", "301": b"paused", "401": b"cold"},
    )
    beta = FakeTelegramClient(canned_messages=beta_messages, media_by_message={"501": b"beta-one"})
    skipped = FakeTelegramClient(
        canned_messages={
            "orphan": [_build_photo_message(message_id="601", channel_id="orphan")],
            "stopped_channel": [_build_photo_message(message_id="701", channel_id="stopped_channel")],
            "no_secret_channel": [_build_photo_message(message_id="801", channel_id="no_secret_channel")],
            "disabled_channel": [_build_photo_message(message_id="901", channel_id="disabled_channel")],
            "future_flood_channel": [_build_photo_message(message_id="1001", channel_id="future_flood_channel")],
        },
        media_by_message={"601": b"orphan", "701": b"stopped", "801": b"secret", "901": b"disabled"},
    )
    created_for: list[str] = []
    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={
            "alpha": alpha,
            "beta": beta,
            "stopped": skipped,
            "no_secret": skipped,
            "disabled": skipped,
            "future_flood": skipped,
        },
        created_for=created_for,
    )

    reports = await manager.catch_up_all()

    assert [(report.session_name, report.channel_id) for report in reports] == [
        ("alpha", "alpha_one"),
        ("alpha", "alpha_two"),
        ("beta", "beta_one"),
    ]
    assert created_for == ["alpha", "beta"]
    assert alpha.downloaded_message_ids == ["101", "201"]
    assert beta.downloaded_message_ids == ["501"]
    assert skipped.downloaded_message_ids == []
    assert await migrated_db_session.scalar(select(func.count()).select_from(PipelineIngestRequest)) == 3


async def test_manager_source_engagement_client_for_event_reuses_cached_client_and_validates_session(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_a = await _seed_session(migrated_db_session, session_name="session-a")
    session_b = await _seed_session(migrated_db_session, session_name="session-b")
    engagement_disabled = await _seed_session(
        migrated_db_session,
        session_name="engagement-disabled",
        engagement_enabled=False,
    )
    flood_waited = await _seed_session(
        migrated_db_session,
        session_name="flood-waited",
        status=TelegramSessionStatus.FLOOD_WAIT,
        flood_wait_until=_now() + timedelta(minutes=10),
    )
    session_a_client = FakeTelegramClient()
    session_b_client = FakeTelegramClient()
    skipped = FakeTelegramClient()
    created_for: list[str] = []
    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={
            "session-a": session_a_client,
            "session-b": session_b_client,
            "engagement-disabled": skipped,
            "flood-waited": skipped,
        },
        created_for=created_for,
    )

    first = await manager.source_engagement_client_for_event(_source_engagement_event_for_session(session_a))
    second = await manager.source_engagement_client_for_event(_source_engagement_event_for_session(session_a))
    other_session = await manager.source_engagement_client_for_event(_source_engagement_event_for_session(session_b))

    assert first is session_a_client
    assert second is session_a_client
    assert other_session is session_b_client
    assert created_for == ["session-a", "session-b"]
    assert session_a_client.closed is False
    assert session_b_client.closed is False

    with pytest.raises(PipelineTelegramSessionNotRunnableError):
        _ = await manager.source_engagement_client_for_event(
            _source_engagement_event_for_session(session_a, session_name="renamed-session"),
        )
    with pytest.raises(PipelineTelegramSessionNotRunnableError):
        _ = await manager.source_engagement_client_for_event(_source_engagement_event_for_session(engagement_disabled))
    with pytest.raises(PipelineTelegramFloodWaitError):
        _ = await manager.source_engagement_client_for_event(_source_engagement_event_for_session(flood_waited))

    assert skipped.closed is False
    await manager.shutdown()
    assert session_a_client.closed is True
    assert session_b_client.closed is True


async def test_manager_flood_wait_parks_one_session_and_continues_healthy_session(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    flood_row = await _seed_session(migrated_db_session, session_name="flood")
    healthy_row = await _seed_session(migrated_db_session, session_name="healthy")
    await _seed_channel(migrated_db_session, platform_id="flood_channel", session_name="flood")
    await _seed_channel(migrated_db_session, platform_id="healthy_channel", session_name="healthy")

    class _FloodOnDownload(FakeTelegramClient):
        async def download_media(self, message: RawTelegramMessage) -> bytes:
            self.downloaded_message_ids.append(message.message_id)
            raise PipelineTelegramFloodWaitError("cooldown", wait_seconds=120)

    flood = _FloodOnDownload(
        canned_messages={"flood_channel": [_build_photo_message(message_id="1", channel_id="flood_channel")]},
    )
    healthy = FakeTelegramClient(
        canned_messages={"healthy_channel": [_build_photo_message(message_id="2", channel_id="healthy_channel")]},
        media_by_message={"2": b"healthy"},
    )
    manager = _build_manager(postgres_session_factory, clients_by_name={"flood": flood, "healthy": healthy})

    reports = await manager.catch_up_all()

    assert [(report.session_name, report.channel_id) for report in reports] == [
        ("flood", "flood_channel"),
        ("healthy", "healthy_channel"),
    ]
    assert any("flood_wait" in error for error in reports[0].errors)
    await migrated_db_session.refresh(flood_row)
    await migrated_db_session.refresh(healthy_row)
    assert flood_row.status is TelegramSessionStatus.FLOOD_WAIT
    assert flood_row.flood_wait_until is not None
    assert healthy_row.status is TelegramSessionStatus.ACTIVE
    assert healthy.downloaded_message_ids == ["2"]
    assert flood.closed is True
    assert healthy.closed is False


async def test_manager_auth_required_session_does_not_stop_healthy_catchup(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    auth_row = await _seed_session(migrated_db_session, session_name="auth")
    healthy_row = await _seed_session(migrated_db_session, session_name="healthy")
    await _seed_channel(migrated_db_session, platform_id="auth_channel", session_name="auth")
    await _seed_channel(migrated_db_session, platform_id="healthy_auth_channel", session_name="healthy")

    class _AuthOnIter(FakeTelegramClient):
        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            _ = (channel_id, limit)
            raise PipelineTelegramSessionAuthRequiredError("auth key revoked")
            yield  # pragma: no cover - keeps this an async generator

    auth = _AuthOnIter(
        canned_messages={"auth_channel": [_build_photo_message(message_id="1", channel_id="auth_channel")]},
    )
    healthy = FakeTelegramClient(
        canned_messages={
            "healthy_auth_channel": [_build_photo_message(message_id="2", channel_id="healthy_auth_channel")],
        },
        media_by_message={"2": b"healthy-auth"},
    )
    manager = _build_manager(postgres_session_factory, clients_by_name={"auth": auth, "healthy": healthy})

    reports = await manager.catch_up_all()

    assert [(report.session_name, report.channel_id) for report in reports] == [("healthy", "healthy_auth_channel")]
    await migrated_db_session.refresh(auth_row)
    await migrated_db_session.refresh(healthy_row)
    assert auth_row.status is TelegramSessionStatus.AUTH_REQUIRED
    assert healthy_row.status is TelegramSessionStatus.ACTIVE
    assert auth.closed is True
    assert healthy.downloaded_message_ids == ["2"]


async def test_manager_banned_session_quarantines_and_does_not_stop_healthy_catchup(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    banned_row = await _seed_session(migrated_db_session, session_name="banned")
    healthy_row = await _seed_session(migrated_db_session, session_name="healthy")
    await _seed_channel(migrated_db_session, platform_id="banned_channel", session_name="banned")
    await _seed_channel(migrated_db_session, platform_id="healthy_banned_channel", session_name="healthy")

    class _BannedOnIter(FakeTelegramClient):
        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            _ = (channel_id, limit)
            raise PipelineTelegramSessionBannedError("session revoked")
            yield  # pragma: no cover - keeps this an async generator

    banned = _BannedOnIter(
        canned_messages={"banned_channel": [_build_photo_message(message_id="1", channel_id="banned_channel")]},
    )
    healthy = FakeTelegramClient(
        canned_messages={
            "healthy_banned_channel": [_build_photo_message(message_id="2", channel_id="healthy_banned_channel")],
        },
        media_by_message={"2": b"healthy-after-banned"},
    )
    manager = _build_manager(postgres_session_factory, clients_by_name={"banned": banned, "healthy": healthy})

    reports = await manager.catch_up_all()

    assert [(report.session_name, report.channel_id) for report in reports] == [("healthy", "healthy_banned_channel")]
    await migrated_db_session.refresh(banned_row)
    await migrated_db_session.refresh(healthy_row)
    assert banned_row.status is TelegramSessionStatus.QUARANTINED
    assert banned_row.quarantined_at is not None
    assert healthy_row.status is TelegramSessionStatus.ACTIVE
    assert banned.closed is True
    assert healthy.downloaded_message_ids == ["2"]


async def test_manager_replay_uses_current_assignment_and_rejects_orphan_paused_and_leaked_channel(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="alpha")
    await _seed_session(migrated_db_session, session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="assigned_to_beta", session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="wrong_guard", session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="orphan_replay", session_name=None)
    await _seed_channel(migrated_db_session, platform_id="paused_replay", session_name="beta", is_paused=True)

    alpha = FakeTelegramClient()
    beta = FakeTelegramClient()
    beta.pin_single_message(
        channel_id="assigned_to_beta",
        post_id="42",
        message=_build_photo_message(message_id="42", channel_id="assigned_to_beta"),
        media=b"beta-replay",
    )
    beta.pin_single_message(
        channel_id="wrong_guard",
        post_id="7",
        message=_build_photo_message(message_id="7", channel_id="not_wrong_guard"),
    )
    created_for: list[str] = []
    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={"alpha": alpha, "beta": beta},
        created_for=created_for,
    )

    result = await manager.replay_post("assigned_to_beta", "42")

    assert result.outcome is CrawlerIngestOutcome.INGESTED
    assert created_for == ["beta"]
    assert beta.downloaded_message_ids == ["42"]
    assert alpha.downloaded_message_ids == []

    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await manager.replay_post("orphan_replay", "1")
    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await manager.replay_post("paused_replay", "1")
    with pytest.raises(PipelineTelegramMalformedMessageError):
        _ = await manager.replay_post("wrong_guard", "7")


async def test_manager_live_listeners_scope_channels_and_disable_invalidates_stale_client(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    alpha_row = await _seed_session(migrated_db_session, session_name="alpha")
    beta_row = await _seed_session(migrated_db_session, session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="alpha_live", session_name="alpha")
    await _seed_channel(migrated_db_session, platform_id="alpha_paused_live", session_name="alpha", is_paused=True)
    await _seed_channel(
        migrated_db_session,
        platform_id="alpha_disabled_live",
        session_name="alpha",
        live_enabled=False,
    )
    await _seed_channel(migrated_db_session, platform_id="beta_live", session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="orphan_live", session_name=None)

    class _LeakyLiveClient(FakeTelegramClient):
        listened_channel_ids: list[tuple[str, ...]]
        unbound_message_guard_completed: asyncio.Event

        def __init__(
            self,
            *,
            live_messages: dict[str, list[RawTelegramMessage]],
            media_by_message: dict[str, bytes],
        ) -> None:
            super().__init__(live_messages=live_messages, media_by_message=media_by_message)
            self.listened_channel_ids = []
            self.unbound_message_guard_completed = asyncio.Event()

        async def listen_live(
            self,
            *,
            channel_ids: Sequence[str],
            ready_event: asyncio.Event | None = None,
        ) -> AsyncIterator[TelegramLiveEvent]:
            self.listened_channel_ids.append(tuple(channel_ids))
            if ready_event is not None:
                ready_event.set()
            for channel_id in channel_ids:
                for message in self.live_messages.get(channel_id, []):
                    yield TelegramNewMessageEvent(message=message)
            yield TelegramNewMessageEvent(
                message=_build_photo_message(message_id="leak", channel_id="orphan_live"),
            )
            # Resuming after the deliberately unbound yield proves the runtime
            # ran the channel guard and requested the next stream item.
            self.unbound_message_guard_completed.set()
            while True:
                await asyncio.sleep(60)

    alpha = _LeakyLiveClient(
        live_messages={"alpha_live": [_build_photo_message(message_id="101", channel_id="alpha_live")]},
        media_by_message={"101": b"alpha-live", "leak": b"leak"},
    )
    beta = _LeakyLiveClient(
        live_messages={"beta_live": [_build_photo_message(message_id="201", channel_id="beta_live")]},
        media_by_message={"201": b"beta-live", "leak": b"leak"},
    )
    manager = _build_manager(postgres_session_factory, clients_by_name={"alpha": alpha, "beta": beta})

    try:
        await manager.start_live_all()
        alpha_handle = manager._live_handles[alpha_row.id]  # noqa: SLF001 - test verifies manager lifecycle.
        beta_handle = manager._live_handles[beta_row.id]  # noqa: SLF001 - test verifies manager lifecycle.
        alpha_task = alpha_handle.runtime._live_tasks["alpha"]  # noqa: SLF001 - test verifies manager lifecycle.
        beta_task = beta_handle.runtime._live_tasks["beta"]  # noqa: SLF001 - test verifies manager lifecycle.
        await _await_live_listener_events_or_completion(
            events=(alpha.unbound_message_guard_completed, beta.unbound_message_guard_completed),
            listener_tasks=(alpha_task, beta_task),
        )

        assert alpha.listened_channel_ids == [("alpha_live",)]
        assert beta.listened_channel_ids == [("beta_live",)]
        assert alpha.downloaded_message_ids == ["101"]
        assert beta.downloaded_message_ids == ["201"]
        assert "leak" not in alpha.downloaded_message_ids
        assert "leak" not in beta.downloaded_message_ids

        alpha_row.enabled = False
        await migrated_db_session.commit()
        await manager.start_live_all()

        assert alpha.closed is True
        assert beta.closed is False
    finally:
        await manager.shutdown()

    assert beta.closed is True


async def test_manager_live_start_failure_does_not_prevent_other_sessions_starting(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="alpha")
    beta_row = await _seed_session(migrated_db_session, session_name="beta")
    await _seed_channel(migrated_db_session, platform_id="alpha_live", session_name="alpha")
    await _seed_channel(migrated_db_session, platform_id="beta_live", session_name="beta")

    class _BlockingLiveClient(FakeTelegramClient):
        def __init__(self) -> None:
            super().__init__()
            self.listener_started = asyncio.Event()

        async def listen_live(
            self,
            *,
            channel_ids: Sequence[str],
            ready_event: asyncio.Event | None = None,
        ) -> AsyncIterator[TelegramLiveEvent]:
            _ = tuple(channel_ids)
            if ready_event is not None:
                ready_event.set()
            self.listener_started.set()
            await asyncio.Event().wait()
            yield TelegramNewMessageEvent(  # pragma: no cover
                message=_build_photo_message(message_id="unreachable", channel_id="unreachable"),
            )

        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            assert self.listener_started.is_set()
            async for message in super().iter_latest_channel_messages(channel_id=channel_id, limit=limit):
                yield message

    beta_client = _BlockingLiveClient()

    def _client_factory(row: TelegramSession) -> FakeTelegramClient:
        if row.name == "alpha":
            raise RuntimeError("alpha connection failed")
        assert row.name == "beta"
        return beta_client

    manager = TelegramSessionManager(
        settings=Settings(),
        session_factory=postgres_session_factory,
        telegram_client_factory=_client_factory,
        ingest_service_factory=lambda db_session: PipelineCrawlerIngestService.from_settings(
            db_session,
            settings=Settings(),
            storage_client=FakeStorageClient(),
        ),
    )

    try:
        failed_session_names = await manager.start_live_all()

        assert failed_session_names == ["alpha"]
        assert beta_row.id in manager._live_handles  # noqa: SLF001 - verifies failure isolation.
        assert beta_client.closed is False
        await asyncio.wait_for(beta_client.listener_started.wait(), timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS)

        beta_handle = manager._live_handles[beta_row.id]  # noqa: SLF001 - verifies non-disruptive retry.
        retry_result = await manager.retry_incomplete()

        assert retry_result.failed_session_names == ("alpha",)
        assert manager._live_handles[beta_row.id] is beta_handle  # noqa: SLF001 - verifies non-disruptive retry.
        assert beta_client.closed is False
    finally:
        await manager.shutdown()

    assert beta_client.closed is True


async def test_manager_supervisor_restarts_completed_live_handle_and_closes_stale_client(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await _seed_session(migrated_db_session, session_name="alpha")
    await _seed_channel(migrated_db_session, platform_id="restart_live", session_name="alpha")
    created: list[FakeTelegramClient] = []

    class _ProviderErrorLiveClient(FakeTelegramClient):
        async def listen_live(
            self,
            *,
            channel_ids: Sequence[str],
            ready_event: asyncio.Event | None = None,
        ) -> AsyncIterator[TelegramLiveEvent]:
            _ = tuple(channel_ids)
            if ready_event is not None:
                ready_event.set()
            raise PipelineTelegramProviderUnavailableError("transient live failure")
            yield TelegramNewMessageEvent(  # pragma: no cover - keeps this an async generator
                message=_build_photo_message(message_id="unreachable", channel_id="unreachable"),
            )

    def _client_factory(row: TelegramSession) -> FakeTelegramClient:
        assert row.name == "alpha"
        if not created:
            client = _ProviderErrorLiveClient()
        else:
            client = FakeTelegramClient(
                live_messages={"restart_live": [_build_photo_message(message_id="2", channel_id="restart_live")]},
                media_by_message={"2": b"restart-live"},
            )
        created.append(client)
        return client

    manager = TelegramSessionManager(
        settings=Settings(),
        session_factory=postgres_session_factory,
        telegram_client_factory=_client_factory,
        ingest_service_factory=lambda db_session: PipelineCrawlerIngestService.from_settings(
            db_session,
            settings=Settings(),
            storage_client=FakeStorageClient(),
        ),
    )

    try:
        await manager.start_live_session("alpha")
        initial_handle = manager._live_handles[session_row.id]  # noqa: SLF001 - test verifies manager lifecycle.
        initial_task = initial_handle.runtime._live_tasks["alpha"]  # noqa: SLF001 - test verifies manager lifecycle.
        await asyncio.wait_for(
            asyncio.shield(initial_task),
            timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS,
        )

        assert len(created) == 1
        assert created[0].closed is False

        failed_session_names = await manager.supervise_live_listeners()
        restarted_handle = manager._live_handles[session_row.id]  # noqa: SLF001 - test verifies manager lifecycle.
        restarted_task = restarted_handle.runtime._live_tasks["alpha"]  # noqa: SLF001 - test verifies manager lifecycle.
        await asyncio.wait_for(
            asyncio.shield(restarted_task),
            timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS,
        )

        assert len(created) == 2
        assert failed_session_names == ()
        assert created[0].closed is True
        assert created[1].closed is False
        assert created[1].downloaded_message_ids == ["2"]
        await migrated_db_session.refresh(session_row)
        assert session_row.status is TelegramSessionStatus.ACTIVE
    finally:
        await manager.shutdown()

    assert created[1].closed is True


async def test_manager_configuration_snapshot_ignores_runtime_state_and_detects_control_changes(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await _seed_session(migrated_db_session, session_name="alpha")
    channel = await _seed_channel(migrated_db_session, platform_id="snapshot_channel", session_name="alpha")
    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={"alpha": FakeTelegramClient()},
    )

    initial_snapshot = await manager.configuration_snapshot()

    session_row.last_heartbeat_at = _now() + timedelta(minutes=1)
    channel.last_read_post_id = "42"
    channel.last_fetched_at = _now()
    channel.title = "Refreshed title"
    channel.subscriber_count = 500
    await migrated_db_session.commit()

    assert await manager.configuration_snapshot() == initial_snapshot

    channel.live_enabled = False
    await migrated_db_session.commit()

    assert await manager.configuration_snapshot() != initial_snapshot


async def test_manager_reload_catches_up_source_added_after_live_start_and_rebuilds_listener(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_row = await _seed_session(migrated_db_session, session_name="alpha")
    await _seed_channel(
        migrated_db_session,
        platform_id="old_source",
        session_name="alpha",
        last_read_post_id="10",
    )
    created: list[FakeTelegramClient] = []
    listeners_ready = asyncio.Event()

    class _RecordingLiveClient(FakeTelegramClient):
        listened_channel_ids: list[tuple[str, ...]]
        listener_started: asyncio.Event

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.listened_channel_ids = []
            self.listener_started = asyncio.Event()

        async def listen_live(
            self,
            *,
            channel_ids: Sequence[str],
            ready_event: asyncio.Event | None = None,
        ) -> AsyncIterator[TelegramLiveEvent]:
            self.listened_channel_ids.append(tuple(channel_ids))
            if ready_event is not None:
                ready_event.set()
            self.listener_started.set()
            await asyncio.Event().wait()
            yield TelegramNewMessageEvent(  # pragma: no cover
                message=_build_photo_message(message_id="unreachable", channel_id="unreachable"),
            )

        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            assert self.listener_started.is_set()
            assert listeners_ready.is_set()
            async for message in super().iter_latest_channel_messages(channel_id=channel_id, limit=limit):
                yield message

    def _client_factory(row: TelegramSession) -> FakeTelegramClient:
        assert row.name == "alpha"
        if not created:
            client = _RecordingLiveClient()
        else:
            client = _RecordingLiveClient(
                canned_messages={
                    "old_source": [_build_photo_message(message_id="10", channel_id="old_source")],
                    "new_source": [_build_photo_message(message_id="20", channel_id="new_source")],
                },
                media_by_message={"10": b"old-source", "20": b"new-source"},
            )
        created.append(client)
        return client

    manager = TelegramSessionManager(
        settings=Settings(),
        session_factory=postgres_session_factory,
        telegram_client_factory=_client_factory,
        ingest_service_factory=lambda db_session: PipelineCrawlerIngestService.from_settings(
            db_session,
            settings=Settings(),
            storage_client=FakeStorageClient(),
        ),
    )

    try:
        await manager.start_live_all()
        first_client = created[0]
        assert isinstance(first_client, _RecordingLiveClient)
        await asyncio.wait_for(first_client.listener_started.wait(), timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS)
        assert first_client.listened_channel_ids == [("old_source",)]

        new_channel = await _seed_channel(
            migrated_db_session,
            platform_id="new_source",
            session_name="alpha",
        )

        reload_result = await manager.reload(on_listeners_ready=listeners_ready.set)

        assert listeners_ready.is_set()
        assert created[0].closed is True
        assert len(created) == 2
        second_client = created[1]
        assert isinstance(second_client, _RecordingLiveClient)
        await asyncio.wait_for(second_client.listener_started.wait(), timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS)
        assert set(second_client.listened_channel_ids[-1]) == {"old_source", "new_source"}
        assert second_client.downloaded_message_ids == ["20"]
        assert {report.channel_id for report in reload_result.catchup_reports} == {"old_source", "new_source"}
        assert reload_result.retry_required is False
        await migrated_db_session.refresh(new_channel)
        assert new_channel.last_read_post_id == "20"
        assert new_channel.last_fetched_at is not None
        assert await migrated_db_session.scalar(select(func.count()).select_from(PipelineIngestRequest)) == 1
        await migrated_db_session.refresh(session_row)
        assert session_row.live_listener_started_at is not None
    finally:
        await manager.shutdown()

    assert created[1].closed is True


async def test_manager_reuses_invalidates_reloads_and_shutdowns_cached_clients(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="alpha")
    await _seed_channel(migrated_db_session, platform_id="alpha_cache", session_name="alpha", live_enabled=False)
    created: list[FakeTelegramClient] = []

    def _client_factory(row: TelegramSession) -> FakeTelegramClient:
        assert row.name == "alpha"
        client = FakeTelegramClient(
            canned_messages={"alpha_cache": [_build_photo_message(message_id="1", channel_id="alpha_cache")]},
            media_by_message={"1": b"alpha-cache"},
        )
        created.append(client)
        return client

    manager = TelegramSessionManager(
        settings=Settings(),
        session_factory=postgres_session_factory,
        telegram_client_factory=_client_factory,
        ingest_service_factory=lambda db_session: PipelineCrawlerIngestService.from_settings(
            db_session,
            settings=Settings(),
            storage_client=FakeStorageClient(),
        ),
    )

    _ = await manager.catch_up_session("alpha")
    _ = await manager.catch_up_session("alpha")
    assert len(created) == 1
    assert created[0].closed is False

    await manager.invalidate_session(session_name="alpha")
    assert created[0].closed is True

    _ = await manager.catch_up_session("alpha")
    assert len(created) == 2
    assert created[1].closed is False

    await manager.reload()
    assert created[1].closed is True

    _ = await manager.catch_up_session("alpha")
    assert len(created) == 3
    await manager.shutdown()
    assert created[2].closed is True


async def test_manager_processes_older_backfill_without_stopping_live_listener(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="backfill")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="backfill_channel",
        session_name="backfill",
        last_read_post_id="10",
    )
    channel.oldest_observed_post_id = "8"
    channel.history_cursor_post_id = "8"
    channel.initial_catchup_completed = True
    job = SourceChannelBackfillJob(
        source_channel_id=channel.id,
        requested_message_count=7,
    )
    migrated_db_session.add(job)
    await migrated_db_session.commit()

    class _ConcurrentBackfillClient(FakeTelegramClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.listener_started = asyncio.Event()
            self.release_listener = asyncio.Event()

        async def listen_live(
            self,
            *,
            channel_ids: Sequence[str],
            ready_event: asyncio.Event | None = None,
        ) -> AsyncIterator[TelegramLiveEvent]:
            _ = channel_ids
            if ready_event is not None:
                ready_event.set()
            self.listener_started.set()
            await self.release_listener.wait()
            if False:  # pragma: no cover - preserves async-generator shape.
                yield TelegramNewMessageEvent(
                    message=_build_photo_message(message_id="unused", channel_id="backfill_channel"),
                )

    messages = [_build_photo_message(message_id=str(i), channel_id="backfill_channel") for i in range(1, 11)]
    client = _ConcurrentBackfillClient(
        canned_messages={"backfill_channel": messages},
        media_by_message={message.message_id: b"img" for message in messages},
    )
    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={"backfill": client},
    )

    try:
        await manager.start_live_session("backfill")
        await asyncio.wait_for(client.listener_started.wait(), timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS)
        live_handle = next(iter(manager._live_handles.values()))  # noqa: SLF001 - lifecycle assertion.
        live_task = live_handle.runtime._live_tasks["backfill"]  # noqa: SLF001 - lifecycle assertion.

        assert await manager.process_backfill_jobs() == 1

        await migrated_db_session.refresh(job)
        await migrated_db_session.refresh(channel)
        assert job.status is SourceChannelBackfillJobStatus.COMPLETED
        assert job.scanned_message_count == 7
        assert job.cursor_post_id == "1"
        assert job.last_error_text is None
        assert channel.last_read_post_id == "10"
        assert channel.oldest_observed_post_id == "1"
        assert channel.history_cursor_post_id == "1"
        assert live_task.done() is False
        assert client.closed is False
    finally:
        client.release_listener.set()
        await manager.shutdown()

    assert client.closed is True


async def test_manager_processes_only_one_backfill_page_per_lease(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="sliced-backfill")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="sliced_backfill_channel",
        session_name="sliced-backfill",
        last_read_post_id="102",
    )
    channel.oldest_observed_post_id = "102"
    channel.history_cursor_post_id = "102"
    channel.initial_catchup_completed = True
    job = SourceChannelBackfillJob(source_channel_id=channel.id, requested_message_count=101)
    migrated_db_session.add(job)
    await migrated_db_session.commit()
    messages = [
        _build_photo_message(message_id=str(index), channel_id="sliced_backfill_channel") for index in range(1, 103)
    ]
    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={
            "sliced-backfill": FakeTelegramClient(
                canned_messages={"sliced_backfill_channel": messages},
                media_by_message={message.message_id: b"img" for message in messages},
            )
        },
    )

    assert await manager.process_backfill_jobs() == 1
    await migrated_db_session.refresh(job)
    assert job.status is SourceChannelBackfillJobStatus.QUEUED
    assert job.scanned_message_count == 100
    assert job.attempt_count == 1
    assert job.lease_generation == 1

    assert await manager.process_backfill_jobs() == 1
    await migrated_db_session.refresh(job)
    assert job.status is SourceChannelBackfillJobStatus.COMPLETED
    assert job.scanned_message_count == 101
    assert job.attempt_count == 1
    assert job.lease_generation == 2


async def test_manager_stops_automatic_backfill_retries_after_five_attempts(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(migrated_db_session, session_name="bounded-backfill")
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="bounded_backfill_channel",
        session_name="bounded-backfill",
        last_read_post_id="10",
    )
    channel.oldest_observed_post_id = "8"
    channel.history_cursor_post_id = "8"
    channel.initial_catchup_completed = True
    job = SourceChannelBackfillJob(source_channel_id=channel.id, requested_message_count=5)
    migrated_db_session.add(job)
    await migrated_db_session.commit()

    class _UnavailableHistoryClient(FakeTelegramClient):
        async def iter_older_channel_messages(
            self,
            *,
            channel_id: str,
            before_message_id: int,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            _ = (channel_id, before_message_id, limit)
            raise PipelineTelegramProviderUnavailableError("history provider unavailable")
            yield  # pragma: no cover - preserves async-generator shape.

    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={"bounded-backfill": _UnavailableHistoryClient()},
    )

    for attempt_number in range(1, 6):
        assert await manager.process_backfill_jobs() == 1
        await migrated_db_session.refresh(job)
        assert job.attempt_count == attempt_number
        if attempt_number < 5:
            assert job.status is SourceChannelBackfillJobStatus.WAITING_RETRY
            job.next_attempt_at = _now() - timedelta(seconds=1)
            await migrated_db_session.commit()

    assert job.status is SourceChannelBackfillJobStatus.FAILED
    assert job.is_retryable is True
    assert job.next_attempt_at is None
    assert job.last_error_code == "provider_unavailable"
    attempts = (
        (
            await migrated_db_session.execute(
                select(SourceChannelBackfillAttempt)
                .where(SourceChannelBackfillAttempt.backfill_job_id == job.id)
                .order_by(SourceChannelBackfillAttempt.attempt_number)
            )
        )
        .scalars()
        .all()
    )
    assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3, 4, 5]
    assert all(attempt.finished_at is not None for attempt in attempts)


async def test_manager_persists_backfill_failure_after_processing_session_rollback(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(
        migrated_db_session,
        session_name="disabled-backfill",
        catchup_enabled=False,
    )
    channel = await _seed_channel(
        migrated_db_session,
        platform_id="disabled_backfill_channel",
        session_name="disabled-backfill",
        last_read_post_id="10",
    )
    channel.history_cursor_post_id = "8"
    job = SourceChannelBackfillJob(
        source_channel_id=channel.id,
        requested_message_count=5,
    )
    migrated_db_session.add(job)
    await migrated_db_session.commit()

    manager = _build_manager(
        postgres_session_factory,
        clients_by_name={"disabled-backfill": FakeTelegramClient()},
    )

    assert await manager.process_backfill_jobs() == 1

    await migrated_db_session.refresh(job)
    assert job.status is SourceChannelBackfillJobStatus.FAILED
    assert job.last_error_text is not None
    assert "catch-up disabled" in job.last_error_text
    assert job.locked_at is None
    assert job.lock_owner is None
