# ruff: noqa: TC003
"""Telegram crawler runtime contract locked in T01 for T02/T03/T04 to fill in.

Every orchestration method on :class:`TelegramCrawlerRuntime` raises
``NotImplementedError`` in T01. The typed signatures are the contract: T02
ships the real Telethon adapter + catch-up + live-listener implementation,
T03 ships operator surfaces, and T04 ships the freshness SLO harness. Locking
the method signatures here means those follow-up tasks do not churn the
service or the integration tests every time they land a new implementation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSessionState
from memexpert.models.enums import TelegramSessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.crawlers.telegram.client import PipelineTelegramClientProtocol
    from memexpert.schemas.content_pipeline import CrawlerIngestResult
    from memexpert.services.content_pipeline import ContentPipelineService


_T02_STUB_MESSAGE = "T02 implements this; the contract is locked in T01."


class CrawlerCatchupReport(BaseModel):
    """Bounded summary of one catch-up sweep on one channel.

    T02 populates and returns this model from
    :meth:`TelegramCrawlerRuntime.catch_up_channel`. T04's freshness harness
    consumes it so the crawler contract does not need a second
    proof-harness-specific shape. ``errors`` is a bounded tuple of
    normalized reasons the runtime saw during the sweep so operators can
    see partial-success outcomes without losing the sweep's other counters.
    """

    model_config = ConfigDict(extra="forbid")

    session_name: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=255)
    messages_scanned: StrictInt = Field(default=0, ge=0)
    messages_ingested: StrictInt = Field(default=0, ge=0)
    messages_skipped_unsupported: StrictInt = Field(default=0, ge=0)
    messages_skipped_dedup: StrictInt = Field(default=0, ge=0)
    started_at: datetime
    finished_at: datetime
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class TelegramCrawlerRuntime:
    """Orchestrates catch-up + live listeners on top of the crawler service entrypoint.

    Composed from three injected collaborators so tests can swap the real
    pieces for fakes: the :class:`ContentPipelineService` instance that
    owns durable ingest, the :class:`PipelineTelegramClientProtocol` that
    talks to Telegram, and the application :class:`Settings` that carry
    the crawler rate limit + SLO thresholds. The database session is
    carried separately so the stub method that DOES have real behavior
    (:meth:`_load_session_state`) can read/write the session-state row
    without going through the pipeline service.
    """

    pipeline_service: ContentPipelineService
    telegram_client: PipelineTelegramClientProtocol
    session: AsyncSession
    settings: Settings

    async def catch_up_channel(
        self,
        session_name: str,
        channel_id: str,
    ) -> CrawlerCatchupReport:
        """Consume Telegram history from ``last_read_post_id + 1`` to ``channel.catchup_message_limit``.

        T02 will iterate the adapter's typed messages, ingest each through
        :meth:`ContentPipelineService.create_crawler_ingest`, and return a
        populated :class:`CrawlerCatchupReport`. T01 locks the signature
        only.
        """

        _ = (session_name, channel_id)
        raise NotImplementedError(_T02_STUB_MESSAGE)

    async def start_live_listener(self, session_name: str) -> None:
        """Start the Telethon live listener for all channels owned by this session.

        T02 owns the actual Telethon ``EventHandler`` wiring; T01 locks the
        signature so the operator surfaces T03 adds can reference it.
        """

        _ = session_name
        raise NotImplementedError(_T02_STUB_MESSAGE)

    async def stop_live_listener(self, session_name: str) -> None:
        """Signal the live listener to drain cleanly and mark the session stopped.

        T02 owns the cancellation plumbing; T01 locks the signature.
        """

        _ = session_name
        raise NotImplementedError(_T02_STUB_MESSAGE)

    async def reassign_channel(
        self,
        channel_id: str,
        new_session_name: str,
    ) -> None:
        """Move one channel from its current session to a different session.

        T03 owns the operator surface that calls this method; T01 locks
        the signature so T02's live listener knows when it must release
        channels mid-flight.
        """

        _ = (channel_id, new_session_name)
        raise NotImplementedError(_T02_STUB_MESSAGE)

    async def replay_post(
        self,
        channel_id: str,
        post_id: str,
    ) -> CrawlerIngestResult:
        """Re-ingest one Telegram message by (channel_id, post_id) through the service.

        T03 exposes this as an operator route so failed crawler ingests
        can be retried by id; T01 locks the typed signature so the route
        handler in T03 can rely on it without touching service code.
        """

        _ = (channel_id, post_id)
        raise NotImplementedError(_T02_STUB_MESSAGE)

    async def _load_session_state(self, session_name: str) -> TelegramSessionState:
        """Return the durable session-state row for ``session_name``, creating it if missing.

        This is the ONE method on the runtime that has real behavior in
        T01: T02's real Telethon adapter needs a trusted way to read and
        stamp session health, and locking the row-creation contract here
        means the session state table can be populated safely from both
        T02's catch-up path and T03's operator surface. A fresh row is
        created in the ``STOPPED`` status because the runtime has not yet
        proved the session is connected.
        """

        result = await self.session.execute(
            select(TelegramSessionState)
            .where(TelegramSessionState.session_name == session_name)
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        row = TelegramSessionState(
            id=uuid.uuid7(),
            session_name=session_name,
            status=TelegramSessionStatus.STOPPED,
            last_heartbeat_at=utcnow(),
        )
        self.session.add(row)
        await self.session.flush()
        return row


__all__ = [
    "CrawlerCatchupReport",
    "TelegramCrawlerRuntime",
]
