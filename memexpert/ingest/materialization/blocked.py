"""Blocked perceptual-hash materialization policy and row creation."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.core.perceptual_hashes import (
    DEFAULT_PERCEPTUAL_HASH_ALGORITHM,
    hamming_distance_hex,
    perceptual_hash_bit_size,
)
from memexpert.ingest.materialization.models import (
    FAILED_BLOCKED_PHASH_CODE,
    BlockedPerceptualHashMatch,
    PreparedMaterialization,
)
from memexpert.ingest.materialization.objects import meme_file_id_from_original_key
from memexpert.ingest.source_metadata import (
    source_engagement_metrics,
    source_forward_ids,
    source_is_forwarded,
    source_published_at,
)
from memexpert.models.content import (
    BlockedPerceptualHash,
    Meme,
    MemeFile,
    MemeSource,
    ModerationDecision,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    ModerationAction,
    PipelineIngestRequestStatus,
    SourceAttachReason,
)
from memexpert.pipeline import constants as _consts
from memexpert.pipeline.helpers import trim_error_text
from memexpert.services.source_engagement import add_initial_source_engagement_snapshot

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.models.content import PipelineIngestRequest


async def find_blocked_perceptual_hash_match(
    session: AsyncSession,
    perceptual_hash: str,
) -> BlockedPerceptualHashMatch | None:
    """Return the closest active blocked pHash match for the incoming perceptual hash."""

    try:
        hash_size = perceptual_hash_bit_size(perceptual_hash)
    except ValueError:
        return None
    rows = (
        await session.execute(
            select(BlockedPerceptualHash)
            .where(
                BlockedPerceptualHash.is_active.is_(True),
                BlockedPerceptualHash.hash_algorithm == DEFAULT_PERCEPTUAL_HASH_ALGORITHM,
                BlockedPerceptualHash.hash_size == hash_size,
            )
            .order_by(BlockedPerceptualHash.created_at.asc(), BlockedPerceptualHash.id.asc())
        )
    ).scalars().all()

    best_match: BlockedPerceptualHashMatch | None = None
    for blocked_hash in rows:
        distance = hamming_distance_hex(perceptual_hash, blocked_hash.perceptual_hash)
        if distance is None or distance > blocked_hash.max_hamming_distance:
            continue
        candidate = BlockedPerceptualHashMatch(blocked_hash=blocked_hash, hamming_distance=distance)
        if best_match is None or candidate.hamming_distance < best_match.hamming_distance:
            best_match = candidate
    return best_match


async def materialize_blocked_request(
    session: AsyncSession,
    *,
    ingest_request: PipelineIngestRequest,
    prepared: PreparedMaterialization,
    blocked_match: BlockedPerceptualHashMatch,
    created_at: datetime,
) -> None:
    """Persist failed/blocked audit rows and terminal request state."""

    meme_id = uuid.uuid7()
    meme_file_id = meme_file_id_from_original_key(prepared.object_key)
    await _create_blocked_rows(
        session,
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        ingest_request=ingest_request,
        prepared=prepared,
        blocked_match=blocked_match,
        created_at=created_at,
    )
    ingest_request.status = PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH
    ingest_request.failure_code = FAILED_BLOCKED_PHASH_CODE
    ingest_request.failure_detail = blocked_hash_error_text(blocked_match)
    ingest_request.locked_at = None
    ingest_request.materialized_meme_id = meme_id
    ingest_request.materialized_meme_file_id = meme_file_id
    ingest_request.matched_meme_file_id = None
    ingest_request.source_attach_reason = SourceAttachReason.BLOCKED_PERCEPTUAL_HASH_NEW_FILE
    await session.flush()


async def _create_blocked_rows(
    session: AsyncSession,
    *,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID,
    ingest_request: PipelineIngestRequest,
    prepared: PreparedMaterialization,
    blocked_match: BlockedPerceptualHashMatch,
    created_at: datetime,
) -> None:
    blocked_hash = blocked_match.blocked_hash
    error_text = blocked_hash_error_text(blocked_match)
    event_id = uuid.uuid7()
    forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
    source_row = MemeSource(
        file_id=meme_file_id,
        platform=ingest_request.source_platform,
        source_id=ingest_request.source_id,
        post_id=ingest_request.post_id,
        is_first_source=not source_is_forwarded(ingest_request.source_metadata),
        source_alive=True,
        published_at=source_published_at(ingest_request.source_metadata),
        forwarded_from_source_id=forwarded_from_source_id,
        forwarded_from_post_id=forwarded_from_post_id,
        attach_reason=SourceAttachReason.BLOCKED_PERCEPTUAL_HASH_NEW_FILE,
    )
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

    session.add(
        MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.FAILED,
            width=prepared.width,
            height=prepared.height,
            file_size_bytes=prepared.file_size_bytes,
            mime_type=prepared.mime_type,
            s3_original_key=prepared.object_key,
            perceptual_hash=prepared.perceptual_hash,
            sha256_hex=prepared.sha256_hex,
            ingest_origin=IngestFileOrigin.BLOCKED_PERCEPTUAL_HASH,
            blocked_perceptual_hash_id=blocked_hash.id,
        )
    )
    session.add_all(
        [
            source_row,
            PipelineStageJournal(
                meme_file_id=meme_file_id,
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.FAILED,
                attempt_count=1,
                last_event_id=event_id,
                normalized_reason=_consts.PIPELINE_REASON_BLOCKED_PERCEPTUAL_HASH,
                last_error_text=error_text,
                is_retryable=False,
                started_at=created_at,
                finished_at=created_at,
            ),
            ModerationDecision(
                meme=meme,
                admin_user_id=None,
                action=ModerationAction.HIDE,
                reason=blocked_hash.reason,
                note=error_text,
                previous_is_public=False,
                previous_is_nsfw=False,
                new_is_public=False,
                new_is_nsfw=False,
                previous_template_id=None,
                new_template_id=None,
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


def blocked_hash_error_text(blocked_match: BlockedPerceptualHashMatch) -> str:
    """Build the persisted failure detail for a blocked pHash match."""

    blocked_hash = blocked_match.blocked_hash
    note_suffix = f" Note: {blocked_hash.note}" if blocked_hash.note else ""
    return trim_error_text(
        "Upload matched blocked perceptual hash "
        f"{blocked_hash.id} ({blocked_hash.hash_algorithm}, distance "
        f"{blocked_match.hamming_distance}/{blocked_hash.max_hamming_distance}, reason "
        f"{blocked_hash.reason.value}).{note_suffix}"
    )


__all__ = [
    "blocked_hash_error_text",
    "find_blocked_perceptual_hash_match",
    "materialize_blocked_request",
]
