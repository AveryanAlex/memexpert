"""New meme materialization row creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memexpert.ingest.materialization.sources import source_views
from memexpert.ingest.source_metadata import (
    source_engagement_metrics,
    source_forward_ids,
    source_is_forwarded,
    source_published_at,
    source_reactions,
)
from memexpert.models.content import Meme, MemeFile, MemeSource, PipelineStageJournal
from memexpert.models.enums import (
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    SourceAttachReason,
)
from memexpert.services.source_engagement import add_initial_source_engagement_snapshot

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.ingest.materialization.models import PreparedMaterialization
    from memexpert.models.content import PipelineIngestRequest


async def create_new_content_rows(
    session: AsyncSession,
    *,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID,
    ingest_request: PipelineIngestRequest,
    prepared: PreparedMaterialization,
    publish_event_id: uuid.UUID,
    created_at: datetime,
) -> None:
    """Create rows for a brand-new meme and its first file/source."""

    forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
    meme = Meme(
        id=meme_id,
        media_type=prepared.media_type,
        primary_file_id=meme_file_id,
        language=ContentLanguage.NONE,
        is_public=False,
        author_user_id=ingest_request.owner_user_id,
    )
    session.add(meme)
    await session.flush()

    source_row = MemeSource(
        file_id=meme_file_id,
        platform=ingest_request.source_platform,
        source_id=ingest_request.source_id,
        post_id=ingest_request.post_id,
        views=source_views(ingest_request),
        reactions=source_reactions(ingest_request.source_metadata),
        is_first_source=not source_is_forwarded(ingest_request.source_metadata),
        source_alive=True,
        published_at=source_published_at(ingest_request.source_metadata),
        forwarded_from_source_id=forwarded_from_source_id,
        forwarded_from_post_id=forwarded_from_post_id,
        attach_reason=SourceAttachReason.NEW_FILE,
    )

    session.add(
        MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.PENDING,
            width=prepared.width,
            height=prepared.height,
            file_size_bytes=prepared.file_size_bytes,
            mime_type=prepared.mime_type,
            s3_original_key=prepared.object_key,
            perceptual_hash=prepared.perceptual_hash,
            sha256_hex=prepared.sha256_hex,
            ingest_origin=IngestFileOrigin.NEW_MEME,
        )
    )
    session.add_all(
        [
            source_row,
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=1,
                last_event_id=publish_event_id,
                is_retryable=False,
                started_at=created_at,
                finished_at=created_at,
            ),
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.PENDING,
                attempt_count=0,
                last_event_id=publish_event_id,
                is_retryable=True,
            ),
        ]
    )
    await add_initial_source_engagement_snapshot(
        session,
        source_row,
        source_engagement_metrics(ingest_request.source_metadata),
        captured_at=created_at,
    )
    await session.flush()


__all__ = ["create_new_content_rows"]
