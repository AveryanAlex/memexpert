# ruff: noqa: TC001,TC003
"""Materialized pipeline item read and detail service."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from memexpert.models.base import utcnow
from memexpert.models.content import MemeFile
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.helpers import matches_list_filter, resolve_current_stage, sorted_stage_entries
from memexpert.pipeline.reporting import build_item_detail
from memexpert.pipeline.state import PipelineDatabaseService
from memexpert.schemas.content_pipeline import (
    ContentPipelineItemDetail,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
)


class PipelineItemReadService(PipelineDatabaseService):
    """Assemble operator-facing materialized item list/read/detail DTOs."""

    async def get_item(self, meme_file_id: uuid.UUID) -> ContentPipelineItemRead:
        meme_file = await self._get_meme_file(meme_file_id)
        return self._build_item_read(meme_file)

    async def get_item_detail(self, meme_file_id: uuid.UUID) -> ContentPipelineItemDetail:
        meme_file = await self._get_meme_file(meme_file_id)
        base = self._build_item_read(meme_file)
        return await build_item_detail(self._session, meme_file=meme_file, base=base)

    async def list_items(
        self,
        *,
        filter_by: ContentPipelineItemFilter = ContentPipelineItemFilter.FAILED,
        limit: int = _consts.DEFAULT_PIPELINE_ITEMS_LIMIT,
        stuck_after_seconds: int = _consts.DEFAULT_STUCK_AFTER_SECONDS,
    ) -> tuple[ContentPipelineItemRead, ...]:
        resolved_limit = max(1, min(limit, _consts.MAX_PIPELINE_ITEMS_LIMIT))
        resolved_stuck_after_seconds = max(stuck_after_seconds, 1)
        stale_before = utcnow() - timedelta(seconds=resolved_stuck_after_seconds)

        result = await self._session.execute(
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.sources),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .order_by(MemeFile.created_at.desc())
        )

        items: list[ContentPipelineItemRead] = []
        for meme_file in result.scalars().all():
            stage_entries = sorted_stage_entries(meme_file)
            if not stage_entries:
                continue

            current_entry = resolve_current_stage(stage_entries)
            if not matches_list_filter(
                current_entry,
                filter_by=filter_by,
                stale_before=stale_before,
            ):
                continue

            items.append(self._build_item_read(meme_file, stage_entries=stage_entries, current_entry=current_entry))
            if len(items) >= resolved_limit:
                break

        return tuple(items)


__all__ = ["PipelineItemReadService"]
