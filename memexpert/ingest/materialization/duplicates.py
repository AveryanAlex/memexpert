"""Exact perceptual-hash duplicate materialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.ingest.materialization.sources import source_views
from memexpert.ingest.source_metadata import source_forward_ids, source_published_at, source_reactions
from memexpert.models.content import MemeFile, MemeSource, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    SourceAttachReason,
)

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.ingest.materialization.models import PreparedMaterialization
    from memexpert.models.content import PipelineIngestRequest


async def find_exact_phash_match(session: AsyncSession, perceptual_hash: str) -> MemeFile | None:
    """Find the earliest non-blocked file with the same perceptual hash."""

    result = await session.execute(
        select(MemeFile)
        .where(
            MemeFile.perceptual_hash == perceptual_hash,
            MemeFile.blocked_perceptual_hash_id.is_(None),
        )
        .order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_phash_duplicate_rows(
    session: AsyncSession,
    *,
    phash_match: MemeFile,
    meme_file_id: uuid.UUID,
    ingest_request: PipelineIngestRequest,
    prepared: PreparedMaterialization,
    publish_event_id: uuid.UUID,
    created_at: datetime,
) -> None:
    """Create a new file/source under an existing meme for an exact pHash match."""

    forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
    session.add(
        MemeFile(
            id=meme_file_id,
            meme_id=phash_match.meme_id,
            status=ContentProcessingStatus.PENDING,
            width=prepared.width,
            height=prepared.height,
            file_size_bytes=prepared.file_size_bytes,
            mime_type=prepared.mime_type,
            s3_original_key=prepared.object_key,
            perceptual_hash=prepared.perceptual_hash,
            sha256_hex=prepared.sha256_hex,
            ingest_origin=IngestFileOrigin.PHASH_EXACT_EXISTING_MEME,
            matched_meme_file_id=phash_match.id,
        )
    )
    session.add_all(
        [
            MemeSource(
                file_id=meme_file_id,
                platform=ingest_request.source_platform,
                source_id=ingest_request.source_id,
                post_id=ingest_request.post_id,
                views=source_views(ingest_request),
                reactions=source_reactions(ingest_request.source_metadata),
                is_first_source=False,
                source_alive=True,
                published_at=source_published_at(ingest_request.source_metadata),
                forwarded_from_source_id=forwarded_from_source_id,
                forwarded_from_post_id=forwarded_from_post_id,
                attach_reason=SourceAttachReason.PHASH_EXACT_NEW_FILE,
                matched_meme_file_id=phash_match.id,
            ),
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
    await session.flush()


__all__ = ["create_phash_duplicate_rows", "find_exact_phash_match"]
