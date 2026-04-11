# ruff: noqa: TC001,TC003
"""Operator-only crawler inspect, session-distribution, and freshness routes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, status

from memexpert.api.dependencies.pipeline import (
    PIPELINE_ERROR_RESPONSES,
    CrawlerOperationsServiceDep,
    require_pipeline_operator_token,
    to_pipeline_http_error,
)
from memexpert.crawlers.telegram.client import PipelineTelegramError
from memexpert.models.enums import SourcePlatform
from memexpert.schemas.content_pipeline import (
    CrawlerIngestResult,
    TelegramSessionStateRead,
)
from memexpert.schemas.crawler import (
    CrawlerChannelRead,
    CrawlerChannelReassignRequest,
    CrawlerChannelReplayPostRequest,
    CrawlerFreshnessSnapshot,
)
from memexpert.services import PipelineServiceError

router = APIRouter(
    prefix="/crawler",
    tags=["crawler"],
    dependencies=[Depends(require_pipeline_operator_token)],
)


@router.get(
    "/sessions",
    response_model=list[TelegramSessionStateRead],
    responses=PIPELINE_ERROR_RESPONSES,
    summary="List Telegram sessions with owned channel counts",
)
async def list_crawler_sessions(
    operations_service: CrawlerOperationsServiceDep,
) -> list[TelegramSessionStateRead]:
    """Return every tracked Telegram session with its owned channel count."""

    try:
        return await operations_service.list_sessions()
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.get(
    "/channels",
    response_model=list[CrawlerChannelRead],
    responses=PIPELINE_ERROR_RESPONSES,
    summary="List tracked source channels with runtime inspect fields",
)
async def list_crawler_channels(
    operations_service: CrawlerOperationsServiceDep,
    platform: Annotated[SourcePlatform | None, Query()] = None,
    session_name: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    include_paused: Annotated[bool, Query()] = True,
) -> list[CrawlerChannelRead]:
    """Return tracked source channels, optionally filtered by platform + session + paused flag."""

    try:
        return await operations_service.list_channels(
            platform=platform,
            session_name=session_name,
            include_paused=include_paused,
        )
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.post(
    "/channels/{source_channel_id}/pause",
    response_model=CrawlerChannelRead,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_200_OK,
    summary="Pause one curated source channel (idempotent)",
)
async def pause_crawler_channel(
    source_channel_id: Annotated[uuid.UUID, Path()],
    operations_service: CrawlerOperationsServiceDep,
) -> CrawlerChannelRead:
    """Set ``is_paused=True`` on the matching channel row; idempotent."""

    try:
        return await operations_service.pause_channel(source_channel_id)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.post(
    "/channels/{source_channel_id}/resume",
    response_model=CrawlerChannelRead,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_200_OK,
    summary="Resume one curated source channel (idempotent)",
)
async def resume_crawler_channel(
    source_channel_id: Annotated[uuid.UUID, Path()],
    operations_service: CrawlerOperationsServiceDep,
) -> CrawlerChannelRead:
    """Set ``is_paused=False`` on the matching channel row; idempotent."""

    try:
        return await operations_service.resume_channel(source_channel_id)
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.post(
    "/channels/{source_channel_id}/reassign",
    response_model=CrawlerChannelRead,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_200_OK,
    summary="Bind a channel to a different Telegram session",
)
async def reassign_crawler_channel(
    source_channel_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[CrawlerChannelReassignRequest, Body()],
    operations_service: CrawlerOperationsServiceDep,
) -> CrawlerChannelRead:
    """Reassign one channel to ``payload.new_session_name``.

    Unknown target sessions surface as HTTP 409 with the
    ``crawler_invalid_session`` code so operators see a distinct error
    from the broader ``crawler_session_not_runnable`` taxonomy (which
    fires when a known session is parked in flood-wait or quarantine).
    The live listener is NOT rebound on the fly — the caller restarts
    the session's listener to pick up the new assignment.
    """

    try:
        return await operations_service.reassign_channel(
            source_channel_id,
            new_session_name=payload.new_session_name,
        )
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.post(
    "/channels/{source_channel_id}/replay-post",
    response_model=CrawlerIngestResult,
    responses=PIPELINE_ERROR_RESPONSES,
    status_code=status.HTTP_200_OK,
    summary="Replay one Telegram post without advancing the channel checkpoint",
)
async def replay_crawler_channel_post(
    source_channel_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[CrawlerChannelReplayPostRequest, Body()],
    operations_service: CrawlerOperationsServiceDep,
) -> CrawlerIngestResult:
    """Re-ingest one post by id.

    Unknown post ids surface as HTTP 422 with the
    ``telegram_malformed_message`` code because the Telethon adapter
    raises :class:`PipelineTelegramMalformedMessageError` for "no such
    message" — the runtime already treats that error class as
    non-retryable and the operator surface mirrors the same taxonomy.
    The channel checkpoint is intentionally NOT advanced by a replay.
    """

    try:
        return await operations_service.replay_channel_post(
            source_channel_id,
            post_id=payload.post_id,
        )
    except PipelineTelegramError as exc:
        raise to_pipeline_http_error(exc) from exc
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


@router.get(
    "/freshness",
    response_model=CrawlerFreshnessSnapshot,
    responses=PIPELINE_ERROR_RESPONSES,
    summary="Read the bounded freshness snapshot aggregated from durable pipeline truth",
)
async def read_crawler_freshness_snapshot(
    operations_service: CrawlerOperationsServiceDep,
    since: Annotated[datetime | None, Query()] = None,
    limit_per_channel: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> CrawlerFreshnessSnapshot:
    """Return the freshness snapshot used by operators to chase SLO drift.

    The SLO p50/p95 pass flags follow the convention "no data means
    pass" — an empty snapshot cannot manufacture a failure on its own.
    T04's freshness proof harness is the one that actually fails a run
    when there are not enough samples.
    """

    try:
        return await operations_service.get_crawler_freshness_snapshot(
            since=since,
            limit_per_channel=limit_per_channel,
        )
    except PipelineServiceError as exc:
        raise to_pipeline_http_error(exc) from exc


__all__ = ["router"]
