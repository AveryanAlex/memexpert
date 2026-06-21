"""Integration coverage for scheduled source engagement enqueue and capture services."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

import memexpert.services.source_engagement_capture as source_engagement_capture_module
from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramProviderUnavailableError,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.manager import TelegramSessionManager
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeSource,
    MemeSourceEngagementSnapshot,
    RabbitMQOutboxMessage,
    SourceChannel,
    TelegramSession,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    RabbitMQOutboxMessageStatus,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.pipeline.events import (
    PIPELINE_MEME_SOURCE_AGGREGATE_TYPE,
    SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE,
    SourceEngagementCaptureRequestedEvent,
    build_source_engagement_capture_requested_payload,
    build_source_engagement_capture_routing_key,
    build_source_engagement_session_key,
)
from memexpert.services.source_engagement_capture import (
    build_pipeline_source_engagement_telegram_client_factory,
    capture_source_engagement_request,
)
from memexpert.services.source_engagement_scheduler import run_scheduler_source_engagement_capture_batch

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class SourceFixture:
    source: MemeSource
    published_at: datetime
    scheduled_for: datetime


def test_source_engagement_session_key_and_payload_contract() -> None:
    telegram_session_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    session_name = "Session A / primary"
    session_key = build_source_engagement_session_key(telegram_session_id, session_name)
    created_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)

    assert session_key == build_source_engagement_session_key(telegram_session_id, session_name)
    assert session_key != build_source_engagement_session_key(uuid.uuid7(), session_name)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", session_key)
    assert build_source_engagement_capture_routing_key(Settings(), session_key=session_key) == (
        f"pipeline.source_engagement_capture.{session_key}"
    )

    payload = build_source_engagement_capture_requested_payload(
        event_id=uuid.uuid7(),
        meme_source_id=uuid.uuid7(),
        source_platform=SourcePlatform.TELEGRAM,
        source_id="contract-channel",
        post_id="42",
        scheduled_for=created_at,
        schedule_label=SourceEngagementScheduleLabel.PLUS_1H,
        telegram_session_id=telegram_session_id,
        session_name=session_name,
        created_at=created_at,
    )
    event = SourceEngagementCaptureRequestedEvent.model_validate(payload)

    assert event.telegram_session_id == telegram_session_id
    assert event.session_name == session_name
    assert event.session_key == session_key


async def test_source_engagement_scheduler_claims_due_sources_and_writes_outbox(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    due = await _create_meme_source(
        migrated_db_session,
        source_id="due-channel",
        post_id="100",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    _ = await _create_meme_source(
        migrated_db_session,
        source_id="future-channel",
        post_id="101",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 15, 0, tzinfo=UTC),
    )
    _ = await _create_meme_source(
        migrated_db_session,
        source_id="dead-channel",
        post_id="102",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        source_alive=False,
    )
    _ = await _create_meme_source(
        migrated_db_session,
        source_id="locked-channel",
        post_id="103",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        locked_at=now - timedelta(minutes=5),
    )
    telegram_session = TelegramSession(
        name="session-a",
        display_name="Session A",
        status=TelegramSessionStatus.ACTIVE,
    )
    migrated_db_session.add(telegram_session)
    await migrated_db_session.flush()
    session_key = build_source_engagement_session_key(telegram_session.id, telegram_session.name)
    migrated_db_session.add(
        SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="due-channel",
            username="due",
            title="Due Channel",
            telegram_session_id=telegram_session.id,
        )
    )
    await migrated_db_session.commit()

    result = await run_scheduler_source_engagement_capture_batch(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "scheduler_source_engagement_capture_batch_size": 10,
                "scheduler_source_engagement_capture_lease_timeout_seconds": 1800.0,
            }
        ),
        now=now,
        lock_owner="test-scheduler",
    )

    assert result.claimed == 1
    assert result.enqueued == 1
    assert result.meme_source_ids == (due.source.id,)

    async with postgres_session_factory() as session:
        source = await session.get(MemeSource, due.source.id)
        outbox_rows = (await session.execute(select(RabbitMQOutboxMessage))).scalars().all()

    assert source is not None
    assert source.engagement_check_locked_at == now
    assert source.engagement_check_lock_owner == "test-scheduler"
    assert source.engagement_check_attempt_count == 1
    assert len(outbox_rows) == 1
    outbox = outbox_rows[0]
    assert outbox.id == result.outbox_message_ids[0]
    assert outbox.status is RabbitMQOutboxMessageStatus.PENDING
    assert outbox.exchange == "memexpert.pipeline"
    assert outbox.routing_key == f"pipeline.source_engagement_capture.{session_key}"
    assert outbox.event_type == SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE
    assert outbox.aggregate_type == PIPELINE_MEME_SOURCE_AGGREGATE_TYPE
    assert outbox.aggregate_id == str(due.source.id)
    payload = SourceEngagementCaptureRequestedEvent.model_validate(outbox.payload)
    assert payload.meme_source_id == due.source.id
    assert payload.source_platform is SourcePlatform.TELEGRAM
    assert payload.source_id == "due-channel"
    assert payload.post_id == "100"
    assert payload.scheduled_for == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert payload.schedule_label is SourceEngagementScheduleLabel.PLUS_1H
    assert payload.telegram_session_id == telegram_session.id
    assert payload.session_name == "session-a"
    assert payload.session_key == session_key


async def test_source_engagement_scheduler_caps_claims_per_session_and_routes_distinct_sessions(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    telegram_session_a = TelegramSession(
        name="Session A / Primary",
        display_name="Session A",
        status=TelegramSessionStatus.ACTIVE,
    )
    telegram_session_b = TelegramSession(
        name="session-b",
        display_name="Session B",
        status=TelegramSessionStatus.ACTIVE,
    )
    migrated_db_session.add_all([telegram_session_a, telegram_session_b])
    await migrated_db_session.flush()

    session_a_sources = [
        await _create_meme_source(
            migrated_db_session,
            source_id=f"session-a-channel-{index}",
            post_id=f"10{index}",
            published_at=datetime(2026, 1, 1, 12, index, tzinfo=UTC),
            scheduled_for=datetime(2026, 1, 1, 13, index, tzinfo=UTC),
        )
        for index in range(3)
    ]
    session_b_sources = [
        await _create_meme_source(
            migrated_db_session,
            source_id=f"session-b-channel-{index}",
            post_id=f"20{index}",
            published_at=datetime(2026, 1, 1, 12, 3 + index, tzinfo=UTC),
            scheduled_for=datetime(2026, 1, 1, 13, 3 + index, tzinfo=UTC),
        )
        for index in range(2)
    ]
    migrated_db_session.add_all(
        [
            *[
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id=fixture.source.source_id,
                    username=fixture.source.source_id,
                    title=fixture.source.source_id,
                    telegram_session_id=telegram_session_a.id,
                )
                for fixture in session_a_sources
            ],
            *[
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id=fixture.source.source_id,
                    username=fixture.source.source_id,
                    title=fixture.source.source_id,
                    telegram_session_id=telegram_session_b.id,
                )
                for fixture in session_b_sources
            ],
        ]
    )
    await migrated_db_session.commit()

    result = await run_scheduler_source_engagement_capture_batch(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "scheduler_source_engagement_capture_batch_size": 3,
                "scheduler_source_engagement_capture_per_session_batch_size": 2,
                "scheduler_source_engagement_capture_lease_timeout_seconds": 1800.0,
            }
        ),
        now=now,
        lock_owner="test-scheduler",
    )

    assert result.claimed == 3
    assert result.enqueued == 3
    async with postgres_session_factory() as session:
        outbox_rows = (
            await session.execute(select(RabbitMQOutboxMessage).order_by(RabbitMQOutboxMessage.created_at.asc()))
        ).scalars().all()
        source_a_0 = await session.get(MemeSource, session_a_sources[0].source.id)
        source_a_1 = await session.get(MemeSource, session_a_sources[1].source.id)
        source_a_2 = await session.get(MemeSource, session_a_sources[2].source.id)
        source_b_0 = await session.get(MemeSource, session_b_sources[0].source.id)

    session_keys = {
        telegram_session_a.id: build_source_engagement_session_key(telegram_session_a.id, telegram_session_a.name),
        telegram_session_b.id: build_source_engagement_session_key(telegram_session_b.id, telegram_session_b.name),
    }
    session_names = {
        telegram_session_a.id: telegram_session_a.name,
        telegram_session_b.id: telegram_session_b.name,
    }
    counts_by_session_id = {telegram_session_a.id: 0, telegram_session_b.id: 0}
    routing_keys_by_session_id: dict[uuid.UUID, str] = {}
    for outbox in outbox_rows:
        payload = SourceEngagementCaptureRequestedEvent.model_validate(outbox.payload)
        counts_by_session_id[payload.telegram_session_id] += 1
        routing_keys_by_session_id[payload.telegram_session_id] = outbox.routing_key
        assert payload.session_name == session_names[payload.telegram_session_id]
        assert payload.session_key == session_keys[payload.telegram_session_id]
        assert outbox.routing_key == f"pipeline.source_engagement_capture.{payload.session_key}"

    assert counts_by_session_id == {telegram_session_a.id: 2, telegram_session_b.id: 1}
    assert len(set(routing_keys_by_session_id.values())) == 2
    assert source_a_0 is not None
    assert source_a_0.engagement_check_locked_at == now
    assert source_a_1 is not None
    assert source_a_1.engagement_check_locked_at == now
    assert source_a_2 is not None
    assert source_a_2.engagement_check_locked_at is None
    assert source_b_0 is not None
    assert source_b_0.engagement_check_locked_at == now


async def test_source_engagement_scheduler_filters_unrunnable_channels_and_sessions(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    orphan = await _create_meme_source(
        migrated_db_session,
        source_id="orphan-channel",
        post_id="200",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    channel_engagement_disabled = await _create_meme_source(
        migrated_db_session,
        source_id="disabled-engagement-channel",
        post_id="201",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    inactive_channel = await _create_meme_source(
        migrated_db_session,
        source_id="inactive-channel",
        post_id="202",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    disabled_session_source = await _create_meme_source(
        migrated_db_session,
        source_id="disabled-session-channel",
        post_id="203",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    engagement_disabled_session_source = await _create_meme_source(
        migrated_db_session,
        source_id="engagement-disabled-session-channel",
        post_id="204",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    auth_required_source = await _create_meme_source(
        migrated_db_session,
        source_id="auth-required-session-channel",
        post_id="205",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    stopped_source = await _create_meme_source(
        migrated_db_session,
        source_id="stopped-session-channel",
        post_id="206",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    quarantined_source = await _create_meme_source(
        migrated_db_session,
        source_id="quarantined-session-channel",
        post_id="207",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    future_flood_wait_source = await _create_meme_source(
        migrated_db_session,
        source_id="future-flood-wait-session-channel",
        post_id="208",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    expired_flood_wait_source = await _create_meme_source(
        migrated_db_session,
        source_id="expired-flood-wait-session-channel",
        post_id="209",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    active_session = TelegramSession(
        name="active-session",
        display_name="Session A",
        status=TelegramSessionStatus.ACTIVE,
    )
    disabled_session = TelegramSession(
        name="disabled-session",
        display_name="Disabled Session",
        status=TelegramSessionStatus.ACTIVE,
        enabled=False,
    )
    engagement_disabled_session = TelegramSession(
        name="engagement-disabled-session",
        display_name="Engagement Disabled Session",
        status=TelegramSessionStatus.ACTIVE,
        engagement_enabled=False,
    )
    auth_required_session = TelegramSession(
        name="auth-required-session",
        display_name="Auth Required Session",
        status=TelegramSessionStatus.AUTH_REQUIRED,
    )
    stopped_session = TelegramSession(
        name="stopped-session",
        display_name="Stopped Session",
        status=TelegramSessionStatus.STOPPED,
    )
    quarantined_session = TelegramSession(
        name="quarantined-session",
        display_name="Quarantined Session",
        status=TelegramSessionStatus.QUARANTINED,
    )
    future_flood_wait_session = TelegramSession(
        name="future-flood-wait-session",
        display_name="Future FloodWait Session",
        status=TelegramSessionStatus.FLOOD_WAIT,
        flood_wait_until=now + timedelta(minutes=30),
    )
    expired_flood_wait_session = TelegramSession(
        name="expired-flood-wait-session",
        display_name="Expired FloodWait Session",
        status=TelegramSessionStatus.FLOOD_WAIT,
        flood_wait_until=now - timedelta(minutes=1),
        last_error_class="FloodWaitError",
        last_error_text="wait 60 seconds",
    )
    migrated_db_session.add_all(
        [
            active_session,
            disabled_session,
            engagement_disabled_session,
            auth_required_session,
            stopped_session,
            quarantined_session,
            future_flood_wait_session,
            expired_flood_wait_session,
        ]
    )
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="orphan-channel",
                title="Orphan Channel",
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="disabled-engagement-channel",
                title="Disabled Engagement Channel",
                telegram_session_id=active_session.id,
                engagement_enabled=False,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="inactive-channel",
                title="Inactive Channel",
                telegram_session_id=active_session.id,
                is_active=False,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="disabled-session-channel",
                title="Disabled Session Channel",
                telegram_session_id=disabled_session.id,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="engagement-disabled-session-channel",
                title="Engagement Disabled Session Channel",
                telegram_session_id=engagement_disabled_session.id,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="auth-required-session-channel",
                title="Auth Required Session Channel",
                telegram_session_id=auth_required_session.id,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="stopped-session-channel",
                title="Stopped Session Channel",
                telegram_session_id=stopped_session.id,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="quarantined-session-channel",
                title="Quarantined Session Channel",
                telegram_session_id=quarantined_session.id,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="future-flood-wait-session-channel",
                title="Future FloodWait Session Channel",
                telegram_session_id=future_flood_wait_session.id,
            ),
            SourceChannel(
                platform=SourcePlatform.TELEGRAM,
                platform_id="expired-flood-wait-session-channel",
                title="Expired FloodWait Session Channel",
                telegram_session_id=expired_flood_wait_session.id,
            ),
        ]
    )
    await migrated_db_session.commit()

    result = await run_scheduler_source_engagement_capture_batch(
        postgres_session_factory,
        settings=Settings.model_validate(
            {
                "scheduler_source_engagement_capture_batch_size": 10,
                "scheduler_source_engagement_capture_per_session_batch_size": 10,
                "scheduler_source_engagement_capture_lease_timeout_seconds": 1800.0,
            }
        ),
        now=now,
        lock_owner="test-scheduler",
    )

    assert result.claimed == 1
    assert result.enqueued == 1
    async with postgres_session_factory() as session:
        skipped_sources = [
            await session.get(MemeSource, fixture.source.id)
            for fixture in (
                orphan,
                channel_engagement_disabled,
                inactive_channel,
                disabled_session_source,
                engagement_disabled_session_source,
                auth_required_source,
                stopped_source,
                quarantined_source,
                future_flood_wait_source,
            )
        ]
        eligible_source = await session.get(MemeSource, expired_flood_wait_source.source.id)
        future_flood_session = await session.get(TelegramSession, future_flood_wait_session.id)
        expired_flood_session = await session.get(TelegramSession, expired_flood_wait_session.id)
        outbox_rows = (await session.execute(select(RabbitMQOutboxMessage))).scalars().all()

    assert all(source is not None and source.engagement_check_locked_at is None for source in skipped_sources)
    assert eligible_source is not None
    assert eligible_source.engagement_check_locked_at == now
    assert future_flood_session is not None
    assert future_flood_session.status is TelegramSessionStatus.FLOOD_WAIT
    assert future_flood_session.flood_wait_until == now + timedelta(minutes=30)
    assert expired_flood_session is not None
    assert expired_flood_session.status is TelegramSessionStatus.ACTIVE
    assert expired_flood_session.flood_wait_until is None
    assert expired_flood_session.last_error_class is None
    assert expired_flood_session.last_error_text is None
    assert len(outbox_rows) == 1
    payload = SourceEngagementCaptureRequestedEvent.model_validate(outbox_rows[0].payload)
    assert payload.telegram_session_id == expired_flood_wait_session.id
    assert payload.session_name == "expired-flood-wait-session"


async def test_source_engagement_capture_success_appends_scheduled_snapshot_and_advances_monthly(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    published_at = datetime(2025, 1, 15, 10, 30, tzinfo=UTC)
    scheduled_for = datetime(2026, 6, 15, 10, 30, tzinfo=UTC)
    captured_at = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    fixture = await _create_meme_source(
        migrated_db_session,
        source_id="monthly-channel",
        post_id="200",
        published_at=published_at,
        scheduled_for=scheduled_for,
        locked_at=captured_at - timedelta(minutes=5),
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient()
    fake.pin_single_message(
        channel_id="monthly-channel",
        post_id="200",
        message=RawTelegramMessage(
            message_id="200",
            channel_id="monthly-channel",
            channel_username="monthly",
            channel_title="Monthly Channel",
            published_at=published_at,
            media_type="photo",
            view_count=123,
            reactions={"fire": 2, "heart": 3},
            forward_count=4,
            comment_count=5,
            comments_state=SourceEngagementCommentsState.ENABLED,
        ),
        media=b"must-not-download",
    )
    monkeypatch.setattr(source_engagement_capture_module, "utcnow", lambda: captured_at)

    result = await capture_source_engagement_request(
        postgres_session_factory,
        _capture_event(fixture, schedule_label=SourceEngagementScheduleLabel.MONTHLY),
        telegram_client_factory=lambda _event: fake,
    )

    assert result.fetch_status is SourceEngagementFetchStatus.SUCCESS
    assert result.duplicate is False
    assert fake.downloaded_message_ids == []
    assert fake.closed is True

    async with postgres_session_factory() as session:
        source = await session.get(MemeSource, fixture.source.id)
        snapshots = (await session.execute(select(MemeSourceEngagementSnapshot))).scalars().all()

    assert source is not None
    assert source.source_alive is True
    assert source.last_engagement_check_at == captured_at
    assert source.next_engagement_check_at == datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
    assert source.engagement_check_locked_at is None
    assert source.last_engagement_error_code is None
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.fetch_status is SourceEngagementFetchStatus.SUCCESS
    assert snapshot.schedule_label is SourceEngagementScheduleLabel.MONTHLY
    assert snapshot.scheduled_for == scheduled_for
    assert snapshot.view_count == 123
    assert snapshot.reactions == {"fire": 2, "heart": 3}
    assert snapshot.reaction_count == 5
    assert snapshot.comment_count == 5
    assert snapshot.forward_count == 4
    assert snapshot.comments_state is SourceEngagementCommentsState.ENABLED


async def test_source_engagement_capture_manager_factory_reuses_cached_clients_without_per_message_close(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_a_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    session_b_id = uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acc")
    migrated_db_session.add_all(
        [
            TelegramSession(
                id=session_a_id,
                name="session-a",
                display_name="Session A",
                status=TelegramSessionStatus.ACTIVE,
                encrypted_string_session="encrypted-session-a",
            ),
            TelegramSession(
                id=session_b_id,
                name="session-b",
                display_name="Session B",
                status=TelegramSessionStatus.ACTIVE,
                encrypted_string_session="encrypted-session-b",
            ),
        ]
    )
    fixture_a_1 = await _create_meme_source(
        migrated_db_session,
        source_id="manager-a-one",
        post_id="100",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        locked_at=datetime(2026, 1, 1, 13, 30, tzinfo=UTC),
    )
    fixture_a_2 = await _create_meme_source(
        migrated_db_session,
        source_id="manager-a-two",
        post_id="101",
        published_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 1, tzinfo=UTC),
        locked_at=datetime(2026, 1, 1, 13, 30, tzinfo=UTC),
    )
    fixture_b = await _create_meme_source(
        migrated_db_session,
        source_id="manager-b-one",
        post_id="200",
        published_at=datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 2, tzinfo=UTC),
        locked_at=datetime(2026, 1, 1, 13, 30, tzinfo=UTC),
    )
    await migrated_db_session.commit()
    fake_a = FakeTelegramClient()
    fake_a.pin_single_message(
        channel_id="manager-a-one",
        post_id="100",
        message=RawTelegramMessage(
            message_id="100",
            channel_id="manager-a-one",
            channel_username=None,
            channel_title="Manager A One",
            published_at=fixture_a_1.published_at,
            media_type="photo",
            view_count=10,
        ),
    )
    fake_a.pin_single_message(
        channel_id="manager-a-two",
        post_id="101",
        message=RawTelegramMessage(
            message_id="101",
            channel_id="manager-a-two",
            channel_username=None,
            channel_title="Manager A Two",
            published_at=fixture_a_2.published_at,
            media_type="photo",
            view_count=11,
        ),
    )
    fake_b = FakeTelegramClient()
    fake_b.pin_single_message(
        channel_id="manager-b-one",
        post_id="200",
        message=RawTelegramMessage(
            message_id="200",
            channel_id="manager-b-one",
            channel_username=None,
            channel_title="Manager B One",
            published_at=fixture_b.published_at,
            media_type="photo",
            view_count=20,
        ),
    )
    clients_by_name = {"session-a": fake_a, "session-b": fake_b}
    created_for: list[str] = []

    def _client_factory(row: TelegramSession) -> FakeTelegramClient:
        created_for.append(row.name)
        return clients_by_name[row.name]

    manager = TelegramSessionManager(
        settings=Settings(),
        session_factory=postgres_session_factory,
        telegram_client_factory=_client_factory,
    )
    factory = build_pipeline_source_engagement_telegram_client_factory(Settings(), session_manager=manager)

    first = await capture_source_engagement_request(
        postgres_session_factory,
        _capture_event(fixture_a_1, telegram_session_id=session_a_id, session_name="session-a"),
        telegram_client_factory=factory,
        close_telegram_client_after_capture=False,
    )
    second = await capture_source_engagement_request(
        postgres_session_factory,
        _capture_event(fixture_a_2, telegram_session_id=session_a_id, session_name="session-a"),
        telegram_client_factory=factory,
        close_telegram_client_after_capture=False,
    )
    third = await capture_source_engagement_request(
        postgres_session_factory,
        _capture_event(fixture_b, telegram_session_id=session_b_id, session_name="session-b"),
        telegram_client_factory=factory,
        close_telegram_client_after_capture=False,
    )

    assert [first.fetch_status, second.fetch_status, third.fetch_status] == [
        SourceEngagementFetchStatus.SUCCESS,
        SourceEngagementFetchStatus.SUCCESS,
        SourceEngagementFetchStatus.SUCCESS,
    ]
    assert created_for == ["session-a", "session-b"]
    assert fake_a.closed is False
    assert fake_b.closed is False

    await manager.shutdown()
    assert fake_a.closed is True
    assert fake_b.closed is True


async def test_source_engagement_capture_missing_message_records_not_found_and_marks_source_dead(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    captured_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    fixture = await _create_meme_source(
        migrated_db_session,
        source_id="missing-channel",
        post_id="300",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        locked_at=captured_at - timedelta(minutes=5),
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient()
    monkeypatch.setattr(source_engagement_capture_module, "utcnow", lambda: captured_at)

    result = await capture_source_engagement_request(
        postgres_session_factory,
        _capture_event(fixture),
        telegram_client_factory=lambda _event: fake,
    )

    assert result.fetch_status is SourceEngagementFetchStatus.NOT_FOUND
    assert result.error_code == "PipelineTelegramMalformedMessageError"
    async with postgres_session_factory() as session:
        source = await session.get(MemeSource, fixture.source.id)
        snapshot = await session.scalar(select(MemeSourceEngagementSnapshot))

    assert source is not None
    assert source.source_alive is False
    assert source.engagement_check_locked_at is None
    assert source.last_engagement_error_code == "PipelineTelegramMalformedMessageError"
    assert snapshot is not None
    assert snapshot.fetch_status is SourceEngagementFetchStatus.NOT_FOUND
    assert snapshot.source_alive is False
    assert snapshot.view_count is None
    assert snapshot.reactions is None
    assert snapshot.reaction_count is None


async def test_source_engagement_capture_transient_failure_records_failed_and_clears_lease(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    captured_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    fixture = await _create_meme_source(
        migrated_db_session,
        source_id="failed-channel",
        post_id="400",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        scheduled_for=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        locked_at=captured_at - timedelta(minutes=5),
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient(next_error=PipelineTelegramProviderUnavailableError("telegram down"))
    monkeypatch.setattr(source_engagement_capture_module, "utcnow", lambda: captured_at)

    result = await capture_source_engagement_request(
        postgres_session_factory,
        _capture_event(fixture),
        telegram_client_factory=lambda _event: fake,
    )

    assert result.fetch_status is SourceEngagementFetchStatus.FAILED
    assert result.error_code == "PipelineTelegramProviderUnavailableError"
    async with postgres_session_factory() as session:
        source = await session.get(MemeSource, fixture.source.id)
        snapshot = await session.scalar(select(MemeSourceEngagementSnapshot))

    assert source is not None
    assert source.source_alive is True
    assert source.next_engagement_check_at == fixture.scheduled_for
    assert source.engagement_check_locked_at is None
    assert source.last_engagement_error_code == "PipelineTelegramProviderUnavailableError"
    assert snapshot is not None
    assert snapshot.fetch_status is SourceEngagementFetchStatus.FAILED
    assert snapshot.source_alive is True


async def test_source_engagement_capture_duplicate_scheduled_message_is_idempotent(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    published_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    scheduled_for = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    first_captured_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    fixture = await _create_meme_source(
        migrated_db_session,
        source_id="dupe-channel",
        post_id="500",
        published_at=published_at,
        scheduled_for=scheduled_for,
        locked_at=first_captured_at - timedelta(minutes=5),
    )
    await migrated_db_session.commit()
    fake = FakeTelegramClient()
    fake.pin_single_message(
        channel_id="dupe-channel",
        post_id="500",
        message=RawTelegramMessage(
            message_id="500",
            channel_id="dupe-channel",
            channel_username="dupe",
            channel_title="Dupe Channel",
            published_at=published_at,
            media_type="photo",
            view_count=11,
            reactions=None,
        ),
    )
    event = _capture_event(fixture)
    monkeypatch.setattr(source_engagement_capture_module, "utcnow", lambda: first_captured_at)
    first = await capture_source_engagement_request(
        postgres_session_factory,
        event,
        telegram_client_factory=lambda _event: fake,
    )

    second = await capture_source_engagement_request(
        postgres_session_factory,
        event.model_copy(update={"event_id": uuid.uuid7()}),
        telegram_client_factory=lambda _event: fake,
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.snapshot_id == first.snapshot_id
    async with postgres_session_factory() as session:
        snapshots = (await session.execute(select(MemeSourceEngagementSnapshot))).scalars().all()

    assert len(snapshots) == 1


async def _create_meme_source(
    session: AsyncSession,
    *,
    source_id: str,
    post_id: str,
    published_at: datetime,
    scheduled_for: datetime,
    platform: SourcePlatform = SourcePlatform.TELEGRAM,
    source_alive: bool = True,
    locked_at: datetime | None = None,
) -> SourceFixture:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    session.add(
        Meme(
            id=meme_id,
            media_type=ContentKind.IMAGE,
            primary_file_id=file_id,
            language=ContentLanguage.NONE,
        )
    )
    await session.flush()
    source = MemeSource(
        file_id=file_id,
        platform=platform,
        source_id=source_id,
        post_id=post_id,
        source_alive=source_alive,
        published_at=published_at,
        next_engagement_check_at=scheduled_for,
        engagement_check_locked_at=locked_at,
        engagement_check_lock_owner="existing-lock" if locked_at is not None else None,
    )
    session.add_all(
        [
            MemeFile(
                id=file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.READY,
                s3_original_key=f"pipeline/originals/{file_id}/original.png",
            ),
            source,
        ]
    )
    await session.flush()
    return SourceFixture(source=source, published_at=published_at, scheduled_for=scheduled_for)


def _capture_event(
    fixture: SourceFixture,
    *,
    schedule_label: SourceEngagementScheduleLabel = SourceEngagementScheduleLabel.PLUS_1H,
    telegram_session_id: uuid.UUID | None = None,
    session_name: str = "session-a",
) -> SourceEngagementCaptureRequestedEvent:
    resolved_telegram_session_id = telegram_session_id or uuid.UUID("018f0f4d-37f1-7d32-9a60-7c84ec5f3acb")
    return SourceEngagementCaptureRequestedEvent(
        event_id=uuid.uuid7(),
        event_type=SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE,
        meme_source_id=fixture.source.id,
        source_platform=fixture.source.platform,
        source_id=fixture.source.source_id,
        post_id=fixture.source.post_id,
        scheduled_for=fixture.scheduled_for,
        schedule_label=schedule_label,
        telegram_session_id=resolved_telegram_session_id,
        session_name=session_name,
        session_key=build_source_engagement_session_key(resolved_telegram_session_id, session_name),
        created_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
