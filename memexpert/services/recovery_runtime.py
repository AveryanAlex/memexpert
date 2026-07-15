"""Durable execution and reconciliation for browser-admin recovery jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.messaging.rabbitmq_outbox import outbox_message_from_spec
from memexpert.models.base import utcnow
from memexpert.models.content import (
    MemeFileSyncTargetSnapshot,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    SourceChannel,
    SourceChannelBackfillJob,
    SourceChannelPost,
)
from memexpert.models.enums import (
    ContentPipelineStageStatus,
    PipelineIngestRequestStatus,
    RabbitMQOutboxMessageStatus,
    RecoveryCapability,
    RecoveryDeadLetterStatus,
    RecoveryJobItemStatus,
    RecoveryJobStatus,
    RecoveryWorkKind,
    SourceChannelBackfillJobStatus,
    SourceChannelPostStatus,
    SyncTargetStatus,
)
from memexpert.models.operations import PipelineDeadLetter, RecoveryJob, RecoveryJobItem
from memexpert.pipeline.events import (
    MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
    PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
    build_media_inspect_message_spec,
)
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.services.admin_recovery import (
    AdminRecoveryConflictError,
    AdminRecoveryNotFoundError,
    AdminRecoveryService,
)
from memexpert.services.pipeline_reliability import is_historical_admission_open, is_stage_admitted

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.crawlers.telegram.manager import TelegramSessionManager
    from memexpert.schemas.admin_recovery import RecoveryWorkRead


logger = logging.getLogger(__name__)

_GENERAL_ACTIONS: Final = {
    RecoveryCapability.REINSPECT_INGEST,
    RecoveryCapability.RETRY_STAGE,
    RecoveryCapability.RESYNC_TARGET,
    RecoveryCapability.REBUILD_OUTBOX,
    RecoveryCapability.RECOVER_DEAD_LETTER,
    RecoveryCapability.ARCHIVE_DEAD_LETTER,
}
_TELEGRAM_ACTIONS: Final = {
    RecoveryCapability.RESUME_BACKFILL,
    RecoveryCapability.REPLAY_SOURCE_POST,
    RecoveryCapability.RECOVER_DEAD_LETTER,
}
_TERMINAL_ITEM_STATUSES: Final = {
    RecoveryJobItemStatus.SUCCEEDED,
    RecoveryJobItemStatus.FAILED,
    RecoveryJobItemStatus.SKIPPED_STALE,
    RecoveryJobItemStatus.CANCELLED,
}
_SUCCESSFUL_ITEM_STATUSES: Final = {
    RecoveryJobItemStatus.SUCCEEDED,
    RecoveryJobItemStatus.CANCELLED,
}
_RECOVERY_DISPATCH_STALE_AFTER: Final = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class RecoveryDispatchResult:
    claimed: int
    dispatched: int
    waiting_capacity: int
    failed: int
    skipped_stale: int
    reclaimed: int = 0


@dataclass(slots=True)
class _MutableDispatchCounts:
    claimed: int = 0
    dispatched: int = 0
    waiting_capacity: int = 0
    failed: int = 0
    skipped_stale: int = 0

    def freeze(self) -> RecoveryDispatchResult:
        return RecoveryDispatchResult(
            claimed=self.claimed,
            dispatched=self.dispatched,
            waiting_capacity=self.waiting_capacity,
            failed=self.failed,
            skipped_stale=self.skipped_stale,
        )


async def run_recovery_dispatch_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    batch_size: int = 50,
) -> RecoveryDispatchResult:
    """Dispatch a bounded batch of non-Telegram recovery work."""

    runtime = RecoveryRuntime(session_factory=session_factory, settings=settings or get_settings())
    reclaimed = await runtime.reclaim_stuck_work(batch_size=max(min(batch_size, 100), 1))
    result = await runtime.dispatch_general_batch(batch_size=batch_size)
    await runtime.reconcile(batch_size=max(batch_size * 4, 100))
    return replace(result, reclaimed=reclaimed)


async def run_recovery_reconcile_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    batch_size: int = 200,
) -> int:
    """Reconcile dispatched work and aggregate recovery-job counters."""

    runtime = RecoveryRuntime(session_factory=session_factory, settings=settings or get_settings())
    return await runtime.reconcile(batch_size=batch_size)


async def run_telegram_recovery_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    """Run Telegram recovery/backfill work beside the isolated Telegram worker role."""

    from memexpert.crawlers.telegram.manager import TelegramSessionManager

    session_factory = get_async_session_factory()
    manager = TelegramSessionManager(settings=settings, session_factory=session_factory)
    runtime = RecoveryRuntime(session_factory=session_factory, settings=settings)
    poll_seconds = float(getattr(settings, "recovery_telegram_poll_interval_seconds", 5.0))
    batch_size = int(getattr(settings, "recovery_telegram_batch_size", 10))
    try:
        while not stop_event.is_set():
            try:
                result = await runtime.dispatch_telegram_batch(manager, batch_size=batch_size)
                processed_backfill = await manager.process_backfill_jobs()
                reconciled = await runtime.reconcile(batch_size=max(batch_size * 4, 40))
                if result.claimed or processed_backfill or reconciled:
                    logger.info(
                        "telegram_recovery_iteration_completed",
                        extra={
                            "event": "telegram_recovery_iteration_completed",
                            "claimed": result.claimed,
                            "dispatched": result.dispatched,
                            "waiting_capacity": result.waiting_capacity,
                            "failed": result.failed,
                            "skipped_stale": result.skipped_stale,
                            "processed_backfill": processed_backfill,
                            "reconciled": reconciled,
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "telegram_recovery_iteration_failed",
                    extra={"event": "telegram_recovery_iteration_failed"},
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue
    finally:
        await manager.shutdown()


class RecoveryRuntime:
    """Execute version-fenced recovery actions and reconcile their outcomes."""

    def __init__(self, *, session_factory: AsyncSessionFactory, settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def dispatch_general_batch(self, *, batch_size: int) -> RecoveryDispatchResult:
        counts = _MutableDispatchCounts()
        for _ in range(max(batch_size, 1)):
            outcome = await self._dispatch_next_general()
            if outcome is None:
                break
            counts.claimed += 1
            setattr(counts, outcome, getattr(counts, outcome) + 1)
        return counts.freeze()

    async def reclaim_stuck_work(self, *, batch_size: int) -> int:
        """Reconstruct lost stage/ingest dispatches without operator intervention."""

        reclaimed = 0
        for _ in range(max(batch_size, 1)):
            outcome = await self._reclaim_next_stuck_stage()
            if outcome is None:
                outcome = await self._reclaim_next_stuck_ingest()
            if outcome is None:
                break
            if outcome is False:
                break
            reclaimed += int(outcome)
        return reclaimed

    async def dispatch_telegram_batch(
        self,
        manager: TelegramSessionManager,
        *,
        batch_size: int,
    ) -> RecoveryDispatchResult:
        counts = _MutableDispatchCounts()
        for _ in range(max(batch_size, 1)):
            outcome = await self._dispatch_next_telegram(manager)
            if outcome is None:
                break
            counts.claimed += 1
            setattr(counts, outcome, getattr(counts, outcome) + 1)
        return counts.freeze()

    async def reconcile(self, *, batch_size: int) -> int:
        reconciled = 0
        async with self._session_factory() as session:
            items = (
                (
                    await session.execute(
                        select(RecoveryJobItem)
                        .where(RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED)
                        .order_by(RecoveryJobItem.dispatched_at.asc(), RecoveryJobItem.id.asc())
                        .with_for_update(skip_locked=True)
                        .limit(max(batch_size, 1))
                    )
                )
                .scalars()
                .all()
            )
            for item in items:
                terminal = await self._reconcile_item(session, item)
                reconciled += int(terminal)
            await self._reconcile_jobs(session)
            await session.commit()
        return reconciled

    async def _dispatch_next_general(self) -> str | None:
        async with self._session_factory() as session:
            item = await self._claim_next_item(session, actions=_GENERAL_ACTIONS, telegram=False)
            if item is None:
                return None
            try:
                work = await self._validate_claimed_item(session, item)
            except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                    error=str(exc),
                )
                await session.commit()
                return "skipped_stale"
            if not await self._is_admitted(session, item, work):
                item.status = RecoveryJobItemStatus.WAITING_CAPACITY
                item.normalized_reason = "pipeline_capacity_closed"
                item.safe_error_text = "Recovery is waiting for the pipeline backlog admission gate to reopen."
                await session.commit()
                return "waiting_capacity"
            try:
                await self._execute_general_action(session, item, work)
            except Exception as exc:  # noqa: BLE001 - persist one bounded operator-visible action failure.
                await session.rollback()
                await self._mark_item_failed(item.id, exc)
                logger.exception(
                    "recovery_dispatch_failed",
                    extra={
                        "event": "recovery_dispatch_failed",
                        "recovery_item_id": str(item.id),
                        "action": item.action.value,
                    },
                )
                return "failed"
            return "dispatched"

    async def _reclaim_next_stuck_stage(self) -> bool | None:
        stale_before = utcnow() - timedelta(seconds=self._settings.pipeline_stuck_reclaim_after_seconds)
        async with self._session_factory() as session:
            row = await session.scalar(
                select(PipelineStageJournal)
                .where(
                    PipelineStageJournal.status == ContentPipelineStageStatus.PROCESSING,
                    PipelineStageJournal.updated_at < stale_before,
                )
                .order_by(PipelineStageJournal.updated_at.asc(), PipelineStageJournal.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            linked_recovery_item = None
            if row.last_event_id is not None:
                linked_recovery_item = await session.scalar(
                    select(RecoveryJobItem).where(
                        RecoveryJobItem.dispatch_event_id == row.last_event_id,
                        RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED,
                    )
                )
            row.status = ContentPipelineStageStatus.FAILED
            row.normalized_reason = "stale_stage_reclaimed"
            row.last_error_text = "The automatic reconciler recovered a stage whose worker lease made no progress."
            row.is_retryable = True
            row.retry_after = None
            row.finished_at = utcnow()
            if row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts:
                await session.commit()
                return True
            stage_row_id = row.id
            try:
                await PipelineReplayService(session, settings=self._settings).replay_item(
                    row.meme_file_id,
                    stage=row.stage,
                    recovery_item=linked_recovery_item,
                )
            except Exception:
                await session.rollback()
                logger.exception(
                    "stuck_pipeline_stage_reclaim_failed",
                    extra={
                        "event": "stuck_pipeline_stage_reclaim_failed",
                        "pipeline_stage_journal_id": str(stage_row_id),
                    },
                )
                return False
            return True

    async def _reclaim_next_stuck_ingest(self) -> bool | None:
        stale_before = utcnow() - timedelta(seconds=self._settings.pipeline_stuck_reclaim_after_seconds)
        async with self._session_factory() as session:
            request = await session.scalar(
                select(PipelineIngestRequest)
                .where(
                    PipelineIngestRequest.status == PipelineIngestRequestStatus.MEDIA_INSPECTING,
                    PipelineIngestRequest.updated_at < stale_before,
                )
                .order_by(PipelineIngestRequest.updated_at.asc(), PipelineIngestRequest.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if request is None:
                return None
            if request.attempt_count >= self._settings.pipeline_broker_retry_max_attempts:
                request.status = PipelineIngestRequestStatus.PUBLISH_FAILED
                request.failure_code = "media_inspect_stuck_attempts_exhausted"
                request.failure_detail = "Media inspection made no progress after the automatic attempt budget."
                request.locked_at = None
                await session.commit()
                return True

            existing_outbox = await session.scalar(
                select(RabbitMQOutboxMessage.id).where(
                    RabbitMQOutboxMessage.aggregate_type == PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
                    RabbitMQOutboxMessage.aggregate_id == str(request.id),
                    RabbitMQOutboxMessage.event_type == MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
                    RabbitMQOutboxMessage.status.in_(
                        (
                            RabbitMQOutboxMessageStatus.PENDING,
                            RabbitMQOutboxMessageStatus.FAILED,
                            RabbitMQOutboxMessageStatus.PUBLISHING,
                            RabbitMQOutboxMessageStatus.PUBLISHED,
                        )
                    ),
                )
            )
            request.status = PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
            request.failure_code = None
            request.failure_detail = None
            request.locked_at = None
            if existing_outbox is None:
                spec = build_media_inspect_message_spec(request, settings=self._settings)
                session.add(outbox_message_from_spec(spec))
                linked_recovery_item = await session.scalar(
                    select(RecoveryJobItem).where(
                        RecoveryJobItem.work_kind == RecoveryWorkKind.INGEST_REQUEST,
                        RecoveryJobItem.work_id == str(request.id),
                        RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED,
                    )
                )
                if linked_recovery_item is not None:
                    linked_recovery_item.dispatch_event_id = uuid.UUID(spec.message_id)
                    linked_recovery_item.dispatched_at = utcnow()
            await session.commit()
            return True

    async def _dispatch_next_telegram(self, manager: TelegramSessionManager) -> str | None:
        async with self._session_factory() as session:
            item = await self._claim_next_item(session, actions=_TELEGRAM_ACTIONS, telegram=True)
            if item is None:
                return None
            try:
                work = await self._validate_claimed_item(session, item)
            except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                    error=str(exc),
                )
                await session.commit()
                return "skipped_stale"
            if not await self._is_admitted(session, item, work):
                item.status = RecoveryJobItemStatus.WAITING_CAPACITY
                item.normalized_reason = "pipeline_capacity_closed"
                item.safe_error_text = "Recovery is waiting for the pipeline backlog admission gate to reopen."
                await session.commit()
                return "waiting_capacity"
            try:
                await self._execute_telegram_action(session, item, work, manager)
            except Exception as exc:  # noqa: BLE001 - isolate one Telegram recovery from the worker loop.
                await session.rollback()
                await self._mark_item_failed(item.id, exc)
                logger.exception(
                    "telegram_recovery_dispatch_failed",
                    extra={
                        "event": "telegram_recovery_dispatch_failed",
                        "recovery_item_id": str(item.id),
                        "action": item.action.value,
                    },
                )
                return "failed"
            return "dispatched"

    async def _claim_next_item(
        self,
        session: AsyncSession,
        *,
        actions: set[RecoveryCapability],
        telegram: bool,
    ) -> RecoveryJobItem | None:
        now = utcnow()
        stale_before = now - _RECOVERY_DISPATCH_STALE_AFTER
        kind_predicate = (
            RecoveryJobItem.work_kind.in_((RecoveryWorkKind.BACKFILL, RecoveryWorkKind.SOURCE_POST))
            if telegram
            else RecoveryJobItem.work_kind.not_in((RecoveryWorkKind.BACKFILL, RecoveryWorkKind.SOURCE_POST))
        )
        item = await session.scalar(
            select(RecoveryJobItem)
            .join(RecoveryJob, RecoveryJob.id == RecoveryJobItem.recovery_job_id)
            .where(
                RecoveryJob.status.in_((RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING)),
                RecoveryJobItem.action.in_(actions),
                kind_predicate,
                (
                    RecoveryJobItem.status.in_((RecoveryJobItemStatus.QUEUED, RecoveryJobItemStatus.WAITING_CAPACITY))
                    | (
                        (RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED)
                        & (RecoveryJobItem.dispatched_at < stale_before)
                        & RecoveryJobItem.dispatch_event_id.is_(None)
                    )
                ),
            )
            .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        job = await session.get(RecoveryJob, item.recovery_job_id, with_for_update=True)
        if job is not None and job.status is RecoveryJobStatus.QUEUED:
            job.status = RecoveryJobStatus.RUNNING
        return item

    async def _validate_claimed_item(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
    ) -> RecoveryWorkRead:
        await self._lock_canonical_work(session, item)
        work = await AdminRecoveryService(session).get_work(item.work_kind, item.work_id)
        if item.action is RecoveryCapability.RECOVER_DEAD_LETTER:
            await self._lock_recovery_dead_letter(session, item, work)
            work = await AdminRecoveryService(session).get_work(item.work_kind, item.work_id)
        if work.version != item.expected_version:
            raise AdminRecoveryConflictError("Recovery target changed after it was scheduled.")
        if item.action not in work.capabilities:
            raise AdminRecoveryConflictError(work.blocked_reason or "The scheduled recovery action is no longer valid.")
        return work

    async def _lock_canonical_work(self, session: AsyncSession, item: RecoveryJobItem) -> None:
        model_by_kind = {
            RecoveryWorkKind.PIPELINE_STAGE: PipelineStageJournal,
            RecoveryWorkKind.SYNC_TARGET: MemeFileSyncTargetSnapshot,
            RecoveryWorkKind.INGEST_REQUEST: PipelineIngestRequest,
            RecoveryWorkKind.OUTBOX: RabbitMQOutboxMessage,
            RecoveryWorkKind.BACKFILL: SourceChannelBackfillJob,
            RecoveryWorkKind.SOURCE_POST: SourceChannelPost,
            RecoveryWorkKind.DEAD_LETTER: PipelineDeadLetter,
        }
        model = model_by_kind[item.work_kind]
        row = await _try_load_uuid_row(session, model, item.work_id, with_for_update=True)
        if row is None:
            raise AdminRecoveryNotFoundError(
                f"Recovery work {item.work_kind.value}/{item.work_id} does not exist."
            )

    async def _lock_recovery_dead_letter(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        work: RecoveryWorkRead,
    ) -> None:
        dead_letter_id = work.details.get("dead_letter_id")
        if dead_letter_id is None and item.work_kind is RecoveryWorkKind.DEAD_LETTER:
            dead_letter_id = item.work_id
        if dead_letter_id is None:
            raise AdminRecoveryConflictError("The linked dead letter is no longer recoverable.")
        dead_letter = await _try_load_uuid_row(
            session,
            PipelineDeadLetter,
            str(dead_letter_id),
            with_for_update=True,
        )
        if dead_letter is None:
            raise AdminRecoveryNotFoundError(f"Recovery dead letter {dead_letter_id} does not exist.")
        self._assert_dead_letter_recoverable(dead_letter, item)

    @staticmethod
    def _assert_dead_letter_recoverable(
        dead_letter: PipelineDeadLetter,
        item: RecoveryJobItem,
    ) -> None:
        if dead_letter.status is RecoveryDeadLetterStatus.UNRESOLVED:
            return
        if (
            dead_letter.status is RecoveryDeadLetterStatus.RECOVERY_QUEUED
            and dead_letter.recovery_item_id == item.id
        ):
            return
        raise AdminRecoveryConflictError("The linked dead letter is no longer recoverable by this job.")

    async def _is_admitted(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        work: RecoveryWorkRead,
    ) -> bool:
        if item.work_kind in {RecoveryWorkKind.BACKFILL, RecoveryWorkKind.SOURCE_POST}:
            return await is_historical_admission_open(session)
        if work.stage is not None and item.action in {
            RecoveryCapability.RETRY_STAGE,
            RecoveryCapability.RESYNC_TARGET,
            RecoveryCapability.RECOVER_DEAD_LETTER,
        }:
            return await is_stage_admitted(session, work.stage)
        return True

    async def _execute_general_action(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        work: RecoveryWorkRead,
    ) -> None:
        if item.action is RecoveryCapability.RETRY_STAGE:
            await self._retry_stage(session, item)
            return
        if item.action is RecoveryCapability.RESYNC_TARGET:
            await self._resync_target(session, item)
            return
        if item.action is RecoveryCapability.REINSPECT_INGEST:
            await self._reinspect_ingest(session, item)
            return
        if item.action is RecoveryCapability.REBUILD_OUTBOX:
            await self._rebuild_outbox(session, item)
            return
        if item.action is RecoveryCapability.ARCHIVE_DEAD_LETTER:
            await self._archive_dead_letter(session, item)
            return
        if item.action is RecoveryCapability.RECOVER_DEAD_LETTER:
            await self._recover_dead_letter(session, item, work)
            return
        raise RuntimeError(f"Unsupported general recovery action {item.action.value!r}.")

    async def _execute_telegram_action(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        work: RecoveryWorkRead,
        manager: TelegramSessionManager,
    ) -> None:
        if item.action is RecoveryCapability.RECOVER_DEAD_LETTER:
            dead_letter = await self._load_linked_dead_letter(session, item, work)
            dead_letter.status = RecoveryDeadLetterStatus.RECOVERY_QUEUED
            dead_letter.recovery_item_id = item.id
            if item.work_kind is RecoveryWorkKind.BACKFILL:
                await self._queue_backfill(session, item)
                return
            if item.work_kind is RecoveryWorkKind.SOURCE_POST:
                await self._replay_source_post(session, item, manager)
                return
            raise RuntimeError("Telegram dead letter is not linked to replayable Telegram work.")
        if item.action is RecoveryCapability.RESUME_BACKFILL:
            await self._queue_backfill(session, item)
            return
        if item.action is RecoveryCapability.REPLAY_SOURCE_POST:
            await self._replay_source_post(session, item, manager)
            return
        raise RuntimeError(f"Unsupported Telegram recovery action {item.action.value!r}.")

    async def _retry_stage(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        *,
        work_id: str | None = None,
    ) -> None:
        stage_row = await _load_uuid_row(session, PipelineStageJournal, work_id or item.work_id)
        await PipelineReplayService(session, settings=self._settings).replay_item(
            stage_row.meme_file_id,
            stage=stage_row.stage,
            recovery_item=item,
        )

    async def _resync_target(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        *,
        work_id: str | None = None,
    ) -> None:
        snapshot = await _load_uuid_row(session, MemeFileSyncTargetSnapshot, work_id or item.work_id)
        await PipelineReplayService(session, settings=self._settings).replay_sync_target(
            snapshot.meme_file_id,
            snapshot.sync_target,
            recovery_item=item,
        )

    async def _reinspect_ingest(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        *,
        work_id: str | None = None,
    ) -> None:
        request = await _load_uuid_row(session, PipelineIngestRequest, work_id or item.work_id)
        request.status = PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING
        request.failure_code = None
        request.failure_detail = None
        request.locked_at = None
        spec = build_media_inspect_message_spec(request, settings=self._settings)
        session.add(outbox_message_from_spec(spec))
        item.status = RecoveryJobItemStatus.DISPATCHED
        item.dispatch_event_id = uuid.UUID(spec.message_id)
        item.dispatched_at = utcnow()
        item.normalized_reason = None
        item.safe_error_text = None
        await session.commit()

    async def _rebuild_outbox(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        *,
        work_id: str | None = None,
    ) -> None:
        outbox = await _load_uuid_row(session, RabbitMQOutboxMessage, work_id or item.work_id)
        outbox.status = RabbitMQOutboxMessageStatus.PENDING
        outbox.next_retry_at = utcnow()
        outbox.locked_at = None
        outbox.lock_owner = None
        outbox.last_error_text = None
        item.status = RecoveryJobItemStatus.DISPATCHED
        item.dispatch_event_id = _try_parse_uuid(outbox.message_id)
        item.dispatched_at = utcnow()
        item.finished_at = None
        item.normalized_reason = None
        item.safe_error_text = None
        await session.commit()

    async def _archive_dead_letter(self, session: AsyncSession, item: RecoveryJobItem) -> None:
        dead_letter = await _load_uuid_row(session, PipelineDeadLetter, item.work_id)
        dead_letter.status = RecoveryDeadLetterStatus.ARCHIVED
        dead_letter.resolved_at = utcnow()
        dead_letter.recovery_item_id = item.id
        dead_letter.resolution_note = "Archived by an audited admin recovery action."
        self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
        await session.commit()

    async def _recover_dead_letter(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        work: RecoveryWorkRead,
    ) -> None:
        dead_letter = await self._load_linked_dead_letter(session, item, work)
        dead_letter.status = RecoveryDeadLetterStatus.RECOVERY_QUEUED
        dead_letter.recovery_item_id = item.id
        if item.work_kind is RecoveryWorkKind.PIPELINE_STAGE:
            await self._retry_stage(session, item)
            return
        if item.work_kind is RecoveryWorkKind.SYNC_TARGET:
            await self._resync_target(session, item)
            return
        if item.work_kind is RecoveryWorkKind.INGEST_REQUEST:
            await self._reinspect_ingest(session, item)
            return
        if item.work_kind is RecoveryWorkKind.OUTBOX:
            await self._rebuild_outbox(session, item)
            return
        if item.work_kind is RecoveryWorkKind.DEAD_LETTER and dead_letter.work_kind is not None:
            linked_work_id = dead_letter.work_id
            if not linked_work_id:
                raise RuntimeError("Dead letter is missing its linked canonical work id.")
            if dead_letter.work_kind is RecoveryWorkKind.PIPELINE_STAGE:
                await self._retry_stage(session, item, work_id=linked_work_id)
                return
            if dead_letter.work_kind is RecoveryWorkKind.SYNC_TARGET:
                await self._resync_target(session, item, work_id=linked_work_id)
                return
            if dead_letter.work_kind is RecoveryWorkKind.INGEST_REQUEST:
                await self._reinspect_ingest(session, item, work_id=linked_work_id)
                return
            if dead_letter.work_kind is RecoveryWorkKind.OUTBOX:
                await self._rebuild_outbox(session, item, work_id=linked_work_id)
                return
        raise RuntimeError("Dead letter does not reference canonical work that can be replayed safely.")

    async def _load_linked_dead_letter(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        work: RecoveryWorkRead,
    ) -> PipelineDeadLetter:
        dead_letter_id = work.details.get("dead_letter_id")
        if dead_letter_id is not None:
            dead_letter = await _load_uuid_row(session, PipelineDeadLetter, str(dead_letter_id))
            self._assert_dead_letter_recoverable(dead_letter, item)
            return dead_letter
        if item.work_kind is RecoveryWorkKind.DEAD_LETTER:
            dead_letter = await _load_uuid_row(session, PipelineDeadLetter, item.work_id)
            self._assert_dead_letter_recoverable(dead_letter, item)
            return dead_letter
        dead_letter = await session.scalar(
            select(PipelineDeadLetter)
            .where(
                PipelineDeadLetter.work_kind == item.work_kind,
                PipelineDeadLetter.work_id == item.work_id,
                PipelineDeadLetter.status.in_(
                    (RecoveryDeadLetterStatus.UNRESOLVED, RecoveryDeadLetterStatus.RECOVERY_QUEUED)
                ),
            )
            .order_by(PipelineDeadLetter.created_at.asc())
            .with_for_update()
        )
        if dead_letter is None:
            raise RuntimeError("The linked durable dead letter no longer exists.")
        self._assert_dead_letter_recoverable(dead_letter, item)
        return dead_letter

    async def _queue_backfill(self, session: AsyncSession, item: RecoveryJobItem) -> None:
        job = await _load_uuid_row(session, SourceChannelBackfillJob, item.work_id)
        job.status = SourceChannelBackfillJobStatus.QUEUED
        job.is_retryable = False
        job.next_attempt_at = None
        job.completed_at = None
        job.locked_at = None
        job.lock_owner = None
        job.last_error_code = None
        job.last_error_class = None
        job.last_error_text = None
        item.status = RecoveryJobItemStatus.DISPATCHED
        item.dispatched_at = utcnow()
        item.normalized_reason = None
        item.safe_error_text = None
        await session.commit()

    async def _replay_source_post(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        manager: TelegramSessionManager,
    ) -> None:
        post = await _load_uuid_row(session, SourceChannelPost, item.work_id)
        channel = await session.get(SourceChannel, post.source_channel_id)
        if channel is None:
            raise RuntimeError("Recovery source post references a missing channel.")
        item.status = RecoveryJobItemStatus.DISPATCHED
        item.dispatched_at = utcnow()
        item.normalized_reason = None
        item.safe_error_text = None
        await session.commit()
        await manager.replay_post(channel.platform_id, post.post_id)
        async with self._session_factory() as completion_session:
            completion_item = await completion_session.get(RecoveryJobItem, item.id, with_for_update=True)
            if completion_item is not None:
                self._finish_item(completion_item, status=RecoveryJobItemStatus.SUCCEEDED)
                await self._resolve_linked_dead_letter(completion_session, completion_item, succeeded=True)
                await completion_session.commit()

    async def _reconcile_item(self, session: AsyncSession, item: RecoveryJobItem) -> bool:
        if item.work_kind is RecoveryWorkKind.PIPELINE_STAGE:
            row = await _try_load_uuid_row(session, PipelineStageJournal, item.work_id)
            if row is None or item.dispatch_event_id is None or row.last_event_id != item.dispatch_event_id:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is ContentPipelineStageStatus.SUCCEEDED:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status is ContentPipelineStageStatus.FAILED and (
                not row.is_retryable or row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts
            ):
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.normalized_reason,
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.SYNC_TARGET:
            row = await _try_load_uuid_row(session, MemeFileSyncTargetSnapshot, item.work_id)
            if row is None or item.dispatch_event_id is None or row.last_event_id != item.dispatch_event_id:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is SyncTargetStatus.SYNCED:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif (
                row.status is SyncTargetStatus.FAILED
                and row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts
            ):
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.normalized_reason,
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.INGEST_REQUEST:
            row = await _try_load_uuid_row(session, PipelineIngestRequest, item.work_id)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status in {
                PipelineIngestRequestStatus.MATERIALIZED,
                PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE,
            }:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status in {
                PipelineIngestRequestStatus.FAILED_INVALID_MEDIA,
                PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH,
                PipelineIngestRequestStatus.PUBLISH_FAILED,
            }:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.failure_code,
                    error=row.failure_detail,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.BACKFILL:
            row = await _try_load_uuid_row(session, SourceChannelBackfillJob, item.work_id)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status in {
                SourceChannelBackfillJobStatus.COMPLETED,
                SourceChannelBackfillJobStatus.COMPLETED_WITH_FAILURES,
            }:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status in {SourceChannelBackfillJobStatus.FAILED, SourceChannelBackfillJobStatus.CANCELLED}:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.last_error_code,
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.SOURCE_POST:
            row = await _try_load_uuid_row(session, SourceChannelPost, item.work_id)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is SourceChannelPostStatus.ACCEPTED:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status in {SourceChannelPostStatus.FAILED, SourceChannelPostStatus.UNSUPPORTED}:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.last_error_code,
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.OUTBOX:
            row = await _try_load_uuid_row(session, RabbitMQOutboxMessage, item.work_id)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is RabbitMQOutboxMessageStatus.PUBLISHED:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif (
                row.status is RabbitMQOutboxMessageStatus.FAILED
                and row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts
            ):
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason="outbox_publish_failed",
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.DEAD_LETTER:
            row = await _try_load_uuid_row(session, PipelineDeadLetter, item.work_id)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status in {RecoveryDeadLetterStatus.RESOLVED, RecoveryDeadLetterStatus.ARCHIVED}:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status is RecoveryDeadLetterStatus.RECOVERY_QUEUED and row.work_kind and row.work_id:
                outcome = await self._linked_recovery_outcome(
                    session,
                    kind=row.work_kind,
                    work_id=row.work_id,
                    dispatch_event_id=item.dispatch_event_id,
                )
                if outcome is None:
                    return False
                status, reason, error = outcome
                self._finish_item(item, status=status, reason=reason, error=error)
            else:
                return False
        else:
            return False

        await self._resolve_linked_dead_letter(
            session,
            item,
            succeeded=item.status is RecoveryJobItemStatus.SUCCEEDED,
        )
        return True

    async def _linked_recovery_outcome(
        self,
        session: AsyncSession,
        *,
        kind: RecoveryWorkKind,
        work_id: str,
        dispatch_event_id: uuid.UUID | None,
    ) -> tuple[RecoveryJobItemStatus, str | None, str | None] | None:
        if kind is RecoveryWorkKind.PIPELINE_STAGE:
            row = await _try_load_uuid_row(session, PipelineStageJournal, work_id)
            if row is None or dispatch_event_id is None or row.last_event_id != dispatch_event_id:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is ContentPipelineStageStatus.SUCCEEDED:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status is ContentPipelineStageStatus.FAILED and (
                not row.is_retryable or row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts
            ):
                return RecoveryJobItemStatus.FAILED, row.normalized_reason, row.last_error_text
            return None
        if kind is RecoveryWorkKind.SYNC_TARGET:
            row = await _try_load_uuid_row(session, MemeFileSyncTargetSnapshot, work_id)
            if row is None or dispatch_event_id is None or row.last_event_id != dispatch_event_id:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is SyncTargetStatus.SYNCED:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if (
                row.status is SyncTargetStatus.FAILED
                and row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts
            ):
                return RecoveryJobItemStatus.FAILED, row.normalized_reason, row.last_error_text
            return None
        if kind is RecoveryWorkKind.INGEST_REQUEST:
            row = await _try_load_uuid_row(session, PipelineIngestRequest, work_id)
            if row is None:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status in {
                PipelineIngestRequestStatus.MATERIALIZED,
                PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE,
            }:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status in {
                PipelineIngestRequestStatus.FAILED_INVALID_MEDIA,
                PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH,
                PipelineIngestRequestStatus.PUBLISH_FAILED,
            }:
                return RecoveryJobItemStatus.FAILED, row.failure_code, row.failure_detail
            return None
        if kind is RecoveryWorkKind.BACKFILL:
            row = await _try_load_uuid_row(session, SourceChannelBackfillJob, work_id)
            if row is None:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status in {
                SourceChannelBackfillJobStatus.COMPLETED,
                SourceChannelBackfillJobStatus.COMPLETED_WITH_FAILURES,
            }:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status in {SourceChannelBackfillJobStatus.FAILED, SourceChannelBackfillJobStatus.CANCELLED}:
                return RecoveryJobItemStatus.FAILED, row.last_error_code, row.last_error_text
            return None
        if kind is RecoveryWorkKind.SOURCE_POST:
            row = await _try_load_uuid_row(session, SourceChannelPost, work_id)
            if row is None:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is SourceChannelPostStatus.ACCEPTED:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status in {SourceChannelPostStatus.FAILED, SourceChannelPostStatus.UNSUPPORTED}:
                return RecoveryJobItemStatus.FAILED, row.last_error_code, row.last_error_text
            return None
        if kind is RecoveryWorkKind.OUTBOX:
            row = await _try_load_uuid_row(session, RabbitMQOutboxMessage, work_id)
            if row is None:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is RabbitMQOutboxMessageStatus.PUBLISHED:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if (
                row.status is RabbitMQOutboxMessageStatus.FAILED
                and row.attempt_count >= self._settings.pipeline_broker_retry_max_attempts
            ):
                return RecoveryJobItemStatus.FAILED, "outbox_publish_failed", row.last_error_text
        return None

    async def _resolve_linked_dead_letter(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        *,
        succeeded: bool,
    ) -> None:
        dead_letters = (
            (
                await session.execute(
                    select(PipelineDeadLetter).where(PipelineDeadLetter.recovery_item_id == item.id).with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for dead_letter in dead_letters:
            if succeeded:
                dead_letter.status = RecoveryDeadLetterStatus.RESOLVED
                dead_letter.resolved_at = utcnow()
                dead_letter.resolution_note = "Canonical recovery completed successfully."
            else:
                dead_letter.status = RecoveryDeadLetterStatus.UNRESOLVED
                dead_letter.recovery_item_id = None
                dead_letter.resolution_note = "Canonical recovery failed; the dead letter remains unresolved."

    async def _reconcile_jobs(self, session: AsyncSession) -> None:
        await session.flush()
        job_ids = (
            (
                await session.execute(
                    select(RecoveryJob.id).where(
                        RecoveryJob.status.in_((RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING))
                    )
                )
            )
            .scalars()
            .all()
        )
        for job_id in job_ids:
            job = await session.get(RecoveryJob, job_id, with_for_update=True)
            if job is None:
                continue
            statuses = (
                (await session.execute(select(RecoveryJobItem.status).where(RecoveryJobItem.recovery_job_id == job.id)))
                .scalars()
                .all()
            )
            terminal_count = sum(status in _TERMINAL_ITEM_STATUSES for status in statuses)
            failed_count = sum(
                status not in _SUCCESSFUL_ITEM_STATUSES for status in statuses if status in _TERMINAL_ITEM_STATUSES
            )
            job.completed_count = terminal_count
            job.failed_count = failed_count
            if statuses and terminal_count == len(statuses):
                job.status = RecoveryJobStatus.COMPLETED_WITH_FAILURES if failed_count else RecoveryJobStatus.COMPLETED
                job.completed_at = utcnow()

    async def _mark_item_failed(self, item_id: uuid.UUID, exc: Exception) -> None:
        async with self._session_factory() as session:
            item = await session.get(RecoveryJobItem, item_id, with_for_update=True)
            if item is None:
                return
            self._finish_item(
                item,
                status=RecoveryJobItemStatus.FAILED,
                reason=type(exc).__name__,
                error=str(exc),
            )
            await self._resolve_linked_dead_letter(session, item, succeeded=False)
            await self._reconcile_jobs(session)
            await session.commit()

    @staticmethod
    def _finish_item(
        item: RecoveryJobItem,
        *,
        status: RecoveryJobItemStatus,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        item.status = status
        item.normalized_reason = reason[:128] if reason else None
        item.safe_error_text = _safe_error(error)
        item.finished_at = utcnow()


async def _load_uuid_row(session: AsyncSession, model: type, raw_id: str):
    row = await _try_load_uuid_row(session, model, raw_id, with_for_update=True)
    if row is None:
        raise RuntimeError(f"Recovery target {model.__name__}/{raw_id} no longer exists.")
    return row


async def _try_load_uuid_row(
    session: AsyncSession,
    model: type,
    raw_id: str,
    *,
    with_for_update: bool = False,
):
    try:
        row_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    return await session.get(model, row_id, with_for_update=with_for_update)


def _safe_error(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized[:2000] or None


def _try_parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


__all__ = [
    "RecoveryDispatchResult",
    "RecoveryRuntime",
    "run_recovery_dispatch_batch",
    "run_recovery_reconcile_batch",
    "run_telegram_recovery_loop",
]
