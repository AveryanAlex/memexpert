"""Collection and invite service primitives with membership and active-save invariants."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.models.collection import Collection, CollectionInvite, CollectionMember
from memexpert.models.enums import (
    AccountType,
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
)
from memexpert.models.user import User
from memexpert.schemas import CollectionInviteRead, CollectionMemberRead, CollectionRead, UserRead
from memexpert.services.errors import (
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
from memexpert.services.user_service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_COLLECTION_TITLE_LENGTH: Final = 120
MAX_COLLECTION_LABEL_LENGTH: Final = 120
MAX_INVITE_TOKEN_HASH_LENGTH: Final = 64
WRITE_ROLES: Final = frozenset({CollectionMembershipRole.OWNER, CollectionMembershipRole.EDITOR})


class CollectionService:
    """Service-layer helpers for custom collections, memberships, invites, and active-save state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session: AsyncSession = session

    async def get_collection(self, collection_id: object) -> CollectionRead | None:
        """Return a collection with memberships and invites if it exists."""

        collection = await self._get_collection_model(collection_id)
        return None if collection is None else CollectionRead.model_validate(collection)

    async def create_custom_collection(
        self,
        *,
        owner_user_id: object,
        title: str,
        description: str | None = None,
        visibility: CollectionVisibility | str = CollectionVisibility.PRIVATE,
    ) -> CollectionRead:
        """Create a custom collection for a full account and bootstrap owner membership."""

        owner = await self._get_user_model(owner_user_id)
        if owner is None:
            raise UserNotFoundError(f"User {owner_user_id} does not exist.")
        if owner.account_type is AccountType.GUEST:
            raise GuestCollectionAccessError("Guest accounts cannot create custom collections.")

        collection = Collection(
            owner_id=owner.id,
            title=_normalize_collection_title(title),
            description=_normalize_optional_text(description),
            kind=CollectionKind.CUSTOM,
            visibility=_resolve_visibility(visibility),
        )
        self._session.add(collection)
        await self._session.flush()

        self._session.add(
            CollectionMember(
                collection_id=collection.id,
                user_id=owner.id,
                role=CollectionMembershipRole.OWNER,
            )
        )

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to persist the custom collection.") from exc

        persisted_collection = await self._get_collection_model(collection.id)
        if persisted_collection is None:  # pragma: no cover - defensive branch
            raise CollectionServiceError("Created collection could not be reloaded.")
        return CollectionRead.model_validate(persisted_collection)

    async def ensure_member(
        self,
        *,
        collection_id: object,
        user_id: object,
        role: CollectionMembershipRole | str,
    ) -> CollectionMemberRead:
        """Insert or update a membership row while preserving the owner invariant."""

        collection = await self._get_collection_model(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {collection_id} does not exist.")

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        resolved_role = _resolve_membership_role(role)
        if collection.owner_id == user.id:
            resolved_role = CollectionMembershipRole.OWNER
        elif resolved_role is CollectionMembershipRole.OWNER:
            raise InvalidCollectionMembershipError(
                "Only the collection owner can hold the owner membership role.",
            )

        result = await self._session.execute(
            select(CollectionMember).where(
                and_(
                    CollectionMember.collection_id == collection.id,
                    CollectionMember.user_id == user.id,
                )
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            membership = CollectionMember(
                collection_id=collection.id,
                user_id=user.id,
                role=resolved_role,
            )
            self._session.add(membership)
        else:
            membership.role = resolved_role

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to persist the collection membership.") from exc

        return CollectionMemberRead.model_validate(membership)

    async def create_invite(
        self,
        *,
        collection_id: object,
        token_hash: str,
        created_by_user_id: object | None = None,
        role: CollectionMembershipRole | str = CollectionMembershipRole.VIEWER,
        channel: CollectionInviteChannel | str = CollectionInviteChannel.DIRECT_LINK,
        label: str | None = None,
        max_uses: int | None = None,
        expires_at: datetime | None = None,
        recipient_email: str | None = None,
    ) -> CollectionInviteRead:
        """Create a reusable invite record for a collection."""

        collection = await self._get_collection_model(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {collection_id} does not exist.")

        creator: User | None = None
        if created_by_user_id is not None:
            creator = await self._get_user_model(created_by_user_id)
            if creator is None:
                raise UserNotFoundError(f"User {created_by_user_id} does not exist.")
            _ensure_user_can_collaborate(creator)
            if not _user_can_write_collection(creator.id, collection):
                raise CollectionWriteAccessError(
                    f"User {creator.id} cannot create invites for collection {collection.id}.",
                )

        resolved_role = _resolve_membership_role(role)
        if resolved_role is CollectionMembershipRole.OWNER:
            raise InvalidCollectionInviteError("Invite links cannot grant the owner role.")

        resolved_channel = _resolve_invite_channel(channel)
        normalized_token_hash = _normalize_token_hash(token_hash)
        normalized_label = _normalize_optional_label(label)
        normalized_max_uses = _normalize_max_uses(max_uses)
        normalized_recipient_email = (
            UserService.normalize_email(recipient_email)
            if recipient_email is not None
            else None
        )

        if resolved_channel is CollectionInviteChannel.EMAIL and normalized_recipient_email is None:
            raise InvalidCollectionInviteError("Email invites require a recipient_email value.")
        if expires_at is not None and expires_at <= datetime.now(expires_at.tzinfo):
            raise InvalidCollectionInviteError("Invite expiry must be in the future.")

        invite = CollectionInvite(
            collection_id=collection.id,
            created_by_user_id=None if creator is None else creator.id,
            token_hash=normalized_token_hash,
            role=resolved_role,
            channel=resolved_channel,
            label=normalized_label,
            status=CollectionInviteStatus.PENDING,
            max_uses=normalized_max_uses,
            expires_at=expires_at,
            recipient_email=normalized_recipient_email,
        )
        self._session.add(invite)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _integrity_constraint_name(exc) == "uq_collection_invites_token_hash":
                raise DuplicateCollectionInviteError("Invite token hash already exists.") from exc
            raise CollectionServiceError("Failed to persist the collection invite.") from exc

        return CollectionInviteRead.model_validate(invite)

    async def update_active_save_collection(
        self,
        *,
        user_id: object,
        collection_id: object,
    ) -> UserRead:
        """Point a user's active save destination at a writable collection."""

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        collection = await self._get_collection_model(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {collection_id} does not exist.")

        if user.account_type is AccountType.GUEST and collection.kind is not CollectionKind.FAVORITES:
            raise GuestCollectionAccessError(
                "Guest accounts can only use Favorites as the active save collection.",
            )
        if not _user_can_write_collection(user.id, collection):
            raise CollectionWriteAccessError(
                f"User {user.id} cannot write to collection {collection.id}.",
            )

        user.active_save_collection_id = collection.id
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to update the active save collection.") from exc

        return UserRead.model_validate(user)

    async def _get_collection_model(self, collection_id: object) -> Collection | None:
        result = await self._session.execute(
            select(Collection)
            .options(
                selectinload(Collection.memberships),
                selectinload(Collection.invites),
            )
            .where(Collection.id == collection_id)
        )
        return result.scalar_one_or_none()

    async def _get_user_model(self, user_id: object) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


def _normalize_collection_title(title: str) -> str:
    normalized_title = title.strip()
    if not normalized_title:
        raise InvalidCollectionTitleError("Collection title cannot be blank.")
    if len(normalized_title) > MAX_COLLECTION_TITLE_LENGTH:
        raise InvalidCollectionTitleError(
            f"Collection title must be at most {MAX_COLLECTION_TITLE_LENGTH} characters long.",
        )
    return normalized_title


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized_value = value.strip()
    return normalized_value or None


def _normalize_optional_label(label: str | None) -> str | None:
    normalized_label = _normalize_optional_text(label)
    if normalized_label is None:
        return None
    if len(normalized_label) > MAX_COLLECTION_LABEL_LENGTH:
        raise InvalidCollectionInviteError(
            f"Invite label must be at most {MAX_COLLECTION_LABEL_LENGTH} characters long.",
        )
    return normalized_label


def _normalize_token_hash(token_hash: str) -> str:
    normalized_token_hash = token_hash.strip()
    if not normalized_token_hash:
        raise InvalidCollectionInviteError("Invite token hash cannot be blank.")
    if len(normalized_token_hash) > MAX_INVITE_TOKEN_HASH_LENGTH:
        raise InvalidCollectionInviteError(
            f"Invite token hash must be at most {MAX_INVITE_TOKEN_HASH_LENGTH} characters long.",
        )
    return normalized_token_hash


def _normalize_max_uses(max_uses: int | None) -> int | None:
    if max_uses is None:
        return None
    if max_uses <= 0:
        raise InvalidCollectionInviteError("Invite max_uses must be greater than zero.")
    return max_uses


def _resolve_visibility(visibility: CollectionVisibility | str) -> CollectionVisibility:
    try:
        return visibility if isinstance(visibility, CollectionVisibility) else CollectionVisibility(visibility)
    except ValueError as exc:
        raise InvalidCollectionTitleError(f"Unsupported collection visibility {visibility!r}.") from exc


def _resolve_membership_role(role: CollectionMembershipRole | str) -> CollectionMembershipRole:
    try:
        return role if isinstance(role, CollectionMembershipRole) else CollectionMembershipRole(role)
    except ValueError as exc:
        raise InvalidCollectionMembershipError(f"Unsupported collection membership role {role!r}.") from exc


def _resolve_invite_channel(channel: CollectionInviteChannel | str) -> CollectionInviteChannel:
    try:
        return channel if isinstance(channel, CollectionInviteChannel) else CollectionInviteChannel(channel)
    except ValueError as exc:
        raise InvalidCollectionInviteError(f"Unsupported collection invite channel {channel!r}.") from exc


def _ensure_user_can_collaborate(user: User) -> None:
    if user.account_type is AccountType.GUEST:
        raise GuestCollectionAccessError("Guest accounts cannot create collection invites.")
    if user.telegram_id is not None or user.google_id is not None or user.email_verified_at is not None:
        return
    raise CollectionVerificationRequiredError(
        "Collection collaboration requires a verified email or linked Telegram/Google identity.",
    )


def _user_can_write_collection(user_id: object, collection: Collection) -> bool:
    if collection.owner_id == user_id:
        return True
    return any(
        membership.user_id == user_id and membership.role in WRITE_ROLES
        for membership in collection.memberships
    )


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    candidates = [exc.orig, getattr(exc.orig, "__cause__", None)]
    for candidate in candidates:
        if candidate is None:
            continue

        constraint_name = getattr(candidate, "constraint_name", None)
        if isinstance(constraint_name, str):
            return constraint_name

        diag = getattr(candidate, "diag", None)
        diag_constraint_name = getattr(diag, "constraint_name", None)
        if isinstance(diag_constraint_name, str):
            return diag_constraint_name

    return None


__all__ = [
    "CollectionService",
    "MAX_COLLECTION_TITLE_LENGTH",
]
