"""Pipeline ingest-request locking and materialization attempt state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.content import PipelineIngestRequest
from memexpert.models.enums import PipelineIngestRequestStatus
from memexpert.services.errors import PipelineIngestError

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


_ELIGIBLE_STATUSES = frozenset({PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING})


async def load_locked_ingest_request(session: AsyncSession, ingest_request_id: uuid.UUID) -> PipelineIngestRequest:
    """Load one ingest request under a row lock for materialization."""

    result = await session.execute(
        select(PipelineIngestRequest).where(PipelineIngestRequest.id == ingest_request_id).with_for_update()
    )
    ingest_request = result.scalar_one_or_none()
    if ingest_request is None:
        raise PipelineIngestError(f"Pipeline ingest request {ingest_request_id} does not exist.")
    return ingest_request


def is_materialization_eligible(ingest_request: PipelineIngestRequest) -> bool:
    """Return whether the request is eligible for a media-inspect materialization attempt."""

    return ingest_request.status in _ELIGIBLE_STATUSES


def mark_materialization_attempt_started(ingest_request: PipelineIngestRequest, *, started_at: datetime) -> None:
    """Move an eligible request into the in-progress materialization state."""

    ingest_request.status = PipelineIngestRequestStatus.MEDIA_INSPECTING
    ingest_request.locked_at = started_at
    ingest_request.attempt_count += 1
    ingest_request.failure_code = None
    ingest_request.failure_detail = None


__all__ = [
    "is_materialization_eligible",
    "load_locked_ingest_request",
    "mark_materialization_attempt_started",
]
