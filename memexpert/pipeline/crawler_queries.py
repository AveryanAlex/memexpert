# ruff: noqa: TC003
"""Crawler-side SELECT/UPDATE helpers shared by pipeline ingest code."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.content import MemeFile, MemeSource, SourceChannel
from memexpert.pipeline.helpers import compare_telegram_post_ids
from memexpert.services.errors import CrawlerChannelNotTrackedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.models.enums import SourceAttachReason, SourcePlatform
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


async def meme_has_first_source(
    session: AsyncSession,
    meme_id: uuid.UUID,
) -> bool:
    """Return ``True`` iff any source row on a meme already claims first-source."""

    result = await session.execute(
        select(MemeSource.id)
        .join(MemeFile, MemeFile.id == MemeSource.file_id)
        .where(
            MemeFile.meme_id == meme_id,
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
    attach_reason: SourceAttachReason,
    matched_meme_file_id: uuid.UUID | None = None,
) -> MemeSource:
    """Add a new ``MemeSource`` row to an existing file on SHA256 match.

    SHA256 identity means the crawler saw the same physical file, so no new
    ``MemeFile`` or pipeline work is created. The new row carries the repost's
    ``published_at``, its forward-chain attribution, and its own
    ``is_first_source`` flag.

    This helper is not currently used by crawler ingest. If it is reintroduced,
    callers must also add the corresponding initial engagement snapshot in the
    same transaction.
    """

    new_source_row = MemeSource(
        file_id=meme_file.id,
        platform=raw_post.platform,
        source_id=raw_post.source_id,
        post_id=raw_post.post_id,
        is_first_source=is_first_source,
        source_alive=True,
        published_at=raw_post.published_at,
        forwarded_from_source_id=raw_post.forward.source_id if raw_post.forward is not None else None,
        forwarded_from_post_id=raw_post.forward.post_id if raw_post.forward is not None else None,
        attach_reason=attach_reason,
        matched_meme_file_id=matched_meme_file_id,
    )
    session.add(new_source_row)
    return new_source_row


__all__ = [
    "advance_source_channel_checkpoint",
    "attach_crawler_source_row_to_meme_file",
    "find_existing_crawler_source_row",
    "get_tracked_source_channel",
    "meme_has_first_source",
]
