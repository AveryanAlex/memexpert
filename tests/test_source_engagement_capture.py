"""Integration coverage for scheduled source engagement enqueue and capture services."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramProviderUnavailableError,
    RawTelegramMessage,
)
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeSource,
    MemeSourceEngagementSnapshot,
    RabbitMQOutboxMessage,
    SourceChannel,
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
)
from memexpert.pipeline.events import (
    PIPELINE_MEME_SOURCE_AGGREGATE_TYPE,
    SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE,
    SourceEngagementCaptureRequestedEvent,
)
from memexpert.services.source_engagement_capture import capture_source_engagement_request
from memexpert.services.source_engagement_scheduler import run_scheduler_source_engagement_capture_batch

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class SourceFixture:
    source: MemeSource
    published_at: datetime
    scheduled_for: datetime


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
    migrated_db_session.add(
        SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="due-channel",
            username="due",
            title="Due Channel",
            session_id="session-a",
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
    assert outbox.routing_key == "pipeline.source_engagement_capture"
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
    assert payload.session_name == "session-a"


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
    monkeypatch.setattr("memexpert.services.source_engagement_capture.utcnow", lambda: captured_at)

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
    monkeypatch.setattr("memexpert.services.source_engagement_capture.utcnow", lambda: captured_at)

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
    monkeypatch.setattr("memexpert.services.source_engagement_capture.utcnow", lambda: captured_at)

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
    monkeypatch.setattr("memexpert.services.source_engagement_capture.utcnow", lambda: first_captured_at)
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
) -> SourceEngagementCaptureRequestedEvent:
    return SourceEngagementCaptureRequestedEvent(
        event_id=uuid.uuid7(),
        event_type=SOURCE_ENGAGEMENT_CAPTURE_REQUESTED_EVENT_TYPE,
        meme_source_id=fixture.source.id,
        source_platform=fixture.source.platform,
        source_id=fixture.source.source_id,
        post_id=fixture.source.post_id,
        scheduled_for=fixture.scheduled_for,
        schedule_label=schedule_label,
        session_name="session-a",
        created_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
