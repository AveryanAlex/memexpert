#!/usr/bin/env python3
"""Seed and prove the deterministic containerized PRD E2E corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx
from botocore.exceptions import ClientError
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Distance, VectorParams
from sqlalchemy import select

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.core.meilisearch import PipelineMeilisearchSyncClient
from memexpert.core.qdrant import PipelineQdrantSyncClient
from memexpert.core.storage import get_pipeline_storage_settings, get_s3_client
from memexpert.core.voyage import build_pipeline_voyage_client
from memexpert.models.base import utcnow
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeSeoPage,
    MemeSource,
    PipelineStageJournal,
)
from memexpert.models.enums import (
    ContentKind,
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    EmbeddingInputType,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineErrorResponse,
    ContentPipelineItemDetail,
    ContentPipelineUploadRead,
    SmokeProofResult,
)
from memexpert.services.search_index_sync import (
    build_meilisearch_document,
    build_qdrant_sync_payload,
    load_search_index_state,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_API_BASE_URL: Final = "http://api:8000"
DEFAULT_ARTIFACTS_DIR: Final = Path("/artifacts")
DEFAULT_TIMEOUT_SECONDS: Final = 180.0
DEFAULT_API_TIMEOUT_SECONDS: Final = 20.0
POLL_INTERVAL_SECONDS: Final = 1.0
E2E_SOURCE_ID: Final = "e2e-prd-seed"
E2E_UPLOAD_SOURCE_ID: Final = "e2e-prd-upload"
E2E_MODEL_ID: Final = "e2e-prd-seed"
E2E_PROMPT_VERSION: Final = "e2e-prd-v1"
UUID_NAMESPACE: Final = uuid.UUID("176f5e31-6e5d-5e43-80aa-1f7aa3aa0d4b")


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

    def upload_cat_png(self, *, image_bytes: bytes, run_id: str) -> ContentPipelineUploadRead:
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
        return _validate_response(response, expected_status=201, model=ContentPipelineUploadRead)

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
        upload = api_client.upload_cat_png(image_bytes=cat_png, run_id=run_id)
        detail = wait_for_dual_synced(
            api_client,
            meme_file_id=upload.meme_file_id,
            timeout_seconds=args.timeout_seconds,
        )
        print(f"Uploaded item dual-synced: meme_file_id={upload.meme_file_id}")
        dual_index_result = wait_for_dual_index_proof(
            api_client,
            meme_file_id=upload.meme_file_id,
            timeout_seconds=args.timeout_seconds,
        )
        slug = await publish_created_meme(
            settings=settings,
            meme_id=detail.meme_id,
            query="cat",
        )

        seeded = await seed_direct_corpus(
            settings=settings,
            s3_client=s3_client,
            qdrant_sync_client=qdrant_sync_client,
            meili_client=meili_client,
            specs=specs,
        )
        print(f"Seeded deterministic public corpus: {', '.join(item.category for item in seeded)}")

        assert_created_is_distinct(created_meme_id=detail.meme_id, seeded=seeded)
        created_search_payload = api_client.public_search("cat")
        assert_public_search_contains(created_search_payload, meme_id=detail.meme_id)
        created_detail_payload = assert_public_detail_resolves(
            api_client,
            slug=slug,
            meme_id=detail.meme_id,
        )
        seeded_proofs = prove_seeded_public_corpus(api_client, seeded=seeded)

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
        "seeded_memes": [
            {
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
            for item in seeded
        ],
        "created_meme": {
            "meme_id": str(detail.meme_id),
            "meme_file_id": str(upload.meme_file_id),
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
                ocr_text=spec.ocr_text,
                language=spec.language,
                is_nsfw=spec.is_nsfw,
                is_public=True,
                popularity_score=10.0,
                tags=list(spec.tags),
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
                is_primary=True,
            )
            session.add(meme_file)
            await session.flush()
            meme.primary_file_id = meme_file_id
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
            tags=("cat", "e2e-prd"),
        ),
        SeedSpec(
            category="dog",
            color=(0, 0, 255),
            slug="e2e-prd-dog-search",
            ocr_text="dog e2e prd corpus meme",
            caption="Deterministic dog search meme",
            alt_text="Blue square dog PRD E2E meme fixture",
            query="dog",
            tags=("dog", "e2e-prd"),
        ),
        SeedSpec(
            category="frog",
            color=(0, 255, 0),
            slug="e2e-prd-frog-search",
            ocr_text="frog e2e prd corpus meme",
            caption="Deterministic frog search meme",
            alt_text="Green square frog PRD E2E meme fixture",
            query="frog",
            tags=("frog", "e2e-prd"),
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

    for meme_id in meme_ids:
        meme = await session.get(Meme, meme_id)
        if meme is not None:
            await session.delete(meme)

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


async def publish_created_meme(*, settings: Settings, meme_id: uuid.UUID, query: str) -> str:
    slug = f"e2e-prd-created-{meme_id.hex[:12]}"
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        meme = await session.get(Meme, meme_id)
        if meme is None:
            raise E2ESeedError(f"Created meme {meme_id} is missing from the database.")
        meme.is_public = True
        meme.is_nsfw = False
        if query not in meme.tags:
            meme.tags = [*meme.tags, query, "e2e-prd"]
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
        await session.commit()
    _ = settings
    return slug


def wait_for_dual_synced(
    client: PipelineApiClient,
    *,
    meme_file_id: uuid.UUID,
    timeout_seconds: float,
) -> ContentPipelineItemDetail:
    deadline = time.monotonic() + timeout_seconds
    last_detail: ContentPipelineItemDetail | None = None
    while time.monotonic() < deadline:
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
        time.sleep(POLL_INTERVAL_SECONDS)
    snapshot = last_detail.model_dump(mode="json") if last_detail is not None else None
    raise E2ESeedError(
        f"Timed out waiting for {meme_file_id} to dual-sync. Last detail: {snapshot}",
    )


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
    hit_ids = [
        item.get("meme", {}).get("id")
        for item in payload.get("items", [])
        if isinstance(item, dict)
    ]
    if str(meme_id) not in hit_ids:
        raise E2ESeedError(f"Public search did not include {label} {meme_id}; hits={hit_ids}")


def assert_public_search_excludes(payload: dict[str, Any], *, meme_id: uuid.UUID, label: str) -> None:
    hit_ids = [
        item.get("meme", {}).get("id")
        for item in payload.get("items", [])
        if isinstance(item, dict)
    ]
    if str(meme_id) in hit_ids:
        raise E2ESeedError(f"Anonymous public search exposed hidden {label} {meme_id}; hits={hit_ids}")


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


def _stable_uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(UUID_NAMESPACE, name)


def _validate_response[ModelT](
    response: httpx.Response,
    *,
    expected_status: int,
    model: type[ModelT],
) -> ModelT:
    payload = _validate_json_response(response, expected_status=expected_status)
    try:
        return model.model_validate(payload)  # type: ignore[attr-defined, no-any-return]
    except ValidationError as exc:
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned a malformed payload: {exc}",
        ) from exc


def _validate_json_response(response: httpx.Response, *, expected_status: int) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise E2ESeedError(
            f"{response.request.method} {response.request.url.path} returned non-JSON output: "
            f"{exc}; body={response.text!r}",
        ) from exc

    if response.status_code != expected_status:
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
