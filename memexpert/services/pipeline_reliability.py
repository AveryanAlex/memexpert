"""Durable capacity, circuit-breaker, and dead-letter reliability primitives."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memexpert.models.base import utcnow
from memexpert.models.content import MemeFileSyncTargetSnapshot, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    DependencyCircuitStatus,
    PipelineAttemptOutcome,
    PipelineCapacityStatus,
    RecoveryDeadLetterStatus,
    RecoveryWorkKind,
    SyncTargetKind,
)
from memexpert.models.operations import (
    DependencyCircuitState,
    PipelineCapacityState,
    PipelineDeadLetter,
    PipelineStageAttempt,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory


_THROUGHPUT_WINDOW: Final = timedelta(minutes=15)
_SAFE_TEXT_LIMIT: Final = 4_000
_SAFE_COLLECTION_LIMIT: Final = 100
_SENSITIVE_KEY_PARTS: Final = ("authorization", "cookie", "password", "secret", "session", "token")
_SYNC_TARGET_BY_STAGE: Final = {
    ContentPipelineStage.SYNC_QDRANT: SyncTargetKind.QDRANT,
    ContentPipelineStage.SYNC_MEILI: SyncTargetKind.MEILISEARCH,
}
DEPENDENCY_BY_STAGE: Final = {
    ContentPipelineStage.OCR: "ocr",
    ContentPipelineStage.EMBED: "voyage_qdrant",
    ContentPipelineStage.CLASSIFY: "classification",
    ContentPipelineStage.SYNC_QDRANT: "qdrant",
    ContentPipelineStage.SYNC_MEILI: "meilisearch",
}


@dataclass(frozen=True, slots=True)
class PipelineCapacityPolicy:
    """Hysteresis thresholds used by the periodic capacity observer."""

    close_pending_count: int
    reopen_pending_count: int
    close_oldest_age_seconds: float
    reopen_oldest_age_seconds: float

    def __post_init__(self) -> None:
        if self.close_pending_count < 1:
            raise ValueError("capacity close pending count must be positive")
        if self.reopen_pending_count < 0 or self.reopen_pending_count >= self.close_pending_count:
            raise ValueError("capacity reopen pending count must be below the close count")
        if self.close_oldest_age_seconds <= 0:
            raise ValueError("capacity close age must be positive")
        if self.reopen_oldest_age_seconds < 0:
            raise ValueError("capacity reopen age cannot be negative")
        if self.reopen_oldest_age_seconds >= self.close_oldest_age_seconds:
            raise ValueError("capacity reopen age must be below the close age")


@dataclass(frozen=True, slots=True)
class PipelineCapacityRefreshResult:
    observed_stages: int
    closed_stages: tuple[ContentPipelineStage, ...]


class DependencyCircuitOpenError(RuntimeError):
    """Raised when a shared provider circuit does not admit another call."""


async def refresh_pipeline_capacity_states(
    session_factory: AsyncSessionFactory,
    *,
    policy: PipelineCapacityPolicy,
) -> PipelineCapacityRefreshResult:
    """Refresh every stage's durable admission state with close/reopen hysteresis."""

    now = utcnow()
    throughput_since = now - _THROUGHPUT_WINDOW
    async with session_factory() as session:
        backlog_rows = (
            await session.execute(
                select(
                    PipelineStageJournal.stage,
                    func.count(PipelineStageJournal.id),
                    func.min(PipelineStageJournal.updated_at),
                )
                .where(
                    PipelineStageJournal.status.in_(
                        (ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING)
                    )
                )
                .group_by(PipelineStageJournal.stage)
            )
        ).all()
        throughput_rows = (
            await session.execute(
                select(PipelineStageAttempt.stage, func.count(PipelineStageAttempt.id))
                .where(
                    PipelineStageAttempt.outcome == PipelineAttemptOutcome.SUCCEEDED,
                    PipelineStageAttempt.finished_at >= throughput_since,
                )
                .group_by(PipelineStageAttempt.stage)
            )
        ).all()
        existing_rows = {
            row.stage: row for row in (await session.execute(select(PipelineCapacityState).with_for_update())).scalars()
        }

        backlog = {stage: (int(count), oldest) for stage, count, oldest in backlog_rows}
        throughput = {stage: int(count) / 15.0 for stage, count in throughput_rows}
        closed_stages: list[ContentPipelineStage] = []
        for stage in ContentPipelineStage:
            pending_count, oldest_pending_at = backlog.get(stage, (0, None))
            oldest_age_seconds = (
                max((now - oldest_pending_at).total_seconds(), 0.0) if oldest_pending_at is not None else 0.0
            )
            throughput_per_minute = throughput.get(stage, 0.0)
            drain_eta_seconds = (
                pending_count / throughput_per_minute * 60.0 if pending_count and throughput_per_minute > 0 else None
            )
            row = existing_rows.get(stage)
            previous_status = row.status if row is not None else PipelineCapacityStatus.OPEN
            should_close = (
                pending_count >= policy.close_pending_count or oldest_age_seconds >= policy.close_oldest_age_seconds
            )
            may_reopen = (
                pending_count <= policy.reopen_pending_count and oldest_age_seconds <= policy.reopen_oldest_age_seconds
            )
            status = previous_status
            if previous_status is PipelineCapacityStatus.OPEN and should_close:
                status = PipelineCapacityStatus.CLOSED
            elif previous_status is PipelineCapacityStatus.CLOSED and may_reopen:
                status = PipelineCapacityStatus.OPEN

            reason = None
            if status is PipelineCapacityStatus.CLOSED:
                closed_stages.append(stage)
                reason = (
                    "pending_count_high" if pending_count >= policy.close_pending_count else "oldest_pending_age_high"
                )
            if row is None:
                row = PipelineCapacityState(stage=stage)
                session.add(row)
            row.status = status
            row.pending_count = pending_count
            row.oldest_pending_age_seconds = oldest_age_seconds
            row.throughput_per_minute_15m = throughput_per_minute
            row.drain_eta_seconds = drain_eta_seconds
            row.reason = reason
            row.observed_at = now

        await session.commit()
    return PipelineCapacityRefreshResult(
        observed_stages=len(ContentPipelineStage),
        closed_stages=tuple(closed_stages),
    )


