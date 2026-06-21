"""Focused tests for the Telegram inline service boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

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
from memexpert.schemas.meme import MemeSearchPageRead
from memexpert.services.meme_search import MemeSearchFilters, MemeSearchScope, MemeSearchService
from memexpert.services.telegram_inline import (
    TelegramInlineMemeSearchService,
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
    popularity_score: float = 0.0,
    author_user_id: uuid.UUID | None = None,
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
        author_user_id=author_user_id,
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


def build_service(session: AsyncSession) -> TelegramInlineService:
    return TelegramInlineService(
        session,
        meme_search_service=UnusedMemeSearchService(),
        media_url_provider=FakeMediaUrlProvider(),
        bot_scope=BOT_SCOPE,
    )


def build_text_service(session: AsyncSession, search_service: TelegramInlineMemeSearchService) -> TelegramInlineService:
    return TelegramInlineService(
        session,
        meme_search_service=search_service,
        media_url_provider=FakeMediaUrlProvider(),
        bot_scope=BOT_SCOPE,
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
        author_user_id=viewer.id,
    )
    shared_private, shared_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=20.0,
        author_user_id=stranger.id,
    )
    unauthorized_private, unauthorized_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=100.0,
        author_user_id=stranger.id,
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
async def test_empty_query_orders_pins_recent_popular_dedupes_before_pagination(
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

    page = await build_service(migrated_db_session).search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=2,
        offset=1,
    )
    next_page = await build_service(migrated_db_session).search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=2,
        offset=3,
    )

    assert page.is_personal is True
    assert page.limit == 2
    assert page.offset == 1
    assert page.total == 4
    assert page.has_more is True
    assert [item.cached_file_id for item in page.items] == ["cached-pinned-only", "cached-recent-only"]
    assert_inline_page_attribution(
        page,
        ranks=[2, 3],
        source_algorithms=["personalized_discovery", "personalized_discovery"],
    )
    assert [item.cached_file_id for item in next_page.items] == ["cached-popular-only"]
    assert_inline_page_attribution(next_page, ranks=[4], source_algorithms=["popular"])
    assert next_page.total == 4
    assert next_page.has_more is False


@pytest.mark.asyncio
async def test_empty_query_for_new_and_inactive_telegram_user_returns_only_public_popular_memes(
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
        author_user_id=inactive_user.id,
    )
    await add_file_id_cache(migrated_db_session, file=public_file, telegram_file_id="cached-public")
    await add_file_id_cache(migrated_db_session, file=private_file, telegram_file_id="cached-private")
    await migrated_db_session.commit()

    service = build_service(migrated_db_session)
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
    assert_inline_page_attribution(new_user_page, ranks=[1], source_algorithms=["popular"])
    assert_inline_page_attribution(inactive_page, ranks=[1], source_algorithms=["popular"])
    assert private_meme.id not in {item.meme.id for item in new_user_page.items + inactive_page.items}


@pytest.mark.asyncio
async def test_empty_query_filters_stale_pins_and_recent_events_by_visibility(
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
        author_user_id=viewer.id,
    )
    shared_private, shared_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=30.0,
        author_user_id=stranger.id,
    )
    public_meme, public_file = await create_meme_file(migrated_db_session, is_public=True, popularity_score=40.0)
    stranger_private, stranger_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=1000.0,
        author_user_id=stranger.id,
    )
    unauthorized_private, unauthorized_file = await create_meme_file(
        migrated_db_session,
        is_public=False,
        popularity_score=900.0,
        author_user_id=stranger.id,
    )
    shared_collection = Collection(owner_id=stranger.id, title="Shared with viewer")
    unauthorized_collection = Collection(owner_id=stranger.id, title="Not shared with viewer")
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
            PinnedMeme(user_id=viewer.id, meme_id=stranger_private.id, position=1),
            PinnedMeme(user_id=viewer.id, meme_id=authored_private.id, position=2),
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

    page = await build_service(migrated_db_session).search_inline_memes(
        telegram_user_id=TELEGRAM_ID,
        query="",
        limit=20,
        offset=0,
    )

    assert page.is_personal is True
    assert page.total == 3
    assert [item.meme.id for item in page.items] == [authored_private.id, shared_private.id, public_meme.id]
    assert_inline_page_attribution(
        page,
        ranks=[1, 2, 3],
        source_algorithms=["personalized_discovery", "personalized_discovery", "popular"],
    )
    assert stranger_private.id not in {item.meme.id for item in page.items}
    assert unauthorized_private.id not in {item.meme.id for item in page.items}
