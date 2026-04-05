# ruff: noqa: F821,TC003,UP037
"""Collection, membership, invite, and save-surface ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from memexpert.models.enums import (
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    string_enum,
)

if TYPE_CHECKING:
    from memexpert.models.content import Meme
    from memexpert.models.user import User


class Collection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """User-owned meme collections, including the system Favorites collection."""

    __tablename__ = "collections"
    __table_args__ = (
        CheckConstraint("char_length(trim(title)) > 0", name="collections_title_not_blank"),
        Index("ix_collections_owner_id_updated_at", "owner_id", "updated_at"),
        Index(
            "uq_collections_one_favorites_per_owner",
            "owner_id",
            unique=True,
            postgresql_where=text("kind = 'favorites'"),
        ),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[CollectionKind] = mapped_column(
        string_enum(CollectionKind),
        default=CollectionKind.CUSTOM,
        nullable=False,
    )
    visibility: Mapped[CollectionVisibility] = mapped_column(
        string_enum(CollectionVisibility),
        default=CollectionVisibility.PRIVATE,
        nullable=False,
    )

    owner: Mapped["User"] = relationship(
        "User",
        back_populates="owned_collections",
        foreign_keys=[owner_id],
    )
    active_for_users: Mapped[list["User"]] = relationship(
        "User",
        back_populates="active_save_collection",
        foreign_keys="User.active_save_collection_id",
    )
    memberships: Mapped[list["CollectionMember"]] = relationship(
        "CollectionMember",
        back_populates="collection",
        cascade="all, delete-orphan",
    )
    invites: Mapped[list["CollectionInvite"]] = relationship(
        "CollectionInvite",
        back_populates="collection",
        cascade="all, delete-orphan",
    )
    saved_memes: Mapped[list["CollectionMeme"]] = relationship(
        "CollectionMeme",
        back_populates="collection",
        cascade="all, delete-orphan",
    )


class CollectionMember(Base):
    """Membership rows controlling per-user access to a collection."""

    __tablename__ = "collection_members"
    __table_args__ = (
        Index("ix_collection_members_user_id_role", "user_id", "role"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[CollectionMembershipRole] = mapped_column(
        string_enum(CollectionMembershipRole),
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    collection: Mapped["Collection"] = relationship("Collection", back_populates="memberships")
    user: Mapped["User"] = relationship("User", back_populates="memberships")


class CollectionInvite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Multi-link invite records for shared collection access."""

    __tablename__ = "collection_invites"
    __table_args__ = (
        CheckConstraint("max_uses IS NULL OR max_uses > 0", name="collection_invites_max_uses_positive"),
        CheckConstraint("use_count >= 0", name="collection_invites_use_count_non_negative"),
        Index("uq_collection_invites_token_hash", "token_hash", unique=True),
        Index("ix_collection_invites_collection_id_status", "collection_id", "status"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[CollectionMembershipRole] = mapped_column(
        string_enum(CollectionMembershipRole),
        default=CollectionMembershipRole.VIEWER,
        nullable=False,
    )
    channel: Mapped[CollectionInviteChannel] = mapped_column(
        string_enum(CollectionInviteChannel),
        default=CollectionInviteChannel.DIRECT_LINK,
        nullable=False,
    )
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[CollectionInviteStatus] = mapped_column(
        string_enum(CollectionInviteStatus),
        default=CollectionInviteStatus.PENDING,
        nullable=False,
    )
    max_uses: Mapped[int | None] = mapped_column(nullable=True)
    use_count: Mapped[int] = mapped_column(default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    collection: Mapped["Collection"] = relationship("Collection", back_populates="invites")
    created_by_user: Mapped["User | None"] = relationship(
        "User",
        back_populates="collection_invites_created",
        foreign_keys=[created_by_user_id],
    )


class CollectionMeme(Base):
    """Association table mapping saved memes into collections."""

    __tablename__ = "collection_memes"
    __table_args__ = (
        Index("ix_collection_memes_collection_added_at", "collection_id", "added_at", "meme_id"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    added_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    collection: Mapped["Collection"] = relationship("Collection", back_populates="saved_memes")
    meme: Mapped["Meme"] = relationship("Meme", back_populates="saved_in_collections")
    added_by_user: Mapped["User | None"] = relationship(
        "User",
        back_populates="saved_memes",
        foreign_keys=[added_by_user_id],
    )


class PinnedMeme(Base):
    """User-specific pin order used for quick access surfaces."""

    __tablename__ = "pinned_memes"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 20", name="pinned_memes_position_range"),
        Index("uq_pinned_memes_user_id_position", "user_id", "position", unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(nullable=False)
    pinned_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="pinned_memes")
    meme: Mapped["Meme"] = relationship("Meme", back_populates="pinned_by_users")


__all__ = [
    "Collection",
    "CollectionInvite",
    "CollectionMember",
    "CollectionMeme",
    "PinnedMeme",
]
