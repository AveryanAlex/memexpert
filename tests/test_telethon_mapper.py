"""Pure-Python tests for :mod:`memexpert.crawlers.telegram.telethon_mapper`.

These tests deliberately avoid importing ``telethon`` so the mapper keeps
running in environments that only have the fake crawler client installed.
Fake Telethon message shapes are built from tiny dataclasses with only
the attributes the mapper reads — duck typing is sufficient because the
mapper types its inputs via ``Protocol`` classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from memexpert.crawlers.telegram.client import (
    PipelineTelegramMalformedMessageError,
    RawTelegramMessage,
)
from memexpert.crawlers.telegram.telethon_mapper import TelethonMessageNormalizer
from memexpert.schemas.content_pipeline import CrawlerForwardAttribution

if TYPE_CHECKING:
    from memexpert.crawlers.telegram.telethon_mapper import _TelethonMessageLike


def _as_message(fake: Any) -> _TelethonMessageLike:
    """Duck-typed pass-through so mypy stops flagging protocol variance.

    ``_TelethonMessageLike`` is a runtime-checkable Protocol, so at
    runtime the fake dataclasses satisfy it structurally. Mypy's
    invariant attribute typing still complains because ``_FakeMessage``
    uses narrow types, so we cast through ``Any`` at the call site.
    """

    return cast("_TelethonMessageLike", fake)


@dataclass
class _FakePeerChannel:
    channel_id: int


@dataclass
class _FakeMessageFwdHeader:
    from_id: _FakePeerChannel | None = None
    from_name: str | None = None
    channel_post: int | None = None
    date: datetime | None = None


@dataclass
class _FakeReactionEmoji:
    emoticon: str


@dataclass
class _FakeReactionCustomEmoji:
    document_id: int


@dataclass
class _FakeReactionCount:
    reaction: Any
    count: int


@dataclass
class _FakeMessageReactions:
    results: list[_FakeReactionCount] = field(default_factory=list)


@dataclass
class _FakeDocument:
    mime_type: str | None


@dataclass
class _FakeMessage:
    id: int
    date: datetime | None = None
    photo: object | None = None
    document: _FakeDocument | None = None
    views: int | None = None
    reactions: _FakeMessageReactions | None = None
    fwd_from: _FakeMessageFwdHeader | None = None


def _now() -> datetime:
    return datetime(2024, 5, 1, 12, 30, 0, tzinfo=UTC)


def test_normalizer_builds_photo_projection_with_preserved_raw_payload() -> None:
    message = _FakeMessage(
        id=42,
        date=_now(),
        photo=object(),
        views=150,
        reactions=_FakeMessageReactions(
            results=[
                _FakeReactionCount(reaction=_FakeReactionEmoji(emoticon="heart"), count=7),
                _FakeReactionCount(reaction=_FakeReactionEmoji(emoticon="fire"), count=3),
            ],
        ),
    )

    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="memes_channel",
        channel_title="Memes Channel",
        channel_username="memes",
    )

    assert isinstance(result, RawTelegramMessage)
    assert result.message_id == "42"
    assert result.channel_id == "memes_channel"
    assert result.channel_title == "Memes Channel"
    assert result.channel_username == "memes"
    assert result.media_type == "photo"
    assert result.views == 150
    assert result.reactions == {"heart": 7, "fire": 3}
    assert result.forward is None
    # raw_payload MUST be preserved so the adapter can hand it back to
    # ``download_media`` without re-resolving the message.
    assert result.raw_payload is message


def test_normalizer_classifies_document_media_by_mime_type() -> None:
    gif_message = _FakeMessage(
        id=1,
        date=_now(),
        document=_FakeDocument(mime_type="image/gif"),
    )
    video_message = _FakeMessage(
        id=2,
        date=_now(),
        document=_FakeDocument(mime_type="video/mp4"),
    )
    mixed_case_video = _FakeMessage(
        id=3,
        date=_now(),
        document=_FakeDocument(mime_type="VIDEO/QUICKTIME"),
    )
    unsupported_audio = _FakeMessage(
        id=4,
        date=_now(),
        document=_FakeDocument(mime_type="audio/ogg"),
    )
    neither_media = _FakeMessage(
        id=5,
        date=_now(),
    )

    projections = [
        TelethonMessageNormalizer.build(
            message=_as_message(m),
            channel_id="c",
            channel_title="C",
            channel_username=None,
        )
        for m in (gif_message, video_message, mixed_case_video, unsupported_audio, neither_media)
    ]

    assert [p.media_type for p in projections] == ["gif", "video", "video", "unsupported", "unsupported"]


def test_normalizer_extracts_forward_with_channel_id_and_channel_post() -> None:
    forward_date = _now()
    message = _FakeMessage(
        id=10,
        date=_now(),
        photo=object(),
        fwd_from=_FakeMessageFwdHeader(
            from_id=_FakePeerChannel(channel_id=555_666_777),
            channel_post=99,
            date=forward_date,
            from_name="Original Channel",
        ),
    )

    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="reposter",
        channel_title="Reposter",
        channel_username=None,
    )

    assert result.forward == CrawlerForwardAttribution(
        source_id="555666777",
        post_id="99",
        channel_username=None,
        channel_title="Original Channel",
    )


def test_normalizer_handles_anonymous_forward_with_synthetic_id() -> None:
    message = _FakeMessage(
        id=11,
        date=_now(),
        photo=object(),
        fwd_from=_FakeMessageFwdHeader(
            from_id=None,
            channel_post=None,
            from_name="Anonymous User",
        ),
    )

    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="reposter",
        channel_title="Reposter",
        channel_username=None,
    )

    assert result.forward is not None
    assert result.forward.source_id == "anonymous:Anonymous User"
    assert result.forward.post_id == "0"
    assert result.forward.channel_title == "Anonymous User"


def test_normalizer_drops_forward_without_channel_or_name() -> None:
    message = _FakeMessage(
        id=12,
        date=_now(),
        photo=object(),
        fwd_from=_FakeMessageFwdHeader(from_id=None, from_name=None),
    )

    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="reposter",
        channel_title="Reposter",
        channel_username=None,
    )
    assert result.forward is None


def test_normalizer_extracts_custom_emoji_reactions_with_document_id_key() -> None:
    message = _FakeMessage(
        id=13,
        date=_now(),
        photo=object(),
        reactions=_FakeMessageReactions(
            results=[
                _FakeReactionCount(
                    reaction=_FakeReactionCustomEmoji(document_id=42),
                    count=5,
                ),
            ],
        ),
    )

    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="c",
        channel_title="C",
        channel_username=None,
    )
    assert result.reactions == {"custom:42": 5}


def test_normalizer_promotes_naive_date_to_utc() -> None:
    naive_date = datetime(2024, 1, 1, 0, 0, 0)
    message = _FakeMessage(
        id=14,
        date=naive_date,
        photo=object(),
    )
    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="c",
        channel_title="C",
        channel_username=None,
    )
    assert result.published_at == naive_date.replace(tzinfo=UTC)


def test_normalizer_rejects_message_without_date() -> None:
    message = _FakeMessage(id=15, date=None, photo=object())
    with pytest.raises(PipelineTelegramMalformedMessageError):
        _ = TelethonMessageNormalizer.build(
            message=_as_message(message),
            channel_id="c",
            channel_title="C",
            channel_username=None,
        )


def test_normalizer_defaults_views_to_zero_when_missing() -> None:
    message = _FakeMessage(id=16, date=_now(), photo=object(), views=None)
    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="c",
        channel_title="C",
        channel_username=None,
    )
    assert result.views == 0


def test_normalizer_skips_malformed_reactions_without_raising() -> None:
    message = _FakeMessage(
        id=17,
        date=_now(),
        photo=object(),
        reactions=_FakeMessageReactions(
            results=[
                _FakeReactionCount(reaction=object(), count=3),  # unknown shape, skipped
                _FakeReactionCount(
                    reaction=_FakeReactionEmoji(emoticon="thumbs_up"),
                    count=1,
                ),
            ],
        ),
    )
    result = TelethonMessageNormalizer.build(
        message=_as_message(message),
        channel_id="c",
        channel_title="C",
        channel_username=None,
    )
    assert result.reactions == {"thumbs_up": 1}
