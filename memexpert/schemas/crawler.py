# ruff: noqa: TC001,TC003
"""Typed schemas for the T03 crawler operator surface.

These models power the ``/api/v1/crawler/*`` routes introduced in S04 T03.
They live in their own module (instead of inside
``memexpert.schemas.content_pipeline``) because that file already carries
the S01-S03 ingest + sync + smoke-proof contract and adding the crawler
channel/freshness projections would push it well past its soft-cap size.
Keeping the crawler schemas here makes the operator-surface surface area
scannable without touching the frozen pipeline schema module.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from memexpert.models.enums import SourcePlatform
from memexpert.schemas.content_pipeline import (
    MAX_POST_ID_LENGTH,
    MAX_SOURCE_ID_LENGTH,
    MAX_TELEGRAM_CHANNEL_TITLE_LENGTH,
    MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH,
    MAX_TELEGRAM_SESSION_NAME_LENGTH,
)

# Bounded upper limit on ``CrawlerFreshnessSnapshot.sample_items`` so a
# pathological snapshot with tens of thousands of items does not produce
# an unbounded response body. The reporting layer trims to this cap even
# if the caller asked for a larger ``limit_per_channel`` value.
MAX_FRESHNESS_SAMPLE_ITEMS = 50


class CrawlerChannelRead(BaseModel):
    """Operator-facing projection of one tracked ``SourceChannel`` row.

    Every field is derived from the durable ``source_channels`` table so
    operators see the same state the runtime consumes. Never include
    runtime-only counters here — those belong on ``CrawlerCatchupReport``
    or the freshness snapshot, not on the inspect projection.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    platform: SourcePlatform
    platform_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    username: str | None = Field(
        default=None,
        max_length=MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH,
    )
    title: str = Field(min_length=1, max_length=MAX_TELEGRAM_CHANNEL_TITLE_LENGTH)
    subscriber_count: int | None = None
    is_active: bool
    is_paused: bool
    catchup_enabled: bool
    catchup_message_limit: StrictInt = Field(ge=1)
    session_id: str | None = Field(
        default=None,
        max_length=MAX_TELEGRAM_SESSION_NAME_LENGTH,
    )
    last_read_post_id: str | None = Field(
        default=None,
        max_length=MAX_POST_ID_LENGTH,
    )
    last_fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CrawlerChannelReassignRequest(BaseModel):
    """Request body for the ``POST /channels/{id}/reassign`` route.

    The operator provides the exact session name the channel should bind
    to next. The service layer validates that the target session row
    exists before mutating durable state so unknown sessions surface as a
    loud typed error instead of silently orphaning the channel.
    """

    model_config = ConfigDict(extra="forbid")

    new_session_name: str = Field(min_length=1, max_length=MAX_TELEGRAM_SESSION_NAME_LENGTH)

    @field_validator("new_session_name")
    @classmethod
    def _normalize_session_name(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("new_session_name must not be blank.")
        return normalized_value


class CrawlerChannelReplayPostRequest(BaseModel):
    """Request body for the ``POST /channels/{id}/replay-post`` route."""

    model_config = ConfigDict(extra="forbid")

    post_id: str = Field(min_length=1, max_length=MAX_POST_ID_LENGTH)

    @field_validator("post_id")
    @classmethod
    def _normalize_post_id(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("post_id must not be blank.")
        return normalized_value


class CrawlerFreshnessSampleItem(BaseModel):
    """One row of the bounded per-item sample emitted with the freshness snapshot.

    Each sample row names a concrete meme file so operators can drill into
    the enriched inspect surface from the freshness report. ``freshness_seconds``
    is ``None`` for items whose sync chain has not reached both targets; those
    items still count toward the snapshot's ``item_count`` but do not
    contribute to the percentile computation.
    """

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID
    source_channel_id: uuid.UUID
    published_at: datetime | None = None
    first_ingested_at: datetime | None = None
    both_synced_at: datetime | None = None
    freshness_seconds: float | None = None


class CrawlerFreshnessChannelBreakdown(BaseModel):
    """Per-channel slice of the freshness snapshot.

    Populated by grouping the bounded sample pool by ``source_channel_id``.
    ``most_recent_freshness_seconds`` is the freshness of the single most
    recent item that actually has both sync targets synced — it lets
    operators see the tip of the freshness curve per channel without
    reading every sample row.
    """

    model_config = ConfigDict(extra="forbid")

    source_channel_id: uuid.UUID
    platform_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    channel_title: str = Field(min_length=1, max_length=MAX_TELEGRAM_CHANNEL_TITLE_LENGTH)
    item_count: StrictInt = Field(ge=0)
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    most_recent_item_at: datetime | None = None
    most_recent_freshness_seconds: float | None = None


class CrawlerFreshnessSnapshot(BaseModel):
    """Aggregated freshness snapshot returned by ``GET /crawler/freshness``.

    ``p50_seconds`` / ``p95_seconds`` are derived from the subset of
    sample items that reached both sync targets. When no samples have
    both_synced truth the percentiles are ``None`` and ``slo_*_pass``
    defaults to ``True`` (convention: "no data" must not be a synthetic
    SLO violation — T04's proof harness is the one that actually fails
    a run when there are not enough samples).
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_evaluated_at: datetime
    since: datetime | None = None
    item_count: StrictInt = Field(ge=0)
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    slo_p50_seconds: float = Field(gt=0)
    slo_p95_seconds: float = Field(gt=0)
    slo_p50_pass: bool
    slo_p95_pass: bool
    per_channel: tuple[CrawlerFreshnessChannelBreakdown, ...] = ()
    sample_items: tuple[CrawlerFreshnessSampleItem, ...] = ()


__all__ = [
    "MAX_FRESHNESS_SAMPLE_ITEMS",
    "CrawlerChannelReassignRequest",
    "CrawlerChannelRead",
    "CrawlerChannelReplayPostRequest",
    "CrawlerFreshnessChannelBreakdown",
    "CrawlerFreshnessSampleItem",
    "CrawlerFreshnessSnapshot",
]
