# ruff: noqa: F821,TC003,UP037
"""Identity, lifecycle, audit, and analytics ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, case, func, or_, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from memexpert.models.enums import (
    AccountDeletionAction,
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    ChannelSuggestionStatus,
    SourcePlatform,
    UserLanguage,
    string_enum,
)

if TYPE_CHECKING:
    from memexpert.models.collection import Collection, CollectionInvite, CollectionMember, CollectionMeme, PinnedMeme
    from memexpert.models.content import Meme, ModerationDecision, ModerationReport


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Application account shared by guest and full users."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email IS NULL OR email = lower(email)", name="users_email_lowercase"),
        Index(
            "uq_users_telegram_id_not_null",
            "telegram_id",
            unique=True,
            postgresql_where=text("telegram_id IS NOT NULL"),
        ),
        Index(
            "uq_users_google_id_not_null",
            "google_id",
            unique=True,
            postgresql_where=text("google_id IS NOT NULL"),
        ),
        Index(
            "uq_users_email_not_null",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        """Apply security-sensitive defaults for transient ORM instances too."""

        kwargs.setdefault("is_admin", False)
        super().__init__(**kwargs)

    status: Mapped[AccountStatus] = mapped_column(
        string_enum(AccountStatus),
        default=AccountStatus.ACTIVE,
        nullable=False,
    )
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    google_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_save_collection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    nsfw_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    language: Mapped[UserLanguage] = mapped_column(
        string_enum(UserLanguage),
        default=UserLanguage.ANY,
        nullable=False,
    )
    last_active_at: Mapped[datetime | None] = mapped_column(nullable=True)
    guest_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deletion_due_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    token_nonce: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
        nullable=False,
    )

    @hybrid_property
    def has_linked_login_identity(self) -> bool:
        """Return whether this user has any durable login identity attached."""

        return (
            self.telegram_id is not None
            or self.google_id is not None
            or self.email is not None
            or (self.password_hash is not None and bool(self.password_hash.strip()))
        )

    @has_linked_login_identity.inplace.expression
    @classmethod
    def _has_linked_login_identity_expression(cls):
        return or_(
            cls.telegram_id.is_not(None),
            cls.google_id.is_not(None),
            cls.email.is_not(None),
            func.length(func.trim(cls.password_hash)) > 0,
        )

    @hybrid_property
    def account_type(self) -> AccountType:
        """Derived public account type based on linked login identities."""

        return AccountType.FULL if self.has_linked_login_identity else AccountType.GUEST

    @account_type.inplace.expression
    @classmethod
    def _account_type_expression(cls):
        return case(
            (cls.has_linked_login_identity, AccountType.FULL.value),
            else_=AccountType.GUEST.value,
        )

    active_save_collection: Mapped["Collection | None"] = relationship(
        "Collection",
        back_populates="active_for_users",
        foreign_keys=[active_save_collection_id],
        post_update=True,
    )
    owned_collections: Mapped[list["Collection"]] = relationship(
        "Collection",
        back_populates="owner",
        foreign_keys="Collection.owner_id",
    )
    memberships: Mapped[list["CollectionMember"]] = relationship(
        "CollectionMember",
        back_populates="user",
        foreign_keys="CollectionMember.user_id",
        cascade="all, delete-orphan",
    )
    collection_invites_created: Mapped[list["CollectionInvite"]] = relationship(
        "CollectionInvite",
        back_populates="created_by_user",
        foreign_keys="CollectionInvite.created_by_user_id",
    )
    saved_memes: Mapped[list["CollectionMeme"]] = relationship(
        "CollectionMeme",
        back_populates="added_by_user",
        foreign_keys="CollectionMeme.added_by_user_id",
    )
    pinned_memes: Mapped[list["PinnedMeme"]] = relationship(
        "PinnedMeme",
        back_populates="user",
        foreign_keys="PinnedMeme.user_id",
        cascade="all, delete-orphan",
    )
    login_events: Mapped[list["LoginEvent"]] = relationship(
        "LoginEvent",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    telegram_link_codes: Mapped[list["TelegramLinkCode"]] = relationship(
        "TelegramLinkCode",
        back_populates="guest_user",
        primaryjoin=lambda: User.id == foreign(TelegramLinkCode.guest_user_id),
        passive_deletes="all",
    )
    analytics_events: Mapped[list["AnalyticsEvent"]] = relationship(
        "AnalyticsEvent",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    inline_usage_events: Mapped[list["InlineUsageEvent"]] = relationship(
        "InlineUsageEvent",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    channel_suggestions: Mapped[list["ChannelSuggestion"]] = relationship(
        "ChannelSuggestion",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    authored_memes: Mapped[list["Meme"]] = relationship(
        "Meme",
        back_populates="author",
        foreign_keys="Meme.author_user_id",
    )
    moderation_reports_submitted: Mapped[list["ModerationReport"]] = relationship(
        "ModerationReport",
        back_populates="reporter_user",
        foreign_keys="ModerationReport.reporter_user_id",
    )
    moderation_reports_resolved: Mapped[list["ModerationReport"]] = relationship(
        "ModerationReport",
        back_populates="resolved_by_admin_user",
        foreign_keys="ModerationReport.resolved_by_admin_user_id",
    )
    moderation_decisions: Mapped[list["ModerationDecision"]] = relationship(
        "ModerationDecision",
        back_populates="admin_user",
        foreign_keys="ModerationDecision.admin_user_id",
    )


class LoginEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable audit row for every session issued to a user.

    Captured on every call to ``AuthService.issue_session_for_user`` so
    operators can review per-user sign-in history (IP, user-agent, when).
    Rows are never mutated after insert — ``token_nonce`` bumping handles
    revocation at the JWT layer, so login events stay immutable.
    """

    __tablename__ = "login_events"
    __table_args__ = (
        Index("ix_login_events_user_id_occurred_at", "user_id", "occurred_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        default=utcnow,
        server_default=text("now()"),
        nullable=False,
    )

    user: Mapped["User"] = relationship("User", back_populates="login_events")


class TelegramLinkCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persisted opaque Telegram start-link codes for guest-to-full linking."""

    __tablename__ = "telegram_link_codes"
    __table_args__ = (
        Index("uq_telegram_link_codes_code_hash", "code_hash", unique=True),
        Index("ix_telegram_link_codes_guest_user_id_expires_at", "guest_user_id", "expires_at"),
        Index(
            "ix_telegram_link_codes_redeemed_by_telegram_id_redeemed_at",
            "redeemed_by_telegram_id",
            "redeemed_at",
        ),
    )

    guest_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    redeemed_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    guest_user: Mapped["User"] = relationship(
        "User",
        back_populates="telegram_link_codes",
        primaryjoin=lambda: foreign(TelegramLinkCode.guest_user_id) == User.id,
    )


class AccountMergeLog(UUIDPrimaryKeyMixin, Base):
    """Audit records for guest-to-full account merge operations."""

    __tablename__ = "account_merge_logs"
    __table_args__ = (
        Index("ix_account_merge_logs_target_account_id_created_at", "target_account_id", "created_at"),
    )

    guest_account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    favorites_transferred: Mapped[int] = mapped_column(default=0, nullable=False)
    views_transferred: Mapped[int] = mapped_column(default=0, nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class AccountDeletionLog(UUIDPrimaryKeyMixin, Base):
    """Audit log for deletion requests and hard-delete processing."""

    __tablename__ = "account_deletion_logs"
    __table_args__ = (
        Index("ix_account_deletion_logs_user_id_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[AccountDeletionAction] = mapped_column(
        string_enum(AccountDeletionAction),
        nullable=False,
    )
    details: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class AnalyticsEvent(UUIDPrimaryKeyMixin, Base):
    """General-purpose product analytics event stream stored in PostgreSQL."""

    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_events_user_id_occurred_at", "user_id", "occurred_at"),
        Index("ix_analytics_events_event_type_occurred_at", "event_type", "occurred_at"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[AnalyticsEventType] = mapped_column(
        string_enum(AnalyticsEventType),
        nullable=False,
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    user: Mapped["User | None"] = relationship("User", back_populates="analytics_events")


class InlineUsageEvent(UUIDPrimaryKeyMixin, Base):
    """Inline-bot usage telemetry used for viral-coefficient measurement."""

    __tablename__ = "inline_usage_events"
    __table_args__ = (
        Index("ix_inline_usage_events_user_id_occurred_at", "user_id", "occurred_at"),
        Index("ix_inline_usage_events_group_hash_occurred_at", "group_hash", "occurred_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="inline_usage_events")


class ChannelSuggestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """User-submitted channel suggestions for future crawl coverage."""

    __tablename__ = "channel_suggestions"
    __table_args__ = (
        Index("ix_channel_suggestions_status_created_at", "status", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SourcePlatform] = mapped_column(
        string_enum(SourcePlatform),
        nullable=False,
    )
    channel_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ChannelSuggestionStatus] = mapped_column(
        string_enum(ChannelSuggestionStatus),
        default=ChannelSuggestionStatus.PENDING,
        nullable=False,
    )
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="channel_suggestions")


__all__ = [
    "AccountDeletionLog",
    "AccountMergeLog",
    "AnalyticsEvent",
    "ChannelSuggestion",
    "InlineUsageEvent",
    "LoginEvent",
    "TelegramLinkCode",
    "User",
]
