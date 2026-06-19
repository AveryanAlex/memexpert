"""API-safe Telegram crawler ingest wrapper over raw ingest requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.ingest.accept_service import PipelineIngestAcceptService
from memexpert.ingest.schemas import IngestAcceptOutcome, IngestAcceptResult, IngestAcceptSource, IngestRequestRead
from memexpert.models.base import utcnow
from memexpert.models.content import MemeSource, PipelineIngestRequest, SourceChannel
from memexpert.models.enums import SourceAttachReason, SourcePlatform
from memexpert.pipeline.constants import (
    CRAWLER_MEDIA_DEFAULT_CONTENT_TYPES,
    CRAWLER_MEDIA_DEFAULT_FILENAMES,
)
from memexpert.pipeline.crawler_queries import (
    advance_source_channel_checkpoint,
    find_existing_crawler_source_row,
    get_tracked_source_channel,
)
from memexpert.schemas.content_pipeline import CrawlerIngestOutcome, CrawlerIngestResult, RawCrawlerPost
from memexpert.services.errors import PipelineIngestError, PipelineSourceConflictError

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.config import Settings
    from memexpert.ingest.accept_service import ObjectStorageClient


class CrawlerAcceptService(Protocol):
    async def accept_bytes(
        self,
        *,
        source: IngestAcceptSource,
        filename: str | None,
        content_type: str | None,
        media_bytes: bytes,
    ) -> IngestAcceptResult: ...


class PipelineCrawlerIngestService:
    """Preserve crawler guards while accepting bytes through raw ingest requests."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        accept_service: CrawlerAcceptService | None = None,
    ) -> None:
        self._session = session
        self._accept_service = accept_service or PipelineIngestAcceptService(session)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
    ) -> Self:
        """Build the crawler wrapper with the same API-safe accept service as uploads."""

        return cls(
            session,
            accept_service=PipelineIngestAcceptService.from_settings(
                session,
                settings=settings,
                storage_client=storage_client,
            ),
        )

    async def try_accept_without_media(
        self,
        *,
        platform: SourcePlatform,
        source_id: str,
        post_id: str,
        published_at: datetime | None,
        advance_checkpoint: bool = True,
    ) -> CrawlerIngestResult | None:
        """Return a terminal crawler result that does not require downloading bytes."""

        received_at = utcnow()
        _channel, result = await self._resolve_without_media(
            platform=platform,
            source_id=source_id,
            post_id=post_id,
            published_at=published_at,
            received_at=received_at,
            advance_checkpoint=advance_checkpoint,
        )
        return result

    async def accept_crawler_post(
        self,
        raw_post: RawCrawlerPost,
        *,
        advance_checkpoint: bool = True,
    ) -> CrawlerIngestResult:
        """Accept downloaded crawler bytes into the async raw ingest-request path."""

        received_at = utcnow()
        if raw_post.media_type not in CRAWLER_MEDIA_DEFAULT_FILENAMES:
            return CrawlerIngestResult(
                outcome=CrawlerIngestOutcome.SKIPPED_UNSUPPORTED_MEDIA,
                published_at=raw_post.published_at,
                received_at=received_at,
            )

        channel, replay_result = await self._resolve_without_media(
            platform=raw_post.platform,
            source_id=raw_post.source_id,
            post_id=raw_post.post_id,
            published_at=raw_post.published_at,
            received_at=received_at,
            advance_checkpoint=advance_checkpoint,
        )
        if replay_result is not None:
            return replay_result

        try:
            accept_result = await self._accept_service.accept_bytes(
                source=self._build_accept_source(raw_post),
                filename=raw_post.filename or CRAWLER_MEDIA_DEFAULT_FILENAMES[raw_post.media_type],
                content_type=raw_post.content_type or CRAWLER_MEDIA_DEFAULT_CONTENT_TYPES[raw_post.media_type],
                media_bytes=raw_post.media_bytes,
            )
        except PipelineSourceConflictError:
            # A concurrent accept may have won after the pre-download check.
            _channel, replay_result = await self._resolve_without_media(
                platform=raw_post.platform,
                source_id=raw_post.source_id,
                post_id=raw_post.post_id,
                published_at=raw_post.published_at,
                received_at=received_at,
                advance_checkpoint=advance_checkpoint,
            )
            if replay_result is not None:
                return replay_result
            raise

        return await self._finalize_accept_result(
            channel=channel,
            raw_post=raw_post,
            accept_result=accept_result,
            received_at=received_at,
            advance_checkpoint=advance_checkpoint,
        )

    async def _resolve_without_media(
        self,
        *,
        platform: SourcePlatform,
        source_id: str,
        post_id: str,
        published_at: datetime | None,
        received_at: datetime,
        advance_checkpoint: bool,
    ) -> tuple[SourceChannel, CrawlerIngestResult | None]:
        channel = await get_tracked_source_channel(
            self._session,
            platform=platform,
            source_id=source_id,
        )
        if channel.is_paused:
            return channel, CrawlerIngestResult(
                outcome=CrawlerIngestOutcome.SKIPPED_PAUSED_CHANNEL,
                published_at=published_at,
                received_at=received_at,
            )

        existing_request = await self._find_existing_request(
            platform=platform,
            source_id=source_id,
            post_id=post_id,
        )
        if existing_request is not None:
            existing_source = await find_existing_crawler_source_row(
                self._session,
                platform=platform,
                source_id=source_id,
                post_id=post_id,
            )
            replay_result = self._crawler_result_for_request(
                IngestRequestRead.model_validate(existing_request),
                source_row=existing_source,
                published_at=published_at,
                received_at=received_at,
            )
            if advance_checkpoint:
                await self._advance_checkpoint(channel, post_id=post_id, fetched_at=received_at)
            return channel, replay_result

        existing_source = await find_existing_crawler_source_row(
            self._session,
            platform=platform,
            source_id=source_id,
            post_id=post_id,
        )
        if existing_source is None:
            return channel, None

        replay_result = self._crawler_result_for_source(
            existing_source,
            published_at=published_at,
            received_at=received_at,
        )
        if advance_checkpoint:
            await self._advance_checkpoint(channel, post_id=post_id, fetched_at=received_at)
        return channel, replay_result

    async def _finalize_accept_result(
        self,
        *,
        channel: SourceChannel,
        raw_post: RawCrawlerPost,
        accept_result: IngestAcceptResult,
        received_at: datetime,
        advance_checkpoint: bool,
    ) -> CrawlerIngestResult:
        source_row = await find_existing_crawler_source_row(
            self._session,
            platform=raw_post.platform,
            source_id=raw_post.source_id,
            post_id=raw_post.post_id,
        )
        crawler_result = self._crawler_result_for_accept(
            accept_result,
            source_row=source_row,
            published_at=raw_post.published_at,
            received_at=received_at,
        )
        if advance_checkpoint:
            await self._advance_checkpoint(channel, post_id=raw_post.post_id, fetched_at=received_at)
        return crawler_result

    async def _advance_checkpoint(
        self,
        channel: SourceChannel,
        *,
        post_id: str,
        fetched_at: datetime,
    ) -> None:
        await advance_source_channel_checkpoint(channel, post_id=post_id, fetched_at=fetched_at)
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise PipelineIngestError("Failed to persist crawler checkpoint advancement.") from exc

    async def _find_existing_request(
        self,
        *,
        platform: SourcePlatform,
        source_id: str,
        post_id: str,
    ) -> PipelineIngestRequest | None:
        result = await self._session.execute(
            select(PipelineIngestRequest)
            .where(
                PipelineIngestRequest.source_platform == platform,
                PipelineIngestRequest.source_id == source_id,
                PipelineIngestRequest.post_id == post_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _build_accept_source(raw_post: RawCrawlerPost) -> IngestAcceptSource:
        return IngestAcceptSource(
            source_platform=SourcePlatform.TELEGRAM,
            source_id=raw_post.source_id,
            post_id=raw_post.post_id,
            views=raw_post.views,
            source_metadata=_crawler_source_metadata(raw_post),
        )

    @staticmethod
    def _crawler_result_for_accept(
        accept_result: IngestAcceptResult,
        *,
        source_row: MemeSource | None,
        published_at: datetime | None,
        received_at: datetime,
    ) -> CrawlerIngestResult:
        request = accept_result.ingest_request
        if accept_result.outcome is IngestAcceptOutcome.ACCEPTED_ASYNC:
            return CrawlerIngestResult(
                outcome=CrawlerIngestOutcome.INGESTED,
                sha256_hex=request.sha256_hex,
                published_at=published_at,
                received_at=received_at,
            )
        if accept_result.outcome is IngestAcceptOutcome.RESOLVED_SHA_DUPLICATE:
            return CrawlerIngestResult(
                meme_file_id=request.materialized_meme_file_id,
                meme_source_id=source_row.id if source_row is not None else None,
                outcome=_sha_duplicate_outcome(request.source_attach_reason),
                duplicate_of_meme_id=request.materialized_meme_id,
                matched_meme_file_id=request.matched_meme_file_id,
                source_attach_reason=request.source_attach_reason,
                sha256_hex=request.sha256_hex,
                published_at=published_at,
                received_at=received_at,
            )
        return PipelineCrawlerIngestService._crawler_result_for_request(
            request,
            source_row=source_row,
            published_at=published_at,
            received_at=received_at,
        )

    @staticmethod
    def _crawler_result_for_request(
        request: IngestRequestRead,
        *,
        source_row: MemeSource | None = None,
        published_at: datetime | None,
        received_at: datetime,
    ) -> CrawlerIngestResult:
        return CrawlerIngestResult(
            meme_file_id=request.materialized_meme_file_id,
            meme_source_id=source_row.id if source_row is not None else None,
            outcome=CrawlerIngestOutcome.SKIPPED_DUPLICATE_POST_ID,
            duplicate_of_meme_id=request.materialized_meme_id,
            matched_meme_file_id=request.matched_meme_file_id,
            source_attach_reason=request.source_attach_reason,
            sha256_hex=request.sha256_hex,
            published_at=published_at,
            received_at=received_at,
        )

    @staticmethod
    def _crawler_result_for_source(
        source_row: MemeSource,
        *,
        published_at: datetime | None,
        received_at: datetime,
    ) -> CrawlerIngestResult:
        return CrawlerIngestResult(
            meme_file_id=source_row.file_id,
            meme_source_id=source_row.id,
            outcome=CrawlerIngestOutcome.SKIPPED_DUPLICATE_POST_ID,
            matched_meme_file_id=source_row.matched_meme_file_id,
            source_attach_reason=source_row.attach_reason,
            published_at=published_at,
            received_at=received_at,
        )


def _crawler_source_metadata(raw_post: RawCrawlerPost) -> dict[str, object]:
    metadata: dict[str, object] = {
        "channel_username": raw_post.channel_username,
        "channel_title": raw_post.channel_title,
        "media_type": raw_post.media_type,
        "published_at": raw_post.published_at.isoformat(),
        "reactions": dict(raw_post.reactions),
    }
    if raw_post.forward is not None:
        metadata["forward"] = raw_post.forward.model_dump(mode="json")
    return metadata


def _sha_duplicate_outcome(source_attach_reason: SourceAttachReason | None) -> CrawlerIngestOutcome:
    if source_attach_reason is SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE:
        return CrawlerIngestOutcome.BLOCKED_SHA256_EXISTING_FILE
    return CrawlerIngestOutcome.SHA256_EXACT_EXISTING_FILE


__all__ = ["CrawlerAcceptService", "PipelineCrawlerIngestService"]
