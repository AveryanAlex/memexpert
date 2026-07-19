"""Integration coverage for the isolated Telegram post metadata backfill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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
)
from memexpert.crawlers.telegram.metadata_backfill import TelegramPostMetadataBackfiller
from memexpert.models.content import (
    PipelineIngestRequest,
    RabbitMQOutboxMessage,
    SourceChannel,
    SourceChannelPost,
    TelegramSession,
)
from memexpert.models.enums import (
    PipelineIngestRequestStatus,
    SourceChannelPostStatus,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.schemas.telegram_post import TelegramPostMetadata, TelegramTextEntity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _now() -> datetime:
    return datetime.now(tz=UTC)


class _RecordingTelegramClient(FakeTelegramClient):
    def __init__(
        self,
        *,
        single_messages: dict[tuple[str, str], RawTelegramMessage] | None = None,
        errors: Sequence[Exception | None] = (),
        before_fetch: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(single_messages=single_messages or {})
        self.fetch_batches: list[tuple[str, tuple[str, ...]]] = []
        self._errors = list(errors)
        self._before_fetch = before_fetch

    async def fetch_messages(
        self,
        *,
        channel_id: str,
        post_ids: Sequence[str],
    ) -> dict[str, RawTelegramMessage | None]:
        self.fetch_batches.append((channel_id, tuple(post_ids)))
        if self._before_fetch is not None:
            await self._before_fetch(len(self.fetch_batches))
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        return await super().fetch_messages(channel_id=channel_id, post_ids=post_ids)


def _raw_message(
    *,
    channel_id: str,
    post_id: str,
    metadata: TelegramPostMetadata,
) -> RawTelegramMessage:
    return RawTelegramMessage(
        message_id=post_id,
        channel_id=channel_id,
        channel_username=None,
        channel_title="Backfill channel",
        published_at=_now(),
        media_type=None,
        telegram_post=metadata,
    )


async def _seed_session(session: AsyncSession, *, name: str = "metadata-session") -> TelegramSession:
    row = TelegramSession(
        name=name,
        display_name=name,
        encrypted_string_session=f"encrypted-{name}",
        status=TelegramSessionStatus.ACTIVE,
        enabled=True,
        max_requests_per_second=2.5,
    )
    session.add(row)
    await session.flush()
    return row


async def _seed_channel(
    session: AsyncSession,
    *,
    telegram_session: TelegramSession | None,
    platform_id: str,
    username: str | None = None,
) -> SourceChannel:
    row = SourceChannel(
        platform=SourcePlatform.TELEGRAM,
        platform_id=platform_id,
        username=username,
        title=platform_id,
        telegram_session_id=None if telegram_session is None else telegram_session.id,
        last_read_post_id="900",
        oldest_observed_post_id="10",
        history_cursor_post_id="9",
        last_fetched_at=_now() - timedelta(days=1),
        initial_catchup_completed=True,
    )
    session.add(row)
    await session.flush()
    return row


def _post(
    channel: SourceChannel,
    *,
    post_id: str,
    status: SourceChannelPostStatus = SourceChannelPostStatus.ACCEPTED,
) -> SourceChannelPost:
    return SourceChannelPost(
        source_channel_id=channel.id,
        post_id=post_id,
        published_at=_now() - timedelta(days=2),
        media_type="photo",
        status=status,
        attempt_count=7,
        last_error_code="preserve-error",
        last_error_text="preserve details",
        is_retryable=True,
        last_attempt_at=_now() - timedelta(hours=1),
    )


async def test_apply_backfills_text_no_text_and_deletion_without_touching_pipeline_state(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_session = await _seed_session(migrated_db_session)
    channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="metadata_channel",
    )
    text_post = _post(channel, post_id="10")
    no_text_post = _post(channel, post_id="11", status=SourceChannelPostStatus.UNSUPPORTED)
    missing_post = _post(channel, post_id="12", status=SourceChannelPostStatus.FAILED)
    ingest_request = PipelineIngestRequest(
        source_platform=SourcePlatform.TELEGRAM,
        source_id=channel.platform_id,
        post_id=text_post.post_id,
        source_metadata={"preserved": True},
        status=PipelineIngestRequestStatus.MEDIA_INSPECTING,
        attempt_count=4,
    )
    migrated_db_session.add_all([text_post, no_text_post, missing_post, ingest_request])
    await migrated_db_session.commit()
    channel_id = channel.id
    ingest_request_id = ingest_request.id

    original_checkpoint = (
        channel.last_read_post_id,
        channel.oldest_observed_post_id,
        channel.history_cursor_post_id,
        channel.last_fetched_at,
    )
    original_post_state = {
        row.post_id: (
            row.status,
            row.attempt_count,
            row.last_error_code,
            row.last_error_text,
            row.is_retryable,
            row.last_attempt_at,
        )
        for row in (text_post, no_text_post, missing_post)
    }
    edited_at = _now() - timedelta(minutes=5)
    client = _RecordingTelegramClient(
        single_messages={
            (channel.platform_id, "10"): _raw_message(
                channel_id=channel.platform_id,
                post_id="10",
                metadata=TelegramPostMetadata(
                    text="  Привет 👋\ncaption  ",
                    text_entities=(TelegramTextEntity(type="bold", offset=2, length=6),),
                    media_group_id="987654321",
                    reply_to_post_id="9",
                    edited_at=edited_at,
                ),
            ),
            (channel.platform_id, "11"): _raw_message(
                channel_id=channel.platform_id,
                post_id="11",
                metadata=TelegramPostMetadata(text=None),
            ),
        },
    )
    created_for: list[str] = []
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=lambda row: (created_for.append(row.name), client)[1],
    )

    result = await backfiller.run(dry_run=False, batch_size=2)

    assert result.candidates_inspected == 3
    assert result.batches_processed == 2
    assert result.captured == 2
    assert result.missing == 1
    assert result.transient_failures == 0
    assert created_for == [telegram_session.name]
    assert client.fetch_batches == [(channel.platform_id, ("10", "11")), (channel.platform_id, ("12",))]
    assert client.downloaded_message_ids == []
    assert client.closed is True

    migrated_db_session.expire_all()
    persisted_posts = list(
        (
            await migrated_db_session.execute(
                select(SourceChannelPost)
                .where(SourceChannelPost.source_channel_id == channel_id)
                .order_by(SourceChannelPost.post_id.asc()),
            )
        )
        .scalars()
        .all(),
    )
    persisted_text, persisted_no_text, persisted_missing = persisted_posts
    assert persisted_text.first_observed_text == "  Привет 👋\ncaption  "
    assert persisted_text.latest_text == "  Привет 👋\ncaption  "
    assert persisted_text.first_observed_text_entities == [{"type": "bold", "offset": 2, "length": 6}]
    assert persisted_text.latest_text_entities == [{"type": "bold", "offset": 2, "length": 6}]
    assert persisted_text.media_group_id == "987654321"
    assert persisted_text.reply_to_post_id == "9"
    assert persisted_text.telegram_edited_at == edited_at
    assert persisted_text.metadata_version == 1
    assert persisted_text.metadata_first_observed_at is not None
    assert persisted_text.metadata_last_observed_at == persisted_text.metadata_first_observed_at
    assert persisted_text.is_deleted is False
    assert persisted_no_text.first_observed_text is None
    assert persisted_no_text.latest_text is None
    assert persisted_no_text.first_observed_text_entities == []
    assert persisted_no_text.metadata_version == 1
    assert persisted_missing.metadata_version == 0
    assert persisted_missing.is_deleted is True
    assert persisted_missing.deletion_observed_at is not None
    for row in persisted_posts:
        assert (
            row.status,
            row.attempt_count,
            row.last_error_code,
            row.last_error_text,
            row.is_retryable,
            row.last_attempt_at,
        ) == original_post_state[row.post_id]

    persisted_channel = await migrated_db_session.get(SourceChannel, channel_id)
    assert persisted_channel is not None
    assert (
        persisted_channel.last_read_post_id,
        persisted_channel.oldest_observed_post_id,
        persisted_channel.history_cursor_post_id,
        persisted_channel.last_fetched_at,
    ) == original_checkpoint
    persisted_request = await migrated_db_session.get(PipelineIngestRequest, ingest_request_id)
    assert persisted_request is not None
    assert persisted_request.status is PipelineIngestRequestStatus.MEDIA_INSPECTING
    assert persisted_request.attempt_count == 4
    assert persisted_request.source_metadata == {"preserved": True}
    assert await migrated_db_session.scalar(select(func.count()).select_from(RabbitMQOutboxMessage)) == 0

    rerun = await backfiller.run(dry_run=False, batch_size=1)
    assert rerun.candidates_inspected == 0
    assert client.fetch_batches == [(channel.platform_id, ("10", "11")), (channel.platform_id, ("12",))]


async def test_dry_run_and_repeatable_channel_filters_do_not_mutate_rows(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_session = await _seed_session(migrated_db_session, name="filter-session")
    selected = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="selected-id",
        username="selected_username",
    )
    ignored = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="ignored-id",
        username="ignored_username",
    )
    selected_post = _post(selected, post_id="21")
    ignored_post = _post(ignored, post_id="22")
    migrated_db_session.add_all([selected_post, ignored_post])
    await migrated_db_session.commit()
    selected_post_id = selected_post.id
    ignored_post_id = ignored_post.id
    client = _RecordingTelegramClient(
        single_messages={
            (selected.platform_id, selected_post.post_id): _raw_message(
                channel_id=selected.platform_id,
                post_id=selected_post.post_id,
                metadata=TelegramPostMetadata(text="preview only"),
            ),
        },
    )
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=lambda _row: client,
    )

    result = await backfiller.run(
        dry_run=True,
        channel_filters=("not-a-channel", "@selected_username"),
        batch_size=1,
    )

    assert result.dry_run is True
    assert result.channels_inspected == 1
    assert result.candidates_inspected == 1
    assert result.captured == 1
    assert client.fetch_batches == [(selected.platform_id, (selected_post.post_id,))]
    migrated_db_session.expire_all()
    persisted_selected = await migrated_db_session.get(SourceChannelPost, selected_post_id)
    persisted_ignored = await migrated_db_session.get(SourceChannelPost, ignored_post_id)
    assert persisted_selected is not None
    assert persisted_ignored is not None
    assert persisted_selected.metadata_version == 0
    assert persisted_ignored.metadata_version == 0


async def test_dry_run_default_client_factory_disables_telethon_session_state_persistence(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memexpert.crawlers.telegram.telethon_adapter as adapter_module

    telegram_session = await _seed_session(migrated_db_session, name="read-only-backfill-session")
    channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="read-only-backfill-channel",
    )
    post = _post(channel, post_id="25")
    migrated_db_session.add(post)
    await migrated_db_session.commit()
    calls: list[tuple[Settings, str, bool]] = []
    expected_client = FakeTelegramClient(
        single_messages={
            (channel.platform_id, post.post_id): _raw_message(
                channel_id=channel.platform_id,
                post_id=post.post_id,
                metadata=TelegramPostMetadata(text="preview only"),
            ),
        },
    )

    class _PipelineTelethonClient:
        @staticmethod
        def create(
            *,
            settings: Settings,
            session_name: str,
            persist_session_state: bool,
        ) -> FakeTelegramClient:
            calls.append((settings, session_name, persist_session_state))
            return expected_client

    monkeypatch.setattr(adapter_module, "PipelineTelethonClient", _PipelineTelethonClient)
    settings = Settings()
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=settings,
    )

    result = await backfiller.run(dry_run=True, batch_size=1)

    assert result.captured == 1
    assert calls == [(settings, telegram_session.name, False)]


async def test_transient_batch_is_retriable_and_flood_wait_parks_and_skips_assigned_session(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_session = await _seed_session(migrated_db_session, name="failure-session")
    first_channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="failure-one",
    )
    second_channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="failure-two",
    )
    posts = [
        _post(first_channel, post_id="31"),
        _post(first_channel, post_id="32"),
        _post(first_channel, post_id="33"),
        _post(second_channel, post_id="34"),
    ]
    migrated_db_session.add_all(posts)
    await migrated_db_session.commit()
    post_row_ids = tuple(post.id for post in posts)
    telegram_session_id = telegram_session.id

    async def assert_previous_batch_committed(fetch_number: int) -> None:
        if fetch_number != 3:
            return
        async with postgres_session_factory() as session:
            second = await session.get(SourceChannelPost, posts[1].id)
            assert second is not None
            assert second.metadata_version == 1

    client = _RecordingTelegramClient(
        single_messages={
            (first_channel.platform_id, "32"): _raw_message(
                channel_id=first_channel.platform_id,
                post_id="32",
                metadata=TelegramPostMetadata(text="committed batch"),
            ),
        },
        errors=(
            PipelineTelegramProviderUnavailableError("temporary outage"),
            None,
            PipelineTelegramFloodWaitError("slow down", wait_seconds=90),
        ),
        before_fetch=assert_previous_batch_committed,
    )
    factory_calls: list[str] = []
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=lambda row: (factory_calls.append(row.name), client)[1],
    )

    result = await backfiller.run(dry_run=False, batch_size=1)

    assert result.candidates_inspected == 3
    assert result.captured == 1
    assert result.transient_failures == 2
    assert result.sessions_parked == 1
    assert result.sessions_skipped == 0
    assert result.sessions_requiring_attention == 0
    assert result.retry_required is True
    assert factory_calls == [telegram_session.name]
    assert client.fetch_batches == [
        (first_channel.platform_id, ("31",)),
        (first_channel.platform_id, ("32",)),
        (first_channel.platform_id, ("33",)),
    ]

    migrated_db_session.expire_all()
    persisted = {
        row.post_id: row
        for row in (
            await migrated_db_session.execute(
                select(SourceChannelPost).where(
                    SourceChannelPost.id.in_(post_row_ids),
                ),
            )
        )
        .scalars()
        .all()
    }
    assert persisted["31"].metadata_version == 0
    assert persisted["32"].metadata_version == 1
    assert persisted["33"].metadata_version == 0
    assert persisted["34"].metadata_version == 0
    parked = await migrated_db_session.get(TelegramSession, telegram_session_id)
    assert parked is not None
    assert parked.status is TelegramSessionStatus.FLOOD_WAIT
    assert parked.flood_wait_until is not None and parked.flood_wait_until > _now()
    assert parked.last_error_class == "PipelineTelegramFloodWaitError"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_quarantined"),
    [
        (
            PipelineTelegramSessionBannedError("session revoked"),
            TelegramSessionStatus.QUARANTINED,
            True,
        ),
        (
            PipelineTelegramSessionAuthRequiredError("auth key revoked"),
            TelegramSessionStatus.AUTH_REQUIRED,
            False,
        ),
        (
            PipelineTelegramSessionNotRunnableError("session cannot run"),
            TelegramSessionStatus.QUARANTINED,
            True,
        ),
        (
            PipelineTelegramMalformedMessageError("malformed post"),
            TelegramSessionStatus.ACTIVE,
            False,
        ),
    ],
)
async def test_apply_classifies_terminal_errors_without_counting_them_as_transient(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    error: Exception,
    expected_status: TelegramSessionStatus,
    expected_quarantined: bool,
) -> None:
    telegram_session = await _seed_session(migrated_db_session, name="terminal-session")
    channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="terminal-channel",
    )
    post = _post(channel, post_id="41")
    migrated_db_session.add(post)
    await migrated_db_session.commit()
    session_id = telegram_session.id
    post_id = post.id
    client = _RecordingTelegramClient(errors=(error,))
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=lambda _row: client,
    )

    result = await backfiller.run(dry_run=False, batch_size=1)

    assert result.candidates_inspected == 1
    assert result.captured == 0
    assert result.missing == 0
    assert result.transient_failures == 0
    assert result.permanent_failures == 1
    assert result.sessions_quarantined == int(expected_quarantined)
    assert result.sessions_requiring_attention == int(
        not isinstance(error, PipelineTelegramMalformedMessageError),
    )
    assert client.closed is True

    migrated_db_session.expire_all()
    persisted_session = await migrated_db_session.get(TelegramSession, session_id)
    persisted_post = await migrated_db_session.get(SourceChannelPost, post_id)
    assert persisted_session is not None
    assert persisted_post is not None
    assert persisted_session.status is expected_status
    assert (persisted_session.quarantined_at is not None) is expected_quarantined
    if expected_status is TelegramSessionStatus.ACTIVE:
        assert persisted_session.last_error_class is None
        assert persisted_session.last_error_text is None
    else:
        assert persisted_session.last_error_class == type(error).__name__
        assert persisted_session.last_error_text == str(error)
    assert persisted_post.metadata_version == 0
    assert persisted_post.is_deleted is False


async def test_banned_session_dry_run_reports_terminal_outcome_without_quarantining(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_session = await _seed_session(migrated_db_session, name="dry-run-banned-session")
    channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="dry-run-banned-channel",
    )
    post = _post(channel, post_id="51")
    migrated_db_session.add(post)
    await migrated_db_session.commit()
    session_id = telegram_session.id
    post_id = post.id
    client = _RecordingTelegramClient(
        errors=(PipelineTelegramSessionBannedError("session revoked in preview"),),
    )
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=lambda _row: client,
    )

    result = await backfiller.run(dry_run=True, batch_size=1)

    assert result.dry_run is True
    assert result.transient_failures == 0
    assert result.permanent_failures == 1
    assert result.sessions_quarantined == 1
    assert result.sessions_requiring_attention == 1

    migrated_db_session.expire_all()
    persisted_session = await migrated_db_session.get(TelegramSession, session_id)
    persisted_post = await migrated_db_session.get(SourceChannelPost, post_id)
    assert persisted_session is not None
    assert persisted_post is not None
    assert persisted_session.status is TelegramSessionStatus.ACTIVE
    assert persisted_session.quarantined_at is None
    assert persisted_session.last_error_class is None
    assert persisted_session.last_error_text is None
    assert persisted_post.metadata_version == 0
    assert persisted_post.is_deleted is False


@pytest.mark.parametrize(
    ("status", "cooldown_seconds", "requires_attention"),
    [
        (TelegramSessionStatus.AUTH_REQUIRED, None, True),
        (TelegramSessionStatus.FLOOD_WAIT, 300, False),
        (TelegramSessionStatus.FLOOD_WAIT, -300, True),
    ],
    ids=("auth-required", "active-flood-wait", "expired-flood-wait"),
)
async def test_preexisting_unavailable_session_is_counted_once_and_classified(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    status: TelegramSessionStatus,
    cooldown_seconds: int | None,
    requires_attention: bool,
) -> None:
    telegram_session = await _seed_session(
        migrated_db_session,
        name=f"preexisting-{status.value}-{cooldown_seconds}",
    )
    telegram_session.status = status
    telegram_session.flood_wait_until = (
        None if cooldown_seconds is None else _now() + timedelta(seconds=cooldown_seconds)
    )
    first_channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id=f"preexisting-first-{cooldown_seconds}",
    )
    second_channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id=f"preexisting-second-{cooldown_seconds}",
    )
    migrated_db_session.add_all(
        [
            _post(first_channel, post_id="61"),
            _post(second_channel, post_id="62"),
        ],
    )
    await migrated_db_session.commit()
    factory_calls: list[str] = []

    def client_factory(row: TelegramSession) -> FakeTelegramClient:
        factory_calls.append(row.name)
        return FakeTelegramClient()

    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=client_factory,
    )

    result = await backfiller.run(dry_run=True, batch_size=1)

    assert result.channels_inspected == 2
    assert result.candidates_inspected == 0
    assert result.sessions_skipped == 1
    assert result.sessions_requiring_attention == int(requires_attention)
    assert result.operator_attention_required is requires_attention
    assert result.retry_required is True
    assert factory_calls == []


async def test_mismatched_batch_messages_are_permanent_but_omitted_keys_are_transient(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram_session = await _seed_session(migrated_db_session, name="contract-session")
    channel = await _seed_channel(
        migrated_db_session,
        telegram_session=telegram_session,
        platform_id="contract-channel",
    )
    posts = [_post(channel, post_id=post_id) for post_id in ("70", "71", "72")]
    migrated_db_session.add_all(posts)
    await migrated_db_session.commit()
    post_ids = tuple(post.id for post in posts)

    class _ContractBreakingClient(_RecordingTelegramClient):
        async def fetch_messages(
            self,
            *,
            channel_id: str,
            post_ids: Sequence[str],
        ) -> dict[str, RawTelegramMessage | None]:
            self.fetch_batches.append((channel_id, tuple(post_ids)))
            return {
                "70": _raw_message(
                    channel_id=channel_id,
                    post_id="700",
                    metadata=TelegramPostMetadata(text="wrong id"),
                ),
                "71": _raw_message(
                    channel_id="different-channel",
                    post_id="71",
                    metadata=TelegramPostMetadata(text="wrong channel"),
                ),
                # An omitted requested key is a transport/adapter uncertainty,
                # not Telegram explicitly reporting the message missing.
            }

    client = _ContractBreakingClient()
    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=lambda _row: client,
    )

    result = await backfiller.run(dry_run=False, batch_size=3)

    assert result.permanent_failures == 2
    assert result.transient_failures == 1
    assert result.missing == 0
    assert result.operator_attention_required is True
    assert result.retry_required is True
    migrated_db_session.expire_all()
    persisted = list(
        (
            await migrated_db_session.execute(
                select(SourceChannelPost).where(SourceChannelPost.id.in_(post_ids)),
            )
        )
        .scalars()
        .all(),
    )
    assert all(post.metadata_version == 0 and not post.is_deleted for post in persisted)


async def test_unassigned_channel_with_candidates_requires_operator_attention(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    channel = await _seed_channel(
        migrated_db_session,
        telegram_session=None,
        platform_id="unassigned-channel",
    )
    post = _post(channel, post_id="80")
    migrated_db_session.add(post)
    await migrated_db_session.commit()
    post_id = post.id
    factory_calls: list[str] = []

    def client_factory(row: TelegramSession) -> FakeTelegramClient:
        factory_calls.append(row.name)
        return FakeTelegramClient()

    backfiller = TelegramPostMetadataBackfiller(
        postgres_session_factory,
        settings=Settings(),
        telegram_client_factory=client_factory,
    )

    result = await backfiller.run(dry_run=True, batch_size=1)

    assert result.channels_inspected == 1
    assert result.candidates_inspected == 0
    assert result.unassigned_channels == 1
    assert result.operator_attention_required is True
    assert result.retry_required is False
    assert factory_calls == []
    migrated_db_session.expire_all()
    persisted_post = await migrated_db_session.get(SourceChannelPost, post_id)
    assert persisted_post is not None
    assert persisted_post.metadata_version == 0
    assert persisted_post.is_deleted is False
