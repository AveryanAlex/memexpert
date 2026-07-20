"""Durable execution and reconciliation for browser-admin recovery jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.crawlers.telegram.client import (
    PipelineTelegramMalformedMessageError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
)
from memexpert.messaging.rabbitmq_outbox import (
    observe_recovery_stage_publication_outcome,
    outbox_message_from_spec,
)
from memexpert.models.base import utcnow
from memexpert.models.content import (
    MemeFile,
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
    MediaGenerationStatus,
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
from memexpert.models.operations import (
    MediaGeneration,
    PipelineDeadLetter,
    RecoveryJob,
    RecoveryJobItem,
    SourceChannelBackfillAttempt,
)
from memexpert.pipeline.constants import (
    PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD,
    PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD,
)
from memexpert.pipeline.events import (
    MEDIA_INSPECT_REQUESTED_EVENT_TYPE,
    PIPELINE_INGEST_REQUEST_AGGREGATE_TYPE,
    build_media_inspect_message_spec,
)
from memexpert.pipeline.replay import PipelineReplayService
from memexpert.schemas.pipeline_ingest import CrawlerIngestOutcome
from memexpert.services.admin_recovery import (
    AdminRecoveryConflictError,
    AdminRecoveryNotFoundError,
    AdminRecoveryOriginalMissingError,
    AdminRecoveryService,
    AdminRecoveryStorageUnavailableError,
)
from memexpert.services.errors import CrawlerSessionNotRunnableError, PipelineReplayNotAllowedError
from memexpert.services.pipeline_reliability import is_historical_admission_open, is_stage_admitted
from memexpert.services.safe_errors import sanitize_operational_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.crawlers.telegram.manager import TelegramSessionManager
    from memexpert.schemas.admin_recovery import RecoveryWorkRead


logger = logging.getLogger(__name__)

_GENERAL_ACTIONS: Final = {
    RecoveryCapability.REINSPECT_INGEST,
    RecoveryCapability.RETRY_STAGE,
    RecoveryCapability.REPLAY_STAGE,
    RecoveryCapability.REGENERATE_DERIVATIVES,
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
    RecoveryJobItemStatus.SKIPPED_DEPENDENCY,
    RecoveryJobItemStatus.CANCELLED,
}
_SUCCESSFUL_ITEM_STATUSES: Final = {
    RecoveryJobItemStatus.SUCCEEDED,
    RecoveryJobItemStatus.CANCELLED,
}
_RECOVERY_DISPATCH_STALE_AFTER: Final = timedelta(minutes=15)
_TERMINAL_SYNC_FAILURE_REASONS: Final = {
    PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD,
    PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD,
}
_TERMINAL_SOURCE_POST_ERRORS: Final = (
    CrawlerSessionNotRunnableError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
)
_NON_STAGE_BUDGET_BASE_KEY: Final = "non_stage_budget_consumed_at_dispatch"


class RecoveryTerminalDispatchError(RuntimeError):
    """A deterministic dispatch defect that must not consume retry budget."""


@dataclass(frozen=True, slots=True)
class RecoveryDispatchResult:
    claimed: int
    dispatched: int
    waiting_capacity: int
    failed: int
    skipped_stale: int
    skipped_dependency: int = 0
    materialized_pages: int = 0
    reclaimed: int = 0


@dataclass(slots=True)
class _MutableDispatchCounts:
    claimed: int = 0
    dispatched: int = 0
    waiting_capacity: int = 0
    failed: int = 0
    skipped_stale: int = 0
    skipped_dependency: int = 0

    def freeze(self) -> RecoveryDispatchResult:
        return RecoveryDispatchResult(
            claimed=self.claimed,
            dispatched=self.dispatched,
            waiting_capacity=self.waiting_capacity,
            failed=self.failed,
            skipped_stale=self.skipped_stale,
            skipped_dependency=self.skipped_dependency,
        )


async def run_recovery_dispatch_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    batch_size: int = 50,
) -> RecoveryDispatchResult:
    """Dispatch a bounded batch of non-Telegram recovery work."""

    runtime = RecoveryRuntime(session_factory=session_factory, settings=settings or get_settings())
    materialized_pages = await runtime.materialize_previews(max_pages=1)
    reclaimed = await runtime.reclaim_stuck_work(batch_size=max(min(batch_size, 100), 1))
    result = await runtime.dispatch_general_batch(batch_size=batch_size)
    await runtime.reconcile(batch_size=max(batch_size * 4, 100))
    return replace(result, reclaimed=reclaimed, materialized_pages=materialized_pages)


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

    async def materialize_previews(self, *, max_pages: int) -> int:
        """Advance a bounded number of durable query-preview pages."""

        materialized = 0
        for _ in range(max(max_pages, 1)):
            async with self._session_factory() as session:
                progressed = await AdminRecoveryService(session).materialize_next_preparing_job()
            if not progressed:
                break
            materialized += 1
        return materialized

    async def dispatch_general_batch(self, *, batch_size: int) -> RecoveryDispatchResult:
        counts = _MutableDispatchCounts()
        counts.skipped_dependency = await self._skip_failed_dependencies(batch_size=max(batch_size, 1))
        claimed_item_ids: set[uuid.UUID] = set()
        for _ in range(max(batch_size, 1)):
            outcome = await self._dispatch_next_general(excluded_item_ids=claimed_item_ids)
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
        counts.skipped_dependency = await self._skip_failed_dependencies(batch_size=max(batch_size, 1))
        claimed_item_ids: set[uuid.UUID] = set()
        for _ in range(max(batch_size, 1)):
            outcome = await self._dispatch_next_telegram(manager, excluded_item_ids=claimed_item_ids)
            if outcome is None:
                break
            counts.claimed += 1
            setattr(counts, outcome, getattr(counts, outcome) + 1)
        return counts.freeze()

    async def reconcile(self, *, batch_size: int) -> int:
        reconciled = await self._skip_failed_dependencies(batch_size=max(batch_size, 1))
        async with self._session_factory() as session:
            candidates = (
                await session.execute(
                    select(RecoveryJobItem.id, RecoveryJobItem.recovery_job_id)
                    .join(RecoveryJob, RecoveryJob.id == RecoveryJobItem.recovery_job_id)
                    .where(
                        RecoveryJob.status.in_(
                            (
                                RecoveryJobStatus.QUEUED,
                                RecoveryJobStatus.RUNNING,
                                RecoveryJobStatus.CANCELLING,
                            )
                        ),
                        RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED,
                    )
                    .order_by(RecoveryJobItem.dispatched_at.asc(), RecoveryJobItem.id.asc())
                    .limit(max(batch_size, 1))
                )
            ).all()
            candidate_item_ids = {item_id for item_id, _job_id in candidates}
            candidate_job_ids = {job_id for _item_id, job_id in candidates}
            locked_jobs = (
                (
                    await session.execute(
                        select(RecoveryJob)
                        .where(
                            RecoveryJob.id.in_(candidate_job_ids),
                            RecoveryJob.status.in_(
                                (
                                    RecoveryJobStatus.QUEUED,
                                    RecoveryJobStatus.RUNNING,
                                    RecoveryJobStatus.CANCELLING,
                                )
                            ),
                        )
                        .order_by(RecoveryJob.id.asc())
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            locked_job_ids = {job.id for job in locked_jobs}
            items = (
                (
                    await session.execute(
                        select(RecoveryJobItem)
                        .where(
                            RecoveryJobItem.id.in_(candidate_item_ids),
                            RecoveryJobItem.recovery_job_id.in_(locked_job_ids),
                            RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED,
                        )
                        .order_by(RecoveryJobItem.dispatched_at.asc(), RecoveryJobItem.id.asc())
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for item in items:
                terminal = await self._reconcile_item(session, item)
                reconciled += int(terminal)
            await self._reconcile_jobs(session, job_ids=locked_job_ids)
            await session.commit()
        async with self._session_factory() as session:
            await self._reconcile_jobs(session)
            await session.commit()
        return reconciled

    async def _skip_failed_dependencies(self, *, batch_size: int) -> int:
        parent = aliased(RecoveryJobItem)
        async with self._session_factory() as session:
            candidates = (
                await session.execute(
                    select(RecoveryJobItem.id, RecoveryJobItem.recovery_job_id)
                    .join(parent, parent.id == RecoveryJobItem.parent_item_id)
                    .join(RecoveryJob, RecoveryJob.id == RecoveryJobItem.recovery_job_id)
                    .where(
                        RecoveryJob.status.in_(
                            (
                                RecoveryJobStatus.QUEUED,
                                RecoveryJobStatus.RUNNING,
                                RecoveryJobStatus.CANCELLING,
                            )
                        ),
                        RecoveryJobItem.status == RecoveryJobItemStatus.WAITING_DEPENDENCY,
                        parent.status.in_(
                            (
                                RecoveryJobItemStatus.FAILED,
                                RecoveryJobItemStatus.SKIPPED_STALE,
                                RecoveryJobItemStatus.SKIPPED_DEPENDENCY,
                                RecoveryJobItemStatus.CANCELLED,
                            )
                        ),
                    )
                    .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
                    .limit(max(batch_size, 1))
                )
            ).all()
            child_ids = {item_id for item_id, _job_id in candidates}
            candidate_job_ids = {job_id for _item_id, job_id in candidates}
            locked_jobs = (
                (
                    await session.execute(
                        select(RecoveryJob)
                        .where(RecoveryJob.id.in_(candidate_job_ids))
                        .order_by(RecoveryJob.id.asc())
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            locked_job_ids = {job.id for job in locked_jobs}
            children = (
                (
                    await session.execute(
                        select(RecoveryJobItem)
                        .join(parent, parent.id == RecoveryJobItem.parent_item_id)
                        .where(
                            RecoveryJobItem.id.in_(child_ids),
                            RecoveryJobItem.recovery_job_id.in_(locked_job_ids),
                            RecoveryJobItem.status == RecoveryJobItemStatus.WAITING_DEPENDENCY,
                            parent.status.in_(
                                (
                                    RecoveryJobItemStatus.FAILED,
                                    RecoveryJobItemStatus.SKIPPED_STALE,
                                    RecoveryJobItemStatus.SKIPPED_DEPENDENCY,
                                    RecoveryJobItemStatus.CANCELLED,
                                )
                            ),
                        )
                        .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
                        .with_for_update(of=RecoveryJobItem, skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for child in children:
                self._finish_item(
                    child,
                    status=RecoveryJobItemStatus.SKIPPED_DEPENDENCY,
                    reason="parent_step_failed",
                    error="A prerequisite Replay & Repair step did not succeed.",
                )
            if children:
                await self._reconcile_jobs(session, job_ids=locked_job_ids)
                await session.commit()
            return len(children)

    async def _dispatch_next_general(self, *, excluded_item_ids: set[uuid.UUID]) -> str | None:
        async with self._session_factory() as session:
            item = await self._claim_next_item(
                session,
                actions=_GENERAL_ACTIONS,
                telegram=False,
                excluded_item_ids=excluded_item_ids,
            )
            if item is None:
                return None
            excluded_item_ids.add(item.id)
            try:
                work = await self._validate_claimed_item(session, item)
            except AdminRecoveryStorageUnavailableError as exc:
                item.status = RecoveryJobItemStatus.WAITING_CAPACITY
                item.normalized_reason = "original_storage_unavailable"
                item.safe_error_text = sanitize_operational_error(exc)
                await session.commit()
                return "waiting_capacity"
            except AdminRecoveryOriginalMissingError as exc:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="missing_original",
                    error=str(exc),
                )
                await session.commit()
                return "skipped_stale"
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
                await self._record_dispatch_failure(
                    item.id,
                    exc,
                    retryable=(
                        not isinstance(
                            exc,
                            (
                                AdminRecoveryConflictError,
                                AdminRecoveryNotFoundError,
                                PipelineReplayNotAllowedError,
                                RecoveryTerminalDispatchError,
                            ),
                        )
                        and not (
                            item.work_kind is RecoveryWorkKind.SOURCE_POST
                            and isinstance(exc, _TERMINAL_SOURCE_POST_ERRORS)
                        )
                    ),
                )
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

    async def _dispatch_next_telegram(
        self,
        manager: TelegramSessionManager,
        *,
        excluded_item_ids: set[uuid.UUID],
    ) -> str | None:
        async with self._session_factory() as session:
            item = await self._claim_next_item(
                session,
                actions=_TELEGRAM_ACTIONS,
                telegram=True,
                excluded_item_ids=excluded_item_ids,
            )
            if item is None:
                return None
            excluded_item_ids.add(item.id)
            try:
                work = await self._validate_claimed_item(session, item)
            except AdminRecoveryStorageUnavailableError as exc:
                item.status = RecoveryJobItemStatus.WAITING_CAPACITY
                item.normalized_reason = "original_storage_unavailable"
                item.safe_error_text = sanitize_operational_error(exc)
                await session.commit()
                return "waiting_capacity"
            except AdminRecoveryOriginalMissingError as exc:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="missing_original",
                    error=str(exc),
                )
                await session.commit()
                return "skipped_stale"
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
                await self._record_dispatch_failure(
                    item.id,
                    exc,
                    retryable=not isinstance(
                        exc,
                        (
                            AdminRecoveryConflictError,
                            AdminRecoveryNotFoundError,
                            PipelineReplayNotAllowedError,
                            RecoveryTerminalDispatchError,
                        ),
                    ),
                )
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
        excluded_item_ids: set[uuid.UUID],
    ) -> RecoveryJobItem | None:
        now = utcnow()
        stale_before = now - _RECOVERY_DISPATCH_STALE_AFTER
        kind_predicate = (
            RecoveryJobItem.work_kind.in_((RecoveryWorkKind.BACKFILL, RecoveryWorkKind.SOURCE_POST))
            if telegram
            else RecoveryJobItem.work_kind.not_in((RecoveryWorkKind.BACKFILL, RecoveryWorkKind.SOURCE_POST))
        )
        parent = aliased(RecoveryJobItem)
        dependency_satisfied = or_(
            RecoveryJobItem.parent_item_id.is_(None),
            parent.status == RecoveryJobItemStatus.SUCCEEDED,
        )
        item_eligible = (
            (
                RecoveryJobItem.status.in_(
                    (
                        RecoveryJobItemStatus.QUEUED,
                        RecoveryJobItemStatus.WAITING_DEPENDENCY,
                        RecoveryJobItemStatus.WAITING_CAPACITY,
                    )
                )
                & dependency_satisfied
            )
            | (
                (RecoveryJobItem.status == RecoveryJobItemStatus.DISPATCHED)
                & (RecoveryJobItem.dispatched_at < stale_before)
                & RecoveryJobItem.dispatch_event_id.is_(None)
                & (RecoveryJobItem.work_kind != RecoveryWorkKind.BACKFILL)
            )
        )
        job = await session.scalar(
            select(RecoveryJob)
            .join(RecoveryJobItem, RecoveryJobItem.recovery_job_id == RecoveryJob.id)
            .outerjoin(parent, parent.id == RecoveryJobItem.parent_item_id)
            .where(
                RecoveryJob.status.in_((RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING)),
                RecoveryJobItem.action.in_(actions),
                RecoveryJobItem.id.not_in(excluded_item_ids),
                kind_predicate,
                item_eligible,
            )
            .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
            .with_for_update(of=RecoveryJob, skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        item = await session.scalar(
            select(RecoveryJobItem)
            .join(RecoveryJob, RecoveryJob.id == RecoveryJobItem.recovery_job_id)
            .outerjoin(parent, parent.id == RecoveryJobItem.parent_item_id)
            .where(
                RecoveryJob.id == job.id,
                RecoveryJob.status.in_((RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING)),
                RecoveryJobItem.action.in_(actions),
                RecoveryJobItem.id.not_in(excluded_item_ids),
                kind_predicate,
                item_eligible,
            )
            .order_by(RecoveryJobItem.created_at.asc(), RecoveryJobItem.id.asc())
            .with_for_update(of=RecoveryJobItem, skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        if job.status is RecoveryJobStatus.QUEUED:
            job.status = RecoveryJobStatus.RUNNING
        return item

    async def _validate_claimed_item(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
    ) -> RecoveryWorkRead:
        admin_service = AdminRecoveryService(session)
        await admin_service.verify_recovery_item_source_object(item)
        await self._lock_canonical_work(session, item)
        candidate_actions = {
            RecoveryCapability.REPLAY_STAGE,
            RecoveryCapability.REGENERATE_DERIVATIVES,
            RecoveryCapability.RECOVER_DEAD_LETTER,
        }
        if item.action in candidate_actions:
            created_planned_stage = (
                await self._ensure_planned_stage_row(session, item)
                if item.action
                in {
                    RecoveryCapability.REPLAY_STAGE,
                    RecoveryCapability.REGENERATE_DERIVATIVES,
                }
                else False
            )
            candidate = await admin_service.get_candidate(
                item.work_kind,
                item.work_id,
                ignore_recovery_item_id=item.id,
                verify_source_object=False,
            )
            action = next(
                (entry for entry in candidate.actions if entry.capability is item.action),
                None,
            )
            if action is None or not action.available:
                blocked = action.blocked_prerequisites if action is not None else []
                raise AdminRecoveryConflictError(
                    "; ".join(blocked) or "The scheduled Replay & Repair action is no longer valid."
                )
            if "terminal_override" in action.required_acknowledgements and not item.terminal_override_acknowledged:
                raise AdminRecoveryConflictError(
                    "The scheduled terminal replay is missing its audited acknowledgement."
                )
            work = candidate.work
        else:
            created_planned_stage = False
            work = await admin_service.get_work(item.work_kind, item.work_id)
        if item.action is RecoveryCapability.RECOVER_DEAD_LETTER:
            await self._lock_recovery_dead_letter(session, item, work)
            work = await admin_service.get_work(item.work_kind, item.work_id)
        expected_missing_stage = item.expected_version.startswith("missing:")
        if expected_missing_stage and not created_planned_stage:
            raise AdminRecoveryConflictError(
                "A dependent stage appeared after preview; create a fresh Replay & Repair job."
            )
        if work.version != item.expected_version and not created_planned_stage:
            raise AdminRecoveryConflictError("Recovery target changed after it was scheduled.")
        if created_planned_stage:
            item.expected_version = work.version
        if (
            item.action
            not in {
                RecoveryCapability.REPLAY_STAGE,
                RecoveryCapability.REGENERATE_DERIVATIVES,
            }
            and item.action not in work.capabilities
        ):
            raise AdminRecoveryConflictError(work.blocked_reason or "The scheduled recovery action is no longer valid.")
        item.canonical_version = work.version
        return work

    async def _ensure_planned_stage_row(self, session: AsyncSession, item: RecoveryJobItem) -> bool:
        if item.meme_file_id is None or item.stage is None:
            raise AdminRecoveryConflictError("Replay stage metadata is incomplete.")
        if _try_parse_uuid(item.work_id) is not None:
            return False
        row = await session.scalar(
            select(PipelineStageJournal)
            .where(
                PipelineStageJournal.meme_file_id == item.meme_file_id,
                PipelineStageJournal.stage == item.stage,
            )
            .with_for_update()
        )
        created = row is None
        if created:
            row = PipelineStageJournal(
                id=uuid.uuid7(),
                meme_file_id=item.meme_file_id,
                stage=item.stage,
                status=ContentPipelineStageStatus.FAILED,
                attempt_count=0,
                normalized_reason="orchestrated_stage_not_materialized",
                is_retryable=True,
            )
            session.add(row)
            await session.flush()
        item.work_kind = RecoveryWorkKind.PIPELINE_STAGE
        item.work_id = str(row.id)
        return created

    async def _lock_canonical_work(self, session: AsyncSession, item: RecoveryJobItem) -> None:
        if item.action in {
            RecoveryCapability.REPLAY_STAGE,
            RecoveryCapability.REGENERATE_DERIVATIVES,
        }:
            if item.meme_file_id is None:
                raise AdminRecoveryConflictError("Replay stage is missing its canonical file id.")
            meme_file = await session.get(MemeFile, item.meme_file_id, with_for_update=True)
            if meme_file is None:
                raise AdminRecoveryNotFoundError(f"Pipeline file {item.meme_file_id} does not exist.")
            row_id = _try_parse_uuid(item.work_id)
            if row_id is not None:
                model = (
                    MemeFileSyncTargetSnapshot
                    if item.work_kind is RecoveryWorkKind.SYNC_TARGET
                    else PipelineStageJournal
                )
                row = await session.get(model, row_id, with_for_update=True)
                if row is None:
                    raise AdminRecoveryNotFoundError(
                        f"Recovery work {item.work_kind.value}/{item.work_id} does not exist."
                    )
            return
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
            raise AdminRecoveryNotFoundError(f"Recovery work {item.work_kind.value}/{item.work_id} does not exist.")

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
        if dead_letter.status is RecoveryDeadLetterStatus.RECOVERY_QUEUED and dead_letter.recovery_item_id == item.id:
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
            RecoveryCapability.REPLAY_STAGE,
            RecoveryCapability.REGENERATE_DERIVATIVES,
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
        if item.action in {
            RecoveryCapability.REPLAY_STAGE,
            RecoveryCapability.REGENERATE_DERIVATIVES,
        }:
            await self._replay_admin_stage(session, item)
            return
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
        raise RecoveryTerminalDispatchError(f"Unsupported general recovery action {item.action.value!r}.")

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
            raise RecoveryTerminalDispatchError("Telegram dead letter is not linked to replayable Telegram work.")
        if item.action is RecoveryCapability.RESUME_BACKFILL:
            await self._queue_backfill(session, item)
            return
        if item.action is RecoveryCapability.REPLAY_SOURCE_POST:
            await self._replay_source_post(session, item, manager)
            return
        raise RecoveryTerminalDispatchError(f"Unsupported Telegram recovery action {item.action.value!r}.")

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

    async def _replay_admin_stage(self, session: AsyncSession, item: RecoveryJobItem) -> None:
        if item.meme_file_id is None or item.stage is None:
            raise RecoveryTerminalDispatchError("Replay stage item is missing canonical stage metadata.")
        await PipelineReplayService(session, settings=self._settings).replay_admin_stage(
            item.meme_file_id,
            stage=item.stage,
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
        item.attempt_budget_start = 1
        _mark_non_stage_budget_dispatch(item)
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
        if item.attempt_budget_start is None:
            item.attempt_budget_start = max(outbox.attempt_count + 1, 1)
        _mark_non_stage_budget_dispatch(item)
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
        if item.work_kind in {RecoveryWorkKind.PIPELINE_STAGE, RecoveryWorkKind.SYNC_TARGET}:
            await self._replay_admin_stage(session, item)
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
                raise RecoveryTerminalDispatchError("Dead letter is missing its linked canonical work id.")
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
        raise RecoveryTerminalDispatchError(
            "Dead letter does not reference canonical work that can be replayed safely."
        )

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
            raise RecoveryTerminalDispatchError("The linked durable dead letter no longer exists.")
        self._assert_dead_letter_recoverable(dead_letter, item)
        return dead_letter

    async def _queue_backfill(self, session: AsyncSession, item: RecoveryJobItem) -> None:
        job = await _load_uuid_row(session, SourceChannelBackfillJob, item.work_id)
        if item.attempt_budget_start is None:
            item.attempt_budget_start = max(job.attempt_count + 1, 1)
        _mark_non_stage_budget_dispatch(item)
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
            raise RecoveryTerminalDispatchError("Recovery source post references a missing channel.")
        if item.attempt_budget_start is None:
            item.attempt_budget_start = max(post.attempt_count + 1, 1)
        _mark_non_stage_budget_dispatch(item)
        item.status = RecoveryJobItemStatus.DISPATCHED
        item.dispatched_at = utcnow()
        item.normalized_reason = None
        item.safe_error_text = None
        await session.commit()
        result = await manager.replay_post(channel.platform_id, post.post_id)
        if result.outcome is CrawlerIngestOutcome.SKIPPED_PAUSED_CHANNEL:
            raise RuntimeError("The source channel was paused while the recovery replay was running.")
        if result.outcome in {
            CrawlerIngestOutcome.SKIPPED_UNSUPPORTED_MEDIA,
            CrawlerIngestOutcome.REJECTED_MALFORMED,
        }:
            raise RecoveryTerminalDispatchError("The source post is not replayable media.")
        async with self._session_factory() as completion_session:
            completion_job = await completion_session.get(
                RecoveryJob,
                item.recovery_job_id,
                with_for_update=True,
            )
            if completion_job is None:
                return
            completion_item = await completion_session.get(RecoveryJobItem, item.id, with_for_update=True)
            if completion_item is not None and completion_item.status is RecoveryJobItemStatus.DISPATCHED:
                self._finish_item(completion_item, status=RecoveryJobItemStatus.SUCCEEDED)
                await self._resolve_linked_dead_letter(completion_session, completion_item, succeeded=True)
                await self._reconcile_jobs(completion_session, job_ids={completion_job.id})
                await completion_session.commit()

    async def _dispatch_outbox_for_item(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
    ) -> RabbitMQOutboxMessage | None:
        if item.dispatch_event_id is None:
            return None
        return await session.scalar(
            select(RabbitMQOutboxMessage)
            .where(RabbitMQOutboxMessage.message_id == str(item.dispatch_event_id))
            .with_for_update()
        )

    @staticmethod
    def _observe_outbox_retry_budget(
        item: RecoveryJobItem,
        outbox: RabbitMQOutboxMessage,
    ) -> None:
        if item.stage is not None:
            observe_recovery_stage_publication_outcome(item, outbox)
            return
        if _has_non_stage_budget_base(item):
            if item.attempt_budget_start is None:
                item.attempt_budget_start = max(outbox.attempt_count, 1)
            attempt_budget_start = item.attempt_budget_start
            assert attempt_budget_start is not None
            budget_base = _non_stage_budget_base(item)
        else:
            attempt_budget_start = 1
            budget_base = 0
        if outbox.status is RabbitMQOutboxMessageStatus.FAILED:
            delivery_failures = max(outbox.attempt_count - attempt_budget_start + 1, 0)
        elif outbox.status is RabbitMQOutboxMessageStatus.PUBLISHED:
            delivery_failures = max(outbox.attempt_count - attempt_budget_start, 0)
        else:
            return
        item.retryable_failures_consumed = max(
            item.retryable_failures_consumed,
            budget_base + delivery_failures,
        )

    async def _reconcile_exhausted_stage_dispatch_outbox(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
    ) -> bool:
        """Release an unpublished stage reservation after its broker budget expires.

        A broker may have accepted a message even when the publisher observed an
        error.  Only restore a journal row while the same event is still the
        untouched PENDING reservation; PROCESSING, FAILED, or SUCCEEDED state is
        canonical worker evidence and must win.
        """

        if item.meme_file_id is None or item.stage is None or item.dispatch_event_id is None:
            return False
        outbox = await self._dispatch_outbox_for_item(session, item)
        if outbox is None:
            return False
        self._observe_outbox_retry_budget(item, outbox)
        if (
            outbox.status is not RabbitMQOutboxMessageStatus.FAILED
            or item.retryable_failures_consumed < item.retry_limit
        ):
            return False

        stage_row = await session.scalar(
            select(PipelineStageJournal)
            .where(
                PipelineStageJournal.meme_file_id == item.meme_file_id,
                PipelineStageJournal.stage == item.stage,
            )
            .with_for_update()
        )
        if (
            stage_row is None
            or stage_row.last_event_id != item.dispatch_event_id
            or stage_row.status is not ContentPipelineStageStatus.PENDING
        ):
            return False

        if not _restore_stage_snapshot(stage_row, item.previous_stage_state):
            # Legacy recovery rows may not have a snapshot.  They still must
            # not leave an undispatchable PENDING reservation behind.
            stage_row.status = ContentPipelineStageStatus.FAILED
            stage_row.normalized_reason = "outbox_publish_failed"
            stage_row.last_error_text = sanitize_operational_error(outbox.last_error_text)
            stage_row.is_retryable = True
            stage_row.retry_after = None
            stage_row.started_at = None
            stage_row.finished_at = utcnow()

        self._finish_item(
            item,
            status=RecoveryJobItemStatus.FAILED,
            reason="outbox_publish_failed",
            error=outbox.last_error_text,
        )
        await self._resolve_linked_dead_letter(session, item, succeeded=False)
        return True

    async def _observe_backfill_retry_budget(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
    ) -> bool:
        retryable_failures = int(
            await session.scalar(
                select(func.count(SourceChannelBackfillAttempt.id)).where(
                    SourceChannelBackfillAttempt.recovery_item_id == item.id,
                    SourceChannelBackfillAttempt.finished_at.is_not(None),
                    SourceChannelBackfillAttempt.is_retryable.is_(True),
                )
            )
            or 0
        )
        item.retryable_failures_consumed = max(
            item.retryable_failures_consumed,
            _non_stage_budget_base(item) + retryable_failures,
        )
        latest_finished_retryable = await session.scalar(
            select(SourceChannelBackfillAttempt.is_retryable)
            .where(
                SourceChannelBackfillAttempt.recovery_item_id == item.id,
                SourceChannelBackfillAttempt.finished_at.is_not(None),
            )
            .order_by(
                SourceChannelBackfillAttempt.attempt_number.desc(),
                SourceChannelBackfillAttempt.id.desc(),
            )
            .limit(1)
        )
        return latest_finished_retryable is False

    async def _requeue_async_failure(
        self,
        session: AsyncSession,
        item: RecoveryJobItem,
        *,
        reason: str | None,
        error: str | None,
        retryable: bool,
        consume_failure: bool = True,
    ) -> bool:
        """Version-fence one observed canonical failure before another dispatch."""

        try:
            work = await AdminRecoveryService(session).get_work(item.work_kind, item.work_id)
        except AdminRecoveryNotFoundError as exc:
            self._finish_item(
                item,
                status=RecoveryJobItemStatus.SKIPPED_STALE,
                reason="canonical_state_changed",
                error=str(exc),
            )
            return True
        if retryable and consume_failure and item.canonical_version != work.version:
            item.retryable_failures_consumed += 1
        item.canonical_version = work.version
        job = await session.get(RecoveryJob, item.recovery_job_id)
        can_retry = (
            retryable
            and item.retryable_failures_consumed < item.retry_limit
            and job is not None
            and job.status in {RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING}
        )
        if not can_retry:
            self._finish_item(
                item,
                status=RecoveryJobItemStatus.FAILED,
                reason=reason,
                error=error,
            )
            return True
        item.status = RecoveryJobItemStatus.QUEUED
        item.expected_version = work.version
        item.dispatch_event_id = None
        item.dispatched_at = None
        item.finished_at = None
        item.attempt_budget_start = None
        item.normalized_reason = reason[:128] if reason else None
        item.safe_error_text = sanitize_operational_error(error)
        return False

    async def _reconcile_item(self, session: AsyncSession, item: RecoveryJobItem) -> bool:
        if await self._reconcile_exhausted_stage_dispatch_outbox(session, item):
            return True
        if (
            item.work_kind is RecoveryWorkKind.INGEST_REQUEST
            and item.retryable_failures_consumed >= item.retry_limit
            and item.normalized_reason is not None
        ):
            self._finish_item(
                item,
                status=RecoveryJobItemStatus.FAILED,
                reason=item.normalized_reason,
                error=item.safe_error_text,
            )
            await self._resolve_linked_dead_letter(session, item, succeeded=False)
            return True
        if item.action is RecoveryCapability.REGENERATE_DERIVATIVES:
            generation = await session.scalar(
                select(MediaGeneration)
                .where(MediaGeneration.recovery_item_id == item.id)
                .order_by(MediaGeneration.created_at.desc())
                .limit(1)
            )
            if generation is None or generation.status in {
                MediaGenerationStatus.GENERATING,
                MediaGenerationStatus.VERIFIED,
                MediaGenerationStatus.UPLOADED,
            }:
                return False
            if generation.status in {MediaGenerationStatus.ACTIVE, MediaGenerationStatus.SUPERSEDED}:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif generation.status in {MediaGenerationStatus.FAILED, MediaGenerationStatus.STALE}:
                if item.retryable_failures_consumed < item.retry_limit:
                    return False
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=generation.safe_failure_reason,
                    error=generation.safe_failure_text,
                )
            else:
                return False
        elif (
            item.action is RecoveryCapability.REPLAY_STAGE and item.meme_file_id is not None and item.stage is not None
        ):
            row = await session.scalar(
                select(PipelineStageJournal).where(
                    PipelineStageJournal.meme_file_id == item.meme_file_id,
                    PipelineStageJournal.stage == item.stage,
                )
            )
            if row is None or item.dispatch_event_id is None or row.last_event_id != item.dispatch_event_id:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is ContentPipelineStageStatus.SUCCEEDED:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status is ContentPipelineStageStatus.FAILED and (
                not row.is_retryable or item.retryable_failures_consumed >= item.retry_limit
            ):
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.normalized_reason,
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.PIPELINE_STAGE:
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
                not row.is_retryable or item.retryable_failures_consumed >= item.retry_limit
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
            elif row.status is SyncTargetStatus.FAILED and (
                row.normalized_reason in _TERMINAL_SYNC_FAILURE_REASONS
                or item.retryable_failures_consumed >= item.retry_limit
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
            row = await _try_load_uuid_row(session, PipelineIngestRequest, item.work_id, with_for_update=True)
            dispatch_outbox = await self._dispatch_outbox_for_item(session, item)
            if dispatch_outbox is not None:
                self._observe_outbox_retry_budget(item, dispatch_outbox)
                if dispatch_outbox.status is RabbitMQOutboxMessageStatus.FAILED:
                    if item.retryable_failures_consumed >= item.retry_limit:
                        self._finish_item(
                            item,
                            status=RecoveryJobItemStatus.FAILED,
                            reason="outbox_publish_failed",
                            error=dispatch_outbox.last_error_text,
                        )
                        await self._resolve_linked_dead_letter(session, item, succeeded=False)
                        return True
                    else:
                        return False
                elif dispatch_outbox.status in {
                    RabbitMQOutboxMessageStatus.PENDING,
                    RabbitMQOutboxMessageStatus.PUBLISHING,
                }:
                    return False
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
            }:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.failure_code,
                    error=row.failure_detail,
                )
            elif row.status is PipelineIngestRequestStatus.PUBLISH_FAILED:
                terminal = await self._requeue_async_failure(
                    session,
                    item,
                    reason=row.failure_code,
                    error=row.failure_detail,
                    retryable=True,
                )
                if not terminal:
                    return False
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.BACKFILL:
            row = await _try_load_uuid_row(session, SourceChannelBackfillJob, item.work_id, with_for_update=True)
            terminal_attempt = await self._observe_backfill_retry_budget(session, item)
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
            elif row.status is SourceChannelBackfillJobStatus.FAILED:
                if row.is_retryable and not terminal_attempt and item.retryable_failures_consumed < item.retry_limit:
                    terminal = await self._requeue_async_failure(
                        session,
                        item,
                        reason=row.last_error_code,
                        error=row.last_error_text,
                        retryable=True,
                        consume_failure=False,
                    )
                    if not terminal:
                        return False
                else:
                    self._finish_item(
                        item,
                        status=RecoveryJobItemStatus.FAILED,
                        reason=row.last_error_code,
                        error=row.last_error_text,
                    )
            elif row.status is SourceChannelBackfillJobStatus.CANCELLED:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=row.last_error_code,
                    error=row.last_error_text,
                )
            else:
                return False
        elif item.work_kind is RecoveryWorkKind.SOURCE_POST:
            row = await _try_load_uuid_row(session, SourceChannelPost, item.work_id, with_for_update=True)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is SourceChannelPostStatus.ACCEPTED:
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status is SourceChannelPostStatus.FAILED and row.is_retryable:
                terminal = await self._requeue_async_failure(
                    session,
                    item,
                    reason=row.last_error_code,
                    error=row.last_error_text,
                    retryable=True,
                )
                if not terminal:
                    return False
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
            row = await _try_load_uuid_row(session, RabbitMQOutboxMessage, item.work_id, with_for_update=True)
            if row is None:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.SKIPPED_STALE,
                    reason="canonical_state_changed",
                )
            elif row.status is RabbitMQOutboxMessageStatus.PUBLISHED:
                self._observe_outbox_retry_budget(item, row)
                self._finish_item(item, status=RecoveryJobItemStatus.SUCCEEDED)
            elif row.status is RabbitMQOutboxMessageStatus.FAILED:
                self._observe_outbox_retry_budget(item, row)
                if item.retryable_failures_consumed >= item.retry_limit:
                    self._finish_item(
                        item,
                        status=RecoveryJobItemStatus.FAILED,
                        reason="outbox_publish_failed",
                        error=row.last_error_text,
                    )
                else:
                    return False
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
                    item=item,
                    kind=row.work_kind,
                    work_id=row.work_id,
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
        item: RecoveryJobItem,
        kind: RecoveryWorkKind,
        work_id: str,
    ) -> tuple[RecoveryJobItemStatus, str | None, str | None] | None:
        dispatch_event_id = item.dispatch_event_id
        if kind is RecoveryWorkKind.PIPELINE_STAGE:
            row = await _try_load_uuid_row(session, PipelineStageJournal, work_id)
            if row is None or dispatch_event_id is None or row.last_event_id != dispatch_event_id:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is ContentPipelineStageStatus.SUCCEEDED:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status is ContentPipelineStageStatus.FAILED and (
                not row.is_retryable or item.retryable_failures_consumed >= item.retry_limit
            ):
                return RecoveryJobItemStatus.FAILED, row.normalized_reason, row.last_error_text
            return None
        if kind is RecoveryWorkKind.SYNC_TARGET:
            row = await _try_load_uuid_row(session, MemeFileSyncTargetSnapshot, work_id)
            if row is None or dispatch_event_id is None or row.last_event_id != dispatch_event_id:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is SyncTargetStatus.SYNCED:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status is SyncTargetStatus.FAILED and (
                row.normalized_reason in _TERMINAL_SYNC_FAILURE_REASONS
                or item.retryable_failures_consumed >= item.retry_limit
            ):
                return RecoveryJobItemStatus.FAILED, row.normalized_reason, row.last_error_text
            return None
        if kind is RecoveryWorkKind.INGEST_REQUEST:
            row = await _try_load_uuid_row(session, PipelineIngestRequest, work_id, with_for_update=True)
            dispatch_outbox = await self._dispatch_outbox_for_item(session, item)
            if dispatch_outbox is not None:
                self._observe_outbox_retry_budget(item, dispatch_outbox)
                if (
                    dispatch_outbox.status is RabbitMQOutboxMessageStatus.FAILED
                    and item.retryable_failures_consumed >= item.retry_limit
                ):
                    return RecoveryJobItemStatus.FAILED, "outbox_publish_failed", dispatch_outbox.last_error_text
                if dispatch_outbox.status is not RabbitMQOutboxMessageStatus.PUBLISHED:
                    return None
            if item.retryable_failures_consumed >= item.retry_limit and item.normalized_reason is not None:
                return RecoveryJobItemStatus.FAILED, item.normalized_reason, item.safe_error_text
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
            row = await _try_load_uuid_row(session, SourceChannelBackfillJob, work_id, with_for_update=True)
            terminal_attempt = await self._observe_backfill_retry_budget(session, item)
            if row is None:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status in {
                SourceChannelBackfillJobStatus.COMPLETED,
                SourceChannelBackfillJobStatus.COMPLETED_WITH_FAILURES,
            }:
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status is SourceChannelBackfillJobStatus.FAILED and (
                terminal_attempt or item.retryable_failures_consumed >= item.retry_limit
            ):
                return RecoveryJobItemStatus.FAILED, row.last_error_code, row.last_error_text
            if row.status is SourceChannelBackfillJobStatus.CANCELLED:
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
            row = await _try_load_uuid_row(session, RabbitMQOutboxMessage, work_id, with_for_update=True)
            if row is None:
                return RecoveryJobItemStatus.SKIPPED_STALE, "canonical_state_changed", None
            if row.status is RabbitMQOutboxMessageStatus.PUBLISHED:
                self._observe_outbox_retry_budget(item, row)
                return RecoveryJobItemStatus.SUCCEEDED, None, None
            if row.status is RabbitMQOutboxMessageStatus.FAILED:
                self._observe_outbox_retry_budget(item, row)
                if item.retryable_failures_consumed >= item.retry_limit:
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

    async def _reconcile_jobs(
        self,
        session: AsyncSession,
        *,
        job_ids: set[uuid.UUID] | None = None,
    ) -> None:
        await session.flush()
        job_id_stmt = select(RecoveryJob.id).where(
            RecoveryJob.status.in_(
                (
                    RecoveryJobStatus.QUEUED,
                    RecoveryJobStatus.RUNNING,
                    RecoveryJobStatus.CANCELLING,
                )
            )
        )
        if job_ids is not None:
            job_id_stmt = job_id_stmt.where(RecoveryJob.id.in_(job_ids))
        active_job_ids = (
            (
                await session.execute(job_id_stmt.order_by(RecoveryJob.id.asc()))
            )
            .scalars()
            .all()
        )
        jobs = (
            (
                await session.execute(
                    select(RecoveryJob)
                    .where(RecoveryJob.id.in_(active_job_ids))
                    .order_by(RecoveryJob.id.asc())
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            items = (
                (
                    await session.execute(
                        select(RecoveryJobItem)
                        .where(RecoveryJobItem.recovery_job_id == job.id)
                        .order_by(RecoveryJobItem.id.asc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            statuses = [item.status for item in items]
            terminal_count = sum(status in _TERMINAL_ITEM_STATUSES for status in statuses)
            unsuccessful_count = sum(
                status not in _SUCCESSFUL_ITEM_STATUSES for status in statuses if status in _TERMINAL_ITEM_STATUSES
            )
            job.completed_count = terminal_count
            counts = {status: statuses.count(status) for status in RecoveryJobItemStatus}
            job.failed_count = counts[RecoveryJobItemStatus.FAILED]
            job.queued_count = counts[RecoveryJobItemStatus.QUEUED]
            job.waiting_count = (
                counts[RecoveryJobItemStatus.WAITING_DEPENDENCY] + counts[RecoveryJobItemStatus.WAITING_CAPACITY]
            )
            job.dispatched_count = counts[RecoveryJobItemStatus.DISPATCHED]
            job.succeeded_count = counts[RecoveryJobItemStatus.SUCCEEDED]
            job.stale_count = counts[RecoveryJobItemStatus.SKIPPED_STALE]
            job.skipped_count = counts[RecoveryJobItemStatus.SKIPPED_DEPENDENCY]
            job.cancelled_count = counts[RecoveryJobItemStatus.CANCELLED]
            if statuses and terminal_count == len(statuses):
                if job.status is RecoveryJobStatus.CANCELLING:
                    job.status = RecoveryJobStatus.CANCELLED
                else:
                    job.status = (
                        RecoveryJobStatus.COMPLETED_WITH_FAILURES
                        if unsuccessful_count
                        else RecoveryJobStatus.COMPLETED
                    )
                job.completed_at = utcnow()

    async def _record_dispatch_failure(
        self,
        item_id: uuid.UUID,
        exc: Exception,
        *,
        retryable: bool,
    ) -> None:
        async with self._session_factory() as session:
            job_id = await session.scalar(
                select(RecoveryJobItem.recovery_job_id).where(RecoveryJobItem.id == item_id)
            )
            if job_id is None:
                return
            job = await session.get(RecoveryJob, job_id, with_for_update=True)
            if job is None:
                return
            item = await session.get(RecoveryJobItem, item_id, with_for_update=True)
            if item is None:
                return
            if item.status in _TERMINAL_ITEM_STATUSES:
                return
            if retryable:
                item.retryable_failures_consumed += 1
            can_retry = (
                retryable
                and item.retryable_failures_consumed < item.retry_limit
                and job.status in {RecoveryJobStatus.QUEUED, RecoveryJobStatus.RUNNING}
            )
            if can_retry:
                try:
                    await self._lock_canonical_work(session, item)
                    work = await AdminRecoveryService(session).get_work(item.work_kind, item.work_id)
                except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as state_exc:
                    self._finish_item(
                        item,
                        status=RecoveryJobItemStatus.SKIPPED_STALE,
                        reason="canonical_state_changed",
                        error=str(state_exc),
                    )
                else:
                    item.status = RecoveryJobItemStatus.QUEUED
                    item.expected_version = work.version
                    item.canonical_version = work.version
                    item.dispatch_event_id = None
                    item.dispatched_at = None
                    item.finished_at = None
                    item.normalized_reason = type(exc).__name__[:128]
                    item.safe_error_text = sanitize_operational_error(exc)
            else:
                self._finish_item(
                    item,
                    status=RecoveryJobItemStatus.FAILED,
                    reason=type(exc).__name__,
                    error=str(exc),
                )
                await self._resolve_linked_dead_letter(session, item, succeeded=False)
            await self._reconcile_jobs(session, job_ids={job.id})
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
        item.safe_error_text = sanitize_operational_error(error)
        item.finished_at = utcnow()
        item.reservation_active = False


async def _load_uuid_row(session: AsyncSession, model: type, raw_id: str):
    row = await _try_load_uuid_row(session, model, raw_id, with_for_update=True)
    if row is None:
        raise RecoveryTerminalDispatchError(f"Recovery target {model.__name__}/{raw_id} no longer exists.")
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


def _try_parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _mark_non_stage_budget_dispatch(item: RecoveryJobItem) -> None:
    state = dict(item.previous_stage_state)
    state[_NON_STAGE_BUDGET_BASE_KEY] = item.retryable_failures_consumed
    item.previous_stage_state = state


def _non_stage_budget_base(item: RecoveryJobItem) -> int:
    value = item.previous_stage_state.get(_NON_STAGE_BUDGET_BASE_KEY)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _has_non_stage_budget_base(item: RecoveryJobItem) -> bool:
    value = item.previous_stage_state.get(_NON_STAGE_BUDGET_BASE_KEY)
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _restore_stage_snapshot(
    stage_row: PipelineStageJournal,
    snapshot: dict[str, object],
) -> bool:
    """Restore a validated pre-replay stage snapshot without partial mutation."""

    try:
        raw_attempt_count = snapshot["attempt_count"]
        raw_is_retryable = snapshot["is_retryable"]
        if (
            not isinstance(raw_attempt_count, int)
            or isinstance(raw_attempt_count, bool)
            or raw_attempt_count < 0
            or not isinstance(raw_is_retryable, bool)
        ):
            return False
        status = ContentPipelineStageStatus(str(snapshot["status"]))
        last_event_id = _snapshot_optional_uuid(snapshot.get("last_event_id"))
        normalized_reason = _snapshot_optional_text(snapshot.get("normalized_reason"))
        last_error_text = _snapshot_optional_text(snapshot.get("last_error_text"))
        retry_after = _snapshot_optional_datetime(snapshot.get("retry_after"))
        started_at = _snapshot_optional_datetime(snapshot.get("started_at"))
        finished_at = _snapshot_optional_datetime(snapshot.get("finished_at"))
    except KeyError, TypeError, ValueError:
        return False

    stage_row.status = status
    stage_row.attempt_count = raw_attempt_count
    stage_row.last_event_id = last_event_id
    stage_row.normalized_reason = normalized_reason
    stage_row.last_error_text = last_error_text
    stage_row.is_retryable = raw_is_retryable
    stage_row.retry_after = retry_after
    stage_row.started_at = started_at
    stage_row.finished_at = finished_at
    return True


def _snapshot_optional_uuid(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Snapshot UUID must be a string or null.")
    return uuid.UUID(value)


def _snapshot_optional_text(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("Snapshot text must be a string or null.")


def _snapshot_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Snapshot datetime must be a string or null.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Snapshot datetime must be timezone-aware.")
    return parsed


__all__ = [
    "RecoveryDispatchResult",
    "RecoveryRuntime",
    "run_recovery_dispatch_batch",
    "run_recovery_reconcile_batch",
    "run_telegram_recovery_loop",
]
