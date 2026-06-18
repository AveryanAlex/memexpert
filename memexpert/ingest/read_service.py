# ruff: noqa: TC001,TC003
"""Operator read service for raw ingest requests."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.ingest.schemas import IngestRequestRead
from memexpert.models.content import PipelineIngestRequest
from memexpert.models.enums import PipelineIngestRequestStatus
from memexpert.services.errors import PipelineItemNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PipelineIngestReadService:
    """Read raw ingest requests without touching materialized pipeline items."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_requests(
        self,
        *,
        status: PipelineIngestRequestStatus | None = None,
        limit: int = 50,
    ) -> tuple[IngestRequestRead, ...]:
        """Return recent raw ingest requests for operator inspection."""

        resolved_limit = max(1, min(limit, 200))
        statement = select(PipelineIngestRequest).order_by(PipelineIngestRequest.created_at.desc())
        if status is not None:
            statement = statement.where(PipelineIngestRequest.status == status)
        result = await self._session.execute(statement.limit(resolved_limit))
        return tuple(IngestRequestRead.model_validate(row) for row in result.scalars().all())

    async def get_request(self, ingest_request_id: uuid.UUID) -> IngestRequestRead:
        """Return one raw ingest request or raise a route-translatable 404."""

        ingest_request = await self._session.get(PipelineIngestRequest, ingest_request_id)
        if ingest_request is None:
            raise PipelineItemNotFoundError(f"Ingest request {ingest_request_id} does not exist.")
        return IngestRequestRead.model_validate(ingest_request)


__all__ = ["PipelineIngestReadService"]
