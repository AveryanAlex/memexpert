# ruff: noqa: TC003
"""Multi-session supervisor for DB-backed Telegram crawler runtimes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, or_, select

from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramFloodWaitError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
)
from memexpert.crawlers.telegram.runtime import CrawlerCatchupReport, TelegramCrawlerRuntime
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, TelegramSession
from memexpert.models.enums import SourcePlatform, TelegramSessionStatus
from memexpert.services.errors import CrawlerSessionNotRunnableError

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

    async def catch_up_all(self) -> list[CrawlerCatchupReport]:
        """Catch up every runnable catch-up-enabled session without cross-session aborts."""

        runnable_sessions = await self._discover_runnable_sessions(workload="catchup")
        reports: list[CrawlerCatchupReport] = []
        for session_state in runnable_sessions:
            try:
                reports.extend(await self._catch_up_session(session_state.name, propagate_session_errors=False))
            except Exception:  # noqa: BLE001 - isolate one account from the rest of the sweep.
                logger.exception("Telegram catch-up failed unexpectedly for session %s.", session_state.name)
                await self.invalidate_session(session_id=session_state.id)
        return reports

    async def catch_up_session(self, session_name: str) -> list[CrawlerCatchupReport]:
        """Catch up active channels assigned to one runnable session."""

        return await self._catch_up_session(session_name, propagate_session_errors=True)

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

    async def start_live_all(self) -> None:
        """Start one listener for each runnable live-enabled session and stop stale ones."""

        if not self.settings.crawler_live_mode_enabled:
            await self.stop_live_all()
            return
        runnable_sessions = await self._discover_runnable_sessions(workload="live")
        runnable_ids = {row.id for row in runnable_sessions}
        for session_id in tuple(self._live_handles):
            if session_id not in runnable_ids:
                await self._stop_live_handle(session_id, mark_stopped=False)
                await self.invalidate_session(session_id=session_id)
        await self._invalidate_cached_clients_not_in(runnable_ids)
        for session_state in runnable_sessions:
            await self.start_live_session(session_state.name)

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

    async def reload(self) -> None:
        """Close stale clients/listeners and rebuild live listeners from current DB state."""

        await self.stop_live_all(mark_stopped=False)
        for session_id in tuple(self._client_cache):
            await self.invalidate_session(session_id=session_id)
        await self.start_live_all()

    async def shutdown(self) -> None:
        """Stop all listeners and close every cached Telegram client."""

        await self.stop_live_all(mark_stopped=False)
        for session_id in tuple(self._client_cache):
            await self.invalidate_session(session_id=session_id)

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
                    reports.append(await runtime.catch_up_channel(session_state.name, channel_id))
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
            select(TelegramSession)
            .where(TelegramSession.name == session_name)
            .limit(1),
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
            session_state.flood_wait_until is not None
            and self._as_utc(session_state.flood_wait_until) > utcnow()
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
    async def _maybe_await(
        value: PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
    ) -> PipelineTelegramClientProtocol:
        if inspect.isawaitable(value):
            return cast("PipelineTelegramClientProtocol", await value)
        return value


__all__ = [
    "CrawlerIngestServiceFactory",
    "TelegramClientFactory",
    "TelegramSessionManager",
]
