"""Concurrency coverage for per-user pin mutation serialization."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from memexpert.models.collection import PinnedMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.services import CollectionService, UserService
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _create_meme(session: AsyncSession) -> Meme:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        is_public=True,
    )
    meme_file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        s3_original_key=f"pipeline/originals/test/{meme_id}.jpg",
        mime_type="image/jpeg",
        width=640,
        height=480,
        quality_score=0.8,
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    return meme


async def _pin_in_fresh_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    meme_id: uuid.UUID,
):
    async with session_factory() as session:
        return await CollectionService(session).pin_meme_result(user_id=user_id, meme_id=meme_id)


async def test_concurrent_duplicate_pin_requests_are_idempotent(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_full_user_via_upgrade(
        UserService(migrated_db_session),
        email="concurrent-duplicate-pin@example.com",
    )
    meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    first, second = await asyncio.gather(
        _pin_in_fresh_session(postgres_session_factory, user_id=user.id, meme_id=meme.id),
        _pin_in_fresh_session(postgres_session_factory, user_id=user.id, meme_id=meme.id),
    )

    assert sorted(result.changed for result in (first, second)) == [False, True]
    assert {result.item.meme_id for result in (first, second)} == {meme.id}
    assert {result.item.position for result in (first, second)} == {1}
    assert (
        await migrated_db_session.scalar(
            select(func.count()).select_from(PinnedMeme).where(PinnedMeme.user_id == user.id)
        )
        == 1
    )


async def test_concurrent_distinct_pins_receive_unique_positions(
    migrated_db_session: AsyncSession,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_full_user_via_upgrade(
        UserService(migrated_db_session),
        email="concurrent-distinct-pins@example.com",
    )
    first_meme = await _create_meme(migrated_db_session)
    second_meme = await _create_meme(migrated_db_session)
    await migrated_db_session.commit()

    first, second = await asyncio.gather(
        _pin_in_fresh_session(postgres_session_factory, user_id=user.id, meme_id=first_meme.id),
        _pin_in_fresh_session(postgres_session_factory, user_id=user.id, meme_id=second_meme.id),
    )

    assert first.changed is True
    assert second.changed is True
    assert {result.item.meme_id for result in (first, second)} == {first_meme.id, second_meme.id}
    assert {result.item.position for result in (first, second)} == {1, 2}
    persisted = list(
        await migrated_db_session.scalars(
            select(PinnedMeme).where(PinnedMeme.user_id == user.id).order_by(PinnedMeme.position.asc())
        )
    )
    assert [pin.position for pin in persisted] == [1, 2]
    assert {pin.meme_id for pin in persisted} == {first_meme.id, second_meme.id}
