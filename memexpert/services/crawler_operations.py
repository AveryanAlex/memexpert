# ruff: noqa: TC003
"""Operator-facing crawler service that sits on top of :class:`TelegramCrawlerRuntime`.

Keeps the route layer thin: every ``/api/v1/crawler/*`` route delegates
to exactly one method on :class:`CrawlerOperationsService`. The service
resolves ``source_channel_id`` UUIDs to ``(platform, platform_id)``
pairs, owns the session-distribution invariants (reassigning to an
unknown session surfaces as :class:`CrawlerInvalidSessionError`, not
:class:`CrawlerSessionNotRunnableError`), and projects durable rows
into the typed schemas the operator surface returns.

The service does NOT construct the runtime — the DI layer does — so
tests can swap the real Telethon client for :class:`FakeTelegramClient`
without re-building the entire dependency graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from memexpert.models.content import SourceChannel, TelegramSession
from memexpert.models.enums import SourcePlatform
from memexpert.schemas.content_pipeline import TelegramSessionRead
from memexpert.schemas.crawler import (
    CrawlerChannelRead,
    CrawlerFreshnessSnapshot,
)
from memexpert.services.crawler_freshness import build_crawler_freshness_snapshot
from memexpert.services.errors import (
    CrawlerChannelNotFoundError,
    CrawlerInvalidSessionError,
    CrawlerSessionNotRunnableError,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.crawlers.telegram.runtime import TelegramCrawlerRuntime
    from memexpert.schemas.content_pipeline import CrawlerIngestResult


@dataclass(slots=True)
class CrawlerOperationsService:
    """Thin service layer that backs the operator ``/api/v1/crawler/*`` routes."""

    session: AsyncSession
    runtime: TelegramCrawlerRuntime

    async def list_sessions(self) -> list[TelegramSessionRead]:
        """Return every Telegram session row with its owned channel count.

        The owned-channel count is derived from a dedicated ``SELECT
        telegram_session_id, COUNT(*)`` grouping over Telegram
        ``source_channels``. Sessions with no channels still appear in the
        result with ``owned_channel_count=0`` so operators can see idle
        sessions at a glance.
        """

        session_rows = (
            await self.session.execute(
                select(TelegramSession).order_by(TelegramSession.name),
            )
        ).scalars().all()
        counts_by_session = await self._count_channels_by_session()
        projections: list[TelegramSessionRead] = []
        for row in session_rows:
            base = TelegramSessionRead.model_validate(row)
            projections.append(
                base.model_copy(
                    update={"owned_channel_count": counts_by_session.get(row.id, 0)},
                ),
            )
        return projections

    async def list_channels(
        self,
        *,
        platform: SourcePlatform | None = None,
        session_name: str | None = None,
        include_paused: bool = True,
    ) -> list[CrawlerChannelRead]:
        """Return the tracked source channels filtered by platform + session + paused flag."""

        stmt = select(SourceChannel).options(selectinload(SourceChannel.telegram_session)).order_by(SourceChannel.title)
        if platform is not None:
            stmt = stmt.where(SourceChannel.platform == platform)
        if session_name is not None:
            stmt = stmt.join(SourceChannel.telegram_session).where(TelegramSession.name == session_name)
        if not include_paused:
            stmt = stmt.where(SourceChannel.is_paused.is_(False))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [CrawlerChannelRead.model_validate(row) for row in rows]

    async def pause_channel(self, source_channel_id: uuid.UUID) -> CrawlerChannelRead:
        """Set ``is_paused=True`` for one channel; idempotent on re-pause."""

        channel = await self._get_channel_or_raise(source_channel_id)
        if not channel.is_paused:
            channel.is_paused = True
            await self.session.commit()
        return CrawlerChannelRead.model_validate(await self._get_channel_or_raise(channel.id))

    async def resume_channel(self, source_channel_id: uuid.UUID) -> CrawlerChannelRead:
        """Set ``is_paused=False`` for one channel; idempotent on re-resume."""

        channel = await self._get_channel_or_raise(source_channel_id)
        if channel.is_paused:
            channel.is_paused = False
            await self.session.commit()
        return CrawlerChannelRead.model_validate(await self._get_channel_or_raise(channel.id))

    async def reassign_channel(
        self,
        source_channel_id: uuid.UUID,
        *,
        new_session_name: str,
    ) -> CrawlerChannelRead:
        """Bind one channel to ``new_session_name`` using the runtime's reassign path.

        Surface the runtime's "unknown target session" error as the
        distinct :class:`CrawlerInvalidSessionError` so the operator
        route can map it to a ``409 crawler_invalid_session`` response
        instead of conflating it with the broader session-not-runnable
        taxonomy. The runtime itself still raises the original error for
        programmatic Python callers.
        """

        channel = await self._get_channel_or_raise(source_channel_id)
        try:
            await self.runtime.reassign_channel(
                channel.platform_id,
                new_session_name,
            )
        except CrawlerSessionNotRunnableError as exc:
            # T02's runtime emits CrawlerSessionNotRunnableError for two
            # reasons: the target session does not exist at all, and the
            # channel row is missing. We just resolved the channel row
            # above, so the only remaining cause is "unknown target"
            # and that deserves the distinct operator error code.
            raise CrawlerInvalidSessionError(str(exc)) from exc
        return CrawlerChannelRead.model_validate(await self._get_channel_or_raise(channel.id))

    async def replay_channel_post(
        self,
        source_channel_id: uuid.UUID,
        *,
        post_id: str,
    ) -> CrawlerIngestResult:
        """Resolve the channel and delegate to :meth:`TelegramCrawlerRuntime.replay_post`."""

        channel = await self._get_channel_or_raise(source_channel_id)
        return await self.runtime.replay_post(channel.platform_id, post_id)

    async def get_crawler_freshness_snapshot(
        self,
        *,
        since: datetime | None = None,
        limit_per_channel: int = 100,
    ) -> CrawlerFreshnessSnapshot:
        """Return the freshness snapshot aggregated from durable pipeline truth."""

        bounded_limit = max(1, min(limit_per_channel, 1000))
        settings = self.runtime.settings
        return await build_crawler_freshness_snapshot(
            self.session,
            since=since,
            limit_per_channel=bounded_limit,
            slo_p50_seconds=settings.crawler_freshness_slo_p50_seconds,
            slo_p95_seconds=settings.crawler_freshness_slo_p95_seconds,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_channel_or_raise(self, source_channel_id: uuid.UUID) -> SourceChannel:
        """Load one ``SourceChannel`` row or raise :class:`CrawlerChannelNotFoundError`."""

        row = await self.session.scalar(
            select(SourceChannel)
            .options(selectinload(SourceChannel.telegram_session))
            .where(SourceChannel.id == source_channel_id)
            .execution_options(populate_existing=True)
            .limit(1),
        )
        if row is None:
            raise CrawlerChannelNotFoundError(
                f"Source channel {source_channel_id} does not exist.",
            )
        return row

    async def _count_channels_by_session(self) -> dict[uuid.UUID, int]:
        """Return ``{telegram_session_id: count}`` for every Telegram channel with a session."""

        stmt = (
            select(SourceChannel.telegram_session_id, func.count(SourceChannel.id))
            .where(
                SourceChannel.platform == SourcePlatform.TELEGRAM,
                SourceChannel.telegram_session_id.is_not(None),
            )
            .group_by(SourceChannel.telegram_session_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {telegram_session_id: count for telegram_session_id, count in rows if telegram_session_id is not None}


__all__ = [
    "CrawlerOperationsService",
]
