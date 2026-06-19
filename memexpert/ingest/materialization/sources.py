"""Source-metadata helpers shared by materialization row writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memexpert.models.content import PipelineIngestRequest


def source_views(ingest_request: PipelineIngestRequest) -> int:
    """Normalize source view counts for persisted source rows."""

    raw_views = ingest_request.source_metadata.get("views")
    if isinstance(raw_views, int) and raw_views >= 0:
        return raw_views
    return 0


__all__ = ["source_views"]
