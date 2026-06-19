"""Transactional transcode outbox creation for materialized meme files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.messaging.rabbitmq_outbox import RabbitPublisher
from memexpert.models.content import MemeFile
from memexpert.pipeline.events import build_meme_created_transcode_message_spec
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings


async def create_transcode_outbox_message(
    session: AsyncSession,
    *,
    meme_file_id: uuid.UUID,
    event_id: uuid.UUID,
    created_at: datetime,
    settings: Settings,
) -> uuid.UUID:
    """Add the downstream transcode outbox message for a materialized meme file."""

    result = await session.execute(select(MemeFile).where(MemeFile.id == meme_file_id))
    meme_file = result.scalar_one_or_none()
    if meme_file is None:
        raise PipelineIngestError(f"Materialized meme file {meme_file_id} disappeared before outbox creation.")

    spec = build_meme_created_transcode_message_spec(
        meme_file,
        event_id=event_id,
        created_at=created_at,
        settings=settings,
    )
    return await RabbitPublisher(settings=settings).publish(spec, session=session)


__all__ = ["create_transcode_outbox_message"]
