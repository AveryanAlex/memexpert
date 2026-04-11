"""User/account service primitives with Favorites bootstrap invariants."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMember
from memexpert.models.enums import (
    AccountStatus,
    AccountType,
    AuthProvider,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    UserLanguage,
)
from memexpert.models.user import User
from memexpert.schemas import CollectionRead, UserRead
from memexpert.schemas.auth import normalize_auth_email
from memexpert.services._integrity import integrity_constraint_name
from memexpert.services.errors import (
    DuplicateFavoritesCollectionError,
    DuplicateIdentityError,
    InvalidIdentityError,
    UserNotFoundError,
    UserServiceError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

DEFAULT_GUEST_LIFETIME: Final = timedelta(days=90)
FAVORITES_TITLE: Final = "Favorites"
MAX_GOOGLE_ID_LENGTH: Final = 255


class UserService:
    """Service-layer account creation, lookup, and lifecycle helpers."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        guest_lifetime: timedelta = DEFAULT_GUEST_LIFETIME,
    ) -> None:
        self._session: AsyncSession = session
        self._guest_lifetime: timedelta = guest_lifetime

    @staticmethod
    def normalize_email(email: str | None) -> str | None:
        """Normalize and validate a user email, delegating to the shared auth-schema helper."""

        if email is None:
            return None
        try:
            return normalize_auth_email(email)
        except ValueError as exc:
            raise InvalidIdentityError(str(exc)) from exc

    @staticmethod
    def normalize_google_id(google_id: str | None) -> str | None:
        if google_id is None:
            return None

        normalized_google_id = google_id.strip()
        if not normalized_google_id:
            raise InvalidIdentityError("Google subject cannot be blank.")
        if len(normalized_google_id) > MAX_GOOGLE_ID_LENGTH:
            raise InvalidIdentityError(
                f"Google subject must be at most {MAX_GOOGLE_ID_LENGTH} characters long.",
            )
        return normalized_google_id

    @staticmethod
    def normalize_telegram_id(telegram_id: int | None) -> int | None:
        if telegram_id is None:
            return None
        if telegram_id <= 0:
            raise InvalidIdentityError("Telegram ID must be a positive integer.")
        return telegram_id

    @staticmethod
    def _normalize_password_hash(password_hash: str | None) -> str | None:
        if password_hash is None:
            return None

        normalized_password_hash = password_hash.strip()
        if not normalized_password_hash:
            raise InvalidIdentityError("Password hash cannot be blank when provided.")
        return normalized_password_hash

    async def get_by_id(self, user_id: object) -> UserRead | None:
        """Return a user by primary key if it exists."""

        result = await self._session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        return None if user is None else UserRead.model_validate(user)

    async def get_by_email(self, email: str) -> UserRead | None:
        """Return a user by normalized email lookup if it exists."""

        normalized_email = self.normalize_email(email)
        result = await self._session.execute(select(User).where(User.email == normalized_email))
        user = result.scalar_one_or_none()
        return None if user is None else UserRead.model_validate(user)

    async def get_by_google_id(self, google_id: str) -> UserRead | None:
        """Return a user by Google subject if it exists."""

        normalized_google_id = self.normalize_google_id(google_id)
        result = await self._session.execute(select(User).where(User.google_id == normalized_google_id))
        user = result.scalar_one_or_none()
        return None if user is None else UserRead.model_validate(user)

    async def get_by_telegram_id(self, telegram_id: int) -> UserRead | None:
        """Return a user by Telegram identifier if it exists."""

        normalized_telegram_id = self.normalize_telegram_id(telegram_id)
        result = await self._session.execute(select(User).where(User.telegram_id == normalized_telegram_id))
        user = result.scalar_one_or_none()
        return None if user is None else UserRead.model_validate(user)

    async def get_by_provider(
        self,
        provider: AuthProvider | str,
        subject: str | int,
    ) -> UserRead | None:
        """Return a user by provider-specific identity lookup."""

        resolved_provider = self._resolve_provider(provider)
        if resolved_provider is AuthProvider.GOOGLE:
            if not isinstance(subject, str):
                raise InvalidIdentityError("Google subject lookups require a string subject.")
            return await self.get_by_google_id(subject)
        if resolved_provider is AuthProvider.PASSWORD:
            if not isinstance(subject, str):
                raise InvalidIdentityError("Email/password lookups require an email string.")
            return await self.get_by_email(subject)
        if not isinstance(subject, int):
            raise InvalidIdentityError("Telegram lookups require an integer Telegram ID.")
        return await self.get_by_telegram_id(subject)

    async def get_locked_user_record(self, user_id: object) -> User | None:
        """Return a row-locked user ORM record when it exists."""

        result = await self._session.execute(select(User).where(User.id == user_id).with_for_update())
        return result.scalar_one_or_none()

    async def get_locked_guest_user(self, user_id: object) -> User | None:
        """Return a row-locked guest account when the persisted state still matches."""

        result = await self._session.execute(
            select(User)
            .where(User.id == user_id, User.account_type == AccountType.GUEST)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_locked_full_user(self, user_id: object) -> User | None:
        """Return a row-locked full account when the persisted state still matches."""

        result = await self._session.execute(
            select(User)
            .where(User.id == user_id, User.account_type == AccountType.FULL)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_locked_owned_collections(self, user_id: object) -> list[Collection]:
        """Return all owner-scoped collections for a user under row lock."""

        result = await self._session.execute(
            select(Collection)
            .where(Collection.owner_id == user_id)
            .order_by(Collection.created_at.asc(), Collection.id.asc())
            .with_for_update()
        )
        return list(result.scalars().all())

    async def get_locked_favorites_collection_record(self, user_id: object) -> Collection | None:
        """Return the caller's Favorites collection ORM row under lock if it exists."""

        result = await self._session.execute(
            select(Collection)
            .where(
                Collection.owner_id == user_id,
                Collection.kind == CollectionKind.FAVORITES,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_favorites_collection(self, user_id: object) -> CollectionRead | None:
        """Return the caller's Favorites collection if it exists."""

        result = await self._session.execute(
            select(Collection)
            .options(
                selectinload(Collection.memberships),
                selectinload(Collection.invites),
            )
            .where(
                Collection.owner_id == user_id,
                Collection.kind == CollectionKind.FAVORITES,
            )
        )
        collection = result.scalar_one_or_none()
        return None if collection is None else CollectionRead.model_validate(collection)

    async def create_guest_user(
        self,
        *,
        language: UserLanguage = UserLanguage.ANY,
        nsfw_enabled: bool = False,
    ) -> UserRead:
        """Create a guest account with an initialized Favorites collection."""

        now = utcnow()
        user = User(
            account_type=AccountType.GUEST,
            status=AccountStatus.ACTIVE,
            language=language,
            nsfw_enabled=nsfw_enabled,
            last_active_at=now,
            guest_expires_at=now + self._guest_lifetime,
        )
        favorites = await self._create_user_with_favorites(user)
        return UserRead.model_validate(user if user.active_save_collection_id == favorites.id else user)

    async def create_full_user(
        self,
        *,
        telegram_id: int | None = None,
        google_id: str | None = None,
        email: str | None = None,
        email_verified_at: datetime | None = None,
        password_hash: str | None = None,
        language: UserLanguage = UserLanguage.ANY,
        nsfw_enabled: bool = False,
        commit: bool = True,
    ) -> UserRead:
        """Create a full account with identity fields and Favorites bootstrap."""

        normalized_telegram_id = self.normalize_telegram_id(telegram_id)
        normalized_google_id = self.normalize_google_id(google_id)
        normalized_email = self.normalize_email(email)
        normalized_password_hash = self._normalize_password_hash(password_hash)

        if (
            normalized_telegram_id is None
            and normalized_google_id is None
            and normalized_email is None
        ):
            raise InvalidIdentityError(
                "Full accounts require at least one identity: telegram_id, google_id, or email.",
            )
        if normalized_email is None and email_verified_at is not None:
            raise InvalidIdentityError("email_verified_at requires an email address.")
        if normalized_email is None and normalized_password_hash is not None:
            raise InvalidIdentityError("password_hash requires an email identity.")

        await self._ensure_identity_is_available(
            telegram_id=normalized_telegram_id,
            google_id=normalized_google_id,
            email=normalized_email,
        )

        user = User(
            account_type=AccountType.FULL,
            status=AccountStatus.ACTIVE,
            telegram_id=normalized_telegram_id,
            google_id=normalized_google_id,
            email=normalized_email,
            email_verified_at=email_verified_at,
            password_hash=normalized_password_hash,
            language=language,
            nsfw_enabled=nsfw_enabled,
            last_active_at=utcnow(),
            guest_expires_at=None,
        )
        favorites = await self._create_user_with_favorites(user, commit=commit)
        return UserRead.model_validate(user if user.active_save_collection_id == favorites.id else user)

    async def attach_google_identity(
        self,
        *,
        user_id: object,
        google_id: str,
        email_verified_at: datetime | None = None,
        commit: bool = True,
    ) -> UserRead:
        """Attach a Google subject to an existing user without creating a duplicate account."""

        normalized_google_id = self.normalize_google_id(google_id)
        result = await self._session.execute(select(User).where(User.id == user_id).with_for_update())
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        if user.google_id is not None and user.google_id != normalized_google_id:
            raise DuplicateIdentityError(
                f"User {user.id} is already linked to Google subject {user.google_id!r}.",
            )

        if user.google_id is None:
            await self._ensure_identity_is_available(
                telegram_id=None,
                google_id=normalized_google_id,
                email=None,
            )
            user.google_id = normalized_google_id

        if email_verified_at is not None:
            user.email_verified_at = email_verified_at

        try:
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint_name = integrity_constraint_name(exc)
            if constraint_name == "uq_users_google_id_not_null":
                raise DuplicateIdentityError(
                    f"Google subject {normalized_google_id!r} is already linked to another account.",
                ) from exc
            raise UserServiceError("Failed to attach the Google identity to the user.") from exc

        return UserRead.model_validate(user)

    async def attach_telegram_identity(
        self,
        *,
        user_id: object,
        telegram_id: int,
        commit: bool = True,
    ) -> UserRead:
        """Attach a Telegram identifier to an existing user without duplicating an account."""

        normalized_telegram_id = self.normalize_telegram_id(telegram_id)
        result = await self._session.execute(select(User).where(User.id == user_id).with_for_update())
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        if user.telegram_id is not None and user.telegram_id != normalized_telegram_id:
            raise DuplicateIdentityError(
                f"User {user.id} is already linked to Telegram ID {user.telegram_id}.",
            )

        if user.telegram_id is None:
            await self._ensure_identity_is_available(
                telegram_id=normalized_telegram_id,
                google_id=None,
                email=None,
            )
            user.telegram_id = normalized_telegram_id

        try:
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint_name = integrity_constraint_name(exc)
            if constraint_name == "uq_users_telegram_id_not_null":
                raise DuplicateIdentityError(
                    f"Telegram ID {normalized_telegram_id} is already linked to another account.",
                ) from exc
            raise UserServiceError("Failed to attach the Telegram identity to the user.") from exc

        return UserRead.model_validate(user)

    async def upgrade_guest_to_full_account(
        self,
        *,
        user_id: object,
        telegram_id: int | None = None,
        google_id: str | None = None,
        email: str | None = None,
        email_verified_at: datetime | None = None,
        password_hash: str | None = None,
        commit: bool = True,
    ) -> UserRead:
        """Promote a guest account in place while safely attaching one or more identities."""

        normalized_telegram_id = self.normalize_telegram_id(telegram_id)
        normalized_google_id = self.normalize_google_id(google_id)
        normalized_email = self.normalize_email(email)
        normalized_password_hash = self._normalize_password_hash(password_hash)

        if (
            normalized_telegram_id is None
            and normalized_google_id is None
            and normalized_email is None
        ):
            raise InvalidIdentityError(
                "Full accounts require at least one identity: telegram_id, google_id, or email.",
            )
        if normalized_email is None and email_verified_at is not None:
            raise InvalidIdentityError("email_verified_at requires an email address.")
        if normalized_email is None and normalized_password_hash is not None:
            raise InvalidIdentityError("password_hash requires an email identity.")

        result = await self._session.execute(select(User).where(User.id == user_id).with_for_update())
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        if user.account_type is not AccountType.GUEST:
            raise UserServiceError(f"User {user.id} is not a guest account.")

        if user.telegram_id is not None and user.telegram_id != normalized_telegram_id:
            raise DuplicateIdentityError(
                f"User {user.id} is already linked to Telegram ID {user.telegram_id}.",
            )
        if user.google_id is not None and user.google_id != normalized_google_id:
            raise DuplicateIdentityError(
                f"User {user.id} is already linked to Google subject {user.google_id!r}.",
            )
        if user.email is not None and user.email != normalized_email:
            raise DuplicateIdentityError(
                f"User {user.id} is already linked to email {user.email!r}.",
            )
        if (
            user.password_hash is not None
            and normalized_password_hash is not None
            and user.password_hash != normalized_password_hash
        ):
            raise DuplicateIdentityError(
                f"User {user.id} already has a different password hash attached.",
            )

        await self._ensure_identity_is_available(
            telegram_id=normalized_telegram_id if user.telegram_id is None else None,
            google_id=normalized_google_id if user.google_id is None else None,
            email=normalized_email if user.email is None else None,
        )

        user.account_type = AccountType.FULL
        user.guest_expires_at = None
        if normalized_telegram_id is not None:
            user.telegram_id = normalized_telegram_id
        if normalized_google_id is not None:
            user.google_id = normalized_google_id
        if normalized_email is not None:
            user.email = normalized_email
        if email_verified_at is not None:
            user.email_verified_at = email_verified_at
        if normalized_password_hash is not None:
            user.password_hash = normalized_password_hash

        try:
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint_name = integrity_constraint_name(exc)
            if constraint_name == "uq_users_email_not_null":
                raise DuplicateIdentityError(
                    f"Email {normalized_email!r} is already linked to another account.",
                ) from exc
            if constraint_name == "uq_users_google_id_not_null":
                raise DuplicateIdentityError(
                    f"Google subject {normalized_google_id!r} is already linked to another account.",
                ) from exc
            if constraint_name == "uq_users_telegram_id_not_null":
                raise DuplicateIdentityError(
                    f"Telegram ID {normalized_telegram_id} is already linked to another account.",
                ) from exc
            raise UserServiceError("Failed to upgrade the guest account in place.") from exc

        return UserRead.model_validate(user)

    async def touch_last_active(
        self,
        user_id: object,
        *,
        occurred_at: datetime | None = None,
    ) -> UserRead:
        """Persist the latest activity timestamp for a user."""

        result = await self._session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        user.last_active_at = occurred_at or utcnow()
        try:
            await self._session.commit()
        except IntegrityError as exc:  # pragma: no cover - defensive branch
            await self._session.rollback()
            raise UserServiceError("Failed to persist the user's activity timestamp.") from exc

        return UserRead.model_validate(user)

    async def _create_user_with_favorites(self, user: User, *, commit: bool = True) -> Collection:
        try:
            self._session.add(user)
            await self._session.flush()

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

            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint_name = integrity_constraint_name(exc)
            if constraint_name == "uq_collections_one_favorites_per_owner":
                raise DuplicateFavoritesCollectionError(
                    f"User {user.id} already has a Favorites collection.",
                ) from exc
            if constraint_name in {
                "uq_users_email_not_null",
                "uq_users_google_id_not_null",
                "uq_users_telegram_id_not_null",
            }:
                raise DuplicateIdentityError(
                    "One of the provided identities is already linked to another account.",
                ) from exc
            raise UserServiceError("Failed to create the user and Favorites bootstrap rows.") from exc

        return favorites

    async def _ensure_identity_is_available(
        self,
        *,
        telegram_id: int | None,
        google_id: str | None,
        email: str | None,
    ) -> None:
        conditions: list[ColumnElement[bool]] = []
        if telegram_id is not None:
            conditions.append(User.telegram_id == telegram_id)
        if google_id is not None:
            conditions.append(User.google_id == google_id)
        if email is not None:
            conditions.append(User.email == email)

        if not conditions:
            return

        result = await self._session.execute(select(User).where(or_(*conditions)))
        existing_users = result.scalars().all()
        for existing_user in existing_users:
            if telegram_id is not None and existing_user.telegram_id == telegram_id:
                raise DuplicateIdentityError(
                    f"Telegram ID {telegram_id} is already linked to user {existing_user.id}.",
                )
            if google_id is not None and existing_user.google_id == google_id:
                raise DuplicateIdentityError(
                    f"Google subject {google_id!r} is already linked to user {existing_user.id}.",
                )
            if email is not None and existing_user.email == email:
                raise DuplicateIdentityError(
                    f"Email {email!r} is already linked to user {existing_user.id}.",
                )

    @staticmethod
    def _resolve_provider(provider: AuthProvider | str) -> AuthProvider:
        try:
            return provider if isinstance(provider, AuthProvider) else AuthProvider(provider)
        except ValueError as exc:
            raise InvalidIdentityError(f"Unsupported auth provider {provider!r}.") from exc


__all__ = [
    "DEFAULT_GUEST_LIFETIME",
    "FAVORITES_TITLE",
    "UserService",
]
