"""Reconcile published PostgreSQL synonym snapshots into Meilisearch."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from memexpert.core.config import Settings, get_settings
from memexpert.core.meilisearch_settings import (
    MeilisearchSettingsClient,
    MeilisearchSettingsClientProtocol,
    MeilisearchSettingsError,
)
from memexpert.models.base import utcnow
from memexpert.models.enums import (
    SearchSynonymLocale,
    SearchSynonymRevisionStatus,
    SearchSynonymSyncStatus,
)
from memexpert.models.search_synonyms import (
    SearchSynonymCatalog,
    SearchSynonymRevision,
    SearchSynonymSyncState,
)
from memexpert.services.search_synonym_compiler import (
    SEARCH_SYNONYM_COMPILER_VERSION,
    compile_search_synonyms,
    hash_synonym_map,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory


MEILISEARCH_SYNONYM_SYNC_STATE_ID = "meilisearch"
MAX_RECONCILE_ERROR_LENGTH = 1000


class SynonymSnapshotValidationError(ValueError):
    """Raised when published snapshots cannot form one trustworthy map."""


class SynonymSettingsVerificationError(RuntimeError):
    """Raised when the provider map differs after a successful settings task."""


@dataclass(frozen=True, slots=True)
class PublishedSynonymSnapshot:
    """Immutable scheduler projection of one published locale revision."""

    revision_id: uuid.UUID
    locale: str
    revision_number: int
    source_text: str
    compiled_synonyms: object
    compiler_version: str
    compiled_hash: str


@dataclass(frozen=True, slots=True)
class MeilisearchSettingsReconcileResult:
    """Bounded outcome suitable for scheduler logs and focused tests."""

    status: SearchSynonymSyncStatus
    reason: str
    changed: bool
    desired_hash: str | None
    actual_hash: str | None
    provider_task_uid: int | None
    revision_count: int
    duration_seconds: float


class SearchSynonymSyncRepositoryProtocol(Protocol):
    """Persistence boundary used by the reconciliation state machine."""

    async def load_published_snapshots(self) -> tuple[PublishedSynonymSnapshot, ...]: ...

    async def record_no_published_revisions(
        self,
        *,
        expected_snapshot_ids: tuple[str, ...],
        attempted_at: datetime,
    ) -> SearchSynonymSyncStatus | None: ...

    async def record_syncing(
        self,
        *,
        desired_hash: str,
        desired_revision_ids: Mapping[str, str],
        expected_snapshot_ids: tuple[str, ...],
        actual_hash: str,
        attempted_at: datetime,
    ) -> bool: ...

    async def record_task_uid(
        self,
        task_uid: int,
        *,
        expected_snapshot_ids: tuple[str, ...],
    ) -> bool: ...

    async def record_success(
        self,
        *,
        desired_hash: str,
        desired_revision_ids: Mapping[str, str],
        expected_snapshot_ids: tuple[str, ...],
        actual_hash: str,
        task_uid: int | None,
        provider_applied: bool,
        succeeded_at: datetime,
    ) -> bool: ...

    async def record_failure(
        self,
        *,
        desired_hash: str | None,
        desired_revision_ids: Mapping[str, str],
        expected_snapshot_ids: tuple[str, ...],
        actual_hash: str | None,
        task_uid: int | None,
        safe_error: str,
        failed_at: datetime,
    ) -> bool: ...


class SqlAlchemySearchSynonymSyncRepository:
    """PostgreSQL implementation of the synonym reconciliation ledger."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def load_published_snapshots(self) -> tuple[PublishedSynonymSnapshot, ...]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SearchSynonymRevision, SearchSynonymCatalog.locale)
                .join(SearchSynonymCatalog, SearchSynonymCatalog.id == SearchSynonymRevision.catalog_id)
                .where(SearchSynonymRevision.status == SearchSynonymRevisionStatus.PUBLISHED)
                .order_by(SearchSynonymCatalog.locale, SearchSynonymRevision.revision_number),
            )
            return tuple(
                PublishedSynonymSnapshot(
                    revision_id=revision.id,
                    locale=locale.value,
                    revision_number=revision.revision_number,
                    source_text=revision.source_text,
                    compiled_synonyms=revision.compiled_synonyms,
                    compiler_version=revision.compiler_version,
                    compiled_hash=revision.compiled_hash,
                )
                for revision, locale in result.all()
            )

    async def record_no_published_revisions(
        self,
        *,
        expected_snapshot_ids: tuple[str, ...],
        attempted_at: datetime,
    ) -> SearchSynonymSyncStatus | None:
        async with self._session_factory() as session, session.begin():
            state, created = await self._load_state(session)
            if not await self._snapshot_is_current(
                session,
                expected_snapshot_ids=expected_snapshot_ids,
            ):
                return None
            state.desired_hash = None
            state.desired_revision_ids = {}
            state.last_attempt_at = attempted_at
            if state.applied_hash is None:
                state.status = SearchSynonymSyncStatus.IDLE
                state.last_error = None
            else:
                state.status = SearchSynonymSyncStatus.FAILED
                state.last_error = (
                    "No published synonym revisions remain after a prior application; "
                    "refusing to clear Meilisearch settings."
                )
                state.last_failure_at = attempted_at
            _advance_version(state, created=created)
            return state.status

    async def record_syncing(
        self,
        *,
        desired_hash: str,
        desired_revision_ids: Mapping[str, str],
        expected_snapshot_ids: tuple[str, ...],
        actual_hash: str,
        attempted_at: datetime,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            state, created = await self._load_state(session)
            if not await self._snapshot_is_current(
                session,
                expected_snapshot_ids=expected_snapshot_ids,
            ):
                return False
            state.status = SearchSynonymSyncStatus.SYNCING
            state.desired_hash = desired_hash
            state.desired_revision_ids = dict(desired_revision_ids)
            state.actual_hash = actual_hash
            state.provider_task_uid = None
            state.last_error = None
            state.last_attempt_at = attempted_at
            _advance_version(state, created=created)
            return True

    async def record_task_uid(
        self,
        task_uid: int,
        *,
        expected_snapshot_ids: tuple[str, ...],
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            state, created = await self._load_state(session)
            if not await self._snapshot_is_current(
                session,
                expected_snapshot_ids=expected_snapshot_ids,
            ):
                return False
            state.provider_task_uid = task_uid
            _advance_version(state, created=created)
            return True

    async def record_success(
        self,
        *,
        desired_hash: str,
        desired_revision_ids: Mapping[str, str],
        expected_snapshot_ids: tuple[str, ...],
        actual_hash: str,
        task_uid: int | None,
        provider_applied: bool,
        succeeded_at: datetime,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            state, created = await self._load_state(session)
            if not await self._snapshot_is_current(
                session,
                expected_snapshot_ids=expected_snapshot_ids,
            ):
                return False
            state.status = SearchSynonymSyncStatus.SYNCED
            state.desired_hash = desired_hash
            state.applied_hash = desired_hash
            state.actual_hash = actual_hash
            state.desired_revision_ids = dict(desired_revision_ids)
            state.applied_revision_ids = dict(desired_revision_ids)
            if task_uid is not None:
                state.provider_task_uid = task_uid
            state.last_error = None
            state.last_attempt_at = succeeded_at
            if provider_applied:
                state.last_success_at = succeeded_at
            _advance_version(state, created=created)
            return True

    async def record_failure(
        self,
        *,
        desired_hash: str | None,
        desired_revision_ids: Mapping[str, str],
        expected_snapshot_ids: tuple[str, ...],
        actual_hash: str | None,
        task_uid: int | None,
        safe_error: str,
        failed_at: datetime,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            state, created = await self._load_state(session)
            if not await self._snapshot_is_current(
                session,
                expected_snapshot_ids=expected_snapshot_ids,
            ):
                return False
            state.status = SearchSynonymSyncStatus.FAILED
            state.desired_hash = desired_hash
            state.desired_revision_ids = dict(desired_revision_ids)
            if actual_hash is not None:
                state.actual_hash = actual_hash
            state.provider_task_uid = task_uid
            state.last_error = safe_error[:MAX_RECONCILE_ERROR_LENGTH]
            state.last_attempt_at = failed_at
            state.last_failure_at = failed_at
            _advance_version(state, created=created)
            return True

    async def _load_state(self, session: AsyncSession) -> tuple[SearchSynonymSyncState, bool]:
        state = await session.get(
            SearchSynonymSyncState,
            MEILISEARCH_SYNONYM_SYNC_STATE_ID,
            with_for_update=True,
        )
        if state is not None:
            return state, False

        state = SearchSynonymSyncState(
            id=MEILISEARCH_SYNONYM_SYNC_STATE_ID,
            status=SearchSynonymSyncStatus.IDLE,
            desired_revision_ids={},
            applied_revision_ids={},
            version=1,
        )
        session.add(state)
        return state, True

    async def _snapshot_is_current(
        self,
        session: AsyncSession,
        *,
        expected_snapshot_ids: tuple[str, ...],
    ) -> bool:
        current_ids = tuple(
            sorted(
                str(revision_id)
                for revision_id in (
                    await session.execute(
                        select(SearchSynonymRevision.id).where(
                            SearchSynonymRevision.status
                            == SearchSynonymRevisionStatus.PUBLISHED
                        )
                    )
                ).scalars()
            )
        )
        return current_ids == expected_snapshot_ids


class MeilisearchSettingsReconciler:
    """Apply the published synonym map only when the provider has drifted."""

    def __init__(
        self,
        repository: SearchSynonymSyncRepositoryProtocol,
        *,
        client: MeilisearchSettingsClientProtocol,
    ) -> None:
        self._repository = repository
        self._client = client

    async def run(self, *, now: datetime | None = None) -> MeilisearchSettingsReconcileResult:
        start_seconds = time.perf_counter()
        attempted_at = now or utcnow()
        snapshots = await self._repository.load_published_snapshots()
        expected_snapshot_ids = _snapshot_ids(snapshots)

        if not snapshots:
            recorded_status = await self._repository.record_no_published_revisions(
                expected_snapshot_ids=expected_snapshot_ids,
                attempted_at=attempted_at,
            )
            if recorded_status is None:
                return _superseded_result(
                    start_seconds=start_seconds,
                    desired_hash=None,
                    actual_hash=None,
                    provider_task_uid=None,
                    revision_count=0,
                    changed=False,
                )
            return MeilisearchSettingsReconcileResult(
                status=recorded_status,
                reason=(
                    "no_published_revisions"
                    if recorded_status is SearchSynonymSyncStatus.IDLE
                    else "published_revisions_missing_after_apply"
                ),
                changed=False,
                desired_hash=None,
                actual_hash=None,
                provider_task_uid=None,
                revision_count=0,
                duration_seconds=time.perf_counter() - start_seconds,
            )

        revision_ids = _best_effort_revision_ids(snapshots)
        try:
            desired_synonyms, revision_ids = combine_published_synonym_snapshots(snapshots)
            desired_hash = hash_canonical_synonym_map(desired_synonyms)
        except SynonymSnapshotValidationError as exc:
            failed_at = now or utcnow()
            recorded = await self._repository.record_failure(
                desired_hash=None,
                desired_revision_ids=revision_ids,
                expected_snapshot_ids=expected_snapshot_ids,
                actual_hash=None,
                task_uid=None,
                safe_error=_safe_reconcile_error(exc),
                failed_at=failed_at,
            )
            if not recorded:
                return _superseded_result(
                    start_seconds=start_seconds,
                    desired_hash=None,
                    actual_hash=None,
                    provider_task_uid=None,
                    revision_count=len(snapshots),
                    changed=False,
                )
            return _failed_result(
                start_seconds=start_seconds,
                reason="invalid_published_snapshots",
                desired_hash=None,
                actual_hash=None,
                provider_task_uid=None,
                revision_count=len(snapshots),
            )

        try:
            current_synonyms = canonicalize_synonym_map(await self._client.get_synonyms())
            actual_hash = hash_canonical_synonym_map(current_synonyms)
        except Exception as exc:  # noqa: BLE001 - provider boundary records a safe durable failure.
            failed_at = now or utcnow()
            recorded = await self._repository.record_failure(
                desired_hash=desired_hash,
                desired_revision_ids=revision_ids,
                expected_snapshot_ids=expected_snapshot_ids,
                actual_hash=None,
                task_uid=None,
                safe_error=_safe_reconcile_error(exc),
                failed_at=failed_at,
            )
            if not recorded:
                return _superseded_result(
                    start_seconds=start_seconds,
                    desired_hash=desired_hash,
                    actual_hash=None,
                    provider_task_uid=None,
                    revision_count=len(snapshots),
                    changed=False,
                )
            return _failed_result(
                start_seconds=start_seconds,
                reason="provider_read_failed",
                desired_hash=desired_hash,
                actual_hash=None,
                provider_task_uid=None,
                revision_count=len(snapshots),
            )

        if actual_hash == desired_hash:
            succeeded_at = now or utcnow()
            recorded = await self._repository.record_success(
                desired_hash=desired_hash,
                desired_revision_ids=revision_ids,
                expected_snapshot_ids=expected_snapshot_ids,
                actual_hash=actual_hash,
                task_uid=None,
                provider_applied=False,
                succeeded_at=succeeded_at,
            )
            if not recorded:
                return _superseded_result(
                    start_seconds=start_seconds,
                    desired_hash=desired_hash,
                    actual_hash=actual_hash,
                    provider_task_uid=None,
                    revision_count=len(snapshots),
                    changed=False,
                )
            return MeilisearchSettingsReconcileResult(
                status=SearchSynonymSyncStatus.SYNCED,
                reason="in_sync",
                changed=False,
                desired_hash=desired_hash,
                actual_hash=actual_hash,
                provider_task_uid=None,
                revision_count=len(snapshots),
                duration_seconds=time.perf_counter() - start_seconds,
            )

        recorded_syncing = await self._repository.record_syncing(
            desired_hash=desired_hash,
            desired_revision_ids=revision_ids,
            expected_snapshot_ids=expected_snapshot_ids,
            actual_hash=actual_hash,
            attempted_at=attempted_at,
        )
        if not recorded_syncing:
            return _superseded_result(
                start_seconds=start_seconds,
                desired_hash=desired_hash,
                actual_hash=actual_hash,
                provider_task_uid=None,
                revision_count=len(snapshots),
                changed=False,
            )
        task_uid: int | None = None
        verified_hash: str | None = None
        task_state_is_current = True
        try:
            task_uid = await self._client.submit_synonyms(desired_synonyms)
            task_state_is_current = await self._repository.record_task_uid(
                task_uid,
                expected_snapshot_ids=expected_snapshot_ids,
            )
            await self._client.wait_for_task(task_uid)
            verified_synonyms = canonicalize_synonym_map(await self._client.get_synonyms())
            verified_hash = hash_canonical_synonym_map(verified_synonyms)
            if verified_hash != desired_hash:
                raise SynonymSettingsVerificationError(
                    "Meilisearch synonym verification did not match the published snapshot.",
                )
        except Exception as exc:  # noqa: BLE001 - provider boundary records a safe durable failure.
            if not task_state_is_current:
                return _superseded_result(
                    start_seconds=start_seconds,
                    desired_hash=desired_hash,
                    actual_hash=verified_hash or actual_hash,
                    provider_task_uid=task_uid,
                    revision_count=len(snapshots),
                    changed=task_uid is not None,
                )
            failed_at = now or utcnow()
            failure_actual_hash = verified_hash or actual_hash
            recorded = await self._repository.record_failure(
                desired_hash=desired_hash,
                desired_revision_ids=revision_ids,
                expected_snapshot_ids=expected_snapshot_ids,
                actual_hash=failure_actual_hash,
                task_uid=task_uid,
                safe_error=_safe_reconcile_error(exc),
                failed_at=failed_at,
            )
            if not recorded:
                return _superseded_result(
                    start_seconds=start_seconds,
                    desired_hash=desired_hash,
                    actual_hash=failure_actual_hash,
                    provider_task_uid=task_uid,
                    revision_count=len(snapshots),
                    changed=task_uid is not None,
                )
            return _failed_result(
                start_seconds=start_seconds,
                reason="provider_apply_failed",
                desired_hash=desired_hash,
                actual_hash=failure_actual_hash,
                provider_task_uid=task_uid,
                revision_count=len(snapshots),
            )

        assert verified_hash is not None
        if not task_state_is_current:
            return _superseded_result(
                start_seconds=start_seconds,
                desired_hash=desired_hash,
                actual_hash=verified_hash,
                provider_task_uid=task_uid,
                revision_count=len(snapshots),
                changed=True,
            )
        succeeded_at = now or utcnow()
        recorded = await self._repository.record_success(
            desired_hash=desired_hash,
            desired_revision_ids=revision_ids,
            expected_snapshot_ids=expected_snapshot_ids,
            actual_hash=verified_hash,
            task_uid=task_uid,
            provider_applied=True,
            succeeded_at=succeeded_at,
        )
        if not recorded:
            return _superseded_result(
                start_seconds=start_seconds,
                desired_hash=desired_hash,
                actual_hash=verified_hash,
                provider_task_uid=task_uid,
                revision_count=len(snapshots),
                changed=True,
            )
        return MeilisearchSettingsReconcileResult(
            status=SearchSynonymSyncStatus.SYNCED,
            reason="applied",
            changed=True,
            desired_hash=desired_hash,
            actual_hash=verified_hash,
            provider_task_uid=task_uid,
            revision_count=len(snapshots),
            duration_seconds=time.perf_counter() - start_seconds,
        )

    async def run_until_current(
        self,
        *,
        now: datetime | None = None,
        max_attempts: int = 3,
    ) -> MeilisearchSettingsReconcileResult:
        """Immediately converge after a publication supersedes an in-flight run."""

        result: MeilisearchSettingsReconcileResult | None = None
        for _attempt in range(max_attempts):
            result = await self.run(now=now)
            if result.reason != "superseded":
                return result
        assert result is not None
        return result


async def run_meilisearch_settings_reconcile(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings | None = None,
    client: MeilisearchSettingsClientProtocol | None = None,
    now: datetime | None = None,
) -> MeilisearchSettingsReconcileResult:
    """Build production dependencies and execute one reconciliation attempt."""

    resolved_settings = settings or get_settings()
    repository = SqlAlchemySearchSynonymSyncRepository(session_factory)
    owned_client: MeilisearchSettingsClient | None = None
    resolved_client = client
    if resolved_client is None:
        owned_client = MeilisearchSettingsClient(settings=resolved_settings)
        resolved_client = owned_client
    reconciler = MeilisearchSettingsReconciler(
        repository,
        client=resolved_client,
    )
    try:
        return await reconciler.run_until_current(now=now)
    finally:
        if owned_client is not None:
            await owned_client.aclose()


def canonicalize_synonym_map(raw_synonyms: object) -> dict[str, list[str]]:
    """Validate and canonicalize one complete synonym map for stable hashing."""

    if not isinstance(raw_synonyms, dict):
        raise SynonymSnapshotValidationError("Synonym snapshot must be a JSON object.")

    canonical: dict[str, list[str]] = {}
    for raw_key, raw_values in raw_synonyms.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise SynonymSnapshotValidationError("Synonym snapshot contains an invalid key.")
        if not isinstance(raw_values, list) or not raw_values:
            raise SynonymSnapshotValidationError("Synonym snapshot contains an invalid alternatives list.")
        if any(not isinstance(value, str) or not value.strip() for value in raw_values):
            raise SynonymSnapshotValidationError("Synonym snapshot contains an invalid alternative.")
        canonical[raw_key] = sorted({value for value in raw_values if isinstance(value, str)})
    return {key: canonical[key] for key in sorted(canonical)}


def combine_published_synonym_snapshots(
    snapshots: tuple[PublishedSynonymSnapshot, ...],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Combine EN/RU revisions deterministically and reject collisions."""

    combined: dict[str, list[str]] = {}
    revision_ids: dict[str, str] = {}
    for snapshot in sorted(snapshots, key=lambda item: (item.locale, item.revision_number, str(item.revision_id))):
        if snapshot.locale not in {"en", "ru"}:
            raise SynonymSnapshotValidationError("Published synonym snapshot has an unsupported locale.")
        if snapshot.locale in revision_ids:
            raise SynonymSnapshotValidationError("Multiple published synonym revisions exist for one locale.")
        revision_ids[snapshot.locale] = str(snapshot.revision_id)
        canonical_snapshot = _validate_snapshot_integrity(snapshot)
        if not canonical_snapshot:
            raise SynonymSnapshotValidationError(
                "A published locale synonym snapshot is empty; refusing to clear provider settings.",
            )
        for key, alternatives in canonical_snapshot.items():
            previous = combined.get(key)
            if previous is not None and previous != alternatives:
                raise SynonymSnapshotValidationError(
                    "Published synonym snapshots contain a conflicting duplicate key.",
                )
            combined[key] = alternatives
    if not combined:
        raise SynonymSnapshotValidationError(
            "Published synonym snapshots compile to an empty map; refusing to clear provider settings.",
        )
    return {key: combined[key] for key in sorted(combined)}, revision_ids


def _validate_snapshot_integrity(
    snapshot: PublishedSynonymSnapshot,
) -> dict[str, list[str]]:
    if snapshot.compiler_version != SEARCH_SYNONYM_COMPILER_VERSION:
        raise SynonymSnapshotValidationError(
            "Published synonym snapshot uses an unsupported compiler version.",
        )
    if not isinstance(snapshot.source_text, str):
        raise SynonymSnapshotValidationError("Published synonym snapshot has invalid source text.")
    try:
        locale = SearchSynonymLocale(snapshot.locale)
    except ValueError as exc:
        raise SynonymSnapshotValidationError(
            "Published synonym snapshot has an unsupported locale.",
        ) from exc

    compiled = compile_search_synonyms(snapshot.source_text, locale=locale)
    canonical_snapshot = canonicalize_synonym_map(snapshot.compiled_synonyms)
    if not canonical_snapshot:
        raise SynonymSnapshotValidationError(
            "A published locale synonym snapshot is empty; refusing to clear provider settings.",
        )
    if not compiled.valid:
        raise SynonymSnapshotValidationError(
            "Published synonym snapshot source no longer passes compiler validation.",
        )
    if canonical_snapshot != compiled.compiled_synonyms:
        raise SynonymSnapshotValidationError(
            "Published synonym snapshot does not match its authored source.",
        )
    if snapshot.compiled_hash != compiled.compiled_hash:
        raise SynonymSnapshotValidationError(
            "Published synonym snapshot hash does not match its compiler output.",
        )
    return canonical_snapshot


def hash_canonical_synonym_map(synonyms: Mapping[str, list[str]]) -> str:
    """Hash canonical compact JSON without leaking map contents to logs."""

    digest, _payload_bytes = hash_synonym_map(
        {key: list(values) for key, values in synonyms.items()},
    )
    return digest


def meilisearch_settings_reconcile_result_log_extra(
    job_id: str,
    result: MeilisearchSettingsReconcileResult,
) -> dict[str, object]:
    """Return a key-free structured summary for scheduler logs."""

    return {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "status": result.status.value,
        "degraded_mode": result.status == SearchSynonymSyncStatus.FAILED,
        "reason": result.reason,
        "changed": result.changed,
        "desired_hash": result.desired_hash,
        "actual_hash": result.actual_hash,
        "provider_task_uid": result.provider_task_uid,
        "revision_count": result.revision_count,
        "duration_seconds": result.duration_seconds,
    }


def _advance_version(state: SearchSynonymSyncState, *, created: bool) -> None:
    if not created:
        state.version += 1


def _best_effort_revision_ids(snapshots: tuple[PublishedSynonymSnapshot, ...]) -> dict[str, str]:
    revision_ids: dict[str, str] = {}
    for snapshot in snapshots:
        if snapshot.locale in revision_ids:
            return {}
        revision_ids[snapshot.locale] = str(snapshot.revision_id)
    return revision_ids


def _snapshot_ids(snapshots: tuple[PublishedSynonymSnapshot, ...]) -> tuple[str, ...]:
    return tuple(sorted(str(snapshot.revision_id) for snapshot in snapshots))


def _safe_reconcile_error(exc: Exception) -> str:
    if isinstance(exc, (MeilisearchSettingsError, SynonymSnapshotValidationError, SynonymSettingsVerificationError)):
        value = str(exc) or type(exc).__name__
    else:
        value = f"Meilisearch settings reconciliation failed ({type(exc).__name__})."
    return value[:MAX_RECONCILE_ERROR_LENGTH]


def _failed_result(
    *,
    start_seconds: float,
    reason: str,
    desired_hash: str | None,
    actual_hash: str | None,
    provider_task_uid: int | None,
    revision_count: int,
) -> MeilisearchSettingsReconcileResult:
    return MeilisearchSettingsReconcileResult(
        status=SearchSynonymSyncStatus.FAILED,
        reason=reason,
        changed=False,
        desired_hash=desired_hash,
        actual_hash=actual_hash,
        provider_task_uid=provider_task_uid,
        revision_count=revision_count,
        duration_seconds=time.perf_counter() - start_seconds,
    )


def _superseded_result(
    *,
    start_seconds: float,
    desired_hash: str | None,
    actual_hash: str | None,
    provider_task_uid: int | None,
    revision_count: int,
    changed: bool,
) -> MeilisearchSettingsReconcileResult:
    return MeilisearchSettingsReconcileResult(
        status=SearchSynonymSyncStatus.PENDING,
        reason="superseded",
        changed=changed,
        desired_hash=desired_hash,
        actual_hash=actual_hash,
        provider_task_uid=provider_task_uid,
        revision_count=revision_count,
        duration_seconds=time.perf_counter() - start_seconds,
    )


__all__ = [
    "MEILISEARCH_SYNONYM_SYNC_STATE_ID",
    "MeilisearchSettingsReconcileResult",
    "MeilisearchSettingsReconciler",
    "PublishedSynonymSnapshot",
    "SearchSynonymSyncRepositoryProtocol",
    "SqlAlchemySearchSynonymSyncRepository",
    "SynonymSettingsVerificationError",
    "SynonymSnapshotValidationError",
    "canonicalize_synonym_map",
    "combine_published_synonym_snapshots",
    "hash_canonical_synonym_map",
    "meilisearch_settings_reconcile_result_log_extra",
    "run_meilisearch_settings_reconcile",
]
