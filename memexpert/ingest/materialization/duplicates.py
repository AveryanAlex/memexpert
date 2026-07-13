"""Exact perceptual-hash duplicate materialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import aliased

from memexpert.ingest.collection_targets import (
    save_meme_to_target_collection,
)
from memexpert.ingest.policy import ApproximateMergeScope, refresh_effective_visibility
from memexpert.ingest.sha_dedupe import sha_match_attach_reason
from memexpert.ingest.source_metadata import (
    source_engagement_metrics,
    source_forward_ids,
    source_published_at,
)
from memexpert.ingest.target_collection_metadata import TargetCollectionMetadataError, parse_target_collection_id
from memexpert.models.content import Meme, MemeFile, MemeSource, PipelineStageJournal
from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    IngestFileOrigin,
    PipelineIngestRequestStatus,
    SourceAttachReason,
)
from memexpert.services.errors import PipelinePayloadValidationError
from memexpert.services.source_engagement import add_initial_source_engagement_snapshot

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.ingest.materialization.models import PreparedMaterialization
    from memexpert.models.content import PipelineIngestRequest


async def find_exact_phash_match(
    session: AsyncSession,
    perceptual_hash: str,
    *,
    scope: ApproximateMergeScope,
) -> MemeFile | None:
    """Find an eligible same-scope file with the same perceptual hash."""

    if not scope.can_match:
        return None

    stmt = (
        select(MemeFile)
        .join(Meme, Meme.id == MemeFile.meme_id)
        .where(
            MemeFile.perceptual_hash == perceptual_hash,
            MemeFile.blocked_perceptual_hash_id.is_(None),
            Meme.is_public.is_(scope.is_public),
        )
    )
    if not scope.is_public:
        assert scope.uploader_user_id is not None
        source_file = aliased(MemeFile)
        source = aliased(MemeSource)
        same_uploader_source = (
            select(source.id)
            .join(source_file, source_file.id == source.file_id)
            .where(
                source_file.meme_id == Meme.id,
                source.uploader_user_id == scope.uploader_user_id,
            )
            .exists()
        )
        other_uploader_source = (
            select(source.id)
            .join(source_file, source_file.id == source.file_id)
            .where(
                source_file.meme_id == Meme.id,
                source.uploader_user_id.is_not(None),
                source.uploader_user_id != scope.uploader_user_id,
            )
            .exists()
        )
        stmt = stmt.where(same_uploader_source, ~other_uploader_source)

    result = await session.execute(
        stmt.order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
        .with_for_update(of=Meme)
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
    source_row = MemeSource(
        file_id=meme_file_id,
        platform=ingest_request.source_platform,
        source_id=ingest_request.source_id,
        post_id=ingest_request.post_id,
        source_kind=ingest_request.source_kind,
        uploader_user_id=ingest_request.uploader_user_id,
        is_first_source=False,
        source_alive=True,
        published_at=source_published_at(ingest_request.source_metadata),
        forwarded_from_source_id=forwarded_from_source_id,
        forwarded_from_post_id=forwarded_from_post_id,
        attach_reason=SourceAttachReason.PHASH_EXACT_NEW_FILE,
        matched_meme_file_id=phash_match.id,
    )
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


async def resolve_sha_duplicate_request(
    session: AsyncSession,
    *,
    matched_file: MemeFile,
    ingest_request: PipelineIngestRequest,
    resolved_at: datetime,
) -> None:
    """Resolve a post-inspection exact SHA race onto the canonical file."""

    attach_reason = sha_match_attach_reason(matched_file)
    meme = await session.scalar(select(Meme).where(Meme.id == matched_file.meme_id).with_for_update())
    if meme is None:
        raise PipelinePayloadValidationError(f"Matched meme {matched_file.meme_id} does not exist.")

    forwarded_from_source_id, forwarded_from_post_id = source_forward_ids(ingest_request.source_metadata)
    source_row = MemeSource(
        file_id=matched_file.id,
        platform=ingest_request.source_platform,
        source_id=ingest_request.source_id,
        post_id=ingest_request.post_id,
        source_kind=ingest_request.source_kind,
        uploader_user_id=ingest_request.uploader_user_id,
        is_first_source=False,
        source_alive=True,
        published_at=source_published_at(ingest_request.source_metadata),
        forwarded_from_source_id=forwarded_from_source_id,
        forwarded_from_post_id=forwarded_from_post_id,
        attach_reason=attach_reason,
        matched_meme_file_id=matched_file.id,
    )
    session.add(source_row)
    await add_initial_source_engagement_snapshot(
        session,
        source_row,
        source_engagement_metrics(ingest_request.source_metadata),
        captured_at=resolved_at,
    )
    if attach_reason is SourceAttachReason.SHA256_EXACT_EXISTING_FILE:
        await attach_target_collection_if_requested(
            session,
            ingest_request=ingest_request,
            meme_id=matched_file.meme_id,
        )
    await refresh_effective_visibility(
        session,
        meme,
        incoming_source_kind=ingest_request.source_kind,
    )

    ingest_request.status = PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE
    ingest_request.failure_code = None
    ingest_request.failure_detail = None
    ingest_request.locked_at = None
    ingest_request.temp_original_object_key = None
    ingest_request.materialized_meme_id = matched_file.meme_id
    ingest_request.materialized_meme_file_id = matched_file.id
    ingest_request.matched_meme_file_id = matched_file.id
    ingest_request.source_attach_reason = attach_reason
    await session.flush()


async def attach_target_collection_if_requested(
    session: AsyncSession,
    *,
    ingest_request: PipelineIngestRequest,
    meme_id: uuid.UUID,
) -> None:
    """Attach a materialized meme to the requested target collection, if any."""

    try:
        target_collection_id = parse_target_collection_id(ingest_request.user_metadata)
    except TargetCollectionMetadataError as exc:
        raise PipelinePayloadValidationError(str(exc)) from exc
    await save_meme_to_target_collection(
        session,
        uploader_user_id=ingest_request.uploader_user_id,
        target_collection_id=target_collection_id,
        meme_id=meme_id,
    )
    await session.flush()


__all__ = [
    "attach_target_collection_if_requested",
    "create_phash_duplicate_rows",
    "find_exact_phash_match",
    "resolve_sha_duplicate_request",
]
