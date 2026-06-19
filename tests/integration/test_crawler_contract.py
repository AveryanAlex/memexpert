"""Contract tests for the Telegram crawler adapter protocol, fake client, and stub runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from memexpert.core.config import Settings
from memexpert.crawlers.telegram.client import (
    FakeTelegramClient,
    PipelineTelegramClientProtocol,
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramMessageMapper,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionBannedError,
    RawTelegramChannel,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.runtime import (
    CrawlerCatchupReport,
    TelegramCrawlerRuntime,
)
from memexpert.models.content import TelegramSessionState
from memexpert.models.enums import SourcePlatform, TelegramSessionStatus
from memexpert.schemas.content_pipeline import (
    CrawlerForwardAttribution,
    CrawlerIngestOutcome,
    CrawlerIngestResult,
    RawCrawlerPost,
    TelegramSessionStateRead,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> datetime:
    return datetime.now(tz=UTC)


_TelegramMediaType = Literal["photo", "gif", "video", "unsupported"] | None


def _build_message(
    *,
    message_id: str = "42",
    channel_id: str = "memes_channel",
    media_type: str = "photo",
    forward: CrawlerForwardAttribution | None = None,
) -> RawTelegramMessage:
    return RawTelegramMessage(
        message_id=message_id,
        channel_id=channel_id,
        channel_username=None,
        channel_title="Memes Channel",
        published_at=_now(),
        media_type=cast("_TelegramMediaType", media_type),
        views=11,
        reactions={"heart": 3},
        forward=forward,
    )


def test_pipeline_telegram_message_mapper_builds_raw_crawler_post_with_forward() -> None:
    forward = CrawlerForwardAttribution(
        source_id="original_channel",
        post_id="99",
        channel_username=None,
        channel_title="Original",
    )
    message = _build_message(forward=forward)
    raw_post = PipelineTelegramMessageMapper.build_raw_crawler_post(message, b"image-bytes")

    assert isinstance(raw_post, RawCrawlerPost)
    assert raw_post.platform is SourcePlatform.TELEGRAM
    assert raw_post.source_id == "memes_channel"
    assert raw_post.post_id == "42"
    assert raw_post.media_type == "photo"
    assert raw_post.media_bytes == b"image-bytes"
    assert raw_post.forward == forward
    assert raw_post.published_at == message.published_at
    # Filename + content_type are intentionally None — the service fills in
    # crawler-friendly defaults when reaching the media processor.
    assert raw_post.filename is None
    assert raw_post.content_type is None


def test_pipeline_telegram_message_mapper_rejects_unsupported_media_type() -> None:
    message = _build_message(media_type="unsupported")
    with pytest.raises(PipelineTelegramMalformedMessageError):
        _ = PipelineTelegramMessageMapper.build_raw_crawler_post(message, b"irrelevant")


def test_pipeline_telegram_message_mapper_rejects_empty_media_bytes() -> None:
    message = _build_message()
    with pytest.raises(PipelineTelegramMalformedMessageError):
        _ = PipelineTelegramMessageMapper.build_raw_crawler_post(message, b"")


def test_raw_crawler_post_rejects_non_telegram_platforms() -> None:
    with pytest.raises(ValidationError):
        _ = RawCrawlerPost(
            platform=SourcePlatform.REDDIT,
            source_id="r/memes",
            post_id="abc",
            published_at=_now(),
            channel_username=None,
            channel_title=None,
            media_type="photo",
            media_bytes=b"bytes",
            filename=None,
            content_type=None,
            views=0,
            reactions={},
            forward=None,
        )


async def test_fake_telegram_client_iter_channel_messages_respects_limit_and_min_id() -> None:
    messages = [
        _build_message(message_id="10"),
        _build_message(message_id="20"),
        _build_message(message_id="30"),
    ]
    client = FakeTelegramClient(
        canned_messages={"memes_channel": messages},
        media_by_message={msg.message_id: b"bytes" for msg in messages},
    )
    # Sanity-check that the fake honors the declared protocol.
    assert isinstance(client, PipelineTelegramClientProtocol)

    collected = [
        message
        async for message in client.iter_channel_messages(
            channel_id="memes_channel",
            min_message_id=15,
            limit=5,
        )
    ]
    assert [m.message_id for m in collected] == ["20", "30"]

    bounded = [
        message
        async for message in client.iter_channel_messages(
            channel_id="memes_channel",
            min_message_id=None,
            limit=1,
        )
    ]
    assert [m.message_id for m in bounded] == ["10"]


async def test_fake_telegram_client_download_media_and_close_track_calls() -> None:
    message = _build_message()
    client = FakeTelegramClient(
        canned_messages={"memes_channel": [message]},
        media_by_message={"42": b"image-bytes"},
    )

    media = await client.download_media(message)
    assert media == b"image-bytes"
    assert client.downloaded_message_ids == ["42"]

    assert client.closed is False
    await client.close()
    assert client.closed is True


async def test_fake_telegram_client_raises_pinned_error_then_recovers() -> None:
    message = _build_message()
    client = FakeTelegramClient(
        canned_messages={"memes_channel": [message]},
        media_by_message={"42": b"image-bytes"},
        next_error=PipelineTelegramFloodWaitError("cooldown", wait_seconds=5),
    )

    with pytest.raises(PipelineTelegramFloodWaitError):
        async for _ in client.iter_channel_messages(
            channel_id="memes_channel",
            min_message_id=None,
            limit=1,
        ):
            pass

    # After the pinned error fires once, subsequent calls behave normally.
    collected = [
        message
        async for message in client.iter_channel_messages(
            channel_id="memes_channel",
            min_message_id=None,
            limit=1,
        )
    ]
    assert len(collected) == 1


async def test_fake_telegram_client_resolve_channel_returns_canned_row() -> None:
    canned = RawTelegramChannel(
        channel_id="memes_channel",
        username="memes",
        title="Memes Channel",
        subscriber_count=1234,
    )
    client = FakeTelegramClient(canned_channels={"memes": canned})

    resolved = await client.resolve_channel("memes")
    assert resolved == canned

    with pytest.raises(PipelineTelegramMalformedMessageError):
        _ = await client.resolve_channel("unknown")


async def test_fake_telegram_client_fetch_single_message_returns_pinned_entry() -> None:
    message = _build_message(message_id="100", channel_id="memes_channel")
    client = FakeTelegramClient()
    client.pin_single_message(
        channel_id="memes_channel",
        post_id="100",
        message=message,
        media=b"single-message-media",
    )

    fetched = await client.fetch_single_message(
        channel_id="memes_channel",
        post_id="100",
    )
    assert fetched is message

    # The media helper pins media alongside the message entry.
    media_bytes = await client.download_media(fetched)
    assert media_bytes == b"single-message-media"

    # Unknown (channel, post) pairs raise the typed malformed error.
    with pytest.raises(PipelineTelegramMalformedMessageError):
        _ = await client.fetch_single_message(
            channel_id="memes_channel",
            post_id="999",
        )


def test_pipeline_telegram_errors_form_a_taxonomy() -> None:
    # Every concrete error class must subclass the base so the runtime
    # dispatcher can classify them with a single except clause.
    assert issubclass(PipelineTelegramFloodWaitError, PipelineTelegramError)
    assert issubclass(PipelineTelegramProviderUnavailableError, PipelineTelegramError)
    assert issubclass(PipelineTelegramSessionBannedError, PipelineTelegramError)
    assert issubclass(PipelineTelegramMalformedMessageError, PipelineTelegramError)


@dataclass(slots=True)
class _StubCrawlerIngestService:
    """Minimal crawler ingest double for runtime stub tests."""

    calls: list[RawCrawlerPost] = field(default_factory=list)

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
        self.calls.append(raw_post)
        return CrawlerIngestResult(
            outcome=CrawlerIngestOutcome.INGESTED,
            received_at=_now(),
        )


async def test_telegram_crawler_runtime_load_session_state_creates_and_returns_rows(
    migrated_db_session: AsyncSession,
) -> None:
    service = _StubCrawlerIngestService()
    client = FakeTelegramClient()
    runtime = TelegramCrawlerRuntime(
        ingest_service=service,
        telegram_client=client,
        session=migrated_db_session,
        settings=Settings(),
    )

    row = await runtime._load_session_state("primary")
    assert isinstance(row, TelegramSessionState)
    assert row.session_name == "primary"
    assert row.status is TelegramSessionStatus.STOPPED

    # Calling again returns the existing row instead of inserting a duplicate.
    same_row = await runtime._load_session_state("primary")
    assert same_row.id == row.id

    # The read projection serializes the row shape operator surfaces rely on.
    projection = TelegramSessionStateRead.model_validate(row)
    assert projection.session_name == "primary"
    assert projection.status is TelegramSessionStatus.STOPPED


def test_crawler_catchup_report_is_extra_forbid_and_defaults_counters_to_zero() -> None:
    now = _now()
    report = CrawlerCatchupReport(
        session_name="primary",
        channel_id="memes_channel",
        started_at=now,
        finished_at=now,
    )
    assert report.messages_scanned == 0
    assert report.messages_ingested == 0
    assert report.messages_skipped_unsupported == 0
    assert report.messages_skipped_dedup == 0
    assert report.errors == ()

    with pytest.raises(ValidationError):
        _ = CrawlerCatchupReport.model_validate(
            {
                "session_name": "primary",
                "channel_id": "memes_channel",
                "started_at": now,
                "finished_at": now,
                "extra_field": "nope",
            }
        )


async def test_telegram_crawler_runtime_accepts_async_mock_clients(
    migrated_db_session: AsyncSession,
) -> None:
    """The runtime's close + load helpers must work with AsyncMock stand-ins.

    Tests in T02/T03 rely on AsyncMock-based fakes for the telegram client,
    so make sure T01's contract keeps those drop-in replacements usable.
    """

    service = _StubCrawlerIngestService()
    mock_client = cast("PipelineTelegramClientProtocol", AsyncMock())
    runtime = TelegramCrawlerRuntime(
        ingest_service=service,
        telegram_client=mock_client,
        session=migrated_db_session,
        settings=Settings(),
    )

    row = await runtime._load_session_state("mock_session")
    assert row.session_name == "mock_session"
