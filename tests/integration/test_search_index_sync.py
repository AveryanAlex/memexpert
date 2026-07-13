"""Integration tests for canonical search-index sync payload construction."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import Meme, MemeFile, MemeSeoPage, MemeSource, MemeSourceEngagementSnapshot, MemeTemplate
from memexpert.models.enums import (
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    IngestSourceKind,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
)
from memexpert.models.user import User
from memexpert.services.search_index_sync import (
    SEARCH_INDEX_ALGORITHM_VERSION,
    build_meilisearch_document,
    build_qdrant_sync_payload,
    load_search_index_state,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.transactional_db


async def _create_meme_with_primary_file(
    session: AsyncSession,
    *,
    media_type: ContentKind = ContentKind.IMAGE,
    language: ContentLanguage = ContentLanguage.EN,
    tags: list[str] | None = None,
    is_public: bool = True,
    is_nsfw: bool = False,
    popularity_score: float = 0.0,
    like_count: int = 0,
    uploader_user_id: uuid.UUID | None = None,
    ocr_text: str | None = None,
    quality_score: float = 0.8,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=media_type,
        primary_file_id=file_id,
        language=language,
        tags=tags or [],
        is_public=is_public,
        is_nsfw=is_nsfw,
        like_count=like_count,
        ocr_text=ocr_text,
    )

    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        s3_original_key=f"pipeline/originals/{meme_id}.jpg",
        mime_type="image/jpeg",
        quality_score=quality_score,
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    if uploader_user_id is not None:
        session.add(
            MemeSource(
                file_id=meme_file.id,
                platform=SourcePlatform.TELEGRAM,
                source_id=f"search-index-uploader-{meme_file.id}",
                post_id="upload",
                source_kind=IngestSourceKind.USER_UPLOAD,
                uploader_user_id=uploader_user_id,
                source_alive=True,
            )
        )
        await session.flush()
    if popularity_score > 0.0:
        await _set_source_engagement_score(session, meme_file.id, source_views=max(1, int(popularity_score)))
    return meme, meme_file


async def _set_source_engagement_score(session: AsyncSession, meme_file_id: uuid.UUID, *, source_views: int) -> None:
    source = await session.scalar(select(MemeSource).where(MemeSource.file_id == meme_file_id))
    if source is None:
        source = MemeSource(
            file_id=meme_file_id,
            platform=SourcePlatform.TELEGRAM,
            source_id=f"search-index-{meme_file_id.hex}",
            post_id="1",
            source_kind=IngestSourceKind.PUBLIC_CRAWLER,
            is_first_source=True,
            source_alive=True,
        )
        session.add(source)
        await session.flush()
    session.add(
        MemeSourceEngagementSnapshot(
            meme_source_id=source.id,
            capture_reason=SourceEngagementCaptureReason.MANUAL_REFRESH,
            view_count=source_views,
            reactions={},
            reaction_count=0,
            comment_count=None,
            forward_count=0,
            comments_state=SourceEngagementCommentsState.UNKNOWN,
            fetch_status=SourceEngagementFetchStatus.SUCCESS,
            source_alive=True,
            raw_metrics={"test": True},
        )
    )
    await session.flush()


async def test_search_index_state_builds_collection_aware_public_crawled_payloads(
    migrated_db_session: AsyncSession,
) -> None:
    public_owner = User()
    unlisted_owner = User()
    private_owner = User()
    member = User()
    migrated_db_session.add_all([public_owner, unlisted_owner, private_owner, member])
    await migrated_db_session.flush()

    meme, meme_file = await _create_meme_with_primary_file(
        migrated_db_session,
        tags=["frog", "wizard"],
        is_public=True,
        popularity_score=12.5,
        like_count=3,
        ocr_text="frog wizard caption",
        quality_score=0.91,
    )
    template = MemeTemplate(slug="frog-template", name="Frog Template")
    migrated_db_session.add(template)
    await migrated_db_session.flush()
    meme.template_id = template.id
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=meme.id,
            slug="frog-wizard",
            page_title="Frog Wizard",
            meta_description="A frog wizard meme",
            alt_text="frog wizard",
            model_id="test-model",
            prompt_version="v1",
        )
    )

    public_collection = Collection(
        owner_id=public_owner.id,
        title="Public frogs",
        visibility=CollectionVisibility.PUBLIC,
    )
    unlisted_collection = Collection(
        owner_id=unlisted_owner.id,
        title="Unlisted frogs",
        visibility=CollectionVisibility.UNLISTED,
    )
    private_collection = Collection(
        owner_id=private_owner.id,
        title="Private frogs",
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add_all([public_collection, unlisted_collection, private_collection])
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            CollectionMeme(collection_id=public_collection.id, meme_id=meme.id, added_by_user_id=public_owner.id),
            CollectionMeme(collection_id=unlisted_collection.id, meme_id=meme.id, added_by_user_id=unlisted_owner.id),
            CollectionMeme(collection_id=private_collection.id, meme_id=meme.id, added_by_user_id=private_owner.id),
            CollectionMember(
                collection_id=unlisted_collection.id,
                user_id=member.id,
                role=CollectionMembershipRole.VIEWER,
            ),
            CollectionMember(
                collection_id=private_collection.id,
                user_id=member.id,
                role=CollectionMembershipRole.EDITOR,
            ),
        ]
    )
    await migrated_db_session.commit()

    loaded_state = await load_search_index_state(migrated_db_session, meme_file.id)
    qdrant_payload = build_qdrant_sync_payload(loaded_state.canonical)
    meili_document = build_meilisearch_document(loaded_state.canonical)

    assert qdrant_payload.search_index_algorithm_version == SEARCH_INDEX_ALGORITHM_VERSION
    assert meili_document.search_index_algorithm_version == SEARCH_INDEX_ALGORITHM_VERSION
    assert qdrant_payload.is_public is True
    assert meili_document.is_public is True
    assert qdrant_payload.uploader_user_ids == []
    assert qdrant_payload.media_type == ContentKind.IMAGE.value
    assert qdrant_payload.language == ContentLanguage.EN.value
    assert qdrant_payload.tags == ["frog", "wizard"]
    assert qdrant_payload.template_id == str(template.id)
    assert qdrant_payload.template_slug == "frog-template"
    assert qdrant_payload.seo_page_slug == "frog-wizard"
    assert qdrant_payload.popularity_score > 0.0
    assert qdrant_payload.like_count == 3
    assert qdrant_payload.quality_score == 0.91
    assert qdrant_payload.ocr_snippet == "frog wizard caption"
    assert set(qdrant_payload.collection_ids) == {
        str(public_collection.id),
        str(unlisted_collection.id),
        str(private_collection.id),
    }
    assert qdrant_payload.public_collection_ids == [str(public_collection.id)]
    assert qdrant_payload.unlisted_collection_ids == [str(unlisted_collection.id)]
    assert qdrant_payload.private_collection_ids == [str(private_collection.id)]
    assert set(qdrant_payload.shared_collection_ids) == {
        str(unlisted_collection.id),
        str(private_collection.id),
    }
    assert set(qdrant_payload.collection_owner_user_ids) == {
        str(public_owner.id),
        str(unlisted_owner.id),
        str(private_owner.id),
    }
    assert qdrant_payload.collection_member_user_ids == [str(member.id)]
    assert meili_document.meme_file_id == str(meme_file.id)
    assert meili_document.collection_ids == qdrant_payload.collection_ids
    assert meili_document.collection_owner_user_ids == qdrant_payload.collection_owner_user_ids
    assert meili_document.collection_member_user_ids == qdrant_payload.collection_member_user_ids


async def test_search_index_state_rebuild_reflects_visibility_collection_tag_and_popularity_changes(
    migrated_db_session: AsyncSession,
) -> None:
    author = User()
    collection_owner = User()
    collaborator = User()
    migrated_db_session.add_all([author, collection_owner, collaborator])
    await migrated_db_session.flush()

    meme, meme_file = await _create_meme_with_primary_file(
        migrated_db_session,
        tags=["old-tag"],
        is_public=False,
        popularity_score=1.0,
        like_count=1,
        uploader_user_id=author.id,
        ocr_text="private upload",
    )
    await migrated_db_session.commit()

    initial_state = await load_search_index_state(migrated_db_session, meme_file.id)
    initial_payload = build_qdrant_sync_payload(initial_state.canonical)
    assert initial_payload.is_public is False
    assert initial_payload.uploader_user_ids == [str(author.id)]
    assert initial_payload.collection_ids == []
    assert initial_payload.tags == ["old-tag"]
    assert initial_payload.template_id is None
    initial_popularity_score = initial_payload.popularity_score
    assert initial_popularity_score > 0.0
    assert initial_payload.like_count == 1

    template = MemeTemplate(slug="fresh-template", name="Fresh Template")
    shared_collection = Collection(
        owner_id=collection_owner.id,
        title="Shared uploads",
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add_all([template, shared_collection])
    await migrated_db_session.flush()

    meme.tags = ["fresh-tag", "shared"]
    meme.template_id = template.id
    await _set_source_engagement_score(migrated_db_session, meme_file.id, source_views=42)
    meme.like_count = 7
    meme.is_public = True
    migrated_db_session.add(
        MemeSeoPage(
            meme_id=meme.id,
            slug="fresh-upload",
            page_title="Fresh Upload",
            meta_description="Updated private upload",
            alt_text="fresh upload",
            model_id="test-model",
            prompt_version="v2",
        )
    )
    migrated_db_session.add_all(
        [
            CollectionMeme(collection_id=shared_collection.id, meme_id=meme.id, added_by_user_id=collection_owner.id),
            CollectionMember(
                collection_id=shared_collection.id,
                user_id=collaborator.id,
                role=CollectionMembershipRole.VIEWER,
            ),
        ]
    )
    shared_collection.visibility = CollectionVisibility.UNLISTED
    await migrated_db_session.commit()

    rebuilt_state = await load_search_index_state(migrated_db_session, meme_file.id)
    rebuilt_payload = build_qdrant_sync_payload(rebuilt_state.canonical)
    rebuilt_document = build_meilisearch_document(rebuilt_state.canonical)

    assert rebuilt_payload.is_public is True
    assert rebuilt_payload.tags == ["fresh-tag", "shared"]
    assert rebuilt_payload.template_id == str(template.id)
    assert rebuilt_payload.template_slug == "fresh-template"
    assert rebuilt_payload.seo_page_slug == "fresh-upload"
    assert rebuilt_payload.popularity_score > initial_popularity_score
    assert rebuilt_payload.like_count == 7
    assert rebuilt_payload.collection_ids == [str(shared_collection.id)]
    assert rebuilt_payload.public_collection_ids == []
    assert rebuilt_payload.unlisted_collection_ids == [str(shared_collection.id)]
    assert rebuilt_payload.private_collection_ids == []
    assert rebuilt_payload.shared_collection_ids == [str(shared_collection.id)]
    assert rebuilt_payload.collection_owner_user_ids == [str(collection_owner.id)]
    assert rebuilt_payload.collection_member_user_ids == [str(collaborator.id)]
    assert rebuilt_document.is_public is True
    assert rebuilt_document.collection_ids == [str(shared_collection.id)]
    assert rebuilt_document.popularity_score == rebuilt_payload.popularity_score
    assert rebuilt_document.like_count == 7
