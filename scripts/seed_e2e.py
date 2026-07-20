#!/usr/bin/env python3
"""Seed and prove the deterministic containerized PRD E2E corpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

import httpx
from botocore.exceptions import ClientError
from PIL import Image, PngImagePlugin
from pydantic import BaseModel, ValidationError
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Distance, VectorParams
from sqlalchemy import delete, func, or_, select, update

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_engine, get_async_session_factory
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantSyncClient
from memexpert.core.storage import derive_preview_image_object_key, get_pipeline_storage_settings, get_s3_client
from memexpert.core.voyage import build_pipeline_voyage_client
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService
from memexpert.ingest.schemas import IngestRequestRead
from memexpert.media.contracts import WEB_VIDEO_PROFILE_ID
from memexpert.messaging.rabbitmq_outbox_runtime import (
    RabbitMQOutboxPublisherBatchResult,
    run_rabbitmq_outbox_publisher_batch,
)
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionInvite, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeSeoPage,
    MemeSource,
    MemeSourceEngagementSnapshot,
    MemeTemplate,
    PipelineStageJournal,
    SourceChannel,
)
from memexpert.models.enums import (
    AccountStatus,
    AnalyticsEventType,
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    EmbeddingInputType,
    IngestSourceKind,
    MediaGenerationStatus,
    MemeVisibilityMode,
    PipelineIngestRequestStatus,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.operations import MediaGeneration
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.content_pipeline import (
    ContentPipelineErrorResponse,
    ContentPipelineItemDetail,
    CrawlerIngestOutcome,
    CrawlerIngestResult,
    RawCrawlerPost,
)
from memexpert.services.public_trends import refresh_public_trend_materialized_views
from memexpert.services.search_index_sync import (
    build_meilisearch_document,
    build_qdrant_sync_payload,
    load_search_index_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.core.meilisearch import MeilisearchSyncClientProtocol
    from memexpert.core.qdrant import QdrantSyncClientProtocol

DEFAULT_API_BASE_URL: Final = "http://api:8000"
DEFAULT_ARTIFACTS_DIR: Final = Path("/artifacts")
DEFAULT_TIMEOUT_SECONDS: Final = 180.0
DEFAULT_API_TIMEOUT_SECONDS: Final = 20.0
POLL_INTERVAL_SECONDS: Final = 1.0
HTTP_RETRY_INITIAL_BACKOFF_SECONDS: Final = 0.25
HTTP_RETRY_MAX_BACKOFF_SECONDS: Final = 5.0
HTTP_RETRY_MIN_DELAY_SECONDS: Final = 0.05
TRANSIENT_HTTP_STATUS_CODES: Final = frozenset({408, 425, 429, *range(500, 600)})
IDEMPOTENT_HTTP_METHODS: Final = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PUT"})
E2E_SOURCE_ID: Final = "e2e-prd-seed"
E2E_UPLOAD_SOURCE_ID: Final = "e2e-prd-upload"
E2E_PROMOTION_SOURCE_ID: Final = "e2e-prd-public-crawler"
E2E_UPLOAD_USER_EMAIL: Final = "private-upload.e2e@memexpert.test"
E2E_MODEL_ID: Final = "e2e-prd-seed"
E2E_PROMPT_VERSION: Final = "e2e-prd-v1"
E2E_ACCOUNT_PASSWORD: Final = "memexpert-e2e-password"
E2E_ACCOUNT_PASSWORD_HASH: Final = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.TaORhf.fwRuC9z.8O1TWVf5KG9lIYBS"
E2E_OWNER_EMAIL: Final = "collection.owner.e2e@memexpert.test"
E2E_MEMBER_EMAIL: Final = "collection.member.e2e@memexpert.test"
E2E_COLLECTION_TITLE: Final = "Launch E2E Collection"
E2E_COLLECTION_DESCRIPTION: Final = "Private launch fixture for collection-management E2E."
E2E_COLLECTION_INVITE_TOKEN: Final = "memexpert-e2e-collection-invite-token"
E2E_PUBLIC_TRENDS_TAG_SLUG: Final = "e2e-prd-trends"
E2E_PUBLIC_TRENDS_TEMPLATE_SLUG: Final = "e2e-prd-template"
E2E_PUBLIC_TRENDS_TEMPLATE_NAME: Final = "E2E PRD Template"
E2E_PUBLIC_TRENDS_TEMPLATE_DESCRIPTION: Final = "Deterministic template fixture for public trends E2E."
E2E_PUBLIC_TRENDS_TIMELINE_GRANULARITY: Final = "month"
E2E_PUBLIC_TRENDS_TIMELINE_PERIOD: Final = "2026-01"
E2E_PUBLIC_TRENDS_MEME_CATEGORIES: Final = ("cat", "dog", "frog")
UUID_NAMESPACE: Final = uuid.UUID("176f5e31-6e5d-5e43-80aa-1f7aa3aa0d4b")
TERMINAL_INGEST_FAILURE_STATUSES: Final = frozenset(
    {
        PipelineIngestRequestStatus.FAILED_BLOCKED_PHASH,
        PipelineIngestRequestStatus.FAILED_INVALID_MEDIA,
        PipelineIngestRequestStatus.PUBLISH_FAILED,
    }
)


class E2ESeedError(RuntimeError):
    """Raised when the seed/proof flow cannot complete truthfully."""


@dataclass(slots=True)
class MonotonicDeadline:
    """One caller-owned retry/poll deadline with an injectable clock and sleeper."""

    expires_at: float
    _monotonic: Callable[[], float]
    _sleep: Callable[[float], None]

    @classmethod
    def after(
        cls,
        timeout_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> MonotonicDeadline:
        return cls(
            expires_at=monotonic() + max(0.0, timeout_seconds),
            _monotonic=monotonic,
            _sleep=sleep,
        )

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self._monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def sleep_for(self, seconds: float) -> None:
        duration = min(max(0.0, seconds), self.remaining())
        if duration > 0:
            self._sleep(duration)


def _retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except TypeError, ValueError, OverflowError:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or datetime.now(tz=UTC)
        seconds = (retry_at - current).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _http_retry_delay(*, attempts: int, retry_after: str | None = None) -> float:
    requested_delay = _retry_after_seconds(retry_after)
    exponential_delay = HTTP_RETRY_INITIAL_BACKOFF_SECONDS * (2 ** max(0, attempts - 1))
    delay = requested_delay if requested_delay is not None else exponential_delay
    return min(HTTP_RETRY_MAX_BACKOFF_SECONDS, max(HTTP_RETRY_MIN_DELAY_SECONDS, delay))


@dataclass(frozen=True, slots=True)
class SeedSpec:
    category: str
    color: tuple[int, int, int]
    slug: str
    ocr_text: str
    caption: str
    alt_text: str
    query: str
    tags: tuple[str, ...]
    is_nsfw: bool = False
    language: ContentLanguage = ContentLanguage.EN
    media_type: ContentKind = ContentKind.IMAGE


@dataclass(frozen=True, slots=True)
class PublicTrendSnapshotSpec:
    captured_at: datetime
    source_views: int
    source_reactions: int
    source_reposts: int
    platform_views: int
    platform_sends: int
    platform_saves: int
    platform_likes: int


@dataclass(frozen=True, slots=True)
class SeededMeme:
    category: str
    meme_id: uuid.UUID
    meme_file_id: uuid.UUID
    slug: str
    query: str
    object_key: str
    title: str
    tags: tuple[str, ...]
    is_nsfw: bool
    language: ContentLanguage
    media_type: ContentKind


@dataclass(frozen=True, slots=True)
class SeededE2EUser:
    label: str
    user_id: uuid.UUID
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class PrivateUploadFixture:
    user_id: uuid.UUID
    collection_id: uuid.UUID
    crawler_channel_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class SeededCollectionManagementFixture:
    owner: SeededE2EUser
    member: SeededE2EUser
    collection_id: uuid.UUID
    title: str
    description: str
    visibility: CollectionVisibility
    saved_memes: tuple[SeededMeme, ...]
    pinned_memes: tuple[SeededMeme, ...]
    invite_id: uuid.UUID
    invite_token: str

    @property
    def invite_path(self) -> str:
        return f"/collection/invite/{self.invite_token}"


PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY: Final[dict[str, tuple[PublicTrendSnapshotSpec, ...]]] = {
    "cat": (
        PublicTrendSnapshotSpec(
            captured_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
            source_views=120,
            source_reactions=12,
            source_reposts=4,
            platform_views=40,
            platform_sends=3,
            platform_saves=5,
            platform_likes=7,
        ),
        PublicTrendSnapshotSpec(
            captured_at=datetime(2026, 1, 12, 12, 0, tzinfo=UTC),
            source_views=180,
            source_reactions=18,
            source_reposts=6,
            platform_views=60,
            platform_sends=5,
            platform_saves=8,
            platform_likes=12,
        ),
    ),
    "dog": (
        PublicTrendSnapshotSpec(
            captured_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
            source_views=90,
            source_reactions=9,
            source_reposts=3,
            platform_views=35,
            platform_sends=2,
            platform_saves=4,
            platform_likes=6,
        ),
        PublicTrendSnapshotSpec(
            captured_at=datetime(2026, 1, 12, 12, 0, tzinfo=UTC),
            source_views=130,
            source_reactions=13,
            source_reposts=4,
            platform_views=50,
            platform_sends=4,
            platform_saves=6,
            platform_likes=9,
        ),
    ),
    "frog": (
        PublicTrendSnapshotSpec(
            captured_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
            source_views=70,
            source_reactions=7,
            source_reposts=2,
            platform_views=30,
            platform_sends=1,
            platform_saves=3,
            platform_likes=5,
        ),
        PublicTrendSnapshotSpec(
            captured_at=datetime(2026, 1, 12, 12, 0, tzinfo=UTC),
            source_views=110,
            source_reactions=11,
            source_reposts=3,
            platform_views=45,
            platform_sends=3,
            platform_saves=5,
            platform_likes=8,
        ),
    ),
}


class PipelineApiClient:
    """Typed HTTP client wrapper for the operator and public proof routes."""

    def __init__(
        self,
        *,
        base_url: str,
        operator_token: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._request_timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Memexpert-Operator-Token": operator_token},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PipelineApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        deadline: MonotonicDeadline,
        retry_safe: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue one request with transient retries bounded by the shared deadline."""

        normalized_method = method.upper()
        may_retry = normalized_method in IDEMPOTENT_HTTP_METHODS or retry_safe
        attempts = 0
        last_failure = "request was not attempted"

        while True:
            remaining = deadline.remaining()
            if remaining <= 0:
                raise E2ESeedError(
                    f"{normalized_method} {path} exhausted the overall HTTP deadline after {attempts} attempt(s); "
                    f"last transient failure: {last_failure}",
                )

            attempts += 1
            try:
                response = self._client.request(
                    normalized_method,
                    path,
                    timeout=min(self._request_timeout_seconds, remaining),
                    **kwargs,
                )
            except httpx.TransportError as exc:
                if not may_retry:
                    raise E2ESeedError(
                        f"{normalized_method} {path} failed with ambiguous transport error and is not retry-safe; "
                        f"the write was not retried: {type(exc).__name__}: {exc}",
                    ) from exc
                last_failure = f"{type(exc).__name__}: {exc}"
                self._sleep_before_http_retry(
                    method=normalized_method,
                    path=path,
                    attempts=attempts,
                    deadline=deadline,
                    delay_seconds=_http_retry_delay(attempts=attempts),
                    failure=last_failure,
                )
                continue

            if response.status_code not in TRANSIENT_HTTP_STATUS_CODES or not may_retry:
                return response

            last_failure = f"HTTP {response.status_code}: {response.text[:500]!r}"
            self._sleep_before_http_retry(
                method=normalized_method,
                path=path,
                attempts=attempts,
                deadline=deadline,
                delay_seconds=_http_retry_delay(
                    attempts=attempts,
                    retry_after=response.headers.get("Retry-After"),
                ),
                failure=last_failure,
            )

    def _sleep_before_http_retry(
        self,
        *,
        method: str,
        path: str,
        attempts: int,
        deadline: MonotonicDeadline,
        delay_seconds: float,
        failure: str,
    ) -> None:
        delay = min(delay_seconds, deadline.remaining())
        print(
            f"Transient {method} {path} failure on attempt {attempts}: {failure}; retrying in {delay:.2f}s.",
            file=sys.stderr,
            flush=True,
        )
        deadline.sleep_for(delay_seconds)

    def healthcheck(self, *, deadline: MonotonicDeadline) -> None:
        response = self._request("GET", "/health", deadline=deadline)
        if response.status_code != 200:
            raise E2ESeedError(
                f"GET /health returned unexpected status {response.status_code}: {response.text!r}",
            )

    def upload_cat_png(
        self,
        *,
        image_bytes: bytes,
        run_id: str,
        uploader_user_id: uuid.UUID,
        target_collection_id: uuid.UUID,
        deadline: MonotonicDeadline,
    ) -> IngestRequestRead:
        return self.upload_media(
            media_bytes=image_bytes,
            filename="e2e-prd-cat.png",
            content_type="image/png",
            post_id=run_id,
            uploader_user_id=uploader_user_id,
            target_collection_id=target_collection_id,
            deadline=deadline,
        )

    def upload_media(
        self,
        *,
        media_bytes: bytes,
        filename: str,
        content_type: str,
        post_id: str,
        uploader_user_id: uuid.UUID,
        target_collection_id: uuid.UUID,
        deadline: MonotonicDeadline,
    ) -> IngestRequestRead:
        # The API persists and uniquely constrains (source_platform, source_id, post_id),
        # and returns that existing request on replay. That durable identity makes a
        # response-lost retry safe; arbitrary POST requests remain non-retryable.
        response = self._request(
            "POST",
            "/api/v1/pipeline/uploads",
            deadline=deadline,
            retry_safe=True,
            data={
                "source_platform": SourcePlatform.TELEGRAM.value,
                "source_id": E2E_UPLOAD_SOURCE_ID,
                "post_id": post_id,
                "uploader_user_id": str(uploader_user_id),
                "target_collection_id": str(target_collection_id),
                "view_count": "1",
            },
            files={"file": (filename, media_bytes, content_type)},
        )
        return _validate_response(response, expected_status=(200, 202), model=IngestRequestRead)

    def get_ingest_request(
        self,
        ingest_request_id: uuid.UUID,
        *,
        deadline: MonotonicDeadline,
    ) -> IngestRequestRead:
        response = self._request(
            "GET",
            f"/api/v1/pipeline/ingest-requests/{ingest_request_id}",
            deadline=deadline,
        )
        return _validate_response(response, expected_status=200, model=IngestRequestRead)

    def get_item_detail(
        self,
        meme_file_id: uuid.UUID,
        *,
        deadline: MonotonicDeadline,
    ) -> ContentPipelineItemDetail:
        response = self._request(
            "GET",
            f"/api/v1/pipeline/items/{meme_file_id}/detail",
            deadline=deadline,
        )
        return _validate_response(response, expected_status=200, model=ContentPipelineItemDetail)

    def public_search(
        self,
        query: str,
        *,
        deadline: MonotonicDeadline,
        include_nsfw: bool = False,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/memes/search",
            deadline=deadline,
            params={"query": query, "include_nsfw": str(include_nsfw).lower(), "limit": "10", "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)

    def public_detail_by_slug(
        self,
        slug: str,
        *,
        deadline: MonotonicDeadline,
        include_nsfw: bool = False,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/memes/slug/{slug}",
            deadline=deadline,
            params={"include_nsfw": str(include_nsfw).lower()},
        )
        return _validate_json_response(response, expected_status=200)

    def public_detail_by_slug_status(
        self,
        slug: str,
        *,
        deadline: MonotonicDeadline,
        include_nsfw: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        response = self._request(
            "GET",
            f"/api/v1/memes/slug/{slug}",
            deadline=deadline,
            params={"include_nsfw": str(include_nsfw).lower()},
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise E2ESeedError(
                f"GET /api/v1/memes/slug/{slug} returned non-JSON output: {exc}; body={response.text!r}",
            ) from exc
        if not isinstance(payload, dict):
            raise E2ESeedError(
                f"GET /api/v1/memes/slug/{slug} returned non-object JSON: {type(payload).__name__}",
            )
        return response.status_code, payload

    def public_trend_page(self, *, deadline: MonotonicDeadline, limit: int = 20) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/memes/trends",
            deadline=deadline,
            params={"limit": str(limit), "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)

    def public_tag_trend_summaries(
        self,
        *,
        deadline: MonotonicDeadline,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "/api/v1/memes/trends/tags",
            deadline=deadline,
            params={"limit": str(limit), "offset": "0"},
        )
        return _validate_json_list_response(response, expected_status=200)

    def public_template_trend_summaries(
        self,
        *,
        deadline: MonotonicDeadline,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "/api/v1/memes/trends/templates",
            deadline=deadline,
            params={"limit": str(limit), "offset": "0"},
        )
        return _validate_json_list_response(response, expected_status=200)

    def public_trend_comparison(
        self,
        items: list[str],
        *,
        deadline: MonotonicDeadline,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/memes/trends/compare",
            deadline=deadline,
            params=[("item", item) for item in items],
        )
        return _validate_json_response(response, expected_status=200)

    def public_trend_timeline(
        self,
        *,
        deadline: MonotonicDeadline,
        granularity: str = "month",
        limit: int = 12,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/memes/trends/timeline",
            deadline=deadline,
            params={"granularity": granularity, "limit": str(limit), "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)

    def public_tag_landing(self, tag_slug: str, *, deadline: MonotonicDeadline) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/memes/tags/{tag_slug}",
            deadline=deadline,
            params={"limit": "20", "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)

    def public_template_landing(self, template_slug: str, *, deadline: MonotonicDeadline) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/memes/templates/{template_slug}",
            deadline=deadline,
            params={"limit": "20", "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=DEFAULT_API_BASE_URL,
        help="Base URL of the running API container (default: %(default)s).",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR,
        help="Directory where seed.json will be written (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-phase bounded wait/retry budget for API and eventual-consistency proofs (default: %(default)s).",
    )
    parser.add_argument(
        "--api-timeout-seconds",
        type=float,
        default=DEFAULT_API_TIMEOUT_SECONDS,
        help="Per-request HTTP timeout (default: %(default)s).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional deterministic upload post id (defaults to uuid7 hex).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(_run(args))
    except E2ESeedError as exc:
        print(f"PRD E2E seed failed: {exc}", file=sys.stderr)
        return 2
    return 0


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    validate_provider_policy(settings)
    run_id = args.run_id or uuid.uuid7().hex
    started_at = datetime.now(tz=UTC)
    artifacts_dir = args.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    print("Ensuring S3 bucket, Qdrant collection, and Meilisearch index")
    s3_client = get_s3_client()
    await ensure_bucket(settings=settings, s3_client=s3_client)
    await ensure_qdrant_collection(settings=settings)
    meili_client = PipelineMeilisearchSyncClient(settings=settings)
    await meili_client.ensure_index()

    qdrant_sync_client = PipelineQdrantSyncClient(settings=settings)
    session_factory = get_async_session_factory()
    specs = build_seed_specs()
    await cleanup_e2e_rows(settings=settings, specs=specs)
    private_upload_fixture = await seed_private_upload_fixture()

    cat_png = build_png_bytes((255, 0, 0))
    operator_token = settings.pipeline_operator_token.get_secret_value()

    def phase_deadline() -> MonotonicDeadline:
        return MonotonicDeadline.after(args.timeout_seconds)

    with PipelineApiClient(
        base_url=args.api_base_url,
        operator_token=operator_token,
        timeout_seconds=args.api_timeout_seconds,
    ) as api_client:
        api_client.healthcheck(deadline=phase_deadline())
        print("Uploading generated cat PNG through /api/v1/pipeline/uploads")
        ingest_request = api_client.upload_cat_png(
            image_bytes=cat_png,
            run_id=run_id,
            uploader_user_id=private_upload_fixture.user_id,
            target_collection_id=private_upload_fixture.collection_id,
            deadline=phase_deadline(),
        )
        materialized_request, meme_file_id = await wait_for_ingest_materialized_meme_file(
            api_client,
            ingest_request_id=ingest_request.id,
            settings=settings,
            session_factory=session_factory,
            deadline=phase_deadline(),
        )
        detail = await wait_for_dual_synced(
            api_client,
            meme_file_id=meme_file_id,
            settings=settings,
            session_factory=session_factory,
            deadline=phase_deadline(),
        )
        print(f"Uploaded item dual-synced: ingest_request_id={ingest_request.id} meme_file_id={meme_file_id}")
        print("Generating audible and silent moving-media fixtures with FFmpeg")
        moving_media_fixtures = await asyncio.to_thread(_build_audio_profile_media_fixtures, settings)
        audio_profile_proofs: list[dict[str, object]] = []
        for fixture_name, fixture_bytes, expected_audio, expected_frame_rate in moving_media_fixtures:
            print(f"Uploading {fixture_name} through /api/v1/pipeline/uploads")
            media_ingest = api_client.upload_media(
                media_bytes=fixture_bytes,
                filename=f"e2e-prd-{fixture_name}.webm",
                content_type="video/webm",
                post_id=f"{run_id}-{fixture_name}",
                uploader_user_id=private_upload_fixture.user_id,
                target_collection_id=private_upload_fixture.collection_id,
                deadline=phase_deadline(),
            )
            _materialized_media, moving_media_file_id = await wait_for_ingest_materialized_meme_file(
                api_client,
                ingest_request_id=media_ingest.id,
                settings=settings,
                session_factory=session_factory,
                deadline=phase_deadline(),
            )
            await wait_for_dual_synced(
                api_client,
                meme_file_id=moving_media_file_id,
                settings=settings,
                session_factory=session_factory,
                deadline=phase_deadline(),
            )
            audio_profile_proofs.append(
                await _prove_audio_safe_derivative(
                    settings=settings,
                    session_factory=session_factory,
                    s3_client=s3_client,
                    meme_file_id=moving_media_file_id,
                    fixture_name=fixture_name,
                    expected_audio=expected_audio,
                    expected_frame_rate=expected_frame_rate,
                )
            )
        await assert_private_upload_state(
            meme_id=detail.meme_id,
            meme_file_id=meme_file_id,
            fixture=private_upload_fixture,
        )
        private_search_payload = api_client.public_search("cat", deadline=phase_deadline())
        assert_public_search_excludes(
            private_search_payload,
            meme_id=detail.meme_id,
            label="private upload before crawler promotion",
        )
        slug = await prepare_created_meme_metadata(
            meme_id=detail.meme_id,
            query="cat",
        )
        promotion_result = await promote_private_upload_with_crawler(
            settings=settings,
            image_bytes=cat_png,
            run_id=run_id,
            expected_meme_id=detail.meme_id,
            expected_meme_file_id=meme_file_id,
        )
        await resync_created_public_meme_indexes(
            settings=settings,
            meme_file_id=meme_file_id,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
        )
        created_search_payload = wait_for_public_search_contains(
            api_client,
            query="cat",
            meme_id=detail.meme_id,
            deadline=phase_deadline(),
        )
        created_detail_payload = assert_public_detail_resolves(
            api_client,
            slug=slug,
            meme_id=detail.meme_id,
            deadline=phase_deadline(),
        )

        seeded = await seed_direct_corpus(
            settings=settings,
            s3_client=s3_client,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
            specs=specs,
        )
        await wait_for_meili_hits(meili_client, specs=specs, deadline=phase_deadline())
        print(f"Seeded deterministic public corpus: {', '.join(item.category for item in seeded)}")
        print("Refreshing public trend materialized views")
        await refresh_public_trend_materialized_views(get_async_engine(), concurrently=True)
        public_trends_artifact = build_public_trends_artifact(seeded)

        collection_fixture = await seed_collection_management_fixture(
            settings=settings,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
            seeded=seeded,
        )
        print(
            "Seeded deterministic private collection fixture: "
            f"collection_id={collection_fixture.collection_id} owner={collection_fixture.owner.email}",
        )

        assert_created_is_distinct(created_meme_id=detail.meme_id, seeded=seeded)
        created_search_payload = api_client.public_search("cat", deadline=phase_deadline())
        assert_public_search_contains(created_search_payload, meme_id=detail.meme_id)
        seeded_proofs = prove_seeded_public_corpus(
            api_client,
            seeded=seeded,
            deadline=phase_deadline(),
        )
        public_trends_proof = prove_seeded_public_trends(
            api_client,
            seeded=seeded,
            deadline=phase_deadline(),
        )

    artifact_payload = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "provider_policy": {
            "ocr": settings.pipeline_ocr_provider_mode,
            "voyage": settings.pipeline_voyage_provider_mode,
            "classification": settings.pipeline_classification_provider_mode,
            "voyage_dimensions": settings.pipeline_voyage_output_dimensions,
        },
        "seeded_memes": [_seeded_meme_payload(item) for item in seeded],
        "collection_management": build_collection_management_fixture_payload(collection_fixture),
        "public_trends": public_trends_artifact,
        "created_meme": {
            "meme_id": str(detail.meme_id),
            "meme_file_id": str(meme_file_id),
            "ingest_request_id": str(materialized_request.id),
            "ingest_request_status": materialized_request.status.value,
            "initial_visibility": "private",
            "target_collection_id": str(private_upload_fixture.collection_id),
            "crawler_promotion_outcome": promotion_result.outcome.value,
            "crawler_source_id": E2E_PROMOTION_SOURCE_ID,
            "slug": slug,
            "query": "cat",
            "title": "Created cat pipeline meme",
        },
        "audio_safe_media": audio_profile_proofs,
        "proof": {
            "public_search_total": created_search_payload.get("total"),
            "private_search_hit_ids_before_promotion": [
                item.get("meme", {}).get("id")
                for item in private_search_payload.get("items", [])
                if isinstance(item, dict)
            ],
            "public_search_hit_ids": [
                item.get("meme", {}).get("id")
                for item in created_search_payload.get("items", [])
                if isinstance(item, dict)
            ],
            "public_detail_id": created_detail_payload.get("id"),
            "seeded_detail_ids_by_slug": seeded_proofs,
            "public_trends": public_trends_proof,
        },
    }
    seed_path = artifacts_dir / "seed.json"
    seed_path.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {seed_path}")


async def ensure_bucket(*, settings: Settings, s3_client: Any) -> None:
    storage_settings = get_pipeline_storage_settings(settings)
    try:
        await asyncio.to_thread(s3_client.head_bucket, Bucket=storage_settings.bucket)
        return
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in {404, 400}:
            raise E2ESeedError(f"Unable to inspect S3 bucket {storage_settings.bucket}: {exc}") from exc

    kwargs: dict[str, Any] = {"Bucket": storage_settings.bucket}
    if storage_settings.region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": storage_settings.region}
    try:
        await asyncio.to_thread(s3_client.create_bucket, **kwargs)
    except ClientError as exc:
        raise E2ESeedError(f"Unable to create S3 bucket {storage_settings.bucket}: {exc}") from exc


async def ensure_qdrant_collection(*, settings: Settings) -> None:
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        timeout=max(1, int(settings.pipeline_qdrant_timeout_seconds)),
    )
    try:
        exists = await client.collection_exists(settings.pipeline_qdrant_collection_name)
        if not exists:
            await client.create_collection(
                collection_name=settings.pipeline_qdrant_collection_name,
                vectors_config=VectorParams(
                    size=settings.pipeline_voyage_output_dimensions,
                    distance=Distance.COSINE,
                ),
            )
            return

        info = await client.get_collection(settings.pipeline_qdrant_collection_name)
        size = _extract_qdrant_vector_size(info)
        if size is not None and size != settings.pipeline_voyage_output_dimensions:
            raise E2ESeedError(
                f"Qdrant collection dimension mismatch: {size} != {settings.pipeline_voyage_output_dimensions}",
            )
    except E2ESeedError:
        raise
    except Exception as exc:
        raise E2ESeedError(f"Unable to ensure Qdrant collection: {exc}") from exc
    finally:
        await client.close()


def build_seed_meme(
    *,
    spec: SeedSpec,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID,
    public_trends_template_id: uuid.UUID,
) -> Meme:
    return Meme(
        id=meme_id,
        media_type=spec.media_type,
        primary_file_id=meme_file_id,
        ocr_text=spec.ocr_text,
        language=spec.language,
        is_nsfw=spec.is_nsfw,
        is_public=True,
        like_count=_public_trend_like_count(spec.category),
        tags=list(spec.tags),
        template_id=public_trends_template_id if _seed_spec_has_public_trends(spec) else None,
    )


async def seed_direct_corpus(
    *,
    settings: Settings,
    s3_client: Any,
    qdrant_sync_client: PipelineQdrantSyncClient,
    meili_client: PipelineMeilisearchSyncClient,
    specs: list[SeedSpec],
) -> list[SeededMeme]:
    storage_settings = get_pipeline_storage_settings(settings)
    voyage_client = build_pipeline_voyage_client(settings=settings)
    session_factory = get_async_session_factory()

    async with session_factory() as session:
        seeded: list[SeededMeme] = []
        public_trends_template = build_public_trends_template()
        session.add(public_trends_template)
        await session.flush()
        for spec in specs:
            image_bytes = build_seed_png_bytes(spec)
            object_key = f"{storage_settings.original_prefix}/e2e-prd/{spec.category}.png"
            await asyncio.to_thread(
                s3_client.put_object,
                Bucket=storage_settings.bucket,
                Key=object_key,
                Body=image_bytes,
                ContentType="image/png",
                ContentLength=len(image_bytes),
            )
            embedding = await voyage_client.embed_image(image_bytes=image_bytes, mime_type="image/png")
            now = utcnow()
            meme_id = _stable_uuid(f"{spec.category}:meme")
            meme_file_id = _stable_uuid(f"{spec.category}:file")
            meme = build_seed_meme(
                spec=spec,
                meme_id=meme_id,
                meme_file_id=meme_file_id,
                public_trends_template_id=public_trends_template.id,
            )
            session.add(meme)
            await session.flush()
            meme_file = MemeFile(
                id=meme_file_id,
                meme_id=meme_id,
                status=ContentProcessingStatus.READY,
                width=64,
                height=64,
                file_size_bytes=len(image_bytes),
                mime_type="image/png",
                s3_original_key=object_key,
                quality_score=1.0,
            )
            session.add(meme_file)
            await session.flush()
            source_id = _stable_uuid(f"{spec.category}:source")
            session.add_all(
                [
                    MemeSource(
                        id=source_id,
                        file_id=meme_file_id,
                        platform=SourcePlatform.TELEGRAM,
                        source_id=E2E_SOURCE_ID,
                        post_id=spec.category,
                        is_first_source=True,
                        source_alive=True,
                    ),
                    MemeFileOCRResult(
                        meme_file_id=meme_file_id,
                        engine="fake-seed",
                        fallback_engine=None,
                        fallback_used=False,
                        low_confidence=False,
                        confidence=1.0,
                        language=spec.language,
                        extracted_text=spec.ocr_text,
                        source_object_key=object_key,
                    ),
                    EmbeddingCache(
                        input_hash=embedding.input_hash,
                        input_type=EmbeddingInputType.IMAGE,
                        embedding=embedding.embedding_bytes,
                        model_version=embedding.model,
                        source_file_id=meme_file_id,
                    ),
                    MemeSeoPage(
                        meme_id=meme_id,
                        slug=spec.slug,
                        page_title=spec.caption,
                        meta_description=f"Search fixture for {spec.category} PRD E2E.",
                        alt_text=spec.alt_text,
                        caption=spec.caption,
                        body_text=f"Deterministic {spec.category} PRD E2E fixture.",
                        tags=list(spec.tags),
                        model_id=E2E_MODEL_ID,
                        prompt_version=E2E_PROMPT_VERSION,
                        generated_at=now,
                    ),
                    *build_public_trend_snapshot_rows(meme_source_id=source_id, category=spec.category),
                    *build_public_trend_analytics_event_rows(meme_id=meme_id, category=spec.category),
                    *_build_succeeded_stage_rows(
                        meme_file_id=meme_file_id,
                        event_id=_stable_uuid(f"{spec.category}:event"),
                        now=now,
                    ),
                ],
            )
            await session.flush()

            loaded_index_state = await load_search_index_state(session, meme_file_id)
            qdrant_payload = build_qdrant_sync_payload(loaded_index_state.canonical)
            meili_document = build_meilisearch_document(loaded_index_state.canonical)

            await qdrant_sync_client.upsert_meme_point(
                qdrant_payload,
                embedding.vector,
            )
            await meili_client.upsert_document(meili_document)
            session.add_all(
                [
                    _build_sync_snapshot(
                        meme_file_id=meme_file_id,
                        target=SyncTargetKind.QDRANT,
                        preview={
                            "meme_id": str(qdrant_payload.meme_id),
                            "search_index_algorithm_version": qdrant_payload.search_index_algorithm_version,
                            "is_public": qdrant_payload.is_public,
                            "tags": list(qdrant_payload.tags),
                        },
                        now=now,
                    ),
                    _build_sync_snapshot(
                        meme_file_id=meme_file_id,
                        target=SyncTargetKind.MEILISEARCH,
                        preview={
                            "id": meili_document.id,
                            "search_index_algorithm_version": meili_document.search_index_algorithm_version,
                            "is_public": meili_document.is_public,
                            "ocr_text": meili_document.ocr_text or "",
                        },
                        now=now,
                    ),
                ],
            )
            seeded.append(
                SeededMeme(
                    category=spec.category,
                    meme_id=meme_id,
                    meme_file_id=meme_file_id,
                    slug=spec.slug,
                    query=spec.query,
                    object_key=object_key,
                    title=spec.caption,
                    tags=spec.tags,
                    is_nsfw=spec.is_nsfw,
                    language=spec.language,
                    media_type=spec.media_type,
                ),
            )
        await session.commit()

    return seeded


def build_collection_management_fixture(seeded: list[SeededMeme]) -> SeededCollectionManagementFixture:
    """Build the deterministic full-account collection fixture descriptor."""

    cat = _require_seeded_category(seeded, "cat")
    dog = _require_seeded_category(seeded, "dog")
    return SeededCollectionManagementFixture(
        owner=SeededE2EUser(
            label="owner",
            user_id=_stable_uuid("collection-management:owner:user"),
            email=E2E_OWNER_EMAIL,
            password=E2E_ACCOUNT_PASSWORD,
        ),
        member=SeededE2EUser(
            label="member",
            user_id=_stable_uuid("collection-management:member:user"),
            email=E2E_MEMBER_EMAIL,
            password=E2E_ACCOUNT_PASSWORD,
        ),
        collection_id=_stable_uuid("collection-management:launch:collection"),
        title=E2E_COLLECTION_TITLE,
        description=E2E_COLLECTION_DESCRIPTION,
        visibility=CollectionVisibility.PRIVATE,
        saved_memes=(cat, dog),
        pinned_memes=(cat, dog),
        invite_id=_stable_uuid("collection-management:launch:viewer-invite"),
        invite_token=E2E_COLLECTION_INVITE_TOKEN,
    )


async def seed_collection_management_fixture(
    *,
    settings: Settings,
    qdrant_sync_client: QdrantSyncClientProtocol,
    meili_client: MeilisearchSyncClientProtocol,
    seeded: list[SeededMeme],
) -> SeededCollectionManagementFixture:
    """Persist full-account users, a private collection, an invite, saves, and pins."""

    fixture = build_collection_management_fixture(seeded)
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        await seed_collection_management_fixture_in_session(
            session,
            settings=settings,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
            fixture=fixture,
        )
        await session.commit()
    return fixture


async def seed_collection_management_fixture_in_session(
    session: AsyncSession,
    *,
    settings: Settings,
    qdrant_sync_client: QdrantSyncClientProtocol,
    meili_client: MeilisearchSyncClientProtocol,
    fixture: SeededCollectionManagementFixture,
) -> None:
    now = utcnow()
    owner = User(
        id=fixture.owner.user_id,
        status=AccountStatus.ACTIVE,
        email=fixture.owner.email,
        email_verified_at=now,
        password_hash=E2E_ACCOUNT_PASSWORD_HASH,
        nsfw_enabled=False,
    )
    member = User(
        id=fixture.member.user_id,
        status=AccountStatus.ACTIVE,
        email=fixture.member.email,
        email_verified_at=now,
        password_hash=E2E_ACCOUNT_PASSWORD_HASH,
        nsfw_enabled=False,
    )
    session.add_all([owner, member])
    await session.flush()

    collection = Collection(
        id=fixture.collection_id,
        owner_id=fixture.owner.user_id,
        title=fixture.title,
        description=fixture.description,
        kind=CollectionKind.CUSTOM,
        visibility=fixture.visibility,
    )
    session.add(collection)
    await session.flush()
    owner.active_save_collection_id = collection.id

    session.add_all(
        [
            CollectionMember(
                collection_id=collection.id,
                user_id=fixture.owner.user_id,
                role=CollectionMembershipRole.OWNER,
            ),
            CollectionInvite(
                id=fixture.invite_id,
                collection_id=collection.id,
                created_by_user_id=fixture.owner.user_id,
                token_hash=_collection_invite_token_hash(fixture.invite_token),
                role=CollectionMembershipRole.VIEWER,
                channel=CollectionInviteChannel.DIRECT_LINK,
                label="E2E viewer invite",
                status=CollectionInviteStatus.PENDING,
                max_uses=None,
                use_count=0,
                expires_at=None,
            ),
            *(
                CollectionMeme(
                    collection_id=collection.id,
                    meme_id=meme.meme_id,
                    added_by_user_id=fixture.owner.user_id,
                    added_at=now,
                )
                for meme in fixture.saved_memes
            ),
            *(
                PinnedMeme(
                    user_id=fixture.owner.user_id,
                    meme_id=meme.meme_id,
                    position=index,
                    pinned_at=now,
                )
                for index, meme in enumerate(fixture.pinned_memes, start=1)
            ),
        ],
    )
    await session.flush()

    for meme in fixture.saved_memes:
        await _resync_seeded_meme_indexes_for_collection_fixture(
            session,
            settings=settings,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
            meme=meme,
            now=now,
        )


async def _resync_seeded_meme_indexes_for_collection_fixture(
    session: AsyncSession,
    *,
    settings: Settings,
    qdrant_sync_client: QdrantSyncClientProtocol,
    meili_client: MeilisearchSyncClientProtocol,
    meme: SeededMeme,
    now: datetime,
) -> None:
    loaded_index_state = await load_search_index_state(
        session,
        meme.meme_file_id,
        vector_dimensions=settings.pipeline_voyage_output_dimensions,
    )
    if loaded_index_state.vector is None:
        raise E2ESeedError(f"Seeded collection meme file {meme.meme_file_id} has no embedding vector.")

    qdrant_payload = build_qdrant_sync_payload(loaded_index_state.canonical)
    meili_document = build_meilisearch_document(loaded_index_state.canonical)
    await qdrant_sync_client.upsert_meme_point(qdrant_payload, loaded_index_state.vector)
    await meili_client.upsert_document(meili_document)
    await _upsert_sync_snapshot(
        session,
        meme_file_id=meme.meme_file_id,
        target=SyncTargetKind.QDRANT,
        preview={
            "meme_id": str(qdrant_payload.meme_id),
            "search_index_algorithm_version": qdrant_payload.search_index_algorithm_version,
            "is_public": qdrant_payload.is_public,
            "collection_ids": list(qdrant_payload.collection_ids),
        },
        now=now,
    )
    await _upsert_sync_snapshot(
        session,
        meme_file_id=meme.meme_file_id,
        target=SyncTargetKind.MEILISEARCH,
        preview={
            "id": meili_document.id,
            "search_index_algorithm_version": meili_document.search_index_algorithm_version,
            "is_public": meili_document.is_public,
            "collection_ids": list(meili_document.collection_ids),
        },
        now=now,
    )


def build_collection_management_fixture_payload(fixture: SeededCollectionManagementFixture) -> dict[str, Any]:
    return {
        "owner": _seeded_e2e_user_payload(fixture.owner),
        "member": _seeded_e2e_user_payload(fixture.member),
        "collection": {
            "id": str(fixture.collection_id),
            "title": fixture.title,
            "description": fixture.description,
            "visibility": fixture.visibility.value,
        },
        "invite": {
            "id": str(fixture.invite_id),
            "token": fixture.invite_token,
            "join_path": fixture.invite_path,
        },
        "saved_memes": [_seeded_meme_payload(item) for item in fixture.saved_memes],
        "pinned_memes": [_seeded_meme_payload(item) for item in fixture.pinned_memes],
    }


def _seeded_e2e_user_payload(user: SeededE2EUser) -> dict[str, str]:
    return {
        "label": user.label,
        "user_id": str(user.user_id),
        "email": user.email,
        "password": user.password,
    }


def _seeded_meme_payload(item: SeededMeme) -> dict[str, object]:
    return {
        "category": item.category,
        "meme_id": str(item.meme_id),
        "meme_file_id": str(item.meme_file_id),
        "slug": item.slug,
        "query": item.query,
        "object_key": item.object_key,
        "title": item.title,
        "tags": list(item.tags),
        "is_nsfw": item.is_nsfw,
        "language": item.language.value,
        "media_type": item.media_type.value,
    }


def build_public_trends_template() -> MemeTemplate:
    return MemeTemplate(
        id=_stable_uuid("public-trends:template"),
        slug=E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
        name=E2E_PUBLIC_TRENDS_TEMPLATE_NAME,
        description=E2E_PUBLIC_TRENDS_TEMPLATE_DESCRIPTION,
        is_curated=True,
    )


def build_public_trend_snapshot_rows(
    *,
    meme_source_id: uuid.UUID,
    category: str,
) -> list[MemeSourceEngagementSnapshot]:
    specs = PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category, ())
    if not specs:
        return []

    rows = [
        MemeSourceEngagementSnapshot(
            id=_stable_uuid(f"{category}:source-engagement-baseline"),
            meme_source_id=meme_source_id,
            captured_at=specs[0].captured_at - timedelta(seconds=1),
            capture_reason=SourceEngagementCaptureReason.INGEST_INITIAL,
            schedule_label=SourceEngagementScheduleLabel.INGEST_INITIAL,
            view_count=0,
            reactions={},
            reaction_count=0,
            comment_count=None,
            forward_count=0,
            comments_state=SourceEngagementCommentsState.UNKNOWN,
            fetch_status=SourceEngagementFetchStatus.SUCCESS,
            source_alive=True,
            raw_metrics={"seed": "baseline"},
        )
    ]
    source_views = 0
    source_reactions = 0
    source_reposts = 0
    for index, spec in enumerate(specs, start=1):
        source_views += spec.source_views
        source_reactions += spec.source_reactions
        source_reposts += spec.source_reposts
        rows.append(
            MemeSourceEngagementSnapshot(
                id=_stable_uuid(f"{category}:source-engagement-snapshot:{index}"),
                meme_source_id=meme_source_id,
                captured_at=spec.captured_at,
                capture_reason=SourceEngagementCaptureReason.SCHEDULED,
                schedule_label=SourceEngagementScheduleLabel.PLUS_1D,
                view_count=source_views,
                reactions={"like": source_reactions} if source_reactions else {},
                reaction_count=source_reactions,
                comment_count=None,
                forward_count=source_reposts,
                comments_state=SourceEngagementCommentsState.UNKNOWN,
                fetch_status=SourceEngagementFetchStatus.SUCCESS,
                source_alive=True,
                raw_metrics={"seed": "public_trends"},
            )
        )
    return rows


def build_public_trend_analytics_event_rows(*, meme_id: uuid.UUID, category: str) -> list[AnalyticsEvent]:
    rows: list[AnalyticsEvent] = []
    specs = PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category, ())
    for snapshot_index, spec in enumerate(specs, start=1):
        for metric, event_type, count in _public_trend_platform_events(spec):
            for event_index in range(1, count + 1):
                rows.append(
                    AnalyticsEvent(
                        id=_public_trend_analytics_event_id(
                            category=category,
                            snapshot_index=snapshot_index,
                            metric=metric,
                            event_index=event_index,
                        ),
                        user_id=None,
                        event_type=event_type,
                        payload={
                            "meme_id": str(meme_id),
                            "seed": "e2e-prd-public-trends",
                            "category": category,
                            "metric": metric,
                            "snapshot_index": snapshot_index,
                        },
                        occurred_at=spec.captured_at,
                    )
                )
    return rows


def build_public_trends_artifact(seeded: list[SeededMeme]) -> dict[str, object]:
    representative = _require_seeded_category(seeded, "cat")
    compare_items = [
        f"meme:{representative.slug}",
        f"tag:{E2E_PUBLIC_TRENDS_TAG_SLUG}",
        f"template:{E2E_PUBLIC_TRENDS_TEMPLATE_SLUG}",
    ]
    aggregate_history_points = build_public_trend_aggregate_history_points_payload()
    timeline_snapshot_count = sum(int(point["snapshot_count"]) for point in aggregate_history_points)
    return {
        "trend_path": "/trends",
        "tag": {
            "slug": E2E_PUBLIC_TRENDS_TAG_SLUG,
            "title": _public_tag_title(E2E_PUBLIC_TRENDS_TAG_SLUG),
            "path": f"/tags/{E2E_PUBLIC_TRENDS_TAG_SLUG}",
            "history_points": [dict(point) for point in aggregate_history_points],
        },
        "template": {
            "slug": E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
            "title": f"{E2E_PUBLIC_TRENDS_TEMPLATE_NAME} memes",
            "path": f"/templates/{E2E_PUBLIC_TRENDS_TEMPLATE_SLUG}",
            "history_points": [dict(point) for point in aggregate_history_points],
        },
        "compare": {
            "items": compare_items,
            "path": _query_path("/trends/compare", [("item", item) for item in compare_items]),
        },
        "timeline": {
            "path": _query_path(
                "/trends/timeline",
                [("granularity", E2E_PUBLIC_TRENDS_TIMELINE_GRANULARITY)],
            ),
            "granularity": E2E_PUBLIC_TRENDS_TIMELINE_GRANULARITY,
            "period": E2E_PUBLIC_TRENDS_TIMELINE_PERIOD,
            "period_label": "January 2026",
            "snapshot_count": timeline_snapshot_count,
        },
        "representative_meme": {
            "category": representative.category,
            "slug": representative.slug,
            "title": representative.title,
        },
    }


def build_public_trend_aggregate_history_points_payload() -> list[dict[str, str | int | float]]:
    totals_by_observed_at: dict[datetime, dict[str, float | int]] = {}
    meme_categories_by_observed_at: dict[datetime, set[str]] = {}
    for category in E2E_PUBLIC_TRENDS_MEME_CATEGORIES:
        for observed_at, metrics in _public_trend_daily_metrics(category).items():
            totals = totals_by_observed_at.setdefault(
                observed_at,
                {
                    "snapshot_count": 0,
                    "source_views": 0,
                    "source_reactions": 0,
                    "source_reposts": 0,
                    "platform_views": 0,
                    "platform_sends": 0,
                    "platform_saves": 0,
                    "platform_likes": 0,
                    "value": 0.0,
                },
            )
            meme_categories_by_observed_at.setdefault(observed_at, set()).add(category)
            totals["snapshot_count"] += metrics["snapshot_count"]
            totals["source_views"] += metrics["source_views"]
            totals["source_reactions"] += metrics["source_reactions"]
            totals["source_reposts"] += metrics["source_reposts"]
            totals["platform_views"] += metrics["platform_views"]
            totals["platform_sends"] += metrics["platform_sends"]
            totals["platform_saves"] += metrics["platform_saves"]
            totals["platform_likes"] += metrics["platform_likes"]
            totals["value"] += _public_trend_popularity_score(metrics)

    return [
        {
            "observed_at": observed_at.isoformat(),
            "value": round(float(totals_by_observed_at[observed_at]["value"]), 1),
            "metric": "aggregate_popularity_score",
            "label": "Aggregate popularity score",
            "meme_count": len(meme_categories_by_observed_at[observed_at]),
            "snapshot_count": totals_by_observed_at[observed_at]["snapshot_count"],
            "source_views": totals_by_observed_at[observed_at]["source_views"],
            "source_reactions": totals_by_observed_at[observed_at]["source_reactions"],
            "source_reposts": totals_by_observed_at[observed_at]["source_reposts"],
            "platform_views": totals_by_observed_at[observed_at]["platform_views"],
            "platform_sends": totals_by_observed_at[observed_at]["platform_sends"],
            "platform_saves": totals_by_observed_at[observed_at]["platform_saves"],
            "platform_likes": totals_by_observed_at[observed_at]["platform_likes"],
        }
        for observed_at in sorted(totals_by_observed_at)
    ]


def _public_trend_daily_metrics(category: str) -> dict[datetime, dict[str, int]]:
    metrics_by_observed_at: dict[datetime, dict[str, int]] = {}
    previous_source_views: int | None = None
    previous_source_reactions: int | None = None
    previous_source_reposts: int | None = None

    # The MV counts all successful source snapshots, including the zero baseline
    # row used to turn cumulative source counters into first-day deltas.
    for row in sorted(
        build_public_trend_snapshot_rows(meme_source_id=_stable_uuid(f"{category}:source"), category=category),
        key=lambda item: (item.captured_at, str(item.id)),
    ):
        observed_at = _public_trend_observed_at(row.captured_at)
        metrics = metrics_by_observed_at.setdefault(observed_at, _empty_public_trend_daily_metrics())
        metrics["snapshot_count"] += 1

        current_source_views = _optional_int(row.view_count)
        current_source_reactions = _optional_int(row.reaction_count)
        current_source_reposts = _optional_int(row.forward_count)
        metrics["source_views"] += _non_negative_delta(current_source_views, previous_source_views)
        metrics["source_reactions"] += _non_negative_delta(current_source_reactions, previous_source_reactions)
        metrics["source_reposts"] += _non_negative_delta(current_source_reposts, previous_source_reposts)
        previous_source_views = current_source_views
        previous_source_reactions = current_source_reactions
        previous_source_reposts = current_source_reposts

    for spec in PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category, ()):
        observed_at = _public_trend_observed_at(spec.captured_at)
        metrics = metrics_by_observed_at.setdefault(observed_at, _empty_public_trend_daily_metrics())
        metrics["platform_views"] += spec.platform_views
        metrics["platform_sends"] += spec.platform_sends
        metrics["platform_saves"] += spec.platform_saves
        metrics["platform_likes"] += spec.platform_likes

    return {
        observed_at: metrics
        for observed_at, metrics in metrics_by_observed_at.items()
        if any(
            metrics[key] > 0
            for key in (
                "source_views",
                "source_reactions",
                "source_reposts",
                "platform_views",
                "platform_sends",
                "platform_saves",
                "platform_likes",
            )
        )
    }


def _empty_public_trend_daily_metrics() -> dict[str, int]:
    return {
        "snapshot_count": 0,
        "source_views": 0,
        "source_reactions": 0,
        "source_reposts": 0,
        "platform_views": 0,
        "platform_sends": 0,
        "platform_saves": 0,
        "platform_likes": 0,
    }


def _optional_int(value: int | None) -> int | None:
    return None if value is None else int(value)


def _non_negative_delta(current: int | None, previous: int | None) -> int:
    if current is None or previous is None:
        return 0
    return max(current - previous, 0)


def _seed_spec_has_public_trends(spec: SeedSpec) -> bool:
    return not spec.is_nsfw and spec.category in PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY


def _public_trend_like_count(category: str) -> int:
    specs = PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category)
    if not specs:
        return 0
    return sum(spec.platform_likes for spec in specs)


def _public_trend_analytics_event_ids(category: str) -> list[uuid.UUID]:
    event_ids: list[uuid.UUID] = []
    specs = PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category, ())
    for snapshot_index, spec in enumerate(specs, start=1):
        for metric, _, count in _public_trend_platform_events(spec):
            event_ids.extend(
                _public_trend_analytics_event_id(
                    category=category,
                    snapshot_index=snapshot_index,
                    metric=metric,
                    event_index=event_index,
                )
                for event_index in range(1, count + 1)
            )
    return event_ids


def _public_trend_analytics_event_id(
    *,
    category: str,
    snapshot_index: int,
    metric: str,
    event_index: int,
) -> uuid.UUID:
    return _stable_uuid(f"{category}:public-trend-event:{snapshot_index}:{metric}:{event_index}")


def _public_trend_platform_events(
    spec: PublicTrendSnapshotSpec,
) -> tuple[tuple[str, AnalyticsEventType, int], ...]:
    return (
        ("platform_views", AnalyticsEventType.MEME_VIEW, spec.platform_views),
        ("platform_sends", AnalyticsEventType.MEME_SEND, spec.platform_sends),
        ("platform_saves", AnalyticsEventType.MEME_SAVE, spec.platform_saves),
        ("platform_likes", AnalyticsEventType.MEME_LIKE, spec.platform_likes),
    )


def _public_trend_popularity_score(metrics: dict[str, int]) -> float:
    return (
        math.log1p(max(metrics["source_views"], 0)) * 1.0
        + math.log1p(max(metrics["source_reactions"], 0)) * 2.0
        + math.log1p(max(metrics["source_reposts"], 0)) * 3.0
        + math.log1p(max(metrics["platform_views"], 0)) * 1.0
        + math.log1p(max(metrics["platform_sends"], 0)) * 3.0
        + math.log1p(max(metrics["platform_saves"], 0)) * 4.0
        + math.log1p(max(metrics["platform_likes"], 0)) * 5.0
    )


def _public_trend_observed_at(captured_at: datetime) -> datetime:
    return datetime(captured_at.year, captured_at.month, captured_at.day, tzinfo=UTC)


def _public_tag_title(tag: str) -> str:
    return f"{tag.replace('-', ' ').title()} memes"


def _query_path(path: str, params: list[tuple[str, str]]) -> str:
    query = urlencode(params)
    return f"{path}?{query}" if query else path


def _require_seeded_category(seeded: list[SeededMeme], category: str) -> SeededMeme:
    for item in seeded:
        if item.category == category:
            return item
    raise E2ESeedError(f"Seeded corpus did not include required {category!r} fixture.")


def _collection_invite_token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def build_seed_specs() -> list[SeedSpec]:
    return [
        SeedSpec(
            category="cat",
            color=(255, 0, 0),
            slug="e2e-prd-cat-search",
            ocr_text="cat e2e prd corpus meme",
            caption="Deterministic cat search meme",
            alt_text="Red square cat PRD E2E meme fixture",
            query="cat",
            tags=("cat", "e2e-prd", E2E_PUBLIC_TRENDS_TAG_SLUG),
        ),
        SeedSpec(
            category="dog",
            color=(0, 0, 255),
            slug="e2e-prd-dog-search",
            ocr_text="dog e2e prd corpus meme",
            caption="Deterministic dog search meme",
            alt_text="Blue square dog PRD E2E meme fixture",
            query="dog",
            tags=("dog", "e2e-prd", E2E_PUBLIC_TRENDS_TAG_SLUG),
        ),
        SeedSpec(
            category="frog",
            color=(0, 255, 0),
            slug="e2e-prd-frog-search",
            ocr_text="frog e2e prd corpus meme",
            caption="Deterministic frog search meme",
            alt_text="Green square frog PRD E2E meme fixture",
            query="frog",
            tags=("frog", "e2e-prd", E2E_PUBLIC_TRENDS_TAG_SLUG),
        ),
        SeedSpec(
            category="cat-nsfw",
            color=(128, 0, 128),
            slug="e2e-prd-cat-nsfw",
            ocr_text="cat nsfw e2e prd corpus meme",
            caption="Deterministic cat NSFW meme",
            alt_text="Purple square cat NSFW PRD E2E meme fixture",
            query="cat",
            tags=("cat", "e2e-prd", "nsfw-fixture"),
            is_nsfw=True,
        ),
    ]


async def cleanup_e2e_rows(*, settings: Settings, specs: list[SeedSpec]) -> None:
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        await cleanup_seed_rows(session, settings=settings, specs=specs)
        await session.commit()


async def cleanup_seed_rows(session: AsyncSession, *, settings: Settings, specs: list[SeedSpec]) -> None:
    await cleanup_private_upload_fixture_rows(session)
    await cleanup_collection_management_fixture_rows(session)

    analytics_event_ids = [event_id for spec in specs for event_id in _public_trend_analytics_event_ids(spec.category)]
    if analytics_event_ids:
        await session.execute(delete(AnalyticsEvent).where(AnalyticsEvent.id.in_(analytics_event_ids)))

    source_result = await session.execute(
        select(MemeSource).where(
            MemeSource.source_id.in_([E2E_SOURCE_ID, E2E_UPLOAD_SOURCE_ID, E2E_PROMOTION_SOURCE_ID])
        ),
    )
    meme_ids: set[uuid.UUID] = set()
    for source in source_result.scalars():
        meme_file = await session.get(MemeFile, source.file_id)
        if meme_file is not None:
            meme_ids.add(meme_file.meme_id)

    slug_result = await session.execute(
        select(MemeSeoPage).where(
            MemeSeoPage.slug.in_([spec.slug for spec in specs]) | MemeSeoPage.slug.like("e2e-prd-created-%"),
        ),
    )
    for seo_page in slug_result.scalars():
        meme_ids.add(seo_page.meme_id)
    meme_ids.update(_stable_uuid(f"{spec.category}:meme") for spec in specs)

    for meme_id in meme_ids:
        meme = await session.get(Meme, meme_id)
        if meme is not None:
            await session.delete(meme)
    await session.flush()
    await cleanup_public_trends_template_rows(session)

    voyage_client = build_pipeline_voyage_client(settings=settings)
    for spec in specs:
        embedding = await voyage_client.embed_image(image_bytes=build_seed_png_bytes(spec), mime_type="image/png")
        result = await session.execute(
            select(EmbeddingCache).where(
                EmbeddingCache.input_hash == embedding.input_hash,
                EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
            ),
        )
        for cache_row in result.scalars():
            await session.delete(cache_row)
    await session.flush()


async def seed_private_upload_fixture() -> PrivateUploadFixture:
    fixture = PrivateUploadFixture(
        user_id=_stable_uuid("private-upload:user"),
        collection_id=_stable_uuid("private-upload:favorites"),
        crawler_channel_id=_stable_uuid("private-upload:crawler-channel"),
    )
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        user = User(
            id=fixture.user_id,
            status=AccountStatus.ACTIVE,
            email=E2E_UPLOAD_USER_EMAIL,
            email_verified_at=utcnow(),
            password_hash=E2E_ACCOUNT_PASSWORD_HASH,
            nsfw_enabled=False,
        )
        favorites = Collection(
            id=fixture.collection_id,
            owner_id=fixture.user_id,
            title="Favorites",
            kind=CollectionKind.FAVORITES,
            visibility=CollectionVisibility.PRIVATE,
        )
        crawler_channel = SourceChannel(
            id=fixture.crawler_channel_id,
            platform=SourcePlatform.TELEGRAM,
            platform_id=E2E_PROMOTION_SOURCE_ID,
            title="E2E public crawler promotion source",
            is_active=True,
        )
        session.add_all([user, crawler_channel])
        await session.flush()
        session.add(favorites)
        await session.flush()
        user.active_save_collection_id = favorites.id
        session.add(
            CollectionMember(
                collection_id=favorites.id,
                user_id=user.id,
                role=CollectionMembershipRole.OWNER,
            )
        )
        await session.commit()
    return fixture


async def cleanup_private_upload_fixture_rows(session: AsyncSession) -> None:
    user_id = _stable_uuid("private-upload:user")
    collection_id = _stable_uuid("private-upload:favorites")
    channel_id = _stable_uuid("private-upload:crawler-channel")
    await session.execute(
        update(User)
        .where(or_(User.id == user_id, User.email == E2E_UPLOAD_USER_EMAIL))
        .values(active_save_collection_id=None)
    )
    collections = await session.scalars(
        select(Collection).where(or_(Collection.id == collection_id, Collection.owner_id == user_id))
    )
    for collection in collections:
        await session.delete(collection)
    channels = await session.scalars(
        select(SourceChannel).where(
            or_(
                SourceChannel.id == channel_id,
                SourceChannel.platform_id == E2E_PROMOTION_SOURCE_ID,
            )
        )
    )
    for channel in channels:
        await session.delete(channel)
    users = await session.scalars(select(User).where(or_(User.id == user_id, User.email == E2E_UPLOAD_USER_EMAIL)))
    for user in users:
        await session.delete(user)
    await session.flush()


async def cleanup_public_trends_template_rows(session: AsyncSession) -> None:
    result = await session.execute(
        select(MemeTemplate).where(
            or_(
                MemeTemplate.id == _stable_uuid("public-trends:template"),
                MemeTemplate.slug == E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
            ),
        ),
    )
    for template in result.scalars():
        await session.delete(template)
    await session.flush()


async def cleanup_collection_management_fixture_rows(session: AsyncSession) -> None:
    user_ids = [
        _stable_uuid("collection-management:owner:user"),
        _stable_uuid("collection-management:member:user"),
    ]
    collection_id = _stable_uuid("collection-management:launch:collection")
    collection_ids_result = await session.execute(
        select(Collection.id).where(or_(Collection.id == collection_id, Collection.owner_id.in_(user_ids))),
    )
    collection_ids = list(collection_ids_result.scalars())

    await session.execute(
        update(User)
        .where(
            or_(
                User.id.in_(user_ids),
                User.email.in_([E2E_OWNER_EMAIL, E2E_MEMBER_EMAIL]),
                User.active_save_collection_id.in_(collection_ids),
            ),
        )
        .values(active_save_collection_id=None),
    )
    await session.execute(delete(PinnedMeme).where(PinnedMeme.user_id.in_(user_ids)))

    collections_result = await session.execute(select(Collection).where(Collection.id.in_(collection_ids)))
    for collection in collections_result.scalars():
        await session.delete(collection)
    await session.flush()

    users_result = await session.execute(
        select(User).where(or_(User.id.in_(user_ids), User.email.in_([E2E_OWNER_EMAIL, E2E_MEMBER_EMAIL]))),
    )
    for user in users_result.scalars():
        await session.delete(user)
    await session.flush()


async def assert_private_upload_state(
    *,
    meme_id: uuid.UUID,
    meme_file_id: uuid.UUID,
    fixture: PrivateUploadFixture,
) -> None:
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        meme = await session.get(Meme, meme_id)
        if meme is None:
            raise E2ESeedError(f"Private upload meme {meme_id} is missing from PostgreSQL.")
        if meme.is_public or meme.visibility_mode is not MemeVisibilityMode.AUTO:
            raise E2ESeedError(
                f"Upload {meme_id} did not remain AUTO/private before crawler promotion: "
                f"mode={meme.visibility_mode.value} is_public={meme.is_public}"
            )
        if await session.get(CollectionMeme, (fixture.collection_id, meme_id)) is None:
            raise E2ESeedError(f"Private upload {meme_id} is missing collection membership {fixture.collection_id}.")
        source = await session.scalar(
            select(MemeSource).where(
                MemeSource.file_id == meme_file_id,
                MemeSource.source_id == E2E_UPLOAD_SOURCE_ID,
            )
        )
        if source is None:
            raise E2ESeedError(f"Private upload file {meme_file_id} is missing its upload provenance.")
        if source.source_kind is not IngestSourceKind.OPERATOR_UPLOAD or source.uploader_user_id != fixture.user_id:
            raise E2ESeedError(
                f"Private upload provenance is incorrect: kind={source.source_kind.value} "
                f"uploader={source.uploader_user_id}."
            )


async def prepare_created_meme_metadata(*, meme_id: uuid.UUID, query: str) -> str:
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        slug = await prepare_created_meme_metadata_in_session(session, meme_id=meme_id, query=query)
        await session.commit()
    return slug


async def prepare_created_meme_metadata_in_session(
    session: AsyncSession,
    *,
    meme_id: uuid.UUID,
    query: str,
) -> str:
    slug = f"e2e-prd-created-{meme_id.hex[:12]}"
    meme = await session.get(Meme, meme_id)
    if meme is None:
        raise E2ESeedError(f"Created meme {meme_id} is missing from the database.")
    if meme.is_public:
        raise E2ESeedError(f"Created meme {meme_id} became public before crawler promotion.")
    meme.is_nsfw = False
    tags = list(meme.tags)
    for tag in (query, "e2e-prd"):
        if tag not in tags:
            tags.append(tag)
    meme.tags = tags
    existing_seo = await session.get(MemeSeoPage, meme_id)
    now = utcnow()
    if existing_seo is None:
        session.add(
            MemeSeoPage(
                meme_id=meme_id,
                slug=slug,
                page_title="Created cat pipeline meme",
                meta_description="Created cat upload proven through the containerized PRD E2E pipeline.",
                alt_text="Generated red square cat pipeline upload",
                caption="Created cat pipeline meme",
                body_text="Generated upload proven through pipeline, Qdrant, Meilisearch, and public API.",
                tags=[query, "e2e-prd"],
                model_id=E2E_MODEL_ID,
                prompt_version=E2E_PROMPT_VERSION,
                generated_at=now,
            ),
        )
    else:
        existing_seo.slug = slug
        existing_seo.page_title = "Created cat pipeline meme"
        existing_seo.meta_description = "Created cat upload proven through the containerized PRD E2E pipeline."
        existing_seo.alt_text = "Generated red square cat pipeline upload"
        existing_seo.caption = "Created cat pipeline meme"
        existing_seo.body_text = "Generated upload proven through pipeline, Qdrant, Meilisearch, and public API."
        existing_seo.tags = [query, "e2e-prd"]
        existing_seo.model_id = E2E_MODEL_ID
        existing_seo.prompt_version = E2E_PROMPT_VERSION
        existing_seo.generated_at = now
    await session.flush()
    return slug


async def promote_private_upload_with_crawler(
    *,
    settings: Settings,
    image_bytes: bytes,
    run_id: str,
    expected_meme_id: uuid.UUID,
    expected_meme_file_id: uuid.UUID,
) -> CrawlerIngestResult:
    session_factory = get_async_session_factory()
    crawler_post_id = f"promotion-{run_id}"
    async with session_factory() as session:
        service = PipelineCrawlerIngestService.from_settings(
            session,
            settings=settings,
            storage_client=get_s3_client(),
        )
        result = await service.accept_crawler_post(
            RawCrawlerPost(
                platform=SourcePlatform.TELEGRAM,
                source_id=E2E_PROMOTION_SOURCE_ID,
                post_id=crawler_post_id,
                published_at=utcnow(),
                media_type="photo",
                media_bytes=image_bytes,
                filename="e2e-prd-promoted-cat.png",
                content_type="image/png",
                view_count=2,
            )
        )
    if result.outcome is not CrawlerIngestOutcome.SHA256_EXACT_EXISTING_FILE:
        raise E2ESeedError(f"Crawler promotion returned unexpected outcome {result.outcome.value}.")
    if result.duplicate_of_meme_id != expected_meme_id or result.meme_file_id != expected_meme_file_id:
        raise E2ESeedError(
            "Crawler promotion did not reuse the private canonical meme/file: "
            f"meme={result.duplicate_of_meme_id} file={result.meme_file_id}."
        )

    async with session_factory() as session:
        meme = await session.get(Meme, expected_meme_id)
        if meme is None or not meme.is_public or meme.visibility_mode is not MemeVisibilityMode.AUTO:
            raise E2ESeedError(f"Crawler exact-SHA source did not AUTO-promote meme {expected_meme_id}.")
        file_count = await session.scalar(
            select(func.count()).select_from(MemeFile).where(MemeFile.meme_id == expected_meme_id)
        )
        if file_count != 1:
            raise E2ESeedError(
                f"Crawler exact-SHA promotion created an unexpected file; canonical file count={file_count}."
            )
        crawler_source = await session.scalar(
            select(MemeSource).where(
                MemeSource.file_id == expected_meme_file_id,
                MemeSource.source_id == E2E_PROMOTION_SOURCE_ID,
                MemeSource.post_id == crawler_post_id,
            )
        )
        if crawler_source is None or crawler_source.source_kind is not IngestSourceKind.PUBLIC_CRAWLER:
            raise E2ESeedError("Crawler promotion source provenance was not persisted as public_crawler.")
    return result


async def resync_created_public_meme_indexes(
    *,
    settings: Settings,
    meme_file_id: uuid.UUID,
    qdrant_sync_client: QdrantSyncClientProtocol,
    meili_client: MeilisearchSyncClientProtocol,
) -> None:
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        await resync_created_public_meme_indexes_in_session(
            session,
            settings=settings,
            meme_file_id=meme_file_id,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
        )


async def resync_created_public_meme_indexes_in_session(
    session: AsyncSession,
    *,
    settings: Settings,
    meme_file_id: uuid.UUID,
    qdrant_sync_client: QdrantSyncClientProtocol,
    meili_client: MeilisearchSyncClientProtocol,
) -> None:
    loaded_index_state = await load_search_index_state(
        session,
        meme_file_id,
        vector_dimensions=settings.pipeline_voyage_output_dimensions,
    )
    canonical = loaded_index_state.canonical
    if loaded_index_state.vector is None:
        raise E2ESeedError(f"Created meme file {meme_file_id} has no embedding vector for Qdrant re-sync.")
    if not canonical.is_public:
        raise E2ESeedError(f"Created meme {canonical.meme_id} is not public after crawler promotion.")
    if canonical.seo_page_slug is None:
        raise E2ESeedError(f"Created meme {canonical.meme_id} has no SEO slug after crawler promotion.")

    qdrant_payload = build_qdrant_sync_payload(canonical)
    meili_document = build_meilisearch_document(canonical)
    await qdrant_sync_client.upsert_meme_point(qdrant_payload, loaded_index_state.vector)
    await meili_client.upsert_document(meili_document)

    now = utcnow()
    await _upsert_sync_snapshot(
        session,
        meme_file_id=meme_file_id,
        target=SyncTargetKind.QDRANT,
        preview={
            "meme_id": str(qdrant_payload.meme_id),
            "search_index_algorithm_version": qdrant_payload.search_index_algorithm_version,
            "is_public": qdrant_payload.is_public,
            "tags": list(qdrant_payload.tags),
        },
        now=now,
    )
    await _upsert_sync_snapshot(
        session,
        meme_file_id=meme_file_id,
        target=SyncTargetKind.MEILISEARCH,
        preview={
            "id": meili_document.id,
            "search_index_algorithm_version": meili_document.search_index_algorithm_version,
            "is_public": meili_document.is_public,
            "ocr_text": meili_document.ocr_text or "",
        },
        now=now,
    )
    await session.commit()


def wait_for_public_search_contains(
    client: PipelineApiClient,
    *,
    query: str,
    meme_id: uuid.UUID,
    deadline: MonotonicDeadline,
) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    while not deadline.expired():
        payload = client.public_search(query, deadline=deadline)
        last_payload = payload
        hit_ids = _public_search_hit_ids(payload)
        if str(meme_id) in hit_ids:
            return payload
        deadline.sleep_for(POLL_INTERVAL_SECONDS)

    hit_ids = _public_search_hit_ids(last_payload) if last_payload is not None else []
    raise E2ESeedError(
        f"Public search did not include created meme {meme_id} after crawler-promotion re-sync; hits={hit_ids}",
    )


def _build_audio_profile_media_fixtures(
    settings: Settings,
) -> tuple[tuple[str, bytes, bool, float], ...]:
    """Generate tiny real WebM inputs that exercise Opus audio and silent FPS capping."""

    with tempfile.TemporaryDirectory(prefix="memexpert-e2e-media-") as temp_dir:
        temp_path = Path(temp_dir)
        audible_path = temp_path / "audible-webm-opus.webm"
        silent_path = temp_path / "silent-webm-60fps.webm"
        common = (
            settings.pipeline_ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        )
        _run_e2e_media_command(
            (
                *common,
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=24:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=2",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libvpx-vp9",
                "-deadline",
                "realtime",
                "-cpu-used",
                "8",
                "-b:v",
                "500k",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "libopus",
                "-b:a",
                "96k",
                "-shortest",
                str(audible_path),
            )
        )
        _run_e2e_media_command(
            (
                *common,
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=360x640:rate=60:duration=2",
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "libvpx-vp9",
                "-deadline",
                "realtime",
                "-cpu-used",
                "8",
                "-b:v",
                "500k",
                "-pix_fmt",
                "yuv420p",
                str(silent_path),
            )
        )
        return (
            ("audible-webm-opus", audible_path.read_bytes(), True, 24.0),
            ("silent-webm-60fps", silent_path.read_bytes(), False, 30.0),
        )


def _run_e2e_media_command(args: tuple[str, ...]) -> None:
    try:
        result = subprocess.run(args, capture_output=True, check=False, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise E2ESeedError(f"Could not generate the real moving-media E2E fixture: {type(exc).__name__}.") from exc
    if result.returncode != 0:
        error = " ".join(result.stderr.decode("utf-8", errors="replace").split())[:1000]
        raise E2ESeedError(f"FFmpeg could not generate a moving-media E2E fixture: {error}")


async def _prove_audio_safe_derivative(
    *,
    settings: Settings,
    session_factory: AsyncSessionFactory,
    s3_client: Any,
    meme_file_id: uuid.UUID,
    fixture_name: str,
    expected_audio: bool,
    expected_frame_rate: float,
) -> dict[str, object]:
    """Download and independently probe an activated derivative from the real stack."""

    async with session_factory() as session:
        meme_file = await session.get(MemeFile, meme_file_id)
        if meme_file is None:
            raise E2ESeedError(f"Moving-media fixture {fixture_name} lost its canonical file row.")
        if meme_file.s3_web_video_key is None or meme_file.active_media_generation_id is None:
            raise E2ESeedError(f"Moving-media fixture {fixture_name} did not activate an immutable derivative.")
        generation = await session.get(MediaGeneration, meme_file.active_media_generation_id)
        if generation is None or generation.status is not MediaGenerationStatus.ACTIVE:
            raise E2ESeedError(f"Moving-media fixture {fixture_name} has no active generation ledger row.")
        if generation.web_video_object_key != meme_file.s3_web_video_key:
            raise E2ESeedError(f"Moving-media fixture {fixture_name} has a mismatched active object pointer.")
        if (
            generation.profile != WEB_VIDEO_PROFILE_ID
            or generation.verified_at is None
            or meme_file.web_video_profile != WEB_VIDEO_PROFILE_ID
            or meme_file.web_video_verified_at is None
        ):
            raise E2ESeedError(f"Moving-media fixture {fixture_name} does not use the verified v2 profile.")
        if (
            generation.source_has_audio is not expected_audio
            or generation.output_has_audio is not expected_audio
            or meme_file.source_has_audio is not expected_audio
            or meme_file.web_video_has_audio is not expected_audio
        ):
            raise E2ESeedError(f"Moving-media fixture {fixture_name} has inconsistent persisted audio state.")
        web_video_key = meme_file.s3_web_video_key
        preview_key = derive_preview_image_object_key(
            web_video_key,
            meme_file_id=meme_file.id,
            settings=settings,
        )
        if preview_key != generation.preview_image_object_key:
            raise E2ESeedError(f"Moving-media fixture {fixture_name} points at a mismatched poster generation.")

    storage = get_pipeline_storage_settings(settings)
    web_video_bytes, preview_bytes = await asyncio.gather(
        asyncio.to_thread(_download_e2e_object, s3_client, storage.bucket, web_video_key),
        asyncio.to_thread(_download_e2e_object, s3_client, storage.bucket, preview_key),
    )
    if not preview_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise E2ESeedError(f"Moving-media fixture {fixture_name} did not download a valid PNG poster.")
    observations = await asyncio.to_thread(_probe_downloaded_e2e_video, settings, web_video_bytes)
    streams = observations.get("streams")
    media_format = observations.get("format")
    if not isinstance(streams, list) or not isinstance(media_format, dict):
        raise E2ESeedError(f"FFprobe returned malformed output for moving-media fixture {fixture_name}.")
    videos = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != int(expected_audio):
        raise E2ESeedError(f"Moving-media fixture {fixture_name} has the wrong output stream counts.")
    if len(streams) != len(videos) + len(audios):
        raise E2ESeedError(f"Moving-media fixture {fixture_name} contains an unexpected stream type.")
    video = videos[0]
    format_names = {value.strip() for value in str(media_format.get("format_name", "")).split(",")}
    if "mp4" not in format_names and "mov" not in format_names:
        raise E2ESeedError(f"Moving-media fixture {fixture_name} did not download an MP4 container.")
    if (
        video.get("codec_name") != "h264"
        or str(video.get("profile", "")).casefold() != "high"
        or video.get("pix_fmt") != "yuv420p"
        or _probe_non_negative_int(video.get("level")) != 41
    ):
        raise E2ESeedError(f"Moving-media fixture {fixture_name} violates the H.264 playback profile.")
    width = _probe_non_negative_int(video.get("width"))
    height = _probe_non_negative_int(video.get("height"))
    if (
        width is None
        or height is None
        or width < 2
        or height < 2
        or width % 2
        or height % 2
        or not _within_mobile_video_envelope(width, height)
    ):
        raise E2ESeedError(f"Moving-media fixture {fixture_name} violates the even 1080p envelope.")
    frame_rate = _positive_probe_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if frame_rate is None or frame_rate > 30.01 or abs(frame_rate - expected_frame_rate) > 0.05:
        raise E2ESeedError(f"Moving-media fixture {fixture_name} produced unexpected frame rate {frame_rate!r}.")
    bit_rate = _probe_non_negative_int(video.get("bit_rate"))
    if bit_rate is None or bit_rate <= 0 or bit_rate > 6_300_000:
        raise E2ESeedError(f"Moving-media fixture {fixture_name} violates the 6 Mbps video-rate profile.")
    if audios:
        audio = audios[0]
        normalized_audio_profile = str(audio.get("profile", "")).replace("AAC", "").strip().casefold()
        if (
            audio.get("codec_name") != "aac"
            or normalized_audio_profile not in {"lc", "low complexity"}
            or _probe_non_negative_int(audio.get("sample_rate")) != 48_000
            or _probe_non_negative_int(audio.get("channels")) != 2
        ):
            raise E2ESeedError(f"Moving-media fixture {fixture_name} violates the AAC-LC audio profile.")
    return {
        "fixture": fixture_name,
        "meme_file_id": str(meme_file_id),
        "profile": WEB_VIDEO_PROFILE_ID,
        "downloaded_byte_size": len(web_video_bytes),
        "poster_byte_size": len(preview_bytes),
        "width": width,
        "height": height,
        "frame_rate": frame_rate,
        "video_bit_rate": bit_rate,
        "video_codec": "h264",
        "audio_codec": "aac" if expected_audio else None,
        "source_has_audio": expected_audio,
        "web_video_has_audio": expected_audio,
    }


def _download_e2e_object(client: Any, bucket: str, key: str) -> bytes:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            payload = body.read()
        finally:
            body.close()
    except Exception as exc:  # noqa: BLE001 - render one safe fixture-level failure.
        raise E2ESeedError(f"Could not download an activated E2E media artifact: {type(exc).__name__}.") from exc
    if not isinstance(payload, bytes) or not payload:
        raise E2ESeedError("An activated E2E media artifact was empty.")
    return payload


def _probe_downloaded_e2e_video(settings: Settings, payload: bytes) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="memexpert-e2e-probe-") as temp_dir:
        path = Path(temp_dir) / "downloaded.mp4"
        path.write_bytes(payload)
        args = (
            settings.pipeline_ffprobe_binary,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        )
        try:
            result = subprocess.run(args, capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise E2ESeedError(f"Could not probe the downloaded E2E derivative: {type(exc).__name__}.") from exc
    if result.returncode != 0:
        error = " ".join(result.stderr.decode("utf-8", errors="replace").split())[:1000]
        raise E2ESeedError(f"FFprobe rejected the downloaded E2E derivative: {error}")
    try:
        decoded = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2ESeedError("FFprobe returned malformed JSON for the downloaded E2E derivative.") from exc
    if not isinstance(decoded, dict):
        raise E2ESeedError("FFprobe returned malformed metadata for the downloaded E2E derivative.")
    return decoded


def _positive_probe_rate(value: object) -> float | None:
    try:
        rate = Fraction(str(value))
    except ValueError, ZeroDivisionError:
        return None
    return float(rate) if rate > 0 else None


def _probe_non_negative_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _within_mobile_video_envelope(width: int, height: int) -> bool:
    if width > height:
        return width <= 1920 and height <= 1080
    if height > width:
        return width <= 1080 and height <= 1920
    return width <= 1080 and height <= 1080


async def wait_for_ingest_materialized_meme_file(
    client: PipelineApiClient,
    *,
    ingest_request_id: uuid.UUID,
    settings: Settings,
    session_factory: AsyncSessionFactory,
    deadline: MonotonicDeadline,
) -> tuple[IngestRequestRead, uuid.UUID]:
    last_request: IngestRequestRead | None = None
    last_outbox_result: RabbitMQOutboxPublisherBatchResult | None = None
    while not deadline.expired():
        last_outbox_result = await publish_pending_rabbitmq_outbox(
            settings=settings,
            session_factory=session_factory,
        )
        ingest_request = client.get_ingest_request(ingest_request_id, deadline=deadline)
        last_request = ingest_request
        if ingest_request.status in TERMINAL_INGEST_FAILURE_STATUSES:
            raise E2ESeedError(
                f"Ingest request {ingest_request_id} reached terminal failure status "
                f"{ingest_request.status.value}: {ingest_request.failure_code} - {ingest_request.failure_detail}",
            )
        if ingest_request.materialized_meme_file_id is not None:
            return ingest_request, ingest_request.materialized_meme_file_id
        await _sleep_until_deadline(deadline, POLL_INTERVAL_SECONDS)

    snapshot = last_request.model_dump(mode="json") if last_request is not None else None
    raise E2ESeedError(
        f"Timed out waiting for ingest request {ingest_request_id} to materialize a meme file. "
        f"Last request: {snapshot}; last outbox publish: {_outbox_result_snapshot(last_outbox_result)}",
    )


async def wait_for_dual_synced(
    client: PipelineApiClient,
    *,
    meme_file_id: uuid.UUID,
    settings: Settings,
    session_factory: AsyncSessionFactory,
    deadline: MonotonicDeadline,
) -> ContentPipelineItemDetail:
    last_detail: ContentPipelineItemDetail | None = None
    last_outbox_result: RabbitMQOutboxPublisherBatchResult | None = None
    while not deadline.expired():
        last_outbox_result = await publish_pending_rabbitmq_outbox(
            settings=settings,
            session_factory=session_factory,
        )
        detail = client.get_item_detail(meme_file_id, deadline=deadline)
        last_detail = detail
        qdrant_status = detail.sync_targets.get(SyncTargetKind.QDRANT)
        meili_status = detail.sync_targets.get(SyncTargetKind.MEILISEARCH)
        if (
            qdrant_status is not None
            and meili_status is not None
            and qdrant_status.status is SyncTargetStatus.SYNCED
            and meili_status.status is SyncTargetStatus.SYNCED
        ):
            return detail
        await _sleep_until_deadline(deadline, POLL_INTERVAL_SECONDS)
    snapshot = last_detail.model_dump(mode="json") if last_detail is not None else None
    raise E2ESeedError(
        f"Timed out waiting for {meme_file_id} to dual-sync. Last detail: {snapshot}; "
        f"last outbox publish: {_outbox_result_snapshot(last_outbox_result)}",
    )


async def publish_pending_rabbitmq_outbox(
    *,
    settings: Settings,
    session_factory: AsyncSessionFactory,
) -> RabbitMQOutboxPublisherBatchResult:
    return await run_rabbitmq_outbox_publisher_batch(session_factory, settings=settings)


async def _sleep_until_deadline(deadline: MonotonicDeadline, seconds: float) -> None:
    duration = min(max(0.0, seconds), deadline.remaining())
    if duration > 0:
        await asyncio.sleep(duration)


def _outbox_result_snapshot(result: RabbitMQOutboxPublisherBatchResult | None) -> dict[str, int | float] | None:
    if result is None:
        return None
    return {
        "recovered": result.recovered,
        "claimed": result.claimed,
        "published": result.published,
        "failed": result.failed,
        "duration_seconds": result.duration_seconds,
    }


def validate_provider_policy(settings: Settings) -> None:
    provider_modes = {
        "ocr": settings.pipeline_ocr_provider_mode,
        "voyage": settings.pipeline_voyage_provider_mode,
        "classification": settings.pipeline_classification_provider_mode,
    }
    live_modes = {name: mode for name, mode in provider_modes.items() if mode != "fake"}
    if live_modes:
        raise E2ESeedError(
            "The E2E seed path requires fake provider modes; set "
            "PIPELINE_OCR_PROVIDER_MODE=fake, PIPELINE_VOYAGE_PROVIDER_MODE=fake, "
            "and PIPELINE_CLASSIFICATION_PROVIDER_MODE=fake. "
            f"Current modes: {provider_modes}",
        )


async def wait_for_meili_hits(
    meili_client: PipelineMeilisearchSyncClient,
    *,
    specs: list[SeedSpec],
    deadline: MonotonicDeadline,
) -> None:
    while not deadline.expired():
        missing: list[str] = []
        for spec in specs:
            hits = await meili_client.search(spec.query, limit=10)
            expected_document_id = _stable_uuid(f"{spec.category}:file").hex
            if not any(hit.get("id") == expected_document_id for hit in hits if isinstance(hit, dict)):
                missing.append(spec.category)
        if not missing:
            return
        await _sleep_until_deadline(deadline, 0.5)
    raise E2ESeedError("Timed out waiting for seeded Meilisearch documents to become searchable.")


def assert_created_is_distinct(*, created_meme_id: uuid.UUID, seeded: list[SeededMeme]) -> None:
    seeded_ids = [item.meme_id for item in seeded]
    if len(seeded_ids) != len(set(seeded_ids)):
        raise E2ESeedError(f"Seeded corpus contains duplicate meme ids: {seeded_ids}")
    if created_meme_id in seeded_ids:
        raise E2ESeedError(
            f"Created meme {created_meme_id} must be distinct from seeded corpus ids {seeded_ids}.",
        )


def prove_seeded_public_corpus(
    client: PipelineApiClient,
    *,
    seeded: list[SeededMeme],
    deadline: MonotonicDeadline,
) -> dict[str, dict[str, object]]:
    proof: dict[str, dict[str, object]] = {}
    for item in seeded:
        if item.is_nsfw:
            default_search = client.public_search(item.query, deadline=deadline)
            requested_search = client.public_search(item.query, deadline=deadline, include_nsfw=True)
            assert_public_search_excludes(default_search, meme_id=item.meme_id, label=f"seeded {item.category}")
            assert_public_search_excludes(requested_search, meme_id=item.meme_id, label=f"seeded {item.category}")
            assert_public_detail_hidden(client, slug=item.slug, meme_id=item.meme_id, deadline=deadline)
            assert_public_detail_hidden(
                client,
                slug=item.slug,
                meme_id=item.meme_id,
                deadline=deadline,
                include_nsfw=True,
            )
            proof[item.slug] = {
                "positive_detail_id": None,
                "anonymous_search_hidden_by_default": True,
                "anonymous_search_hidden_with_include_nsfw": True,
                "anonymous_detail_hidden_by_default": True,
                "anonymous_detail_hidden_with_include_nsfw": True,
            }
            continue

        search_payload = client.public_search(item.query, deadline=deadline)
        assert_public_search_contains(search_payload, meme_id=item.meme_id, label=f"seeded {item.category}")
        detail_payload = assert_public_detail_resolves(
            client,
            slug=item.slug,
            meme_id=item.meme_id,
            deadline=deadline,
        )
        proof[item.slug] = {"positive_detail_id": detail_payload.get("id")}
    return proof


def prove_seeded_public_trends(
    client: PipelineApiClient,
    *,
    seeded: list[SeededMeme],
    deadline: MonotonicDeadline,
) -> dict[str, object]:
    representative = _require_seeded_category(seeded, "cat")

    trend_page = client.public_trend_page(deadline=deadline, limit=20)
    trend_ids = [item.get("meme", {}).get("id") for item in trend_page.get("items", []) if isinstance(item, dict)]
    if str(representative.meme_id) not in trend_ids:
        raise E2ESeedError(
            f"Public trends page did not include seeded representative meme {representative.meme_id}; "
            f"trend_ids={trend_ids}",
        )

    tag_summary = _assert_public_trend_summary(
        _find_public_trend_summary(
            client.public_tag_trend_summaries(deadline=deadline, limit=20),
            slug=E2E_PUBLIC_TRENDS_TAG_SLUG,
            label="tag summaries",
        ),
        slug=E2E_PUBLIC_TRENDS_TAG_SLUG,
        title=_public_tag_title(E2E_PUBLIC_TRENDS_TAG_SLUG),
    )
    template_summary = _assert_public_trend_summary(
        _find_public_trend_summary(
            client.public_template_trend_summaries(deadline=deadline, limit=20),
            slug=E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
            label="template summaries",
        ),
        slug=E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
        title=f"{E2E_PUBLIC_TRENDS_TEMPLATE_NAME} memes",
    )

    tag_landing = client.public_tag_landing(E2E_PUBLIC_TRENDS_TAG_SLUG, deadline=deadline)
    _assert_landing_trend_summary(tag_landing, slug=E2E_PUBLIC_TRENDS_TAG_SLUG, label="tag landing")
    template_landing = client.public_template_landing(E2E_PUBLIC_TRENDS_TEMPLATE_SLUG, deadline=deadline)
    _assert_landing_trend_summary(
        template_landing,
        slug=E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
        label="template landing",
    )

    comparison_items = [
        f"meme:{representative.slug}",
        f"tag:{E2E_PUBLIC_TRENDS_TAG_SLUG}",
        f"template:{E2E_PUBLIC_TRENDS_TEMPLATE_SLUG}",
    ]
    comparison = client.public_trend_comparison(comparison_items, deadline=deadline)
    comparison_proof = _assert_public_trend_comparison(comparison, representative=representative)

    timeline = client.public_trend_timeline(
        deadline=deadline,
        granularity=E2E_PUBLIC_TRENDS_TIMELINE_GRANULARITY,
        limit=12,
    )
    timeline_proof = _assert_public_trend_timeline(timeline, representative=representative)

    return {
        "trend_hit_ids": trend_ids,
        "tag_points": len(tag_summary["points"]),
        "template_points": len(template_summary["points"]),
        "comparison": comparison_proof,
        "timeline": timeline_proof,
    }


def _find_public_trend_summary(
    summaries: list[dict[str, Any]],
    *,
    slug: str,
    label: str,
) -> dict[str, Any]:
    for summary in summaries:
        if summary.get("slug") == slug:
            return summary
    raise E2ESeedError(f"Public trend {label} did not include slug {slug!r}: {summaries}")


def _assert_public_trend_summary(summary: dict[str, Any], *, slug: str, title: str) -> dict[str, Any]:
    if summary.get("slug") != slug:
        raise E2ESeedError(f"Public trend summary resolved slug {summary.get('slug')!r}, expected {slug!r}.")
    if summary.get("title") != title:
        raise E2ESeedError(f"Public trend summary {slug!r} title {summary.get('title')!r}, expected {title!r}.")
    points = summary.get("points")
    if not isinstance(points, list) or len(points) < 2:
        raise E2ESeedError(f"Public trend summary {slug!r} must expose at least two real points: {summary}")
    if summary.get("insufficient_history") is not False:
        raise E2ESeedError(f"Public trend summary {slug!r} unexpectedly marked insufficient_history: {summary}")
    _assert_expected_public_trend_points(points, label=f"summary {slug}")
    return summary


def _assert_landing_trend_summary(landing: dict[str, Any], *, slug: str, label: str) -> None:
    trend_summary = landing.get("trend_summary")
    if not isinstance(trend_summary, dict):
        raise E2ESeedError(f"Public {label} did not include trend_summary: {landing}")
    _assert_public_trend_summary(trend_summary, slug=slug, title=str(landing.get("title")))


def _assert_expected_public_trend_points(points: list[Any], *, label: str) -> None:
    expected_points = build_public_trend_aggregate_history_points_payload()
    points_by_day = {str(point.get("observed_at", ""))[:10]: point for point in points if isinstance(point, dict)}
    for expected in expected_points:
        observed_day = str(expected["observed_at"])[:10]
        point = points_by_day.get(observed_day)
        if point is None:
            raise E2ESeedError(f"Public trend {label} missing expected point day {observed_day}: {points}")
        for key in ("value", "meme_count", "snapshot_count", "source_views", "platform_views", "platform_likes"):
            actual_value = round(float(point.get(key) or 0.0), 1) if key == "value" else point.get(key)
            if actual_value != expected[key]:
                raise E2ESeedError(
                    f"Public trend {label} point {observed_day} returned {key}={point.get(key)!r}, "
                    f"expected {expected[key]!r}; point={point}",
                )


def _assert_public_trend_comparison(
    comparison: dict[str, Any],
    *,
    representative: SeededMeme,
) -> dict[str, object]:
    items = comparison.get("items")
    if not isinstance(items, list):
        raise E2ESeedError(f"Public trend comparison returned malformed items: {comparison}")
    series_by_kind = {
        item.get("kind"): item
        for item in items
        if isinstance(item, dict) and item.get("kind") in {"meme", "tag", "template"}
    }
    for kind in ("meme", "tag", "template"):
        if kind not in series_by_kind:
            raise E2ESeedError(f"Public trend comparison missing {kind} series: {comparison}")

    meme_series = series_by_kind["meme"]
    if meme_series.get("title") != representative.title:
        raise E2ESeedError(
            f"Public trend comparison meme title {meme_series.get('title')!r}, expected {representative.title!r}.",
        )
    if meme_series.get("value") != representative.slug:
        raise E2ESeedError(
            f"Public trend comparison meme value {meme_series.get('value')!r}, expected {representative.slug!r}.",
        )
    meme_points = meme_series.get("points")
    if not isinstance(meme_points, list):
        raise E2ESeedError(f"Public trend comparison meme series has malformed points: {comparison}")
    meme_points_by_day = {
        str(point.get("observed_at", ""))[:10]: point for point in meme_points if isinstance(point, dict)
    }
    for observed_at, expected_metrics in _public_trend_daily_metrics(representative.category).items():
        observed_day = observed_at.date().isoformat()
        point = meme_points_by_day.get(observed_day)
        if point is None:
            raise E2ESeedError(
                f"Public trend comparison meme series missing expected point day {observed_day}: {comparison}",
            )
        if point.get("meme_count") != 1:
            raise E2ESeedError(
                f"Public trend comparison meme point {observed_day} returned "
                f"meme_count={point.get('meme_count')!r}, expected 1.",
            )
        for key, expected_value in expected_metrics.items():
            if point.get(key) != expected_value:
                raise E2ESeedError(
                    f"Public trend comparison meme point {observed_day} returned {key}={point.get(key)!r}, "
                    f"expected {expected_value!r}; point={point}",
                )
    for kind in ("meme", "tag", "template"):
        points = series_by_kind[kind].get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise E2ESeedError(f"Public trend comparison {kind} series lacks real history points: {comparison}")
        if series_by_kind[kind].get("insufficient_history") is not False:
            raise E2ESeedError(f"Public trend comparison {kind} series marked insufficient history: {comparison}")
    return {"series_count": len(items), "requested_items": comparison.get("requested_items", [])}


def _assert_public_trend_timeline(
    timeline: dict[str, Any],
    *,
    representative: SeededMeme,
) -> dict[str, object]:
    periods = timeline.get("periods")
    if not isinstance(periods, list):
        raise E2ESeedError(f"Public trend timeline returned malformed periods: {timeline}")
    period = next(
        (
            item
            for item in periods
            if isinstance(item, dict) and item.get("period") == E2E_PUBLIC_TRENDS_TIMELINE_PERIOD
        ),
        None,
    )
    if period is None:
        raise E2ESeedError(f"Public trend timeline missing period {E2E_PUBLIC_TRENDS_TIMELINE_PERIOD}: {timeline}")
    expected_snapshot_count = sum(
        int(point["snapshot_count"]) for point in build_public_trend_aggregate_history_points_payload()
    )
    if period.get("snapshot_count") != expected_snapshot_count:
        raise E2ESeedError(
            f"Public trend timeline period {E2E_PUBLIC_TRENDS_TIMELINE_PERIOD} snapshot_count="
            f"{period.get('snapshot_count')!r}, expected {expected_snapshot_count}.",
        )
    top_memes = period.get("top_memes")
    if not isinstance(top_memes, list) or not top_memes:
        raise E2ESeedError(f"Public trend timeline period has no top memes: {period}")
    first_meme = top_memes[0].get("meme", {}) if isinstance(top_memes[0], dict) else {}
    if first_meme.get("id") != str(representative.meme_id):
        raise E2ESeedError(
            f"Public trend timeline top meme {first_meme.get('id')!r}, expected {representative.meme_id}.",
        )
    return {"period": period.get("period"), "snapshot_count": period.get("snapshot_count")}


def assert_public_detail_resolves(
    client: PipelineApiClient,
    *,
    slug: str,
    meme_id: uuid.UUID,
    deadline: MonotonicDeadline,
    include_nsfw: bool = False,
) -> dict[str, Any]:
    detail_payload = client.public_detail_by_slug(slug, deadline=deadline, include_nsfw=include_nsfw)
    if detail_payload.get("id") != str(meme_id):
        raise E2ESeedError(
            f"Public slug detail {slug!r} resolved {detail_payload.get('id')}, expected {meme_id}.",
        )
    if detail_payload.get("seo_page_slug") != slug:
        raise E2ESeedError(
            f"Public slug detail {slug!r} returned seo_page_slug={detail_payload.get('seo_page_slug')!r}.",
        )
    return detail_payload


def assert_public_detail_hidden(
    client: PipelineApiClient,
    *,
    slug: str,
    meme_id: uuid.UUID,
    deadline: MonotonicDeadline,
    include_nsfw: bool = False,
) -> None:
    status_code, payload = client.public_detail_by_slug_status(
        slug,
        deadline=deadline,
        include_nsfw=include_nsfw,
    )
    if status_code == 404:
        return
    if status_code == 200 and payload.get("id") != str(meme_id):
        return
    raise E2ESeedError(
        f"Anonymous public detail for NSFW slug {slug!r} should be hidden; status={status_code}, payload={payload}",
    )


def assert_public_search_contains(payload: dict[str, Any], *, meme_id: uuid.UUID, label: str = "created meme") -> None:
    hit_ids = _public_search_hit_ids(payload)
    if str(meme_id) not in hit_ids:
        raise E2ESeedError(f"Public search did not include {label} {meme_id}; hits={hit_ids}")


def assert_public_search_excludes(payload: dict[str, Any], *, meme_id: uuid.UUID, label: str) -> None:
    hit_ids = _public_search_hit_ids(payload)
    if str(meme_id) in hit_ids:
        raise E2ESeedError(f"Anonymous public search exposed hidden {label} {meme_id}; hits={hit_ids}")


def _public_search_hit_ids(payload: dict[str, Any]) -> list[object]:
    return [item.get("meme", {}).get("id") for item in payload.get("items", []) if isinstance(item, dict)]


def build_seed_png_bytes(spec: SeedSpec) -> bytes:
    return build_png_bytes(spec.color, metadata_text=f"seed:{spec.category}:{spec.slug}")


def build_png_bytes(color: tuple[int, int, int], *, metadata_text: str | None = None) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", (64, 64), color=color)
    pnginfo = None
    if metadata_text is not None:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("memexpert-e2e", metadata_text)
    image.save(output, format="PNG", pnginfo=pnginfo)
    return output.getvalue()


def _build_succeeded_stage_rows(
    *,
    meme_file_id: uuid.UUID,
    event_id: uuid.UUID,
    now: datetime,
) -> list[PipelineStageJournal]:
    return [
        PipelineStageJournal(
            meme_file_id=meme_file_id,
            stage=stage,
            status=ContentPipelineStageStatus.SUCCEEDED,
            attempt_count=1,
            last_event_id=event_id,
            is_retryable=False,
            started_at=now,
            finished_at=now,
        )
        for stage in ContentPipelineStage
    ]


def _build_sync_snapshot(
    *,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
    preview: dict[str, object],
    now: datetime,
) -> MemeFileSyncTargetSnapshot:
    return MemeFileSyncTargetSnapshot(
        meme_file_id=meme_file_id,
        sync_target=target,
        status=SyncTargetStatus.SYNCED,
        last_event_id=uuid.uuid7(),
        last_payload_preview=preview,
        last_success_at=now,
        last_attempt_at=now,
        attempt_count=1,
    )


async def _upsert_sync_snapshot(
    session: AsyncSession,
    *,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
    preview: dict[str, object],
    now: datetime,
) -> None:
    existing = await session.scalar(
        select(MemeFileSyncTargetSnapshot).where(
            MemeFileSyncTargetSnapshot.meme_file_id == meme_file_id,
            MemeFileSyncTargetSnapshot.sync_target == target,
        ),
    )
    if existing is None:
        session.add(_build_sync_snapshot(meme_file_id=meme_file_id, target=target, preview=preview, now=now))
        await session.flush()
        return

    existing.status = SyncTargetStatus.SYNCED
    existing.last_event_id = uuid.uuid7()
    existing.normalized_reason = None
    existing.last_error_text = None
    existing.last_payload_preview = preview
    existing.last_success_at = now
    existing.last_attempt_at = now
    existing.attempt_count = max(existing.attempt_count + 1, 1)
    await session.flush()


def _stable_uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(UUID_NAMESPACE, name)


def _validate_response[ModelT: BaseModel](
    response: httpx.Response,
    *,
    expected_status: int | tuple[int, ...],
    model: type[ModelT],
) -> ModelT:
    payload = _validate_json_response(response, expected_status=expected_status)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned a malformed payload: {exc}",
        ) from exc


def _validate_json_response(response: httpx.Response, *, expected_status: int | tuple[int, ...]) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned non-JSON output: "
            f"{exc}; body={response.text!r}",
        ) from exc

    expected_statuses = (expected_status,) if isinstance(expected_status, int) else expected_status
    if response.status_code not in expected_statuses:
        try:
            error_payload = ContentPipelineErrorResponse.model_validate(payload)
        except ValidationError:
            rendered = json.dumps(payload, sort_keys=True) if isinstance(payload, dict | list) else repr(payload)
            raise E2ESeedError(
                f"{response.request.method} {response.request.url.path} failed with HTTP "
                f"{response.status_code} and malformed payload: {rendered}",
            ) from None
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} failed with HTTP "
            f"{response.status_code}: {error_payload.code.value} - {error_payload.detail}",
        )

    if not isinstance(payload, dict):
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned non-object JSON: {type(payload).__name__}",
        )
    return payload


def _validate_json_list_response(
    response: httpx.Response,
    *,
    expected_status: int | tuple[int, ...],
) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned non-JSON output: "
            f"{exc}; body={response.text!r}",
        ) from exc

    expected_statuses = (expected_status,) if isinstance(expected_status, int) else expected_status
    if response.status_code not in expected_statuses:
        rendered = json.dumps(payload, sort_keys=True) if isinstance(payload, dict | list) else repr(payload)
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} failed with HTTP "
            f"{response.status_code}: {rendered}",
        )
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned malformed JSON list: "
            f"{type(payload).__name__}",
        )
    return payload


def _extract_qdrant_vector_size(info: object) -> int | None:
    config = getattr(info, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    if isinstance(vectors, VectorParams):
        return int(vectors.size)
    if isinstance(vectors, dict):
        default_vector = vectors.get("") or vectors.get("default")
        if isinstance(default_vector, VectorParams):
            return int(default_vector.size)
        if isinstance(default_vector, dict):
            size_value = default_vector.get("size")
            if isinstance(size_value, int):
                return size_value
    size = getattr(vectors, "size", None)
    if isinstance(size, int):
        return size
    return None


if __name__ == "__main__":
    sys.exit(main())
