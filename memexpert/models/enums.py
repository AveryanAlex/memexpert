"""String-backed enums shared across identity, collection, content, and analytics models."""

from __future__ import annotations

from enum import StrEnum


class AccountType(StrEnum):
    """Supported account lifecycle types."""

    GUEST = "guest"
    FULL = "full"


class AuthProvider(StrEnum):
    """External or first-party identity providers."""

    GOOGLE = "google"
    PASSWORD = "password"
    TELEGRAM = "telegram"


class CollectionKind(StrEnum):
    """The product-level purpose of a collection."""

    CUSTOM = "custom"
    FAVORITES = "favorites"


class CollectionVisibility(StrEnum):
    """Visibility modes for collections shared through the product."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class CollectionMembershipRole(StrEnum):
    """Roles granted to collaborators inside a collection."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class CollectionInviteStatus(StrEnum):
    """Lifecycle states for invite links and invite deliveries."""

    ACCEPTED = "accepted"
    EXPIRED = "expired"
    PENDING = "pending"
    REVOKED = "revoked"


class CollectionInviteChannel(StrEnum):
    """Delivery channels for collection invite flows."""

    DIRECT_LINK = "direct_link"
    EMAIL = "email"
    TELEGRAM = "telegram"


class ContentKind(StrEnum):
    """Content/media kinds stored by the content subsystem."""

    AUDIO = "audio"
    GIF = "gif"
    IMAGE = "image"
    LINK = "link"
    TEXT = "text"
    VIDEO = "video"


class ContentSourceKind(StrEnum):
    """Origin channels tracked for discovered content."""

    EXTERNAL_SITE = "external_site"
    MANUAL_UPLOAD = "manual_upload"
    TELEGRAM = "telegram"
    WEB_CRAWL = "web_crawl"


class AnalyticsEventType(StrEnum):
    """Top-level analytics events recorded by the product."""

    CLICK = "click"
    FAVORITE = "favorite"
    IMPRESSION = "impression"
    SAVE = "save"
    SHARE = "share"
    VIEW = "view"


__all__ = [
    "AccountType",
    "AnalyticsEventType",
    "AuthProvider",
    "CollectionInviteChannel",
    "CollectionInviteStatus",
    "CollectionKind",
    "CollectionMembershipRole",
    "CollectionVisibility",
    "ContentKind",
    "ContentSourceKind",
]
