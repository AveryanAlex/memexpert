"""Invalid-media terminal failure policy for ingest materialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from memexpert.models.enums import PipelineIngestRequestStatus
from memexpert.pipeline.helpers import trim_error_text
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.models.content import PipelineIngestRequest



async def mark_invalid_media(
    session: AsyncSession,
    ingest_request: PipelineIngestRequest,
    *,
    code: str,
    detail: str,
) -> None:
    """Persist a terminal invalid-media failure without creating content rows."""

    ingest_request.status = PipelineIngestRequestStatus.FAILED_INVALID_MEDIA
    ingest_request.failure_code = code
    ingest_request.failure_detail = trim_error_text(detail or "Media inspection failed.")
    ingest_request.locked_at = None
    ingest_request.materialized_meme_id = None
    ingest_request.materialized_meme_file_id = None
    ingest_request.matched_meme_file_id = None
    ingest_request.source_attach_reason = None
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise PipelineIngestError("Failed to persist invalid media ingest-request state.") from exc


__all__ = ["mark_invalid_media"]
