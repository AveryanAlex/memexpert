# ruff: noqa: TC003
"""Crawler-side SELECT/UPDATE helpers shared by the content-pipeline service.

Extracted from :mod:`content_pipeline` so the S04 crawler ingest surface can
be read without scrolling through the S01-S03 upload/stage/inspect surfaces.
Every function takes the SQLAlchemy session explicitly instead of relying on
``self._session`` because that's how the service now delegates work to pure
helpers — the service itself owns the transaction boundary and simply forwards
its session to these helpers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.content import MemeFile, MemeSource, SourceChannel
from memexpert.services.content_pipeline_helpers import compare_telegram_post_ids
from memexpert.services.errors import CrawlerChannelNotTrackedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.models.enums import SourcePlatform
    from memexpert.schemas.content_pipeline import RawCrawlerPost


async def get_tracked_source_channel(
    session: AsyncSession,
    *,
    platform: SourcePlatform,
    source_id: str,
) -> SourceChannel:
    """Return the ``SourceChannel`` row for ``(platform, source_id)`` or raise.

    Raises :class:`CrawlerChannelNotTrackedError` if the row is missing:
    curated crawlers must only consume from channels an operator added.
    """

    result = await session.execute(
        select(SourceChannel)
        .where(
            SourceChannel.platform == platform,
            SourceChannel.platform_id == source_id,
        )
        .limit(1)
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        raise CrawlerChannelNotTrackedError(
            f"Crawler ingest received a post from untracked channel "
            f"{platform.value}:{source_id}.",
        )
    return channel


async def find_existing_crawler_source_row(
    session: AsyncSession,
    *,
    platform: SourcePlatform,
    source_id: str,
    post_id: str,
) -> MemeSource | None:
    """Return the ``MemeSource`` row for this crawler tuple or ``None`` if absent."""

    result = await session.execute(
        select(MemeSource)
        .where(
            MemeSource.platform == platform,
            MemeSource.source_id == source_id,
            MemeSource.post_id == post_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def meme_file_has_first_source(
    session: AsyncSession,
    meme_file_id: uuid.UUID,
) -> bool:
    """Return ``True`` iff some ``MemeSource`` row already claims first-source."""

    result = await session.execute(
        select(MemeSource.id)
        .where(
            MemeSource.file_id == meme_file_id,
            MemeSource.is_first_source.is_(True),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def advance_source_channel_checkpoint(
    channel: SourceChannel,
    *,
    post_id: str,
    fetched_at: datetime,
) -> None:
    """Advance the channel checkpoint monotonically.

    ``last_fetched_at`` is always refreshed because every successful
    ingest path is a proof of liveness. ``last_read_post_id`` only
    advances to a strictly higher value so out-of-order deliveries
    never regress the crawler's scan position.
    """

    channel.last_fetched_at = fetched_at
    if compare_telegram_post_ids(post_id, channel.last_read_post_id) > 0:
        channel.last_read_post_id = post_id


def attach_crawler_source_row_to_meme_file(
    session: AsyncSession,
    *,
    meme_file: MemeFile,
    raw_post: RawCrawlerPost,
    is_first_source: bool,
) -> MemeSource:
    """Add a new ``MemeSource`` row to an existing meme file on pHash dedup.

    The existing meme + meme_file + pipeline stage journal state is
    intentionally untouched — dedupe reuses the durable state already in
    place. The new row carries the repost's ``published_at``, its
    forward-chain attribution, and its own ``is_first_source`` flag.
    """

    new_source_row = MemeSource(
        file_id=meme_file.id,
        platform=raw_post.platform,
        source_id=raw_post.source_id,
        post_id=raw_post.post_id,
        views=raw_post.views,
        reactions=dict(raw_post.reactions),
        is_first_source=is_first_source,
        source_alive=True,
        published_at=raw_post.published_at,
        forwarded_from_source_id=raw_post.forward.source_id if raw_post.forward is not None else None,
        forwarded_from_post_id=raw_post.forward.post_id if raw_post.forward is not None else None,
    )
    session.add(new_source_row)
    return new_source_row


__all__ = [
    "advance_source_channel_checkpoint",
    "attach_crawler_source_row_to_meme_file",
    "find_existing_crawler_source_row",
    "get_tracked_source_channel",
    "meme_file_has_first_source",
]
