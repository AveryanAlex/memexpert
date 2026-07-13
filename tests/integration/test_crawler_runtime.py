"""Integration tests for :class:`TelegramCrawlerRuntime` with FakeTelegramClient.

These tests wire the runtime against a real migrated PostgreSQL session
(provided by :func:`migrated_db_session`) and the in-process
``FakeTelegramClient`` + a fake raw-ingest storage double. No test in this
module imports :mod:`telethon` — the adapter's translation layer is
exercised only by the pure-Python mapper tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramFloodWaitError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionBannedError,
    RawTelegramChannel,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.runtime import (
    CrawlerCatchupReport,
    TelegramCrawlerRuntime,
)
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.models.content import (
    MemeFile,
    PipelineIngestRequest,
    RabbitMQOutboxMessage,
    SourceChannel,
    SourceChannelPost,
    TelegramSession,
)
from memexpert.models.enums import SourceChannelPostStatus, SourcePlatform, TelegramSessionStatus
from memexpert.schemas.content_pipeline import (
    CrawlerForwardAttribution,
    CrawlerIngestOutcome,
    CrawlerIngestResult,
    RawCrawlerPost,
)
from memexpert.services import CrawlerSessionNotRunnableError
from tests.integration.test_ingest_accept_service import FakeStorageClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.transactional_db

_LIVE_LISTENER_TEST_TIMEOUT_SECONDS = 10.0


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _build_photo_message(
    *,
    message_id: str,
    channel_id: str = "curated_channel",
    forward: CrawlerForwardAttribution | None = None,
    views: int = 11,
) -> RawTelegramMessage:
    return RawTelegramMessage(
        message_id=message_id,
        channel_id=channel_id,
        channel_username=None,
        channel_title="Curated Channel",
        published_at=_now(),
        media_type="photo",
        views=views,
        reactions={"heart": 3},
        forward=forward,
    )


def _build_unsupported_message(
    *,
    message_id: str,
    channel_id: str = "curated_channel",
) -> RawTelegramMessage:
    return RawTelegramMessage(
        message_id=message_id,
        channel_id=channel_id,
        channel_username=None,
        channel_title="Curated Channel",
        published_at=_now(),
        media_type="unsupported",
        views=0,
        reactions={},
        forward=None,
    )


async def _seed_active_session(
    session: AsyncSession,
    *,
    session_name: str,
    enabled: bool = True,
) -> TelegramSession:
    row = TelegramSession(
        name=session_name,
        display_name=session_name.title(),
        status=TelegramSessionStatus.ACTIVE,
        enabled=enabled,
        last_heartbeat_at=_now(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _seed_curated_channel(
    session: AsyncSession,
    *,
    platform_id: str,
    title: str = "Curated Channel",
    catchup_message_limit: int = 500,
    catchup_enabled: bool = True,
    live_enabled: bool = True,
    engagement_enabled: bool = True,
    is_paused: bool = False,
    session_name: str | None = "primary",
    last_read_post_id: str | None = None,
) -> SourceChannel:
    telegram_session_id = None
    if session_name is not None:
        telegram_session_id = await session.scalar(
            select(TelegramSession.id).where(TelegramSession.name == session_name),
        )
        assert telegram_session_id is not None
    channel = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id=platform_id,
        title=title,
        is_active=True,
        is_paused=is_paused,
        catchup_message_limit=catchup_message_limit,
        catchup_enabled=catchup_enabled,
        live_enabled=live_enabled,
        engagement_enabled=engagement_enabled,
        telegram_session_id=telegram_session_id,
        last_read_post_id=last_read_post_id,
        initial_catchup_completed=last_read_post_id is not None,
    )
    session.add(channel)
    await session.commit()
    await session.refresh(channel)
    return channel


def _build_runtime(
    session: AsyncSession,
    *,
    telegram_client: FakeTelegramClient,
    phash_tag: str = "R",
    storage_client: FakeStorageClient | None = None,
    settings: Settings | None = None,
) -> TelegramCrawlerRuntime:
    _ = phash_tag
    resolved_settings = settings or Settings()
    service = PipelineCrawlerIngestService.from_settings(
        session,
        settings=resolved_settings,
        storage_client=storage_client or FakeStorageClient(),
    )
    return TelegramCrawlerRuntime(
        ingest_service=service,
        telegram_client=telegram_client,
        session=session,
        settings=resolved_settings,
    )


# ---------------------------------------------------------------------------
# catch_up_channel happy path + counters
# ---------------------------------------------------------------------------


async def test_catch_up_channel_ingests_and_counts_mixed_media(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="curated_channel",
    )

    messages = [
        _build_photo_message(message_id="1"),
        _build_photo_message(message_id="2"),
        RawTelegramMessage(
            message_id="3",
            channel_id="curated_channel",
            channel_username=None,
            channel_title="Curated Channel",
            published_at=_now(),
            media_type="gif",
            views=5,
            reactions={},
            forward=None,
        ),
        RawTelegramMessage(
            message_id="4",
            channel_id="curated_channel",
            channel_username=None,
            channel_title="Curated Channel",
            published_at=_now(),
            media_type="video",
            views=9,
            reactions={},
            forward=None,
        ),
        _build_unsupported_message(message_id="5"),
    ]
    fake = FakeTelegramClient(
        canned_messages={"curated_channel": messages},
        canned_channels={
            "curated_channel": RawTelegramChannel(
                channel_id="curated_channel",
                username=None,
                title="Curated Channel",
                subscriber_count=100,
            ),
        },
        media_by_message={m.message_id: b"bytes-" + m.message_id.encode() for m in messages[:4]},
    )
    storage_client = FakeStorageClient()
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, storage_client=storage_client)

    report = await runtime.catch_up_channel("primary", "curated_channel")

    assert isinstance(report, CrawlerCatchupReport)
    assert report.messages_scanned == 5
    # New crawler media now lands as raw ingest requests. The unsupported
    # media short-circuits before download or raw accept.
    assert report.messages_ingested == 4
    assert report.messages_skipped_dedup == 0
    assert report.messages_skipped_unsupported == 1
    assert report.errors == ()
    assert report.session_name == "primary"
    assert report.channel_id == "curated_channel"

    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "5"
    assert channel.oldest_observed_post_id == "1"
    assert channel.history_cursor_post_id == "1"
    assert channel.initial_catchup_completed is True
    assert channel.last_fetched_at is not None
    # Channel metadata refresh should have updated subscriber_count from
    # the fake resolve_channel response.
    assert channel.subscriber_count == 100
    assert len(storage_client.put_calls) == 4
    assert await migrated_db_session.scalar(select(func.count()).select_from(PipelineIngestRequest)) == 4
    assert await migrated_db_session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage)) == 4
    assert await migrated_db_session.scalar(select(func.count()).select_from(MemeFile)) == 0
    post_rows = (
        await migrated_db_session.execute(
            select(SourceChannelPost)
            .where(SourceChannelPost.source_channel_id == channel.id)
            .order_by(SourceChannelPost.published_at.asc()),
        )
    ).scalars().all()
    assert [row.post_id for row in post_rows] == ["1", "2", "3", "4", "5"]
    assert [row.status for row in post_rows] == [
        SourceChannelPostStatus.ACCEPTED,
        SourceChannelPostStatus.ACCEPTED,
        SourceChannelPostStatus.ACCEPTED,
        SourceChannelPostStatus.ACCEPTED,
        SourceChannelPostStatus.UNSUPPORTED,
    ]


async def test_catch_up_channel_records_successful_empty_poll(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="quiet_channel",
    )
    fake = FakeTelegramClient(
        canned_messages={"quiet_channel": []},
        canned_channels={
            "quiet_channel": RawTelegramChannel(
                channel_id="quiet_channel",
                username="quiet_channel",
                title="Quiet Channel",
                subscriber_count=12,
            ),
        },
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake)

    report = await runtime.catch_up_channel("primary", "quiet_channel")

    assert report.messages_scanned == 0
    assert report.errors == ()
    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id is None
    assert channel.last_fetched_at is not None
    assert channel.initial_catchup_completed is True
    assert channel.history_exhausted is True


async def test_catch_up_channel_inserts_messages_after_an_existing_inventory_snapshot(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="snapshot_channel",
    )
    stream_started = asyncio.Event()
    release_message = asyncio.Event()
    message = _build_unsupported_message(
        message_id="1",
        channel_id="snapshot_channel",
    )

    class _SnapshotPausedClient(FakeTelegramClient):
        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            stream_started.set()
            await release_message.wait()
            async for raw_message in super().iter_latest_channel_messages(
                channel_id=channel_id,
                limit=limit,
            ):
                yield raw_message

    runtime = _build_runtime(
        migrated_db_session,
        telegram_client=_SnapshotPausedClient(
            canned_messages={"snapshot_channel": [message]},
        ),
    )
    catchup_task = asyncio.create_task(runtime.catch_up_channel("primary", "snapshot_channel"))
    await asyncio.wait_for(stream_started.wait(), timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS)
    await asyncio.sleep(0.001)
    snapshot_at = _now()
    release_message.set()

    report = await catchup_task

    assert report.messages_scanned == 1
    post = await migrated_db_session.scalar(
        select(SourceChannelPost).where(
            SourceChannelPost.source_channel_id == channel.id,
            SourceChannelPost.post_id == "1",
        ),
    )
    assert post is not None
    assert post.created_at > snapshot_at
    assert (
        await migrated_db_session.scalar(
            select(func.count())
            .select_from(SourceChannelPost)
            .where(
                SourceChannelPost.source_channel_id == channel.id,
                SourceChannelPost.created_at <= snapshot_at,
            ),
        )
        == 0
    )


async def test_catch_up_channel_respects_catchup_message_limit(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="limited_channel",
        catchup_message_limit=3,
    )

    messages = [_build_photo_message(message_id=str(i), channel_id="limited_channel") for i in range(1, 11)]
    fake = FakeTelegramClient(
        canned_messages={"limited_channel": messages},
        media_by_message={m.message_id: b"img" for m in messages},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="L")

    report = await runtime.catch_up_channel("primary", "limited_channel")

    # Only 3 messages are scanned because the channel's catchup limit is 3.
    # They all accept asynchronously because no materialized MemeFile exists
    # yet for SHA duplicate resolution.
    assert report.messages_scanned == 3
    assert report.messages_ingested == 3
    assert report.messages_skipped_dedup == 0
    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "10"
    assert channel.oldest_observed_post_id == "8"
    assert channel.history_cursor_post_id == "8"
    assert channel.initial_catchup_completed is True
    request_post_ids = set(
        (
            await migrated_db_session.execute(
                select(PipelineIngestRequest.post_id).where(PipelineIngestRequest.source_id == "limited_channel"),
            )
        ).scalars()
    )
    assert request_post_ids == {"8", "9", "10"}


async def test_initial_latest_window_closes_forward_gap_before_returning(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="gap_channel",
        catchup_message_limit=3,
    )
    initial_messages = [_build_photo_message(message_id=str(i), channel_id="gap_channel") for i in range(1, 6)]
    new_messages = [_build_photo_message(message_id=str(i), channel_id="gap_channel") for i in range(6, 8)]

    class _MessagesArriveDuringInitialWindow(FakeTelegramClient):
        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            async for message in super().iter_latest_channel_messages(channel_id=channel_id, limit=limit):
                yield message
            self.canned_messages[channel_id].extend(new_messages)

    all_messages = [*initial_messages, *new_messages]
    fake = _MessagesArriveDuringInitialWindow(
        canned_messages={"gap_channel": initial_messages.copy()},
        media_by_message={message.message_id: b"img" for message in all_messages},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake)

    report = await runtime.catch_up_channel("primary", "gap_channel")

    await migrated_db_session.refresh(channel)
    assert report.messages_scanned == 5
    assert channel.last_read_post_id == "7"
    assert channel.oldest_observed_post_id == "3"
    assert channel.history_cursor_post_id == "3"
    assert channel.initial_catchup_completed is True
    request_post_ids = set(
        (
            await migrated_db_session.execute(
                select(PipelineIngestRequest.post_id).where(PipelineIngestRequest.source_id == "gap_channel"),
            )
        ).scalars()
    )
    assert request_post_ids == {"3", "4", "5", "6", "7"}


async def test_partial_initial_window_retries_latest_messages_before_switching_forward(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="retry_initial_channel",
        catchup_message_limit=3,
    )
    messages = [
        _build_photo_message(message_id=str(i), channel_id="retry_initial_channel")
        for i in range(1, 11)
    ]

    class _InitialStreamFailsOnce(FakeTelegramClient):
        failed = False

        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            async for message in super().iter_latest_channel_messages(channel_id=channel_id, limit=limit):
                yield message
                if not self.failed:
                    self.failed = True
                    raise PipelineTelegramProviderUnavailableError("forced initial disconnect")

    fake = _InitialStreamFailsOnce(
        canned_messages={"retry_initial_channel": messages},
        media_by_message={message.message_id: b"img" for message in messages},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake)

    first_report = await runtime.catch_up_channel("primary", "retry_initial_channel")
    await migrated_db_session.refresh(channel)

    assert first_report.messages_scanned == 1
    assert first_report.errors == ("provider_unavailable:forced initial disconnect",)
    assert channel.last_read_post_id == "8"
    assert channel.history_cursor_post_id == "8"
    assert channel.initial_catchup_completed is False

    second_report = await runtime.catch_up_channel("primary", "retry_initial_channel")
    await migrated_db_session.refresh(channel)

    assert second_report.messages_scanned == 3
    assert channel.last_read_post_id == "10"
    assert channel.history_cursor_post_id == "8"
    assert channel.initial_catchup_completed is True
    request_post_ids = set(
        (
            await migrated_db_session.execute(
                select(PipelineIngestRequest.post_id).where(
                    PipelineIngestRequest.source_id == "retry_initial_channel",
                ),
            )
        ).scalars()
    )
    assert request_post_ids == {"8", "9", "10"}


async def test_older_history_backfill_moves_only_the_oldest_cursor(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="older_channel",
        last_read_post_id="10",
    )
    channel.oldest_observed_post_id = "8"
    channel.history_cursor_post_id = "8"
    channel.initial_catchup_completed = True
    await migrated_db_session.commit()
    messages = [_build_photo_message(message_id=str(i), channel_id="older_channel") for i in range(1, 11)]
    fake = FakeTelegramClient(
        canned_messages={"older_channel": messages},
        media_by_message={message.message_id: b"img" for message in messages},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake)

    first_report = await runtime.catch_up_older_channel(
        "primary",
        "older_channel",
        before_post_id=None,
        limit=3,
    )
    await migrated_db_session.refresh(channel)

    assert first_report.messages_scanned == 3
    assert channel.last_read_post_id == "10"
    assert channel.oldest_observed_post_id == "5"
    assert channel.history_cursor_post_id == "5"
    assert channel.history_exhausted is False

    second_report = await runtime.catch_up_older_channel(
        "primary",
        "older_channel",
        before_post_id=channel.history_cursor_post_id,
        limit=10,
    )
    await migrated_db_session.refresh(channel)

    assert second_report.messages_scanned == 4
    assert channel.last_read_post_id == "10"
    assert channel.oldest_observed_post_id == "1"
    assert channel.history_cursor_post_id == "1"
    assert channel.history_exhausted is True


async def test_older_history_uses_legacy_checkpoint_boundary_not_inventory_minimum(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="legacy_gap_channel",
        last_read_post_id="511",
    )
    channel.oldest_observed_post_id = "6"
    channel.history_cursor_post_id = "512"
    channel.initial_catchup_completed = True
    await migrated_db_session.commit()
    messages = [
        _build_photo_message(message_id=str(i), channel_id="legacy_gap_channel")
        for i in (510, 511, 512)
    ]
    fake = FakeTelegramClient(
        canned_messages={"legacy_gap_channel": messages},
        media_by_message={message.message_id: b"img" for message in messages},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake)

    report = await runtime.catch_up_older_channel(
        "primary",
        "legacy_gap_channel",
        before_post_id=None,
        limit=10,
    )
    await migrated_db_session.refresh(channel)

    assert report.messages_scanned == 2
    assert channel.last_read_post_id == "511"
    assert channel.oldest_observed_post_id == "6"
    assert channel.history_cursor_post_id == "510"
    post_ids = set(
        (
            await migrated_db_session.execute(
                select(SourceChannelPost.post_id).where(
                    SourceChannelPost.source_channel_id == channel.id,
                ),
            )
        ).scalars()
    )
    assert post_ids == {"510", "511"}


async def test_older_history_failure_keeps_unprocessed_messages_below_committed_cursor(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="recoverable_older_channel",
        last_read_post_id="10",
    )
    channel.oldest_observed_post_id = "8"
    channel.history_cursor_post_id = "8"
    channel.initial_catchup_completed = True
    await migrated_db_session.commit()
    messages = [
        _build_photo_message(message_id=str(i), channel_id="recoverable_older_channel")
        for i in range(1, 11)
    ]
    fake = FakeTelegramClient(
        canned_messages={"recoverable_older_channel": messages},
        media_by_message={message.message_id: b"img" for message in messages},
    )

    class _CommitThenFailIngestService:
        def __init__(self) -> None:
            self.post_ids: list[str] = []
            self.fail_post_id: str | None = "5"

        async def try_accept_without_media(
            self,
            *,
            platform: SourcePlatform,
            source_id: str,
            post_id: str,
            published_at: datetime | None,
            advance_checkpoint: bool = True,
        ) -> CrawlerIngestResult | None:
            _ = (platform, source_id, post_id, published_at, advance_checkpoint)
            return None

        async def accept_crawler_post(
            self,
            raw_post: RawCrawlerPost,
            *,
            advance_checkpoint: bool = True,
        ) -> CrawlerIngestResult:
            _ = advance_checkpoint
            self.post_ids.append(raw_post.post_id)
            if raw_post.post_id == self.fail_post_id:
                raise RuntimeError("forced ingest failure")
            await migrated_db_session.commit()
            return CrawlerIngestResult(
                outcome=CrawlerIngestOutcome.INGESTED,
                received_at=_now(),
            )

    ingest_service = _CommitThenFailIngestService()
    runtime = TelegramCrawlerRuntime(
        ingest_service=ingest_service,
        telegram_client=fake,
        session=migrated_db_session,
        settings=Settings(),
    )

    with pytest.raises(RuntimeError, match="forced ingest failure"):
        await runtime.catch_up_older_channel(
            "primary",
            "recoverable_older_channel",
            before_post_id=None,
            limit=3,
        )
    await migrated_db_session.rollback()
    await migrated_db_session.refresh(channel)

    assert ingest_service.post_ids == ["7", "6", "5"]
    assert channel.history_cursor_post_id == "6"

    ingest_service.fail_post_id = None
    await runtime.catch_up_older_channel(
        "primary",
        "recoverable_older_channel",
        before_post_id=channel.history_cursor_post_id,
        limit=3,
    )

    assert ingest_service.post_ids[3:] == ["5", "4", "3"]


async def test_older_history_records_oversized_media_and_advances_history_cursor(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="oversized_history_channel",
        last_read_post_id="10",
    )
    channel.oldest_observed_post_id = "10"
    channel.history_cursor_post_id = "10"
    await migrated_db_session.commit()
    messages = [
        _build_photo_message(message_id=str(i), channel_id="oversized_history_channel")
        for i in range(1, 11)
    ]
    fake = FakeTelegramClient(
        canned_messages={"oversized_history_channel": messages},
        media_by_message={"9": b"ok", "8": b"large", "7": b"ok"},
    )
    runtime = _build_runtime(
        migrated_db_session,
        telegram_client=fake,
        settings=Settings(pipeline_image_upload_max_bytes=4),
    )

    report = await runtime.catch_up_older_channel(
        "primary",
        "oversized_history_channel",
        before_post_id=None,
        limit=3,
    )

    assert report.messages_scanned == 3
    assert report.messages_ingested == 2
    assert report.messages_skipped_unsupported == 1
    assert report.errors == ()
    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "10"
    assert channel.history_cursor_post_id == "7"
    oversized_post = await migrated_db_session.scalar(
        select(SourceChannelPost).where(
            SourceChannelPost.source_channel_id == channel.id,
            SourceChannelPost.post_id == "8",
        ),
    )
    assert oversized_post is not None
    assert oversized_post.status is SourceChannelPostStatus.UNSUPPORTED
    assert oversized_post.last_error_code == "pipeline_payload_too_large"


async def test_catch_up_channel_skips_duplicate_source_before_download(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="dedup_channel",
    )

    seed_message = _build_photo_message(message_id="10", channel_id="dedup_channel")
    seed_fake = FakeTelegramClient(
        canned_messages={"dedup_channel": [seed_message]},
        media_by_message={"10": b"seed-bytes"},
    )
    seed_runtime = _build_runtime(migrated_db_session, telegram_client=seed_fake, phash_tag="D")

    seed_report = await seed_runtime.catch_up_channel("primary", "dedup_channel")
    assert seed_report.messages_ingested == 1

    await migrated_db_session.refresh(channel)
    channel.last_read_post_id = None
    await migrated_db_session.commit()

    duplicate_message = _build_photo_message(message_id="10", channel_id="dedup_channel")
    new_message = _build_photo_message(message_id="11", channel_id="dedup_channel")
    fake = FakeTelegramClient(
        canned_messages={"dedup_channel": [duplicate_message, new_message]},
        media_by_message={
            "10": b"duplicate-should-not-download",
            "11": b"new-bytes",
        },
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="D")

    report = await runtime.catch_up_channel("primary", "dedup_channel")

    assert report.messages_scanned == 2
    assert report.messages_ingested == 1
    assert report.messages_skipped_dedup == 1
    assert fake.downloaded_message_ids == ["11"]

    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "11"


async def test_catch_up_channel_honors_paused_channel(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="paused_channel",
        is_paused=True,
    )
    fake = FakeTelegramClient(
        canned_messages={"paused_channel": [_build_photo_message(message_id="1")]},
        media_by_message={"1": b"img"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="P")

    report = await runtime.catch_up_channel("primary", "paused_channel")

    assert report.messages_scanned == 0
    assert report.messages_ingested == 0
    assert len(report.errors) == 1
    assert "paused" in report.errors[0]


async def test_catch_up_channel_honors_catchup_disabled(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="cold_channel",
        catchup_enabled=False,
    )
    fake = FakeTelegramClient(
        canned_messages={"cold_channel": [_build_photo_message(message_id="1")]},
        media_by_message={"1": b"img"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="C")

    report = await runtime.catch_up_channel("primary", "cold_channel")

    assert report.messages_scanned == 0
    assert report.messages_ingested == 0
    assert len(report.errors) == 1
    assert "catchup_disabled" in report.errors[0]


async def test_catch_up_channel_rejects_orphan_and_differently_assigned_channels(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_active_session(migrated_db_session, session_name="secondary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="orphan_channel",
        session_name=None,
    )
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="secondary_channel",
        session_name="secondary",
    )
    fake = FakeTelegramClient(
        canned_messages={
            "orphan_channel": [_build_photo_message(message_id="1", channel_id="orphan_channel")],
            "secondary_channel": [_build_photo_message(message_id="1", channel_id="secondary_channel")],
        },
        media_by_message={"1": b"img"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="O")

    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await runtime.catch_up_channel("primary", "orphan_channel")
    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await runtime.catch_up_channel("primary", "secondary_channel")

    assert fake.downloaded_message_ids == []


# ---------------------------------------------------------------------------
# Error + session-state paths
# ---------------------------------------------------------------------------


async def test_catch_up_channel_quarantines_session_on_banned_error(
    migrated_db_session: AsyncSession,
) -> None:
    session_row = await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="banned_channel",
    )

    class _BannedOnIter(FakeTelegramClient):
        async def iter_latest_channel_messages(
            self,
            *,
            channel_id: str,
            limit: int,
        ) -> AsyncIterator[RawTelegramMessage]:
            _ = (channel_id, limit)
            raise PipelineTelegramSessionBannedError("session revoked")
            yield  # pragma: no cover - unreachable, kept so the method is an async generator

    fake = _BannedOnIter(
        canned_messages={
            "banned_channel": [_build_photo_message(message_id="1", channel_id="banned_channel")],
        },
        media_by_message={"1": b"img"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="B")

    with pytest.raises(PipelineTelegramSessionBannedError):
        _ = await runtime.catch_up_channel("primary", "banned_channel")

    await migrated_db_session.refresh(session_row)
    assert session_row.status is TelegramSessionStatus.QUARANTINED
    assert session_row.quarantined_at is not None
    assert session_row.last_error_class == "PipelineTelegramSessionBannedError"
    assert session_row.last_error_text is not None
    assert "session revoked" in session_row.last_error_text


async def test_catch_up_channel_flood_wait_parks_session_with_partial_report(
    migrated_db_session: AsyncSession,
) -> None:
    session_row = await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="flood_channel",
    )

    # Two messages are successfully streamed. The FakeTelegramClient has a
    # tiny subclass that raises a flood wait on the SECOND download_media
    # call so the first message ingests and the second trips the flood.
    messages = [
        _build_photo_message(message_id="10", channel_id="flood_channel"),
        _build_photo_message(message_id="20", channel_id="flood_channel"),
    ]

    class _FloodOnSecondDownload(FakeTelegramClient):
        async def download_media(self, message: RawTelegramMessage) -> bytes:
            self.downloaded_message_ids.append(message.message_id)
            if len(self.downloaded_message_ids) >= 2:
                raise PipelineTelegramFloodWaitError(
                    "cooldown",
                    wait_seconds=300,
                )
            return self.media_by_message.get(message.message_id, b"img")

    fake = _FloodOnSecondDownload(
        canned_messages={"flood_channel": messages},
        media_by_message={"10": b"img"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="F")

    report = await runtime.catch_up_channel("primary", "flood_channel")

    assert report.messages_ingested == 1
    assert any("flood_wait" in e for e in report.errors)

    await migrated_db_session.refresh(session_row)
    assert session_row.status is TelegramSessionStatus.FLOOD_WAIT
    assert session_row.flood_wait_until is not None
    cooldown_delta = session_row.flood_wait_until - _now()
    # Allow generous slack for clock drift; the fake pins ``wait_seconds=300``.
    assert timedelta(seconds=250) < cooldown_delta < timedelta(seconds=350)

    await migrated_db_session.refresh(channel)
    # ``last_read_post_id`` advanced for the one message that successfully
    # ingested before the flood fired.
    assert channel.last_read_post_id == "10"


async def test_catch_up_channel_continues_after_per_message_provider_error(
    migrated_db_session: AsyncSession,
) -> None:
    session_row = await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="transient_channel",
    )

    messages = [
        _build_photo_message(message_id="10", channel_id="transient_channel"),
        _build_photo_message(message_id="20", channel_id="transient_channel"),
        _build_photo_message(message_id="30", channel_id="transient_channel"),
    ]

    fake = FakeTelegramClient(
        canned_messages={"transient_channel": messages},
        media_by_message={"10": b"img-10", "30": b"img-30"},
    )
    fake.download_errors["20"] = PipelineTelegramProviderUnavailableError("Telegram hiccup")
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="T")

    report = await runtime.catch_up_channel("primary", "transient_channel")

    # 3 scanned. Message 10 ingests successfully. Message 20's download
    # pin raises PipelineTelegramProviderUnavailableError so the loop
    # records an error and continues. Message 30 downloads cleanly and is
    # accepted as a second raw ingest request.
    assert report.messages_scanned == 3
    assert report.messages_ingested == 2
    assert report.messages_skipped_dedup == 0
    assert any("download_unavailable:20" in e for e in report.errors)

    await migrated_db_session.refresh(session_row)
    assert session_row.status is TelegramSessionStatus.ACTIVE
    failed_post = await migrated_db_session.scalar(
        select(SourceChannelPost).where(
            SourceChannelPost.source_channel_id == (
                select(SourceChannel.id)
                .where(SourceChannel.platform_id == "transient_channel")
                .scalar_subquery()
            ),
            SourceChannelPost.post_id == "20",
        ),
    )
    assert failed_post is not None
    assert failed_post.status is SourceChannelPostStatus.FAILED
    assert failed_post.last_error_code == "download_unavailable"


async def test_catch_up_channel_records_oversized_media_as_unsupported_and_continues(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="oversized_channel",
    )
    messages = [
        _build_photo_message(message_id="10", channel_id="oversized_channel"),
        _build_photo_message(message_id="20", channel_id="oversized_channel"),
        _build_photo_message(message_id="30", channel_id="oversized_channel"),
    ]
    fake = FakeTelegramClient(
        canned_messages={"oversized_channel": messages},
        media_by_message={"10": b"ok", "20": b"large", "30": b"ok"},
    )
    storage_client = FakeStorageClient()
    runtime = _build_runtime(
        migrated_db_session,
        telegram_client=fake,
        storage_client=storage_client,
        settings=Settings(pipeline_image_upload_max_bytes=4),
    )

    report = await runtime.catch_up_channel("primary", "oversized_channel")

    assert report.messages_scanned == 3
    assert report.messages_ingested == 2
    assert report.messages_skipped_unsupported == 1
    assert report.errors == ()
    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "30"
    assert channel.initial_catchup_completed is True
    posts = (
        await migrated_db_session.execute(
            select(SourceChannelPost)
            .where(SourceChannelPost.source_channel_id == channel.id)
            .order_by(SourceChannelPost.post_id.asc()),
        )
    ).scalars().all()
    assert [post.status for post in posts] == [
        SourceChannelPostStatus.ACCEPTED,
        SourceChannelPostStatus.UNSUPPORTED,
        SourceChannelPostStatus.ACCEPTED,
    ]
    assert posts[1].last_error_code == "pipeline_payload_too_large"
    assert posts[1].last_error_text == "Uploaded file exceeds the 4-byte limit."
    assert len(storage_client.put_calls) == 2
    assert await migrated_db_session.scalar(select(func.count()).select_from(PipelineIngestRequest)) == 2


async def test_catch_up_channel_preserves_forward_attribution_on_raw_request(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    # Seed BOTH the original source channel and the reposter channel so
    # forwards resolve cleanly.
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="reposter_channel",
        title="Reposter",
    )

    forward = CrawlerForwardAttribution(
        source_id="origin_channel",
        post_id="99",
        channel_username=None,
        channel_title="Origin",
    )
    message = _build_photo_message(
        message_id="42",
        channel_id="reposter_channel",
        forward=forward,
    )
    fake = FakeTelegramClient(
        canned_messages={"reposter_channel": [message]},
        media_by_message={"42": b"image"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="W")

    report = await runtime.catch_up_channel("primary", "reposter_channel")
    assert report.messages_ingested == 1

    request = (
        await migrated_db_session.execute(
            select(PipelineIngestRequest).where(PipelineIngestRequest.source_id == "reposter_channel"),
        )
    ).scalar_one()
    assert request.source_metadata["forward"] == forward.model_dump(mode="json")
    assert request.source_metadata["reactions"] == {"heart": 3}
    assert request.source_metadata["published_at"] == message.published_at.isoformat()


# ---------------------------------------------------------------------------
# Live listener lifecycle
# ---------------------------------------------------------------------------


async def test_live_listener_round_trips_one_message_and_stops_cleanly(
    migrated_db_session: AsyncSession,
) -> None:
    session_row = await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="live_channel",
        last_read_post_id="100",
    )
    channel.oldest_observed_post_id = "100"
    channel.history_cursor_post_id = "100"
    await migrated_db_session.commit()

    live_message = _build_photo_message(message_id="42", channel_id="live_channel")
    fake = FakeTelegramClient(
        live_messages={"live_channel": [live_message]},
        media_by_message={"42": b"live-bytes"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="V")

    await runtime.start_live_listener("primary")

    live_task = runtime._live_tasks["primary"]
    try:
        await asyncio.wait_for(
            asyncio.shield(live_task),
            timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS,
        )
    finally:
        # The one-shot fake task finishing proves ingest and heartbeat commits
        # completed. Always stop it so a watchdog failure cannot leak a task.
        await runtime.stop_live_listener("primary")

    assert fake.downloaded_message_ids == ["42"]
    await migrated_db_session.refresh(session_row)
    await migrated_db_session.refresh(channel)
    assert session_row.status is TelegramSessionStatus.STOPPED
    assert session_row.live_listener_started_at is None
    assert session_row.last_heartbeat_at is not None
    assert channel.last_read_post_id == "100"
    assert channel.oldest_observed_post_id == "42"
    assert channel.history_cursor_post_id == "100"


async def test_live_listener_skips_duplicate_source_before_download(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="live_dedup_channel",
    )

    seed_message = _build_photo_message(message_id="100", channel_id="live_dedup_channel")
    seed_fake = FakeTelegramClient(
        canned_messages={"live_dedup_channel": [seed_message]},
        media_by_message={"100": b"seed-live-bytes"},
    )
    seed_runtime = _build_runtime(migrated_db_session, telegram_client=seed_fake, phash_tag="L")
    seed_report = await seed_runtime.catch_up_channel("primary", "live_dedup_channel")
    assert seed_report.messages_ingested == 1

    duplicate_message = _build_photo_message(message_id="100", channel_id="live_dedup_channel")
    new_message = _build_photo_message(message_id="101", channel_id="live_dedup_channel")
    fake = FakeTelegramClient(
        live_messages={"live_dedup_channel": [duplicate_message, new_message]},
        media_by_message={
            "100": b"duplicate-live-should-not-download",
            "101": b"new-live-bytes",
        },
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="L")

    await runtime.start_live_listener("primary")

    live_task = runtime._live_tasks["primary"]
    try:
        await asyncio.wait_for(
            asyncio.shield(live_task),
            timeout=_LIVE_LISTENER_TEST_TIMEOUT_SECONDS,
        )
    finally:
        await runtime.stop_live_listener("primary")

    assert fake.downloaded_message_ids == ["101"]
    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "101"


async def test_live_listener_ignores_orphan_and_live_disabled_channels(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="orphan_live_channel",
        session_name=None,
    )
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="disabled_live_channel",
        live_enabled=False,
    )
    fake = FakeTelegramClient(
        live_messages={
            "orphan_live_channel": [_build_photo_message(message_id="100", channel_id="orphan_live_channel")],
            "disabled_live_channel": [_build_photo_message(message_id="101", channel_id="disabled_live_channel")],
        },
        media_by_message={"100": b"orphan", "101": b"disabled"},
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="N")

    await runtime.start_live_listener("primary")

    assert "primary" not in runtime._live_tasks
    assert fake.downloaded_message_ids == []


# ---------------------------------------------------------------------------
# replay_post + reassign_channel
# ---------------------------------------------------------------------------


async def test_replay_post_does_not_advance_checkpoint(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="replay_channel",
        last_read_post_id="500",
    )
    channel.oldest_observed_post_id = "100"
    channel.history_cursor_post_id = "100"
    await migrated_db_session.commit()

    message = _build_photo_message(message_id="42", channel_id="replay_channel")
    fake = FakeTelegramClient()
    fake.pin_single_message(
        channel_id="replay_channel",
        post_id="42",
        message=message,
        media=b"replay-bytes",
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="Y")

    result = await runtime.replay_post("replay_channel", "42")
    assert result.meme_file_id is None
    assert result.outcome is CrawlerIngestOutcome.INGESTED
    assert result.published_at is not None

    await migrated_db_session.refresh(channel)
    # The checkpoint must NOT regress or advance for an idempotent replay.
    assert channel.last_read_post_id == "500"
    assert channel.oldest_observed_post_id == "42"
    assert channel.history_cursor_post_id == "100"


async def test_replay_post_rejects_orphan_channel(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="orphan_replay_channel",
        session_name=None,
    )
    fake = FakeTelegramClient()
    fake.pin_single_message(
        channel_id="orphan_replay_channel",
        post_id="42",
        message=_build_photo_message(message_id="42", channel_id="orphan_replay_channel"),
        media=b"orphan-replay-bytes",
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="O")

    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await runtime.replay_post("orphan_replay_channel", "42")

    assert fake.downloaded_message_ids == []


async def test_reassign_channel_updates_session_binding(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_active_session(migrated_db_session, session_name="secondary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="movable_channel",
    )

    fake = FakeTelegramClient()
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="X")

    await runtime.reassign_channel("movable_channel", "secondary")

    await migrated_db_session.refresh(channel)
    assert channel.telegram_session_id == (
        await migrated_db_session.scalar(select(TelegramSession.id).where(TelegramSession.name == "secondary"))
    )


async def test_reassign_channel_rejects_unknown_session(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="guarded_channel",
    )

    fake = FakeTelegramClient()
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="Z")

    with pytest.raises(CrawlerSessionNotRunnableError):
        await runtime.reassign_channel("guarded_channel", "does_not_exist")


# ---------------------------------------------------------------------------
# Session-not-runnable guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [TelegramSessionStatus.STOPPED, TelegramSessionStatus.QUARANTINED],
)
async def test_catch_up_channel_refuses_non_active_session(
    migrated_db_session: AsyncSession,
    status: TelegramSessionStatus,
) -> None:
    row = TelegramSession(
        name="idle",
        display_name="Idle",
        status=status,
        last_heartbeat_at=_now(),
    )
    migrated_db_session.add(row)
    await migrated_db_session.commit()
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="some_channel",
        session_name="idle",
    )

    fake = FakeTelegramClient()
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="Q")

    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await runtime.catch_up_channel("idle", "some_channel")


async def test_catch_up_channel_refuses_disabled_session(
    migrated_db_session: AsyncSession,
) -> None:
    await _seed_active_session(migrated_db_session, session_name="disabled", enabled=False)
    await _seed_curated_channel(
        migrated_db_session,
        platform_id="disabled_session_channel",
        session_name="disabled",
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=FakeTelegramClient(), phash_tag="E")

    with pytest.raises(CrawlerSessionNotRunnableError):
        _ = await runtime.catch_up_channel("disabled", "disabled_session_channel")


# ---------------------------------------------------------------------------
# CrawlerOperationsService layered tests (T03)
# ---------------------------------------------------------------------------


async def test_crawler_operations_service_reassign_updates_session_binding_and_projects_row(
    migrated_db_session: AsyncSession,
) -> None:
    from memexpert.services import CrawlerInvalidSessionError
    from memexpert.services.crawler_operations import CrawlerOperationsService

    await _seed_active_session(migrated_db_session, session_name="primary")
    await _seed_active_session(migrated_db_session, session_name="secondary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="service_movable",
    )
    runtime = _build_runtime(
        migrated_db_session,
        telegram_client=FakeTelegramClient(),
        phash_tag="S",
    )
    service = CrawlerOperationsService(session=migrated_db_session, runtime=runtime)

    projection = await service.reassign_channel(
        channel.id,
        new_session_name="secondary",
    )
    assert projection.telegram_session_name == "secondary"
    assert projection.id == channel.id

    # The durable row actually moved to the new binding.
    await migrated_db_session.refresh(channel)
    assert channel.telegram_session_id == (
        await migrated_db_session.scalar(select(TelegramSession.id).where(TelegramSession.name == "secondary"))
    )

    # Unknown target session surfaces as the distinct typed error.
    with pytest.raises(CrawlerInvalidSessionError):
        _ = await service.reassign_channel(channel.id, new_session_name="ghost")


async def test_crawler_operations_service_replay_channel_post_delegates_to_runtime(
    migrated_db_session: AsyncSession,
) -> None:
    from memexpert.services.crawler_operations import CrawlerOperationsService

    await _seed_active_session(migrated_db_session, session_name="primary")
    channel = await _seed_curated_channel(
        migrated_db_session,
        platform_id="service_replay",
        last_read_post_id="999",
    )

    fake = FakeTelegramClient()
    fake.pin_single_message(
        channel_id="service_replay",
        post_id="42",
        message=_build_photo_message(message_id="42", channel_id="service_replay"),
        media=b"service-replay-bytes",
    )
    runtime = _build_runtime(migrated_db_session, telegram_client=fake, phash_tag="D")
    service = CrawlerOperationsService(session=migrated_db_session, runtime=runtime)

    result = await service.replay_channel_post(channel.id, post_id="42")
    assert result.outcome is CrawlerIngestOutcome.INGESTED
    assert result.meme_file_id is None

    # Replay still does not advance the durable checkpoint.
    await migrated_db_session.refresh(channel)
    assert channel.last_read_post_id == "999"
