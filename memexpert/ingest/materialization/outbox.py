"""Transactional transcode outbox creation for materialized meme files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.content import MemeFile
from memexpert.pipeline.outbox import build_meme_created_transcode_outbox_event
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.models.content import PipelineOutboxEvent


async def create_transcode_outbox_event(
    session: AsyncSession,
    *,
    meme_file_id: uuid.UUID,
    event_id: uuid.UUID,
    created_at: datetime,
    settings: Settings,
) -> PipelineOutboxEvent:
    """Add the downstream transcode outbox event for a materialized meme file."""

    result = await session.execute(select(MemeFile).where(MemeFile.id == meme_file_id))
    meme_file = result.scalar_one_or_none()
    if meme_file is None:
        raise PipelineIngestError(f"Materialized meme file {meme_file_id} disappeared before outbox creation.")

    outbox_event = build_meme_created_transcode_outbox_event(
        meme_file,
        event_id=event_id,
        created_at=created_at,
        settings=settings,
    )
    session.add(outbox_event)
    return outbox_event


__all__ = ["create_transcode_outbox_event"]