async def is_stage_admitted(session: AsyncSession, stage: ContentPipelineStage) -> bool:
    """Return whether new optional work may enter a stage."""

    status = await session.scalar(select(PipelineCapacityState.status).where(PipelineCapacityState.stage == stage))
    return status is not PipelineCapacityStatus.CLOSED


async def is_historical_admission_open(session: AsyncSession) -> bool:
    """Protect live traffic by pausing backfill/recovery when any heavy stage is closed."""

    closed = await session.scalar(
        select(func.count(PipelineCapacityState.id)).where(
            PipelineCapacityState.stage.in_(
                (
                    ContentPipelineStage.TRANSCODE,
                    ContentPipelineStage.OCR,
                    ContentPipelineStage.EMBED,
                    ContentPipelineStage.CLASSIFY,
                )
            ),
            PipelineCapacityState.status == PipelineCapacityStatus.CLOSED,
        )
    )
    return not closed


async def acquire_dependency_circuit(
    session_factory: AsyncSessionFactory,
    *,
    dependency: str,
    owner: str,
) -> None:
    """Acquire a provider call permit, including one fenced half-open probe."""

    now = utcnow()
    async with session_factory() as session:
        row = await session.scalar(
            select(DependencyCircuitState).where(DependencyCircuitState.dependency == dependency).with_for_update()
        )
        if row is None or row.status is DependencyCircuitStatus.CLOSED:
            return
        if row.status is DependencyCircuitStatus.OPEN:
            if row.retry_at is not None and row.retry_at > now:
                raise DependencyCircuitOpenError(
                    f"Dependency {dependency!r} circuit is open until {row.retry_at.isoformat()}.",
                )
            row.status = DependencyCircuitStatus.HALF_OPEN
            row.probe_owner = owner
            row.probe_generation += 1
            await session.commit()
            return
        if row.probe_owner == owner:
            return
        raise DependencyCircuitOpenError(f"Dependency {dependency!r} circuit has a half-open probe in flight.")


async def record_dependency_success(
    session_factory: AsyncSessionFactory,
    *,
    dependency: str,
) -> None:
    """Close and reset a provider circuit after a successful call."""

    async with session_factory() as session:
        row = await session.scalar(
            select(DependencyCircuitState).where(DependencyCircuitState.dependency == dependency).with_for_update()
        )
        if row is None:
            return
        row.status = DependencyCircuitStatus.CLOSED
        row.error_fingerprint = None
        row.consecutive_failures = 0
        row.opened_at = None
        row.retry_at = None
        row.probe_owner = None
        await session.commit()


