# ruff: noqa: TC003
"""Operator-facing ingest service for durable manual uploads and inspectable journal state."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, Self, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from memexpert.core.broker import ensure_pipeline_broker_started, get_pipeline_broker_settings
from memexpert.core.config import Settings, get_settings
from memexpert.core.media import NormalizedMediaResult, PipelineMediaProcessor, PipelineMediaProcessorProtocol
from memexpert.core.ocr import OCRExtractionResult, OCRProcessorProtocol, PipelineOCRProcessor
from memexpert.core.storage import build_original_object_key, get_pipeline_storage_settings, get_s3_client
from memexpert.core.voyage import VoyageMalformedResponseError
from memexpert.models.base import utcnow
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeSource,
    PipelineStageJournal,
    SourceChannel,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    ContentSourceKind,
    EmbeddingInputType,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineItemDetail,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineStageJournalRead,
    ContentPipelineSyncTargetPreview,
    ContentPipelineUploadMetadata,
    ContentPipelineUploadRead,
    CrawlerIngestOutcome,
    CrawlerIngestResult,
    PerTargetSyncStatus,
    RawCrawlerPost,
    SmokeProofResult,
)
from memexpert.services import content_pipeline_constants as _consts
from memexpert.services.content_merge import ContentMergeService, MergeOutcome
from memexpert.services.content_pipeline_constants import (
    PIPELINE_REASON_SYNC_REPLAY_REQUESTED,
    SYNC_REPLAY_BATCH_MAX,
)
from memexpert.services.content_pipeline_crawler_queries import (
    advance_source_channel_checkpoint,
    attach_crawler_source_row_to_meme_file,
    find_existing_crawler_source_row,
    get_tracked_source_channel,
    meme_file_has_first_source,
)
from memexpert.services.content_pipeline_helpers import (
    StageJournalSnapshot,
    ensure_stage_attempt_is_current,
    is_replay_reserved,
    matches_list_filter,
    normalize_content_type,
    normalize_filename,
    reserve_replay,
    resolve_current_stage,
    snapshot_stage,
    sorted_stage_entries,
    translate_media_processing_error,
    trim_error_text,
    trim_reason,
)
from memexpert.services.content_pipeline_helpers import (
    build_sync_preview_model as _build_sync_preview_model,
)
from memexpert.services.content_pipeline_reporting import (
    build_item_detail,
    decode_sync_preview,
)
from memexpert.services.content_pipeline_smoke import run_smoke_proof
from memexpert.services.errors import (
    CrawlerPublishError,
    PipelineIngestError,
    PipelineItemNotFoundError,
    PipelineMergeTransactionError,
    PipelinePayloadTooLargeError,
    PipelinePayloadValidationError,
    PipelinePublishError,
    PipelineReplayNotAllowedError,
    PipelineSourceConflictError,
    PipelineStorageError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.classification import ClassificationResult
    from memexpert.core.meilisearch import MeilisearchSyncClientProtocol
    from memexpert.core.qdrant import (
        QdrantSimilarityClientProtocol,
        QdrantSimilarityMatch,
        QdrantSyncClientProtocol,
    )
    from memexpert.core.voyage import VoyageEmbeddingResult


class ObjectStorageClient(Protocol):
    """Minimal sync S3-compatible client surface used by the ingest service."""

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> object: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


DispatchEventPublisher = Callable[[ContentPipelineDispatchEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    """Normalized upload bytes plus the derived media metadata written durably."""

    filename: str
    media_type: ContentKind
    mime_type: str
    file_size_bytes: int
    width: int
    height: int
    perceptual_hash: str
    object_key: str


@dataclass(frozen=True, slots=True)
class PipelineStageWorkContext:
    """Compact worker context returned after a stage is marked processing."""

    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    stage: ContentPipelineStage
    mime_type: str | None
    original_object_key: str
    web_video_object_key: str | None


@dataclass(frozen=True, slots=True)
class DownstreamStageDispatch:
    """A durable next-stage dispatch created after one stage succeeds."""

    event: ContentPipelineDispatchEvent
    stage_entry: PipelineStageJournal


class ContentPipelineService:
    """Persist manual uploads before publish and expose inspectable pipeline truth."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        publisher: DispatchEventPublisher | None = None,
        media_processor: PipelineMediaProcessorProtocol | None = None,
        ocr_processor: OCRProcessorProtocol | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage_settings = get_pipeline_storage_settings(self._settings)
        self._broker_settings = get_pipeline_broker_settings(self._settings)
        self._storage_client = storage_client or cast("ObjectStorageClient", get_s3_client())
        self._publisher = publisher or self._publish_dispatch_event
        self._media_processor = media_processor or PipelineMediaProcessor(settings=self._settings)
        self._ocr_processor = ocr_processor or PipelineOCRProcessor(
            settings=self._settings,
            media_processor=self._media_processor,
        )

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
        publisher: DispatchEventPublisher | None = None,
        media_processor: PipelineMediaProcessorProtocol | None = None,
        ocr_processor: OCRProcessorProtocol | None = None,
    ) -> Self:
        """Build the ingest service from shared runtime settings and lazy runtimes."""

        return cls(
            session,
            settings=settings,
            storage_client=storage_client,
            publisher=publisher,
            media_processor=media_processor,
            ocr_processor=ocr_processor,
        )

    async def create_upload(
        self,
        *,
        metadata: ContentPipelineUploadMetadata,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> ContentPipelineUploadRead:
        """Persist an operator upload durably, then publish downstream work exactly once."""

        meme_file_id = uuid.uuid7()
        prepared_upload = await self._prepare_upload(
            meme_file_id=meme_file_id,
            filename=filename,
            content_type=content_type,
            media_bytes=media_bytes,
        )

        await self._put_original_object(prepared_upload=prepared_upload, media_bytes=media_bytes)

        try:
            item, dispatch_event = await self._persist_upload(
                meme_file_id=meme_file_id,
                metadata=metadata,
                prepared_upload=prepared_upload,
            )
        except Exception:
            await self._cleanup_uploaded_object(prepared_upload.object_key)
            raise

        if dispatch_event is None:
            return ContentPipelineUploadRead.model_validate(item.model_dump(mode="python"))

        try:
            await self._publisher(dispatch_event)
        except Exception as exc:
            await self._mark_dispatch_failure(
                meme_file_id=meme_file_id,
                dispatch_event=dispatch_event,
                error=exc,
            )
            raise PipelinePublishError("Upload was stored, but downstream dispatch failed.") from exc

        return ContentPipelineUploadRead.model_validate(item.model_dump(mode="python"))

    async def create_crawler_ingest(self, raw_post: RawCrawlerPost) -> CrawlerIngestResult:
        """Persist one crawler post durably, then publish downstream work exactly once.

        Parallel to :meth:`create_upload` but sourced from the Telegram crawler.
        Reuses the media-inspection, S3 write, auto-dedup, stage-journal, and
        publish-after-commit helpers so the two ingest paths stay byte-identical
        below the row shape. The service layer enforces:

        * unsupported-media short-circuit BEFORE touching S3 or the DB,
        * paused / untracked channel guards BEFORE any durable writes,
        * durable idempotency on ``(platform, source_id, post_id)``,
        * forward-chain attribution preserved on reposter rows,
        * ``last_read_post_id`` monotonic advancement,
        * ``last_fetched_at`` updated on every successful ingest path.
        """

        received_at = utcnow()

        if raw_post.media_type not in _consts.CRAWLER_MEDIA_DEFAULT_FILENAMES:
            # Unsupported media: the caller must not touch S3 or the DB, so
            # we return the terminal outcome immediately. The channel
            # checkpoint stays untouched here because the crawler decides
            # whether this post counts as "scanned" — the service only
            # reports what it saw.
            return CrawlerIngestResult(
                outcome=CrawlerIngestOutcome.SKIPPED_UNSUPPORTED_MEDIA,
                published_at=raw_post.published_at,
                received_at=received_at,
            )

        # Resolve the tracked channel. Curated channels MUST already exist as
        # ``source_channels`` rows — silently accepting a stray crawler post
        # would make the S04 curated-wave contract untrustworthy.
        channel = await get_tracked_source_channel(
            self._session,
            platform=raw_post.platform,
            source_id=raw_post.source_id,
        )
        if channel.is_paused:
            return CrawlerIngestResult(
                outcome=CrawlerIngestOutcome.SKIPPED_PAUSED_CHANNEL,
                published_at=raw_post.published_at,
                received_at=received_at,
            )

        # Idempotency guard: re-deliveries of the same Telegram message hit the
        # existing unique ``(platform, source_id, post_id)`` tuple. We short-
        # circuit with the existing row ids BEFORE touching S3 so replays stay
        # cheap. Checkpoint advancement still happens below because advancing
        # the checkpoint on a known post is correct — the message is durable.
        existing_source_row = await find_existing_crawler_source_row(
            self._session,
            platform=raw_post.platform,
            source_id=raw_post.source_id,
            post_id=raw_post.post_id,
        )
        if existing_source_row is not None:
            await advance_source_channel_checkpoint(
                channel,
                post_id=raw_post.post_id,
                fetched_at=received_at,
            )
            await self._commit_stage_mutation(
                "Failed to persist crawler checkpoint advancement on duplicate post.",
            )
            return CrawlerIngestResult(
                meme_file_id=existing_source_row.file_id,
                meme_source_id=existing_source_row.id,
                outcome=CrawlerIngestOutcome.SKIPPED_DUPLICATE_POST_ID,
                published_at=raw_post.published_at,
                received_at=received_at,
            )

        meme_file_id = uuid.uuid7()
        prepared_upload = await self._prepare_upload(
            meme_file_id=meme_file_id,
            filename=raw_post.filename or _consts.CRAWLER_MEDIA_DEFAULT_FILENAMES[raw_post.media_type],
            content_type=raw_post.content_type or _consts.CRAWLER_MEDIA_DEFAULT_CONTENT_TYPES[raw_post.media_type],
            media_bytes=raw_post.media_bytes,
        )

        await self._put_original_object(
            prepared_upload=prepared_upload,
            media_bytes=raw_post.media_bytes,
        )

        try:
            ingest_result, dispatch_event = await self._persist_crawler_ingest(
                meme_file_id=meme_file_id,
                raw_post=raw_post,
                prepared_upload=prepared_upload,
                channel=channel,
                received_at=received_at,
            )
        except Exception:
            await self._cleanup_uploaded_object(prepared_upload.object_key)
            raise

        if dispatch_event is None:
            return ingest_result

        try:
            await self._publisher(dispatch_event)
        except Exception as exc:
            await self._mark_dispatch_failure(
                meme_file_id=meme_file_id,
                dispatch_event=dispatch_event,
                error=exc,
            )
            raise CrawlerPublishError(
                "Crawler ingest was stored, but downstream dispatch failed.",
            ) from exc

        return ingest_result

    async def get_item(self, meme_file_id: uuid.UUID) -> ContentPipelineItemRead:
        """Return one pipeline item and its stage journal from durable PostgreSQL state."""

        meme_file = await self._get_meme_file(meme_file_id)
        return self._build_item_read(meme_file)

    async def run_search_smoke_proof(
        self,
        *,
        qdrant_sync_client: QdrantSyncClientProtocol,
        qdrant_similarity_client: QdrantSimilarityClientProtocol,
        meilisearch_sync_client: MeilisearchSyncClientProtocol,
        meme_file_id: uuid.UUID,
        query: str | None,
    ) -> SmokeProofResult:
        """Run the dual-target search-sync smoke proof for one pipeline item.

        Thin delegator to :func:`run_smoke_proof` that keeps the reporting
        module import-safe (no SQL session) while giving route handlers a
        familiar ``pipeline_service.run_search_smoke_proof(...)`` entry
        point. The service does not own the Qdrant or Meilisearch clients
        because they must be overridable per request in tests.
        """

        return await run_smoke_proof(
            self._session,
            qdrant_sync_client=qdrant_sync_client,
            qdrant_similarity_client=qdrant_similarity_client,
            meilisearch_sync_client=meilisearch_sync_client,
            meme_file_id=meme_file_id,
            query=query,
        )

    async def resolve_meme_file_id_from_query(
        self,
        *,
        meilisearch_sync_client: MeilisearchSyncClientProtocol,
        query: str,
    ) -> uuid.UUID:
        """Return the top Meilisearch hit's ``meme_file_id`` for the given query.

        Used by the smoke-proof route when an operator passes ``query`` but
        no ``meme_file_id``. Raises :class:`PipelineItemNotFoundError` when
        Meilisearch returns no hits so the route can map it onto a ``404``
        with the standard ``pipeline_item_not_found`` error code. Adapter
        exceptions propagate so the dispatcher surfaces a typed 5xx.
        """

        stripped_query = query.strip()
        if not stripped_query:
            raise PipelinePayloadValidationError(
                "Smoke proof query must not be blank.",
            )
        hits = await meilisearch_sync_client.search(stripped_query, limit=1)
        if not hits:
            raise PipelineItemNotFoundError(
                f"Smoke proof query '{stripped_query}' returned no Meilisearch hits.",
            )
        raw_id = hits[0].get("id") if isinstance(hits[0], dict) else None
        if not isinstance(raw_id, str):
            raise PipelineItemNotFoundError(
                f"Smoke proof query '{stripped_query}' returned a malformed top hit id.",
            )
        try:
            return uuid.UUID(raw_id)
        except ValueError as exc:
            raise PipelineItemNotFoundError(
                f"Smoke proof query '{stripped_query}' returned a malformed top hit id.",
            ) from exc

    async def get_item_detail(self, meme_file_id: uuid.UUID) -> ContentPipelineItemDetail:
        """Return the enriched S02 operator projection with OCR/merge/classify truth.

        The S01 ``get_item`` contract stays untouched; this method builds a
        strict superset that surfaces durable OCR provenance, merge lineage,
        classification, canonical-primary context, and the emitted ``meme_ready``
        event id when the heavy chain has reached classify completion. Optional
        projections are absent (``None`` or empty) whenever the underlying audit
        state has not been produced yet — operators must never see a defaulted
        ``is_nsfw=False`` for an item that has not really been classified.
        """

        meme_file = await self._get_meme_file(meme_file_id)
        base = self._build_item_read(meme_file)
        return await build_item_detail(self._session, meme_file=meme_file, base=base)

    async def list_items(
        self,
        *,
        filter_by: ContentPipelineItemFilter = ContentPipelineItemFilter.FAILED,
        limit: int = _consts.DEFAULT_PIPELINE_ITEMS_LIMIT,
        stuck_after_seconds: int = _consts.DEFAULT_STUCK_AFTER_SECONDS,
    ) -> tuple[ContentPipelineItemRead, ...]:
        """Return operator-visible pipeline items filtered by the current durable state."""

        resolved_limit = max(1, min(limit, _consts.MAX_PIPELINE_ITEMS_LIMIT))
        resolved_stuck_after_seconds = max(stuck_after_seconds, 1)
        stale_before = utcnow() - timedelta(seconds=resolved_stuck_after_seconds)

        result = await self._session.execute(
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .order_by(MemeFile.created_at.desc())
        )

        items: list[ContentPipelineItemRead] = []
        for meme_file in result.scalars().all():
            stage_entries = sorted_stage_entries(meme_file)
            if not stage_entries:
                continue

            current_entry = resolve_current_stage(stage_entries)
            if not matches_list_filter(
                current_entry,
                filter_by=filter_by,
                stale_before=stale_before,
            ):
                continue

            items.append(self._build_item_read(meme_file, stage_entries=stage_entries, current_entry=current_entry))
            if len(items) >= resolved_limit:
                break

        return tuple(items)

    async def replay_item(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage | None = None,
    ) -> ContentPipelineReplayAccepted:
        """Reserve and republish the last retryable failed stage without rewriting ingest state."""

        meme_file = await self._get_meme_file(meme_file_id)
        if not meme_file.s3_original_key:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} is missing durable original storage identifiers.",
            )

        stage_entries = sorted_stage_entries(meme_file)
        target_entry = self._select_replay_entry(stage_entries, requested_stage=stage)

        if is_replay_reserved(target_entry):
            if target_entry.last_event_id is None:
                raise PipelineReplayNotAllowedError(
                    f"Pipeline item {meme_file_id} is already reserved for replay, but its event id is missing.",
                )
            return ContentPipelineReplayAccepted(
                meme_file_id=meme_file.id,
                replay_event_id=target_entry.last_event_id,
                stage=target_entry.stage,
                attempt=max(target_entry.attempt_count, 1),
            )

        replay_attempt = max(target_entry.attempt_count + 1, 1)
        replay_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=target_entry.stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=replay_attempt,
            created_at=utcnow(),
        )
        snapshot = snapshot_stage(target_entry)

        reserve_replay(target_entry, replay_event)
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist replay reservation state.") from exc

        try:
            await self._publisher(replay_event)
        except Exception as exc:
            await self._restore_stage_snapshot(target_entry.id, snapshot)
            raise PipelinePublishError("Replay was reserved, but downstream dispatch failed.") from exc

        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
        )

    async def start_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> PipelineStageWorkContext:
        """Mark one stage as running and return the durable media context workers need."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        started_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.PROCESSING
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = None
        stage_entry.last_error_text = None
        stage_entry.is_retryable = True
        stage_entry.retry_after = None
        stage_entry.started_at = started_at
        stage_entry.finished_at = None
        meme_file.status = ContentProcessingStatus.PROCESSING

        await self._commit_stage_mutation("Failed to persist running stage state.")
        return PipelineStageWorkContext(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            mime_type=meme_file.mime_type,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
        )

    async def mark_stage_processing(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> None:
        """Persist a worker transition from queued to actively running."""

        _ = await self.start_stage_processing(
            meme_file_id=meme_file_id,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_transcode_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        result: NormalizedMediaResult,
    ) -> None:
        """Persist normalized derivative metadata, then advance the durable stage chain."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, ContentPipelineStage.TRANSCODE)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        meme_file.s3_web_video_key = result.web_video_object_key
        meme_file.mime_type = result.mime_type
        meme_file.width = result.width
        meme_file.height = result.height
        meme_file.file_size_bytes = result.file_size_bytes
        meme_file.quality_score = result.quality_score
        meme_file.blur_hash = result.blur_hash
        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.TRANSCODE,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_ocr_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        result: OCRExtractionResult,
    ) -> None:
        """Persist durable OCR provenance, then advance the durable stage chain."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, ContentPipelineStage.OCR)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        if meme_file.ocr_result is None:
            meme_file.ocr_result = MemeFileOCRResult(
                meme_file_id=meme_file.id,
                engine=result.engine,
                fallback_engine=result.fallback_engine,
                fallback_used=result.fallback_used,
                low_confidence=result.low_confidence,
                confidence=result.confidence,
                language=result.language,
                extracted_text=result.extracted_text,
                source_object_key=result.source_object_key,
                last_event_id=event_id,
            )
            self._session.add(meme_file.ocr_result)
        else:
            meme_file.ocr_result.engine = result.engine
            meme_file.ocr_result.fallback_engine = result.fallback_engine
            meme_file.ocr_result.fallback_used = result.fallback_used
            meme_file.ocr_result.low_confidence = result.low_confidence
            meme_file.ocr_result.confidence = result.confidence
            meme_file.ocr_result.language = result.language
            meme_file.ocr_result.extracted_text = result.extracted_text
            meme_file.ocr_result.source_object_key = result.source_object_key
            meme_file.ocr_result.last_event_id = event_id

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.OCR,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_embed_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        embedding_result: VoyageEmbeddingResult,
        similarity_matches: tuple[QdrantSimilarityMatch, ...],
    ) -> MergeOutcome:
        """Persist the durable embedding, run auto-merge, and advance the stage chain."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.EMBED,
        )
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        self._validate_embedding_contract(embedding_result)
        await self._persist_embedding_cache_row(
            meme_file=meme_file,
            embedding_result=embedding_result,
        )

        merge_service = ContentMergeService(
            self._session,
            similarity_threshold=self._settings.pipeline_merge_similarity_threshold,
        )
        try:
            merge_outcome = await merge_service.maybe_merge_after_embed(
                meme_file=meme_file,
                similarity_matches=similarity_matches,
            )
        except PipelineIngestError:
            await self._session.rollback()
            raise
        except Exception as exc:
            # A mid-merge exception (Qdrant outage during similarity lookup, row-lock
            # conflict, or any transient runtime failure) rolls back the single merge
            # transaction and stays replayable — distinct from contract-violation
            # failures above, which surface the base ``PipelineIngestError``.
            await self._session.rollback()
            raise PipelineMergeTransactionError(
                "Failed to apply the post-embed auto-merge transaction.",
            ) from exc

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.EMBED,
            attempt=attempt,
            event_id=event_id,
        )
        return merge_outcome

    async def complete_classify_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        classification_result: ClassificationResult,
    ) -> None:
        """Persist classification truth on the canonical meme and emit meme_ready."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(
            meme_file_id,
            ContentPipelineStage.CLASSIFY,
        )
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        target_meme = await self._get_canonical_meme(meme_file.meme_id)
        target_meme.is_nsfw = classification_result.is_nsfw
        await self._apply_canonical_primary_truth(target_meme)

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=ContentPipelineStage.CLASSIFY,
            attempt=attempt,
            event_id=event_id,
        )

    async def complete_sync_qdrant_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: dict[str, object],
    ) -> PerTargetSyncStatus:
        """Persist a successful Qdrant sync attempt and publish ``meme_qdrant_synced``."""

        return await self._complete_sync_target_stage(
            target=SyncTargetKind.QDRANT,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            payload_preview=payload_preview,
        )

    async def _complete_sync_target_stage(
        self,
        *,
        target: SyncTargetKind,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: dict[str, object],
    ) -> PerTargetSyncStatus:
        """Persist a successful per-target sync attempt and publish the success event.

        Idempotency: running the method twice with the same ``event_id`` reuses
        the existing snapshot row, re-uses the prior success event id on the
        journal row, and does not bump ``attempt_count`` — operators can safely
        drive repeated stage completion through replay without accumulating
        spurious attempts or duplicate events. The Qdrant and Meilisearch
        flows are strictly symmetric and resolved through
        ``SYNC_STAGE_BY_TARGET`` and ``SYNC_SUCCESS_EVENT_TYPE_BY_TARGET``.
        """

        stage = _consts.SYNC_STAGE_BY_TARGET[target]
        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)

        already_succeeded = (
            stage_entry.status is ContentPipelineStageStatus.SUCCEEDED
            and stage_entry.last_event_id == event_id
        )

        preview_model = (
            _build_sync_preview_model(payload_preview, target=target)
            if payload_preview
            else None
        )
        await self._upsert_sync_target_snapshot(
            meme_file_id=meme_file_id,
            target=target,
            status=SyncTargetStatus.SYNCED,
            last_event_id=event_id,
            preview=preview_model,
            normalized_reason=None,
            last_error_text=None,
            bump_attempt=not already_succeeded,
            record_success=True,
        )

        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )

        # Idempotent publish: on repeat calls with the same event id we do not
        # re-emit the success event because the commit above is a no-op under
        # the stage-journal event-id equality check.
        if not already_succeeded:
            await self._publish_sync_success_event(
                meme_file=meme_file,
                stage_entry=stage_entry,
                event_type=_consts.SYNC_SUCCESS_EVENT_TYPE_BY_TARGET[target],
                stage=stage,
            )

        # Re-read the snapshot for the response projection so the caller sees
        # the committed state, not the in-memory mutation.
        return await self._load_sync_target_status(meme_file_id, target)

    async def _publish_sync_success_event(
        self,
        *,
        meme_file: MemeFile,
        stage_entry: PipelineStageJournal,
        event_type: ContentPipelineEventType,
        stage: ContentPipelineStage,
    ) -> None:
        """Publish a post-commit sync-success dispatch event (non-work notification).

        The stage row has already committed as ``SUCCEEDED`` by the time this
        runs; a publish failure must not rewrite the snapshot row (the sync
        did happen), so we record the publish failure as a terminal-replay
        reason on the stage row via the existing ``mark_stage_failed`` path
        and re-raise :class:`PipelinePublishError` so the runtime can
        classify the failure using the standard dispatcher.
        """

        dispatch_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=event_type,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=max(stage_entry.attempt_count, 1),
            created_at=utcnow(),
        )
        try:
            await self._publisher(dispatch_event)
        except Exception as exc:
            # The snapshot row stays truthful as SYNCED (the sync succeeded);
            # we only need to record the publish failure as a replayable
            # stage-row reason so operators can see and replay it.
            await self._mark_sync_success_publish_failure(
                meme_file_id=meme_file.id,
                stage=stage,
                attempt=dispatch_event.attempt,
                event_id=dispatch_event.event_id,
                error=exc,
            )
            raise PipelinePublishError(
                f"Sync target {stage.value} succeeded, but the status notification dispatch failed.",
            ) from exc

    async def _mark_sync_success_publish_failure(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        error: Exception,
    ) -> None:
        """Record a sync-success publish failure as a replayable stage-row reason."""

        try:
            await self.mark_stage_failed(
                meme_file_id=meme_file_id,
                stage=stage,
                attempt=attempt,
                event_id=event_id,
                normalized_reason=_consts.PIPELINE_REASON_PUBLISH_FAILED,
                last_error_text=str(error),
                retryable=True,
            )
        except (PipelineIngestError, PipelineItemNotFoundError):
            return

    async def fail_sync_qdrant_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> PerTargetSyncStatus:
        """Persist a failed Qdrant sync attempt without publishing any success event."""

        return await self._fail_sync_target_stage(
            target=SyncTargetKind.QDRANT,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
        )

    async def complete_sync_meili_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        payload_preview: dict[str, object],
    ) -> PerTargetSyncStatus:
        """Persist a successful Meilisearch sync attempt and publish ``meme_meili_synced``."""

        return await self._complete_sync_target_stage(
            target=SyncTargetKind.MEILISEARCH,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            payload_preview=payload_preview,
        )

    async def fail_sync_meili_stage(
        self,
        *,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> PerTargetSyncStatus:
        """Persist a failed Meilisearch sync attempt without publishing any success event."""

        return await self._fail_sync_target_stage(
            target=SyncTargetKind.MEILISEARCH,
            meme_file_id=meme_file_id,
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
        )

    async def _fail_sync_target_stage(
        self,
        *,
        target: SyncTargetKind,
        meme_file_id: uuid.UUID,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
    ) -> PerTargetSyncStatus:
        """Persist a failed per-target sync attempt without publishing any success event.

        The journal row is delegated to :meth:`mark_stage_failed` so retryability
        logic stays shared with the S02 heavy stages. Prior ``last_success_at``
        timestamps and preview payloads are preserved so operators can still see
        when the target last worked even after a transient failure. Retryability
        is derived from the per-target malformed-payload reason because the
        runtime owns the exception-to-reason mapping and the service honors it
        verbatim.
        """

        is_retryable = normalized_reason != _consts.SYNC_MALFORMED_REASON_BY_TARGET[target]
        await self._upsert_sync_target_snapshot(
            meme_file_id=meme_file_id,
            target=target,
            status=SyncTargetStatus.FAILED,
            last_event_id=event_id,
            preview=None,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            bump_attempt=True,
            record_success=False,
        )
        await self.mark_stage_failed(
            meme_file_id=meme_file_id,
            stage=_consts.SYNC_STAGE_BY_TARGET[target],
            attempt=attempt,
            event_id=event_id,
            normalized_reason=normalized_reason,
            last_error_text=last_error_text,
            retryable=is_retryable,
        )
        return await self._load_sync_target_status(meme_file_id, target)

    async def replay_sync_target(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
    ) -> ContentPipelineReplayAccepted:
        """Queue a per-target sync replay for one pipeline item.

        Mirrors the S01 single-stage replay path but scoped to the ``sync_*``
        row for the requested target. The classify stage must have already
        succeeded — otherwise the per-target sync truth has no ready canonical
        state to advertise and the replay would race with the heavy chain.
        Replays for one target never touch the other target's snapshot row.
        """

        await self._ensure_sync_replay_allowed(meme_file_id)
        return await self._replay_single_sync_target(meme_file_id, target)

    async def replay_sync_target_batch(
        self,
        meme_file_ids: Sequence[uuid.UUID],
        target: SyncTargetKind,
    ) -> tuple[ContentPipelineReplayAccepted, ...]:
        """Queue per-target sync replays for a bounded batch of pipeline items.

        The bounded batch size guard runs at the service layer so route handlers
        cannot accidentally bypass it. Every item in the batch must have already
        reached classify completion; the classify guard fires before any runtime
        work to keep partial-replay bugs impossible.
        """

        if len(meme_file_ids) > SYNC_REPLAY_BATCH_MAX:
            raise PipelineReplayNotAllowedError(
                "Sync replay batch size "
                f"{len(meme_file_ids)} exceeds the configured maximum of {SYNC_REPLAY_BATCH_MAX}.",
            )
        for meme_file_id in meme_file_ids:
            await self._ensure_sync_replay_allowed(meme_file_id)

        accepted: list[ContentPipelineReplayAccepted] = []
        for meme_file_id in meme_file_ids:
            replay = await self._replay_single_sync_target(meme_file_id, target)
            accepted.append(replay)
        return tuple(accepted)

    async def _replay_single_sync_target(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
    ) -> ContentPipelineReplayAccepted:
        """Reserve and republish one per-target sync stage row without touching the other target."""

        stage = _consts.SYNC_STAGE_BY_TARGET[target]
        meme_file = await self._get_meme_file(meme_file_id)
        if not meme_file.s3_original_key:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} is missing durable original storage identifiers.",
            )

        stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is stage),
            None,
        )
        if stage_entry is None:
            # Classify succeeded and fanned out to exactly one downstream stage
            # row (sync_qdrant today, both targets after T03). If operators ask
            # to replay a target that has not been fanned out yet, the replay
            # is genuinely not allowed — we do NOT synthesise a pending row
            # because that would bypass the normal classify-succeeded → fan-out
            # transaction that owns per-target row creation.
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} has no durable {stage.value} stage row yet.",
            )

        if is_replay_reserved(stage_entry):
            if stage_entry.last_event_id is None:
                raise PipelineReplayNotAllowedError(
                    f"Pipeline item {meme_file_id} is already reserved for replay, "
                    "but its event id is missing.",
                )
            return ContentPipelineReplayAccepted(
                meme_file_id=meme_file.id,
                replay_event_id=stage_entry.last_event_id,
                stage=stage,
                attempt=max(stage_entry.attempt_count, 1),
            )

        replay_attempt = max(stage_entry.attempt_count + 1, 1)
        replay_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=ContentPipelineEventType.STAGE_REPLAY_REQUESTED,
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=replay_attempt,
            created_at=utcnow(),
        )
        snapshot = snapshot_stage(stage_entry)
        reserve_replay(stage_entry, replay_event)

        # Mark the per-target snapshot row as an intentional replay so the
        # inspect surface reflects the operator request even before the runtime
        # picks up the dispatch. Prior ``last_success_at`` and preview stay
        # untouched — this is why the helper has a ``record_success=False``
        # flag instead of rewriting the whole row.
        await self._upsert_sync_target_snapshot(
            meme_file_id=meme_file_id,
            target=target,
            status=SyncTargetStatus.PENDING,
            last_event_id=replay_event.event_id,
            preview=None,
            normalized_reason=PIPELINE_REASON_SYNC_REPLAY_REQUESTED,
            last_error_text=None,
            bump_attempt=False,
            record_success=False,
        )
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(
                f"Failed to persist sync replay reservation state for {target.value}.",
            ) from exc

        try:
            await self._publisher(replay_event)
        except Exception as exc:
            await self._restore_stage_snapshot(stage_entry.id, snapshot)
            raise PipelinePublishError(
                f"Sync replay was reserved, but downstream dispatch for {target.value} failed.",
            ) from exc

        return ContentPipelineReplayAccepted(
            meme_file_id=meme_file.id,
            replay_event_id=replay_event.event_id,
            stage=replay_event.stage,
            attempt=replay_event.attempt,
        )

    async def _upsert_sync_target_snapshot(
        self,
        *,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
        status: SyncTargetStatus,
        last_event_id: uuid.UUID | None,
        preview: ContentPipelineSyncTargetPreview | None,
        normalized_reason: str | None,
        last_error_text: str | None,
        bump_attempt: bool,
        record_success: bool,
    ) -> MemeFileSyncTargetSnapshot:
        """Upsert the snapshot row for one ``(meme_file_id, target)`` pair.

        The helper is shared with T03 so the Meilisearch path can call it
        without changes. ``record_success`` bumps ``last_success_at`` and
        refreshes ``last_payload_preview`` to the supplied preview; failure
        callers pass ``record_success=False`` so the last known good
        timestamp and preview stay intact across transient outages.
        """

        now = utcnow()
        snapshots = await self._load_sync_target_snapshots(self._session, meme_file_id)
        existing = snapshots.get(target)

        trimmed_reason = trim_reason(normalized_reason) if normalized_reason is not None else None
        trimmed_error = trim_error_text(last_error_text) if last_error_text is not None else None
        preview_json = preview.model_dump(mode="json") if preview is not None else None

        if existing is None:
            row = MemeFileSyncTargetSnapshot(
                meme_file_id=meme_file_id,
                sync_target=target,
                status=status,
                last_event_id=last_event_id,
                normalized_reason=trimmed_reason,
                last_error_text=trimmed_error,
                last_payload_preview=preview_json or {},
                last_success_at=now if record_success else None,
                last_attempt_at=now,
                attempt_count=1 if bump_attempt else 0,
            )
            self._session.add(row)
            await self._session.flush()
            return row

        existing.status = status
        existing.last_event_id = last_event_id
        existing.normalized_reason = trimmed_reason
        existing.last_error_text = trimmed_error
        existing.last_attempt_at = now
        if bump_attempt:
            existing.attempt_count = existing.attempt_count + 1
        if record_success:
            existing.last_success_at = now
            if preview_json is not None:
                existing.last_payload_preview = preview_json
        await self._session.flush()
        return existing

    async def _load_sync_target_status(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
    ) -> PerTargetSyncStatus:
        """Return the persisted :class:`PerTargetSyncStatus` for one target.

        Reads from a fresh loader call so the caller sees the committed row,
        not the in-memory mutation from the upsert helper. The sync snapshot
        is decoded into the typed :class:`ContentPipelineSyncTargetPreview`
        projection when the stored JSON has a non-empty payload.
        """

        snapshots = await self._load_sync_target_snapshots(self._session, meme_file_id)
        row = snapshots.get(target)
        if row is None:
            raise PipelineIngestError(
                f"Sync target snapshot for {target.value} disappeared after upsert on "
                f"pipeline item {meme_file_id}.",
            )
        return PerTargetSyncStatus(
            target=row.sync_target,
            status=row.status,
            last_event_id=row.last_event_id,
            normalized_reason=row.normalized_reason,
            last_error_text=row.last_error_text,
            last_success_at=row.last_success_at,
            last_attempt_at=row.last_attempt_at,
            attempt_count=row.attempt_count,
            last_preview=decode_sync_preview(row.last_payload_preview, target=target),
        )

    async def get_sync_target_status(
        self,
        meme_file_id: uuid.UUID,
        target: SyncTargetKind,
    ) -> PerTargetSyncStatus:
        """Return the current per-target sync truth for one pipeline item.

        Raises :class:`PipelineItemNotFoundError` when the meme file itself
        does not exist, and when the meme file exists but has no snapshot row
        for the requested target — the route handler maps the latter to a
        ``404`` so operators cannot accidentally conflate "item missing" with
        "item has never been synced to this target".
        """

        _ = await self._get_meme_file(meme_file_id)
        snapshots = await self._load_sync_target_snapshots(self._session, meme_file_id)
        row = snapshots.get(target)
        if row is None:
            raise PipelineItemNotFoundError(
                f"Pipeline item {meme_file_id} has no {target.value} sync snapshot row yet.",
            )
        return PerTargetSyncStatus(
            target=row.sync_target,
            status=row.status,
            last_event_id=row.last_event_id,
            normalized_reason=row.normalized_reason,
            last_error_text=row.last_error_text,
            last_success_at=row.last_success_at,
            last_attempt_at=row.last_attempt_at,
            attempt_count=row.attempt_count,
            last_preview=decode_sync_preview(row.last_payload_preview, target=target),
        )

    async def _ensure_sync_replay_allowed(self, meme_file_id: uuid.UUID) -> None:
        """Raise :class:`PipelineReplayNotAllowedError` when classify has not succeeded.

        The per-target sync truth only makes sense after the heavy chain has
        produced canonical state. Replaying sync before classify succeeds would
        advertise incomplete truth to Qdrant or Meilisearch, so the service
        refuses the request up-front.
        """

        meme_file = await self._get_meme_file(meme_file_id)
        classify_entry = next(
            (
                entry
                for entry in meme_file.pipeline_stage_journal_entries
                if entry.stage is ContentPipelineStage.CLASSIFY
            ),
            None,
        )
        if classify_entry is None or classify_entry.status is not ContentPipelineStageStatus.SUCCEEDED:
            raise PipelineReplayNotAllowedError(
                f"Pipeline item {meme_file_id} cannot replay sync targets before classify succeeds.",
            )

    async def _load_sync_target_snapshots(
        self,
        session: AsyncSession,
        meme_file_id: uuid.UUID,
    ) -> dict[SyncTargetKind, MemeFileSyncTargetSnapshot]:
        """Return the per-target snapshot rows for one meme file keyed by target.

        This helper is the canonical read path T02/T03/T04 rely on so the
        reporting layer and runtime handlers never diverge on how they load
        per-target truth. The method is deliberately small and synchronous in
        spirit (one round-trip) so callers can hold it open inside stage
        transactions without surprising query overhead.
        """

        result = await session.execute(
            select(MemeFileSyncTargetSnapshot).where(
                MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
            )
        )
        return {row.sync_target: row for row in result.scalars().all()}

    async def mark_stage_failed(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
        normalized_reason: str,
        last_error_text: str,
        retryable: bool,
    ) -> None:
        """Persist a failed worker attempt with an explicit retryability decision."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        failed_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.FAILED
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = trim_reason(normalized_reason)
        stage_entry.last_error_text = trim_error_text(last_error_text)
        stage_entry.is_retryable = retryable
        stage_entry.retry_after = (
            failed_at + timedelta(seconds=self._broker_settings.retry_backoff_seconds)
            if retryable
            else None
        )
        stage_entry.started_at = stage_entry.started_at or failed_at
        stage_entry.finished_at = failed_at
        meme_file.status = ContentProcessingStatus.FAILED

        await self._commit_stage_mutation("Failed to persist failed stage state.")

    async def mark_stage_succeeded(
        self,
        *,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> None:
        """Persist a successful worker attempt and enqueue the next durable stage when needed."""

        meme_file, stage_entry = await self._get_meme_file_and_stage_entry(meme_file_id, stage)
        ensure_stage_attempt_is_current(stage_entry, attempt=attempt)
        await self._finalize_stage_success(
            meme_file=meme_file,
            stage_entry=stage_entry,
            stage=stage,
            attempt=attempt,
            event_id=event_id,
        )

    async def _finalize_stage_success(
        self,
        *,
        meme_file: MemeFile,
        stage_entry: PipelineStageJournal,
        stage: ContentPipelineStage,
        attempt: int,
        event_id: uuid.UUID,
    ) -> None:
        finished_at = utcnow()

        stage_entry.status = ContentPipelineStageStatus.SUCCEEDED
        stage_entry.attempt_count = max(stage_entry.attempt_count, attempt)
        stage_entry.last_event_id = event_id
        stage_entry.normalized_reason = None
        stage_entry.last_error_text = None
        stage_entry.is_retryable = False
        stage_entry.retry_after = None
        stage_entry.started_at = stage_entry.started_at or finished_at
        stage_entry.finished_at = finished_at

        downstream_dispatches = self._prepare_downstream_dispatches(
            meme_file=meme_file,
            stage=stage,
            created_at=finished_at,
        )
        # Classification is the terminal heavy-worker stage: once it succeeds the file
        # is truly downstream-ready even though we still publish MEME_READY so S03's
        # sync_qdrant/sync_meili consumers can pick up the canonical state. Sync
        # stages must not regress that READY truth — they advertise search-target
        # visibility independently of the file's processing lifecycle. Every other
        # upstream stage leaves the file in PROCESSING so the readiness truth only
        # flips through the classify path.
        if stage is ContentPipelineStage.CLASSIFY:
            meme_file.status = ContentProcessingStatus.READY
        elif stage not in {ContentPipelineStage.SYNC_QDRANT, ContentPipelineStage.SYNC_MEILI}:
            meme_file.status = ContentProcessingStatus.PROCESSING

        # Classify fan-out is the only case that must be atomic across BOTH
        # sync-target dispatches: publishing either event must not leave one
        # sync stage row persisted while the other fails to dispatch. We
        # therefore publish before committing, so a rollback undoes both the
        # classify success AND the new sync stage rows in one shot when any
        # publish fails. Every other stage keeps the existing commit-then-
        # publish ordering because those downstream transitions are already
        # single-target and their tests expect the predecessor to remain
        # SUCCEEDED after a downstream publish failure.
        if stage is ContentPipelineStage.CLASSIFY and downstream_dispatches:
            await self._atomic_publish_then_commit_fan_out(
                downstream_dispatches=downstream_dispatches,
            )
            return

        await self._commit_stage_mutation("Failed to persist successful stage state.")

        for dispatch in downstream_dispatches:
            try:
                await self._publisher(dispatch.event)
            except Exception as exc:
                await self._mark_publish_failure_for_downstream_stage(dispatch=dispatch, error=exc)
                raise PipelinePublishError("Stage success was stored, but downstream dispatch failed.") from exc

    async def _atomic_publish_then_commit_fan_out(
        self,
        *,
        downstream_dispatches: tuple[DownstreamStageDispatch, ...],
    ) -> None:
        """Publish every fan-out dispatch first, then commit — atomic on failure.

        Flushing the in-memory stage rows before publishing gives the new rows
        their ``id`` values so the publisher can reference them, but the
        surrounding transaction stays open. If ANY publish fails we rollback
        the session so the classify mutation AND both new sync stage rows
        never land in the DB, and then re-raise :class:`PipelinePublishError`
        so the runtime dispatcher can classify the failure and dead-letter or
        retry. If every publish succeeds we commit once and the whole fan-out
        becomes durable at the same instant.
        """

        try:
            await self._session.flush()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(
                "Failed to flush fan-out stage rows before publish.",
            ) from exc

        for dispatch in downstream_dispatches:
            try:
                await self._publisher(dispatch.event)
            except Exception as exc:
                await self._session.rollback()
                raise PipelinePublishError(
                    "Classify fan-out publish failed; rolled back both sync stage rows.",
                ) from exc

        await self._commit_stage_mutation("Failed to persist successful stage state.")

    async def _prepare_upload(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> PreparedUpload:
        normalized_filename = normalize_filename(filename)
        normalized_content_type = normalize_content_type(content_type)
        file_size_bytes = len(media_bytes)
        if file_size_bytes <= 0:
            raise PipelinePayloadValidationError("Uploaded file is empty.")

        try:
            inspected_media = await self._media_processor.inspect_upload(
                filename=normalized_filename,
                content_type=normalized_content_type,
                media_bytes=media_bytes,
            )
        except Exception as exc:
            raise translate_media_processing_error(exc) from exc

        upload_limit = self._upload_limit_for_media_type(inspected_media.media_type)
        if file_size_bytes > upload_limit:
            raise PipelinePayloadTooLargeError(f"Uploaded file exceeds the {upload_limit}-byte limit.")
        if len(inspected_media.perceptual_hash) > _consts.MAX_PERCEPTUAL_HASH_LENGTH:
            raise PipelineIngestError(
                "Configured perceptual-hash size exceeds the persisted meme_files.perceptual_hash contract.",
            )

        return PreparedUpload(
            filename=normalized_filename,
            media_type=inspected_media.media_type,
            mime_type=inspected_media.mime_type,
            file_size_bytes=inspected_media.file_size_bytes,
            width=inspected_media.width,
            height=inspected_media.height,
            perceptual_hash=inspected_media.perceptual_hash,
            object_key=build_original_object_key(
                meme_file_id,
                normalized_filename,
                settings=self._settings,
            ),
        )

    async def _persist_upload(
        self,
        *,
        meme_file_id: uuid.UUID,
        metadata: ContentPipelineUploadMetadata,
        prepared_upload: PreparedUpload,
    ) -> tuple[ContentPipelineItemRead, ContentPipelineDispatchEvent | None]:
        now = utcnow()

        try:
            await self._ensure_source_identifier_is_available(metadata)
            duplicate_match = await self._find_duplicate_match(prepared_upload.perceptual_hash)

            if duplicate_match is None:
                meme_id = uuid.uuid7()
                publish_event_id = uuid.uuid7()
                dispatch_event = ContentPipelineDispatchEvent(
                    event_id=publish_event_id,
                    event_type=ContentPipelineEventType.MEME_CREATED,
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.TRANSCODE,
                    source_kind=ContentSourceKind.MANUAL_UPLOAD,
                    original_object_key=prepared_upload.object_key,
                    attempt=1,
                    created_at=now,
                )
                await self._create_new_upload_rows(
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    metadata=metadata,
                    prepared_upload=prepared_upload,
                    publish_event_id=publish_event_id,
                    created_at=now,
                )
            else:
                dispatch_event = None
                await self._create_duplicate_upload_rows(
                    duplicate_match=duplicate_match,
                    meme_file_id=meme_file_id,
                    metadata=metadata,
                    prepared_upload=prepared_upload,
                    created_at=now,
                )

            await self._session.commit()
        except (PipelinePayloadValidationError, PipelineSourceConflictError):
            await self._session.rollback()
            raise
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist the upload and journal state.") from exc

        item = await self.get_item(meme_file_id)
        return item, dispatch_event

    async def _persist_crawler_ingest(
        self,
        *,
        meme_file_id: uuid.UUID,
        raw_post: RawCrawlerPost,
        prepared_upload: PreparedUpload,
        channel: SourceChannel,
        received_at: datetime,
    ) -> tuple[CrawlerIngestResult, ContentPipelineDispatchEvent | None]:
        """Commit the crawler ingest rows in one transaction and return the result.

        This helper mirrors :meth:`_persist_upload` but diverges on the row
        shapes: the crawler ingest always adds a forward-attribution-aware
        ``MemeSource`` row, populates ``published_at``, and advances the
        ``SourceChannel`` checkpoint. Everything else (dispatch event,
        duplicate-detection, new-meme creation) uses the same helpers the
        upload path uses.
        """

        try:
            duplicate_match = await self._find_duplicate_match(prepared_upload.perceptual_hash)
            is_forwarded = raw_post.forward is not None

            if duplicate_match is None:
                meme_id = uuid.uuid7()
                publish_event_id = uuid.uuid7()
                dispatch_event: ContentPipelineDispatchEvent | None = ContentPipelineDispatchEvent(
                    event_id=publish_event_id,
                    event_type=ContentPipelineEventType.MEME_CREATED,
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.TRANSCODE,
                    source_kind=ContentSourceKind.TELEGRAM,
                    original_object_key=prepared_upload.object_key,
                    attempt=1,
                    created_at=received_at,
                )
                new_source_row = await self._create_new_crawler_ingest_rows(
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    raw_post=raw_post,
                    prepared_upload=prepared_upload,
                    publish_event_id=publish_event_id,
                    received_at=received_at,
                    is_first_source=not is_forwarded,
                )
                outcome = CrawlerIngestOutcome.INGESTED
                duplicate_of_meme_id: uuid.UUID | None = None
            else:
                dispatch_event = None
                # For the dedupe branch we do NOT create a new meme or stage
                # journal; we only attach a new ``MemeSource`` row to the
                # existing ``MemeFile`` and mark ``is_first_source`` based on
                # whether this observation is a forwarded repost AND whether
                # no existing row has claimed first-source status yet.
                any_existing_first_source = await meme_file_has_first_source(
                    self._session,
                    duplicate_match.id,
                )
                should_claim_first_source = (not is_forwarded) and not any_existing_first_source
                new_source_row = attach_crawler_source_row_to_meme_file(
                    self._session,
                    meme_file=duplicate_match,
                    raw_post=raw_post,
                    is_first_source=should_claim_first_source,
                )
                meme_id = duplicate_match.meme_id
                meme_file_id = duplicate_match.id
                outcome = CrawlerIngestOutcome.DEDUPLICATED_EXACT
                duplicate_of_meme_id = duplicate_match.meme_id

            await advance_source_channel_checkpoint(
                channel,
                post_id=raw_post.post_id,
                fetched_at=received_at,
            )
            await self._session.commit()
        except PipelinePayloadValidationError:
            await self._session.rollback()
            raise
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(
                "Failed to persist the crawler ingest and journal state.",
            ) from exc

        # Refresh the source row after commit so callers see the id set.
        await self._session.refresh(new_source_row)

        return (
            CrawlerIngestResult(
                meme_file_id=meme_file_id,
                meme_source_id=new_source_row.id,
                outcome=outcome,
                duplicate_of_meme_id=duplicate_of_meme_id,
                published_at=raw_post.published_at,
                received_at=received_at,
            ),
            dispatch_event,
        )

    async def _create_new_crawler_ingest_rows(
        self,
        *,
        meme_id: uuid.UUID,
        meme_file_id: uuid.UUID,
        raw_post: RawCrawlerPost,
        prepared_upload: PreparedUpload,
        publish_event_id: uuid.UUID,
        received_at: datetime,
        is_first_source: bool,
    ) -> MemeSource:
        """Insert the Meme + MemeFile + MemeSource + stage rows for a new crawler post."""

        meme = Meme(
            id=meme_id,
            media_type=prepared_upload.media_type,
            language=ContentLanguage.NONE,
            is_public=False,
        )
        self._session.add(meme)
        await self._session.flush()

        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.PENDING,
            width=prepared_upload.width,
            height=prepared_upload.height,
            file_size_bytes=prepared_upload.file_size_bytes,
            mime_type=prepared_upload.mime_type,
            s3_original_key=prepared_upload.object_key,
            perceptual_hash=prepared_upload.perceptual_hash,
            is_primary=True,
        )
        self._session.add(meme_file)
        await self._session.flush()

        meme.primary_file_id = meme_file_id
        new_source_row = MemeSource(
            file_id=meme_file_id,
            platform=raw_post.platform,
            source_id=raw_post.source_id,
            post_id=raw_post.post_id,
            views=raw_post.views,
            reactions=dict(raw_post.reactions),
            is_first_source=is_first_source,
            source_alive=True,
            published_at=raw_post.published_at,
            forwarded_from_source_id=raw_post.forward.source_id if raw_post.forward is not None else None,
            forwarded_from_post_id=raw_post.forward.post_id if raw_post.forward is not None else None,
        )
        self._session.add_all(
            [
                new_source_row,
                PipelineStageJournal(
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=publish_event_id,
                    is_retryable=False,
                    started_at=received_at,
                    finished_at=received_at,
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
        await self._session.flush()
        return new_source_row

    async def _create_new_upload_rows(
        self,
        *,
        meme_id: uuid.UUID,
        meme_file_id: uuid.UUID,
        metadata: ContentPipelineUploadMetadata,
        prepared_upload: PreparedUpload,
        publish_event_id: uuid.UUID,
        created_at: datetime,
    ) -> None:
        meme = Meme(
            id=meme_id,
            media_type=prepared_upload.media_type,
            language=ContentLanguage.NONE,
            is_public=False,
        )
        self._session.add(meme)
        await self._session.flush()

        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.PENDING,
            width=prepared_upload.width,
            height=prepared_upload.height,
            file_size_bytes=prepared_upload.file_size_bytes,
            mime_type=prepared_upload.mime_type,
            s3_original_key=prepared_upload.object_key,
            perceptual_hash=prepared_upload.perceptual_hash,
            is_primary=True,
        )
        self._session.add(meme_file)
        await self._session.flush()

        meme.primary_file_id = meme_file_id
        self._session.add_all(
            [
                MemeSource(
                    file_id=meme_file_id,
                    platform=metadata.source_platform,
                    source_id=metadata.source_id,
                    post_id=metadata.post_id,
                    views=metadata.views,
                    reactions={},
                    is_first_source=True,
                    source_alive=True,
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
        await self._session.flush()

    async def _create_duplicate_upload_rows(
        self,
        *,
        duplicate_match: MemeFile,
        meme_file_id: uuid.UUID,
        metadata: ContentPipelineUploadMetadata,
        prepared_upload: PreparedUpload,
        created_at: datetime,
    ) -> None:
        duplicate_event_id = uuid.uuid7()
        self._session.add_all(
            [
                MemeFile(
                    id=meme_file_id,
                    meme_id=duplicate_match.meme_id,
                    status=ContentProcessingStatus.FAILED,
                    width=prepared_upload.width,
                    height=prepared_upload.height,
                    file_size_bytes=prepared_upload.file_size_bytes,
                    mime_type=prepared_upload.mime_type,
                    s3_original_key=prepared_upload.object_key,
                    perceptual_hash=prepared_upload.perceptual_hash,
                    is_primary=False,
                ),
                MemeSource(
                    file_id=meme_file_id,
                    platform=metadata.source_platform,
                    source_id=metadata.source_id,
                    post_id=metadata.post_id,
                    views=metadata.views,
                    reactions={},
                    is_first_source=False,
                    source_alive=True,
                ),
                PipelineStageJournal(
                    meme_file_id=meme_file_id,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.DUPLICATE,
                    attempt_count=1,
                    last_event_id=duplicate_event_id,
                    normalized_reason="duplicate_perceptual_hash",
                    last_error_text=(
                        f"Exact duplicate matched existing meme_file_id {duplicate_match.id}."
                    ),
                    is_retryable=False,
                    started_at=created_at,
                    finished_at=created_at,
                ),
            ]
        )
        await self._session.flush()

    async def _find_duplicate_match(self, perceptual_hash: str) -> MemeFile | None:
        result = await self._session.execute(
            select(MemeFile)
            .where(MemeFile.perceptual_hash == perceptual_hash)
            .order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _ensure_source_identifier_is_available(self, metadata: ContentPipelineUploadMetadata) -> None:
        result = await self._session.execute(
            select(MemeSource)
            .where(
                MemeSource.platform == metadata.source_platform,
                MemeSource.source_id == metadata.source_id,
                MemeSource.post_id == metadata.post_id,
            )
            .limit(1)
        )
        existing_source = result.scalar_one_or_none()
        if existing_source is not None:
            raise PipelineSourceConflictError(
                "Source platform/source_id/post_id is already attached to an uploaded item.",
            )

    async def _put_original_object(
        self,
        *,
        prepared_upload: PreparedUpload,
        media_bytes: bytes,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._storage_client.put_object,
                Bucket=self._storage_settings.bucket,
                Key=prepared_upload.object_key,
                Body=media_bytes,
                ContentType=prepared_upload.mime_type,
                ContentLength=prepared_upload.file_size_bytes,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to store the uploaded original in S3-compatible storage.") from exc

    async def _cleanup_uploaded_object(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._storage_client.delete_object,
                Bucket=self._storage_settings.bucket,
                Key=object_key,
            )
        except Exception:
            return

    async def _publish_dispatch_event(self, event: ContentPipelineDispatchEvent) -> None:
        broker = await ensure_pipeline_broker_started(settings=self._settings)
        payload = event.model_dump(mode="json")
        _ = await broker.publish(
            payload,
            exchange=self._broker_settings.exchange,
            routing_key=self._resolve_routing_key_for_event(event),
            persist=True,
            content_type="application/json",
            message_id=str(event.event_id),
            timestamp=event.created_at,
            mandatory=True,
        )

    def _resolve_routing_key_for_event(self, event: ContentPipelineDispatchEvent) -> str:
        """Select the routing key to use when publishing a dispatch or status event.

        Stage dispatches (e.g. ``meme_ready`` → sync_qdrant) route by stage so
        the appropriate worker queue consumes them. Status events
        (``meme_qdrant_synced``, ``meme_meili_synced``) route by event type to
        a distinct topic key so the sync workers never re-consume their own
        success notifications.
        """

        status_event_types = {
            ContentPipelineEventType.MEME_QDRANT_SYNCED,
            ContentPipelineEventType.MEME_MEILI_SYNCED,
        }
        if event.event_type in status_event_types:
            return self._broker_settings.routing_key_for_event(event.event_type)
        return self._broker_settings.routing_key_for_stage(event.stage)

    async def _mark_dispatch_failure(
        self,
        *,
        meme_file_id: uuid.UUID,
        dispatch_event: ContentPipelineDispatchEvent,
        error: Exception,
    ) -> None:
        try:
            await self.mark_stage_failed(
                meme_file_id=meme_file_id,
                stage=dispatch_event.stage,
                attempt=dispatch_event.attempt,
                event_id=dispatch_event.event_id,
                normalized_reason=_consts.PIPELINE_REASON_PUBLISH_FAILED,
                last_error_text=str(error),
                retryable=True,
            )
        except PipelineIngestError:
            return
        except PipelineItemNotFoundError:
            return

    async def _get_meme_file(self, meme_file_id: uuid.UUID) -> MemeFile:
        result = await self._session.execute(
            select(MemeFile)
            .options(
                selectinload(MemeFile.meme),
                selectinload(MemeFile.ocr_result),
                selectinload(MemeFile.pipeline_stage_journal_entries),
            )
            .where(MemeFile.id == meme_file_id)
        )
        meme_file = result.scalar_one_or_none()
        if meme_file is None:
            raise PipelineItemNotFoundError(f"Pipeline item {meme_file_id} does not exist.")
        return meme_file

    async def _get_canonical_meme(self, meme_id: uuid.UUID) -> Meme:
        result = await self._session.execute(select(Meme).where(Meme.id == meme_id))
        meme = result.scalar_one_or_none()
        if meme is None:
            raise PipelineIngestError(
                f"Canonical meme {meme_id} is missing when finalizing classification.",
            )
        return meme

    async def _apply_canonical_primary_truth(self, meme: Meme) -> None:
        primary_file_id = meme.primary_file_id
        if primary_file_id is None:
            meme.ocr_text = None
            meme.language = ContentLanguage.NONE
            return

        result = await self._session.execute(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == primary_file_id)
        )
        ocr_row = result.scalar_one_or_none()
        if ocr_row is None:
            meme.ocr_text = None
            meme.language = ContentLanguage.NONE
            return

        meme.ocr_text = ocr_row.extracted_text
        meme.language = ocr_row.language

    def _validate_embedding_contract(self, embedding_result: VoyageEmbeddingResult) -> None:
        expected_dimensions = self._settings.pipeline_voyage_output_dimensions
        if embedding_result.dimensions != expected_dimensions:
            raise PipelineIngestError(
                "Rejected embedding vector with unexpected dimensionality "
                f"(got {embedding_result.dimensions}, expected {expected_dimensions}).",
            )
        if len(embedding_result.vector) != expected_dimensions:
            raise PipelineIngestError(
                "Rejected embedding vector whose length does not match the declared dimensions.",
            )
        try:
            _ = embedding_result.embedding_bytes
        except VoyageMalformedResponseError as exc:
            raise PipelineIngestError(str(exc)) from exc

    async def _persist_embedding_cache_row(
        self,
        *,
        meme_file: MemeFile,
        embedding_result: VoyageEmbeddingResult,
    ) -> None:
        existing_result = await self._session.execute(
            select(EmbeddingCache).where(
                EmbeddingCache.input_hash == embedding_result.input_hash,
                EmbeddingCache.model_version == embedding_result.model,
                EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
            )
        )
        existing_cache_row = existing_result.scalar_one_or_none()
        if existing_cache_row is not None:
            existing_cache_row.embedding = embedding_result.embedding_bytes
            existing_cache_row.source_file_id = meme_file.id
            await self._session.flush()
            return

        self._session.add(
            EmbeddingCache(
                input_hash=embedding_result.input_hash,
                input_type=EmbeddingInputType.IMAGE,
                embedding=embedding_result.embedding_bytes,
                model_version=embedding_result.model,
                source_file_id=meme_file.id,
            )
        )
        await self._session.flush()

    async def _get_meme_file_and_stage_entry(
        self,
        meme_file_id: uuid.UUID,
        stage: ContentPipelineStage,
    ) -> tuple[MemeFile, PipelineStageJournal]:
        meme_file = await self._get_meme_file(meme_file_id)
        stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is stage),
            None,
        )
        if stage_entry is None:
            raise PipelineIngestError(
                f"Pipeline item {meme_file_id} does not have durable journal state for stage {stage.value}."
            )
        return meme_file, stage_entry

    async def _commit_stage_mutation(self, failure_message: str) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError(failure_message) from exc

    def _prepare_downstream_dispatches(
        self,
        *,
        meme_file: MemeFile,
        stage: ContentPipelineStage,
        created_at: datetime,
    ) -> tuple[DownstreamStageDispatch, ...]:
        if stage is ContentPipelineStage.CLASSIFY:
            return self._prepare_classify_fan_out_dispatches(
                meme_file=meme_file,
                created_at=created_at,
            )

        next_stage = _consts.NEXT_STAGE_BY_STAGE.get(stage)
        if next_stage is None:
            return ()

        existing_stage_entry = next(
            (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is next_stage),
            None,
        )
        if existing_stage_entry is not None:
            return ()

        dispatch = self._build_downstream_dispatch(
            meme_file=meme_file,
            predecessor_stage=stage,
            next_stage=next_stage,
            created_at=created_at,
        )
        return (dispatch,)

    def _prepare_classify_fan_out_dispatches(
        self,
        *,
        meme_file: MemeFile,
        created_at: datetime,
    ) -> tuple[DownstreamStageDispatch, ...]:
        """Build both sync-target dispatches classify success must hand off to.

        The fan-out is all-or-nothing: if either sync stage row already exists
        (e.g. a prior classify completion is being retried and one downstream
        stage was already created), we skip creating any new rows so the
        atomic-commit contract stays honest. ``_finalize_stage_success``
        relies on this invariant to decide whether to use the publish-then-
        commit path or the normal single-target commit-then-publish path.
        """

        dispatches: list[DownstreamStageDispatch] = []
        for next_stage in _consts.CLASSIFY_FAN_OUT_STAGES:
            existing_stage_entry = next(
                (entry for entry in meme_file.pipeline_stage_journal_entries if entry.stage is next_stage),
                None,
            )
            if existing_stage_entry is not None:
                # Any prior sync stage row present means the fan-out is already
                # past its one-shot creation point; classify completion becomes
                # idempotent (no new rows, no new dispatches).
                return ()
            dispatches.append(
                self._build_downstream_dispatch(
                    meme_file=meme_file,
                    predecessor_stage=ContentPipelineStage.CLASSIFY,
                    next_stage=next_stage,
                    created_at=created_at,
                )
            )
        return tuple(dispatches)

    def _build_downstream_dispatch(
        self,
        *,
        meme_file: MemeFile,
        predecessor_stage: ContentPipelineStage,
        next_stage: ContentPipelineStage,
        created_at: datetime,
    ) -> DownstreamStageDispatch:
        """Assemble one in-memory ``DownstreamStageDispatch`` and attach the new row.

        Shared by the single-target and dual-target fan-out paths so the stage
        row and dispatch event shapes stay identical across both contexts.
        The row is added to the session but not flushed — the caller owns the
        flush/commit ordering so the classify fan-out can publish events
        before committing while the other stages commit first.
        """

        dispatch_event = ContentPipelineDispatchEvent(
            event_id=uuid.uuid7(),
            event_type=_consts.DOWNSTREAM_STAGE_EVENT_TYPES[predecessor_stage],
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            stage=next_stage,
            source_kind=ContentSourceKind.MANUAL_UPLOAD,
            original_object_key=meme_file.s3_original_key,
            attempt=1,
            created_at=created_at,
        )
        stage_entry = PipelineStageJournal(
            meme_file_id=meme_file.id,
            stage=next_stage,
            status=ContentPipelineStageStatus.PENDING,
            attempt_count=0,
            last_event_id=dispatch_event.event_id,
            is_retryable=True,
        )
        self._session.add(stage_entry)
        meme_file.pipeline_stage_journal_entries.append(stage_entry)
        return DownstreamStageDispatch(event=dispatch_event, stage_entry=stage_entry)

    async def _mark_publish_failure_for_downstream_stage(
        self,
        *,
        dispatch: DownstreamStageDispatch,
        error: Exception,
    ) -> None:
        try:
            await self.mark_stage_failed(
                meme_file_id=dispatch.stage_entry.meme_file_id,
                stage=dispatch.stage_entry.stage,
                attempt=dispatch.event.attempt,
                event_id=dispatch.event.event_id,
                normalized_reason=_consts.PIPELINE_REASON_PUBLISH_FAILED,
                last_error_text=str(error),
                retryable=True,
            )
        except (PipelineIngestError, PipelineItemNotFoundError):
            return

    async def _restore_stage_snapshot(self, stage_entry_id: uuid.UUID, snapshot: StageJournalSnapshot) -> None:
        try:
            result = await self._session.execute(
                select(PipelineStageJournal).where(PipelineStageJournal.id == stage_entry_id)
            )
            stage_entry = result.scalar_one_or_none()
            if stage_entry is None:
                raise PipelineIngestError(
                    f"Replay reservation for stage journal {stage_entry_id} disappeared before restore.",
                )

            stage_entry.status = snapshot.status
            stage_entry.attempt_count = snapshot.attempt_count
            stage_entry.last_event_id = snapshot.last_event_id
            stage_entry.normalized_reason = snapshot.normalized_reason
            stage_entry.last_error_text = snapshot.last_error_text
            stage_entry.is_retryable = snapshot.is_retryable
            stage_entry.retry_after = snapshot.retry_after
            stage_entry.started_at = snapshot.started_at
            stage_entry.finished_at = snapshot.finished_at
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise

    def _build_item_read(
        self,
        meme_file: MemeFile,
        *,
        stage_entries: tuple[PipelineStageJournal, ...] | None = None,
        current_entry: PipelineStageJournal | None = None,
    ) -> ContentPipelineItemRead:
        resolved_stage_entries = stage_entries or sorted_stage_entries(meme_file)
        if not resolved_stage_entries:
            raise PipelineIngestError(f"Pipeline item {meme_file.id} is missing journal state.")

        resolved_current_entry = current_entry or resolve_current_stage(resolved_stage_entries)
        return ContentPipelineItemRead(
            meme_id=meme_file.meme_id,
            meme_file_id=meme_file.id,
            current_stage=resolved_current_entry.stage,
            current_status=resolved_current_entry.status,
            original_object_key=meme_file.s3_original_key,
            web_video_object_key=meme_file.s3_web_video_key,
            last_event_id=resolved_current_entry.last_event_id,
            normalized_reason=resolved_current_entry.normalized_reason,
            last_error_text=resolved_current_entry.last_error_text,
            attempt_count=resolved_current_entry.attempt_count,
            stages=tuple(ContentPipelineStageJournalRead.model_validate(entry) for entry in resolved_stage_entries),
        )

    def _select_replay_entry(
        self,
        stage_entries: tuple[PipelineStageJournal, ...],
        *,
        requested_stage: ContentPipelineStage | None,
    ) -> PipelineStageJournal:
        if requested_stage is not None:
            requested_entry = next(
                (entry for entry in stage_entries if entry.stage is requested_stage),
                None,
            )
            if requested_entry is None:
                raise PipelineReplayNotAllowedError(
                    f"Stage {requested_stage.value} has no durable journal row for this pipeline item.",
                )
            if is_replay_reserved(requested_entry):
                return requested_entry
            if requested_entry.status is not ContentPipelineStageStatus.FAILED or not requested_entry.is_retryable:
                raise PipelineReplayNotAllowedError(
                    f"Stage {requested_stage.value} is not in a retryable failed state.",
                )
            return requested_entry

        for stage_entry in reversed(stage_entries):
            if is_replay_reserved(stage_entry):
                return stage_entry
            if stage_entry.status is ContentPipelineStageStatus.FAILED and stage_entry.is_retryable:
                return stage_entry

        raise PipelineReplayNotAllowedError("No failed retryable stage exists for this pipeline item.")

    def _upload_limit_for_media_type(self, media_type: ContentKind) -> int:
        if media_type is ContentKind.IMAGE:
            return self._settings.pipeline_image_upload_max_bytes
        if media_type is ContentKind.GIF:
            return self._settings.pipeline_gif_upload_max_bytes
        if media_type is ContentKind.VIDEO:
            return self._settings.pipeline_video_upload_max_bytes
        return self._settings.pipeline_image_upload_max_bytes


__all__ = [
    "PIPELINE_REASON_SYNC_REPLAY_REQUESTED",
    "SYNC_REPLAY_BATCH_MAX",
    "ContentPipelineService",
    "ContentPipelineUploadRead",
    "PreparedUpload",
    "StageJournalSnapshot",
]
