# ruff: noqa: TC003
"""Auto-merge orchestration for post-embed near-duplicate canonicalization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from memexpert.models.collection import CollectionMeme, PinnedMeme
from memexpert.models.content import (
    Meme,
    MemeFile,
    MemeMergeLog,
    MemePopularitySnapshot,
)
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.qdrant import QdrantSimilarityMatch

MERGE_REASON_HIGH_SIMILARITY = "high_similarity_match"
MERGE_REASON_ADMIN = "admin_destructive_merge"


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """Outcome of a post-embed merge evaluation for one meme file."""

    merged: bool
    target_meme_id: uuid.UUID
    source_meme_id: uuid.UUID | None
    similarity_score: float | None
    primary_file_id: uuid.UUID


class ContentMergeService:
    """Apply auto-merge rules to embed-completed meme files.

    The merge path is deliberately transactional: the stage completion service
    opens one durable commit after stage-row truth changes, and the merge writes
    the entire lineage transfer (files, sources, collection membership, pins,
    popularity, merge-audit row) plus the canonical-primary reselection into
    that same session. Any failure raises ``PipelineIngestError`` so the
    enclosing commit is rolled back and the ``embed`` stage stays replayable.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        similarity_threshold: float,
    ) -> None:
        self._session = session
        self._similarity_threshold = similarity_threshold

    async def maybe_merge_after_embed(
        self,
        *,
        meme_file: MemeFile,
        similarity_matches: tuple[QdrantSimilarityMatch, ...],
    ) -> MergeOutcome:
        """Merge the file's meme into an older canonical target when the threshold fires.

        The merge pass deliberately does not touch ``Meme.ocr_text``, ``Meme.language``,
        or ``Meme.is_nsfw`` — those fields are owned by the classify completion path so
        that downstream slices see a single truthful write after embed + merge + classify
        all finish.
        """

        initial_target = await self._evaluate_initial_target(meme_file=meme_file)
        merge_target = await self._select_merge_target(
            meme_file=meme_file,
            similarity_matches=similarity_matches,
            current_meme_created_at=initial_target.created_at,
        )

        if merge_target is None:
            return MergeOutcome(
                merged=False,
                target_meme_id=meme_file.meme_id,
                source_meme_id=None,
                similarity_score=None,
                primary_file_id=initial_target.primary_file_id,
            )

        target_meme, matched_score = merge_target
        return await self._apply_merge(
            source_meme=initial_target,
            source_meme_file_id=meme_file.id,
            target_meme=target_meme,
            similarity_score=matched_score,
            merge_reason=MERGE_REASON_HIGH_SIMILARITY,
        )

    async def merge_memes_for_admin(
        self,
        *,
        source_meme: Meme,
        target_meme: Meme,
        admin_user_id: uuid.UUID,
        note: str,
        affected_snapshot: dict[str, object],
    ) -> MergeOutcome:
        """Merge one explicit source meme into a target using the shared lineage transfer path."""

        return await self._apply_merge(
            source_meme=source_meme,
            source_meme_file_id=source_meme.primary_file_id,
            target_meme=target_meme,
            similarity_score=None,
            merge_reason=MERGE_REASON_ADMIN,
            details={
                "admin_user_id": str(admin_user_id),
                "admin_note": note,
                "affected_snapshot": affected_snapshot,
            },
        )

    async def _evaluate_initial_target(self, *, meme_file: MemeFile) -> Meme:
        result = await self._session.execute(
            select(Meme).where(Meme.id == meme_file.meme_id).with_for_update()
        )
        meme = result.scalar_one_or_none()
        if meme is None:
            raise PipelineIngestError(
                f"Meme {meme_file.meme_id} disappeared before the embed merge transaction ran.",
            )
        return meme

    async def _select_merge_target(
        self,
        *,
        meme_file: MemeFile,
        similarity_matches: tuple[QdrantSimilarityMatch, ...],
        current_meme_created_at: datetime,
    ) -> tuple[Meme, float] | None:
        sorted_matches = sorted(
            (
                match
                for match in similarity_matches
                if match.similarity_score > self._similarity_threshold
                and match.meme_id != meme_file.meme_id
                and match.meme_file_id != meme_file.id
            ),
            key=lambda match: match.similarity_score,
            reverse=True,
        )
        for match in sorted_matches:
            result = await self._session.execute(select(Meme).where(Meme.id == match.meme_id))
            target_meme = result.scalar_one_or_none()
            if target_meme is None:
                continue
            if target_meme.created_at >= current_meme_created_at:
                # Only merge into the *older* canonical meme.
                continue
            return target_meme, match.similarity_score
        return None

    async def _apply_merge(
        self,
        *,
        source_meme: Meme,
        source_meme_file_id: uuid.UUID | None,
        target_meme: Meme,
        similarity_score: float | None,
        merge_reason: str,
        details: dict[str, object] | None = None,
    ) -> MergeOutcome:
        moved_file_ids = await self._transfer_meme_files(source_meme_id=source_meme.id, target_meme_id=target_meme.id)
        await self._transfer_collection_memberships(source_meme_id=source_meme.id, target_meme_id=target_meme.id)
        await self._transfer_pins(source_meme_id=source_meme.id, target_meme_id=target_meme.id)
        await self._transfer_popularity_snapshots(
            source_meme_id=source_meme.id,
            target_meme_id=target_meme.id,
        )
        self._accumulate_popularity(source_meme=source_meme, target_meme=target_meme)
        await self._delete_source_meme(source_meme=source_meme)

        primary_file_id = await self._reselect_primary_file(target_meme.id)

        log_details = dict(details or {})
        log_details["moved_file_ids"] = [str(file_id) for file_id in moved_file_ids]

        self._session.add(
            MemeMergeLog(
                source_meme_id=source_meme.id,
                source_meme_file_id=source_meme_file_id,
                target_meme_id=target_meme.id,
                target_primary_file_id=primary_file_id,
                similarity_score=similarity_score,
                merge_reason=merge_reason,
                details=log_details,
            )
        )
        await self._session.flush()

        return MergeOutcome(
            merged=True,
            target_meme_id=target_meme.id,
            source_meme_id=source_meme.id,
            similarity_score=similarity_score,
            primary_file_id=primary_file_id,
        )

    async def _transfer_meme_files(
        self,
        *,
        source_meme_id: uuid.UUID,
        target_meme_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        result = await self._session.execute(
            select(MemeFile.id).where(MemeFile.meme_id == source_meme_id)
        )
        moved_file_ids = tuple(result.scalars().all())
        if not moved_file_ids:
            return ()

        _ = await self._session.execute(
            update(MemeFile)
            .where(MemeFile.meme_id == source_meme_id)
            .values(meme_id=target_meme_id)
        )
        await self._session.flush()
        return moved_file_ids

    async def _transfer_collection_memberships(
        self,
        *,
        source_meme_id: uuid.UUID,
        target_meme_id: uuid.UUID,
    ) -> None:
        # Skip CollectionMeme rows whose (collection_id, target_meme_id) already exists to
        # keep the composite primary key intact, then re-point any remaining rows.
        existing_rows_result = await self._session.execute(
            select(CollectionMeme.collection_id).where(CollectionMeme.meme_id == target_meme_id)
        )
        collections_with_target = {row for row in existing_rows_result.scalars().all()}

        colliding_result = await self._session.execute(
            select(CollectionMeme).where(CollectionMeme.meme_id == source_meme_id)
        )
        for membership in colliding_result.scalars().all():
            if membership.collection_id in collections_with_target:
                await self._session.delete(membership)
            else:
                membership.meme_id = target_meme_id
        await self._session.flush()

    async def _transfer_pins(
        self,
        *,
        source_meme_id: uuid.UUID,
        target_meme_id: uuid.UUID,
    ) -> None:
        existing_pins_result = await self._session.execute(
            select(PinnedMeme.user_id).where(PinnedMeme.meme_id == target_meme_id)
        )
        users_with_target_pin = {row for row in existing_pins_result.scalars().all()}

        source_pins_result = await self._session.execute(
            select(PinnedMeme).where(PinnedMeme.meme_id == source_meme_id)
        )
        for pin in source_pins_result.scalars().all():
            if pin.user_id in users_with_target_pin:
                await self._session.delete(pin)
            else:
                pin.meme_id = target_meme_id
        await self._session.flush()

    async def _transfer_popularity_snapshots(
        self,
        *,
        source_meme_id: uuid.UUID,
        target_meme_id: uuid.UUID,
    ) -> None:
        # Snapshots are uniquely scoped by (meme_id, captured_at). Preserve existing target
        # snapshots by deleting source rows whose captured_at collides, then re-point the rest.
        target_captured_at_result = await self._session.execute(
            select(MemePopularitySnapshot.captured_at).where(
                MemePopularitySnapshot.meme_id == target_meme_id
            )
        )
        target_captured_at_values = {value for value in target_captured_at_result.scalars().all()}

        source_snapshots_result = await self._session.execute(
            select(MemePopularitySnapshot).where(MemePopularitySnapshot.meme_id == source_meme_id)
        )
        for snapshot in source_snapshots_result.scalars().all():
            if snapshot.captured_at in target_captured_at_values:
                await self._session.delete(snapshot)
            else:
                snapshot.meme_id = target_meme_id
        await self._session.flush()

    @staticmethod
    def _accumulate_popularity(*, source_meme: Meme, target_meme: Meme) -> None:
        target_meme.like_count += source_meme.like_count
        target_meme.popularity_score = max(target_meme.popularity_score, source_meme.popularity_score)

    async def _delete_source_meme(self, *, source_meme: Meme) -> None:
        await self._session.delete(source_meme)
        await self._session.flush()

    async def _reselect_primary_file(self, meme_id: uuid.UUID) -> uuid.UUID:
        files_result = await self._session.execute(
            select(MemeFile.id)
            .where(MemeFile.meme_id == meme_id)
            .order_by(MemeFile.quality_score.desc(), MemeFile.created_at.desc(), MemeFile.id.desc())
            .limit(1)
        )
        best_file_id = files_result.scalar_one_or_none()
        if best_file_id is None:
            raise PipelineIngestError(f"Merged target meme {meme_id} has no files available for primary selection.")

        await self._session.execute(
            update(Meme).where(Meme.id == meme_id).values(primary_file_id=best_file_id)
        )
        await self._session.flush()
        return best_file_id

__all__ = [
    "MERGE_REASON_ADMIN",
    "MERGE_REASON_HIGH_SIMILARITY",
    "ContentMergeService",
    "MergeOutcome",
]