async def record_dependency_failure(
    session_factory: AsyncSessionFactory,
    *,
    dependency: str,
    error: Exception,
    failure_threshold: int,
    cooldown_seconds: float,
) -> None:
    """Increment a provider fingerprint and open the circuit at the configured threshold."""

    if failure_threshold < 1:
        raise ValueError("circuit failure threshold must be positive")
    if cooldown_seconds <= 0:
        raise ValueError("circuit cooldown must be positive")
    now = utcnow()
    fingerprint = hashlib.sha256(
        f"{type(error).__module__}.{type(error).__qualname__}:{str(error).strip()}".encode()
    ).hexdigest()[:128]
    async with session_factory() as session:
        await session.execute(
            pg_insert(DependencyCircuitState)
            .values(
                id=uuid.uuid7(),
                dependency=dependency,
                status=DependencyCircuitStatus.CLOSED,
                consecutive_failures=0,
                probe_generation=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_dependency_circuit_states_dependency")
        )
        row = await session.scalar(
            select(DependencyCircuitState).where(DependencyCircuitState.dependency == dependency).with_for_update()
        )
        if row is None:  # pragma: no cover - insert/select invariant.
            raise RuntimeError(f"Failed to initialize dependency circuit {dependency!r}.")
        if row.error_fingerprint == fingerprint:
            row.consecutive_failures += 1
        else:
            row.error_fingerprint = fingerprint
            row.consecutive_failures = 1
        if row.status is DependencyCircuitStatus.HALF_OPEN or row.consecutive_failures >= failure_threshold:
            row.status = DependencyCircuitStatus.OPEN
            row.opened_at = now
            row.retry_at = now + timedelta(seconds=cooldown_seconds)
            row.probe_owner = None
        await session.commit()


async def record_pipeline_dead_letter(
    session_factory: AsyncSessionFactory,
    *,
    payload: object,
    headers: Mapping[str, object] | None,
    broker_message_id: str | None,
    normalized_reason: str,
) -> uuid.UUID:
    """Write or reconcile one final broker delivery before its original message is acknowledged."""

    decoded_payload = _decode_payload(payload)
    payload_hash = hashlib.sha256(
        json.dumps(decoded_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    deduplication_key = hashlib.sha256(
        f"{broker_message_id or ''}:{normalized_reason}:{payload_hash}".encode()
    ).hexdigest()
    safe_payload = _safe_mapping(decoded_payload)
    safe_headers = _safe_mapping(dict(headers or {}))
    death_count = _death_count(headers)
    event_type = _optional_text(decoded_payload.get("event_type"), limit=128)

    async with session_factory() as session:
        work_kind, work_id = await _resolve_dead_letter_work(session, decoded_payload)
        now = utcnow()
        dead_letter_id = uuid.uuid7()
        statement = (
            pg_insert(PipelineDeadLetter)
            .values(
                id=dead_letter_id,
                deduplication_key=deduplication_key,
                broker_message_id=_optional_text(broker_message_id, limit=255),
                payload_hash=payload_hash,
                event_type=event_type,
                work_kind=work_kind,
                work_id=work_id,
                normalized_reason=normalized_reason[:128],
                death_count=death_count,
                safe_payload=safe_payload,
                safe_headers=safe_headers,
                status=RecoveryDeadLetterStatus.UNRESOLVED,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_pipeline_dead_letters_deduplication_key",
                set_={
                    "death_count": func.greatest(PipelineDeadLetter.death_count, death_count),
                    "safe_headers": safe_headers,
                    "status": case(
                        (
                            PipelineDeadLetter.status == RecoveryDeadLetterStatus.RESOLVED,
                            RecoveryDeadLetterStatus.UNRESOLVED,
                        ),
                        else_=PipelineDeadLetter.status,
                    ),
                    "recovery_item_id": case(
                        (PipelineDeadLetter.status == RecoveryDeadLetterStatus.RESOLVED, None),
                        else_=PipelineDeadLetter.recovery_item_id,
                    ),
                    "resolved_at": case(
                        (PipelineDeadLetter.status == RecoveryDeadLetterStatus.RESOLVED, None),
                        else_=PipelineDeadLetter.resolved_at,
                    ),
                    "resolution_note": case(
                        (PipelineDeadLetter.status == RecoveryDeadLetterStatus.RESOLVED, None),
                        else_=PipelineDeadLetter.resolution_note,
                    ),
                    "updated_at": now,
                },
            )
            .returning(PipelineDeadLetter.id)
        )
        resolved_id = (await session.execute(statement)).scalar_one()
        await session.commit()
        return resolved_id


async def _resolve_dead_letter_work(
    session: AsyncSession,
    payload: dict[str, object],
) -> tuple[RecoveryWorkKind | None, str | None]:
    ingest_request_id = _parse_uuid(payload.get("ingest_request_id"))
    if ingest_request_id is not None:
        return RecoveryWorkKind.INGEST_REQUEST, str(ingest_request_id)

    meme_file_id = _parse_uuid(payload.get("meme_file_id"))
    raw_stage = payload.get("stage")
    if meme_file_id is None or not isinstance(raw_stage, str):
        return None, None
    try:
        stage = ContentPipelineStage(raw_stage)
    except ValueError:
        return None, None

    target = _SYNC_TARGET_BY_STAGE.get(stage)
    if target is not None:
        snapshot_id = await session.scalar(
            select(MemeFileSyncTargetSnapshot.id).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
                MemeFileSyncTargetSnapshot.sync_target == target,
            )
        )
        if snapshot_id is not None:
            return RecoveryWorkKind.SYNC_TARGET, str(snapshot_id)
    journal_id = await session.scalar(
        select(PipelineStageJournal.id).where(
            PipelineStageJournal.meme_file_id == meme_file_id,
            PipelineStageJournal.stage == stage,
        )
    )
    if journal_id is not None:
        return RecoveryWorkKind.PIPELINE_STAGE, str(journal_id)
    return None, None


def _decode_payload(payload: object) -> dict[str, object]:
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    if isinstance(payload, (str, bytes, bytearray)):
        raw_text = payload.decode(errors="replace") if isinstance(payload, (bytes, bytearray)) else payload
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError:
            return {"raw": raw_text[:_SAFE_TEXT_LIMIT]}
        if isinstance(decoded, dict):
            return {str(key): value for key, value in decoded.items()}
        return {"raw": decoded}
    return {"raw": payload}


def _safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= _SAFE_COLLECTION_LIMIT:
            result["_truncated"] = True
            break
        normalized_key = str(key)[:255]
        if any(part in normalized_key.lower() for part in _SENSITIVE_KEY_PARTS):
            result[normalized_key] = "[redacted]"
        else:
            result[normalized_key] = _safe_value(item, depth=0)
    return result


def _safe_value(value: object, *, depth: int) -> object:
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_SAFE_TEXT_LIMIT]
    if isinstance(value, (bytes, bytearray)):
        return value.decode(errors="replace")[:_SAFE_TEXT_LIMIT]
    if isinstance(value, Mapping):
        return _safe_mapping({str(key): item for key, item in value.items()})
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:_SAFE_COLLECTION_LIMIT]]
    return str(value)[:_SAFE_TEXT_LIMIT]


def _death_count(headers: Mapping[str, object] | None) -> int:
    if headers is None:
        return 1
    raw_deaths = headers.get("x-death")
    if not isinstance(raw_deaths, list):
        return 1
    maximum = 0
    for entry in raw_deaths:
        if not isinstance(entry, Mapping):
            continue
        raw_count = entry.get("count")
        try:
            count = (
                int(raw_count) if isinstance(raw_count, (str, int, float)) and not isinstance(raw_count, bool) else 0
            )
        except TypeError, ValueError:
            count = 0
        maximum = max(maximum, count)
    return max(maximum + 1, 1)


def _parse_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except TypeError, ValueError, AttributeError:
        return None


def _optional_text(value: object, *, limit: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized[:limit] or None


__all__ = [
    "DEPENDENCY_BY_STAGE",
    "DependencyCircuitOpenError",
    "PipelineCapacityPolicy",
    "PipelineCapacityRefreshResult",
    "acquire_dependency_circuit",
    "is_historical_admission_open",
    "is_stage_admitted",
    "record_dependency_failure",
    "record_dependency_success",
    "record_pipeline_dead_letter",
    "refresh_pipeline_capacity_states",
]
