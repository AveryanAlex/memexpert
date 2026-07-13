"""Target collection metadata and authorization helpers for private upload ingest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.enums import AccountType, CollectionKind, CollectionMembershipRole, IngestSourceKind
from memexpert.models.user import User
from memexpert.services.errors import PipelinePayloadValidationError

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

_WRITE_ROLES: Final = frozenset({CollectionMembershipRole.OWNER, CollectionMembershipRole.EDITOR})


async def validate_target_collection_write(
    session: AsyncSession,
    *,
    uploader_user_id: uuid.UUID | None,
    target_collection_id: uuid.UUID | None,
) -> Collection | None:
    """Validate that the uploader can write to the requested target collection."""

    if target_collection_id is None:
        return None
    if uploader_user_id is None:
        raise PipelinePayloadValidationError("uploader_user_id is required when target_collection_id is supplied.")

    user = await session.scalar(select(User).where(User.id == uploader_user_id))
    if user is None:
        raise PipelinePayloadValidationError(f"User {uploader_user_id} does not exist.")

    collection = await session.scalar(
        select(Collection)
        .options(selectinload(Collection.memberships))
        .where(Collection.id == target_collection_id)
        .execution_options(populate_existing=True)
    )
    if collection is None:
        raise PipelinePayloadValidationError(f"Collection {target_collection_id} does not exist.")
    if user.account_type is AccountType.GUEST and collection.kind is not CollectionKind.FAVORITES:
        raise PipelinePayloadValidationError("Guest accounts can only use Favorites as the active save collection.")
    if not _user_can_write_collection(user.id, collection):
        raise PipelinePayloadValidationError(f"User {user.id} cannot write to collection {collection.id}.")
    return collection


async def save_meme_to_target_collection(
    session: AsyncSession,
    *,
    uploader_user_id: uuid.UUID | None,
    target_collection_id: uuid.UUID | None,
    meme_id: uuid.UUID,
) -> None:
    """Insert the idempotent collection save row for a validated target collection."""

    collection = await validate_target_collection_write(
        session,
        uploader_user_id=uploader_user_id,
        target_collection_id=target_collection_id,
    )
    if collection is None:
        return

    await session.execute(
        pg_insert(CollectionMeme)
        .values(collection_id=collection.id, meme_id=meme_id, added_by_user_id=uploader_user_id)
        .on_conflict_do_nothing(index_elements=[CollectionMeme.collection_id, CollectionMeme.meme_id])
    )


async def resolve_target_collection_id(
    session: AsyncSession,
    *,
    source_kind: IngestSourceKind,
    uploader_user_id: uuid.UUID | None,
    target_collection_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Resolve user uploads to their active collection and validate explicit targets."""

    resolved_target = target_collection_id
    if source_kind is IngestSourceKind.USER_UPLOAD and resolved_target is None:
        if uploader_user_id is None:
            raise PipelinePayloadValidationError("uploader_user_id is required for user uploads.")
        user = await session.scalar(select(User).where(User.id == uploader_user_id))
        if user is None:
            raise PipelinePayloadValidationError(f"User {uploader_user_id} does not exist.")
        resolved_target = user.active_save_collection_id
        if resolved_target is None:
            raise PipelinePayloadValidationError("User upload has no active save collection.")

    await validate_target_collection_write(
        session,
        uploader_user_id=uploader_user_id,
        target_collection_id=resolved_target,
    )
    return resolved_target


def _user_can_write_collection(user_id: object, collection: Collection) -> bool:
    if collection.owner_id == user_id:
        return True
    return any(
        membership.user_id == user_id and membership.role in _WRITE_ROLES
        for membership in collection.memberships
    )


__all__ = [
    "save_meme_to_target_collection",
    "resolve_target_collection_id",
    "validate_target_collection_write",
]
