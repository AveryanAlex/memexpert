# ruff: noqa: TC001,TC003
"""Shared database helpers for focused content-pipeline services."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Self

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from memexpert.core.config import Settings, get_settings
from memexpert.models.content import Meme, MemeFile, MemeFileOCRResult, PipelineStageJournal
from memexpert.models.enums import ContentLanguage, ContentPipelineStage
from memexpert.pipeline.helpers import StageJournalSnapshot, resolve_current_stage, sorted_stage_entries
from memexpert.schemas.content_pipeline import ContentPipelineItemRead, ContentPipelineStageJournalRead
from memexpert.services.errors import PipelineIngestError, PipelineItemNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PipelineDatabaseService:
    """Small base for focused services that need pipeline ORM state."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
    ) -> Self:
        """Build the focused service from shared runtime settings."""

        return cls(session, settings=settings)

    async def _get_meme_file(
        self,
        meme_file_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MemeFile:
        statement = (
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.ocr_result),
                selectinload(MemeFile.sources),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .where(MemeFile.id == meme_file_id)
        )
        if for_update:
            # ``populate_existing`` is required here: a long-lived worker session
            # may already have loaded the file before another delivery activated a
            # generation.  The row lock alone must not leave that stale pointer in
            # the identity map.
            statement = statement.with_for_update(of=MemeFile).execution_options(populate_existing=True)
        result = await self._session.execute(statement)
        meme_file = result.scalar_one_or_none()
        if meme_file is None:
            raise PipelineItemNotFoundError(f"Pipeline item {meme_file_id} does not exist.")
        return meme_file

    async def _get_canonical_meme(self, meme_id: uuid.UUID) -> Meme:
        result = await self._session.execute(select(Meme).where(Meme.id == meme_id))
        meme = result.scalar_one_or_none()
        if meme is None:
            raise PipelineIngestError(
                f"Canonical meme {meme_id} is missing when finalizing classification.",
            )
        return meme

    async def _apply_canonical_primary_truth(self, meme: Meme) -> None:
        primary_file_id = meme.primary_file_id
        result = await self._session.execute(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == primary_file_id)
        )
        ocr_row = result.scalar_one_or_none()
        if ocr_row is None:
            meme.ocr_text = None
            meme.language = ContentLanguage.NONE
            return

        meme.ocr_text = ocr_row.extracted_text
        meme.language = ocr_row.language

    async def _get_meme_file_and_stage_entry(
        self,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        *,
        for_update: bool = False,
    ) -> tuple[MemeFile, PipelineStageJournal]:
        meme_file = await self._get_meme_file(meme_file_id, for_update=for_update)
        if for_update:
            # Lock aggregate rows in the same file -> stage order used by media
            # generation reservation.  The separate select also refreshes a
            # journal row that may already be present in this session.
            stage_entry = await self._session.scalar(
                select(PipelineStageJournal)
                .where(
                    PipelineStageJournal.meme_file_id == meme_file_id,
                    PipelineStageJournal.stage == stage,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        else:
            stage_entry = next(
                (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is stage),
                None,
            )
        if stage_entry is None:
            raise PipelineIngestError(
                f"Pipeline item {meme_file_id} does not have durable journal state for stage {stage.value}."
            )
        return meme_file, stage_entry

    async def _commit_stage_mutation(self, failure_message: str) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(failure_message) from exc

    async def _restore_stage_snapshot(self, stage_entry_id: uuid.UUID, snapshot: StageJournalSnapshot) -> None:
        try:
            result = await self._session.execute(
                select(PipelineStageJournal).where(PipelineStageJournal.id == stage_entry_id)
            )
            stage_entry = result.scalar_one_or_none()
            if stage_entry is None:
                raise PipelineIngestError(
                    f"Replay reservation for stage journal {stage_entry_id} disappeared before restore.",
                )

            stage_entry.status = snapshot.status
            stage_entry.attempt_count = snapshot.attempt_count
            stage_entry.last_event_id = snapshot.last_event_id
            stage_entry.normalized_reason = snapshot.normalized_reason
            stage_entry.last_error_text = snapshot.last_error_text
            stage_entry.is_retryable = snapshot.is_retryable
            stage_entry.retry_after = snapshot.retry_after
            stage_entry.started_at = snapshot.started_at
            stage_entry.finished_at = snapshot.finished_at
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise

    def _build_item_read(
        self,
        meme_file: MemeFile,
        *,
        stage_entries: tuple[PipelineStageJournal, ...] | None = None,
        current_entry: PipelineStageJournal | None = None,
    ) -> ContentPipelineItemRead:
        resolved_stage_entries = stage_entries or sorted_stage_entries(meme_file)
        if not resolved_stage_entries:
            raise PipelineIngestError(f"Pipeline item {meme_file.id} is missing journal state.")

        resolved_current_entry = current_entry or resolve_current_stage(resolved_stage_entries)
        latest_source = max(
            meme_file.sources,
            key=lambda source: (source.created_at, source.id),
            default=None,
        )
        return ContentPipelineItemRead(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            sha256_hex=meme_file.sha256_hex,
            ingest_origin=meme_file.ingest_origin,
            matched_meme_file_id=meme_file.matched_meme_file_id,
            latest_source_id=latest_source.id if latest_source is not None else None,
            latest_source_attach_reason=latest_source.attach_reason if latest_source is not None else None,
            latest_source_matched_meme_file_id=(
                latest_source.matched_meme_file_id if latest_source is not None else None
            ),
            current_stage=resolved_current_entry.stage,
            current_status=resolved_current_entry.status,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
            last_event_id=resolved_current_entry.last_event_id,
            normalized_reason=resolved_current_entry.normalized_reason,
            last_error_text=resolved_current_entry.last_error_text,
            attempt_count=resolved_current_entry.attempt_count,
            stages=tuple(ContentPipelineStageJournalRead.model_validate(entry) for entry in resolved_stage_entries),
        )


__all__ = ["PipelineDatabaseService"]
