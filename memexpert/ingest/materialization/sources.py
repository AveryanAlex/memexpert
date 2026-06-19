"""Source-metadata helpers shared by materialization row writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memexpert.ingest.source_metadata import source_view_count

if TYPE_CHECKING:
    from memexpert.models.content import PipelineIngestRequest


def source_views(ingest_request: PipelineIngestRequest) -> int:
    """Normalize source view counts for persisted source rows."""

    return source_view_count(ingest_request.source_metadata) or 0


__all__ = ["source_views"]
