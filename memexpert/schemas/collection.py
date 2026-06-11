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


__all__ = [
    "CollectionInviteRead",
    "CollectionMemeRead",
    "CollectionMemberRead",
    "CollectionRead",
    "PinnedMemeRead",
]
