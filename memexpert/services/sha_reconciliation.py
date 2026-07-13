"""Resumable exact-SHA duplicate reconciliation for the provenance migration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, text, update

from memexpert.ingest.policy import refresh_effective_visibility, resolve_visibility_mode
from memexpert.ingest.sha_dedupe import acquire_sha256_advisory_lock
from memexpert.models.collection import Collection, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemeOfTheDaySelection,
    MemeSeoPage,
    MemeSource,
    ModerationDecision,
    ModerationReport,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    TelegramFileIdCache,
)
from memexpert.models.enums import CollectionKind, ContentProcessingStatus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

MERGE_REASON_SHA256_RECONCILIATION = "sha256_reconciliation"


@dataclass(frozen=True, slots=True)
class ShaReconciliationResult:
    """One committed SHA-group reconciliation summary."""

    sha256_hex: str
    canonical_meme_id: uuid.UUID
    canonical_meme_file_id: uuid.UUID
    obsolete_meme_ids: tuple[uuid.UUID, ...]
    obsolete_meme_file_ids: tuple[uuid.UUID, ...]


class ShaDuplicateReconciliationService:
    """Merge one duplicate hash group at a time inside the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_next_duplicate_sha(self) -> str | None:
        """Return the lexicographically next currently duplicated non-null SHA."""

        return await self._session.scalar(
            select(MemeFile.sha256_hex)
            .where(MemeFile.sha256_hex.is_not(None))
            .group_by(MemeFile.sha256_hex)
            .having(func.count(MemeFile.id) > 1)
            .order_by(MemeFile.sha256_hex)
            .limit(1)
        )

    async def reconcile_next(self) -> ShaReconciliationResult | None:
        """Reconcile the next duplicate group, returning ``None`` when complete."""

        sha256_hex = await self.find_next_duplicate_sha()
        if sha256_hex is None:
            return None
        return await self.reconcile_sha(sha256_hex)

    async def reconcile_sha(self, sha256_hex: str) -> ShaReconciliationResult:
        """Reconcile one exact hash under an advisory lock and deterministic row locks."""

        await acquire_sha256_advisory_lock(self._session, sha256_hex)
        files = list(
            (
                await self._session.execute(
                    select(MemeFile)
                    .where(MemeFile.sha256_hex == sha256_hex)
                    .order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
                    .with_for_update()
                )
            ).scalars()
        )
        if not files:
            raise ValueError(f"SHA group {sha256_hex} no longer exists.")

        memes = list(
            (
                await self._session.execute(
                    select(Meme)
                    .where(Meme.id.in_({file.meme_id for file in files}))
                    .order_by(Meme.created_at.asc(), Meme.id.asc())
                    .with_for_update()
                )
            ).scalars()
        )
        canonical_file = min(
            files,
            key=lambda file: (
                file.blocked_perceptual_hash_id is None,
                file.status is not ContentProcessingStatus.READY,
                file.created_at,
                file.id,
            ),
        )
        canonical_meme = memes[0]
        obsolete_files = tuple(file for file in files if file.id != canonical_file.id)
        obsolete_meme_ids = tuple(meme.id for meme in memes if meme.id != canonical_meme.id)

        if not obsolete_files:
            return ShaReconciliationResult(
                sha256_hex=sha256_hex,
                canonical_meme_id=canonical_meme.id,
                canonical_meme_file_id=canonical_file.id,
                obsolete_meme_ids=(),
                obsolete_meme_file_ids=(),
            )

        details = await self._build_merge_details(
            sha256_hex=sha256_hex,
            canonical_meme=canonical_meme,
            canonical_file=canonical_file,
            obsolete_files=obsolete_files,
            obsolete_meme_ids=obsolete_meme_ids,
        )
        for obsolete_file in obsolete_files:
            self._session.add(
                MemeMergeLog(
                    source_meme_id=obsolete_file.meme_id,
                    source_meme_file_id=obsolete_file.id,
                    target_meme_id=canonical_meme.id,
                    target_primary_file_id=canonical_file.id,
                    similarity_score=1.0,
                    merge_reason=MERGE_REASON_SHA256_RECONCILIATION,
                    details=details,
                )
            )
        await self._session.flush()

        canonical_meme.visibility_mode = resolve_visibility_mode(meme.visibility_mode for meme in memes)
        canonical_meme.is_nsfw = any(meme.is_nsfw for meme in memes)
        canonical_meme.tags = list(dict.fromkeys(tag for meme in memes for tag in meme.tags))
        if canonical_meme.ocr_text is None:
            canonical_meme.ocr_text = next((meme.ocr_text for meme in memes if meme.ocr_text), None)
        if canonical_meme.template_id is None:
            canonical_meme.template_id = next((meme.template_id for meme in memes if meme.template_id), None)

        canonical_file.meme_id = canonical_meme.id
        canonical_meme.primary_file_id = canonical_file.id
        await self._session.flush()

        for obsolete_file in obsolete_files:
            await self._transfer_file_dependencies(obsolete_file.id, canonical_file.id)

        obsolete_file_ids = tuple(file.id for file in obsolete_files)
        if obsolete_meme_ids:
            await self._session.execute(
                update(MemeFile)
                .where(
                    MemeFile.meme_id.in_(obsolete_meme_ids),
                    MemeFile.id.not_in(obsolete_file_ids),
                )
                .values(meme_id=canonical_meme.id)
            )
            for source_meme_id in obsolete_meme_ids:
                await self._transfer_meme_dependencies(source_meme_id, canonical_meme.id)

        await self._session.flush()

        await self._session.execute(delete(MemeFile).where(MemeFile.id.in_(obsolete_file_ids)))
        if obsolete_meme_ids:
            await self._session.execute(delete(Meme).where(Meme.id.in_(obsolete_meme_ids)))

        canonical_meme.primary_file_id = await self._select_primary_file(canonical_meme.id)
        await self._recompute_like_count(canonical_meme)
        await refresh_effective_visibility(self._session, canonical_meme)
        await self._session.flush()

        return ShaReconciliationResult(
            sha256_hex=sha256_hex,
            canonical_meme_id=canonical_meme.id,
            canonical_meme_file_id=canonical_file.id,
            obsolete_meme_ids=obsolete_meme_ids,
            obsolete_meme_file_ids=obsolete_file_ids,
        )

    async def _transfer_file_dependencies(self, source_file_id: uuid.UUID, target_file_id: uuid.UUID) -> None:
        await self._session.execute(
            update(MemeSource).where(MemeSource.file_id == source_file_id).values(file_id=target_file_id)
        )
        await self._session.execute(
            update(EmbeddingCache)
            .where(EmbeddingCache.source_file_id == source_file_id)
            .values(source_file_id=target_file_id)
        )
        await self._session.execute(
            update(PipelineIngestRequest)
            .where(PipelineIngestRequest.materialized_meme_file_id == source_file_id)
            .values(materialized_meme_file_id=target_file_id)
        )
        await self._session.execute(
            update(PipelineIngestRequest)
            .where(PipelineIngestRequest.matched_meme_file_id == source_file_id)
            .values(matched_meme_file_id=target_file_id)
        )
        await self._session.execute(
            update(MemeFile)
            .where(MemeFile.matched_meme_file_id == source_file_id)
            .values(matched_meme_file_id=target_file_id)
        )
        await self._session.execute(
            update(MemeSource)
            .where(MemeSource.matched_meme_file_id == source_file_id)
            .values(matched_meme_file_id=target_file_id)
        )
        await self._transfer_file_outbox_references(source_file_id, target_file_id)
        await self._transfer_stage_rows(source_file_id, target_file_id)
        await self._transfer_sync_rows(source_file_id, target_file_id)
        await self._transfer_ocr_row(source_file_id, target_file_id)
        await self._transfer_telegram_cache_rows(source_file_id, target_file_id)

    async def _transfer_stage_rows(self, source_file_id: uuid.UUID, target_file_id: uuid.UUID) -> None:
        target_stages = set(
            (
                await self._session.execute(
                    select(PipelineStageJournal.stage).where(PipelineStageJournal.meme_file_id == target_file_id)
                )
            ).scalars()
        )
        source_rows = list(
            (
                await self._session.execute(
                    select(PipelineStageJournal).where(PipelineStageJournal.meme_file_id == source_file_id)
                )
            ).scalars()
        )
        for row in source_rows:
            if row.stage in target_stages:
                await self._session.delete(row)
            else:
                row.meme_file_id = target_file_id
        await self._session.flush()

    async def _transfer_sync_rows(self, source_file_id: uuid.UUID, target_file_id: uuid.UUID) -> None:
        target_kinds = set(
            (
                await self._session.execute(
                    select(MemeFileSyncTargetSnapshot.sync_target).where(
                        MemeFileSyncTargetSnapshot.meme_file_id == target_file_id
                    )
                )
            ).scalars()
        )
        source_rows = list(
            (
                await self._session.execute(
                    select(MemeFileSyncTargetSnapshot).where(
                        MemeFileSyncTargetSnapshot.meme_file_id == source_file_id
                    )
                )
            ).scalars()
        )
        for row in source_rows:
            if row.sync_target in target_kinds:
                await self._session.delete(row)
            else:
                row.meme_file_id = target_file_id
        await self._session.flush()

    async def _transfer_ocr_row(self, source_file_id: uuid.UUID, target_file_id: uuid.UUID) -> None:
        target_row = await self._session.scalar(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == target_file_id)
        )
        source_row = await self._session.scalar(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == source_file_id)
        )
        if source_row is None:
            return
        if target_row is None:
            source_row.meme_file_id = target_file_id
        else:
            await self._session.delete(source_row)
        await self._session.flush()

    async def _transfer_telegram_cache_rows(self, source_file_id: uuid.UUID, target_file_id: uuid.UUID) -> None:
        target_keys = set(
            (
                await self._session.execute(
                    select(TelegramFileIdCache.media_format, TelegramFileIdCache.bot_scope).where(
                        TelegramFileIdCache.meme_file_id == target_file_id
                    )
                )
            ).all()
        )
        source_rows = list(
            (
                await self._session.execute(
                    select(TelegramFileIdCache).where(TelegramFileIdCache.meme_file_id == source_file_id)
                )
            ).scalars()
        )
        for row in source_rows:
            if (row.media_format, row.bot_scope) in target_keys:
                await self._session.delete(row)
            else:
                row.meme_file_id = target_file_id
        await self._session.flush()

    async def _transfer_meme_dependencies(self, source_meme_id: uuid.UUID, target_meme_id: uuid.UUID) -> None:
        await self._transfer_collection_rows(source_meme_id, target_meme_id)
        await self._transfer_pin_rows(source_meme_id, target_meme_id)
        await self._session.execute(
            update(ModerationReport).where(ModerationReport.meme_id == source_meme_id).values(meme_id=target_meme_id)
        )
        await self._session.execute(
            update(ModerationDecision)
            .where(ModerationDecision.meme_id == source_meme_id)
            .values(meme_id=target_meme_id)
        )
        await self._session.execute(
            update(MemeOfTheDaySelection)
            .where(MemeOfTheDaySelection.meme_id == source_meme_id)
            .values(meme_id=target_meme_id)
        )
        await self._session.execute(
            update(PipelineIngestRequest)
            .where(PipelineIngestRequest.materialized_meme_id == source_meme_id)
            .values(materialized_meme_id=target_meme_id)
        )
        await self._transfer_meme_outbox_references(source_meme_id, target_meme_id)
        await self._transfer_seo_page(source_meme_id, target_meme_id)
        await self._transfer_analytics_history(source_meme_id, target_meme_id)

    async def _transfer_file_outbox_references(
        self,
        source_file_id: uuid.UUID,
        target_file_id: uuid.UUID,
    ) -> None:
        source_id = str(source_file_id)
        target_id = str(target_file_id)
        await self._session.execute(
            update(RabbitMQOutboxMessage)
            .where(
                RabbitMQOutboxMessage.aggregate_type == "meme_file",
                RabbitMQOutboxMessage.aggregate_id == source_id,
            )
            .values(aggregate_id=target_id)
        )
        await self._session.execute(
            update(RabbitMQOutboxMessage)
            .where(RabbitMQOutboxMessage.ordering_key == source_id)
            .values(ordering_key=target_id)
        )
        for field_name in ("meme_file_id", "matched_meme_file_id", "materialized_meme_file_id"):
            await self._rewrite_outbox_payload_id(field_name, source_id=source_id, target_id=target_id)

    async def _transfer_meme_outbox_references(
        self,
        source_meme_id: uuid.UUID,
        target_meme_id: uuid.UUID,
    ) -> None:
        source_id = str(source_meme_id)
        target_id = str(target_meme_id)
        for field_name in ("meme_id", "duplicate_of_meme_id", "materialized_meme_id"):
            await self._rewrite_outbox_payload_id(field_name, source_id=source_id, target_id=target_id)

    async def _rewrite_outbox_payload_id(
        self,
        field_name: str,
        *,
        source_id: str,
        target_id: str,
    ) -> None:
        await self._session.execute(
            text(
                f"""
                UPDATE rabbitmq_outbox_messages
                SET payload = jsonb_set(
                    payload,
                    '{{{field_name}}}',
                    to_jsonb(CAST(:target_id AS text)),
                    false
                )
                WHERE payload ->> '{field_name}' = :source_id
                """
            ),
            {"source_id": source_id, "target_id": target_id},
        )

    async def _transfer_collection_rows(self, source_meme_id: uuid.UUID, target_meme_id: uuid.UUID) -> None:
        target_collections = set(
            (
                await self._session.execute(
                    select(CollectionMeme.collection_id).where(CollectionMeme.meme_id == target_meme_id)
                )
            ).scalars()
        )
        rows = list(
            (
                await self._session.execute(select(CollectionMeme).where(CollectionMeme.meme_id == source_meme_id))
            ).scalars()
        )
        for row in rows:
            if row.collection_id in target_collections:
                await self._session.delete(row)
            else:
                row.meme_id = target_meme_id
        await self._session.flush()

    async def _transfer_pin_rows(self, source_meme_id: uuid.UUID, target_meme_id: uuid.UUID) -> None:
        target_users = set(
            (
                await self._session.execute(select(PinnedMeme.user_id).where(PinnedMeme.meme_id == target_meme_id))
            ).scalars()
        )
        rows = list(
            (
                await self._session.execute(select(PinnedMeme).where(PinnedMeme.meme_id == source_meme_id))
            ).scalars()
        )
        for row in rows:
            if row.user_id in target_users:
                await self._session.delete(row)
            else:
                row.meme_id = target_meme_id
        await self._session.flush()

    async def _transfer_seo_page(self, source_meme_id: uuid.UUID, target_meme_id: uuid.UUID) -> None:
        source_page = await self._session.get(MemeSeoPage, source_meme_id)
        if source_page is None:
            return
        target_page = await self._session.get(MemeSeoPage, target_meme_id)
        if target_page is None:
            source_page.meme_id = target_meme_id
        else:
            target_page.tags = list(dict.fromkeys([*target_page.tags, *source_page.tags]))
            await self._session.delete(source_page)
        await self._session.flush()

    async def _transfer_analytics_history(self, source_meme_id: uuid.UUID, target_meme_id: uuid.UUID) -> None:
        await self._session.execute(
            text(
                """
                UPDATE analytics_events
                SET payload =
                    CASE WHEN payload ->> 'meme_id' = :source_id
                        THEN jsonb_set(payload, '{meme_id}', to_jsonb(CAST(:target_id AS text)), false)
                        ELSE payload END
                WHERE payload ->> 'meme_id' = :source_id
                """
            ),
            {"source_id": str(source_meme_id), "target_id": str(target_meme_id)},
        )
        await self._session.execute(
            text(
                """
                UPDATE analytics_events
                SET payload = jsonb_set(payload, '{refs,meme_id}', to_jsonb(CAST(:target_id AS text)), false)
                WHERE payload #>> '{refs,meme_id}' = :source_id
                """
            ),
            {"source_id": str(source_meme_id), "target_id": str(target_meme_id)},
        )

    async def _select_primary_file(self, meme_id: uuid.UUID) -> uuid.UUID:
        primary_file_id = await self._session.scalar(
            select(MemeFile.id)
            .where(MemeFile.meme_id == meme_id)
            .order_by(MemeFile.quality_score.desc(), MemeFile.created_at.asc(), MemeFile.id.asc())
            .limit(1)
        )
        if primary_file_id is None:
            raise ValueError(f"Reconciled meme {meme_id} has no files.")
        return primary_file_id

    async def _recompute_like_count(self, meme: Meme) -> None:
        meme.like_count = int(
            await self._session.scalar(
                select(func.count(func.distinct(Collection.owner_id)))
                .select_from(CollectionMeme)
                .join(Collection, Collection.id == CollectionMeme.collection_id)
                .where(CollectionMeme.meme_id == meme.id, Collection.kind == CollectionKind.FAVORITES)
            )
            or 0
        )

    async def _build_merge_details(
        self,
        *,
        sha256_hex: str,
        canonical_meme: Meme,
        canonical_file: MemeFile,
        obsolete_files: tuple[MemeFile, ...],
        obsolete_meme_ids: tuple[uuid.UUID, ...],
    ) -> dict[str, object]:
        return {
            "sha256_hex": sha256_hex,
            "canonical_meme_id": str(canonical_meme.id),
            "canonical_meme_file_id": str(canonical_file.id),
            "obsolete_meme_ids": [str(meme_id) for meme_id in obsolete_meme_ids],
            "obsolete_files": [
                {
                    "meme_file_id": str(file.id),
                    "meme_id": str(file.meme_id),
                    "s3_original_key": file.s3_original_key,
                    "s3_web_video_key": file.s3_web_video_key,
                    "qdrant_point_id": str(file.id),
                    "meilisearch_document_id": file.id.hex,
                }
                for file in obsolete_files
            ],
        }


__all__ = [
    "MERGE_REASON_SHA256_RECONCILIATION",
    "ShaDuplicateReconciliationService",
    "ShaReconciliationResult",
]
