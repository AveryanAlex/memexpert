"""Focused tests for the Telegram inline service boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from memexpert.core.config import Settings
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme, MemeFile, MemeSource, MemeSourceEngagementSnapshot, TelegramFileIdCache
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionMembershipRole,
    ContentKind,
    ContentLanguage,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourcePlatform,
    TelegramMediaFormat,
)
from memexpert.models.user import AnalyticsEvent, User
from memexpert.schemas.meme import (
    MemeResultAttributionRead,
    MemeSearchPageRead,
    PublicMemeCardRead,
    PublicMemeFileRead,
    PublicMemeSearchResultRead,
    RecommendationFeedPageRead,
)
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchScope, MemeSearchService
from memexpert.services.recommendations.telegram_sessions import TelegramInlineSessionStore
from memexpert.services.telegram_inline import (
    TelegramInlineMemeSearchService,
    TelegramInlineRecommendationService,
    TelegramInlineSearchPage,
    TelegramInlineService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.schemas.meme import MemeFileRead

pytestmark = pytest.mark.transactional_db


BOT_SCOPE = "telegram-inline-service-test-scope"
TELEGRAM_ID = 8_102_203


class UnusedMemeSearchService:
    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "telegram_inline_search",
    ) -> MemeSearchPageRead:
        _ = (query, viewer_user_id, filters, limit, offset, surface)
        raise AssertionError("Empty-query discovery should not call text search.")


class RecordingMemeSearchService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def search_memes(
        self,
        query: str,
        *,
        viewer_user_id: uuid.UUID | None = None,
        filters: MemeSearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        surface: str = "telegram_inline_search",
    ) -> MemeSearchPageRead:
        self.calls.append(
            {
                "query": query,
                "viewer_user_id": viewer_user_id,
                "scope": None if filters is None else filters.scope,
                "include_nsfw": None if filters is None else filters.include_nsfw,
                "limit": limit,
                "offset": offset,
                "surface": surface,
            }
        )
        return MemeSearchPageRead(
            items=[],
            limit=limit,
            offset=offset,
            total=0,
            has_more=False,
            request_id="req_recording_inline_text",
        )


class UnusedRecommendationService:
    async def home_feed(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
        cursor: str | None = None,
        offset: int = 0,
    ) -> RecommendationFeedPageRead:
        _ = (viewer_user_id, filters, limit, cursor, offset)
        raise AssertionError("Text queries should not call the home recommender.")


class RecordingRecommendationService:
    def __init__(self, pages: dict[str | None, RecommendationFeedPageRead]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def home_feed(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filters: MemeSearchFilters,
        limit: int,
        cursor: str | None = None,
        offset: int = 0,
    ) -> RecommendationFeedPageRead:
        self.calls.append(
            {
                "viewer_user_id": viewer_user_id,
                "scope": filters.scope,
                "include_nsfw": filters.include_nsfw,
                "limit": limit,
                "cursor": cursor,
                "offset": offset,
            }
        )
        return self.pages[cursor]


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        _ = ex
        self.values[key] = value
        return True

    async def sadd(self, key: str, *values: str) -> int:
        before = len(self.sets.setdefault(key, set()))
        self.sets[key].update(values)
        return len(self.sets[key]) - before

    async def smembers(self, key: str) -> object:
        return self.sets.get(key, set())

    async def expire(self, key: str, seconds: int) -> bool:
        _ = (key, seconds)
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.values.pop(key, None) is not None)
            deleted += int(self.sets.pop(key, None) is not None)
        return deleted


class FakeMediaUrlProvider:
    async def get_media_url(self, file: MemeFileRead) -> str | None:
        _ = file
        return None


async def create_meme_file(
    session: AsyncSession,
    *,
    media_type: ContentKind = ContentKind.IMAGE,
    mime_type: str = "image/jpeg",
    is_public: bool = True,
    is_nsfw: bool = False,
    popularity_score: float = 0.0,
    uploader_user_id: uuid.UUID | None = None,
    s3_original_key: str | None = None,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=media_type,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=[media_type.value],
        is_public=is_public,
        is_nsfw=is_nsfw,
    )
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        s3_original_key=s3_original_key or f"https://cdn.example.test/{meme_id}.jpg",
        mime_type=mime_type,
        width=640,
        height=480,
        quality_score=0.9,
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()
    if uploader_user_id is not None:
        collection_id = await session.scalar(
            select(User.active_save_collection_id).where(User.id == uploader_user_id)
        )
        if collection_id is None:
            collection = Collection(owner_id=uploader_user_id, title=f"Inline upload {meme.id}")
            session.add(collection)
            await session.flush()
            collection_id = collection.id
        session.add(
            CollectionMeme(collection_id=collection_id, meme_id=meme.id, added_by_user_id=uploader_user_id)
        )
        await session.flush()
    if popularity_score > 0.0:
        source = MemeSource(
            file_id=file_id,
            platform=SourcePlatform.TELEGRAM,
            source_id=f"inline-service-{meme_id.hex}",
            post_id="1",
            is_first_source=True,
            source_alive=True,
        )
        session.add(source)
        await session.flush()
        session.add(
            MemeSourceEngagementSnapshot(
                meme_source_id=source.id,
                capture_reason=SourceEngagementCaptureReason.MANUAL_REFRESH,
                view_count=max(1, int(popularity_score)),
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
    return meme, file


async def add_file_id_cache(
    session: AsyncSession,
    *,
    file: MemeFile,
    telegram_file_id: str,
    media_format: TelegramMediaFormat = TelegramMediaFormat.PHOTO,
) -> None:
    session.add(
        TelegramFileIdCache(
            meme_file_id=file.id,
            bot_scope=BOT_SCOPE,
            media_format=media_format,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=f"unique-{telegram_file_id}",
        )
    )


def inline_settings() -> Settings:
    return Settings(auth_jwt_secret=SecretStr("inline-service-test-secret-with-32-bytes"))


def build_service(
    session: AsyncSession,
    *,
    recommendation_service: TelegramInlineRecommendationService | None = None,
    redis: MemoryRedis | None = None,
) -> TelegramInlineService:
    settings = inline_settings()
    return TelegramInlineService(
        session,
        meme_search_service=UnusedMemeSearchService(),
        recommendation_service=recommendation_service or UnusedRecommendationService(),
        media_url_provider=FakeMediaUrlProvider(),
        bot_scope=BOT_SCOPE,
        inline_sessions=TelegramInlineSessionStore(redis=redis or MemoryRedis(), settings=settings),
        settings=settings,
    )


def build_text_service(session: AsyncSession, search_service: TelegramInlineMemeSearchService) -> TelegramInlineService:
    settings = inline_settings()
    return TelegramInlineService(
        session,
        meme_search_service=search_service,
        recommendation_service=UnusedRecommendationService(),
        media_url_provider=FakeMediaUrlProvider(),
        bot_scope=BOT_SCOPE,
        inline_sessions=TelegramInlineSessionStore(redis=MemoryRedis(), settings=settings),
        settings=settings,
    )


def recommendation_page_for(
    entries: list[tuple[Meme, MemeFile]],
    *,
    cursor: str | None = None,
    has_more: bool = False,
    total: int | None = None,
    request_id: str = "req_home_inline",
) -> RecommendationFeedPageRead:
    now = datetime.now(UTC)
    items = [
        PublicMemeSearchResultRead(
            meme=PublicMemeCardRead(
                id=meme.id,
                media_type=meme.media_type,
                language=meme.language,
                is_nsfw=meme.is_nsfw,
                popularity_score=0.5,
                like_count=meme.like_count,
                tags=list(meme.tags),
                primary_file=PublicMemeFileRead(
                    id=file.id,
                    mime_type=file.mime_type,
                    width=file.width,
                    height=file.height,
                    file_size_bytes=file.file_size_bytes,
                    blur_hash=file.blur_hash,
                    quality_score=file.quality_score,
                ),
                caption=None,
                created_at=meme.created_at or now,
                updated_at=meme.updated_at or now,
            ),
            attribution=MemeResultAttributionRead(
                request_id=request_id,
                surface="web_home",
                source_algorithm="personalized_recommendations",
                rank=rank,
                algorithm_version="personalized_v2",
                score=1.0,
                reason="multi_source_personalized",
            ),
        )
        for rank, (meme, file) in enumerate(entries, start=1)
    ]
    return RecommendationFeedPageRead(
        items=items,
        request_id=request_id,
        feed_session_id="home-inline-test",
        next_cursor=cursor,
        expires_at=now + timedelta(hours=2),
        has_more=has_more,
        limit=20,
        offset=0,
        total=len(items) if total is None else total,
    )


def assert_inline_page_attribution(
    page: TelegramInlineSearchPage,
    *,
    ranks: list[int],
    source_algorithms: list[str],
) -> None:
    request_id = page.request_id
    assert request_id.startswith("req_")
    impressions = [item.attribution.impression_id for item in page.items]
    assert len(impressions) == len(set(impressions))
    assert [item.attribution.request_id for item in page.items] == [request_id] * len(page.items)
    assert [item.attribution.rank for item in page.items] == ranks
    assert [item.attribution.source_algorithm for item in page.items] == source_algorithms


@pytest.mark.asyncio
async def test_text_query_creates_missing_telegram_user_and_searches_all_accessible_scope(
    migrated_db_session: AsyncSession,
) -> None:
    search_service = RecordingMemeSearchService()

    page = await build_text_service(migrated_db_session, search_service).search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="  cats  ",
        limit=7,
        offset=3,
    )

    created_user = await migrated_db_session.scalar(select(User).where(User.telegram_id == TELEGRAM_ID))
    assert created_user is not None
    assert created_user.account_type is AccountType.FULL
    assert created_user.status is AccountStatus.ACTIVE
    assert created_user.nsfw_enabled is False
    assert search_service.calls == [
        {
            "query": "cats",
            "viewer_user_id": created_user.id,
            "scope": MemeSearchScope.ALL,
            "include_nsfw": False,
            "limit": 7,
            "offset": 3,
            "surface": "telegram_inline_search",
        }
    ]
    assert page.is_personal is True
    assert page.request_id == "req_recording_inline_text"


@pytest.mark.asyncio
async def test_text_query_searches_private_and_shared_memes_only_for_authorized_telegram_user(
    migrated_db_session: AsyncSession,
) -> None:
    viewer = User(telegram_id=TELEGRAM_ID)
    stranger = User(email="inline-search-stranger@example.com")
    migrated_db_session.add_all([viewer, stranger])
    await migrated_db_session.flush()

    public_meme, public_file = await create_meme_file(migrated_db_session, is_public=True, popularity_score=10.0)
    authored_private, authored_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=30.0,
        uploader_user_id=viewer.id,
    )
    shared_private, shared_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=20.0,
        uploader_user_id=stranger.id,
    )
    unauthorized_private, unauthorized_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=100.0,
        uploader_user_id=stranger.id,
    )
    shared_collection = Collection(owner_id=stranger.id, title="Inline shared search")
    unauthorized_collection = Collection(owner_id=stranger.id, title="Inline unauthorized search")
    migrated_db_session.add_all([shared_collection, unauthorized_collection])
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            CollectionMember(
                collection_id=shared_collection.id,
                user_id=viewer.id,
                role=CollectionMembershipRole.VIEWER,
            ),
            CollectionMeme(collection_id=shared_collection.id, meme_id=shared_private.id, added_by_user_id=stranger.id),
            CollectionMeme(
                collection_id=unauthorized_collection.id,
                meme_id=unauthorized_private.id,
                added_by_user_id=stranger.id,
            ),
        ]
    )
    for file, cached_id in [
        (public_file, "cached-inline-search-public"),
        (authored_file, "cached-inline-search-authored"),
        (shared_file, "cached-inline-search-shared"),
        (unauthorized_file, "cached-inline-search-unauthorized"),
    ]:
        await add_file_id_cache(migrated_db_session, file=file, telegram_file_id=cached_id)
    await migrated_db_session.commit()

    service = build_text_service(migrated_db_session, MemeSearchService(migrated_db_session))
    authorized_page = await service.search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="private",
        limit=20,
        offset=0,
    )
    unauthorized_page = await service.search_inline_memes(
        telegram_user_id=TELEGRAM_ID + 1,
        query="private",
        limit=20,
        offset=0,
    )

    assert authorized_page.is_personal is True
    assert [item.meme.id for item in authorized_page.items] == [
        authored_private.id,
        shared_private.id,
        public_meme.id,
    ]
    assert unauthorized_page.is_personal is True
    assert [item.meme.id for item in unauthorized_page.items] == [public_meme.id]
    assert unauthorized_private.id not in {item.meme.id for item in authorized_page.items + unauthorized_page.items}


@pytest.mark.asyncio
async def test_empty_query_orders_pins_before_frozen_home_and_uses_opaque_cursor(
    migrated_db_session: AsyncSession,
) -> None:
    user = User(telegram_id=TELEGRAM_ID)
    migrated_db_session.add(user)
    await migrated_db_session.flush()

    pinned_duplicate, pinned_duplicate_file = await create_meme_file(
        migrated_db_session,
        popularity_score=90.0,
    )
    pinned_only, pinned_only_file = await create_meme_file(migrated_db_session, popularity_score=70.0)
    recent_only, recent_only_file = await create_meme_file(migrated_db_session, popularity_score=80.0)
    popular_only, popular_only_file = await create_meme_file(migrated_db_session, popularity_score=100.0)
    migrated_db_session.add_all(
        [
            PinnedMeme(user_id=user.id, meme_id=pinned_duplicate.id, position=1),
            PinnedMeme(user_id=user.id, meme_id=pinned_only.id, position=2),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(recent_only.id), "meme_file_id": str(recent_only_file.id)},
                occurred_at=datetime.now(UTC),
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(pinned_duplicate.id), "meme_file_id": str(pinned_duplicate_file.id)},
                occurred_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        ]
    )
    for file, cached_id in [
        (pinned_duplicate_file, "cached-pinned-duplicate"),
        (pinned_only_file, "cached-pinned-only"),
        (recent_only_file, "cached-recent-only"),
        (popular_only_file, "cached-popular-only"),
    ]:
        await add_file_id_cache(migrated_db_session, file=file, telegram_file_id=cached_id)
    await migrated_db_session.commit()

    recommendation_service = RecordingRecommendationService(
        {
            None: recommendation_page_for(
                [
                    (pinned_duplicate, pinned_duplicate_file),
                    (recent_only, recent_only_file),
                    (popular_only, popular_only_file),
                ]
            )
        }
    )
    redis = MemoryRedis()
    service = build_service(
        migrated_db_session,
        recommendation_service=recommendation_service,
        redis=redis,
    )
    page = await service.search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=2,
        offset=0,
    )
    assert page.next_cursor is not None
    next_page = await service.search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=2,
        cursor=page.next_cursor,
    )

    assert page.is_personal is True
    assert page.limit == 2
    assert page.offset == 0
    assert page.total == 5
    assert page.has_more is True
    assert len(page.next_cursor) <= 64
    assert not page.next_cursor.isdecimal()
    assert [item.cached_file_id for item in page.items] == [
        "cached-pinned-duplicate",
        "cached-pinned-only",
    ]
    assert_inline_page_attribution(
        page,
        ranks=[1, 2],
        source_algorithms=["explicit_pins", "explicit_pins"],
    )
    assert all(item.attribution.attribution_token for item in page.items)
    assert [item.cached_file_id for item in next_page.items] == [
        "cached-recent-only",
        "cached-popular-only",
    ]
    assert_inline_page_attribution(
        next_page,
        ranks=[3, 4],
        source_algorithms=["personalized_recommendations", "personalized_recommendations"],
    )
    assert next_page.total == 5
    assert next_page.has_more is False
    assert next_page.next_cursor is None
    assert recommendation_service.calls == [
        {
            "viewer_user_id": user.id,
            "scope": MemeSearchScope.PUBLIC,
            "include_nsfw": False,
            "limit": 20,
            "cursor": None,
            "offset": 0,
        }
    ]


@pytest.mark.asyncio
async def test_empty_query_for_new_and_inactive_user_returns_only_public_home_memes(
    migrated_db_session: AsyncSession,
) -> None:
    inactive_user = User(telegram_id=TELEGRAM_ID, status=AccountStatus.DELETION_PENDING)
    migrated_db_session.add(inactive_user)
    await migrated_db_session.flush()
    public_meme, public_file = await create_meme_file(migrated_db_session, is_public=True, popularity_score=50.0)
    private_meme, private_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=500.0,
        uploader_user_id=inactive_user.id,
    )
    await add_file_id_cache(migrated_db_session, file=public_file, telegram_file_id="cached-public")
    await add_file_id_cache(migrated_db_session, file=private_file, telegram_file_id="cached-private")
    await migrated_db_session.commit()

    recommendation_service = RecordingRecommendationService(
        {None: recommendation_page_for([(private_meme, private_file), (public_meme, public_file)])}
    )
    service = build_service(
        migrated_db_session,
        recommendation_service=recommendation_service,
        redis=MemoryRedis(),
    )
    new_user_telegram_id = 999_888
    new_user_page = await service.search_inline_memes(
        telegram_user_id=new_user_telegram_id,
        query="",
        limit=20,
        offset=0,
    )
    inactive_page = await service.search_inline_memes(telegram_user_id=TELEGRAM_ID, query="", limit=20, offset=0)
    created_user = await migrated_db_session.scalar(select(User).where(User.telegram_id == new_user_telegram_id))

    assert created_user is not None
    assert created_user.account_type is AccountType.FULL
    assert created_user.status is AccountStatus.ACTIVE
    assert created_user.nsfw_enabled is False
    assert new_user_page.is_personal is True
    assert inactive_page.is_personal is False
    assert [item.meme.id for item in new_user_page.items] == [public_meme.id]
    assert [item.meme.id for item in inactive_page.items] == [public_meme.id]
    assert_inline_page_attribution(
        new_user_page,
        ranks=[1],
        source_algorithms=["personalized_recommendations"],
    )
    assert_inline_page_attribution(
        inactive_page,
        ranks=[1],
        source_algorithms=["personalized_recommendations"],
    )
    assert private_meme.id not in {item.meme.id for item in new_user_page.items + inactive_page.items}
    assert all(call["scope"] is MemeSearchScope.PUBLIC for call in recommendation_service.calls)
    assert all(call["include_nsfw"] is False for call in recommendation_service.calls)


@pytest.mark.asyncio
async def test_empty_query_rechecks_public_visibility_for_pins_and_home(
    migrated_db_session: AsyncSession,
) -> None:
    viewer = User(telegram_id=TELEGRAM_ID)
    stranger = User(email="inline-stranger@example.com")
    migrated_db_session.add_all([viewer, stranger])
    await migrated_db_session.flush()

    authored_private, authored_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=20.0,
        uploader_user_id=viewer.id,
    )
    shared_private, shared_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=30.0,
        uploader_user_id=stranger.id,
    )
    public_meme, public_file = await create_meme_file(migrated_db_session, is_public=True, popularity_score=40.0)
    stranger_private, stranger_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=1000.0,
        uploader_user_id=stranger.id,
    )
    unauthorized_private, unauthorized_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=900.0,
        uploader_user_id=stranger.id,
    )
    viewer_collection = Collection(owner_id=viewer.id, title="Viewer private memes")
    shared_collection = Collection(owner_id=stranger.id, title="Shared with viewer")
    unauthorized_collection = Collection(owner_id=stranger.id, title="Not shared with viewer")
    migrated_db_session.add_all([viewer_collection, shared_collection, unauthorized_collection])
    await migrated_db_session.flush()
    migrated_db_session.add_all(
        [
            CollectionMember(
                collection_id=shared_collection.id,
                user_id=viewer.id,
                role=CollectionMembershipRole.VIEWER,
            ),
            CollectionMeme(
                collection_id=viewer_collection.id,
                meme_id=authored_private.id,
                added_by_user_id=viewer.id,
            ),
            CollectionMeme(collection_id=shared_collection.id, meme_id=shared_private.id, added_by_user_id=stranger.id),
            CollectionMeme(
                collection_id=unauthorized_collection.id,
                meme_id=unauthorized_private.id,
                added_by_user_id=stranger.id,
            ),
            PinnedMeme(user_id=viewer.id, meme_id=stranger_private.id, position=1),
            PinnedMeme(user_id=viewer.id, meme_id=authored_private.id, position=2),
            PinnedMeme(user_id=viewer.id, meme_id=shared_private.id, position=3),
            AnalyticsEvent(
                user_id=viewer.id,
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(shared_private.id), "meme_file_id": str(shared_file.id)},
                occurred_at=datetime.now(UTC),
            ),
            AnalyticsEvent(
                user_id=viewer.id,
                event_type=AnalyticsEventType.MEME_SEND,
                payload={"meme_id": str(unauthorized_private.id), "meme_file_id": str(unauthorized_file.id)},
                occurred_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        ]
    )
    for file, cached_id in [
        (authored_file, "cached-authored-private"),
        (shared_file, "cached-shared-private"),
        (public_file, "cached-public"),
        (stranger_file, "cached-stranger-private"),
        (unauthorized_file, "cached-unauthorized-private"),
    ]:
        await add_file_id_cache(migrated_db_session, file=file, telegram_file_id=cached_id)
    await migrated_db_session.commit()

    recommendation_service = RecordingRecommendationService(
        {
            None: recommendation_page_for(
                [
                    (authored_private, authored_file),
                    (shared_private, shared_file),
                    (public_meme, public_file),
                    (stranger_private, stranger_file),
                    (unauthorized_private, unauthorized_file),
                ]
            )
        }
    )
    page = await build_service(
        migrated_db_session,
        recommendation_service=recommendation_service,
        redis=MemoryRedis(),
    ).search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=20,
        offset=0,
    )

    assert page.is_personal is True
    assert [item.meme.id for item in page.items] == [
        authored_private.id,
        shared_private.id,
        public_meme.id,
    ]
    assert_inline_page_attribution(
        page,
        ranks=[1, 2, 3],
        source_algorithms=["explicit_pins", "explicit_pins", "personalized_recommendations"],
    )
    assert stranger_private.id not in {item.meme.id for item in page.items}
    assert unauthorized_private.id not in {item.meme.id for item in page.items}


@pytest.mark.asyncio
async def test_empty_query_filters_sendability_and_nsfw_before_filling_page(
    migrated_db_session: AsyncSession,
) -> None:
    user = User(telegram_id=TELEGRAM_ID, nsfw_enabled=False)
    migrated_db_session.add(user)
    await migrated_db_session.flush()
    video_meme, video_file = await create_meme_file(
        migrated_db_session,
        media_type=ContentKind.VIDEO,
        mime_type="video/webm",
    )
    nsfw_meme, nsfw_file = await create_meme_file(migrated_db_session, is_nsfw=True)
    private_meme, private_file = await create_meme_file(migrated_db_session, is_public=False)
    first_meme, first_file = await create_meme_file(migrated_db_session)
    second_meme, second_file = await create_meme_file(migrated_db_session)
    for file, cached_id in [
        (video_file, "cached-unsupported-video"),
        (nsfw_file, "cached-filtered-nsfw"),
        (private_file, "cached-filtered-private"),
        (first_file, "cached-first-sendable"),
        (second_file, "cached-second-sendable"),
    ]:
        await add_file_id_cache(migrated_db_session, file=file, telegram_file_id=cached_id)
    await migrated_db_session.commit()

    recommendation_service = RecordingRecommendationService(
        {
            None: recommendation_page_for(
                [
                    (video_meme, video_file),
                    (nsfw_meme, nsfw_file),
                    (private_meme, private_file),
                    (first_meme, first_file),
                ],
                cursor="home-next",
                has_more=True,
                total=5,
            ),
            "home-next": recommendation_page_for(
                [(second_meme, second_file)],
                total=5,
            ),
        }
    )
    page = await build_service(
        migrated_db_session,
        recommendation_service=recommendation_service,
        redis=MemoryRedis(),
    ).search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=2,
    )

    assert [item.cached_file_id for item in page.items] == [
        "cached-first-sendable",
        "cached-second-sendable",
    ]
    assert page.has_more is False
    assert [call["cursor"] for call in recommendation_service.calls] == [None, "home-next"]
    assert all(call["scope"] is MemeSearchScope.PUBLIC for call in recommendation_service.calls)
    assert all(call["include_nsfw"] is False for call in recommendation_service.calls)
