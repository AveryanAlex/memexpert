# ruff: noqa: TC001,TC003
"""Typed crawler ingest request/response schemas and operator upload metadata.

S01 introduced the operator upload metadata that rides alongside a manual
file upload. S04 added the crawler entrypoint: a second ``create_ingest``
call that consumes the Telegram crawler feed. Both contracts live here so
callers can import the S01/S04 ingest surface without pulling in the full
content-pipeline read-model surface. ``memexpert.schemas.content_pipeline``
re-exports every public name below for backward compatibility.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from memexpert.models.enums import SourcePlatform
from memexpert.schemas.pipeline_base import (
    MAX_POST_ID_LENGTH,
    MAX_SOURCE_ID_LENGTH,
    MAX_TELEGRAM_CHANNEL_TITLE_LENGTH,
    MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH,
    MAX_TELEGRAM_CONTENT_TYPE_LENGTH,
    MAX_TELEGRAM_FILENAME_LENGTH,
)


class ContentPipelineUploadMetadata(BaseModel):
    """Operator-supplied provenance metadata accepted alongside an uploaded file."""

    model_config = ConfigDict(extra="forbid")

    source_platform: SourcePlatform
    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    post_id: str = Field(min_length=1, max_length=MAX_POST_ID_LENGTH)
    owner_user_id: uuid.UUID | None = None
    views: StrictInt = Field(default=0, ge=0)

    @field_validator("source_id", "post_id")
    @classmethod
    def _normalize_required_source_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("source provenance fields must not be blank.")
        return normalized_value


# ---------------------------------------------------------------------------
# Crawler ingest contract
# ---------------------------------------------------------------------------
#
# S04 introduces a second ingest entrypoint that consumes the Telegram crawler
# feed. The schemas below lock the typed shape the ingest service accepts and
# the typed outcome it returns. ``SourcePlatform`` is reused directly instead
# of introducing a separate ``CrawlerSourcePlatform`` enum: the crawler-valid
# set (TELEGRAM today) is a strict subset of ``SourcePlatform`` and inventing
# a second enum would force every caller to map between them with no added
# type safety. A per-field validator below rejects non-Telegram platforms
# inside ``RawCrawlerPost`` so the crawler contract stays narrow even though
# it shares the enum type.
CrawlerSourcePlatform = SourcePlatform
"""Alias kept for docs/readability: crawler-valid platforms are a subset of SourcePlatform."""

CRAWLER_MEDIA_TYPE_VALUES: frozenset[Literal["photo", "gif", "video"]] = frozenset(
    {"photo", "gif", "video"},
)

# Conservative upper bound on the in-memory payload ``RawCrawlerPost`` may
# carry. The service re-enforces the per-media-type limits from
# ``Settings.pipeline_*_upload_max_bytes`` when it sees the actual media
# kind, but this schema-level guard prevents an obviously oversized blob
# from reaching the service at all.
_RAW_CRAWLER_POST_MEDIA_BYTES_HARD_LIMIT = 256 * 1024 * 1024


class CrawlerForwardAttribution(BaseModel):
    """Original-author attribution captured from a forwarded Telegram message.

    When a curated channel reposts a message from another channel, the
    ``MemeSource.source_id``/``post_id`` pair still points at the channel
    where we SAW the post. These fields preserve the original author pair
    so the product surface can render "originally posted by X" without
    losing the provenance of where the crawler observed the repost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    post_id: str = Field(min_length=1, max_length=MAX_POST_ID_LENGTH)
    channel_username: str | None = Field(
        default=None,
        max_length=MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH,
    )
    channel_title: str | None = Field(
        default=None,
        max_length=MAX_TELEGRAM_CHANNEL_TITLE_LENGTH,
    )

    @field_validator("source_id", "post_id")
    @classmethod
    def _normalize_required_forward_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("forward attribution identifiers must not be blank.")
        return normalized_value


