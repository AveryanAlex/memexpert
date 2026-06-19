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


class IngestFileOrigin(StrEnum):
    """Why a durable ``MemeFile`` row was created during ingest."""

    NEW_MEME = "new_meme"
    PHASH_EXACT_EXISTING_MEME = "phash_exact_existing_meme"
    BLOCKED_PERCEPTUAL_HASH = "blocked_perceptual_hash"


class SourceAttachReason(StrEnum):
    """Why a source observation was attached to a meme file."""

    NEW_FILE = "new_file"
    SHA256_EXACT_EXISTING_FILE = "sha256_exact_existing_file"
    PHASH_EXACT_NEW_FILE = "phash_exact_new_file"
    BLOCKED_SHA256_EXISTING_FILE = "blocked_sha256_existing_file"
    BLOCKED_PERCEPTUAL_HASH_NEW_FILE = "blocked_perceptual_hash_new_file"


class ContentPipelineStage(StrEnum):
    """Pipeline stages recorded in the DB-backed journal."""

    INGEST = "ingest"
    TRANSCODE = "transcode"
    OCR = "ocr"
    EMBED = "embed"
    CLASSIFY = "classify"
    SYNC_QDRANT = "sync_qdrant"
    SYNC_MEILI = "sync_meili"


class ContentPipelineStageStatus(StrEnum):
    """Latest-state outcomes recorded for each pipeline stage."""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class PipelineIngestRequestStatus(StrEnum):
    """Lifecycle states for raw ingest requests before content materialization."""

    ACCEPTED = "accepted"
    MEDIA_INSPECT_PENDING = "media_inspect_pending"
    MEDIA_INSPECTING = "media_inspecting"
    MATERIALIZED = "materialized"
    RESOLVED_SHA_DUPLICATE = "resolved_sha_duplicate"
    FAILED_INVALID_MEDIA = "failed_invalid_media"
    FAILED_BLOCKED_PHASH = "failed_blocked_phash"
    PUBLISH_FAILED = "publish_failed"


class RabbitMQOutboxMessageStatus(StrEnum):
    """Lifecycle states for durable RabbitMQ outbox messages."""

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


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


class ModerationReportStatus(StrEnum):
    """Lifecycle states for user/admin reports against memes."""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ModerationReason(StrEnum):
    """Normalized moderation reason taxonomy shown to admins."""

    COPYRIGHT = "copyright"
    HARASSMENT = "harassment"
    ILLEGAL = "illegal"
    NSFW = "nsfw"
    OTHER = "other"
    SPAM = "spam"


class ModerationAction(StrEnum):
    """Audited admin actions that may change meme moderation flags."""

    HIDE = "hide"
    HIDE_AND_MARK_NSFW = "hide_and_mark_nsfw"
    MARK_NSFW = "mark_nsfw"
    MARK_SFW = "mark_sfw"
    NO_ACTION = "no_action"
    TEMPLATE_OVERRIDE = "template_override"
    OVERRIDE_FLAGS = "override_flags"
    PUBLISH = "publish"


class EmbeddingInputType(StrEnum):
    """Embedding cache entry kinds persisted in PostgreSQL."""

    IMAGE = "image"
    TEXT = "text"


class TelegramMediaFormat(StrEnum):
    """Telegram delivery formats whose file_id values can be cached."""

    ANIMATION = "animation"
    PHOTO = "photo"


class SyncTargetKind(StrEnum):
    """External search targets that must be kept in sync with canonical meme truth.

    Each value maps to exactly one independent operational target. The per-target
    snapshot table, service stubs, and schema projections all use this enum so
    operators see Qdrant and Meilisearch as separate targets that can succeed,
    fail, and be replayed without bleeding into each other's state.
    """

    QDRANT = "qdrant"
    MEILISEARCH = "meilisearch"


class SyncTargetStatus(StrEnum):
    """Per-target sync lifecycle states for the S03 search-sync contract.

    This taxonomy is intentionally separate from ``ContentPipelineStageStatus``
    so sync progress and heavy-chain stage status never collide — operators must
    be able to see a stage finished while a target is still pending and still
    distinguish a transient sync failure from a heavy-stage failure.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SYNCED = "synced"
    FAILED = "failed"


class TelegramSessionStatus(StrEnum):
    """Operational lifecycle for a Telethon userbot session.

    Deliberately separate from ``SyncTargetStatus`` and
    ``ContentPipelineStageStatus`` so session health does not collide with
    sync-target or stage-journal semantics: an ``active`` session is
    connected and eligible for ingest work, ``flood_wait`` records a
    Telegram-enforced cooldown, ``quarantined`` documents that an operator
    pulled the session out of rotation, and ``stopped`` marks a session
    that was intentionally brought down.
    """

    ACTIVE = "active"
    FLOOD_WAIT = "flood_wait"
    QUARANTINED = "quarantined"
    STOPPED = "stopped"


class AnalyticsEventType(StrEnum):
    """General analytics event names recorded by product surfaces."""

    ACCOUNT_MERGE = "account_merge"
    AUTH_EVENT = "auth_event"
    CHANNEL_SUGGEST = "channel_suggest"
    CLICK = "click"
    COLLECTION_ACTION = "collection_action"
    FAVORITE = "favorite"
    IMPRESSION = "impression"
    INLINE_CHOSEN = "inline_chosen"
    INLINE_QUERY = "inline_query"
    INLINE_SENT = "inline_sent"
    INLINE_SERVED = "inline_served"
    MEME_DETAIL_CLICK = "meme_detail_click"
    MEME_DOWNLOAD = "meme_download"
    MEME_IMPRESSION = "meme_impression"
    MEME_LIKE = "meme_like"
    MEME_PIN = "meme_pin"
    MEME_REPORT = "meme_report"
    MEME_SAVE = "meme_save"
    MEME_SEND = "meme_send"
    MEME_SHARE = "meme_share"
    MEME_VIEW = "meme_view"
    MINIAPP_OPEN = "miniapp_open"
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
    "IngestFileOrigin",
    "ModerationAction",
    "ModerationReason",
    "ModerationReportStatus",
    "SourcePlatform",
    "SourceAttachReason",
    "SyncTargetKind",
    "SyncTargetStatus",
    "TelegramMediaFormat",
    "TelegramSessionStatus",
    "UserLanguage",
    "string_enum",
]
