"""Bounded scheduler batch jobs for deferred search-index and SEO work."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import and_, case, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memexpert.core.config import Settings, get_settings
from memexpert.core.meilisearch import (
    MeilisearchSyncClientProtocol,
    MeilisearchSyncConflictError,
    MeilisearchSyncMalformedResponseError,
    MeilisearchSyncProviderUnavailableError,
    MeilisearchSyncTimeoutError,
    PipelineMeilisearchDocument,
    PipelineMeilisearchSyncClient,
)
from memexpert.core.qdrant import (
    PipelineQdrantSyncClient,
    QdrantSyncClientProtocol,
    QdrantSyncConflictError,
    QdrantSyncMalformedResponseError,
    QdrantSyncPayload,
    QdrantSyncProviderUnavailableError,
    QdrantSyncTimeoutError,
)
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import Meme, MemeFile, MemeFileSyncTargetSnapshot, MemeSeoPage, MemeTemplate
from memexpert.models.enums import ContentProcessingStatus, SyncTargetKind, SyncTargetStatus
from memexpert.pipeline.helpers import build_sync_preview_model, trim_error_text, trim_reason
from memexpert.services.errors import PipelineIngestError
from memexpert.services.meme_seo import MemeSeoGenerationService, MemeSeoProviderProtocol, build_meme_seo_provider
from memexpert.services.search_index_sync import (
    build_meilisearch_document,
    build_qdrant_sync_payload,
    load_search_index_state,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory

_EARLIEST_SYNC_CLOCK = datetime(1970, 1, 1, tzinfo=UTC)
_SYNC_TARGETS: tuple[SyncTargetKind, ...] = (SyncTargetKind.QDRANT, SyncTargetKind.MEILISEARCH)
_MAX_PREVIEW_TEXT_LENGTH = 2048
_MAX_PREVIEW_LIST_LENGTH = 100

_SYNC_QDRANT_CONFLICT = "sync_qdrant_conflict"
_SYNC_QDRANT_MALFORMED_PAYLOAD = "sync_qdrant_malformed_payload"
_SYNC_QDRANT_PROVIDER_BLOCKED = "sync_qdrant_provider_blocked"
_SYNC_QDRANT_TIMEOUT = "sync_qdrant_timeout"
_SYNC_MEILI_CONFLICT = "sync_meili_conflict"
_SYNC_MEILI_MALFORMED_PAYLOAD = "sync_meili_malformed_payload"
_SYNC_MEILI_PROVIDER_BLOCKED = "sync_meili_provider_blocked"
_SYNC_MEILI_TIMEOUT = "sync_meili_timeout"


class _BatchResultProtocol(Protocol):
    @property
    def scanned(self) -> int: ...

    @property
    def updated(self) -> int: ...

    @property
    def failed(self) -> int: ...

    @property
    def skipped(self) -> int: ...

    @property
    def duration_seconds(self) -> float: ...


@dataclass(frozen=True, slots=True)
class SearchIndexBatchJobResult:
    """Aggregate outcome for one scheduler search-index sync run."""

    scanned: int
    updated: int
    failed: int
    skipped: int
    duration_seconds: float
    index_sync_unsynced_count: int = 0
    index_sync_failed_count: int = 0
    index_sync_processing_count: int = 0
    index_sync_oldest_lag_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SeoBacklogBatchJobResult:
    """Aggregate outcome for one scheduler SEO backlog run."""

    scanned: int
    updated: int
    failed: int
    skipped: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _SearchIndexBacklogSnapshot:
    index_sync_unsynced_count: int
    index_sync_failed_count: int
    index_sync_processing_count: int
    index_sync_oldest_lag_seconds: float | None


@dataclass(frozen=True, slots=True)
class _ClaimedSyncTarget:
    snapshot_id: uuid.UUID
    meme_file_id: uuid.UUID
    target: SyncTargetKind
    attempt_count: int


@dataclass(frozen=True, slots=True)
class _SeoCandidateOutcome:
    meme_id: uuid.UUID
    status: str


class SearchIndexBatchJobService:
    """Claim and sync bounded search-index work from durable snapshot rows."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        settings: Settings | None = None,
        qdrant_client: QdrantSyncClientProtocol | None = None,
        meilisearch_client: MeilisearchSyncClientProtocol | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._qdrant_client = qdrant_client or PipelineQdrantSyncClient(settings=self._settings)
        self._meilisearch_client = meilisearch_client or PipelineMeilisearchSyncClient(settings=self._settings)

    async def run(self) -> SearchIndexBatchJobResult:
        start_seconds = time.perf_counter()
        scanned = 0
        updated = 0
        failed = 0
        skipped = 0

        for target in _SYNC_TARGETS:
            claims = await self._claim_target_batch(target)
            scanned += len(claims)
            for claim in claims:
                outcome = await self._process_claim(claim)
                if outcome == "updated":
                    updated += 1
                elif outcome == "failed":
                    failed += 1
                else:
                    skipped += 1

        backlog = await self._load_backlog_snapshot()
        return SearchIndexBatchJobResult(
            scanned=scanned,
            updated=updated,
            failed=failed,
            skipped=skipped,
            duration_seconds=time.perf_counter() - start_seconds,
            index_sync_unsynced_count=backlog.index_sync_unsynced_count,
            index_sync_failed_count=backlog.index_sync_failed_count,
            index_sync_processing_count=backlog.index_sync_processing_count,
            index_sync_oldest_lag_seconds=backlog.index_sync_oldest_lag_seconds,
        )

    async def _load_backlog_snapshot(self) -> _SearchIndexBacklogSnapshot:
        lag_statuses = (SyncTargetStatus.PENDING, SyncTargetStatus.FAILED, SyncTargetStatus.PROCESSING)
        stmt = select(
            func.coalesce(
                func.sum(case((MemeFileSyncTargetSnapshot.status.in_(lag_statuses), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((MemeFileSyncTargetSnapshot.status == SyncTargetStatus.FAILED, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((MemeFileSyncTargetSnapshot.status == SyncTargetStatus.PROCESSING, 1), else_=0)),
                0,
            ),
            func.min(
                case(
                    (MemeFileSyncTargetSnapshot.status.in_(lag_statuses), MemeFileSyncTargetSnapshot.updated_at),
                    else_=None,
                )
            ),
        )
        async with self._session_factory() as session:
            unsynced_count, failed_count, processing_count, oldest_updated_at = (await session.execute(stmt)).one()
            stale_primary_payload_count, stale_primary_payload_oldest = (
                await session.execute(
                    select(
                        func.count(),
                        func.min(MemeFileSyncTargetSnapshot.updated_at),
                    )
                    .select_from(MemeFileSyncTargetSnapshot)
                    .join(MemeFile, MemeFile.id == MemeFileSyncTargetSnapshot.meme_file_id)
                    .join(Meme, Meme.id == MemeFile.meme_id)
                    .where(
                        MemeFileSyncTargetSnapshot.sync_target == SyncTargetKind.QDRANT,
                        MemeFileSyncTargetSnapshot.status == SyncTargetStatus.SYNCED,
                        MemeFile.status == ContentProcessingStatus.READY,
                        MemeFileSyncTargetSnapshot.last_payload_preview["is_primary_file"]
                        .as_boolean()
                        .is_distinct_from(Meme.primary_file_id == MemeFile.id),
                    )
                )
            ).one()

        unsynced_count = int(unsynced_count or 0) + int(stale_primary_payload_count or 0)
        if isinstance(stale_primary_payload_oldest, datetime) and (
            not isinstance(oldest_updated_at, datetime)
            or stale_primary_payload_oldest < oldest_updated_at
        ):
            oldest_updated_at = stale_primary_payload_oldest

        oldest_lag_seconds = None
        if isinstance(oldest_updated_at, datetime):
            oldest_lag_seconds = max((utcnow() - oldest_updated_at).total_seconds(), 0.0)
        return _SearchIndexBacklogSnapshot(
            index_sync_unsynced_count=unsynced_count,
            index_sync_failed_count=int(failed_count or 0),
            index_sync_processing_count=int(processing_count or 0),
            index_sync_oldest_lag_seconds=oldest_lag_seconds,
        )

    async def _claim_target_batch(self, target: SyncTargetKind) -> list[_ClaimedSyncTarget]:
        batch_size = self._settings.scheduler_search_index_sync_batch_size
        claims = await self._claim_existing_target_snapshots(target, limit=batch_size)
        if len(claims) >= batch_size:
            return claims
        missing_claims = await self._claim_missing_target_snapshots(target, limit=batch_size - len(claims))
        return [*claims, *missing_claims]

    async def _claim_existing_target_snapshots(
        self,
        target: SyncTargetKind,
        *,
        limit: int,
    ) -> list[_ClaimedSyncTarget]:
        if limit <= 0:
            return []

        now = utcnow()
        processing_stale_before = now - timedelta(
            seconds=self._settings.scheduler_search_index_sync_processing_timeout_seconds,
        )
        canonical_clock = _canonical_search_index_clock_expr()
        qdrant_payload_contract_stale = (
            MemeFileSyncTargetSnapshot.last_payload_preview["is_primary_file"]
            .as_boolean()
            .is_distinct_from(Meme.primary_file_id == MemeFile.id)
            if target is SyncTargetKind.QDRANT
            else literal(False)
        )
        status_priority = case(
            (MemeFileSyncTargetSnapshot.status == SyncTargetStatus.FAILED, 0),
            (MemeFileSyncTargetSnapshot.status == SyncTargetStatus.PENDING, 1),
            (MemeFileSyncTargetSnapshot.status == SyncTargetStatus.PROCESSING, 2),
            else_=3,
        )
        stmt = (
            select(MemeFileSyncTargetSnapshot)
            .join(MemeFile, MemeFile.id == MemeFileSyncTargetSnapshot.meme_file_id)
            .join(Meme, Meme.id == MemeFile.meme_id)
            .outerjoin(MemeSeoPage, MemeSeoPage.meme_id == Meme.id)
            .outerjoin(MemeTemplate, MemeTemplate.id == Meme.template_id)
            .where(
                MemeFileSyncTargetSnapshot.sync_target == target,
                MemeFile.status == ContentProcessingStatus.READY,
                or_(
                    MemeFileSyncTargetSnapshot.status.in_((SyncTargetStatus.PENDING, SyncTargetStatus.FAILED)),
                    and_(
                        MemeFileSyncTargetSnapshot.status == SyncTargetStatus.PROCESSING,
                        or_(
                            MemeFileSyncTargetSnapshot.last_attempt_at.is_(None),
                            MemeFileSyncTargetSnapshot.last_attempt_at <= processing_stale_before,
                        ),
                    ),
                    and_(
                        MemeFileSyncTargetSnapshot.status == SyncTargetStatus.SYNCED,
                        or_(
                            MemeFileSyncTargetSnapshot.last_success_at.is_(None),
                            canonical_clock > MemeFileSyncTargetSnapshot.last_success_at,
                            qdrant_payload_contract_stale,
                        ),
                    ),
                ),
            )
            .order_by(status_priority, MemeFileSyncTargetSnapshot.updated_at, MemeFileSyncTargetSnapshot.id)
            .limit(limit)
            .with_for_update(of=MemeFileSyncTargetSnapshot, skip_locked=True)
        )

        async with self._session_factory() as session, session.begin():
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                row.status = SyncTargetStatus.PROCESSING
                row.normalized_reason = None
                row.last_error_text = None
                row.last_attempt_at = now
                row.attempt_count = row.attempt_count + 1

        return [
            _ClaimedSyncTarget(
                snapshot_id=row.id,
                meme_file_id=row.meme_file_id,
                target=target,
                attempt_count=row.attempt_count,
            )
            for row in rows
        ]

    async def _claim_missing_target_snapshots(
        self,
        target: SyncTargetKind,
        *,
        limit: int,
    ) -> list[_ClaimedSyncTarget]:
        if limit <= 0:
            return []

        now = utcnow()
        existing_snapshot = (
            select(MemeFileSyncTargetSnapshot.id)
            .where(
                MemeFileSyncTargetSnapshot.meme_file_id == MemeFile.id,
                MemeFileSyncTargetSnapshot.sync_target == target,
            )
            .exists()
        )
        stmt = (
            select(MemeFile.id)
            .join(Meme, Meme.id == MemeFile.meme_id)
            .where(
                MemeFile.status == ContentProcessingStatus.READY,
                ~existing_snapshot,
            )
            .order_by(MemeFile.updated_at, MemeFile.id)
            .limit(limit)
            .with_for_update(of=MemeFile, skip_locked=True)
        )

        async with self._session_factory() as session, session.begin():
            meme_file_ids = list((await session.execute(stmt)).scalars().all())
            if not meme_file_ids:
                return []

            insert_stmt = (
                pg_insert(MemeFileSyncTargetSnapshot)
                .values(
                    [
                        {
                            "meme_file_id": meme_file_id,
                            "sync_target": target,
                            "status": SyncTargetStatus.PROCESSING,
                            "normalized_reason": None,
                            "last_error_text": None,
                            "last_payload_preview": {},
                            "last_success_at": None,
                            "last_attempt_at": now,
                            "attempt_count": 1,
                        }
                        for meme_file_id in meme_file_ids
                    ]
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        MemeFileSyncTargetSnapshot.meme_file_id,
                        MemeFileSyncTargetSnapshot.sync_target,
                    )
                )
                .returning(MemeFileSyncTargetSnapshot.id, MemeFileSyncTargetSnapshot.meme_file_id)
            )
            inserted_rows = (await session.execute(insert_stmt)).all()

        return [
            _ClaimedSyncTarget(
                snapshot_id=snapshot_id,
                meme_file_id=meme_file_id,
                target=target,
                attempt_count=1,
            )
            for snapshot_id, meme_file_id in inserted_rows
        ]

    async def _process_claim(self, claim: _ClaimedSyncTarget) -> str:
        try:
            preview_fields = await self._upsert_claimed_target(claim)
        except Exception as exc:  # noqa: BLE001 - every external/canonical failure is recorded on the snapshot.
            return "failed" if await self._record_target_failure(claim, exc) else "skipped"

        return "updated" if await self._record_target_success(claim, preview_fields) else "skipped"

    async def _upsert_claimed_target(self, claim: _ClaimedSyncTarget) -> dict[str, object]:
        if claim.target is SyncTargetKind.QDRANT:
            async with self._session_factory() as session:
                loaded_state = await load_search_index_state(
                    session,
                    claim.meme_file_id,
                    vector_dimensions=self._settings.pipeline_voyage_output_dimensions,
                )
            if loaded_state.vector is None:
                raise PipelineIngestError(
                    f"Search-index sync could not find an embedding vector for {claim.meme_file_id}.",
                )
            payload = build_qdrant_sync_payload(loaded_state.canonical)
            await self._qdrant_client.upsert_meme_point(payload=payload, vector=loaded_state.vector)
            return _qdrant_preview_fields(payload)

        async with self._session_factory() as session:
            loaded_state = await load_search_index_state(session, claim.meme_file_id)
        document = build_meilisearch_document(loaded_state.canonical)
        await self._meilisearch_client.upsert_document(document)
        return _meilisearch_preview_fields(document)

    async def _record_target_success(
        self,
        claim: _ClaimedSyncTarget,
        preview_fields: Mapping[str, object],
    ) -> bool:
        now = utcnow()
        preview = build_sync_preview_model(preview_fields, target=claim.target)
        preview_json = preview.model_dump(mode="json")
        stmt = (
            update(MemeFileSyncTargetSnapshot)
            .where(
                MemeFileSyncTargetSnapshot.id == claim.snapshot_id,
                MemeFileSyncTargetSnapshot.status == SyncTargetStatus.PROCESSING,
                MemeFileSyncTargetSnapshot.attempt_count == claim.attempt_count,
            )
            .values(
                status=SyncTargetStatus.SYNCED,
                normalized_reason=None,
                last_error_text=None,
                last_payload_preview=preview_json,
                last_success_at=now,
                last_attempt_at=now,
            )
            .returning(MemeFileSyncTargetSnapshot.id)
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(stmt)
            finalized_snapshot_id = result.scalar_one_or_none()
        return finalized_snapshot_id is not None

    async def _record_target_failure(self, claim: _ClaimedSyncTarget, exc: Exception) -> bool:
        now = utcnow()
        stmt = (
            update(MemeFileSyncTargetSnapshot)
            .where(
                MemeFileSyncTargetSnapshot.id == claim.snapshot_id,
                MemeFileSyncTargetSnapshot.status == SyncTargetStatus.PROCESSING,
                MemeFileSyncTargetSnapshot.attempt_count == claim.attempt_count,
            )
            .values(
                status=SyncTargetStatus.FAILED,
                normalized_reason=trim_reason(_normalize_sync_failure_reason(claim.target, exc)),
                last_error_text=trim_error_text(str(exc) or type(exc).__name__),
                last_attempt_at=now,
            )
            .returning(MemeFileSyncTargetSnapshot.id)
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(stmt)
            finalized_snapshot_id = result.scalar_one_or_none()
        return finalized_snapshot_id is not None


class SeoBacklogBatchJobService:
    """Generate bounded public-safe SEO backlog work under per-meme row locks."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        settings: Settings | None = None,
        provider: MemeSeoProviderProtocol | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._provider = provider or build_meme_seo_provider(settings=self._settings)

    async def run(self) -> SeoBacklogBatchJobResult:
        start_seconds = time.perf_counter()
        scanned = 0
        updated = 0
        failed = 0
        skipped = 0
        attempted_meme_ids: set[uuid.UUID] = set()

        for _ in range(self._settings.scheduler_seo_backlog_batch_size):
            outcome = await self._process_next_candidate(attempted_meme_ids)
            if outcome is None:
                break
            attempted_meme_ids.add(outcome.meme_id)
            scanned += 1
            if outcome.status == "updated":
                updated += 1
            elif outcome.status == "failed":
                failed += 1
            else:
                skipped += 1

        return SeoBacklogBatchJobResult(
            scanned=scanned,
            updated=updated,
            failed=failed,
            skipped=skipped,
            duration_seconds=time.perf_counter() - start_seconds,
        )

    async def _process_next_candidate(self, attempted_meme_ids: set[uuid.UUID]) -> _SeoCandidateOutcome | None:
        claimed: tuple[uuid.UUID, bool] | None = None
        try:
            async with self._session_factory() as session, session.begin():
                claimed = await self._claim_next_seo_candidate(session, attempted_meme_ids=attempted_meme_ids)
                if claimed is None:
                    return None
                meme_id, force = claimed
                service = MemeSeoGenerationService(
                    session,
                    provider=self._provider,
                    settings=self._settings,
                )
                result = await service.generate_for_meme_id(meme_id, force=force, commit=False)
        except Exception:
            if claimed is None:
                return None
            return _SeoCandidateOutcome(meme_id=claimed[0], status="failed")

        if result.status == "generated":
            return _SeoCandidateOutcome(meme_id=meme_id, status="updated")
        if result.status == "failed":
            return _SeoCandidateOutcome(meme_id=meme_id, status="failed")
        return _SeoCandidateOutcome(meme_id=meme_id, status="skipped")

    async def _claim_next_seo_candidate(
        self,
        session: AsyncSession,
        *,
        attempted_meme_ids: set[uuid.UUID],
    ) -> tuple[uuid.UUID, bool] | None:
        missing_rank = 0
        stale_rank = 1
        backlog_rank = case((MemeSeoPage.meme_id.is_(None), missing_rank), else_=stale_rank).label("backlog_rank")
        stmt = (
            select(Meme.id, backlog_rank)
            .outerjoin(MemeSeoPage, MemeSeoPage.meme_id == Meme.id)
            .where(
                Meme.is_public.is_(True),
                Meme.is_nsfw.is_(False),
                or_(
                    MemeSeoPage.meme_id.is_(None),
                    and_(
                        MemeSeoPage.prompt_version != self._settings.pipeline_seo_prompt_version,
                        MemeSeoPage.edited_at.is_(None),
                    ),
                ),
            )
            .order_by(backlog_rank, Meme.created_at, Meme.id)
            .limit(1)
            .with_for_update(of=Meme, skip_locked=True)
        )
        if attempted_meme_ids:
            stmt = stmt.where(Meme.id.not_in(attempted_meme_ids))
        row = (await session.execute(stmt)).one_or_none()
        if row is None:
            return None
        meme_id, rank = row
        return meme_id, rank == stale_rank


async def run_scheduler_search_index_sync_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings,
    qdrant_client: QdrantSyncClientProtocol | None = None,
    meilisearch_client: MeilisearchSyncClientProtocol | None = None,
) -> SearchIndexBatchJobResult:
    service = SearchIndexBatchJobService(
        session_factory,
        settings=settings,
        qdrant_client=qdrant_client,
        meilisearch_client=meilisearch_client,
    )
    return await service.run()


async def run_scheduler_seo_backlog_batch(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings,
    provider: MemeSeoProviderProtocol | None = None,
) -> SeoBacklogBatchJobResult:
    service = SeoBacklogBatchJobService(session_factory, settings=settings, provider=provider)
    return await service.run()


def scheduler_batch_result_log_extra(job_id: str, result: _BatchResultProtocol) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "status": "completed",
        "degraded_mode": result.failed > 0,
        "scanned": result.scanned,
        "updated": result.updated,
        "failed": result.failed,
        "skipped": result.skipped,
        "duration_seconds": result.duration_seconds,
    }
    for field_name in (
        "index_sync_unsynced_count",
        "index_sync_failed_count",
        "index_sync_processing_count",
        "index_sync_oldest_lag_seconds",
    ):
        field_value = getattr(result, field_name, None)
        if field_value is not None:
            extra[field_name] = field_value
    return extra


def _canonical_search_index_clock_expr() -> object:
    collection_updated_at = (
        select(func.max(Collection.updated_at))
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .where(CollectionMeme.meme_id == Meme.id)
        .correlate(Meme)
        .scalar_subquery()
    )
    collection_meme_added_at = (
        select(func.max(CollectionMeme.added_at))
        .where(CollectionMeme.meme_id == Meme.id)
        .correlate(Meme)
        .scalar_subquery()
    )
    collection_member_joined_at = (
        select(func.max(CollectionMember.joined_at))
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .join(CollectionMember, CollectionMember.collection_id == Collection.id)
        .where(CollectionMeme.meme_id == Meme.id)
        .correlate(Meme)
        .scalar_subquery()
    )
    earliest = literal(_EARLIEST_SYNC_CLOCK)
    return func.greatest(
        Meme.updated_at,
        MemeFile.updated_at,
        func.coalesce(MemeSeoPage.edited_at, MemeSeoPage.generated_at, earliest),
        func.coalesce(MemeTemplate.updated_at, earliest),
        func.coalesce(collection_updated_at, earliest),
        func.coalesce(collection_meme_added_at, earliest),
        func.coalesce(collection_member_joined_at, earliest),
    )


def _qdrant_preview_fields(payload: QdrantSyncPayload) -> dict[str, object]:
    return _bounded_preview_fields(
        {
            "meme_id": payload.meme_id,
            "meme_file_id": payload.meme_file_id,
            "search_index_algorithm_version": payload.search_index_algorithm_version,
            "is_public": payload.is_public,
            "is_primary_file": payload.is_primary_file,
            "uploader_user_ids": payload.uploader_user_ids,
            "media_type": payload.media_type,
            "language": payload.language,
            "is_nsfw": payload.is_nsfw,
            "seo_page_slug": payload.seo_page_slug,
            "template_id": payload.template_id,
            "template_slug": payload.template_slug,
            "popularity_score": payload.popularity_score,
            "like_count": payload.like_count,
            "created_at": payload.created_at,
            "updated_at": payload.updated_at,
            "tags": payload.tags,
            "collection_ids": payload.collection_ids,
            "public_collection_ids": payload.public_collection_ids,
            "unlisted_collection_ids": payload.unlisted_collection_ids,
            "private_collection_ids": payload.private_collection_ids,
            "shared_collection_ids": payload.shared_collection_ids,
            "collection_owner_user_ids": payload.collection_owner_user_ids,
            "collection_member_user_ids": payload.collection_member_user_ids,
            "ocr_snippet": payload.ocr_snippet,
            "quality_score": payload.quality_score,
            "source_object_key": payload.source_object_key,
        }
    )


def _meilisearch_preview_fields(document: PipelineMeilisearchDocument) -> dict[str, object]:
    return _bounded_preview_fields(
        {
            "id": document.id,
            "meme_id": document.meme_id,
            "meme_file_id": document.meme_file_id,
            "search_index_algorithm_version": document.search_index_algorithm_version,
            "is_public": document.is_public,
            "media_type": document.media_type,
            "language": document.language,
            "is_nsfw": document.is_nsfw,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
            "tags": document.tags,
            "seo_page_slug": document.seo_page_slug,
            "template_id": document.template_id,
            "template_slug": document.template_slug,
            "popularity_score": document.popularity_score,
            "like_count": document.like_count,
            "quality_score": document.quality_score,
            "collection_ids": document.collection_ids,
            "public_collection_ids": document.public_collection_ids,
            "unlisted_collection_ids": document.unlisted_collection_ids,
            "private_collection_ids": document.private_collection_ids,
            "shared_collection_ids": document.shared_collection_ids,
            "collection_owner_user_ids": document.collection_owner_user_ids,
            "collection_member_user_ids": document.collection_member_user_ids,
            "ocr_text": document.ocr_text,
        }
    )


def _bounded_preview_fields(raw_fields: Mapping[str, object]) -> dict[str, object]:
    return {key: _bounded_preview_value(value) for key, value in raw_fields.items() if value is not None}


def _bounded_preview_value(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value[:_MAX_PREVIEW_TEXT_LENGTH]
    if isinstance(value, list):
        return [_bounded_preview_value(item) for item in value[:_MAX_PREVIEW_LIST_LENGTH]]
    if isinstance(value, tuple):
        return [_bounded_preview_value(item) for item in value[:_MAX_PREVIEW_LIST_LENGTH]]
    return value


def _normalize_sync_failure_reason(target: SyncTargetKind, exc: Exception) -> str:
    if target is SyncTargetKind.QDRANT:
        if isinstance(exc, QdrantSyncTimeoutError):
            return _SYNC_QDRANT_TIMEOUT
        if isinstance(exc, QdrantSyncConflictError):
            return _SYNC_QDRANT_CONFLICT
        if isinstance(exc, QdrantSyncMalformedResponseError):
            return _SYNC_QDRANT_MALFORMED_PAYLOAD
        if isinstance(exc, QdrantSyncProviderUnavailableError):
            return _SYNC_QDRANT_PROVIDER_BLOCKED
        return _SYNC_QDRANT_PROVIDER_BLOCKED

    if isinstance(exc, MeilisearchSyncTimeoutError):
        return _SYNC_MEILI_TIMEOUT
    if isinstance(exc, MeilisearchSyncConflictError):
        return _SYNC_MEILI_CONFLICT
    if isinstance(exc, MeilisearchSyncMalformedResponseError):
        return _SYNC_MEILI_MALFORMED_PAYLOAD
    if isinstance(exc, MeilisearchSyncProviderUnavailableError):
        return _SYNC_MEILI_PROVIDER_BLOCKED
    return _SYNC_MEILI_PROVIDER_BLOCKED


__all__ = [
    "SearchIndexBatchJobResult",
    "SearchIndexBatchJobService",
    "SeoBacklogBatchJobResult",
    "SeoBacklogBatchJobService",
    "run_scheduler_search_index_sync_batch",
    "run_scheduler_seo_backlog_batch",
    "scheduler_batch_result_log_extra",
]
