"""Normal materialization policy for transcodable content."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from memexpert.ingest.materialization.duplicates import (
    attach_target_collection_if_requested,
    create_phash_duplicate_rows,
    find_exact_phash_match,
)
from memexpert.ingest.materialization.new_content import create_new_content_rows
from memexpert.ingest.materialization.objects import meme_file_id_from_original_key
from memexpert.ingest.materialization.outbox import create_transcode_outbox_message
from memexpert.models.enums import PipelineIngestRequestStatus, SourceAttachReason

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.ingest.materialization.models import PreparedMaterialization
    from memexpert.models.content import PipelineIngestRequest


async def materialize_transcodable_request(
    session: AsyncSession,
    *,
    ingest_request: PipelineIngestRequest,
    prepared: PreparedMaterialization,
    created_at: datetime,
    settings: Settings,
) -> uuid.UUID:
    """Materialize new or exact-pHash-duplicate content and enqueue transcode work."""

    phash_match = await find_exact_phash_match(
        session,
        prepared.perceptual_hash,
        owner_user_id=ingest_request.owner_user_id,
    )
    meme_file_id = meme_file_id_from_original_key(prepared.object_key)
    event_id = uuid.uuid7()
    if phash_match is None:
        meme_id = uuid.uuid7()
        await create_new_content_rows(
            session,
            meme_id=meme_id,
            meme_file_id=meme_file_id,
            ingest_request=ingest_request,
            prepared=prepared,
            publish_event_id=event_id,
            created_at=created_at,
        )
        source_attach_reason = SourceAttachReason.NEW_FILE
        matched_meme_file_id: uuid.UUID | None = None
    else:
        meme_id = phash_match.meme_id
        await create_phash_duplicate_rows(
            session,
            phash_match=phash_match,
            meme_file_id=meme_file_id,
            ingest_request=ingest_request,
            prepared=prepared,
            publish_event_id=event_id,
            created_at=created_at,
        )
        source_attach_reason = SourceAttachReason.PHASH_EXACT_NEW_FILE
        matched_meme_file_id = phash_match.id

    await attach_target_collection_if_requested(session, ingest_request=ingest_request, meme_id=meme_id)
    outbox_message_id = await create_transcode_outbox_message(
        session,
        meme_file_id=meme_file_id,
        event_id=event_id,
        created_at=created_at,
        settings=settings,
    )
    ingest_request.status = PipelineIngestRequestStatus.MATERIALIZED
    ingest_request.failure_code = None
    ingest_request.failure_detail = None
    ingest_request.locked_at = None
    ingest_request.materialized_meme_id = meme_id
    ingest_request.materialized_meme_file_id = meme_file_id
    ingest_request.matched_meme_file_id = matched_meme_file_id
    ingest_request.source_attach_reason = source_attach_reason
    await session.flush()
    return outbox_message_id


__all__ = ["materialize_transcodable_request"]
