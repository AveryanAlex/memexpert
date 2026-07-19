# ruff: noqa: TC003
"""Single-session Telegram crawler runtime that drives catch-up + live listeners.

T02 fills in the orchestration pieces T01 locked as signature-only stubs.
The runtime composes a crawler ingest service (which owns Telegram-specific
guards before delegating bytes to raw ingest), a :class:`PipelineTelegramClientProtocol`
(real Telethon client or :class:`FakeTelegramClient`), a SQLAlchemy
session (used for :class:`TelegramSession` + :class:`SourceChannel`
mutations that do not belong to the ingest entrypoint), and the
application settings. The manager layers multi-session distribution and
freshness inspection on top of this per-session contract.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from memexpert.crawlers.telegram.client import (
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramMessageMapper,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    RawTelegramMessage,
    TelegramMessageEditedEvent,
    TelegramMessagesDeletedEvent,
    TelegramNewMessageEvent,
)
from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, SourceChannelPost, TelegramSession
from memexpert.models.enums import SourceChannelPostStatus, SourcePlatform, TelegramSessionStatus
from memexpert.pipeline.helpers import compare_telegram_post_ids
from memexpert.schemas.content_pipeline import CrawlerIngestOutcome
from memexpert.services.errors import CrawlerSessionNotRunnableError, PipelinePayloadTooLargeError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.crawlers.telegram.client import PipelineTelegramClientProtocol
    from memexpert.schemas.content_pipeline import CrawlerIngestResult, RawCrawlerPost


logger = logging.getLogger(__name__)

_LIVE_LISTENER_START_TIMEOUT_SECONDS = 30.0

# Bounded tuple size for the catch-up report's ``errors`` field. We do not
# want a pathological channel to produce an unbounded error list that
# operators cannot render in the inspect surface; keeping the first few
# errors gives enough diagnostic context while capping memory use.
_MAX_CATCHUP_ERRORS = 16


class CrawlerIngestServiceProtocol(Protocol):
    async def try_accept_without_media(
        self,
        *,
        platform: SourcePlatform,
        source_id: str,
        post_id: str,
        published_at: datetime | None,
        advance_checkpoint: bool = True,
    ) -> CrawlerIngestResult | None: ...

    async def accept_crawler_post(
        self,
        raw_post: RawCrawlerPost,
        *,
        advance_checkpoint: bool = True,
    ) -> CrawlerIngestResult: ...


class CrawlerCatchupReport(BaseModel):
    """Bounded summary of one catch-up sweep on one channel.

    The freshness surface consumes this model so the crawler contract does not
    need a second diagnostics-specific shape. ``errors`` is a bounded tuple of
    normalized reasons the runtime saw during the
    sweep so operators can see partial-success outcomes without losing
    the sweep's other counters.
    """

    model_config = ConfigDict(extra="forbid")

    session_name: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=255)
    messages_scanned: StrictInt = Field(default=0, ge=0)
    messages_ingested: StrictInt = Field(default=0, ge=0)
    messages_skipped_unsupported: StrictInt = Field(default=0, ge=0)
    messages_skipped_dedup: StrictInt = Field(default=0, ge=0)
    messages_quarantined: StrictInt = Field(default=0, ge=0)
    retryable_failure_post_id: str | None = None
    started_at: datetime
    finished_at: datetime
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class _CatchupCounters:
    """Internal accumulators the catch-up loop updates while iterating messages."""

    scanned: int = 0
    ingested: int = 0
    skipped_unsupported: int = 0
    skipped_dedup: int = 0
    quarantined: int = 0
    retryable_failure_post_id: str | None = None
    errors: list[str] = field(default_factory=list)

    def record_error(self, message: str) -> None:
        """Append an error string while staying under the bounded cap."""

        if len(self.errors) >= _MAX_CATCHUP_ERRORS:
            return
        self.errors.append(message)


@dataclass(slots=True)
class TelegramCrawlerRuntime:
    """Orchestrates catch-up + live listeners on top of the crawler service entrypoint.

    Composed from four injected collaborators so tests can swap the real
    pieces for fakes: a crawler ingest service, a
    :class:`PipelineTelegramClientProtocol`, a SQLAlchemy session, and
    the application :class:`Settings`. The runtime's background live
    listener task handles are tracked on the instance so
    :meth:`stop_live_listener` can cancel them cleanly.
    """

    ingest_service: CrawlerIngestServiceProtocol
    telegram_client: PipelineTelegramClientProtocol
    session: AsyncSession
    settings: Settings
    _live_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    # ------------------------------------------------------------------
    # Public orchestration surface
    # ------------------------------------------------------------------

    async def catch_up_channel(
        self,
        session_name: str,
        channel_id: str,
    ) -> CrawlerCatchupReport:
        """Sweep a tracked channel's history from the current checkpoint forward.

        Returns a populated :class:`CrawlerCatchupReport` describing what
        happened. Flood-wait and provider-unavailable errors produce a
        partial report with the session parked in the appropriate durable
        state; a banned session is quarantined and the error re-raises
        so the operator surface sees the hard failure. Unsupported media
        is counted as a skip, not an error.
        """

        started_at = utcnow()
        telegram_session = await self._require_runnable_session(session_name)
        channel_row = await self._get_owned_channel_or_raise(channel_id, telegram_session)

        if (
            not channel_row.is_active
            or channel_row.is_paused
            or not channel_row.catchup_enabled
            or not telegram_session.catchup_enabled
        ):
            reason = (
                "inactive"
                if not channel_row.is_active
                else "paused"
                if channel_row.is_paused
                else "catchup_disabled"
                if not channel_row.catchup_enabled
                else "session_catchup_disabled"
            )
            return CrawlerCatchupReport(
                session_name=session_name,
                channel_id=channel_id,
                started_at=started_at,
                finished_at=utcnow(),
                errors=(f"catch_up skipped: {reason}",),
            )

        limit = min(
            channel_row.catchup_message_limit,
            self.settings.crawler_default_catchup_message_limit,
        )
        min_message_id = self._parse_checkpoint_post_id(channel_row.last_read_post_id)
        initial_latest_window = not channel_row.initial_catchup_completed

        counters = _CatchupCounters()
        try:
            await self._refresh_channel_metadata(channel_row)
            if initial_latest_window:
                initial_stream_completed = await self._drive_catchup_loop(
                    counters=counters,
                    channel_row=channel_row,
                    stream=self.telegram_client.iter_latest_channel_messages(
                        channel_id=channel_id,
                        limit=limit,
                    ),
                    telegram_session=telegram_session,
                    advance_checkpoint=True,
                    advance_history_cursor=True,
                )
                initial_messages_scanned = counters.scanned
                if initial_stream_completed and initial_messages_scanned < limit:
                    channel_row.history_exhausted = True
                if initial_stream_completed:
                    channel_row.initial_catchup_completed = True
                    await self._drain_forward_pages(
                        counters=counters,
                        channel_row=channel_row,
                        channel_id=channel_id,
                        limit=limit,
                        telegram_session=telegram_session,
                    )
            else:
                await self._drain_forward_pages(
                    counters=counters,
                    channel_row=channel_row,
                    channel_id=channel_id,
                    limit=limit,
                    telegram_session=telegram_session,
                    initial_min_message_id=min_message_id,
                )
        except PipelineTelegramFloodWaitError as exc:
            counters.record_error(f"flood_wait:{exc.wait_seconds}s")
            self._park_session_on_flood_wait(
                telegram_session,
                wait_seconds=exc.wait_seconds,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
        except PipelineTelegramSessionBannedError as exc:
            await self._quarantine_session(
                telegram_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            raise
        except PipelineTelegramSessionAuthRequiredError as exc:
            await self._mark_session_auth_required(
                telegram_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            raise

        await self._commit_runtime_state()
        return CrawlerCatchupReport(
            session_name=session_name,
            channel_id=channel_id,
            messages_scanned=counters.scanned,
            messages_ingested=counters.ingested,
            messages_skipped_unsupported=counters.skipped_unsupported,
            messages_skipped_dedup=counters.skipped_dedup,
            messages_quarantined=counters.quarantined,
            retryable_failure_post_id=counters.retryable_failure_post_id,
            started_at=started_at,
            finished_at=utcnow(),
            errors=tuple(counters.errors),
        )

    async def catch_up_older_channel(
        self,
        session_name: str,
        channel_id: str,
        *,
        before_post_id: str | None,
        limit: int,
    ) -> CrawlerCatchupReport:
        """Fetch one older history page without changing the forward checkpoint."""

        started_at = utcnow()
        telegram_session = await self._require_runnable_session(session_name)
        channel_row = await self._get_owned_channel_or_raise(channel_id, telegram_session)
        if (
            not channel_row.is_active
            or channel_row.is_paused
            or not channel_row.catchup_enabled
            or not telegram_session.catchup_enabled
        ):
            raise CrawlerSessionNotRunnableError(
                f"Cannot backfill Telegram channel {channel_id!r} while its catch-up workload is disabled.",
            )
        if not channel_row.initial_catchup_completed:
            raise CrawlerSessionNotRunnableError(
                f"Cannot backfill Telegram channel {channel_id!r} before its initial catch-up completes.",
            )

        boundary = (
            before_post_id
            or channel_row.history_cursor_post_id
            or channel_row.oldest_observed_post_id
            or channel_row.last_read_post_id
        )
        parsed_boundary = self._parse_checkpoint_post_id(boundary)
        if parsed_boundary is None:
            raise CrawlerSessionNotRunnableError(
                f"Cannot backfill Telegram channel {channel_id!r} before its initial catch-up establishes a cursor.",
            )

        counters = _CatchupCounters()
        try:
            stream_completed = await self._drive_catchup_loop(
                counters=counters,
                channel_row=channel_row,
                stream=self.telegram_client.iter_older_channel_messages(
                    channel_id=channel_id,
                    before_message_id=parsed_boundary,
                    limit=limit,
                ),
                telegram_session=telegram_session,
                advance_checkpoint=False,
                advance_history_cursor=True,
            )
            if stream_completed and counters.scanned < limit:
                channel_row.history_exhausted = True
        except PipelineTelegramFloodWaitError as exc:
            counters.record_error(f"flood_wait:{exc.wait_seconds}s")
            self._park_session_on_flood_wait(
                telegram_session,
                wait_seconds=exc.wait_seconds,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
        except PipelineTelegramSessionBannedError as exc:
            await self._quarantine_session(
                telegram_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            raise
        except PipelineTelegramSessionAuthRequiredError as exc:
            await self._mark_session_auth_required(
                telegram_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            raise

        await self._commit_runtime_state()
        return CrawlerCatchupReport(
            session_name=session_name,
            channel_id=channel_id,
            messages_scanned=counters.scanned,
            messages_ingested=counters.ingested,
            messages_skipped_unsupported=counters.skipped_unsupported,
            messages_skipped_dedup=counters.skipped_dedup,
            messages_quarantined=counters.quarantined,
            retryable_failure_post_id=counters.retryable_failure_post_id,
            started_at=started_at,
            finished_at=utcnow(),
            errors=tuple(counters.errors),
        )

    async def start_live_listener(self, session_name: str) -> None:
        """Start a background task that ingests live messages for this session.

        The method returns after the background task is started so the
        caller can continue orchestration. The task handle is tracked on
        the runtime so :meth:`stop_live_listener` can cancel it cleanly.
        Calling ``start_live_listener`` on a session that already has a
        running task is a no-op — operators get idempotent restart
        semantics without double-registering handlers.
        """

        existing_task = self._live_tasks.get(session_name)
        if existing_task is not None and not existing_task.done():
            return

        telegram_session = await self._require_runnable_session(session_name)
        if not telegram_session.live_enabled:
            logger.info(
                "Telegram session %s has live listening disabled; skipping listener start.",
                session_name,
            )
            return
        channel_ids = await self._channel_ids_for_session(telegram_session)
        if not channel_ids:
            logger.info(
                "No active channels bound to session %s; skipping live listener start.",
                session_name,
            )
            return

        telegram_session.live_listener_started_at = utcnow()
        telegram_session.last_heartbeat_at = utcnow()
        await self._commit_runtime_state()

        ready_event = asyncio.Event()
        listener_task = asyncio.create_task(
            self._run_live_listener(
                session_name=session_name,
                channel_ids=channel_ids,
                ready_event=ready_event,
            ),
            name=f"crawler-live-{session_name}",
        )
        self._live_tasks[session_name] = listener_task
        ready_task = asyncio.create_task(ready_event.wait())
        try:
            done, _ = await asyncio.wait(
                (listener_task, ready_task),
                timeout=_LIVE_LISTENER_START_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_task in done:
                await ready_task
                return
            if listener_task in done:
                await listener_task
                raise PipelineTelegramProviderUnavailableError(
                    f"Live listener for Telegram session {session_name!r} exited before registration completed.",
                )
            listener_task.cancel()
            _ = await asyncio.gather(listener_task, return_exceptions=True)
            raise PipelineTelegramProviderUnavailableError(
                f"Live listener for Telegram session {session_name!r} did not register within "
                f"{_LIVE_LISTENER_START_TIMEOUT_SECONDS:g}s.",
            )
        except Exception:
            self._live_tasks.pop(session_name, None)
            raise
        finally:
            if not ready_task.done():
                ready_task.cancel()
            _ = await asyncio.gather(ready_task, return_exceptions=True)

    async def stop_live_listener(self, session_name: str, *, mark_stopped: bool = True) -> None:
        """Cancel the background live listener for ``session_name`` if running."""

        task = self._live_tasks.pop(session_name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(asyncio.wait_for(task, timeout=5.0))
            except TimeoutError, asyncio.CancelledError:
                # Cancellation is expected; the task finishing late is
                # not an error the caller needs to see. Log at debug
                # level so operators can still find it in the worker logs.
                logger.debug("Live listener task for %s did not stop in time.", session_name)
            finally:
                await self.session.rollback()

        # Always update the session row whether or not a task was running
        # so the operator surface sees a consistent stopped-listener state.
        telegram_session = await self._load_session(session_name)
        if mark_stopped:
            telegram_session.status = TelegramSessionStatus.STOPPED
        telegram_session.live_listener_started_at = None
        await self._commit_runtime_state()

    async def reassign_channel(
        self,
        channel_id: str,
        new_session_name: str,
    ) -> None:
        """Bind ``channel_id`` to ``new_session_name`` in durable state.

        Validates the target session exists before mutating the channel
        so stray ids surface as a loud error instead of orphaning the
        channel. The live listener does not rebind on the fly; the manager's
        next automatic configuration reconciliation or process restart picks
        up the new assignment.
        """

        target_session = await self._load_session(new_session_name)
        channel_row = await self._get_tracked_channel(channel_id)
        channel_row.telegram_session_id = target_session.id
        await self._commit_runtime_state()

    async def replay_post(
        self,
        channel_id: str,
        post_id: str,
    ) -> CrawlerIngestResult:
        """Re-ingest one Telegram message by id without advancing the checkpoint.

        Replay goes through the same raw crawler accept entrypoint as the
        catch-up path, but the runtime never touches the
        :class:`SourceChannel` checkpoint state: out-of-band replays must
        not regress (or advance) ``last_read_post_id``. Ingest errors
        propagate so the operator route can surface them as typed
        failures.
        """

        channel = await self._get_tracked_channel(channel_id)
        if not channel.is_active or channel.is_paused:
            reason = "inactive" if not channel.is_active else "paused"
            raise CrawlerSessionNotRunnableError(
                f"Cannot replay Telegram channel {channel_id!r} because it is {reason}.",
            )
        if channel.telegram_session_id is None:
            raise CrawlerSessionNotRunnableError(
                f"Cannot replay Telegram channel {channel_id!r} because it is not assigned to a session.",
            )
        assigned_session = await self.session.get(TelegramSession, channel.telegram_session_id)
        if assigned_session is None:
            raise CrawlerSessionNotRunnableError(
                f"Cannot replay Telegram channel {channel_id!r} because its session is missing.",
            )
        if not assigned_session.enabled or assigned_session.status is not TelegramSessionStatus.ACTIVE:
            raise CrawlerSessionNotRunnableError(
                f"Cannot replay Telegram channel {channel_id!r} because its session is not runnable.",
            )

        try:
            raw_message = await self.telegram_client.fetch_single_message(
                channel_id=channel_id,
                post_id=post_id,
            )
            if raw_message.channel_id != channel_id:
                raise PipelineTelegramMalformedMessageError(
                    "Telegram replay returned message for channel "
                    f"{raw_message.channel_id!r}, expected {channel_id!r}.",
                )
            post_row = await self._observe_post(
                channel,
                raw_message,
                advance_history_cursor=False,
            )
            await self._commit_runtime_state()
            if raw_message.media_type == "unsupported":
                self._mark_post_unsupported(post_row)
                await self._commit_runtime_state()
                raise PipelineTelegramMalformedMessageError(
                    f"Telegram post {channel_id}:{post_id} has no supported media.",
                )
            predownload_result = await self._try_accept_without_media(raw_message, advance_checkpoint=False)
            if predownload_result is not None:
                self._mark_post_for_ingest_outcome(post_row, predownload_result)
                await self._commit_runtime_state()
                return predownload_result

            try:
                media_bytes = await self.telegram_client.download_media(raw_message)
                raw_post = PipelineTelegramMessageMapper.build_raw_crawler_post(
                    raw_message,
                    media_bytes,
                )
                result = await self.ingest_service.accept_crawler_post(raw_post, advance_checkpoint=False)
            except PipelineTelegramError as exc:
                self._mark_post_failed(
                    post_row,
                    error_code=type(exc).__name__,
                    error_text=str(exc),
                    retryable=True,
                )
                await self._commit_runtime_state()
                raise
            self._mark_post_for_ingest_outcome(post_row, result)
            await self._commit_runtime_state()
            return result
        except PipelineTelegramFloodWaitError as exc:
            self._park_session_on_flood_wait(
                assigned_session,
                wait_seconds=exc.wait_seconds,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            await self._commit_runtime_state()
            raise
        except PipelineTelegramSessionBannedError as exc:
            await self._quarantine_session(
                assigned_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            raise
        except PipelineTelegramSessionAuthRequiredError as exc:
            await self._mark_session_auth_required(
                assigned_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _drain_forward_pages(
        self,
        *,
        counters: _CatchupCounters,
        channel_row: SourceChannel,
        channel_id: str,
        limit: int,
        telegram_session: TelegramSession,
        initial_min_message_id: int | None = None,
    ) -> None:
        """Drain forward pages until the checkpoint reaches a short final page."""

        min_message_id = (
            initial_min_message_id
            if initial_min_message_id is not None
            else self._parse_checkpoint_post_id(channel_row.last_read_post_id)
        )
        while True:
            checkpoint_before = channel_row.last_read_post_id
            scanned_before = counters.scanned
            stream_completed = await self._drive_catchup_loop(
                counters=counters,
                channel_row=channel_row,
                stream=self.telegram_client.iter_channel_messages(
                    channel_id=channel_id,
                    min_message_id=min_message_id,
                    limit=limit,
                ),
                telegram_session=telegram_session,
                advance_checkpoint=True,
                advance_history_cursor=False,
            )
            page_messages_scanned = counters.scanned - scanned_before
            if (
                not stream_completed
                or page_messages_scanned < limit
                or channel_row.last_read_post_id == checkpoint_before
            ):
                return
            min_message_id = self._parse_checkpoint_post_id(channel_row.last_read_post_id)

    async def _drive_catchup_loop(
        self,
        *,
        counters: _CatchupCounters,
        channel_row: SourceChannel,
        stream: AsyncIterator[RawTelegramMessage],
        telegram_session: TelegramSession,
        advance_checkpoint: bool,
        advance_history_cursor: bool,
    ) -> bool:
        """Iterate the adapter stream and ingest each message through the service."""

        try:
            async for raw_message in stream:
                counters.scanned += 1
                post_row = await self._observe_post(
                    channel_row,
                    raw_message,
                    advance_history_cursor=False,
                )
                # SourceChannelPost is authoritative for message context, so
                # commit it before deduplication, download, or ingest work can
                # fail or roll the shared session back.
                await self._commit_runtime_state()
                if raw_message.media_type == "unsupported":
                    counters.skipped_unsupported += 1
                    self._mark_post_unsupported(post_row)
                    if advance_checkpoint:
                        self._advance_forward_checkpoint(channel_row, raw_message.message_id)
                    if advance_history_cursor:
                        self._advance_history_cursor(channel_row, raw_message.message_id)
                    await self._commit_runtime_state()
                    continue
                await self._ingest_single_message(
                    counters=counters,
                    channel_row=channel_row,
                    raw_message=raw_message,
                    post_row=post_row,
                    advance_checkpoint=advance_checkpoint,
                )
                if advance_history_cursor:
                    if post_row.status is SourceChannelPostStatus.FAILED:
                        if post_row.is_retryable and post_row.attempt_count < 3:
                            counters.retryable_failure_post_id = raw_message.message_id
                            await self._commit_runtime_state()
                            return False
                        post_row.quarantined_at = utcnow()
                        counters.quarantined += 1
                        if advance_checkpoint:
                            self._advance_forward_checkpoint(channel_row, raw_message.message_id)
                    self._advance_history_cursor(channel_row, raw_message.message_id)
                await self._commit_runtime_state()
        except PipelineTelegramFloodWaitError as exc:
            counters.record_error(f"flood_wait:{exc.wait_seconds}s")
            self._park_session_on_flood_wait(
                telegram_session,
                wait_seconds=exc.wait_seconds,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            return False
        except PipelineTelegramProviderUnavailableError as exc:
            # Provider errors inside the iteration loop (e.g. a mid-stream
            # disconnect) are transient and the session can retry on the
            # next sweep; the session state stays ``active`` so the next
            # operator-triggered catch-up call runs cleanly.
            counters.record_error(f"provider_unavailable:{exc}")
            return False
        else:
            # A successful empty poll is still operational progress. Without
            # this assignment newly activated but quiet channels remain
            # indistinguishable from channels the crawler never contacted.
            channel_row.last_fetched_at = utcnow()
            return True

    async def _ingest_single_message(
        self,
        *,
        counters: _CatchupCounters,
        channel_row: SourceChannel,
        raw_message: RawTelegramMessage,
        post_row: SourceChannelPost,
        advance_checkpoint: bool,
    ) -> None:
        """Download + ingest one typed message and tally its outcome.

        Per-message provider errors are non-fatal: the loop continues so
        one bad message does not sink the entire catch-up sweep. Malformed
        mapper errors are logged and counted as unsupported because
        re-attempting will keep failing the same way.
        """

        predownload_result = await self._try_accept_without_media(
            raw_message,
            advance_checkpoint=advance_checkpoint,
        )
        if predownload_result is not None:
            self._tally_ingest_outcome(counters, predownload_result)
            self._mark_post_for_ingest_outcome(post_row, predownload_result)
            if advance_checkpoint:
                self._advance_forward_checkpoint(channel_row, raw_message.message_id)
            return

        try:
            media_bytes = await self.telegram_client.download_media(raw_message)
        except PipelineTelegramFloodWaitError as exc:
            self._mark_post_failed(
                post_row,
                error_code="flood_wait",
                error_text=str(exc),
                retryable=True,
            )
            raise
        except PipelineTelegramProviderUnavailableError as exc:
            counters.record_error(
                f"download_unavailable:{raw_message.message_id}:{exc}",
            )
            self._mark_post_failed(
                post_row,
                error_code="download_unavailable",
                error_text=str(exc),
                retryable=True,
            )
            return
        except PipelineTelegramMalformedMessageError as exc:
            counters.record_error(
                f"download_malformed:{raw_message.message_id}:{exc}",
            )
            counters.skipped_unsupported += 1
            self._mark_post_failed(
                post_row,
                error_code="download_malformed",
                error_text=str(exc),
                retryable=False,
            )
            if advance_checkpoint:
                self._advance_forward_checkpoint(channel_row, raw_message.message_id)
            return

        try:
            raw_post = PipelineTelegramMessageMapper.build_raw_crawler_post(
                raw_message,
                media_bytes,
            )
        except PipelineTelegramMalformedMessageError as exc:
            counters.record_error(
                f"mapper_malformed:{raw_message.message_id}:{exc}",
            )
            counters.skipped_unsupported += 1
            self._mark_post_failed(
                post_row,
                error_code="mapper_malformed",
                error_text=str(exc),
                retryable=False,
            )
            if advance_checkpoint:
                self._advance_forward_checkpoint(channel_row, raw_message.message_id)
            return

        try:
            ingest_result = await self.ingest_service.accept_crawler_post(
                raw_post,
                advance_checkpoint=advance_checkpoint,
            )
        except PipelinePayloadTooLargeError as exc:
            counters.skipped_unsupported += 1
            self._mark_post_unsupported(
                post_row,
                error_code=exc.error_code,
                error_text=str(exc),
            )
            if advance_checkpoint:
                self._advance_forward_checkpoint(channel_row, raw_message.message_id)
            return
        except Exception as exc:
            self._mark_post_failed(
                post_row,
                error_code=type(exc).__name__,
                error_text=str(exc),
                retryable=True,
            )
            raise
        self._tally_ingest_outcome(counters, ingest_result)
        self._mark_post_for_ingest_outcome(post_row, ingest_result)
        if advance_checkpoint:
            self._advance_forward_checkpoint(channel_row, raw_message.message_id)

    async def _try_accept_without_media(
        self,
        raw_message: RawTelegramMessage,
        *,
        advance_checkpoint: bool = True,
    ) -> CrawlerIngestResult | None:
        """Return a terminal ingest result that can be decided before media download."""

        return await self.ingest_service.try_accept_without_media(
            platform=SourcePlatform.TELEGRAM,
            source_id=raw_message.channel_id,
            post_id=raw_message.message_id,
            published_at=raw_message.published_at,
            advance_checkpoint=advance_checkpoint,
        )

    async def _observe_post(
        self,
        channel_row: SourceChannel,
        raw_message: RawTelegramMessage,
        *,
        advance_history_cursor: bool,
    ) -> SourceChannelPost:
        """Upsert one observed post and advance only the appropriate cursors."""

        # Admin inventory pagination uses ``created_at`` as its stable
        # observation cutoff, so capture this only after the adapter yields
        # the individual message rather than once for the enclosing page.
        fetched_at = utcnow()
        metadata = raw_message.telegram_post
        text_entities = metadata.entity_json()
        insert_statement = pg_insert(SourceChannelPost).values(
            id=uuid.uuid7(),
            source_channel_id=channel_row.id,
            post_id=raw_message.message_id,
            published_at=raw_message.published_at,
            media_type=raw_message.media_type,
            status=SourceChannelPostStatus.OBSERVED,
            last_error_code=None,
            last_error_text=None,
            attempt_count=1,
            is_retryable=False,
            last_attempt_at=fetched_at,
            first_observed_text=metadata.text,
            latest_text=metadata.text,
            first_observed_text_entities=text_entities,
            latest_text_entities=text_entities,
            media_group_id=metadata.media_group_id,
            reply_to_post_id=metadata.reply_to_post_id,
            telegram_edited_at=metadata.edited_at,
            metadata_first_observed_at=fetched_at,
            metadata_last_observed_at=fetched_at,
            metadata_version=metadata.schema_version,
            is_deleted=False,
            deletion_observed_at=None,
            created_at=fetched_at,
            updated_at=fetched_at,
        )
        excluded = insert_statement.excluded
        statement = (
            insert_statement
            .on_conflict_do_update(
                constraint="uq_source_channel_posts_channel_post",
                set_={
                    "published_at": raw_message.published_at,
                    "media_type": raw_message.media_type,
                    "status": SourceChannelPostStatus.OBSERVED,
                    "last_error_code": None,
                    "last_error_text": None,
                    "attempt_count": SourceChannelPost.attempt_count + 1,
                    "is_retryable": False,
                    "next_attempt_at": None,
                    "last_attempt_at": fetched_at,
                    "quarantined_at": None,
                    "first_observed_text": case(
                        (SourceChannelPost.metadata_version < metadata.schema_version, excluded.first_observed_text),
                        else_=SourceChannelPost.first_observed_text,
                    ),
                    "latest_text": excluded.latest_text,
                    "first_observed_text_entities": case(
                        (
                            SourceChannelPost.metadata_version < metadata.schema_version,
                            excluded.first_observed_text_entities,
                        ),
                        else_=SourceChannelPost.first_observed_text_entities,
                    ),
                    "latest_text_entities": excluded.latest_text_entities,
                    "media_group_id": excluded.media_group_id,
                    "reply_to_post_id": excluded.reply_to_post_id,
                    "telegram_edited_at": excluded.telegram_edited_at,
                    "metadata_first_observed_at": case(
                        (
                            SourceChannelPost.metadata_version < metadata.schema_version,
                            excluded.metadata_first_observed_at,
                        ),
                        else_=SourceChannelPost.metadata_first_observed_at,
                    ),
                    "metadata_last_observed_at": fetched_at,
                    "metadata_version": metadata.schema_version,
                    "is_deleted": False,
                    "deletion_observed_at": None,
                    "updated_at": fetched_at,
                },
            )
            .returning(SourceChannelPost)
        )
        post_row = (
            await self.session.scalars(
                statement.execution_options(populate_existing=True),
            )
        ).one()
        channel_row.last_fetched_at = fetched_at
        if (
            channel_row.oldest_observed_post_id is None
            or compare_telegram_post_ids(
                raw_message.message_id,
                channel_row.oldest_observed_post_id,
            )
            < 0
        ):
            channel_row.oldest_observed_post_id = raw_message.message_id
        if advance_history_cursor and (
            channel_row.history_cursor_post_id is None
            or compare_telegram_post_ids(
                raw_message.message_id,
                channel_row.history_cursor_post_id,
            )
            < 0
        ):
            channel_row.history_cursor_post_id = raw_message.message_id
        return post_row

    async def _refresh_post_metadata_only(
        self,
        channel_row: SourceChannel,
        raw_message: RawTelegramMessage,
    ) -> SourceChannelPost:
        """Refresh an edited message without changing ingest/checkpoint state."""

        observed_at = utcnow()
        metadata = raw_message.telegram_post
        text_entities = metadata.entity_json()
        insert_statement = pg_insert(SourceChannelPost).values(
            id=uuid.uuid7(),
            source_channel_id=channel_row.id,
            post_id=raw_message.message_id,
            published_at=raw_message.published_at,
            media_type=raw_message.media_type,
            status=SourceChannelPostStatus.OBSERVED,
            attempt_count=0,
            is_retryable=False,
            first_observed_text=metadata.text,
            latest_text=metadata.text,
            first_observed_text_entities=text_entities,
            latest_text_entities=text_entities,
            media_group_id=metadata.media_group_id,
            reply_to_post_id=metadata.reply_to_post_id,
            telegram_edited_at=metadata.edited_at,
            metadata_first_observed_at=observed_at,
            metadata_last_observed_at=observed_at,
            metadata_version=metadata.schema_version,
            is_deleted=False,
            deletion_observed_at=None,
            created_at=observed_at,
            updated_at=observed_at,
        )
        excluded = insert_statement.excluded
        statement = (
            insert_statement.on_conflict_do_update(
                constraint="uq_source_channel_posts_channel_post",
                set_={
                    "first_observed_text": case(
                        (SourceChannelPost.metadata_version < metadata.schema_version, excluded.first_observed_text),
                        else_=SourceChannelPost.first_observed_text,
                    ),
                    "latest_text": excluded.latest_text,
                    "first_observed_text_entities": case(
                        (
                            SourceChannelPost.metadata_version < metadata.schema_version,
                            excluded.first_observed_text_entities,
                        ),
                        else_=SourceChannelPost.first_observed_text_entities,
                    ),
                    "latest_text_entities": excluded.latest_text_entities,
                    "media_group_id": excluded.media_group_id,
                    "reply_to_post_id": excluded.reply_to_post_id,
                    "telegram_edited_at": excluded.telegram_edited_at,
                    "metadata_first_observed_at": case(
                        (
                            SourceChannelPost.metadata_version < metadata.schema_version,
                            excluded.metadata_first_observed_at,
                        ),
                        else_=SourceChannelPost.metadata_first_observed_at,
                    ),
                    "metadata_last_observed_at": observed_at,
                    "metadata_version": metadata.schema_version,
                    "is_deleted": False,
                    "deletion_observed_at": None,
                    "updated_at": observed_at,
                },
            )
            .returning(SourceChannelPost)
        )
        return (
            await self.session.scalars(
                statement.execution_options(populate_existing=True),
            )
        ).one()

    async def _mark_posts_deleted(
        self,
        channel_row: SourceChannel,
        post_ids: tuple[str, ...],
    ) -> None:
        """Mark known posts deleted without erasing text or moving checkpoints."""

        if not post_ids:
            return
        observed_at = utcnow()
        await self.session.execute(
            update(SourceChannelPost)
            .where(
                SourceChannelPost.source_channel_id == channel_row.id,
                SourceChannelPost.post_id.in_(post_ids),
            )
            .values(
                is_deleted=True,
                deletion_observed_at=observed_at,
                updated_at=observed_at,
            )
        )

    @staticmethod
    def _advance_forward_checkpoint(channel_row: SourceChannel, post_id: str) -> None:
        if compare_telegram_post_ids(post_id, channel_row.last_read_post_id) > 0:
            channel_row.last_read_post_id = post_id

    @staticmethod
    def _advance_history_cursor(channel_row: SourceChannel, post_id: str) -> None:
        if (
            channel_row.history_cursor_post_id is None
            or compare_telegram_post_ids(post_id, channel_row.history_cursor_post_id) < 0
        ):
            channel_row.history_cursor_post_id = post_id

    @staticmethod
    def _mark_post_accepted(post_row: SourceChannelPost) -> None:
        post_row.status = SourceChannelPostStatus.ACCEPTED
        post_row.last_error_code = None
        post_row.last_error_text = None
        post_row.is_retryable = False
        post_row.next_attempt_at = None
        post_row.quarantined_at = None

    @classmethod
    def _mark_post_for_ingest_outcome(
        cls,
        post_row: SourceChannelPost,
        ingest_result: CrawlerIngestResult,
    ) -> None:
        if ingest_result.outcome in {
            CrawlerIngestOutcome.SKIPPED_UNSUPPORTED_MEDIA,
            CrawlerIngestOutcome.REJECTED_MALFORMED,
        }:
            cls._mark_post_unsupported(post_row)
            return
        if ingest_result.outcome is CrawlerIngestOutcome.SKIPPED_PAUSED_CHANNEL:
            cls._mark_post_failed(
                post_row,
                error_code="channel_paused",
                error_text="The source channel was paused while this post was being ingested.",
                retryable=True,
            )
            return
        cls._mark_post_accepted(post_row)

    @staticmethod
    def _mark_post_unsupported(
        post_row: SourceChannelPost,
        *,
        error_code: str | None = None,
        error_text: str | None = None,
    ) -> None:
        post_row.status = SourceChannelPostStatus.UNSUPPORTED
        post_row.last_error_code = None if error_code is None else error_code[:128]
        post_row.last_error_text = None if error_text is None else error_text[:4000]
        post_row.is_retryable = False
        post_row.next_attempt_at = None
        post_row.quarantined_at = None

    @staticmethod
    def _mark_post_failed(
        post_row: SourceChannelPost,
        *,
        error_code: str,
        error_text: str,
        retryable: bool,
    ) -> None:
        post_row.status = SourceChannelPostStatus.FAILED
        post_row.last_error_code = error_code[:128]
        post_row.last_error_text = error_text[:4000]
        post_row.is_retryable = retryable
        attempted_at = utcnow()
        post_row.last_attempt_at = attempted_at
        post_row.next_attempt_at = (
            attempted_at + timedelta(seconds=min(300.0, 5.0 * 2 ** max(post_row.attempt_count - 1, 0)))
            if retryable
            else None
        )

    @staticmethod
    def _tally_ingest_outcome(
        counters: _CatchupCounters,
        ingest_result: CrawlerIngestResult,
    ) -> None:
        """Update counters based on the service-side ingest outcome."""

        outcome = ingest_result.outcome
        if outcome in {
            CrawlerIngestOutcome.INGESTED,
            CrawlerIngestOutcome.PHASH_EXACT_NEW_FILE,
            CrawlerIngestOutcome.BLOCKED_PERCEPTUAL_HASH,
        }:
            counters.ingested += 1
        elif outcome in {
            CrawlerIngestOutcome.SHA256_EXACT_EXISTING_FILE,
            CrawlerIngestOutcome.BLOCKED_SHA256_EXISTING_FILE,
            CrawlerIngestOutcome.SKIPPED_DUPLICATE_POST_ID,
        }:
            counters.skipped_dedup += 1
        elif outcome in {
            CrawlerIngestOutcome.SKIPPED_UNSUPPORTED_MEDIA,
            CrawlerIngestOutcome.REJECTED_MALFORMED,
        }:
            counters.skipped_unsupported += 1
        elif outcome is CrawlerIngestOutcome.SKIPPED_PAUSED_CHANNEL:
            # The pause flag was flipped mid-sweep; treat as non-ingest.
            counters.record_error("channel_paused_mid_sweep")

    async def _run_live_listener(
        self,
        *,
        session_name: str,
        channel_ids: list[str],
        ready_event: asyncio.Event,
    ) -> None:
        """Handle new, edited, and deleted live Telegram events."""

        try:
            bound_channel_ids = set(channel_ids)
            async for live_event in self.telegram_client.listen_live(
                channel_ids=channel_ids,
                ready_event=ready_event,
            ):
                if isinstance(live_event, TelegramMessagesDeletedEvent):
                    if live_event.channel_id not in bound_channel_ids:
                        continue
                    channel_row = await self._get_tracked_channel(live_event.channel_id)
                    await self._mark_posts_deleted(channel_row, live_event.post_ids)
                    await self._record_live_heartbeat(session_name)
                    continue

                if not isinstance(live_event, (TelegramNewMessageEvent, TelegramMessageEditedEvent)):
                    continue
                raw_message = live_event.message
                if raw_message.channel_id not in bound_channel_ids:
                    logger.warning(
                        "Live listener for %s ignored message %s from unbound channel %s.",
                        session_name,
                        raw_message.message_id,
                        raw_message.channel_id,
                    )
                    continue
                channel_row = await self._get_tracked_channel(raw_message.channel_id)
                if isinstance(live_event, TelegramMessageEditedEvent):
                    await self._refresh_post_metadata_only(channel_row, raw_message)
                    await self._record_live_heartbeat(session_name)
                    continue

                post_row = await self._observe_post(
                    channel_row,
                    raw_message,
                    advance_history_cursor=False,
                )
                await self._commit_runtime_state()
                if raw_message.media_type == "unsupported":
                    self._mark_post_unsupported(post_row)
                    self._advance_forward_checkpoint(channel_row, raw_message.message_id)
                    await self._record_live_heartbeat(session_name)
                    continue
                counters = _CatchupCounters()
                await self._ingest_single_message(
                    counters=counters,
                    channel_row=channel_row,
                    raw_message=raw_message,
                    post_row=post_row,
                    advance_checkpoint=True,
                )
                for error in counters.errors:
                    logger.warning(
                        "Live ingest recorded an error for message %s: %s",
                        raw_message.message_id,
                        error,
                    )
                await self._record_live_heartbeat(session_name)
        except asyncio.CancelledError:
            raise
        except PipelineTelegramFloodWaitError as exc:
            telegram_session = await self._load_session(session_name)
            self._park_session_on_flood_wait(
                telegram_session,
                wait_seconds=exc.wait_seconds,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
            await self._commit_runtime_state()
        except PipelineTelegramSessionBannedError as exc:
            telegram_session = await self._load_session(session_name)
            await self._quarantine_session(
                telegram_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
        except PipelineTelegramSessionAuthRequiredError as exc:
            telegram_session = await self._load_session(session_name)
            await self._mark_session_auth_required(
                telegram_session,
                error_class=type(exc).__name__,
                error_text=str(exc),
            )
        except PipelineTelegramError as exc:
            logger.warning("Live listener for %s hit a provider error: %s", session_name, exc)

    async def _require_runnable_session(self, session_name: str) -> TelegramSession:
        """Return the enabled active session row, else raise."""

        state = await self._load_session(session_name)
        if not state.enabled:
            raise CrawlerSessionNotRunnableError(
                f"Session {session_name!r} is disabled.",
            )
        if state.status is not TelegramSessionStatus.ACTIVE:
            raise CrawlerSessionNotRunnableError(
                f"Session {session_name!r} is {state.status.value}, not active.",
            )
        return state

    async def _record_live_heartbeat(self, session_name: str) -> None:
        telegram_session = await self._load_session(session_name)
        telegram_session.last_heartbeat_at = utcnow()
        await self._commit_runtime_state()

    async def _get_owned_channel_or_raise(
        self,
        channel_id: str,
        telegram_session: TelegramSession,
    ) -> SourceChannel:
        channel = await self._get_tracked_channel(channel_id)
        if channel.telegram_session_id != telegram_session.id:
            raise CrawlerSessionNotRunnableError(
                f"Telegram channel {channel_id!r} is not assigned to session {telegram_session.name!r}.",
            )
        return channel

    async def _get_tracked_channel(self, channel_id: str) -> SourceChannel:
        """Return the ``SourceChannel`` row for a tracked Telegram channel or raise."""

        result = await self.session.execute(
            select(SourceChannel)
            .where(
                SourceChannel.platform == SourcePlatform.TELEGRAM,
                SourceChannel.platform_id == channel_id,
            )
            .limit(1)
        )
        channel = result.scalar_one_or_none()
        if channel is None:
            raise CrawlerSessionNotRunnableError(
                f"Cannot catch up untracked Telegram channel {channel_id!r}.",
            )
        return channel

    async def _refresh_channel_metadata(self, channel_row: SourceChannel) -> None:
        """Refresh drifted channel title + username from the live adapter.

        Kept deliberately small: the runtime only updates the two fields
        that can change on the Telegram side. ``subscriber_count`` is
        refreshed only if the adapter returned one (fakes often omit it).
        Operator-curated columns (``is_paused``, ``catchup_enabled``) are
        intentionally untouched.
        """

        try:
            resolved = await self.telegram_client.resolve_channel(channel_row.platform_id)
        except (
            PipelineTelegramFloodWaitError,
            PipelineTelegramSessionBannedError,
            PipelineTelegramSessionAuthRequiredError,
        ):
            raise
        except PipelineTelegramError as exc:
            logger.warning(
                "Failed to refresh channel metadata for %s: %s",
                channel_row.platform_id,
                exc,
            )
            return
        if resolved.title and resolved.title != channel_row.title:
            channel_row.title = resolved.title
        if resolved.username is not None and resolved.username != channel_row.username:
            channel_row.username = resolved.username
        if resolved.subscriber_count is not None:
            channel_row.subscriber_count = resolved.subscriber_count

    async def _channel_ids_for_session(self, telegram_session: TelegramSession) -> list[str]:
        """Return the active channel ids owned by this session."""

        result = await self.session.execute(
            select(SourceChannel).where(
                SourceChannel.platform == SourcePlatform.TELEGRAM,
                SourceChannel.is_active.is_(True),
                SourceChannel.is_paused.is_(False),
                SourceChannel.live_enabled.is_(True),
                SourceChannel.telegram_session_id == telegram_session.id,
            )
        )
        return [row.platform_id for row in result.scalars().all()]

    def _park_session_on_flood_wait(
        self,
        telegram_session: TelegramSession,
        *,
        wait_seconds: int,
        error_class: str,
        error_text: str,
    ) -> None:
        """Mark the session as ``flood_wait`` and persist the cooldown."""

        telegram_session.status = TelegramSessionStatus.FLOOD_WAIT
        telegram_session.flood_wait_until = utcnow() + timedelta(seconds=wait_seconds)
        telegram_session.last_error_class = error_class
        telegram_session.last_error_text = error_text[:4000]

    async def _quarantine_session(
        self,
        telegram_session: TelegramSession,
        *,
        error_class: str,
        error_text: str,
    ) -> None:
        """Mark the session as ``quarantined`` and persist the failure metadata."""

        now = utcnow()
        telegram_session.status = TelegramSessionStatus.QUARANTINED
        telegram_session.quarantined_at = now
        telegram_session.last_error_class = error_class
        telegram_session.last_error_text = error_text[:4000]
        await self._commit_runtime_state()

    async def _mark_session_auth_required(
        self,
        telegram_session: TelegramSession,
        *,
        error_class: str,
        error_text: str,
    ) -> None:
        """Mark the session auth-required after Telegram rejects stored credentials."""

        telegram_session.status = TelegramSessionStatus.AUTH_REQUIRED
        telegram_session.live_listener_started_at = None
        telegram_session.last_error_class = error_class[:128]
        telegram_session.last_error_text = error_text[:4000]
        await self._commit_runtime_state()

    async def _commit_runtime_state(self) -> None:
        """Commit any pending runtime-side mutations to durable crawler state."""

        try:
            await self.session.commit()
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise CrawlerSessionNotRunnableError(
                "Failed to persist crawler runtime state mutation.",
            ) from exc

    @staticmethod
    def _parse_checkpoint_post_id(post_id: str | None) -> int | None:
        """Return the numeric checkpoint post id, or ``None`` if unset or non-numeric."""

        if post_id is None:
            return None
        try:
            return int(post_id)
        except ValueError:
            return None

    async def _load_session(self, session_name: str) -> TelegramSession:
        """Return the durable session row for ``session_name`` or raise."""

        result = await self.session.execute(
            select(TelegramSession).where(TelegramSession.name == session_name).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        raise CrawlerSessionNotRunnableError(
            f"Telegram session {session_name!r} does not exist.",
        )


__all__ = [
    "CrawlerIngestServiceProtocol",
    "CrawlerCatchupReport",
    "TelegramCrawlerRuntime",
]
