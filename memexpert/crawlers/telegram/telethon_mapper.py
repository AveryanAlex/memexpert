"""Pure-Python mapper that converts Telethon message objects to the typed adapter struct.

Kept in a separate module from :mod:`memexpert.crawlers.telegram.telethon_adapter`
so tests can exercise the mapper without importing Telethon. The mapper uses
duck typing (``Protocol`` classes) so both real Telethon objects and
lightweight dataclass fakes in :mod:`tests.test_telethon_mapper` pass the
same contract without the real SDK being imported.

The mapper is intentionally stateless: every method is a classmethod that
takes the already-known channel identifiers and the raw Telethon message,
and returns a :class:`RawTelegramMessage`. The channel identifiers are
passed in instead of re-derived because the adapter already resolves the
channel once per catch-up sweep and threading it through avoids redundant
``to_id`` lookups per message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from memexpert.crawlers.telegram.client import (
    PipelineTelegramMalformedMessageError,
    RawTelegramMessage,
)
from memexpert.schemas.content_pipeline import CrawlerForwardAttribution

if TYPE_CHECKING:
    from collections.abc import Iterable


@runtime_checkable
class _ChannelEntityLike(Protocol):
    """Minimal duck-typing shape for a Telethon ``Channel`` entity.

    Only the fields the mapper and adapter actually read are required.
    Keeping this narrow lets the mapper accept both real Telethon channel
    objects and in-memory test doubles.
    """

    id: int
    title: str
    username: str | None


@runtime_checkable
class _MessageFwdHeaderLike(Protocol):
    """Minimal duck-typing shape for ``MessageFwdHeader``."""

    from_id: object | None
    from_name: str | None
    channel_post: int | None
    date: datetime | None


@runtime_checkable
class _ReactionItemLike(Protocol):
    """Minimal duck-typing shape for one ``ReactionCount`` entry."""

    reaction: object
    count: int


@runtime_checkable
class _MessageReactionsLike(Protocol):
    """Minimal duck-typing shape for ``MessageReactions``."""

    results: list[_ReactionItemLike]


@runtime_checkable
class _TelethonMessageLike(Protocol):
    """Minimal duck-typing shape for a Telethon ``Message`` object.

    The mapper only reads the attributes listed here. Typing the message
    structurally (rather than importing ``telethon.tl.custom.message.Message``)
    means the pure-Python tests can exercise the mapper with tiny
    dataclasses and zero runtime dependency on Telethon.
    """

    id: int
    date: datetime | None
    photo: object | None
    document: object | None
    views: int | None
    reactions: _MessageReactionsLike | None
    fwd_from: _MessageFwdHeaderLike | None


# Telethon exposes photo + document on the message. Photos are always
# images; documents carry a mime type that tells us whether the document
# is an animated GIF, a video, or something we do not support (audio,
# stickers, files). The mapper never rejects messages outright — the
# caller receives a ``RawTelegramMessage`` with ``media_type="unsupported"``
# and decides whether to count it as a skip.
_GIF_MIME_TYPE = "image/gif"
_MediaType = Literal["photo", "gif", "video", "unsupported"]


def _classify_document_mime(mime_type: str | None) -> _MediaType:
    """Map a document's mime type onto the typed crawler media-type set.

    ``image/gif`` is classified as a GIF (animated), other ``video/*``
    documents are videos, and anything else is unsupported. Classification
    is case-insensitive because Telethon occasionally yields mixed-case
    mime types for forwarded channels.
    """

    if mime_type is None:
        return "unsupported"
    lowered = mime_type.lower()
    if lowered == _GIF_MIME_TYPE:
        return "gif"
    if lowered.startswith("video/"):
        return "video"
    return "unsupported"


def _classify_media(message: _TelethonMessageLike) -> _MediaType:
    """Return the typed media kind the mapper will advertise to the service.

    Photos always map to ``"photo"``. Documents are classified by their
    mime type. Messages with neither a photo nor a document return
    ``"unsupported"`` so the caller can skip them.
    """

    if message.photo is not None:
        return "photo"
    document = message.document
    if document is None:
        return "unsupported"
    mime_type = getattr(document, "mime_type", None)
    if mime_type is not None and not isinstance(mime_type, str):
        mime_type = None
    return _classify_document_mime(mime_type)


def _coerce_aware_datetime(value: datetime | None, *, field_name: str) -> datetime:
    """Return ``value`` promoted to a timezone-aware datetime or raise.

    Telethon message dates are normally tz-aware UTC, but forwarded
    messages occasionally surface as naive datetimes in edge cases. The
    mapper promotes naive values to UTC (they ARE in UTC — Telethon
    documents that internally) and rejects values of the wrong type.
    """

    if value is None:
        raise PipelineTelegramMalformedMessageError(
            f"Telethon message missing required datetime field: {field_name}.",
        )
    if not isinstance(value, datetime):
        raise PipelineTelegramMalformedMessageError(
            f"Telethon message {field_name} has unexpected type: {type(value).__name__}.",
        )
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _extract_reactions(
    reactions: _MessageReactionsLike | None,
) -> dict[str, int]:
    """Return a ``{reaction_key: count}`` dict from a Telethon reactions payload.

    Telethon models emoji reactions and custom-emoji reactions with two
    distinct subclasses (``ReactionEmoji`` and ``ReactionCustomEmoji``).
    The mapper flattens both into a dict keyed by the reaction's
    emoticon (for emoji) or the string form of the custom-emoji document
    id so the service layer's dict-based contract stays stable. Invalid
    entries are skipped instead of raising because reactions are
    informational and should never block ingest.
    """

    if reactions is None:
        return {}
    collected: dict[str, int] = {}
    results = getattr(reactions, "results", None)
    if not isinstance(results, list):
        return collected
    for item in _iter_reaction_items(results):
        reaction_key = _reaction_key(item.reaction)
        if reaction_key is None:
            continue
        collected[reaction_key] = collected.get(reaction_key, 0) + int(item.count)
    return collected


def _iter_reaction_items(
    results: Iterable[object],
) -> Iterable[_ReactionItemLike]:
    for item in results:
        reaction_attr = getattr(item, "reaction", None)
        count_attr = getattr(item, "count", None)
        if reaction_attr is None or not isinstance(count_attr, int):
            continue
        yield item  # type: ignore[misc]


def _reaction_key(reaction: object) -> str | None:
    """Return the stringified key used for one reaction entry.

    Telethon's ``ReactionEmoji`` carries an ``emoticon: str`` field;
    ``ReactionCustomEmoji`` carries a ``document_id: int``. Anything
    else is ignored. The fallback branch covers future reaction kinds
    without raising so the mapper keeps working on unknown variants.
    """

    emoticon = getattr(reaction, "emoticon", None)
    if isinstance(emoticon, str) and emoticon:
        return emoticon
    document_id = getattr(reaction, "document_id", None)
    if isinstance(document_id, int):
        return f"custom:{document_id}"
    return None


def _extract_forward_attribution(
    fwd_from: _MessageFwdHeaderLike | None,
) -> CrawlerForwardAttribution | None:
    """Return forward attribution from a Telethon ``fwd_from`` header, if present.

    Telethon exposes forwards through ``MessageFwdHeader`` with several
    possible shapes:

    * ``from_id`` is a ``PeerChannel`` carrying the original channel id;
    * ``channel_post`` is the numeric id of the original message;
    * ``from_name`` is a free-form name populated for anonymous forwards.

    The mapper preserves everything it can into a
    :class:`CrawlerForwardAttribution`. When neither a channel id nor a
    free-form name is present the forward cannot be attributed and the
    mapper returns ``None`` so the service treats the post as direct.
    """

    if fwd_from is None:
        return None

    channel_id = _forward_channel_id(fwd_from)
    channel_post = getattr(fwd_from, "channel_post", None)
    from_name = getattr(fwd_from, "from_name", None)

    if channel_id is None:
        # Anonymous forward: Telegram intentionally hides the source
        # channel id but may still provide a free-form ``from_name``. We
        # cannot build a first-class attribution without an id, so fall
        # back to a synthetic identifier derived from the name. When the
        # name is also missing the forward is untrackable and we drop it.
        if not isinstance(from_name, str) or not from_name.strip():
            return None
        synthetic_source_id = f"anonymous:{from_name.strip()}"
        synthetic_post_id = str(channel_post) if isinstance(channel_post, int) else "0"
        return CrawlerForwardAttribution(
            source_id=synthetic_source_id,
            post_id=synthetic_post_id,
            channel_username=None,
            channel_title=from_name.strip()[:255],
        )

    if not isinstance(channel_post, int):
        # Telegram occasionally surfaces a forward without a
        # ``channel_post`` when the original was a service message.
        # Fall back to ``0`` so we still capture the source channel id.
        channel_post = 0

    title = from_name if isinstance(from_name, str) and from_name.strip() else None
    return CrawlerForwardAttribution(
        source_id=str(channel_id),
        post_id=str(channel_post),
        channel_username=None,
        channel_title=title,
    )


def _forward_channel_id(fwd_from: _MessageFwdHeaderLike) -> int | None:
    """Return the numeric channel id carried by ``fwd_from.from_id``, if any."""

    from_id = getattr(fwd_from, "from_id", None)
    if from_id is None:
        return None
    channel_id = getattr(from_id, "channel_id", None)
    if isinstance(channel_id, int):
        return channel_id
    return None


class TelethonMessageNormalizer:
    """Converts Telethon ``Message`` objects into :class:`RawTelegramMessage`.

    Kept as a class with a single ``build`` method so the adapter can
    inject a test double if the normalization contract ever needs to be
    swapped out. Real usage just calls ``TelethonMessageNormalizer.build``.
    """

    @staticmethod
    def build(
        *,
        message: _TelethonMessageLike,
        channel_id: str,
        channel_title: str,
        channel_username: str | None,
    ) -> RawTelegramMessage:
        """Return the typed adapter-level projection of one Telethon message.

        Raises :class:`PipelineTelegramMalformedMessageError` if the
        message is missing invariants every Telethon channel message
        must carry (id, date). The raw Telethon object is preserved on
        ``raw_payload`` so the adapter's :meth:`download_media` call can
        hand it back to ``client.download_media`` without re-resolving.
        """

        if not isinstance(message.id, int):
            raise PipelineTelegramMalformedMessageError(
                "Telethon message missing numeric id.",
            )
        published_at = _coerce_aware_datetime(message.date, field_name="date")
        media_type: _MediaType = _classify_media(message)
        views = message.views if isinstance(message.views, int) else 0
        reactions = _extract_reactions(message.reactions)
        forward = _extract_forward_attribution(message.fwd_from)

        return RawTelegramMessage(
            message_id=str(message.id),
            channel_id=channel_id,
            channel_username=channel_username,
            channel_title=channel_title,
            published_at=published_at,
            media_type=media_type,
            views=views,
            reactions=reactions,
            forward=forward,
            raw_payload=message,
        )


__all__ = [
    "TelethonMessageNormalizer",
]
