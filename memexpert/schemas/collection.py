# ruff: noqa: TC001,TC003
"""Collection-facing service and API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from memexpert.models.enums import (
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
)
from memexpert.schemas.base import ORMSchema
from memexpert.schemas.meme import PublicMemeCardRead


class CollectionMemberRead(ORMSchema):
    """DTO for collection membership rows."""

    collection_id: uuid.UUID
    user_id: uuid.UUID
    role: CollectionMembershipRole
    joined_at: datetime


class CollectionInviteRead(ORMSchema):
    """DTO for collection invite-link records."""

    id: uuid.UUID
    collection_id: uuid.UUID
    created_by_user_id: uuid.UUID | None
    role: CollectionMembershipRole
    channel: CollectionInviteChannel
    label: str | None
    status: CollectionInviteStatus
    max_uses: int | None
    use_count: int
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    recipient_email: str | None
    created_at: datetime
    updated_at: datetime


class CollectionRead(ORMSchema):
    """Primary collection DTO with memberships and invites for later services."""

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None
    kind: CollectionKind
    visibility: CollectionVisibility
    memberships: list[CollectionMemberRead] = Field(default_factory=list)
    invites: list[CollectionInviteRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CollectionMemeRead(ORMSchema):
    """DTO for a meme saved into a collection."""

    collection_id: uuid.UUID
    meme_id: uuid.UUID
    added_by_user_id: uuid.UUID | None
    added_at: datetime


class PinnedMemeRead(ORMSchema):
    """DTO for a user's pinned meme position."""

    user_id: uuid.UUID
    meme_id: uuid.UUID
    position: int
    pinned_at: datetime


class CollectionSummaryRead(ORMSchema):
    """Compact collection DTO for profile/library selectors and lists."""

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None
    kind: CollectionKind
    visibility: CollectionVisibility
    role: CollectionMembershipRole
    can_write: bool
    saved_meme_count: int
    created_at: datetime
    updated_at: datetime


class MemeLibraryRead(ORMSchema):
    """Renderable profile/library payload for web clients."""

    favorites: list[PublicMemeCardRead] = Field(default_factory=list)
    pinned_memes: list[PublicMemeCardRead] = Field(default_factory=list)
    collections: list[CollectionSummaryRead] = Field(default_factory=list)
    active_save_collection: CollectionSummaryRead | None = None


class CollectionCapabilitiesRead(ORMSchema):
    """Viewer-specific collection actions exposed to web clients."""

    can_view: bool
    can_add_memes: bool
    can_remove_memes: bool
    can_rename: bool
    can_delete: bool
    can_create_invites: bool
    can_revoke_invites: bool
    can_manage_members: bool
    can_set_active_save: bool


class WebCollectionSummaryRead(ORMSchema):
    """Collection metadata plus the caller's role/capabilities."""

    collection: CollectionRead
    viewer_role: CollectionMembershipRole
    capabilities: CollectionCapabilitiesRead
    active_save_collection_id: uuid.UUID | None


class CollectionSavedMemeRead(ORMSchema):
    """A saved meme card with collection-save metadata."""

    save: CollectionMemeRead
    meme: PublicMemeCardRead


class CollectionDetailRead(WebCollectionSummaryRead):
    """Collection detail payload for the web collection page."""

    saved_memes: list[CollectionSavedMemeRead] = Field(default_factory=list)


class CollectionListRead(ORMSchema):
    """Collection list payload including active-save state."""

    collections: list[WebCollectionSummaryRead] = Field(default_factory=list)
    active_save_collection_id: uuid.UUID | None


class CollectionInviteLinkRead(ORMSchema):
    """Direct-link invite response including the one-time plaintext token."""

    invite: CollectionInviteRead
    token: str
    join_path: str


__all__ = [
    "CollectionInviteRead",
    "CollectionCapabilitiesRead",
    "CollectionDetailRead",
    "CollectionInviteLinkRead",
    "CollectionListRead",
    "CollectionMemeRead",
    "CollectionMemberRead",
    "CollectionRead",
    "CollectionSummaryRead",
    "MemeLibraryRead",
    "CollectionSavedMemeRead",
    "PinnedMemeRead",
    "WebCollectionSummaryRead",
]