class RawCrawlerPost(BaseModel):
    """Typed raw Telegram post fed into the content pipeline crawler entrypoint.

    The service layer consumes this struct verbatim inside
    ``ContentPipelineService.create_crawler_ingest``. Every field is required
    by the ingest contract unless explicitly marked optional here.
    ``media_bytes`` carries the already-downloaded media payload so the
    service does not need to own the Telethon client; T02's real adapter
    materializes it before calling the service.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: CrawlerSourcePlatform
    source_id: str = Field(min_length=1, max_length=MAX_SOURCE_ID_LENGTH)
    post_id: str = Field(min_length=1, max_length=MAX_POST_ID_LENGTH)
    published_at: datetime
    channel_username: str | None = Field(
        default=None,
        max_length=MAX_TELEGRAM_CHANNEL_USERNAME_LENGTH,
    )
    channel_title: str | None = Field(
        default=None,
        max_length=MAX_TELEGRAM_CHANNEL_TITLE_LENGTH,
    )
    media_type: Literal["photo", "gif", "video"]
    media_bytes: bytes
    filename: str | None = Field(default=None, max_length=MAX_TELEGRAM_FILENAME_LENGTH)
    content_type: str | None = Field(default=None, max_length=MAX_TELEGRAM_CONTENT_TYPE_LENGTH)
    views: StrictInt = Field(default=0, ge=0)
    reactions: dict[str, int] = Field(default_factory=dict)
    forward: CrawlerForwardAttribution | None = None

    @field_validator("platform")
    @classmethod
    def _validate_crawler_platform(cls, value: SourcePlatform) -> SourcePlatform:
        if value is not SourcePlatform.TELEGRAM:
            raise ValueError(
                "RawCrawlerPost currently only accepts the TELEGRAM platform; "
                f"got {value.value!r}.",
            )
        return value

    @field_validator("source_id", "post_id")
    @classmethod
    def _normalize_required_crawler_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("crawler provenance fields must not be blank.")
        return normalized_value

    @field_validator("media_bytes")
    @classmethod
    def _validate_media_bytes_size(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError("RawCrawlerPost.media_bytes must not be empty.")
        if len(value) > _RAW_CRAWLER_POST_MEDIA_BYTES_HARD_LIMIT:
            raise ValueError(
                "RawCrawlerPost.media_bytes exceeds the hard upper bound "
                f"({_RAW_CRAWLER_POST_MEDIA_BYTES_HARD_LIMIT} bytes).",
            )
        return value


class CrawlerIngestOutcome(StrEnum):
    """Terminal outcomes returned by ``ContentPipelineService.create_crawler_ingest``.

    The service caller uses these to decide whether the crawler should bump
    its checkpoint, log a no-op, or abort. Both ``SKIPPED_DUPLICATE_POST_ID``
    and ``REJECTED_MALFORMED`` exist so T02's real Telethon adapter has
    distinct terminal states for a re-delivered message and a message that
    failed typed validation at the adapter boundary.
    """

    INGESTED = "ingested"
    DEDUPLICATED_EXACT = "deduplicated_exact"
    DEDUPLICATED_SIMILAR = "deduplicated_similar"
    BLOCKED_PERCEPTUAL_HASH = "blocked_perceptual_hash"
    SKIPPED_UNSUPPORTED_MEDIA = "skipped_unsupported_media"
    SKIPPED_PAUSED_CHANNEL = "skipped_paused_channel"
    SKIPPED_DUPLICATE_POST_ID = "skipped_duplicate_post_id"
    REJECTED_MALFORMED = "rejected_malformed"


class CrawlerIngestResult(BaseModel):
    """Typed result returned by the crawler ingest entrypoint.

    ``meme_file_id`` and ``meme_source_id`` are ``None`` only for
    early-exit outcomes that do not touch the DB
    (``SKIPPED_UNSUPPORTED_MEDIA``). ``duplicate_of_meme_id`` is populated
    for the two ``DEDUPLICATED_*`` outcomes so operators can see which
    existing meme absorbed the repost without a second lookup.
    ``published_at`` mirrors :class:`RawCrawlerPost.published_at` when the
    post was actually consumed, and ``received_at`` is the service-side
    clock for the ingest attempt (used by T04's freshness SLO harness).
    """

    model_config = ConfigDict(extra="forbid")

    meme_file_id: uuid.UUID | None = None
    meme_source_id: uuid.UUID | None = None
    outcome: CrawlerIngestOutcome
    duplicate_of_meme_id: uuid.UUID | None = None
    published_at: datetime | None = None
    received_at: datetime


__all__ = [
    "CRAWLER_MEDIA_TYPE_VALUES",
    "ContentPipelineUploadMetadata",
    "CrawlerForwardAttribution",
    "CrawlerIngestOutcome",
    "CrawlerIngestResult",
    "CrawlerSourcePlatform",
    "RawCrawlerPost",
]
