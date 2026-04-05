# ruff: noqa: I001
"""Integration tests for ORM metadata registration and public schema contracts."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import configure_mappers, selectinload

from memexpert.models import metadata, utcnow
from memexpert.models.collection import Collection, CollectionInvite, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    EmbeddingCache,
    Meme,
    MemeFile,
    MemePopularitySnapshot,
    MemeSeoPage,
    MemeSource,
    MemeTemplate,
    SourceChannel,
    TelegramFileIdCache,
)
from memexpert.models.enums import (
    AccountDeletionAction,
    AccountStatus,
    AccountType,
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
    SourcePlatform,
    TelegramMediaFormat,
    UserLanguage,
)
from memexpert.models.user import (
    AccountDeletionLog,
    AccountMergeLog,
    AnalyticsEvent,
    ChannelSuggestion,
    InlineUsageEvent,
    RefreshToken,
    User,
)
from memexpert.schemas import CollectionRead, UserRead

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

EXPECTED_TABLES = {
    "account_deletion_logs",
    "account_merge_logs",
    "analytics_events",
    "channel_suggestions",
    "collection_invites",
    "collection_members",
    "collection_memes",
    "collections",
    "embedding_cache",
    "inline_usage_events",
    "meme_files",
    "meme_popularity_snapshots",
    "meme_seo_pages",
    "meme_sources",
    "meme_templates",
    "memes",
    "pinned_memes",
    "refresh_tokens",
    "source_channels",
    "telegram_file_id_cache",
    "users",
}


@pytest_asyncio.fixture
async def model_contract_session_factory(
    postgres_async_engine: AsyncEngine,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create a fresh metadata-managed schema for each model-contract test."""

    async with postgres_async_engine.begin() as connection:
        await connection.run_sync(metadata.drop_all, checkfirst=True)
        await connection.run_sync(metadata.create_all)

    try:
        yield postgres_session_factory
    finally:
        async with postgres_async_engine.begin() as connection:
            await connection.run_sync(metadata.drop_all, checkfirst=True)


async def _get_table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync_conn: set(sa_inspect(sync_conn).get_table_names()))


def _postgresql_where(index_name: str, table_name: str) -> str | None:
    table = metadata.tables[table_name]
    for index in table.indexes:
        if index.name == index_name:
            where_clause = index.dialect_options["postgresql"].get("where")
            return None if where_clause is None else str(where_clause)
    return None


def test_metadata_registers_all_expected_tables_and_relationships() -> None:
    configure_mappers()

    assert set(metadata.tables) == EXPECTED_TABLES
    assert "invite_link" not in metadata.tables["collections"].c
    assert metadata.tables["users"].c["active_save_collection_id"].foreign_keys
    assert metadata.tables["collections"].c["owner_id"].foreign_keys
    assert metadata.tables["memes"].c["primary_file_id"].foreign_keys
    assert metadata.tables["meme_files"].c["meme_id"].foreign_keys
    assert _postgresql_where("uq_collections_one_favorites_per_owner", "collections") == "kind = 'favorites'"
    assert _postgresql_where("uq_meme_files_single_primary_per_meme", "meme_files") == "is_primary"

    user_relationships = sa_inspect(User).relationships
    meme_relationships = sa_inspect(Meme).relationships

    assert user_relationships["active_save_collection"].mapper.class_ is Collection
    assert user_relationships["owned_collections"].mapper.class_ is Collection
    assert meme_relationships["files"].mapper.class_ is MemeFile
    assert meme_relationships["primary_file"].mapper.class_ is MemeFile


async def test_metadata_creates_full_schema_on_postgres(postgres_async_engine: AsyncEngine) -> None:
    async with postgres_async_engine.begin() as connection:
        await connection.run_sync(metadata.drop_all, checkfirst=True)
        await connection.run_sync(metadata.create_all)

    assert EXPECTED_TABLES.issubset(await _get_table_names(postgres_async_engine))

    async with postgres_async_engine.begin() as connection:
        await connection.run_sync(metadata.drop_all, checkfirst=True)


