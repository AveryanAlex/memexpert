"""Collection and invite service primitives with membership and active-save invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.models.collection import Collection, CollectionInvite, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import Meme
from memexpert.models.enums import (
    AccountType,
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
)
from memexpert.models.user import User
from memexpert.schemas import (
    CollectionInviteRead,
    CollectionMemberRead,
    CollectionMemeRead,
    CollectionRead,
    CollectionSummaryRead,
    MemeLibraryRead,
    PinnedMemeRead,
    UserRead,
)
from memexpert.services._integrity import integrity_constraint_name
from memexpert.services.errors import (
    CollectionNotFoundError,
    CollectionServiceError,
    CollectionVerificationRequiredError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    DuplicateFavoritesCollectionError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    InvalidCollectionMembershipError,
    InvalidCollectionTitleError,
    InvalidPinnedMemeOrderError,
    PinLimitExceededError,
    UserNotFoundError,
)
from memexpert.services.media_render_urls import MediaRenderUrlService
from memexpert.services.meme_search import MemeNotFoundError, MemeSearchService
from memexpert.services.user_service import UserService

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.schemas.meme import PublicMemeCardRead

MAX_COLLECTION_TITLE_LENGTH: Final = 120
MAX_COLLECTION_LABEL_LENGTH: Final = 120
MAX_INVITE_TOKEN_HASH_LENGTH: Final = 64
MAX_PINNED_MEMES: Final = 20
FAVORITES_TITLE: Final = "Favorites"
WRITE_ROLES: Final = frozenset({CollectionMembershipRole.OWNER, CollectionMembershipRole.EDITOR})


class CollectionService:
    """Service-layer helpers for custom collections, memberships, invites, and active-save state."""

    def __init__(self, session: AsyncSession, *, media_render_service: MediaRenderUrlService | None = None) -> None:
        self._session: AsyncSession = session
        self._media_render_service = media_render_service or MediaRenderUrlService()

    async def get_collection(self, collection_id: object) -> CollectionRead | None:
        """Return a collection with memberships and invites if it exists."""

        collection = await self._get_collection_model(collection_id)
        return None if collection is None else CollectionRead.model_validate(collection)

    async def ensure_favorites_collection(
        self,
        user_id: object,
        *,
        commit: bool = True,
    ) -> CollectionRead:
        """Return the caller's Favorites collection, creating it on first use.

        Lazy bootstrap — the Favorites row, its owner membership, and the
        user's ``active_save_collection_id`` pointer are only materialized
        when the user first interacts with a collection surface. One-shot
        visitors (crawlers, link previews, health checks) never allocate
        these rows.

        Concurrency-safe: takes a row lock on the user before inserting so
        two concurrent ensures converge on the same Favorites row. The
        ``uq_collections_one_favorites_per_owner`` unique index backs the
        invariant as a defensive fallback. ``commit=False`` lets account
        linking piggyback the bootstrap on an outer merge transaction.
        """

        user_service = UserService(self._session)
        user = await user_service.get_locked_user_record(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        existing_favorites = await user_service.get_locked_favorites_collection_record(user_id)
        if existing_favorites is not None:
            loaded = await self._get_collection_model(existing_favorites.id)
            if loaded is None:  # pragma: no cover - defensive branch
                raise CollectionServiceError("Existing Favorites could not be reloaded.")
            return CollectionRead.model_validate(loaded)

        favorites = Collection(
            owner_id=user.id,
            title=FAVORITES_TITLE,
            kind=CollectionKind.FAVORITES,
            visibility=CollectionVisibility.PRIVATE,
        )
        self._session.add(favorites)
        await self._session.flush()

        self._session.add(
            CollectionMember(
                collection_id=favorites.id,
                user_id=user.id,
                role=CollectionMembershipRole.OWNER,
            )
        )
        user.active_save_collection_id = favorites.id

        try:
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            if integrity_constraint_name(exc) == "uq_collections_one_favorites_per_owner":
                raise DuplicateFavoritesCollectionError(
                    f"User {user.id} already has a Favorites collection.",
                ) from exc
            raise CollectionServiceError("Failed to bootstrap the Favorites collection.") from exc

        persisted = await self._get_collection_model(favorites.id)
        if persisted is None:  # pragma: no cover - defensive branch
            raise CollectionServiceError("Created Favorites could not be reloaded.")
        return CollectionRead.model_validate(persisted)

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

    async def list_collections_for_user(self, *, user_id: object) -> list[CollectionRead]:
        """Return collections the user belongs to, newest-updated first, with Favorites ensured."""

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        _ = await self.ensure_favorites_collection(user.id)
        result = await self._session.execute(
            select(Collection)
            .options(selectinload(Collection.memberships), selectinload(Collection.invites))
            .join(CollectionMember, CollectionMember.collection_id == Collection.id)
            .where(CollectionMember.user_id == user.id)
            .order_by(Collection.kind.asc(), Collection.updated_at.desc(), Collection.title.asc())
        )
        return [CollectionRead.model_validate(collection) for collection in result.scalars().unique()]

    async def get_collection_for_user(self, *, collection_id: object, user_id: object) -> CollectionRead:
        """Return a collection only when the user has member-level read access."""

        user, collection = await self._get_collection_for_read(collection_id=collection_id, user_id=user_id)
        _ = user
        return CollectionRead.model_validate(collection)

    async def list_collection_memes(self, *, collection_id: object, user_id: object) -> list[CollectionMemeRead]:
        """Return saved meme rows for a collection the user can read."""

        _user, collection = await self._get_collection_for_read(collection_id=collection_id, user_id=user_id)
        result = await self._session.execute(
            select(CollectionMeme)
            .where(CollectionMeme.collection_id == collection.id)
            .order_by(CollectionMeme.added_at.desc(), CollectionMeme.meme_id.asc())
        )
        return [CollectionMemeRead.model_validate(row) for row in result.scalars()]

    async def update_custom_collection(
        self,
        *,
        collection_id: object,
        user_id: object,
        title: str,
        description: str | None = None,
        visibility: CollectionVisibility | str = CollectionVisibility.PRIVATE,
    ) -> CollectionRead:
        """Update owner-managed metadata for a custom collection."""

        user, collection = await self._get_collection_for_read(collection_id=collection_id, user_id=user_id)
        self._ensure_owner_can_manage_custom_collection(user, collection)
        collection.title = _normalize_collection_title(title)
        collection.description = _normalize_optional_text(description)
        collection.visibility = _resolve_visibility(visibility)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to update the collection.") from exc

        persisted = await self._get_collection_model(collection.id)
        if persisted is None:  # pragma: no cover - defensive branch
            raise CollectionServiceError("Updated collection could not be reloaded.")
        return CollectionRead.model_validate(persisted)

    async def delete_custom_collection(self, *, collection_id: object, user_id: object) -> bool:
        """Delete an owner-managed custom collection and clear active-save pointers."""

        user, collection = await self._get_collection_for_read(collection_id=collection_id, user_id=user_id)
        self._ensure_owner_can_manage_custom_collection(user, collection)
        await self._session.execute(
            update(User).where(User.active_save_collection_id == collection.id).values(active_save_collection_id=None)
        )
        await self._session.delete(collection)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to delete the collection.") from exc
        return True

    async def save_meme_to_collection(
        self,
        *,
        collection_id: object,
        user_id: object,
        meme_id: object,
    ) -> CollectionMemeRead:
        """Save a visible meme into a specific writable collection."""

        user, collection = await self._get_collection_for_read(collection_id=collection_id, user_id=user_id)
        self._ensure_can_write_collection(user, collection)
        meme = await self._get_visible_meme_model(meme_id=meme_id, viewer_user_id=user.id)
        if meme is None:
            raise MemeNotFoundError(f"Meme {meme_id} does not exist.")

        saved_meme, inserted = await self._insert_collection_meme(
            collection_id=collection.id,
            meme_id=meme.id,
            added_by_user_id=user.id,
        )
        if inserted and collection.kind is CollectionKind.FAVORITES:
            await self._increment_like_count(meme.id)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to save the meme to the collection.") from exc
        return CollectionMemeRead.model_validate(saved_meme)

    async def remove_meme_from_collection(self, *, collection_id: object, user_id: object, meme_id: object) -> bool:
        """Remove a meme from a specific writable collection when present."""

        user, collection = await self._get_collection_for_read(collection_id=collection_id, user_id=user_id)
        self._ensure_can_write_collection(user, collection)
        removed = await self._delete_collection_meme(collection_id=collection.id, meme_id=meme_id)
        if not removed:
            return False
        if collection.kind is CollectionKind.FAVORITES:
            await self._decrement_like_count(meme_id)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to remove the meme from the collection.") from exc
        return True

    async def favorite_meme(self, *, user_id: object, meme_id: object) -> CollectionMemeRead:
        """Save a meme to the user's Favorites collection and count the first favorite as a like."""

        meme = await self._get_visible_meme_model(meme_id=meme_id, viewer_user_id=user_id)
        if meme is None:
            raise MemeNotFoundError(f"Meme {meme_id} does not exist.")

        favorites = await self.ensure_favorites_collection(user_id, commit=False)
        saved_meme, inserted = await self._insert_collection_meme(
            collection_id=favorites.id,
            meme_id=meme.id,
            added_by_user_id=favorites.owner_id,
        )
        if inserted:
            await self._increment_like_count(meme.id)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to favorite the meme.") from exc

        return CollectionMemeRead.model_validate(saved_meme)

    async def unfavorite_meme(self, *, user_id: object, meme_id: object) -> bool:
        """Remove a meme from Favorites without bootstrapping an empty Favorites collection."""

        favorites = await self._get_favorites_collection_model(user_id)
        if favorites is None:
            return False

        removed = await self._delete_collection_meme(collection_id=favorites.id, meme_id=meme_id)
        if not removed:
            return False

        await self._decrement_like_count(meme_id)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to unfavorite the meme.") from exc
        return True

    async def list_favorite_memes(self, *, user_id: object) -> list[CollectionMemeRead]:
        """Return Favorites saves newest-first, or an empty list before lazy bootstrap."""

        favorites = await self._get_favorites_collection_model(user_id)
        if favorites is None:
            return []

        result = await self._session.execute(
            select(CollectionMeme)
            .where(CollectionMeme.collection_id == favorites.id)
            .order_by(CollectionMeme.added_at.desc(), CollectionMeme.meme_id.asc())
        )
        return [CollectionMemeRead.model_validate(row) for row in result.scalars()]

    async def get_meme_library(self, *, user_id: object) -> MemeLibraryRead:
        """Return renderable profile/library data for the caller."""

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        active_collection = await self.get_active_save_collection(user_id=user.id)
        collections = await self._list_collection_summaries(user)
        active_summary = next(
            (collection for collection in collections if collection.id == active_collection.id),
            None,
        )
        favorites = await self._load_favorite_cards(user.id)
        pinned_memes = await self._load_pinned_cards(user.id) if user.account_type is AccountType.FULL else []
        return MemeLibraryRead(
            favorites=favorites,
            pinned_memes=pinned_memes,
            collections=collections,
            active_save_collection=active_summary,
        )

    async def get_active_save_collection(self, *, user_id: object) -> CollectionRead:
        """Return the user's active save destination, defaulting to Favorites on first use."""

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        if user.active_save_collection_id is None:
            return await self.ensure_favorites_collection(user.id)

        collection = await self._get_collection_model(user.active_save_collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {user.active_save_collection_id} does not exist.")
        return CollectionRead.model_validate(collection)

    async def save_meme_to_active_collection(self, *, user_id: object, meme_id: object) -> CollectionMemeRead:
        """Save a meme into the user's writable active collection, lazily defaulting to Favorites."""

        user, collection = await self._get_active_collection_model_for_write(user_id)
        meme = await self._get_visible_meme_model(meme_id=meme_id, viewer_user_id=user.id)
        if meme is None:
            raise MemeNotFoundError(f"Meme {meme_id} does not exist.")

        saved_meme, inserted = await self._insert_collection_meme(
            collection_id=collection.id,
            meme_id=meme.id,
            added_by_user_id=user.id,
        )
        if inserted and collection.kind is CollectionKind.FAVORITES:
            await self._increment_like_count(meme.id)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to save the meme to the active collection.") from exc

        return CollectionMemeRead.model_validate(saved_meme)

    async def remove_meme_from_active_collection(self, *, user_id: object, meme_id: object) -> bool:
        """Remove a meme from the user's active save collection when present."""

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        if user.active_save_collection_id is None:
            return False

        collection = await self._get_collection_model(user.active_save_collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {user.active_save_collection_id} does not exist.")
        self._ensure_can_write_collection(user, collection)

        removed = await self._delete_collection_meme(collection_id=collection.id, meme_id=meme_id)
        if not removed:
            return False

        if collection.kind is CollectionKind.FAVORITES:
            await self._decrement_like_count(meme_id)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to remove the meme from the active collection.") from exc
        return True

    async def list_pinned_memes(self, *, user_id: object) -> list[PinnedMemeRead]:
        """Return a full account's pins in display order."""

        await self._get_full_user_model(user_id)
        result = await self._session.execute(
            select(PinnedMeme)
            .where(PinnedMeme.user_id == user_id)
            .order_by(PinnedMeme.position.asc(), PinnedMeme.pinned_at.asc())
        )
        return [PinnedMemeRead.model_validate(row) for row in result.scalars()]

    async def pin_meme(self, *, user_id: object, meme_id: object) -> PinnedMemeRead:
        """Append a meme to the user's pins while preventing duplicates and the 20-pin overflow."""

        user = await self._get_full_user_model(user_id)
        meme = await self._get_visible_meme_model(meme_id=meme_id, viewer_user_id=user.id)
        if meme is None:
            raise MemeNotFoundError(f"Meme {meme_id} does not exist.")

        existing = await self._get_pinned_meme_model(user_id=user.id, meme_id=meme.id)
        if existing is not None:
            return PinnedMemeRead.model_validate(existing)

        pin_count = await self._session.scalar(
            select(func.count()).select_from(PinnedMeme).where(PinnedMeme.user_id == user.id)
        )
        if (pin_count or 0) >= MAX_PINNED_MEMES:
            raise PinLimitExceededError(f"Users can pin at most {MAX_PINNED_MEMES} memes.")

        position = int(pin_count or 0) + 1
        pinned_meme = PinnedMeme(user_id=user.id, meme_id=meme.id, position=position)
        self._session.add(pinned_meme)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to pin the meme.") from exc
        return PinnedMemeRead.model_validate(pinned_meme)

    async def unpin_meme(self, *, user_id: object, meme_id: object) -> bool:
        """Remove a pin and compact the remaining positions."""

        user = await self._get_full_user_model(user_id)
        result = await self._session.execute(
            select(PinnedMeme).where(PinnedMeme.user_id == user.id).order_by(PinnedMeme.position.asc())
        )
        existing_pins = list(result.scalars())
        if not any(pin.meme_id == meme_id for pin in existing_pins):
            return False

        await self._session.execute(delete(PinnedMeme).where(PinnedMeme.user_id == user.id))
        await self._session.flush()
        remaining_pins = [pin for pin in existing_pins if pin.meme_id != meme_id]
        self._session.add_all(
            PinnedMeme(user_id=user.id, meme_id=pin.meme_id, position=index, pinned_at=pin.pinned_at)
            for index, pin in enumerate(remaining_pins, start=1)
        )

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to unpin the meme.") from exc
        return True

    async def reorder_pins(self, *, user_id: object, meme_ids: list[uuid.UUID]) -> list[PinnedMemeRead]:
        """Replace the current pin order with the supplied full ordered pin list."""

        user = await self._get_full_user_model(user_id)
        if len(meme_ids) > MAX_PINNED_MEMES:
            raise InvalidPinnedMemeOrderError(f"Users can pin at most {MAX_PINNED_MEMES} memes.")
        if len(set(meme_ids)) != len(meme_ids):
            raise InvalidPinnedMemeOrderError("Pin reorder payload cannot contain duplicate meme IDs.")

        result = await self._session.execute(
            select(PinnedMeme).where(PinnedMeme.user_id == user.id).order_by(PinnedMeme.position.asc())
        )
        existing_pins = list(result.scalars())
        existing_ids = {pin.meme_id for pin in existing_pins}
        requested_ids = set(meme_ids)
        if existing_ids != requested_ids:
            raise InvalidPinnedMemeOrderError("Pin reorder payload must contain exactly the currently pinned memes.")

        await self._session.execute(delete(PinnedMeme).where(PinnedMeme.user_id == user.id))
        await self._session.flush()
        pinned_at_by_meme_id = {pin.meme_id: pin.pinned_at for pin in existing_pins}
        new_pins = [
            PinnedMeme(user_id=user.id, meme_id=meme_id, position=index, pinned_at=pinned_at_by_meme_id[meme_id])
            for index, meme_id in enumerate(meme_ids, start=1)
        ]
        self._session.add_all(new_pins)

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to reorder pinned memes.") from exc
        return [PinnedMemeRead.model_validate(pin) for pin in new_pins]

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
        if collection.kind is CollectionKind.FAVORITES:
            raise InvalidCollectionInviteError("Favorites collections cannot be shared by invite.")

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
            if integrity_constraint_name(exc) == "uq_collection_invites_token_hash":
                raise DuplicateCollectionInviteError("Invite token hash already exists.") from exc
            raise CollectionServiceError("Failed to persist the collection invite.") from exc

        return CollectionInviteRead.model_validate(invite)

    async def join_invite(self, *, token_hash: str, user_id: object) -> CollectionRead:
        """Redeem a direct-link invite for a full account and return the joined collection."""

        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        if user.account_type is AccountType.GUEST:
            raise GuestCollectionAccessError("Guest accounts cannot join shared collections.")

        invite = await self._get_invite_by_token_hash(token_hash)
        if invite is None:
            raise InvalidCollectionInviteError("Invite link is invalid or expired.")
        _ensure_invite_can_be_used(invite)

        collection = await self._get_collection_model(invite.collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {invite.collection_id} does not exist.")
        if collection.kind is CollectionKind.FAVORITES:
            raise InvalidCollectionInviteError("Favorites collections cannot be joined by invite.")

        existing_role = _membership_role_for_user(user.id, collection)
        if existing_role is None:
            self._session.add(
                CollectionMember(
                    collection_id=collection.id,
                    user_id=user.id,
                    role=invite.role,
                )
            )
            invite.use_count += 1
            invite.last_used_at = datetime.now(UTC)
            if invite.max_uses is not None and invite.use_count >= invite.max_uses:
                invite.status = CollectionInviteStatus.ACCEPTED

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CollectionServiceError("Failed to join the collection invite.") from exc

        persisted = await self._get_collection_model(collection.id)
        if persisted is None:  # pragma: no cover - defensive branch
            raise CollectionServiceError("Joined collection could not be reloaded.")
        return CollectionRead.model_validate(persisted)

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

    async def _list_collection_summaries(self, user: User) -> list[CollectionSummaryRead]:
        result = await self._session.execute(
            select(Collection, CollectionMember.role, func.count(CollectionMeme.meme_id).label("saved_meme_count"))
            .outerjoin(
                CollectionMember,
                and_(CollectionMember.collection_id == Collection.id, CollectionMember.user_id == user.id),
            )
            .outerjoin(CollectionMeme, CollectionMeme.collection_id == Collection.id)
            .where(or_(Collection.owner_id == user.id, CollectionMember.user_id == user.id))
            .group_by(Collection.id, CollectionMember.role)
            .order_by(
                case((Collection.kind == CollectionKind.FAVORITES, 0), else_=1),
                Collection.updated_at.desc(),
                Collection.title.asc(),
            )
        )
        summaries: list[CollectionSummaryRead] = []
        for collection, member_role, saved_meme_count in result.all():
            role = CollectionMembershipRole.OWNER if collection.owner_id == user.id else member_role
            if role is None:  # pragma: no cover - protected by query predicate
                continue
            summaries.append(
                CollectionSummaryRead(
                    id=collection.id,
                    owner_id=collection.owner_id,
                    title=collection.title,
                    description=collection.description,
                    kind=collection.kind,
                    visibility=collection.visibility,
                    role=role,
                    can_write=collection.owner_id == user.id or role in WRITE_ROLES,
                    saved_meme_count=int(saved_meme_count or 0),
                    created_at=collection.created_at,
                    updated_at=collection.updated_at,
                )
            )
        return summaries

    async def _load_favorite_cards(self, user_id: uuid.UUID) -> list[PublicMemeCardRead]:
        favorites = await self._get_favorites_collection_model(user_id)
        if favorites is None:
            return []

        result = await self._session.execute(
            select(CollectionMeme.meme_id)
            .where(CollectionMeme.collection_id == favorites.id)
            .order_by(CollectionMeme.added_at.desc(), CollectionMeme.meme_id.asc())
        )
        meme_ids = list(result.scalars().all())
        return await self._load_public_cards(meme_ids, viewer_user_id=user_id)

    async def _load_pinned_cards(self, user_id: uuid.UUID) -> list[PublicMemeCardRead]:
        result = await self._session.execute(
            select(PinnedMeme.meme_id)
            .where(PinnedMeme.user_id == user_id)
            .order_by(PinnedMeme.position.asc(), PinnedMeme.pinned_at.asc())
        )
        meme_ids = list(result.scalars().all())
        return await self._load_public_cards(meme_ids, viewer_user_id=user_id)

    async def _load_public_cards(
        self,
        meme_ids: list[uuid.UUID],
        *,
        viewer_user_id: uuid.UUID,
    ) -> list[PublicMemeCardRead]:
        meme_search_service = MemeSearchService(self._session, media_render_service=self._media_render_service)
        return await meme_search_service.get_authorized_meme_cards_by_ids(
            tuple(meme_ids),
            viewer_user_id=viewer_user_id,
            include_nsfw=True,
        )

    async def _get_collection_for_read(self, *, collection_id: object, user_id: object) -> tuple[User, Collection]:
        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        collection = await self._get_collection_model(collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {collection_id} does not exist.")
        if collection.kind is not CollectionKind.FAVORITES and user.account_type is AccountType.GUEST:
            raise GuestCollectionAccessError("A full account is required for custom collections.")
        if not _user_can_read_collection(user.id, collection):
            raise CollectionNotFoundError(f"Collection {collection_id} does not exist.")
        return user, collection

    async def _get_invite_by_token_hash(self, token_hash: str) -> CollectionInvite | None:
        result = await self._session.execute(
            select(CollectionInvite).where(CollectionInvite.token_hash == _normalize_token_hash(token_hash))
        )
        return result.scalar_one_or_none()

    async def _get_favorites_collection_model(self, user_id: object) -> Collection | None:
        result = await self._session.execute(
            select(Collection)
            .options(selectinload(Collection.memberships), selectinload(Collection.invites))
            .where(Collection.owner_id == user_id, Collection.kind == CollectionKind.FAVORITES)
        )
        return result.scalar_one_or_none()

    async def _get_collection_meme_model(self, *, collection_id: object, meme_id: object) -> CollectionMeme | None:
        result = await self._session.execute(
            select(CollectionMeme).where(
                CollectionMeme.collection_id == collection_id,
                CollectionMeme.meme_id == meme_id,
            )
        )
        return result.scalar_one_or_none()

    async def _insert_collection_meme(
        self,
        *,
        collection_id: object,
        meme_id: object,
        added_by_user_id: object,
    ) -> tuple[CollectionMeme, bool]:
        result = await self._session.execute(
            pg_insert(CollectionMeme)
            .values(collection_id=collection_id, meme_id=meme_id, added_by_user_id=added_by_user_id)
            .on_conflict_do_nothing(index_elements=[CollectionMeme.collection_id, CollectionMeme.meme_id])
            .returning(CollectionMeme.collection_id)
        )
        inserted = result.scalar_one_or_none() is not None
        saved_meme = await self._get_collection_meme_model(collection_id=collection_id, meme_id=meme_id)
        if saved_meme is None:  # pragma: no cover - defensive concurrent delete branch
            raise CollectionServiceError("Saved meme association could not be loaded.")
        return saved_meme, inserted

    async def _delete_collection_meme(self, *, collection_id: object, meme_id: object) -> bool:
        result = await self._session.execute(
            delete(CollectionMeme)
            .where(CollectionMeme.collection_id == collection_id, CollectionMeme.meme_id == meme_id)
            .returning(CollectionMeme.meme_id)
        )
        return result.scalar_one_or_none() is not None

    async def _get_meme_model(self, meme_id: object) -> Meme | None:
        result = await self._session.execute(select(Meme).where(Meme.id == meme_id))
        return result.scalar_one_or_none()

    async def _get_visible_meme_model(self, *, meme_id: object, viewer_user_id: object) -> Meme | None:
        authorized_collection = (
            select(CollectionMeme.meme_id)
            .select_from(CollectionMeme)
            .join(Collection, Collection.id == CollectionMeme.collection_id)
            .outerjoin(CollectionMember, CollectionMember.collection_id == Collection.id)
            .where(
                CollectionMeme.meme_id == Meme.id,
                or_(Collection.owner_id == viewer_user_id, CollectionMember.user_id == viewer_user_id),
            )
            .exists()
        )
        result = await self._session.execute(
            select(Meme).where(
                Meme.id == meme_id,
                or_(
                    Meme.is_public.is_(True),
                    Meme.author_user_id == viewer_user_id,
                    authorized_collection,
                ),
            )
        )
        return result.scalar_one_or_none()

    async def _increment_like_count(self, meme_id: object) -> None:
        _ = await self._session.execute(
            update(Meme).where(Meme.id == meme_id).values(like_count=Meme.like_count + 1)
        )

    async def _decrement_like_count(self, meme_id: object) -> None:
        _ = await self._session.execute(
            update(Meme).where(Meme.id == meme_id).values(like_count=func.greatest(Meme.like_count - 1, 0))
        )

    async def _get_pinned_meme_model(self, *, user_id: object, meme_id: object) -> PinnedMeme | None:
        result = await self._session.execute(
            select(PinnedMeme).where(PinnedMeme.user_id == user_id, PinnedMeme.meme_id == meme_id)
        )
        return result.scalar_one_or_none()

    async def _get_full_user_model(self, user_id: object) -> User:
        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        if user.account_type is not AccountType.FULL:
            raise GuestCollectionAccessError("A full account is required for pins.")
        return user

    async def _get_active_collection_model_for_write(self, user_id: object) -> tuple[User, Collection]:
        user = await self._get_user_model(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        if user.active_save_collection_id is None:
            favorites = await self.ensure_favorites_collection(user.id, commit=False)
            collection = await self._get_collection_model(favorites.id)
        else:
            collection = await self._get_collection_model(user.active_save_collection_id)
        if collection is None:
            raise CollectionNotFoundError(f"Collection {user.active_save_collection_id} does not exist.")
        self._ensure_can_write_collection(user, collection)
        return user, collection

    def _ensure_can_write_collection(self, user: User, collection: Collection) -> None:
        if user.account_type is AccountType.GUEST and collection.kind is not CollectionKind.FAVORITES:
            raise GuestCollectionAccessError(
                "Guest accounts can only use Favorites as the active save collection.",
            )
        if not _user_can_write_collection(user.id, collection):
            raise CollectionWriteAccessError(
                f"User {user.id} cannot write to collection {collection.id}.",
            )

    def _ensure_owner_can_manage_custom_collection(self, user: User, collection: Collection) -> None:
        if collection.kind is not CollectionKind.CUSTOM:
            raise CollectionWriteAccessError("Favorites collection metadata cannot be changed.")
        if collection.owner_id != user.id:
            raise CollectionWriteAccessError(
                f"Only the owner can manage collection {collection.id}.",
            )

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


def _ensure_invite_can_be_used(invite: CollectionInvite) -> None:
    if invite.channel is not CollectionInviteChannel.DIRECT_LINK:
        raise InvalidCollectionInviteError("Invite link is invalid or expired.")
    if invite.status is not CollectionInviteStatus.PENDING:
        raise InvalidCollectionInviteError("Invite link is invalid or expired.")
    if invite.revoked_at is not None:
        raise InvalidCollectionInviteError("Invite link is invalid or expired.")
    now = datetime.now(UTC)
    expires_at = invite.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            invite.status = CollectionInviteStatus.EXPIRED
            raise InvalidCollectionInviteError("Invite link is invalid or expired.")
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        invite.status = CollectionInviteStatus.ACCEPTED
        raise InvalidCollectionInviteError("Invite link is invalid or expired.")


def _user_can_write_collection(user_id: object, collection: Collection) -> bool:
    if collection.owner_id == user_id:
        return True
    return any(
        membership.user_id == user_id and membership.role in WRITE_ROLES
        for membership in collection.memberships
    )


def _user_can_read_collection(user_id: object, collection: Collection) -> bool:
    if collection.owner_id == user_id:
        return True
    return _membership_role_for_user(user_id, collection) is not None


def _membership_role_for_user(user_id: object, collection: Collection) -> CollectionMembershipRole | None:
    for membership in collection.memberships:
        if membership.user_id == user_id:
            return membership.role
    return None


__all__ = [
    "CollectionService",
    "FAVORITES_TITLE",
    "MAX_COLLECTION_TITLE_LENGTH",
    "MAX_PINNED_MEMES",
]
