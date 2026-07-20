"""Focused coverage for Telegram channel audience scheduling and capture."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    RawTelegramChannelAudience,
)
from memexpert.models.content import (
    RabbitMQOutboxMessage,
    SourceChannel,
    SourceChannelAudienceSnapshot,
    TelegramSession,
)
from memexpert.models.enums import (
    RabbitMQOutboxMessageStatus,
    SourceChannelAudienceCaptureReason,
    SourceChannelAudienceFetchStatus,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.pipeline.events import (
    PIPELINE_SOURCE_CHANNEL_AGGREGATE_TYPE,
    SOURCE_CHANNEL_AUDIENCE_CAPTURE_REQUESTED_EVENT_TYPE,
    SourceChannelAudienceCaptureRequestedEvent,
    build_source_channel_audience_capture_requested_payload,
    build_source_channel_audience_session_key,
)
from memexpert.services.source_channel_audience import (
    SourceChannelAudienceObservation,
    next_daily_source_channel_audience_capture_at,
    record_source_channel_audience_observation,
    source_channel_audience_observation_from_count,
)
from memexpert.services.source_channel_audience_capture import capture_source_channel_audience_request
from memexpert.services.source_channel_audience_scheduler import (
    run_scheduler_source_channel_audience_capture_batch,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_audience_observation_preserves_zero_and_rejects_invalid_shapes() -> None:
    zero = source_channel_audience_observation_from_count(0)
    missing = source_channel_audience_observation_from_count(None)

    assert zero.fetch_status is SourceChannelAudienceFetchStatus.SUCCESS
    assert zero.subscriber_count == 0
    assert missing.fetch_status is SourceChannelAudienceFetchStatus.NOT_EXPOSED
    assert missing.subscriber_count is None
    with pytest.raises(ValueError, match="non-negative"):
        _ = source_channel_audience_observation_from_count(-1)
    with pytest.raises(ValueError, match="require subscriber_count"):
        _ = SourceChannelAudienceObservation(fetch_status=SourceChannelAudienceFetchStatus.SUCCESS)


def test_audience_daily_slot_is_stable_and_always_on_the_next_utc_day() -> None:
    channel_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    captured_at = datetime(2026, 1, 1, 23, 59, tzinfo=UTC)

    first = next_daily_source_channel_audience_capture_at(channel_id, after=captured_at)
    second = next_daily_source_channel_audience_capture_at(channel_id, after=captured_at)

    assert first == second
    assert first.date() == date(2026, 1, 2)
    assert first.tzinfo is UTC


def test_audience_event_contract_is_session_affined() -> None:
    telegram_session_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    payload = build_source_channel_audience_capture_requested_payload(
        event_id=uuid.uuid7(),
        source_channel_id=uuid.uuid7(),
        source_platform=SourcePlatform.TELEGRAM,
        platform_id="public_memes",
        scheduled_for=created_at,
        capture_slot=created_at.date(),
        capture_reason=SourceChannelAudienceCaptureReason.SCHEDULED,
        telegram_session_id=telegram_session_id,
        session_name="Session A / primary",
        created_at=created_at,
    )

    event = SourceChannelAudienceCaptureRequestedEvent.model_validate(payload)
    assert event.session_key == build_source_channel_audience_session_key(
        telegram_session_id,
        "Session A / primary",
    )
    assert re.fullmatch(r"[A-Za-z0-9._-]+", event.session_key)


async def test_audience_scheduler_claims_only_due_runnable_channels_and_writes_outbox(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    telegram_session = TelegramSession(
        name="audience-session",
        display_name="Audience Session",
        status=TelegramSessionStatus.ACTIVE,
    )
    migrated_db_session.add(telegram_session)
    await migrated_db_session.flush()
    due = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id="due_channel",
        username="due_channel",
        title="Due",
        telegram_session_id=telegram_session.id,
        next_audience_capture_at=now - timedelta(hours=1),
    )
    future = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id="future_channel",
        username="future_channel",
        title="Future",
        telegram_session_id=telegram_session.id,
        next_audience_capture_at=now + timedelta(hours=1),
    )
    paused = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id="paused_channel",
        username="paused_channel",
        title="Paused",
        telegram_session_id=telegram_session.id,
        next_audience_capture_at=now - timedelta(hours=1),
        is_paused=True,
    )
    migrated_db_session.add_all((due, future, paused))
    await migrated_db_session.commit()

    result = await run_scheduler_source_channel_audience_capture_batch(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "scheduler_source_channel_audience_capture_batch_size": 10,
                "scheduler_source_channel_audience_capture_per_session_batch_size": 5,
            }
        ),
        now=now,
        lock_owner="audience-test-scheduler",
    )

    assert result.claimed == 1
    assert result.enqueued == 1
    assert result.source_channel_ids == (due.id,)
    async with postgres_session_factory() as session:
        due_row = await session.get(SourceChannel, due.id)
        outbox = await session.scalar(select(RabbitMQOutboxMessage))
    assert due_row is not None
    assert due_row.audience_capture_locked_at == now
    assert due_row.audience_capture_lock_owner == "audience-test-scheduler"
    assert due_row.audience_capture_attempt_count == 1
    assert outbox is not None
    assert outbox.status is RabbitMQOutboxMessageStatus.PENDING
    assert outbox.event_type == SOURCE_CHANNEL_AUDIENCE_CAPTURE_REQUESTED_EVENT_TYPE
    assert outbox.aggregate_type == PIPELINE_SOURCE_CHANNEL_AGGREGATE_TYPE
    assert outbox.aggregate_id == str(due.id)
    event = SourceChannelAudienceCaptureRequestedEvent.model_validate(outbox.payload)
    assert outbox.routing_key == f"pipeline.source_channel_audience_capture.{event.session_key}"
    assert event.source_channel_id == due.id
    assert event.capture_reason is SourceChannelAudienceCaptureReason.SCHEDULED


@pytest.mark.parametrize(
    ("subscriber_count", "expected_status", "expected_cache"),
    [
        (0, SourceChannelAudienceFetchStatus.SUCCESS, 0),
        (None, SourceChannelAudienceFetchStatus.NOT_EXPOSED, 500),
    ],
    ids=["known-zero", "not-exposed"],
)
async def test_audience_capture_records_observation_and_preserves_cache_semantics(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    subscriber_count: int | None,
    expected_status: SourceChannelAudienceFetchStatus,
    expected_cache: int,
) -> None:
    scheduled_for = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    telegram_session, channel = await _insert_channel(
        migrated_db_session,
        scheduled_for=scheduled_for,
        subscriber_count=500,
    )
    fake = FakeTelegramClient(
        canned_channel_audiences={
            channel.platform_id: RawTelegramChannelAudience(
                channel_id="123456",
                subscriber_count=subscriber_count,
            )
        }
    )
    event = _capture_event(telegram_session, channel, scheduled_for=scheduled_for)

    result = await capture_source_channel_audience_request(
        postgres_session_factory,
        event,
        telegram_client_factory=lambda _event: fake,
    )

    assert result.fetch_status is expected_status
    assert fake.audience_fetch_channel_ids == [channel.platform_id]
    assert fake.closed is True
    async with postgres_session_factory() as session:
        channel_row = await session.get(SourceChannel, channel.id)
        snapshot = await session.scalar(select(SourceChannelAudienceSnapshot))
    assert channel_row is not None
    assert snapshot is not None
    assert snapshot.fetch_status is expected_status
    assert snapshot.subscriber_count == subscriber_count
    assert channel_row.subscriber_count == expected_cache
    assert channel_row.audience_capture_locked_at is None
    assert channel_row.audience_capture_lock_owner is None
    assert channel_row.next_audience_capture_at is not None
    assert channel_row.next_audience_capture_at.date() == (snapshot.captured_at + timedelta(days=1)).date()


async def test_failed_audience_capture_never_clears_latest_success_cache(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scheduled_for = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    cached_at = datetime(2025, 12, 31, 14, 0, tzinfo=UTC)
    telegram_session, channel = await _insert_channel(
        migrated_db_session,
        scheduled_for=scheduled_for,
        subscriber_count=500,
        subscriber_count_updated_at=cached_at,
    )
    fake = FakeTelegramClient(next_error=PipelineTelegramProviderUnavailableError("telegram unavailable"))

    result = await capture_source_channel_audience_request(
        postgres_session_factory,
        _capture_event(telegram_session, channel, scheduled_for=scheduled_for),
        telegram_client_factory=lambda _event: fake,
    )

    assert result.fetch_status is SourceChannelAudienceFetchStatus.FAILED
    async with postgres_session_factory() as session:
        channel_row = await session.get(SourceChannel, channel.id)
        snapshot = await session.scalar(select(SourceChannelAudienceSnapshot))
    assert channel_row is not None
    assert snapshot is not None
    assert snapshot.fetch_status is SourceChannelAudienceFetchStatus.FAILED
    assert snapshot.subscriber_count is None
    assert snapshot.error_code == PipelineTelegramProviderUnavailableError.__name__
    assert channel_row.subscriber_count == 500
    assert channel_row.subscriber_count_updated_at == cached_at
    assert channel_row.next_audience_capture_at == scheduled_for
    assert channel_row.audience_capture_locked_at is None
    assert channel_row.audience_capture_lock_owner is None


@pytest.mark.parametrize(
    "stale_state",
    ["inactive", "paused", "engagement_disabled", "reassigned", "rescheduled"],
)
async def test_audience_capture_preflight_rejects_stale_channel_without_rpc_or_state_mutation(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    stale_state: str,
) -> None:
    scheduled_for = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    telegram_session, channel = await _insert_channel(
        migrated_db_session,
        scheduled_for=scheduled_for,
        subscriber_count=500,
    )
    replacement_session = await _insert_replacement_session(migrated_db_session)
    event = _capture_event(telegram_session, channel, scheduled_for=scheduled_for)
    expected_session_id, expected_schedule = await _make_capture_request_stale(
        migrated_db_session,
        channel_id=channel.id,
        stale_state=stale_state,
        scheduled_for=scheduled_for,
        replacement_session_id=replacement_session.id,
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient()
    factory_calls = 0

    def telegram_client_factory(
        _event: SourceChannelAudienceCaptureRequestedEvent,
    ) -> FakeTelegramClient:
        nonlocal factory_calls
        factory_calls += 1
        return fake

    result = await capture_source_channel_audience_request(
        postgres_session_factory,
        event,
        telegram_client_factory=telegram_client_factory,
    )

    assert result.error_code == "stale_audience_capture_request"
    assert result.snapshot_id is None
    assert factory_calls == 0
    assert fake.audience_fetch_channel_ids == []
    assert fake.closed is False
    async with postgres_session_factory() as session:
        channel_row = await session.get(SourceChannel, channel.id)
        snapshot = await session.scalar(select(SourceChannelAudienceSnapshot))
    assert channel_row is not None
    assert snapshot is None
    assert channel_row.telegram_session_id == expected_session_id
    assert channel_row.next_audience_capture_at == expected_schedule
    assert channel_row.audience_capture_locked_at == scheduled_for + timedelta(minutes=5)
    assert channel_row.audience_capture_lock_owner == "newer-scheduler"
    assert channel_row.last_audience_error_code == "newer-state"


@pytest.mark.parametrize(
    "stale_state",
    ["inactive", "paused", "engagement_disabled", "reassigned", "rescheduled"],
)
async def test_audience_capture_final_fence_discards_rpc_result_after_channel_becomes_stale(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    stale_state: str,
) -> None:
    scheduled_for = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    telegram_session, channel = await _insert_channel(
        migrated_db_session,
        scheduled_for=scheduled_for,
        subscriber_count=500,
    )
    replacement_session = await _insert_replacement_session(migrated_db_session)
    event = _capture_event(telegram_session, channel, scheduled_for=scheduled_for)
    await migrated_db_session.commit()

    class MutatingAudienceClient(FakeTelegramClient):
        async def fetch_channel_audience(self, channel_id: str) -> RawTelegramChannelAudience:
            async with postgres_session_factory() as session, session.begin():
                await _make_capture_request_stale(
                    session,
                    channel_id=channel.id,
                    stale_state=stale_state,
                    scheduled_for=scheduled_for,
                    replacement_session_id=replacement_session.id,
                )
            return await super().fetch_channel_audience(channel_id)

    fake = MutatingAudienceClient(
        canned_channel_audiences={
            channel.platform_id: RawTelegramChannelAudience(
                channel_id=channel.platform_id,
                subscriber_count=900,
            )
        }
    )

    result = await capture_source_channel_audience_request(
        postgres_session_factory,
        event,
        telegram_client_factory=lambda _event: fake,
    )

    expected_session_id = replacement_session.id if stale_state == "reassigned" else telegram_session.id
    expected_schedule = (
        scheduled_for + timedelta(days=1) if stale_state == "rescheduled" else scheduled_for
    )
    assert result.error_code == "stale_audience_capture_request"
    assert result.snapshot_id is None
    assert fake.audience_fetch_channel_ids == [channel.platform_id]
    assert fake.closed is True
    async with postgres_session_factory() as session:
        channel_row = await session.get(SourceChannel, channel.id)
        snapshot = await session.scalar(select(SourceChannelAudienceSnapshot))
    assert channel_row is not None
    assert snapshot is None
    assert channel_row.telegram_session_id == expected_session_id
    assert channel_row.next_audience_capture_at == expected_schedule
    assert channel_row.audience_capture_locked_at == scheduled_for + timedelta(minutes=5)
    assert channel_row.audience_capture_lock_owner == "newer-scheduler"
    assert channel_row.last_audience_error_code == "newer-state"
    assert channel_row.subscriber_count == 500


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            PipelineTelegramSessionAuthRequiredError("stored session is no longer authorized"),
            TelegramSessionStatus.AUTH_REQUIRED,
        ),
        (
            PipelineTelegramSessionBannedError("Telegram banned this session"),
            TelegramSessionStatus.QUARANTINED,
        ),
    ],
)
async def test_audience_capture_terminal_session_failure_updates_session_and_invalidates_client(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    error: PipelineTelegramSessionAuthRequiredError | PipelineTelegramSessionBannedError,
    expected_status: TelegramSessionStatus,
) -> None:
    scheduled_for = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    telegram_session, channel = await _insert_channel(
        migrated_db_session,
        scheduled_for=scheduled_for,
        subscriber_count=500,
    )
    telegram_session.live_listener_started_at = scheduled_for - timedelta(minutes=30)
    await migrated_db_session.commit()
    fake = FakeTelegramClient(next_error=error)
    invalidated_session_ids: list[uuid.UUID] = []

    async def invalidate_session(*, session_id: uuid.UUID) -> None:
        invalidated_session_ids.append(session_id)
        await fake.close()

    result = await capture_source_channel_audience_request(
        postgres_session_factory,
        _capture_event(telegram_session, channel, scheduled_for=scheduled_for),
        telegram_client_factory=lambda _event: fake,
        telegram_session_invalidator=invalidate_session,
        close_telegram_client_after_capture=False,
    )

    assert result.fetch_status is None
    assert result.snapshot_id is None
    assert result.error_code == type(error).__name__
    assert invalidated_session_ids == [telegram_session.id]
    assert fake.closed is True
    async with postgres_session_factory() as session:
        session_row = await session.get(TelegramSession, telegram_session.id)
        channel_row = await session.get(SourceChannel, channel.id)
        snapshot = await session.scalar(select(SourceChannelAudienceSnapshot))
    assert session_row is not None
    assert channel_row is not None
    assert snapshot is None
    assert session_row.status is expected_status
    assert session_row.last_error_class == type(error).__name__
    assert session_row.last_error_text == str(error)
    assert channel_row.next_audience_capture_at == scheduled_for
    assert channel_row.audience_capture_locked_at is None
    assert channel_row.audience_capture_lock_owner is None
    assert channel_row.subscriber_count == 500
    if expected_status is TelegramSessionStatus.AUTH_REQUIRED:
        assert session_row.live_listener_started_at is None
        assert session_row.quarantined_at is None
    else:
        assert session_row.quarantined_at is not None


async def test_same_slot_failure_cannot_replace_terminal_success_history(
    migrated_db_session: AsyncSession,
) -> None:
    captured_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    _, channel = await _insert_channel(
        migrated_db_session,
        scheduled_for=captured_at,
        subscriber_count=500,
    )
    success = await record_source_channel_audience_observation(
        migrated_db_session,
        channel,
        SourceChannelAudienceObservation(
            fetch_status=SourceChannelAudienceFetchStatus.SUCCESS,
            subscriber_count=750,
        ),
        capture_reason=SourceChannelAudienceCaptureReason.CRAWLER_REFRESH,
        captured_at=captured_at,
    )
    await migrated_db_session.flush()

    preserved = await record_source_channel_audience_observation(
        migrated_db_session,
        channel,
        SourceChannelAudienceObservation(
            fetch_status=SourceChannelAudienceFetchStatus.FAILED,
            error_code="later_provider_failure",
        ),
        capture_reason=SourceChannelAudienceCaptureReason.CRAWLER_REFRESH,
        captured_at=captured_at + timedelta(hours=1),
    )

    assert preserved.id == success.id
    assert preserved.fetch_status is SourceChannelAudienceFetchStatus.SUCCESS
    assert preserved.subscriber_count == 750
    assert preserved.captured_at == captured_at
    assert channel.subscriber_count == 750
    assert channel.subscriber_count_updated_at == captured_at


async def _insert_replacement_session(session: AsyncSession) -> TelegramSession:
    replacement_session = TelegramSession(
        name=f"audience-replacement-{uuid.uuid7()}",
        display_name="Replacement Audience Session",
        status=TelegramSessionStatus.ACTIVE,
    )
    session.add(replacement_session)
    await session.flush()
    return replacement_session


async def _make_capture_request_stale(
    session: AsyncSession,
    *,
    channel_id: uuid.UUID,
    stale_state: str,
    scheduled_for: datetime,
    replacement_session_id: uuid.UUID,
) -> tuple[uuid.UUID | None, datetime]:
    channel = await session.get(SourceChannel, channel_id, with_for_update=True)
    assert channel is not None
    if stale_state == "inactive":
        channel.is_active = False
    elif stale_state == "paused":
        channel.is_paused = True
    elif stale_state == "engagement_disabled":
        channel.engagement_enabled = False
    elif stale_state == "reassigned":
        channel.telegram_session_id = replacement_session_id
    elif stale_state == "rescheduled":
        channel.next_audience_capture_at = scheduled_for + timedelta(days=1)
    else:  # pragma: no cover - the parametrization above owns this closed set.
        raise AssertionError(f"Unsupported stale state: {stale_state}")
    channel.audience_capture_locked_at = scheduled_for + timedelta(minutes=5)
    channel.audience_capture_lock_owner = "newer-scheduler"
    channel.last_audience_error_code = "newer-state"
    await session.flush()
    assert channel.next_audience_capture_at is not None
    return channel.telegram_session_id, channel.next_audience_capture_at


async def _insert_channel(
    session: AsyncSession,
    *,
    scheduled_for: datetime,
    subscriber_count: int,
    subscriber_count_updated_at: datetime | None = None,
) -> tuple[TelegramSession, SourceChannel]:
    telegram_session = TelegramSession(
        name=f"audience-{uuid.uuid7()}",
        display_name="Audience",
        status=TelegramSessionStatus.ACTIVE,
    )
    session.add(telegram_session)
    await session.flush()
    channel = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id=f"channel_{uuid.uuid7().hex[:12]}",
        username="audience_channel",
        title="Audience Channel",
        telegram_session_id=telegram_session.id,
        subscriber_count=subscriber_count,
        subscriber_count_updated_at=subscriber_count_updated_at,
        next_audience_capture_at=scheduled_for,
        audience_capture_locked_at=scheduled_for,
        audience_capture_lock_owner="test-scheduler",
    )
    session.add(channel)
    await session.commit()
    return telegram_session, channel


def _capture_event(
    telegram_session: TelegramSession,
    channel: SourceChannel,
    *,
    scheduled_for: datetime,
) -> SourceChannelAudienceCaptureRequestedEvent:
    return SourceChannelAudienceCaptureRequestedEvent.model_validate(
        build_source_channel_audience_capture_requested_payload(
            event_id=uuid.uuid7(),
            source_channel_id=channel.id,
            source_platform=channel.platform,
            platform_id=channel.platform_id,
            scheduled_for=scheduled_for,
            capture_slot=scheduled_for.date(),
            capture_reason=SourceChannelAudienceCaptureReason.SCHEDULED,
            telegram_session_id=telegram_session.id,
            session_name=telegram_session.name,
            created_at=scheduled_for,
        )
    )
