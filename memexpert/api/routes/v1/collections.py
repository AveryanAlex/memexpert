# ruff: noqa: TC001,TC003
"""Collection management routes for the web/Mini App MVP."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from memexpert.api.dependencies import (
    AutoGuestUserDep,
    CollectionServiceDep,
    FullAccountUserDep,
    MemeSearchServiceDep,
)
from memexpert.models.enums import (
    CollectionInviteChannel,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
)
from memexpert.schemas.collection import (
    CollectionCapabilitiesRead,
    CollectionDetailRead,
    CollectionInviteLinkRead,
    CollectionListRead,
    CollectionRead,
    CollectionSavedMemeRead,
    WebCollectionSummaryRead,
)
from memexpert.schemas.user import UserRead
from memexpert.services import (
    CollectionNotFoundError,
    CollectionServiceError,
    CollectionVerificationRequiredError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    InvalidCollectionMembershipError,
    InvalidCollectionTitleError,
    UserNotFoundError,
)
from memexpert.services.meme_search import MemeNotFoundError

router = APIRouter(prefix="/collections", tags=["collections"])


class CollectionCreateRequest(BaseModel):
    """Payload for creating a custom collection."""

    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    visibility: CollectionVisibility = CollectionVisibility.PRIVATE


class CollectionUpdateRequest(CollectionCreateRequest):
    """Payload for owner-managed collection metadata updates."""


class CollectionInviteCreateRequest(BaseModel):
    """Payload for creating a direct-link invite."""

    role: CollectionMembershipRole = CollectionMembershipRole.VIEWER
    label: str | None = Field(default=None, max_length=120)
    max_uses: int | None = Field(default=1, ge=1)
    expires_in_hours: int | None = Field(default=168, ge=1, le=24 * 30)


class ActiveSaveUpdateResponse(BaseModel):
    """Response for active-save updates from collection surfaces."""

    active_save_collection_id: uuid.UUID | None


@router.get("", response_model=CollectionListRead, summary="List current user's collections")
async def list_collections(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
) -> CollectionListRead:
    """Return the caller's Favorites/custom collections with viewer capabilities."""

    try:
        active = await collection_service.get_active_save_collection(user_id=current_user.id)
        collections = await collection_service.list_collections_for_user(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return CollectionListRead(
        collections=[_summary(collection, current_user, active.id) for collection in collections],
        active_save_collection_id=active.id,
    )


@router.post(
    "",
    response_model=WebCollectionSummaryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create collection",
)
async def create_collection(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    payload: CollectionCreateRequest,
) -> WebCollectionSummaryRead:
    """Create a custom collection for a full account."""

    try:
        collection = await collection_service.create_custom_collection(
            owner_user_id=current_user.id,
            title=payload.title,
            description=payload.description,
            visibility=payload.visibility,
        )
        active = await collection_service.get_active_save_collection(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return _summary(collection, current_user, active.id)


@router.post("/invites/{token}/join", response_model=WebCollectionSummaryRead, summary="Join collection invite")
async def join_collection_invite(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    token: Annotated[str, Path(min_length=16, max_length=256)],
) -> WebCollectionSummaryRead:
    """Redeem a direct-link invite for the current full account."""

    try:
        collection = await collection_service.join_invite(token_hash=_hash_invite_token(token), user_id=current_user.id)
        active = await collection_service.get_active_save_collection(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return _summary(collection, current_user, active.id)


@router.get("/{collection_id}", response_model=CollectionDetailRead, summary="Read collection detail")
async def get_collection_detail(
    collection_service: CollectionServiceDep,
    meme_search_service: MemeSearchServiceDep,
    current_user: AutoGuestUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
) -> CollectionDetailRead:
    """Return collection metadata plus saved meme cards for members."""

    try:
        collection = await collection_service.get_collection_for_user(
            collection_id=collection_id,
            user_id=current_user.id,
        )
        saved_rows = await collection_service.list_collection_memes(
            collection_id=collection_id,
            user_id=current_user.id,
        )
        active = await collection_service.get_active_save_collection(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc

    cards = await meme_search_service.get_public_meme_cards_by_ids(
        tuple(row.meme_id for row in saved_rows),
        viewer_user_id=current_user.id,
        include_nsfw=current_user.nsfw_enabled,
    )
    cards_by_id = {card.id: card for card in cards}
    saved_memes = [
        CollectionSavedMemeRead(save=row, meme=card)
        for row in saved_rows
        if (card := cards_by_id.get(row.meme_id)) is not None
    ]
    return CollectionDetailRead(
        **_summary(collection, current_user, active.id).model_dump(),
        saved_memes=saved_memes,
    )


@router.patch("/{collection_id}", response_model=WebCollectionSummaryRead, summary="Update collection")
async def update_collection(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
    payload: CollectionUpdateRequest,
) -> WebCollectionSummaryRead:
    """Update owner-managed custom collection metadata."""

    try:
        collection = await collection_service.update_custom_collection(
            collection_id=collection_id,
            user_id=current_user.id,
            title=payload.title,
            description=payload.description,
            visibility=payload.visibility,
        )
        active = await collection_service.get_active_save_collection(user_id=current_user.id)
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return _summary(collection, current_user, active.id)


@router.delete("/{collection_id}", response_model=dict[str, bool], summary="Delete collection")
async def delete_collection(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
) -> dict[str, bool]:
    """Delete an owner-managed custom collection."""

    try:
        deleted = await collection_service.delete_custom_collection(
            collection_id=collection_id,
            user_id=current_user.id,
        )
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return {"deleted": deleted}


@router.put(
    "/{collection_id}/active-save",
    response_model=ActiveSaveUpdateResponse,
    summary="Set active save collection",
)
async def set_active_save_collection(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
) -> ActiveSaveUpdateResponse:
    """Point the caller's Save action at a writable collection."""

    try:
        user = await collection_service.update_active_save_collection(
            user_id=current_user.id,
            collection_id=collection_id,
        )
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return ActiveSaveUpdateResponse(active_save_collection_id=user.active_save_collection_id)


@router.post("/{collection_id}/memes/{meme_id}", response_model=dict[str, bool], summary="Save meme to collection")
async def save_meme_to_collection(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
    meme_id: Annotated[uuid.UUID, Path()],
) -> dict[str, bool]:
    """Save a visible meme into a specific writable collection."""

    try:
        _ = await collection_service.save_meme_to_collection(
            collection_id=collection_id,
            user_id=current_user.id,
            meme_id=meme_id,
        )
    except MemeNotFoundError as exc:
        raise _meme_not_found_http_error() from exc
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return {"saved": True}


@router.delete(
    "/{collection_id}/memes/{meme_id}",
    response_model=dict[str, bool],
    summary="Remove meme from collection",
)
async def remove_meme_from_collection(
    collection_service: CollectionServiceDep,
    current_user: AutoGuestUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
    meme_id: Annotated[uuid.UUID, Path()],
) -> dict[str, bool]:
    """Remove a meme from a specific writable collection."""

    try:
        removed = await collection_service.remove_meme_from_collection(
            collection_id=collection_id,
            user_id=current_user.id,
            meme_id=meme_id,
        )
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return {"removed": removed}


@router.post("/{collection_id}/invites", response_model=CollectionInviteLinkRead, summary="Create direct invite link")
async def create_collection_invite(
    collection_service: CollectionServiceDep,
    current_user: FullAccountUserDep,
    collection_id: Annotated[uuid.UUID, Path()],
    payload: CollectionInviteCreateRequest,
) -> CollectionInviteLinkRead:
    """Create a direct invite token for a writable custom collection."""

    token = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(UTC) + timedelta(hours=payload.expires_in_hours)
        if payload.expires_in_hours is not None
        else None
    )
    try:
        invite = await collection_service.create_invite(
            collection_id=collection_id,
            token_hash=_hash_invite_token(token),
            created_by_user_id=current_user.id,
            role=payload.role,
            channel=CollectionInviteChannel.DIRECT_LINK,
            label=payload.label,
            max_uses=payload.max_uses,
            expires_at=expires_at,
        )
    except CollectionServiceError as exc:
        raise _collection_http_error(exc) from exc
    return CollectionInviteLinkRead(invite=invite, token=token, join_path=f"/collection/invite/{token}")


def _summary(
    collection: CollectionRead,
    current_user: UserRead,
    active_save_collection_id: uuid.UUID | None,
) -> WebCollectionSummaryRead:
    role = _viewer_role(collection, current_user)
    capabilities = _capabilities(collection, current_user, role)
    return WebCollectionSummaryRead(
        collection=collection,
        viewer_role=role,
        capabilities=capabilities,
        active_save_collection_id=active_save_collection_id,
    )


def _viewer_role(collection: CollectionRead, current_user: UserRead) -> CollectionMembershipRole:
    if collection.owner_id == current_user.id:
        return CollectionMembershipRole.OWNER
    for membership in collection.memberships:
        if membership.user_id == current_user.id:
            return membership.role
    return CollectionMembershipRole.VIEWER


def _capabilities(
    collection: CollectionRead,
    current_user: UserRead,
    role: CollectionMembershipRole,
) -> CollectionCapabilitiesRead:
    can_write = role in {CollectionMembershipRole.OWNER, CollectionMembershipRole.EDITOR}
    is_owner_custom = role is CollectionMembershipRole.OWNER and collection.kind is CollectionKind.CUSTOM
    has_collaboration_identity = any(
        (
            current_user.telegram_id is not None,
            current_user.google_id is not None,
            current_user.email_verified_at is not None,
        )
    )
    can_invite = can_write and collection.kind is CollectionKind.CUSTOM and has_collaboration_identity
    return CollectionCapabilitiesRead(
        can_view=True,
        can_add_memes=can_write,
        can_remove_memes=can_write,
        can_rename=is_owner_custom,
        can_delete=is_owner_custom,
        can_create_invites=can_invite,
        can_set_active_save=can_write,
    )


def _hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _meme_not_found_http_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meme was not found.")


def _collection_http_error(exc: CollectionServiceError) -> HTTPException:
    if isinstance(exc, (CollectionNotFoundError, UserNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (CollectionWriteAccessError, GuestCollectionAccessError, CollectionVerificationRequiredError)):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, (DuplicateCollectionInviteError, InvalidCollectionMembershipError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (InvalidCollectionInviteError, InvalidCollectionTitleError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


__all__ = ["router"]
