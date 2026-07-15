# ruff: noqa: TC003
"""Multi-session supervisor for DB-backed Telegram crawler runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, func, or_, select

from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramFloodWaitError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
)
from memexpert.crawlers.telegram.runtime import CrawlerCatchupReport, TelegramCrawlerRuntime
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, SourceChannelBackfillJob, TelegramSession
from memexpert.models.enums import (
    RecoveryJobItemStatus,
    RecoveryWorkKind,
    SourceChannelBackfillJobStatus,
    SourcePlatform,
    TelegramSessionStatus,
)
from memexpert.models.operations import RecoveryJobItem, SourceChannelBackfillAttempt
from memexpert.services.errors import CrawlerSessionNotRunnableError
from memexpert.services.pipeline_reliability import is_historical_admission_open

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory
    from memexpert.pipeline.events import SourceEngagementCaptureRequestedEvent
    from memexpert.schemas.content_pipeline import CrawlerIngestResult


logger = logging.getLogger(__name__)

type TelegramClientFactory = Callable[
    [TelegramSession],
    PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
]
type CrawlerIngestServiceFactory = Callable[["AsyncSession"], PipelineCrawlerIngestService]
type TelegramSessionConfigurationSignature = tuple[
    uuid.UUID,
    str,
    bool,
    TelegramSessionStatus,
    datetime | None,
    datetime | None,
    str | None,
    bool,
    bool,
    float,
]
type TelegramChannelConfigurationSignature = tuple[
    uuid.UUID,
    SourcePlatform,
    str,
    uuid.UUID | None,
    bool,
    bool,
    bool,
    bool,
    int,
]
type TelegramCrawlerConfigurationSnapshot = tuple[
    tuple[TelegramSessionConfigurationSignature, ...],
    tuple[TelegramChannelConfigurationSignature, ...],
]

_NON_RETRYABLE_CATCHUP_ERROR_PREFIXES = (
    "channel_paused_mid_sweep",
    "download_malformed:",
    "mapper_malformed:",
)
_BACKFILL_LOCK_TIMEOUT = timedelta(minutes=5)
_BACKFILL_PAGE_SIZE = 100
_BACKFILL_MAX_AUTOMATIC_ATTEMPTS = 5
_BACKFILL_RETRY_BASE_SECONDS = 30.0


@dataclass(slots=True)
class _CachedTelegramClient:
    session_id: uuid.UUID
    session_name: str
    client: PipelineTelegramClientProtocol


@dataclass(slots=True)
class _LiveRuntimeHandle:
    session_id: uuid.UUID
    session_name: str
    db_session: AsyncSession
    runtime: TelegramCrawlerRuntime


@dataclass(frozen=True, slots=True)
class TelegramCrawlerReloadResult:
    """Outcome of one live-listener-then-catch-up reconciliation."""

    catchup_reports: tuple[CrawlerCatchupReport, ...]
    failed_session_names: tuple[str, ...]

    @property
    def retry_required(self) -> bool:
        if self.failed_session_names:
            return True
        return any(
            not error.startswith(_NON_RETRYABLE_CATCHUP_ERROR_PREFIXES)
            for report in self.catchup_reports
            for error in report.errors
        )


@dataclass(slots=True)
class TelegramSessionManager:
    """Supervise many runnable DB-backed Telegram sessions in one process.

    The manager owns process-local adapter caching and delegates actual
    channel work to :class:`TelegramCrawlerRuntime`, which remains the
    single-session executor. DB sessions are opened from the injected
    factory so catch-up/replay calls and live listeners do not share one
    request-scoped SQLAlchemy session.
    """

    settings: Settings
    session_factory: AsyncSessionFactory
    telegram_client_factory: TelegramClientFactory | None = None
    ingest_service_factory: CrawlerIngestServiceFactory | None = None
    _client_cache: dict[uuid.UUID, _CachedTelegramClient] = field(default_factory=dict, init=False, repr=False)
    _live_handles: dict[uuid.UUID, _LiveRuntimeHandle] = field(default_factory=dict, init=False, repr=False)
    _client_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _backfill_worker_id: str = field(
        default_factory=lambda: f"telegram-crawler-{uuid.uuid7()}",
        init=False,
        repr=False,
    )

    async def catch_up_all(self) -> list[CrawlerCatchupReport]:
        """Catch up every runnable catch-up-enabled session without cross-session aborts."""

        reports, _failed_session_names = await self._catch_up_all_with_failures()
        return reports

    async def _catch_up_all_with_failures(self) -> tuple[list[CrawlerCatchupReport], list[str]]:
        """Catch up runnable sessions and retain failures for reconciliation retry."""

        runnable_sessions = await self._discover_runnable_sessions(workload="catchup")
        reports: list[CrawlerCatchupReport] = []
        failed_session_names: list[str] = []
        for session_state in runnable_sessions:
            try:
                reports.extend(await self._catch_up_session(session_state.name, propagate_session_errors=False))
            except Exception:  # noqa: BLE001 - isolate one account from the rest of the sweep.
                logger.exception("Telegram catch-up failed unexpectedly for session %s.", session_state.name)
                failed_session_names.append(session_state.name)
                await self.invalidate_session(session_id=session_state.id)
        return reports, failed_session_names

    async def catch_up_session(self, session_name: str) -> list[CrawlerCatchupReport]:
        """Catch up active channels assigned to one runnable session."""

        return await self._catch_up_session(session_name, propagate_session_errors=True)

    async def process_backfill_jobs(self) -> int:
        """Claim and finish one queued/stale-running older-history job."""

        async with self.session_factory() as db_session:
            job = await self._claim_backfill_job(db_session)
            if job is None:
                return 0
            job_id = job.id
            try:
                await self._process_claimed_backfill_job(db_session, job)
            except asyncio.CancelledError:
                await db_session.rollback()
                await asyncio.shield(self._requeue_backfill_job(job_id))
                raise
            except Exception as exc:  # noqa: BLE001 - persist terminal operator-visible job failure.
                await db_session.rollback()
                await self._handle_backfill_exception(job_id, exc)
                logger.exception("Telegram older-history backfill job %s failed.", job_id)
            return 1

    async def replay_post(self, channel_id: str, post_id: str) -> CrawlerIngestResult:
        """Replay a post through the channel's currently assigned runnable session."""

        async with self.session_factory() as db_session:
            channel = await self._load_tracked_channel(db_session, channel_id)
            if not channel.is_active or channel.is_paused:
                reason = "inactive" if not channel.is_active else "paused"
                raise CrawlerSessionNotRunnableError(
                    f"Cannot replay Telegram channel {channel_id!r} because it is {reason}.",
                )
            if channel.telegram_session_id is None:
                raise CrawlerSessionNotRunnableError(
                    f"Cannot replay Telegram channel {channel_id!r} because it is not assigned to a session.",
                )
            session_state = await db_session.get(TelegramSession, channel.telegram_session_id)
            if session_state is None:
                raise CrawlerSessionNotRunnableError(
                    f"Cannot replay Telegram channel {channel_id!r} because its session is missing.",
                )
            self._assert_runnable_session(session_state, workload="replay")
            client = await self._get_cached_client(session_state)
            runtime = self._build_runtime(db_session=db_session, telegram_client=client)
            try:
                return await runtime.replay_post(channel_id, post_id)
            except (
                PipelineTelegramFloodWaitError,
                PipelineTelegramSessionBannedError,
                PipelineTelegramSessionAuthRequiredError,
            ):
                await self.invalidate_session(session_id=session_state.id)
                raise

    async def source_engagement_client_for_event(
        self,
        event: SourceEngagementCaptureRequestedEvent,
    ) -> PipelineTelegramClientProtocol:
        """Return the cached runnable client for a source-engagement stats fetch event."""

        async with self.session_factory() as db_session:
            session_state = await db_session.get(TelegramSession, event.telegram_session_id)
            if session_state is None:
                raise PipelineTelegramSessionNotRunnableError(
                    f"Telegram session {event.telegram_session_id} does not exist.",
                )
            if session_state.name != event.session_name:
                raise PipelineTelegramSessionNotRunnableError(
                    f"Telegram session {event.telegram_session_id} is named {session_state.name!r}, "
                    f"not {event.session_name!r}.",
                )
            if self._is_session_flood_waited(session_state):
                wait_seconds = self._remaining_flood_wait_seconds(session_state)
                raise PipelineTelegramFloodWaitError(
                    f"Telegram session {session_state.name!r} is flood-waited for {wait_seconds}s.",
                    wait_seconds=wait_seconds,
                )
            try:
                self._assert_runnable_session(session_state, workload="engagement")
            except CrawlerSessionNotRunnableError as exc:
                raise PipelineTelegramSessionNotRunnableError(str(exc)) from exc
            return await self._get_cached_client(session_state)

    async def start_live_all(self) -> list[str]:
        """Start runnable listeners and return session names that failed to start."""

        if not self.settings.crawler_live_mode_enabled:
            await self.stop_live_all()
            return []
        runnable_sessions = await self._discover_runnable_sessions(workload="live")
        runnable_ids = {row.id for row in runnable_sessions}
        for session_id in tuple(self._live_handles):
            if session_id not in runnable_ids:
                await self._stop_live_handle(session_id, mark_stopped=False)
                await self.invalidate_session(session_id=session_id)
        await self._invalidate_cached_clients_not_in(runnable_ids)
        failed_session_names: list[str] = []
        for session_state in runnable_sessions:
            try:
                await self.start_live_session(session_state.name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - start healthy sessions before reporting the aggregate failure.
                logger.exception("Telegram live listener failed to start for session %s.", session_state.name)
                failed_session_names.append(session_state.name)
                await self.invalidate_session(session_id=session_state.id)
        return failed_session_names

    async def supervise_live_listeners(self) -> tuple[str, ...]:
        """Observe terminated listener tasks and restart only their accounts.

        Listener tasks are intentionally account-scoped.  A provider failure
        therefore invalidates and reconnects that session without disturbing
        healthy listeners, and the process control loop can call this method
        on every reconciliation tick even when DB configuration is unchanged.
        """

        for session_id, handle in tuple(self._live_handles.items()):
            task = handle.runtime._live_tasks.get(  # noqa: SLF001 - manager owns runtime lifecycle.
                handle.session_name,
            )
            if task is None or not task.done():
                continue
            task_error: BaseException | None = None
            if not task.cancelled():
                task_error = task.exception()
            log = logger.error if task_error is not None else logger.warning
            log(
                "telegram_live_listener_terminated",
                extra={
                    "event": "telegram_live_listener_terminated",
                    "session_name": handle.session_name,
                    "error_class": type(task_error).__name__ if task_error is not None else None,
                    "retryable": True,
                },
                exc_info=(type(task_error), task_error, task_error.__traceback__) if task_error is not None else None,
            )
            await self.invalidate_session(session_id=session_id)

        return tuple(await self.start_live_all())

    async def start_live_session(self, session_name: str) -> None:
        """Start the live listener for one runnable live-enabled session."""

        if not self.settings.crawler_live_mode_enabled:
            return
        async with self.session_factory() as probe_session:
            session_state = await self._load_runnable_session(probe_session, session_name, workload="live")
            existing_handle = self._live_handles.get(session_state.id)
            if existing_handle is not None:
                existing_task = existing_handle.runtime._live_tasks.get(  # noqa: SLF001 - manager owns runtime lifecycle.
                    existing_handle.session_name,
                )
                if existing_task is not None and not existing_task.done():
                    return
                await self.invalidate_session(session_id=session_state.id)
            channel_ids = await self._channel_ids_for_session(
                probe_session,
                session_id=session_state.id,
                workload="live",
            )
            if not channel_ids:
                return

        db_session = self.session_factory()
        await db_session.__aenter__()
        try:
            session_state = await self._load_runnable_session(db_session, session_name, workload="live")
            client = await self._get_cached_client(session_state)
            runtime = self._build_runtime(db_session=db_session, telegram_client=client)
            await runtime.start_live_listener(session_state.name)
            if session_state.name not in runtime._live_tasks:  # noqa: SLF001 - manager owns runtime lifecycle here.
                await db_session.__aexit__(None, None, None)
                return
            self._live_handles[session_state.id] = _LiveRuntimeHandle(
                session_id=session_state.id,
                session_name=session_state.name,
                db_session=db_session,
                runtime=runtime,
            )
        except Exception:
            await db_session.__aexit__(None, None, None)
            raise

    async def stop_live_session(self, session_name: str, *, mark_stopped: bool = True) -> None:
        """Stop one live listener by session name."""

        session_id = await self._session_id_for_name(session_name)
        if session_id is None:
            return
        await self._stop_live_handle(session_id, mark_stopped=mark_stopped)
        if mark_stopped:
            await self.invalidate_session(session_id=session_id)

    async def stop_live_all(self, *, mark_stopped: bool = False) -> None:
        """Stop every managed live listener."""

        for session_id in tuple(self._live_handles):
            await self._stop_live_handle(session_id, mark_stopped=mark_stopped)

    async def invalidate_session(
        self,
        *,
        session_id: uuid.UUID | None = None,
        session_name: str | None = None,
    ) -> None:
        """Close and forget one cached client and live listener."""

        resolved_session_id = session_id
        if resolved_session_id is None and session_name is not None:
            resolved_session_id = await self._session_id_for_name(session_name)
        if resolved_session_id is None:
            return
        await self._stop_live_handle(resolved_session_id, mark_stopped=False)
        cached = self._client_cache.pop(resolved_session_id, None)
        if cached is not None:
            with suppress(Exception):
                await cached.client.close()

    async def configuration_snapshot(self) -> TelegramCrawlerConfigurationSnapshot:
        """Return durable crawler-control state for periodic change detection.

        Checkpoints, fetch timestamps, heartbeats, and refreshed Telegram
        metadata are runtime state and intentionally excluded so their normal
        writes never cause a client reconnect.
        """

        async with self.session_factory() as db_session:
            source_channels = list(
                (
                    await db_session.execute(
                        select(SourceChannel)
                        .where(SourceChannel.platform == SourcePlatform.TELEGRAM)
                        .order_by(SourceChannel.id.asc()),
                    )
                )
                .scalars()
                .all(),
            )
            assigned_session_ids = {
                row.telegram_session_id for row in source_channels if row.telegram_session_id is not None
            }
            telegram_sessions = (
                list(
                    (
                        await db_session.execute(
                            select(TelegramSession)
                            .where(TelegramSession.id.in_(assigned_session_ids))
                            .order_by(TelegramSession.id.asc()),
                        )
                    )
                    .scalars()
                    .all(),
                )
                if assigned_session_ids
                else []
            )

        session_signatures = tuple(
            (
                row.id,
                row.name,
                row.enabled,
                row.status,
                row.flood_wait_until,
                row.quarantined_at,
                self._secret_digest(row.encrypted_string_session),
                row.catchup_enabled,
                row.live_enabled,
                row.max_requests_per_second,
            )
            for row in telegram_sessions
        )
        channel_signatures = tuple(
            (
                row.id,
                row.platform,
                row.platform_id,
                row.telegram_session_id,
                row.is_active,
                row.is_paused,
                row.catchup_enabled,
                row.live_enabled,
                row.catchup_message_limit,
            )
            for row in source_channels
        )
        return session_signatures, channel_signatures

    async def reload(
        self,
        *,
        on_listeners_ready: Callable[[], None] | None = None,
    ) -> TelegramCrawlerReloadResult:
        """Register live listeners, then close the forward gap with catch-up."""

        await self.stop_live_all(mark_stopped=False)
        for session_id in tuple(self._client_cache):
            await self.invalidate_session(session_id=session_id)
        failed_session_names = await self.start_live_all()
        if on_listeners_ready is not None:
            on_listeners_ready()
        reports, catchup_failed_session_names = await self._catch_up_all_with_failures()
        failed_session_names.extend(catchup_failed_session_names)
        return TelegramCrawlerReloadResult(
            catchup_reports=tuple(reports),
            failed_session_names=tuple(dict.fromkeys(failed_session_names)),
        )

    async def retry_incomplete(self) -> TelegramCrawlerReloadResult:
        """Retry incomplete catch-up/listener work without tearing down healthy listeners."""

        failed_session_names = await self.start_live_all()
        reports, catchup_failed_session_names = await self._catch_up_all_with_failures()
        failed_session_names.extend(catchup_failed_session_names)
        return TelegramCrawlerReloadResult(
            catchup_reports=tuple(reports),
            failed_session_names=tuple(dict.fromkeys(failed_session_names)),
        )

    async def shutdown(self) -> None:
        """Stop all listeners and close every cached Telegram client."""

        await self.stop_live_all(mark_stopped=False)
        for session_id in tuple(self._client_cache):
            await self.invalidate_session(session_id=session_id)

    async def _claim_backfill_job(
        self,
        db_session: AsyncSession,
    ) -> SourceChannelBackfillJob | None:
        """Lease one due time slice, fencing stale owners and capacity-paused work."""

        now = utcnow()
        stale_before = now - _BACKFILL_LOCK_TIMEOUT
        job = await db_session.scalar(
            select(SourceChannelBackfillJob)
            .where(
                or_(
                    SourceChannelBackfillJob.status == SourceChannelBackfillJobStatus.QUEUED,
                    and_(
                        SourceChannelBackfillJob.status == SourceChannelBackfillJobStatus.WAITING_RETRY,
                        or_(
                            SourceChannelBackfillJob.next_attempt_at.is_(None),
                            SourceChannelBackfillJob.next_attempt_at <= now,
                        ),
                    ),
                    SourceChannelBackfillJob.status == SourceChannelBackfillJobStatus.WAITING_CAPACITY,
                    and_(
                        SourceChannelBackfillJob.status == SourceChannelBackfillJobStatus.RUNNING,
                        or_(
                            SourceChannelBackfillJob.locked_at.is_(None),
                            SourceChannelBackfillJob.locked_at < stale_before,
                        ),
                    ),
                ),
            )
            .order_by(SourceChannelBackfillJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1),
        )
        if job is None:
            return None
        if not await is_historical_admission_open(db_session):
            job.status = SourceChannelBackfillJobStatus.WAITING_CAPACITY
            job.is_retryable = True
            job.next_attempt_at = now + timedelta(seconds=15)
            job.locked_at = None
            job.lock_owner = None
            await db_session.commit()
            return None

        open_attempt = await db_session.scalar(
            select(SourceChannelBackfillAttempt).where(
                SourceChannelBackfillAttempt.backfill_job_id == job.id,
                SourceChannelBackfillAttempt.finished_at.is_(None),
            )
        )
        job.lease_generation += 1
        if open_attempt is None:
            job.attempt_count += 1
            recovery_item_id = await db_session.scalar(
                select(RecoveryJobItem.id)
                .where(
                    RecoveryJobItem.work_kind == RecoveryWorkKind.BACKFILL,
                    RecoveryJobItem.work_id == str(job.id),
                    RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED,
                )
                .order_by(RecoveryJobItem.dispatched_at.desc())
                .limit(1)
            )
            source_channel = await db_session.get(SourceChannel, job.source_channel_id)
            db_session.add(
                SourceChannelBackfillAttempt(
                    backfill_job_id=job.id,
                    attempt_number=job.attempt_count,
                    lease_generation=job.lease_generation,
                    telegram_session_id=(source_channel.telegram_session_id if source_channel is not None else None),
                    recovery_item_id=recovery_item_id,
                    worker_instance_id=self._backfill_worker_id,
                    started_at=now,
                )
            )
        job.status = SourceChannelBackfillJobStatus.RUNNING
        job.started_at = job.started_at or now
        job.locked_at = now
        job.lock_owner = self._backfill_worker_id
        job.is_retryable = False
        job.next_attempt_at = None
        job.last_error_code = None
        job.last_error_class = None
        job.last_error_text = None
        await db_session.commit()
        return job

    async def _process_claimed_backfill_job(
        self,
        db_session: AsyncSession,
        job: SourceChannelBackfillJob,
    ) -> None:
        """Process exactly one bounded page and release the lease for fair scheduling."""

        channel = await db_session.get(SourceChannel, job.source_channel_id)
        if channel is None:
            raise CrawlerSessionNotRunnableError(
                f"Backfill job {job.id} references missing source channel {job.source_channel_id}.",
            )
        if not channel.is_active or channel.is_paused or not channel.catchup_enabled:
            raise CrawlerSessionNotRunnableError(
                f"Backfill job {job.id} source channel is not runnable for catch-up.",
            )
        if channel.telegram_session_id is None:
            raise CrawlerSessionNotRunnableError(
                f"Backfill job {job.id} source channel is not assigned to a Telegram session.",
            )
        session_state = await db_session.get(TelegramSession, channel.telegram_session_id)
        if session_state is None:
            raise CrawlerSessionNotRunnableError(
                f"Backfill job {job.id} source channel session is missing.",
            )
        self._assert_runnable_session(session_state, workload="catchup")

        client = await self._get_cached_client(session_state)
        runtime = self._build_runtime(db_session=db_session, telegram_client=client)
        remaining = job.requested_message_count - job.scanned_message_count
        if remaining <= 0:
            await self._complete_backfill_job(db_session, job)
            return
        page_limit = min(
            remaining,
            channel.catchup_message_limit,
            self.settings.crawler_default_catchup_message_limit,
            _BACKFILL_PAGE_SIZE,
        )
        claimed_generation = job.lease_generation
        report = await runtime.catch_up_older_channel(
            session_state.name,
            channel.platform_id,
            before_post_id=channel.history_cursor_post_id or job.cursor_post_id,
            limit=page_limit,
        )
        await db_session.refresh(channel)
        await db_session.refresh(job)
        if job.lease_generation != claimed_generation or job.lock_owner != self._backfill_worker_id:
            raise RuntimeError(f"Backfill job {job.id} lease changed while its page was running.")

        retrying_post = report.retryable_failure_post_id is not None
        durable_scanned = max(report.messages_scanned - int(retrying_post), 0)
        job.scanned_message_count = min(
            job.requested_message_count,
            job.scanned_message_count + durable_scanned,
        )
        job.quarantined_message_count = min(
            job.scanned_message_count,
            job.quarantined_message_count + report.messages_quarantined,
        )
        job.cursor_post_id = channel.history_cursor_post_id
        if durable_scanned:
            job.last_progress_at = utcnow()

        if retrying_post:
            await self._schedule_backfill_retry(
                db_session,
                job,
                error_code="source_post_retry_pending",
                error_class="PipelineTelegramProviderUnavailableError",
                error_text="; ".join(report.errors) or "Telegram source post needs another attempt.",
                failed_post_id=report.retryable_failure_post_id,
            )
            return

        fatal_errors = tuple(error for error in report.errors if not _is_quarantined_post_error(error))
        if fatal_errors:
            await self._schedule_backfill_retry(
                db_session,
                job,
                error_code=_backfill_error_code(fatal_errors[0]),
                error_class="TelegramBackfillPageError",
                error_text="; ".join(fatal_errors),
            )
            return

        page_exhausted = report.messages_scanned < page_limit or channel.history_exhausted
        requested_complete = job.scanned_message_count >= job.requested_message_count
        if page_exhausted or requested_complete:
            await self._complete_backfill_job(db_session, job)
            return

        job.status = SourceChannelBackfillJobStatus.QUEUED
        job.locked_at = None
        job.lock_owner = None
        job.next_attempt_at = None
        await db_session.commit()

    async def _handle_backfill_exception(self, job_id: uuid.UUID, exc: Exception) -> None:
        """Classify one failed time slice into bounded automatic or operator recovery."""

        manual_only = isinstance(
            exc,
            (
                PipelineTelegramSessionAuthRequiredError,
                PipelineTelegramSessionBannedError,
                CrawlerSessionNotRunnableError,
            ),
        )
        async with self.session_factory() as db_session:
            job = await db_session.get(SourceChannelBackfillJob, job_id, with_for_update=True)
            if job is None:
                return
            await self._schedule_backfill_retry(
                db_session,
                job,
                error_code=_backfill_exception_code(exc),
                error_class=type(exc).__name__,
                error_text=str(exc) or type(exc).__name__,
                manual_only=manual_only,
            )

    async def _schedule_backfill_retry(
        self,
        db_session: AsyncSession,
        job: SourceChannelBackfillJob,
        *,
        error_code: str,
        error_class: str,
        error_text: str,
        failed_post_id: str | None = None,
        manual_only: bool = False,
    ) -> None:
        now = utcnow()
        exhausted = job.attempt_count >= _BACKFILL_MAX_AUTOMATIC_ATTEMPTS
        job.status = (
            SourceChannelBackfillJobStatus.FAILED
            if manual_only or exhausted
            else SourceChannelBackfillJobStatus.WAITING_RETRY
        )
        job.last_error_code = error_code[:128]
        job.last_error_class = error_class[:128]
        job.last_error_text = error_text[:4000]
        job.failed_post_id = failed_post_id
        job.is_retryable = True
        job.next_attempt_at = (
            None
            if manual_only or exhausted
            else now
            + timedelta(
                seconds=min(
                    300.0,
                    _BACKFILL_RETRY_BASE_SECONDS * 2 ** max(job.attempt_count - 1, 0),
                )
            )
        )
        job.completed_at = now if job.status is SourceChannelBackfillJobStatus.FAILED else None
        job.locked_at = None
        job.lock_owner = None
        await self._finish_open_backfill_attempt(
            db_session,
            job,
            normalized_reason=job.last_error_code,
            error_class=job.last_error_class,
            error_text=job.last_error_text,
            failed_post_id=failed_post_id,
            is_retryable=True,
        )
        await db_session.commit()

    async def _complete_backfill_job(
        self,
        db_session: AsyncSession,
        job: SourceChannelBackfillJob,
    ) -> None:
        job.status = (
            SourceChannelBackfillJobStatus.COMPLETED_WITH_FAILURES
            if job.quarantined_message_count
            else SourceChannelBackfillJobStatus.COMPLETED
        )
        job.completed_at = utcnow()
        job.last_progress_at = job.completed_at
        job.locked_at = None
        job.lock_owner = None
        job.last_error_code = None
        job.last_error_class = None
        job.last_error_text = None
        job.failed_post_id = None
        job.is_retryable = False
        job.next_attempt_at = None
        await self._finish_open_backfill_attempt(
            db_session,
            job,
            normalized_reason=None,
            error_class=None,
            error_text=None,
            failed_post_id=None,
            is_retryable=False,
        )
        await db_session.commit()

    @staticmethod
    async def _finish_open_backfill_attempt(
        db_session: AsyncSession,
        job: SourceChannelBackfillJob,
        *,
        normalized_reason: str | None,
        error_class: str | None,
        error_text: str | None,
        failed_post_id: str | None,
        is_retryable: bool,
    ) -> None:
        attempt = await db_session.scalar(
            select(SourceChannelBackfillAttempt)
            .where(
                SourceChannelBackfillAttempt.backfill_job_id == job.id,
                SourceChannelBackfillAttempt.finished_at.is_(None),
            )
            .order_by(SourceChannelBackfillAttempt.attempt_number.desc())
            .with_for_update()
            .limit(1)
        )
        if attempt is None:
            return
        attempt.normalized_reason = normalized_reason
        attempt.error_class = error_class
        attempt.safe_error_text = error_text
        attempt.failed_post_id = failed_post_id
        attempt.is_retryable = is_retryable
        attempt.finished_at = utcnow()

    async def _requeue_backfill_job(self, job_id: uuid.UUID) -> None:
        """Release an in-flight lease during graceful crawler shutdown."""

        async with self.session_factory() as db_session:
            job = await db_session.get(SourceChannelBackfillJob, job_id)
            if job is None:
                return
            job.status = SourceChannelBackfillJobStatus.QUEUED
            job.locked_at = None
            job.lock_owner = None
            await db_session.commit()

    async def _catch_up_session(
        self,
        session_name: str,
        *,
        propagate_session_errors: bool,
    ) -> list[CrawlerCatchupReport]:
        async with self.session_factory() as db_session:
            session_state = await self._load_runnable_session(db_session, session_name, workload="catchup")
            client = await self._get_cached_client(session_state)
            runtime = self._build_runtime(db_session=db_session, telegram_client=client)
            channel_ids = await self._channel_ids_for_session(
                db_session,
                session_id=session_state.id,
                workload="catchup",
            )
            reports: list[CrawlerCatchupReport] = []
            for channel_id in channel_ids:
                try:
                    report = await runtime.catch_up_channel(session_state.name, channel_id)
                    reports.append(report)
                    logger.info(
                        "telegram_crawler_channel_catchup_completed",
                        extra={
                            "event": "telegram_crawler_channel_catchup_completed",
                            "session_name": report.session_name,
                            "channel_id": report.channel_id,
                            "messages_scanned": report.messages_scanned,
                            "messages_ingested": report.messages_ingested,
                            "messages_skipped_unsupported": report.messages_skipped_unsupported,
                            "messages_skipped_dedup": report.messages_skipped_dedup,
                            "errors": report.errors,
                        },
                    )
                except (
                    PipelineTelegramSessionBannedError,
                    PipelineTelegramSessionAuthRequiredError,
                    PipelineTelegramFloodWaitError,
                ):
                    await self.invalidate_session(session_id=session_state.id)
                    if propagate_session_errors:
                        raise
                    break
                if not await self._is_session_still_runnable(
                    db_session,
                    session_id=session_state.id,
                    workload="catchup",
                ):
                    await self.invalidate_session(session_id=session_state.id)
                    break
            return reports

    async def _discover_runnable_sessions(self, *, workload: str) -> list[TelegramSession]:
        async with self.session_factory() as db_session:
            result = await db_session.execute(
                select(TelegramSession)
                .where(*self._runnable_filters(workload=workload))
                .order_by(TelegramSession.name.asc()),
            )
            return list(result.scalars().all())

    async def _load_runnable_session(
        self,
        db_session: AsyncSession,
        session_name: str,
        *,
        workload: str,
    ) -> TelegramSession:
        session_state = await db_session.scalar(
            select(TelegramSession).where(TelegramSession.name == session_name).limit(1),
        )
        if session_state is None:
            raise CrawlerSessionNotRunnableError(f"Telegram session {session_name!r} does not exist.")
        self._assert_runnable_session(session_state, workload=workload)
        return session_state

    async def _is_session_still_runnable(
        self,
        db_session: AsyncSession,
        *,
        session_id: uuid.UUID,
        workload: str,
    ) -> bool:
        session_state = await db_session.get(TelegramSession, session_id, populate_existing=True)
        if session_state is None:
            return False
        try:
            self._assert_runnable_session(session_state, workload=workload)
        except CrawlerSessionNotRunnableError:
            return False
        return True

    def _assert_runnable_session(self, session_state: TelegramSession, *, workload: str) -> None:
        now = utcnow()
        if not session_state.enabled:
            raise CrawlerSessionNotRunnableError(f"Telegram session {session_state.name!r} is disabled.")
        if session_state.status is not TelegramSessionStatus.ACTIVE:
            raise CrawlerSessionNotRunnableError(
                f"Telegram session {session_state.name!r} is {session_state.status.value}, not active.",
            )
        if session_state.flood_wait_until is not None and self._as_utc(session_state.flood_wait_until) > now:
            raise CrawlerSessionNotRunnableError(
                f"Telegram session {session_state.name!r} is flood-waited until {session_state.flood_wait_until}.",
            )
        if not (session_state.encrypted_string_session or "").strip():
            raise CrawlerSessionNotRunnableError(
                f"Telegram session {session_state.name!r} has no stored StringSession material.",
            )
        if workload == "catchup" and not session_state.catchup_enabled:
            raise CrawlerSessionNotRunnableError(f"Telegram session {session_state.name!r} has catch-up disabled.")
        if workload == "live" and not session_state.live_enabled:
            raise CrawlerSessionNotRunnableError(
                f"Telegram session {session_state.name!r} has live listening disabled.",
            )
        if workload == "engagement" and not session_state.engagement_enabled:
            raise CrawlerSessionNotRunnableError(
                f"Telegram session {session_state.name!r} has source engagement disabled.",
            )

    def _runnable_filters(self, *, workload: str) -> Sequence[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [
            TelegramSession.enabled.is_(True),
            TelegramSession.status == TelegramSessionStatus.ACTIVE,
            or_(TelegramSession.flood_wait_until.is_(None), TelegramSession.flood_wait_until <= utcnow()),
            TelegramSession.encrypted_string_session.is_not(None),
            func.length(func.trim(TelegramSession.encrypted_string_session)) > 0,
        ]
        if workload == "catchup":
            filters.append(TelegramSession.catchup_enabled.is_(True))
        elif workload == "live":
            filters.append(TelegramSession.live_enabled.is_(True))
        elif workload == "engagement":
            filters.append(TelegramSession.engagement_enabled.is_(True))
        return filters

    def _is_session_flood_waited(self, session_state: TelegramSession) -> bool:
        if session_state.status is TelegramSessionStatus.FLOOD_WAIT:
            return True
        return bool(
            session_state.flood_wait_until is not None and self._as_utc(session_state.flood_wait_until) > utcnow()
        )

    def _remaining_flood_wait_seconds(self, session_state: TelegramSession) -> int:
        if session_state.flood_wait_until is None:
            return 1
        flood_wait_until = self._as_utc(session_state.flood_wait_until)
        remaining = (flood_wait_until - utcnow()).total_seconds()
        return max(int(remaining), 1)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _channel_ids_for_session(
        self,
        db_session: AsyncSession,
        *,
        session_id: uuid.UUID,
        workload: str,
    ) -> list[str]:
        filters: list[ColumnElement[bool]] = [
            SourceChannel.platform == SourcePlatform.TELEGRAM,
            SourceChannel.is_active.is_(True),
            SourceChannel.is_paused.is_(False),
            SourceChannel.telegram_session_id == session_id,
        ]
        if workload == "catchup":
            filters.append(SourceChannel.catchup_enabled.is_(True))
        elif workload == "live":
            filters.append(SourceChannel.live_enabled.is_(True))
        result = await db_session.execute(
            select(SourceChannel.platform_id).where(*filters).order_by(SourceChannel.title.asc()),
        )
        return list(result.scalars().all())

    async def _load_tracked_channel(self, db_session: AsyncSession, channel_id: str) -> SourceChannel:
        channel = await db_session.scalar(
            select(SourceChannel)
            .where(
                SourceChannel.platform == SourcePlatform.TELEGRAM,
                SourceChannel.platform_id == channel_id,
            )
            .limit(1),
        )
        if channel is None:
            raise CrawlerSessionNotRunnableError(f"Cannot replay untracked Telegram channel {channel_id!r}.")
        return channel

    async def _get_cached_client(self, session_state: TelegramSession) -> PipelineTelegramClientProtocol:
        async with self._client_lock:
            cached = self._client_cache.get(session_state.id)
            if cached is not None and cached.session_name == session_state.name:
                return cached.client
            if cached is not None:
                with suppress(Exception):
                    await cached.client.close()
            client = await self._maybe_await(self._resolved_telegram_client_factory()(session_state))
            self._client_cache[session_state.id] = _CachedTelegramClient(
                session_id=session_state.id,
                session_name=session_state.name,
                client=client,
            )
            return client

    def _build_runtime(
        self,
        *,
        db_session: AsyncSession,
        telegram_client: PipelineTelegramClientProtocol,
    ) -> TelegramCrawlerRuntime:
        return TelegramCrawlerRuntime(
            ingest_service=self._resolved_ingest_service_factory()(db_session),
            telegram_client=telegram_client,
            session=db_session,
            settings=self.settings,
        )

    def _resolved_telegram_client_factory(self) -> TelegramClientFactory:
        if self.telegram_client_factory is not None:
            return self.telegram_client_factory

        def _factory(session_state: TelegramSession) -> PipelineTelegramClientProtocol:
            from memexpert.crawlers.telegram.telethon_adapter import PipelineTelethonClient

            return PipelineTelethonClient.create(settings=self.settings, session_name=session_state.name)

        return _factory

    def _resolved_ingest_service_factory(self) -> CrawlerIngestServiceFactory:
        if self.ingest_service_factory is not None:
            return self.ingest_service_factory

        return lambda db_session: PipelineCrawlerIngestService.from_settings(db_session, settings=self.settings)

    async def _session_id_for_name(self, session_name: str) -> uuid.UUID | None:
        async with self.session_factory() as db_session:
            return await db_session.scalar(
                select(TelegramSession.id).where(TelegramSession.name == session_name).limit(1),
            )

    async def _stop_live_handle(self, session_id: uuid.UUID, *, mark_stopped: bool) -> None:
        handle = self._live_handles.pop(session_id, None)
        if handle is None:
            return
        try:
            await handle.runtime.stop_live_listener(handle.session_name, mark_stopped=mark_stopped)
        except CrawlerSessionNotRunnableError:
            await handle.db_session.rollback()
        finally:
            await handle.db_session.__aexit__(None, None, None)

    async def _invalidate_cached_clients_not_in(self, runnable_ids: set[uuid.UUID]) -> None:
        for session_id in tuple(self._client_cache):
            if session_id not in runnable_ids:
                await self.invalidate_session(session_id=session_id)

    @staticmethod
    def _secret_digest(encrypted_string_session: str | None) -> str | None:
        normalized = (encrypted_string_session or "").strip()
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    async def _maybe_await(
        value: PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
    ) -> PipelineTelegramClientProtocol:
        if inspect.isawaitable(value):
            return cast("PipelineTelegramClientProtocol", await value)
        return value


def _is_quarantined_post_error(error: str) -> bool:
    return error.startswith(("download_unavailable:", "download_malformed:", "mapper_malformed:"))


def _backfill_error_code(error: str) -> str:
    prefix = error.partition(":")[0].strip().lower().replace(" ", "_")
    return (prefix or "telegram_backfill_page_failed")[:128]


def _backfill_exception_code(exc: Exception) -> str:
    if isinstance(exc, PipelineTelegramFloodWaitError):
        return "telegram_flood_wait"
    if isinstance(exc, PipelineTelegramProviderUnavailableError):
        return "telegram_provider_unavailable"
    if isinstance(exc, PipelineTelegramSessionAuthRequiredError):
        return "telegram_session_auth_required"
    if isinstance(exc, PipelineTelegramSessionBannedError):
        return "telegram_session_banned"
    if isinstance(exc, CrawlerSessionNotRunnableError):
        return "telegram_source_or_session_not_runnable"
    return type(exc).__name__[:128]


__all__ = [
    "CrawlerIngestServiceFactory",
    "TelegramCrawlerConfigurationSnapshot",
    "TelegramCrawlerReloadResult",
    "TelegramClientFactory",
    "TelegramSessionManager",
]