async def test_schema_handles_cycles_multi_invites_and_nullable_content_fields(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        owner = User(
            account_type=AccountType.FULL,
            status=AccountStatus.ACTIVE,
            email="owner@example.com",
            language=UserLanguage.ANY,
            last_active_at=utcnow(),
        )
        session.add(owner)
        await session.flush()

        favorites = Collection(
            owner=owner,
            title="Favorites",
            kind=CollectionKind.FAVORITES,
            visibility=CollectionVisibility.PRIVATE,
        )
        shared = Collection(
            owner=owner,
            title="Work Reactions",
            description="Shared board",
            kind=CollectionKind.CUSTOM,
            visibility=CollectionVisibility.UNLISTED,
        )
        session.add_all([favorites, shared])
        await session.flush()

        owner.active_save_collection = favorites
        session.add_all(
            [
                CollectionMember(collection=favorites, user=owner, role=CollectionMembershipRole.OWNER),
                CollectionMember(collection=shared, user=owner, role=CollectionMembershipRole.OWNER),
                CollectionInvite(
                    collection=shared,
                    created_by_user=owner,
                    token_hash="a" * 64,
                    role=CollectionMembershipRole.EDITOR,
                    channel=CollectionInviteChannel.DIRECT_LINK,
                    status=CollectionInviteStatus.PENDING,
                    max_uses=10,
                ),
                CollectionInvite(
                    collection=shared,
                    created_by_user=owner,
                    token_hash="b" * 64,
                    role=CollectionMembershipRole.VIEWER,
                    channel=CollectionInviteChannel.EMAIL,
                    label="Email viewers",
                    recipient_email="viewer@example.com",
                    status=CollectionInviteStatus.PENDING,
                ),
            ]
        )

        template = MemeTemplate(
            slug="drake-template",
            name="Drake Hotline Bling",
            description=None,
            is_curated=False,
            base_image_url=None,
            text_regions=None,
        )
        meme = Meme(
            media_type=ContentKind.IMAGE,
            language=ContentLanguage.EN,
            is_public=False,
            author=owner,
            template=template,
            tags=["reaction", "deadline"],
        )
        file_one = MemeFile(
            meme=meme,
            status=ContentProcessingStatus.READY,
            width=1200,
            height=900,
            file_size_bytes=1024,
            mime_type="image/jpeg",
            s3_original_key="files/1/original.jpg",
            perceptual_hash="c" * 16,
            quality_score=1.0,
            is_primary=True,
        )
        file_two = MemeFile(
            meme=meme,
            status=ContentProcessingStatus.READY,
            width=1080,
            height=810,
            file_size_bytes=900,
            mime_type="image/png",
            s3_original_key="files/2/original.png",
            quality_score=0.8,
            is_primary=False,
        )
        session.add_all([template, meme, file_one, file_two])
        await session.flush()

        meme.primary_file = file_one
        session.add_all(
            [
                MemeSeoPage(
                    meme=meme,
                    slug="drake-monday-deadline",
                    page_title="Drake Monday Deadline Meme",
                    meta_description="A deadline reaction meme.",
                    alt_text="Drake rejecting then approving",
                    caption=None,
                    body_text=None,
                    tags=["deadline", "reaction"],
                    model_id="gemini/gemini-2.5-flash",
                    prompt_version="seo-v3",
                ),
                MemeSource(
                    file=file_one,
                    platform=SourcePlatform.TELEGRAM,
                    source_id="memes_channel",
                    post_id="12345",
                    views=321,
                    reactions={"like": 10},
                    is_first_source=True,
                ),
                MemePopularitySnapshot(
                    meme=meme,
                    source_views=100,
                    source_reactions=12,
                    platform_views=50,
                    platform_sends=5,
                    platform_saves=3,
                    platform_likes=7,
                    popularity_score=1.25,
                ),
                TelegramFileIdCache(
                    meme_file=file_one,
                    bot_scope="main-bot",
                    media_format=TelegramMediaFormat.PHOTO,
                    telegram_file_id="AgACAgIAAxkBAAIBQGmock",
                    telegram_file_unique_id="AQADmock",
                ),
                EmbeddingCache(
                    input_hash="d" * 64,
                    input_type=EmbeddingInputType.IMAGE,
                    embedding=b"x" * 4096,
                    model_version="voyage-multimodal-3.5",
                    source_file=file_one,
                ),
                CollectionMeme(collection=shared, meme=meme, added_by_user=owner),
                PinnedMeme(user=owner, meme=meme, position=1),
                RefreshToken(
                    user=owner,
                    token_hash="e" * 64,
                    device_info="Firefox on Linux",
                    expires_at=utcnow() + timedelta(days=30),
                ),
                AnalyticsEvent(
                    user=owner,
                    event_type=AnalyticsEventType.MEME_VIEW,
                    payload={"meme_id": str(meme.id)},
                ),
                InlineUsageEvent(user=owner, group_hash="feedbeef1234"),
                ChannelSuggestion(
                    user=owner,
                    platform=SourcePlatform.TELEGRAM,
                    channel_url="https://t.me/memes_channel",
                ),
                AccountMergeLog(
                    guest_account_id=owner.id,
                    target_account_id=owner.id,
                    favorites_transferred=1,
                    views_transferred=2,
                    details={"reason": "upgrade"},
                ),
                AccountDeletionLog(
                    user_id=owner.id,
                    action=AccountDeletionAction.CANCELLED,
                    details={"restored": True},
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="987654",
                    username="memes_channel",
                    title="Memes Channel",
                    subscriber_count=1500,
                    is_active=True,
                    last_read_post_id="12345",
                    session_id="crawler-1",
                ),
            ]
        )
        await session.commit()

        result = await session.execute(
            select(User)
            .options(
                selectinload(User.active_save_collection),
                selectinload(User.owned_collections).selectinload(Collection.invites),
            )
            .where(User.id == owner.id)
        )
        persisted_owner = result.scalar_one()

        assert persisted_owner.active_save_collection is not None
        assert persisted_owner.active_save_collection.kind is CollectionKind.FAVORITES
        assert len(persisted_owner.owned_collections) == 2
        shared_collection = next(
            collection for collection in persisted_owner.owned_collections if collection.kind is CollectionKind.CUSTOM
        )
        assert len(shared_collection.invites) == 2

        meme_result = await session.execute(select(Meme).options(selectinload(Meme.files)).where(Meme.id == meme.id))
        persisted_meme = meme_result.scalar_one()
        assert persisted_meme.primary_file_id == file_one.id
        assert len(persisted_meme.files) == 2


async def test_constraints_reject_duplicate_provider_ids_and_duplicate_favorites(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        session.add_all(
            [
                User(account_type=AccountType.FULL, google_id="google-subject-1"),
                User(account_type=AccountType.FULL, google_id="google-subject-1"),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()

        await session.rollback()

    async with model_contract_session_factory() as session:
        owner = User(account_type=AccountType.FULL, email="favorites@example.com")
        session.add(owner)
        await session.flush()
        session.add_all(
            [
                Collection(owner=owner, title="Favorites", kind=CollectionKind.FAVORITES),
                Collection(owner=owner, title="More Favorites", kind=CollectionKind.FAVORITES),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


async def test_constraints_reject_multiple_primary_files_for_one_meme(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        meme = Meme(media_type=ContentKind.IMAGE)
        session.add(meme)
        await session.flush()
        session.add_all(
            [
                MemeFile(
                    meme=meme,
                    status=ContentProcessingStatus.READY,
                    s3_original_key="files/a/original.jpg",
                    is_primary=True,
                ),
                MemeFile(
                    meme=meme,
                    status=ContentProcessingStatus.READY,
                    s3_original_key="files/b/original.jpg",
                    is_primary=True,
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


def test_public_schemas_validate_from_attributes_and_reject_invalid_enums() -> None:
    now = utcnow()
    user_id = uuid.uuid7()
    collection_id = uuid.uuid7()

    user = User(
        id=user_id,
        account_type=AccountType.FULL,
        status=AccountStatus.ACTIVE,
        email="reader@example.com",
        language=UserLanguage.RU,
        nsfw_enabled=True,
        last_active_at=now,
        created_at=now,
        updated_at=now,
    )
    collection = Collection(
        id=collection_id,
        owner_id=user.id,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        visibility=CollectionVisibility.PRIVATE,
        created_at=now,
        updated_at=now,
        memberships=[
            CollectionMember(
                collection_id=collection_id,
                user_id=user.id,
                role=CollectionMembershipRole.OWNER,
                joined_at=now,
            )
        ],
        invites=[
            CollectionInvite(
                id=uuid.uuid7(),
                collection_id=collection_id,
                created_by_user_id=user.id,
                token_hash="f" * 64,
                role=CollectionMembershipRole.VIEWER,
                channel=CollectionInviteChannel.DIRECT_LINK,
                status=CollectionInviteStatus.PENDING,
                use_count=0,
                created_at=now,
                updated_at=now,
            )
        ],
    )

    user_payload = UserRead.model_validate(user).model_dump(mode="json")
    collection_payload = CollectionRead.model_validate(collection).model_dump(mode="json")

    assert user_payload["account_type"] == AccountType.FULL.value
    assert user_payload["language"] == UserLanguage.RU.value
    assert collection_payload["kind"] == CollectionKind.FAVORITES.value
    assert collection_payload["memberships"][0]["role"] == CollectionMembershipRole.OWNER.value
    assert collection_payload["invites"][0]["channel"] == CollectionInviteChannel.DIRECT_LINK.value

    with pytest.raises(ValidationError):
        UserRead.model_validate(
            {
                "id": user.id,
                "account_type": "broken",
                "status": AccountStatus.ACTIVE.value,
                "telegram_id": None,
                "google_id": None,
                "email": None,
                "email_verified_at": None,
                "active_save_collection_id": None,
                "nsfw_enabled": False,
                "language": UserLanguage.ANY.value,
                "last_active_at": None,
                "guest_expires_at": None,
                "deletion_requested_at": None,
                "deletion_due_at": None,
                "deleted_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
