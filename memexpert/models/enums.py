# ruff: noqa: UP047
"""String-backed enums and SQLAlchemy helpers shared across the ORM surface."""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from sqlalchemy import Enum as SQLEnum

EnumT = TypeVar("EnumT", bound=StrEnum)


def string_enum(enum_cls: type[EnumT]) -> SQLEnum:
    """Build a SQLAlchemy enum that stores string values and validates inputs."""

    return SQLEnum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class AccountType(StrEnum):
    """Supported account lifecycle types."""

    GUEST = "guest"
    FULL = "full"


class AccountStatus(StrEnum):
    """Lifecycle states for account availability and deletion grace periods."""

    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class AccountDeletionAction(StrEnum):
    """Audit-log actions recorded for account deletion flows."""

    DELETION_REQUESTED = "deletion_requested"
    GRACE_PERIOD_EXPIRED = "grace_period_expired"
    HARD_DELETED = "hard_deleted"
    CANCELLED = "cancelled"


class AuthProvider(StrEnum):
    """External or first-party identity providers."""

    GOOGLE = "google"
    PASSWORD = "password"
    TELEGRAM = "telegram"


class UserLanguage(StrEnum):
    """Language preferences available for end-user presentation surfaces."""

    ANY = "any"
    EN = "en"
    RU = "ru"


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


class ContentLanguage(StrEnum):
    """Detected language classifications for stored meme content."""

    EN = "en"
    MIXED = "mixed"
    NONE = "none"
    RU = "ru"


class ContentProcessingStatus(StrEnum):
    """Pipeline states for individual meme files."""

    FAILED = "failed"
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"


class ContentPipelineStage(StrEnum):
    """Pipeline stages recorded in the DB-backed journal."""

    INGEST = "ingest"
    TRANSCODE = "transcode"
    SYNC_QDRANT = "sync_qdrant"
    SYNC_MEILI = "sync_meili"


class ContentPipelineStageStatus(StrEnum):
    """Latest-state outcomes recorded for each pipeline stage."""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class ContentSourceKind(StrEnum):
    """Origin channels tracked for discovered content."""

    EXTERNAL_SITE = "external_site"
    MANUAL_UPLOAD = "manual_upload"
    TELEGRAM = "telegram"
    WEB_CRAWL = "web_crawl"


class SourcePlatform(StrEnum):
    """Supported upstream platforms for discovered content and channel suggestions."""

    REDDIT = "reddit"
    TELEGRAM = "telegram"
    VK = "vk"


class ChannelSuggestionStatus(StrEnum):
    """Moderation lifecycle for user-submitted source-channel suggestions."""

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class EmbeddingInputType(StrEnum):
    """Embedding cache entry kinds persisted in PostgreSQL."""

    IMAGE = "image"
    TEXT = "text"


class TelegramMediaFormat(StrEnum):
    """Telegram delivery formats whose file_id values can be cached."""

    ANIMATION = "animation"
    PHOTO = "photo"


class AnalyticsEventType(StrEnum):
    """General analytics event names recorded by product surfaces."""

    CLICK = "click"
    FAVORITE = "favorite"
    IMPRESSION = "impression"
    INLINE_QUERY = "inline_query"
    MEME_LIKE = "meme_like"
    MEME_SAVE = "meme_save"
    MEME_SEND = "meme_send"
    MEME_VIEW = "meme_view"
    SAVE = "save"
    SEARCH_QUERY = "search_query"
    SHARE = "share"
    VIEW = "view"


__all__ = [
    "AccountDeletionAction",
    "AccountStatus",
    "AccountType",
    "AnalyticsEventType",
    "AuthProvider",
    "ChannelSuggestionStatus",
    "CollectionInviteChannel",
    "CollectionInviteStatus",
    "CollectionKind",
    "CollectionMembershipRole",
    "CollectionVisibility",
    "ContentKind",
    "ContentLanguage",
    "ContentPipelineStage",
    "ContentPipelineStageStatus",
    "ContentProcessingStatus",
    "ContentSourceKind",
    "EmbeddingInputType",
    "SourcePlatform",
    "TelegramMediaFormat",
    "UserLanguage",
    "string_enum",
]
