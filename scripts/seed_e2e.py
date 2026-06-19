#!/usr/bin/env python3
"""Seed and prove the deterministic containerized PRD E2E corpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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
from sqlalchemy import delete, or_, select, update

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_engine, get_async_session_factory
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantSyncClient
from memexpert.core.storage import get_pipeline_storage_settings, get_s3_client
from memexpert.core.voyage import build_pipeline_voyage_client
from memexpert.ingest.schemas import IngestRequestRead
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionInvite, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemePopularitySnapshot,
    MemeSeoPage,
    MemeSource,
    MemeTemplate,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    AccountStatus,
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
    PipelineIngestRequestStatus,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.user import User
from memexpert.pipeline.outbox_runtime import PipelineOutboxPublisherBatchResult, run_pipeline_outbox_publisher_batch
from memexpert.schemas.content_pipeline import (
    ContentPipelineErrorResponse,
    ContentPipelineItemDetail,
    SmokeProofResult,
)
from memexpert.services.public_trends import refresh_public_trend_materialized_views
from memexpert.services.search_index_sync import (
    build_meilisearch_document,
    build_qdrant_sync_payload,
    load_search_index_state,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.core.meilisearch import MeilisearchSyncClientProtocol
    from memexpert.core.qdrant import QdrantSyncClientProtocol

DEFAULT_API_BASE_URL: Final = "http://api:8000"
DEFAULT_ARTIFACTS_DIR: Final = Path("/artifacts")
DEFAULT_TIMEOUT_SECONDS: Final = 180.0
DEFAULT_API_TIMEOUT_SECONDS: Final = 20.0
POLL_INTERVAL_SECONDS: Final = 1.0
E2E_SOURCE_ID: Final = "e2e-prd-seed"
E2E_UPLOAD_SOURCE_ID: Final = "e2e-prd-upload"
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
    popularity_score: float


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
            popularity_score=40.0,
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
            popularity_score=80.5,
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
            popularity_score=30.0,
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
            popularity_score=55.5,
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
            popularity_score=20.0,
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
            popularity_score=45.5,
        ),
    ),
}


class PipelineApiClient:
    """Typed HTTP client wrapper for the operator and public proof routes."""

    def __init__(self, *, base_url: str, operator_token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"X-Memexpert-Operator-Token": operator_token},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PipelineApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def healthcheck(self) -> None:
        response = self._client.get("/health")
        if response.status_code != 200:
            raise E2ESeedError(
                f"GET /health returned unexpected status {response.status_code}: {response.text!r}",
            )

    def upload_cat_png(self, *, image_bytes: bytes, run_id: str) -> IngestRequestRead:
        response = self._client.post(
            "/api/v1/pipeline/uploads",
            data={
                "source_platform": SourcePlatform.TELEGRAM.value,
                "source_id": E2E_UPLOAD_SOURCE_ID,
                "post_id": run_id,
                "views": "1",
            },
            files={"file": ("e2e-prd-cat.png", image_bytes, "image/png")},
        )
        return _validate_response(response, expected_status=(200, 202), model=IngestRequestRead)

    def get_ingest_request(self, ingest_request_id: uuid.UUID) -> IngestRequestRead:
        response = self._client.get(f"/api/v1/pipeline/ingest-requests/{ingest_request_id}")
        return _validate_response(response, expected_status=200, model=IngestRequestRead)

    def get_item_detail(self, meme_file_id: uuid.UUID) -> ContentPipelineItemDetail:
        response = self._client.get(f"/api/v1/pipeline/items/{meme_file_id}/detail")
        return _validate_response(response, expected_status=200, model=ContentPipelineItemDetail)

    def run_dual_index_proof(self, meme_file_id: uuid.UUID) -> SmokeProofResult:
        response = self._client.post(
            "/api/v1/pipeline/search/smoke",
            json={"meme_file_id": str(meme_file_id)},
        )
        return _validate_response(response, expected_status=200, model=SmokeProofResult)

    def public_search(self, query: str, *, include_nsfw: bool = False) -> dict[str, Any]:
        response = self._client.get(
            "/api/v1/memes/search",
            params={"query": query, "include_nsfw": str(include_nsfw).lower(), "limit": "10", "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)

    def public_detail_by_slug(self, slug: str, *, include_nsfw: bool = False) -> dict[str, Any]:
        response = self._client.get(
            f"/api/v1/memes/slug/{slug}",
            params={"include_nsfw": str(include_nsfw).lower()},
        )
        return _validate_json_response(response, expected_status=200)

    def public_detail_by_slug_status(self, slug: str, *, include_nsfw: bool = False) -> tuple[int, dict[str, Any]]:
        response = self._client.get(
            f"/api/v1/memes/slug/{slug}",
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

    def public_trend_page(self, *, limit: int = 20) -> dict[str, Any]:
        response = self._client.get("/api/v1/memes/trends", params={"limit": str(limit), "offset": "0"})
        return _validate_json_response(response, expected_status=200)

    def public_tag_trend_summaries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        response = self._client.get("/api/v1/memes/trends/tags", params={"limit": str(limit), "offset": "0"})
        return _validate_json_list_response(response, expected_status=200)

    def public_template_trend_summaries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        response = self._client.get("/api/v1/memes/trends/templates", params={"limit": str(limit), "offset": "0"})
        return _validate_json_list_response(response, expected_status=200)

    def public_trend_comparison(self, items: list[str]) -> dict[str, Any]:
        response = self._client.get("/api/v1/memes/trends/compare", params=[("item", item) for item in items])
        return _validate_json_response(response, expected_status=200)

    def public_trend_timeline(self, *, granularity: str = "month", limit: int = 12) -> dict[str, Any]:
        response = self._client.get(
            "/api/v1/memes/trends/timeline",
            params={"granularity": granularity, "limit": str(limit), "offset": "0"},
        )
        return _validate_json_response(response, expected_status=200)

    def public_tag_landing(self, tag_slug: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/memes/tags/{tag_slug}", params={"limit": "20", "offset": "0"})
        return _validate_json_response(response, expected_status=200)

    def public_template_landing(self, template_slug: str) -> dict[str, Any]:
        response = self._client.get(
            f"/api/v1/memes/templates/{template_slug}",
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
        help="Total bounded wait budget for API/worker proof polling (default: %(default)s).",
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

    cat_png = build_png_bytes((255, 0, 0))
    operator_token = settings.pipeline_operator_token.get_secret_value()
    with PipelineApiClient(
        base_url=args.api_base_url,
        operator_token=operator_token,
        timeout_seconds=args.api_timeout_seconds,
    ) as api_client:
        api_client.healthcheck()
        print("Uploading generated cat PNG through /api/v1/pipeline/uploads")
        ingest_request = api_client.upload_cat_png(image_bytes=cat_png, run_id=run_id)
        materialized_request, meme_file_id = await wait_for_ingest_materialized_meme_file(
            api_client,
            ingest_request_id=ingest_request.id,
            settings=settings,
            session_factory=session_factory,
            timeout_seconds=args.timeout_seconds,
        )
        detail = await wait_for_dual_synced(
            api_client,
            meme_file_id=meme_file_id,
            settings=settings,
            session_factory=session_factory,
            timeout_seconds=args.timeout_seconds,
        )
        print(f"Uploaded item dual-synced: ingest_request_id={ingest_request.id} meme_file_id={meme_file_id}")
        dual_index_result = wait_for_dual_index_proof(
            api_client,
            meme_file_id=meme_file_id,
            timeout_seconds=args.timeout_seconds,
        )
        slug = await publish_created_meme(
            settings=settings,
            meme_id=detail.meme_id,
            query="cat",
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
            timeout_seconds=args.timeout_seconds,
        )
        created_detail_payload = assert_public_detail_resolves(
            api_client,
            slug=slug,
            meme_id=detail.meme_id,
        )

        seeded = await seed_direct_corpus(
            settings=settings,
            s3_client=s3_client,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
            specs=specs,
        )
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
        created_search_payload = api_client.public_search("cat")
        assert_public_search_contains(created_search_payload, meme_id=detail.meme_id)
        seeded_proofs = prove_seeded_public_corpus(api_client, seeded=seeded)
        public_trends_proof = prove_seeded_public_trends(
            api_client,
            seeded=seeded,
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
            "slug": slug,
            "query": "cat",
            "title": "Created cat pipeline meme",
        },
        "proof": {
            "dual_index": dual_index_result.model_dump(mode="json"),
            "public_search_total": created_search_payload.get("total"),
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
                "Qdrant collection dimension mismatch: "
                f"{size} != {settings.pipeline_voyage_output_dimensions}",
            )
    except E2ESeedError:
        raise
    except Exception as exc:
        raise E2ESeedError(f"Unable to ensure Qdrant collection: {exc}") from exc
    finally:
        await client.close()


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
            meme = Meme(
                id=meme_id,
                media_type=spec.media_type,
                primary_file_id=meme_file_id,
                ocr_text=spec.ocr_text,
                language=spec.language,
                is_nsfw=spec.is_nsfw,
                is_public=True,
                popularity_score=_latest_public_trend_popularity_score(spec.category) or 10.0,
                tags=list(spec.tags),
                template_id=public_trends_template.id if _seed_spec_has_public_trends(spec) else None,
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
            session.add_all(
                [
                    MemeSource(
                        file_id=meme_file_id,
                        platform=SourcePlatform.TELEGRAM,
                        source_id=E2E_SOURCE_ID,
                        post_id=spec.category,
                        views=1,
                        reactions={},
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
                    *build_public_trend_snapshot_rows(meme_id=meme_id, category=spec.category),
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

    await wait_for_meili_hits(meili_client, specs=specs)
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


def build_public_trend_snapshot_rows(*, meme_id: uuid.UUID, category: str) -> list[MemePopularitySnapshot]:
    return [
        MemePopularitySnapshot(
            id=_stable_uuid(f"{category}:public-trend-snapshot:{index}"),
            meme_id=meme_id,
            captured_at=spec.captured_at,
            source_views=spec.source_views,
            source_reactions=spec.source_reactions,
            source_reposts=spec.source_reposts,
            platform_views=spec.platform_views,
            platform_sends=spec.platform_sends,
            platform_saves=spec.platform_saves,
            platform_likes=spec.platform_likes,
            popularity_score=spec.popularity_score,
        )
        for index, spec in enumerate(PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category, ()), start=1)
    ]


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
        for spec in PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY[category]:
            observed_at = _public_trend_observed_at(spec)
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
            totals["snapshot_count"] += 1
            totals["source_views"] += spec.source_views
            totals["source_reactions"] += spec.source_reactions
            totals["source_reposts"] += spec.source_reposts
            totals["platform_views"] += spec.platform_views
            totals["platform_sends"] += spec.platform_sends
            totals["platform_saves"] += spec.platform_saves
            totals["platform_likes"] += spec.platform_likes
            totals["value"] += spec.popularity_score

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


def _seed_spec_has_public_trends(spec: SeedSpec) -> bool:
    return not spec.is_nsfw and spec.category in PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY


def _latest_public_trend_popularity_score(category: str) -> float | None:
    specs = PUBLIC_TREND_SNAPSHOT_SPECS_BY_CATEGORY.get(category)
    if not specs:
        return None
    return max(specs, key=lambda spec: spec.captured_at).popularity_score


def _public_trend_observed_at(spec: PublicTrendSnapshotSpec) -> datetime:
    return datetime(spec.captured_at.year, spec.captured_at.month, spec.captured_at.day, tzinfo=UTC)


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
    await cleanup_collection_management_fixture_rows(session)

    source_result = await session.execute(
        select(MemeSource).where(MemeSource.source_id.in_([E2E_SOURCE_ID, E2E_UPLOAD_SOURCE_ID])),
    )
    meme_ids: set[uuid.UUID] = set()
    for source in source_result.scalars():
        meme_file = await session.get(MemeFile, source.file_id)
        if meme_file is not None:
            meme_ids.add(meme_file.meme_id)

    slug_result = await session.execute(
        select(MemeSeoPage).where(
            MemeSeoPage.slug.in_([spec.slug for spec in specs])
            | MemeSeoPage.slug.like("e2e-prd-created-%"),
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


async def publish_created_meme(*, settings: Settings, meme_id: uuid.UUID, query: str) -> str:
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        slug = await publish_created_meme_in_session(session, meme_id=meme_id, query=query)
        await session.commit()
    _ = settings
    return slug


async def publish_created_meme_in_session(session: AsyncSession, *, meme_id: uuid.UUID, query: str) -> str:
    slug = f"e2e-prd-created-{meme_id.hex[:12]}"
    meme = await session.get(Meme, meme_id)
    if meme is None:
        raise E2ESeedError(f"Created meme {meme_id} is missing from the database.")
    meme.is_public = True
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
        raise E2ESeedError(f"Created meme {canonical.meme_id} is not public after publish mutation.")
    if canonical.seo_page_slug is None:
        raise E2ESeedError(f"Created meme {canonical.meme_id} has no SEO slug after publish mutation.")

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
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = client.public_search(query)
        last_payload = payload
        hit_ids = _public_search_hit_ids(payload)
        if str(meme_id) in hit_ids:
            return payload
        time.sleep(POLL_INTERVAL_SECONDS)

    hit_ids = _public_search_hit_ids(last_payload) if last_payload is not None else []
    raise E2ESeedError(
        f"Public search did not include created meme {meme_id} after post-publish re-sync; hits={hit_ids}",
    )


async def wait_for_ingest_materialized_meme_file(
    client: PipelineApiClient,
    *,
    ingest_request_id: uuid.UUID,
    settings: Settings,
    session_factory: AsyncSessionFactory,
    timeout_seconds: float,
) -> tuple[IngestRequestRead, uuid.UUID]:
    deadline = time.monotonic() + timeout_seconds
    last_request: IngestRequestRead | None = None
    last_outbox_result: PipelineOutboxPublisherBatchResult | None = None
    while time.monotonic() < deadline:
        last_outbox_result = await publish_pending_pipeline_outbox(
            settings=settings,
            session_factory=session_factory,
        )
        ingest_request = client.get_ingest_request(ingest_request_id)
        last_request = ingest_request
        if ingest_request.status in TERMINAL_INGEST_FAILURE_STATUSES:
            raise E2ESeedError(
                f"Ingest request {ingest_request_id} reached terminal failure status "
                f"{ingest_request.status.value}: {ingest_request.failure_code} - {ingest_request.failure_detail}",
            )
        if ingest_request.materialized_meme_file_id is not None:
            return ingest_request, ingest_request.materialized_meme_file_id
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

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
    timeout_seconds: float,
) -> ContentPipelineItemDetail:
    deadline = time.monotonic() + timeout_seconds
    last_detail: ContentPipelineItemDetail | None = None
    last_outbox_result: PipelineOutboxPublisherBatchResult | None = None
    while time.monotonic() < deadline:
        last_outbox_result = await publish_pending_pipeline_outbox(
            settings=settings,
            session_factory=session_factory,
        )
        detail = client.get_item_detail(meme_file_id)
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
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    snapshot = last_detail.model_dump(mode="json") if last_detail is not None else None
    raise E2ESeedError(
        f"Timed out waiting for {meme_file_id} to dual-sync. Last detail: {snapshot}; "
        f"last outbox publish: {_outbox_result_snapshot(last_outbox_result)}",
    )


async def publish_pending_pipeline_outbox(
    *,
    settings: Settings,
    session_factory: AsyncSessionFactory,
) -> PipelineOutboxPublisherBatchResult:
    return await run_pipeline_outbox_publisher_batch(session_factory, settings=settings)


def _outbox_result_snapshot(result: PipelineOutboxPublisherBatchResult | None) -> dict[str, int | float] | None:
    if result is None:
        return None
    return {
        "recovered": result.recovered,
        "claimed": result.claimed,
        "published": result.published,
        "failed": result.failed,
        "duration_seconds": result.duration_seconds,
    }


def wait_for_dual_index_proof(
    client: PipelineApiClient,
    *,
    meme_file_id: uuid.UUID,
    timeout_seconds: float,
) -> SmokeProofResult:
    deadline = time.monotonic() + timeout_seconds
    last_result: SmokeProofResult | None = None
    while time.monotonic() < deadline:
        result = client.run_dual_index_proof(meme_file_id)
        last_result = result
        if result.both_targets_searchable:
            return result
        time.sleep(POLL_INTERVAL_SECONDS)

    snapshot = last_result.model_dump(mode="json") if last_result is not None else None
    raise E2ESeedError(
        "Created meme failed the internal dual-index proof before timeout: "
        f"{snapshot}",
    )


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


async def wait_for_meili_hits(meili_client: PipelineMeilisearchSyncClient, *, specs: list[SeedSpec]) -> None:
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        missing: list[str] = []
        for spec in specs:
            hits = await meili_client.search(spec.query, limit=10)
            expected_document_id = _stable_uuid(f"{spec.category}:file").hex
            if not any(hit.get("id") == expected_document_id for hit in hits if isinstance(hit, dict)):
                missing.append(spec.category)
        if not missing:
            return
        await asyncio.sleep(0.5)
    raise E2ESeedError("Timed out waiting for seeded Meilisearch documents to become searchable.")


def assert_created_is_distinct(*, created_meme_id: uuid.UUID, seeded: list[SeededMeme]) -> None:
    seeded_ids = [item.meme_id for item in seeded]
    if len(seeded_ids) != len(set(seeded_ids)):
        raise E2ESeedError(f"Seeded corpus contains duplicate meme ids: {seeded_ids}")
    if created_meme_id in seeded_ids:
        raise E2ESeedError(
            f"Created meme {created_meme_id} must be distinct from seeded corpus ids {seeded_ids}.",
        )


def prove_seeded_public_corpus(client: PipelineApiClient, *, seeded: list[SeededMeme]) -> dict[str, dict[str, object]]:
    proof: dict[str, dict[str, object]] = {}
    for item in seeded:
        if item.is_nsfw:
            default_search = client.public_search(item.query)
            requested_search = client.public_search(item.query, include_nsfw=True)
            assert_public_search_excludes(default_search, meme_id=item.meme_id, label=f"seeded {item.category}")
            assert_public_search_excludes(requested_search, meme_id=item.meme_id, label=f"seeded {item.category}")
            assert_public_detail_hidden(client, slug=item.slug, meme_id=item.meme_id)
            assert_public_detail_hidden(client, slug=item.slug, meme_id=item.meme_id, include_nsfw=True)
            proof[item.slug] = {
                "positive_detail_id": None,
                "anonymous_search_hidden_by_default": True,
                "anonymous_search_hidden_with_include_nsfw": True,
                "anonymous_detail_hidden_by_default": True,
                "anonymous_detail_hidden_with_include_nsfw": True,
            }
            continue

        search_payload = client.public_search(item.query)
        assert_public_search_contains(search_payload, meme_id=item.meme_id, label=f"seeded {item.category}")
        detail_payload = assert_public_detail_resolves(
            client,
            slug=item.slug,
            meme_id=item.meme_id,
        )
        proof[item.slug] = {"positive_detail_id": detail_payload.get("id")}
    return proof


def prove_seeded_public_trends(client: PipelineApiClient, *, seeded: list[SeededMeme]) -> dict[str, object]:
    representative = _require_seeded_category(seeded, "cat")

    trend_page = client.public_trend_page(limit=20)
    trend_ids = [
        item.get("meme", {}).get("id")
        for item in trend_page.get("items", [])
        if isinstance(item, dict)
    ]
    if str(representative.meme_id) not in trend_ids:
        raise E2ESeedError(
            f"Public trends page did not include seeded representative meme {representative.meme_id}; "
            f"trend_ids={trend_ids}",
        )

    tag_summary = _assert_public_trend_summary(
        _find_public_trend_summary(
            client.public_tag_trend_summaries(limit=20),
            slug=E2E_PUBLIC_TRENDS_TAG_SLUG,
            label="tag summaries",
        ),
        slug=E2E_PUBLIC_TRENDS_TAG_SLUG,
        title=_public_tag_title(E2E_PUBLIC_TRENDS_TAG_SLUG),
    )
    template_summary = _assert_public_trend_summary(
        _find_public_trend_summary(
            client.public_template_trend_summaries(limit=20),
            slug=E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
            label="template summaries",
        ),
        slug=E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
        title=f"{E2E_PUBLIC_TRENDS_TEMPLATE_NAME} memes",
    )

    tag_landing = client.public_tag_landing(E2E_PUBLIC_TRENDS_TAG_SLUG)
    _assert_landing_trend_summary(tag_landing, slug=E2E_PUBLIC_TRENDS_TAG_SLUG, label="tag landing")
    template_landing = client.public_template_landing(E2E_PUBLIC_TRENDS_TEMPLATE_SLUG)
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
    comparison = client.public_trend_comparison(comparison_items)
    comparison_proof = _assert_public_trend_comparison(comparison, representative=representative)

    timeline = client.public_trend_timeline(granularity=E2E_PUBLIC_TRENDS_TIMELINE_GRANULARITY, limit=12)
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
    points_by_day = {
        str(point.get("observed_at", ""))[:10]: point
        for point in points
        if isinstance(point, dict)
    }
    for expected in expected_points:
        observed_day = str(expected["observed_at"])[:10]
        point = points_by_day.get(observed_day)
        if point is None:
            raise E2ESeedError(f"Public trend {label} missing expected point day {observed_day}: {points}")
        for key in ("value", "meme_count", "snapshot_count", "source_views", "platform_views", "platform_likes"):
            if point.get(key) != expected[key]:
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
        int(point["snapshot_count"])
        for point in build_public_trend_aggregate_history_points_payload()
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
    include_nsfw: bool = False,
) -> dict[str, Any]:
    detail_payload = client.public_detail_by_slug(slug, include_nsfw=include_nsfw)
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
    include_nsfw: bool = False,
) -> None:
    status_code, payload = client.public_detail_by_slug_status(slug, include_nsfw=include_nsfw)
    if status_code == 404:
        return
    if status_code == 200 and payload.get("id") != str(meme_id):
        return
    raise E2ESeedError(
        f"Anonymous public detail for NSFW slug {slug!r} should be hidden; "
        f"status={status_code}, payload={payload}",
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
    return [
        item.get("meme", {}).get("id")
        for item in payload.get("items", [])
        if isinstance(item, dict)
    ]


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
