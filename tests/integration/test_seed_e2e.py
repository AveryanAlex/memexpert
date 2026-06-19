"""Focused regression tests for the container PRD E2E seed helpers."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.core.voyage import VoyageEmbeddingResult
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionInvite, CollectionMember
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeSourceEngagementSnapshot,
    MemeTemplate,
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
    ContentProcessingStatus,
    EmbeddingInputType,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.models.user import AnalyticsEvent, User
from scripts import seed_e2e

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.meilisearch import PipelineMeilisearchDocument
    from memexpert.core.qdrant import QdrantSyncPayload

class RecordingQdrantSyncClient:
    def __init__(self) -> None:
        self.upsert_calls: list[tuple[QdrantSyncPayload, tuple[float, ...]]] = []

    async def upsert_meme_point(self, payload: QdrantSyncPayload, vector: tuple[float, ...]) -> None:
        self.upsert_calls.append((payload, vector))

    async def fetch_meme_point(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id
        return None

    async def delete_meme_point(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id


class RecordingMeilisearchSyncClient:
    def __init__(self) -> None:
        self.upsert_calls: list[PipelineMeilisearchDocument] = []

    async def upsert_document(self, document: PipelineMeilisearchDocument) -> None:
        self.upsert_calls.append(document)

    async def fetch_document(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id
        return None

    async def delete_document(self, meme_file_id: uuid.UUID) -> None:
        _ = meme_file_id

    async def ensure_index(self) -> None:
        return None

    async def search(self, query: str, *, limit: int = 20, prefilter: object | None = None) -> list[dict[str, Any]]:
        _ = query
        _ = limit
        _ = prefilter
        return []


@pytest.mark.asyncio
async def test_publish_created_meme_resync_rebuilds_public_indexes_from_canonical_db_state(
    migrated_db_session: AsyncSession,
) -> None:
    embedding = VoyageEmbeddingResult(
        model="test-model",
        dimensions=3,
        vector=(0.1, 0.2, 0.3),
        input_hash=uuid.uuid4().hex,
    )
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.EN,
        tags=[],
        is_public=False,
        is_nsfw=True,
        ocr_text="pre-public upload",
    )

    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"pipeline/originals/{meme_id}.png",
        mime_type="image/png",
        quality_score=0.9,
    )
    migrated_db_session.add(meme)
    await migrated_db_session.flush()
    migrated_db_session.add(meme_file)
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            MemeFileOCRResult(
                meme_file_id=meme_file.id,
                engine="fake-test",
                fallback_engine=None,
                fallback_used=False,
                low_confidence=False,
                confidence=1.0,
                language=ContentLanguage.EN,
                extracted_text="cat generated upload ocr",
                source_object_key=meme_file.s3_original_key,
            ),
            EmbeddingCache(
                input_hash=embedding.input_hash,
                input_type=EmbeddingInputType.IMAGE,
                embedding=embedding.embedding_bytes,
                model_version=embedding.model,
                source_file_id=meme_file.id,
            ),
            seed_e2e._build_sync_snapshot(
                meme_file_id=meme_file.id,
                target=SyncTargetKind.QDRANT,
                preview={"is_public": False, "tags": []},
                now=utcnow(),
            ),
            seed_e2e._build_sync_snapshot(
                meme_file_id=meme_file.id,
                target=SyncTargetKind.MEILISEARCH,
                preview={"is_public": False, "tags": []},
                now=utcnow(),
            ),
        ],
    )
    await migrated_db_session.commit()

    slug = await seed_e2e.publish_created_meme_in_session(migrated_db_session, meme_id=meme.id, query="cat")
    await migrated_db_session.commit()

    qdrant_client = RecordingQdrantSyncClient()
    meili_client = RecordingMeilisearchSyncClient()
    await seed_e2e.resync_created_public_meme_indexes_in_session(
        migrated_db_session,
        settings=Settings.model_validate({"pipeline_voyage_output_dimensions": 3}),
        meme_file_id=meme_file.id,
        qdrant_sync_client=qdrant_client,
        meili_client=meili_client,
    )

    assert len(qdrant_client.upsert_calls) == 1
    qdrant_payload, qdrant_vector = qdrant_client.upsert_calls[0]
    assert qdrant_payload.meme_id == meme.id
    assert qdrant_payload.meme_file_id == meme_file.id
    assert qdrant_payload.is_public is True
    assert qdrant_payload.is_nsfw is False
    assert qdrant_payload.tags == ["cat", "e2e-prd"]
    assert qdrant_payload.seo_page_slug == slug
    assert qdrant_vector == pytest.approx(embedding.vector)

    assert len(meili_client.upsert_calls) == 1
    meili_document = meili_client.upsert_calls[0]
    assert meili_document.id == meme_file.id.hex
    assert meili_document.meme_id == str(meme.id)
    assert meili_document.is_public is True
    assert meili_document.is_nsfw is False
    assert meili_document.tags == ["cat", "e2e-prd"]
    assert meili_document.seo_page_slug == slug
    assert meili_document.ocr_text == "cat generated upload ocr"

    snapshot_count = await migrated_db_session.scalar(
        select(func.count()).select_from(MemeFileSyncTargetSnapshot).where(
            MemeFileSyncTargetSnapshot.meme_file_id == meme_file.id,
        ),
    )
    assert snapshot_count == 2
    snapshots = {
        snapshot.sync_target: snapshot
        for snapshot in (
            await migrated_db_session.execute(
                select(MemeFileSyncTargetSnapshot).where(MemeFileSyncTargetSnapshot.meme_file_id == meme_file.id),
            )
        ).scalars()
    }
    assert snapshots[SyncTargetKind.QDRANT].status is SyncTargetStatus.SYNCED
    assert snapshots[SyncTargetKind.QDRANT].attempt_count == 2
    assert snapshots[SyncTargetKind.QDRANT].last_payload_preview["is_public"] is True
    assert snapshots[SyncTargetKind.MEILISEARCH].status is SyncTargetStatus.SYNCED
    assert snapshots[SyncTargetKind.MEILISEARCH].attempt_count == 2
    assert snapshots[SyncTargetKind.MEILISEARCH].last_payload_preview["is_public"] is True


def test_wait_for_public_search_contains_polls_public_api_until_created_meme_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meme_id = uuid.uuid4()
    payloads: list[dict[str, Any]] = [
        {"items": [{"meme": {"id": str(uuid.uuid4())}}]},
        {"items": [{"meme": {"id": str(meme_id)}}]},
    ]

    class PublicSearchClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def public_search(self, query: str) -> dict[str, Any]:
            self.queries.append(query)
            return payloads.pop(0)

    client = PublicSearchClient()
    monkeypatch.setattr(seed_e2e.time, "sleep", lambda _: None)

    result = seed_e2e.wait_for_public_search_contains(
        cast("seed_e2e.PipelineApiClient", client),
        query="cat",
        meme_id=meme_id,
        timeout_seconds=1.0,
    )

    assert result == {"items": [{"meme": {"id": str(meme_id)}}]}
    assert client.queries == ["cat", "cat"]


def test_collection_management_fixture_payload_is_deterministic_private_and_e2e_only() -> None:
    cat = seeded_meme("cat")
    dog = seeded_meme("dog")

    fixture = seed_e2e.build_collection_management_fixture([cat, dog])
    payload = seed_e2e.build_collection_management_fixture_payload(fixture)

    assert payload["owner"] == {
        "label": "owner",
        "user_id": str(seed_e2e._stable_uuid("collection-management:owner:user")),
        "email": seed_e2e.E2E_OWNER_EMAIL,
        "password": seed_e2e.E2E_ACCOUNT_PASSWORD,
    }
    assert payload["member"] == {
        "label": "member",
        "user_id": str(seed_e2e._stable_uuid("collection-management:member:user")),
        "email": seed_e2e.E2E_MEMBER_EMAIL,
        "password": seed_e2e.E2E_ACCOUNT_PASSWORD,
    }
    assert payload["collection"] == {
        "id": str(seed_e2e._stable_uuid("collection-management:launch:collection")),
        "title": seed_e2e.E2E_COLLECTION_TITLE,
        "description": seed_e2e.E2E_COLLECTION_DESCRIPTION,
        "visibility": "private",
    }
    assert payload["invite"] == {
        "id": str(seed_e2e._stable_uuid("collection-management:launch:viewer-invite")),
        "token": seed_e2e.E2E_COLLECTION_INVITE_TOKEN,
        "join_path": f"/collection/invite/{seed_e2e.E2E_COLLECTION_INVITE_TOKEN}",
    }
    assert [item["category"] for item in payload["saved_memes"]] == ["cat", "dog"]
    assert [item["category"] for item in payload["pinned_memes"]] == ["cat", "dog"]
    assert payload["collection"]["visibility"] != "public"


def test_public_trends_artifact_payload_is_deterministic_and_url_ready() -> None:
    cat = seeded_meme("cat")
    dog = seeded_meme("dog")
    frog = seeded_meme("frog")

    payload = seed_e2e.build_public_trends_artifact([cat, dog, frog])

    assert payload["trend_path"] == "/trends"
    assert payload["tag"] == {
        "slug": seed_e2e.E2E_PUBLIC_TRENDS_TAG_SLUG,
        "title": "E2E Prd Trends memes",
        "path": "/tags/e2e-prd-trends",
        "history_points": seed_e2e.build_public_trend_aggregate_history_points_payload(),
    }
    assert payload["template"] == {
        "slug": seed_e2e.E2E_PUBLIC_TRENDS_TEMPLATE_SLUG,
        "title": "E2E PRD Template memes",
        "path": "/templates/e2e-prd-template",
        "history_points": seed_e2e.build_public_trend_aggregate_history_points_payload(),
    }
    assert payload["compare"] == {
        "items": ["meme:e2e-prd-cat-search", "tag:e2e-prd-trends", "template:e2e-prd-template"],
        "path": (
            "/trends/compare?item=meme%3Ae2e-prd-cat-search&item=tag%3Ae2e-prd-trends"
            "&item=template%3Ae2e-prd-template"
        ),
    }
    assert payload["timeline"] == {
        "path": "/trends/timeline?granularity=month",
        "granularity": "month",
        "period": "2026-01",
        "period_label": "January 2026",
        "snapshot_count": 6,
    }
    assert payload["representative_meme"] == {
        "category": "cat",
        "slug": "e2e-prd-cat-search",
        "title": "Deterministic cat search meme",
    }


def test_public_trend_template_and_source_snapshot_helpers_are_deterministic() -> None:
    meme_source_id = seed_e2e._stable_uuid("cat:source")
    template = seed_e2e.build_public_trends_template()
    rows = seed_e2e.build_public_trend_snapshot_rows(meme_source_id=meme_source_id, category="cat")

    assert isinstance(template, MemeTemplate)
    assert template.id == seed_e2e._stable_uuid("public-trends:template")
    assert template.slug == seed_e2e.E2E_PUBLIC_TRENDS_TEMPLATE_SLUG
    assert template.name == seed_e2e.E2E_PUBLIC_TRENDS_TEMPLATE_NAME
    assert template.description == seed_e2e.E2E_PUBLIC_TRENDS_TEMPLATE_DESCRIPTION
    assert template.is_curated is True

    assert all(isinstance(row, MemeSourceEngagementSnapshot) for row in rows)
    assert [row.id for row in rows] == [
        seed_e2e._stable_uuid("cat:source-engagement-baseline"),
        seed_e2e._stable_uuid("cat:source-engagement-snapshot:1"),
        seed_e2e._stable_uuid("cat:source-engagement-snapshot:2"),
    ]
    assert [row.meme_source_id for row in rows] == [meme_source_id, meme_source_id, meme_source_id]
    assert [row.captured_at.isoformat() for row in rows] == [
        "2026-01-05T11:59:59+00:00",
        "2026-01-05T12:00:00+00:00",
        "2026-01-12T12:00:00+00:00",
    ]
    assert [row.view_count for row in rows] == [0, 120, 300]
    assert [row.reaction_count for row in rows] == [0, 12, 30]
    assert [row.forward_count for row in rows] == [0, 4, 10]
    assert seed_e2e.build_public_trend_snapshot_rows(meme_source_id=meme_source_id, category="cat-nsfw") == []


def test_seed_meme_and_platform_event_helpers_use_current_read_model_inputs() -> None:
    cat_spec = next(spec for spec in seed_e2e.build_seed_specs() if spec.category == "cat")
    meme_id = seed_e2e._stable_uuid("cat:meme")
    meme_file_id = seed_e2e._stable_uuid("cat:file")
    template_id = seed_e2e._stable_uuid("public-trends:template")

    meme = seed_e2e.build_seed_meme(
        spec=cat_spec,
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        public_trends_template_id=template_id,
    )
    events = seed_e2e.build_public_trend_analytics_event_rows(meme_id=meme_id, category="cat")

    assert isinstance(meme, Meme)
    assert not hasattr(meme, "popularity_score")
    assert meme.like_count == 19
    assert meme.template_id == template_id
    assert len(events) == 140
    assert all(isinstance(event, AnalyticsEvent) for event in events)
    assert events[0].id == seed_e2e._stable_uuid("cat:public-trend-event:1:platform_views:1")
    assert events[0].event_type is AnalyticsEventType.MEME_VIEW
    assert events[0].payload["meme_id"] == str(meme_id)
    assert events[0].payload["seed"] == "e2e-prd-public-trends"
    assert events[0].occurred_at.isoformat() == "2026-01-05T12:00:00+00:00"
    assert sum(1 for event in events if event.event_type is AnalyticsEventType.MEME_VIEW) == 100
    assert sum(1 for event in events if event.event_type is AnalyticsEventType.MEME_SEND) == 8
    assert sum(1 for event in events if event.event_type is AnalyticsEventType.MEME_SAVE) == 13
    assert sum(1 for event in events if event.event_type is AnalyticsEventType.MEME_LIKE) == 19
    assert seed_e2e.build_public_trend_analytics_event_rows(meme_id=meme_id, category="cat-nsfw") == []


def test_public_trend_aggregate_history_points_payload_uses_real_seed_snapshots() -> None:
    points = seed_e2e.build_public_trend_aggregate_history_points_payload()

    assert points == [
        {
            "observed_at": "2026-01-05T00:00:00+00:00",
            "value": 108.2,
            "metric": "aggregate_popularity_score",
            "label": "Aggregate popularity score",
            "meme_count": 3,
            "snapshot_count": 3,
            "source_views": 280,
            "source_reactions": 28,
            "source_reposts": 9,
            "platform_views": 105,
            "platform_sends": 6,
            "platform_saves": 12,
            "platform_likes": 18,
        },
        {
            "observed_at": "2026-01-12T00:00:00+00:00",
            "value": 131.0,
            "metric": "aggregate_popularity_score",
            "label": "Aggregate popularity score",
            "meme_count": 3,
            "snapshot_count": 3,
            "source_views": 420,
            "source_reactions": 42,
            "source_reposts": 13,
            "platform_views": 155,
            "platform_sends": 12,
            "platform_saves": 19,
            "platform_likes": 29,
        },
    ]


@pytest.mark.asyncio
async def test_cleanup_public_trends_template_rows_removes_deterministic_template(
    migrated_db_session: AsyncSession,
) -> None:
    template = seed_e2e.build_public_trends_template()
    migrated_db_session.add(template)
    await migrated_db_session.flush()

    await seed_e2e.cleanup_public_trends_template_rows(migrated_db_session)

    assert await migrated_db_session.get(MemeTemplate, template.id) is None
    template_count = await migrated_db_session.scalar(
        select(func.count()).select_from(MemeTemplate).where(MemeTemplate.slug == template.slug),
    )
    assert template_count == 0


@pytest.mark.asyncio
async def test_cleanup_collection_management_fixture_rows_removes_seeded_accounts_and_collection(
    migrated_db_session: AsyncSession,
) -> None:
    now = utcnow()
    owner_id = seed_e2e._stable_uuid("collection-management:owner:user")
    member_id = seed_e2e._stable_uuid("collection-management:member:user")
    collection_id = seed_e2e._stable_uuid("collection-management:launch:collection")
    invite_id = seed_e2e._stable_uuid("collection-management:launch:viewer-invite")

    owner = User(
        id=owner_id,
        status=AccountStatus.ACTIVE,
        email=seed_e2e.E2E_OWNER_EMAIL,
        email_verified_at=now,
        password_hash=seed_e2e.E2E_ACCOUNT_PASSWORD_HASH,
    )
    member = User(
        id=member_id,
        status=AccountStatus.ACTIVE,
        email=seed_e2e.E2E_MEMBER_EMAIL,
        email_verified_at=now,
        password_hash=seed_e2e.E2E_ACCOUNT_PASSWORD_HASH,
    )
    migrated_db_session.add_all([owner, member])
    await migrated_db_session.flush()

    migrated_db_session.add(
        Collection(
            id=collection_id,
            owner_id=owner_id,
            title=seed_e2e.E2E_COLLECTION_TITLE,
            kind=CollectionKind.CUSTOM,
            visibility=CollectionVisibility.PRIVATE,
        ),
    )
    await migrated_db_session.flush()
    owner.active_save_collection_id = collection_id
    migrated_db_session.add_all(
        [
            CollectionMember(collection_id=collection_id, user_id=owner_id, role=CollectionMembershipRole.OWNER),
            CollectionInvite(
                id=invite_id,
                collection_id=collection_id,
                created_by_user_id=owner_id,
                token_hash=seed_e2e._collection_invite_token_hash(seed_e2e.E2E_COLLECTION_INVITE_TOKEN),
                role=CollectionMembershipRole.VIEWER,
                channel=CollectionInviteChannel.DIRECT_LINK,
                label="E2E viewer invite",
                status=CollectionInviteStatus.PENDING,
                max_uses=None,
            ),
        ],
    )
    await migrated_db_session.flush()

    await seed_e2e.cleanup_collection_management_fixture_rows(migrated_db_session)

    assert await migrated_db_session.get(Collection, collection_id) is None
    assert await migrated_db_session.get(User, owner_id) is None
    assert await migrated_db_session.get(User, member_id) is None
    invite_count = await migrated_db_session.scalar(
        select(func.count()).select_from(CollectionInvite).where(CollectionInvite.id == invite_id),
    )
    assert invite_count == 0


def seeded_meme(category: str) -> seed_e2e.SeededMeme:
    return seed_e2e.SeededMeme(
        category=category,
        meme_id=seed_e2e._stable_uuid(f"test:{category}:meme"),
        meme_file_id=seed_e2e._stable_uuid(f"test:{category}:file"),
        slug=f"e2e-prd-{category}-search",
        query=category,
        object_key=f"pipeline/originals/e2e-prd/{category}.png",
        title=f"Deterministic {category} search meme",
        tags=(category, "e2e-prd"),
        is_nsfw=False,
        language=ContentLanguage.EN,
        media_type=ContentKind.IMAGE,
    )
