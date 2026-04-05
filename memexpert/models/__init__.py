"""Shared ORM metadata surface for services, tests, and Alembic."""

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from memexpert.models.enums import (
    AccountType,
    AnalyticsEventType,
    AuthProvider,
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentSourceKind,
)

metadata = Base.metadata

__all__ = [
    "AccountType",
    "AnalyticsEventType",
    "AuthProvider",
    "Base",
    "CollectionInviteChannel",
    "CollectionInviteStatus",
    "CollectionKind",
    "CollectionMembershipRole",
    "CollectionVisibility",
    "ContentKind",
    "ContentSourceKind",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "metadata",
]
