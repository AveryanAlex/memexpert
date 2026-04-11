# ruff: noqa: TC001,TC003
"""Guest-to-full account linking and merge orchestration with audit invariants."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Self

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.enums import AccountStatus, AccountType, AnalyticsEventType, CollectionKind
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, InlineUsageEvent, TelegramLinkCode, User
from memexpert.schemas.auth import TelegramWidgetAuthRequest
from memexpert.schemas.user import UserRead
from memexpert.services._auth_validation import (
    normalize_optional_text,
    require_positive_int,
)
from memexpert.services._integrity import integrity_constraint_name
from memexpert.services.errors import (
    AccountLinkAlreadyCompletedError,
    AccountLinkInvariantError,
    AccountUnavailableError,
    AuthConfigurationError,
    DuplicateIdentityError,
    EmailAlreadyInUseError,
    GuestAccountRequiredError,
    ProviderNotConfiguredError,
    ProviderPayloadInvalidError,
    ServiceError,
    UserNotFoundError,
)
from memexpert.services.provider_auth_service import (
    EmailSignupIdentity,
    GoogleIdentity,
    GoogleIdentityResolution,
    ProviderAuthService,
    TelegramIdentity,
)
from memexpert.services.user_service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

TELEGRAM_LINK_START_PREFIX = "link_"
TELEGRAM_MAX_START_PARAMETER_LENGTH = 64
MAX_TELEGRAM_LINK_CODE_LENGTH = TELEGRAM_MAX_START_PARAMETER_LENGTH - len(TELEGRAM_LINK_START_PREFIX)
TELEGRAM_LINK_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
TELEGRAM_LINK_CODE_BYTES = 16


@dataclass(frozen=True, slots=True)
class LinkedProvidersProjection:
    """Read-only provider-link state for a canonical account."""

    email: str | None
    email_verified_at: datetime | None
    has_password: bool
    google_linked: bool
    telegram_linked: bool


@dataclass(frozen=True, slots=True)
class AccountLinkResult:
    """Canonical result returned by guest-link and merge operations."""

    user: UserRead
    linked_providers: LinkedProvidersProjection
    merge_performed: bool
    merge_log_id: uuid.UUID | None
    guest_user_id: uuid.UUID
    canonical_user_id: uuid.UUID
    deleted_guest_user_id: uuid.UUID | None
    favorites_transferred: int
    duplicate_favorites_skipped: int
    analytics_events_transferred: int
    inline_usage_events_transferred: int
    views_transferred: int
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TelegramLinkStartResult:
    """Persisted Telegram deep-link issuance metadata returned to guest callers."""

    code: str
    deep_link_url: str
    expires_at: datetime
    expires_in_seconds: int
    return_url: str


@dataclass(slots=True)
class _LinkAttemptState:
    """Per-flow context used to run the shared link-failure post-mortem check.

    Each ``link_guest_with_*`` entrypoint snapshots the guest at call time and
    sets ``resolved`` after the provider identity has been validated. On
    failure the caller hands this state to
    :meth:`AccountLinkService._handle_link_failure`, which rolls back the
    session and — only when ``resolved`` is ``True`` — asserts that no
    concurrent request already finished the link.
    """

    guest_user_id: uuid.UUID
    operation_started_at: datetime
    initial_guest_snapshot: UserRead | None
    resolved: bool = False


class AccountLinkService:
    """Upgrade guests in place or merge them into an existing canonical full account."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider_auth_service: ProviderAuthService | None = None,
        user_service: UserService | None = None,
        telegram_link_bot_username: str | None = None,
        telegram_link_code_ttl_seconds: int = 600,
        telegram_link_return_url: str | None = None,
    ) -> None:
        self._session: AsyncSession = session
        self._user_service: UserService = user_service or UserService(session)
        self._provider_auth_service: ProviderAuthService = provider_auth_service or ProviderAuthService.from_settings(
            session,
            user_service=self._user_service,
        )
        self._telegram_link_bot_username: str | None = normalize_optional_text(telegram_link_bot_username)
        self._telegram_link_code_ttl_seconds: int = require_positive_int(
            "telegram_link_code_ttl_seconds",
            telegram_link_code_ttl_seconds,
        )
        self._telegram_link_return_url: str | None = normalize_optional_text(telegram_link_return_url)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        provider_auth_service: ProviderAuthService | None = None,
        user_service: UserService | None = None,
    ) -> Self:
        """Build the service from cached runtime settings for later API and bot entrypoints."""

        resolved_settings = settings or get_settings()
        resolved_user_service = user_service or UserService(session)
        resolved_provider_auth_service = provider_auth_service or ProviderAuthService.from_settings(
            session,
            settings=resolved_settings,
            user_service=resolved_user_service,
        )
        return cls(
            session,
            provider_auth_service=resolved_provider_auth_service,
            user_service=resolved_user_service,
            telegram_link_bot_username=resolved_settings.auth_telegram_bot_username,
            telegram_link_code_ttl_seconds=resolved_settings.auth_telegram_link_code_ttl_seconds,
            telegram_link_return_url=(
                str(resolved_settings.auth_telegram_link_return_url)
                if resolved_settings.auth_telegram_link_return_url is not None
                else None
            ),
        )

    async def get_linked_providers(self, *, user_id: object) -> LinkedProvidersProjection:
        """Return the caller's read-only linked-provider projection."""

        result = await self._session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        return self._build_linked_provider_projection(user)

    async def issue_telegram_link_code(self, *, guest_user_id: uuid.UUID) -> TelegramLinkStartResult:
        """Persist a short Telegram deep-link code for a guest-only link handoff."""

        bot_username, return_url = self._require_telegram_link_configuration()
        expires_at = utcnow() + timedelta(seconds=self._telegram_link_code_ttl_seconds)
        code = self._normalize_telegram_link_code(self._generate_telegram_link_code())
        code_hash = self._hash_telegram_link_code(code)

        try:
            guest_user = await self._lock_required_user(guest_user_id)
            self._ensure_guest_can_link(guest_user)
            _ = await self._require_guest_favorites_only_collection(guest_user)
            await self._expire_active_telegram_link_codes(guest_user.id)

            self._session.add(
                TelegramLinkCode(
                    guest_user_id=guest_user.id,
                    code_hash=code_hash,
                    expires_at=expires_at,
                )
            )
            await self._session.commit()
        except ServiceError:
            await self._session.rollback()
            raise
        except IntegrityError as exc:
            await self._session.rollback()
            constraint_name = integrity_constraint_name(exc)
            if constraint_name == "uq_telegram_link_codes_code_hash":
                raise AccountLinkInvariantError("Telegram link code collision detected; please retry.") from exc
            raise AccountLinkInvariantError("Failed to issue the Telegram link code.") from exc

        return TelegramLinkStartResult(
            code=code,
            deep_link_url=self._build_telegram_deep_link(bot_username=bot_username, code=code),
            expires_at=expires_at,
            expires_in_seconds=self._telegram_link_code_ttl_seconds,
            return_url=return_url,
        )

    async def redeem_telegram_link_code(
        self,
        *,
        code: str,
        identity: TelegramIdentity,
    ) -> AccountLinkResult:
        """Redeem a Telegram deep-link code exactly once and reuse the shared merge flow."""

        normalized_code = self._normalize_telegram_link_code(code)
        link_code = await self._lock_telegram_link_code(normalized_code)
        if link_code.redeemed_at is not None:
            raise AccountLinkInvariantError("Telegram link code has already been redeemed.")
        if link_code.expires_at <= utcnow():
            raise AccountLinkInvariantError("Telegram link code has expired.")

        link_code.redeemed_at = utcnow()
        link_code.redeemed_by_telegram_id = identity.telegram_id
        try:
            return await self.link_guest_with_telegram_identity(
                guest_user_id=link_code.guest_user_id,
                identity=identity,
            )
        except ServiceError:
            raise
        except IntegrityError as exc:
            await self._session.rollback()
            raise AccountLinkInvariantError("Failed to redeem the Telegram link code.") from exc

    async def link_guest_with_email_signup(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        email: str,
        password: str,
    ) -> AccountLinkResult:
        """Upgrade a guest account in place with a new email/password identity.

        When ``guest_user_id`` is ``None`` a throwaway guest is bootstrapped
        inside the same transaction and upgraded in place; a validation
        failure rolls the bootstrap back atomically, so the call is
        indistinguishable from a traditional direct signup to observers.
        """

        resolved_guest_id = await self._resolve_guest_user_id(guest_user_id)
        state = await self._start_link_attempt(resolved_guest_id)
        try:
            signup_identity = self._provider_auth_service.prepare_email_signup_identity(
                email=email,
                password=password,
            )
            state.resolved = True
            return await self._upgrade_guest_in_place(
                guest_user_id=resolved_guest_id,
                signup_identity=signup_identity,
            )
        except DuplicateIdentityError as exc:
            await self._handle_link_failure(state, exc)
            raise EmailAlreadyInUseError("Email is already in use.") from exc
        except ServiceError as exc:
            await self._handle_link_failure(state, exc)
            raise
        except IntegrityError as exc:
            await self._handle_link_failure(state, exc)
            raise AccountLinkInvariantError("Failed to link the guest account with email signup.") from exc

    async def link_guest_with_email_login(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        email: str,
        password: str,
    ) -> AccountLinkResult:
        """Merge a guest account into an existing full email/password account.

        When ``guest_user_id`` is ``None`` a throwaway guest is bootstrapped
        and immediately merged into the resolved target account, so callers
        that arrive anonymously see the same end state as callers that
        arrived holding a persistent guest session.
        """

        resolved_guest_id = await self._resolve_guest_user_id(guest_user_id)
        state = await self._start_link_attempt(resolved_guest_id)
        try:
            target_user = await self._provider_auth_service.resolve_email_login_user(
                email=email,
                password=password,
            )
            state.resolved = True
            return await self._merge_guest_into_existing_full(
                guest_user_id=resolved_guest_id,
                target_user_id=target_user.id,
            )
        except ServiceError as exc:
            await self._handle_link_failure(state, exc)
            raise
        except IntegrityError as exc:
            await self._handle_link_failure(state, exc)
            raise AccountLinkInvariantError("Failed to merge the guest account into the email account.") from exc

    async def link_guest_with_google_code(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        code: str,
    ) -> AccountLinkResult:
        """Exchange a Google code, then upgrade or merge the guest accordingly.

        This is a thin delegator; the bootstrap, if any, happens once inside
        :meth:`link_guest_with_google_identity`.
        """

        google_identity = await self._provider_auth_service.resolve_google_identity_from_code(code=code)
        return await self.link_guest_with_google_identity(
            guest_user_id=guest_user_id,
            identity=google_identity,
        )

    async def link_guest_with_google_identity(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        identity: GoogleIdentity,
    ) -> AccountLinkResult:
        """Upgrade a guest with Google or merge it into the canonical Google/full account.

        When ``guest_user_id`` is ``None`` a throwaway guest is bootstrapped
        inside the same transaction; the subsequent upgrade or merge owns
        the commit, and a failure rolls the bootstrap back atomically.
        """

        resolved_guest_id = await self._resolve_guest_user_id(guest_user_id)
        state = await self._start_link_attempt(resolved_guest_id)
        try:
            resolution = await self._provider_auth_service.resolve_google_identity(identity)
            state.resolved = True
            return await self._link_guest_with_google_resolution(
                guest_user_id=resolved_guest_id,
                resolution=resolution,
            )
        except DuplicateIdentityError as exc:
            await self._handle_link_failure(state, exc)
            # Re-resolve so the merge target reflects any user that was
            # created concurrently between the first resolve and the failed
            # upgrade attempt; the post-mortem above would have fired if the
            # whole link already completed elsewhere, so anything else here
            # is a live Google identity we can safely merge into.
            resolution = await self._provider_auth_service.resolve_google_identity(identity)
            if resolution.user is None:
                raise ProviderPayloadInvalidError("Google identity could not be resolved safely.") from exc
            return await self._merge_guest_into_existing_full(
                guest_user_id=resolved_guest_id,
                target_user_id=resolution.user.id,
                attach_google_identity=resolution.matched_by == "verified_email",
                google_identity=identity,
            )
        except ServiceError as exc:
            await self._handle_link_failure(state, exc)
            raise
        except IntegrityError as exc:
            await self._handle_link_failure(state, exc)
            raise AccountLinkInvariantError("Failed to link the guest account with Google.") from exc

    async def link_guest_with_telegram_widget(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        payload: TelegramWidgetAuthRequest,
    ) -> AccountLinkResult:
        """Validate a Telegram Login Widget payload, then upgrade or merge the guest.

        Thin delegator; the bootstrap, if any, happens once inside
        :meth:`link_guest_with_telegram_identity`.
        """

        identity = self._provider_auth_service.resolve_telegram_widget_identity(payload)
        return await self.link_guest_with_telegram_identity(
            guest_user_id=guest_user_id,
            identity=identity,
        )

    async def link_guest_with_telegram_miniapp(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        init_data: str | None,
    ) -> AccountLinkResult:
        """Validate Telegram Mini App initData, then upgrade or merge the guest.

        Thin delegator; the bootstrap, if any, happens once inside
        :meth:`link_guest_with_telegram_identity`.
        """

        identity = self._provider_auth_service.resolve_telegram_miniapp_identity(init_data)
        return await self.link_guest_with_telegram_identity(
            guest_user_id=guest_user_id,
            identity=identity,
        )

    async def link_guest_with_telegram_identity(
        self,
        *,
        guest_user_id: uuid.UUID | None,
        identity: TelegramIdentity,
    ) -> AccountLinkResult:
        """Upgrade a guest with Telegram or merge it into the canonical Telegram account.

        When ``guest_user_id`` is ``None`` a throwaway guest is bootstrapped
        inside the same transaction; the subsequent upgrade or merge owns
        the commit, and a failure rolls the bootstrap back atomically.
        """

        resolved_guest_id = await self._resolve_guest_user_id(guest_user_id)
        state = await self._start_link_attempt(resolved_guest_id)
        try:
            target_user = await self._provider_auth_service.resolve_telegram_identity_user(identity)
            state.resolved = True
            if target_user is None:
                return await self._upgrade_guest_in_place(
                    guest_user_id=resolved_guest_id,
                    telegram_identity=identity,
                )
            return await self._merge_guest_into_existing_full(
                guest_user_id=resolved_guest_id,
                target_user_id=target_user.id,
            )
        except DuplicateIdentityError as exc:
            await self._handle_link_failure(state, exc)
            # Re-resolve so we see any account that was created between the
            # first resolve and the failed upgrade; the post-mortem above
            # already handled the "link finished elsewhere" case.
            target_user = await self._provider_auth_service.resolve_telegram_identity_user(identity)
            if target_user is None:
                raise ProviderPayloadInvalidError("Telegram identity could not be resolved safely.") from exc
            return await self._merge_guest_into_existing_full(
                guest_user_id=resolved_guest_id,
                target_user_id=target_user.id,
            )
        except ServiceError as exc:
            await self._handle_link_failure(state, exc)
            raise
        except IntegrityError as exc:
            await self._handle_link_failure(state, exc)
            raise AccountLinkInvariantError("Failed to link the guest account with Telegram.") from exc

    async def _resolve_guest_user_id(self, guest_user_id: uuid.UUID | None) -> uuid.UUID:
        """Return the caller's guest id or bootstrap a throwaway guest in the same transaction.

        The bootstrap uses ``commit=False`` so the new row participates in
        the outer transaction owned by the subsequent upgrade or merge
        call. A validation failure downstream triggers
        :meth:`_handle_link_failure`, which rolls the session back and
        atomically removes the bootstrapped guest.
        """

        if guest_user_id is not None:
            return guest_user_id
        bootstrapped_guest = await self._user_service.create_guest_user(commit=False)
        return bootstrapped_guest.id

    async def _start_link_attempt(self, guest_user_id: uuid.UUID) -> _LinkAttemptState:
        """Snapshot the guest state so link-failure post-mortem queries can see the pre-image."""

        return _LinkAttemptState(
            guest_user_id=guest_user_id,
            operation_started_at=utcnow(),
            initial_guest_snapshot=await self._user_service.get_by_id(guest_user_id),
        )

    async def _handle_link_failure(self, state: _LinkAttemptState, exc: Exception) -> None:
        """Roll back the session and, when the identity was resolved, assert no concurrent completion.

        Callers invoke this from every ``except`` arm of a
        ``link_guest_with_*`` flow before translating the original error
        into its caller-facing type. When
        :meth:`_raise_if_account_link_already_completed` detects that the
        link already finished on a separate request, it raises
        :class:`AccountLinkAlreadyCompletedError` here and the caller never
        gets to run its translation block.
        """

        await self._session.rollback()
        if state.resolved:
            await self._raise_if_account_link_already_completed(
                guest_user_id=state.guest_user_id,
                operation_started_at=state.operation_started_at,
                initial_guest_snapshot=state.initial_guest_snapshot,
                cause=exc,
            )

    async def _raise_if_account_link_already_completed(
        self,
        *,
        guest_user_id: uuid.UUID,
        operation_started_at: datetime,
        initial_guest_snapshot: UserRead | None,
        cause: Exception,
    ) -> None:
        if initial_guest_snapshot is None:
            return
        if initial_guest_snapshot.account_type is not AccountType.GUEST:
            return
        if initial_guest_snapshot.status is not AccountStatus.ACTIVE:
            return

        merge_log = await self._find_completed_elsewhere_merge_log(
            guest_user_id=guest_user_id,
            operation_started_at=operation_started_at,
        )
        if merge_log is not None:
            raise AccountLinkAlreadyCompletedError(self._account_link_already_completed_detail()) from cause

        surviving_user = await self._find_completed_elsewhere_surviving_user(
            guest_user_id=guest_user_id,
            operation_started_at=operation_started_at,
        )
        if surviving_user is not None:
            raise AccountLinkAlreadyCompletedError(self._account_link_already_completed_detail()) from cause

    async def _find_completed_elsewhere_merge_log(
        self,
        *,
        guest_user_id: uuid.UUID,
        operation_started_at: datetime,
    ) -> AccountMergeLog | None:
        result = await self._session.execute(
            select(AccountMergeLog)
            .where(
                AccountMergeLog.guest_account_id == guest_user_id,
                AccountMergeLog.created_at >= operation_started_at,
            )
            .order_by(AccountMergeLog.created_at.desc())
        )
        return result.scalars().first()

    async def _find_completed_elsewhere_surviving_user(
        self,
        *,
        guest_user_id: uuid.UUID,
        operation_started_at: datetime,
    ) -> User | None:
        result = await self._session.execute(select(User).where(User.id == guest_user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return None
        if user.account_type is not AccountType.FULL:
            return None
        if user.status is not AccountStatus.ACTIVE:
            return None
        if user.guest_expires_at is not None:
            return None
        if user.updated_at < operation_started_at:
            return None
        if not self._has_linked_identity(user):
            return None
        return user

    @staticmethod
    def _account_link_already_completed_detail() -> str:
        return (
            "This account link already completed elsewhere. Refresh or reload MemeXpert to "
            "continue with the current account state instead of retrying."
        )

    @staticmethod
    def _has_linked_identity(user: User) -> bool:
        return (
            user.telegram_id is not None
            or user.google_id is not None
            or user.email is not None
            or (user.password_hash is not None and bool(user.password_hash.strip()))
        )

    async def _link_guest_with_google_resolution(
        self,
        *,
        guest_user_id: uuid.UUID,
        resolution: GoogleIdentityResolution,
    ) -> AccountLinkResult:
        if resolution.user is None:
            return await self._upgrade_guest_in_place(
                guest_user_id=guest_user_id,
                google_identity=resolution.identity,
            )

        return await self._merge_guest_into_existing_full(
            guest_user_id=guest_user_id,
            target_user_id=resolution.user.id,
            attach_google_identity=resolution.matched_by == "verified_email",
            google_identity=resolution.identity,
        )

    async def _upgrade_guest_in_place(
        self,
        *,
        guest_user_id: uuid.UUID,
        signup_identity: EmailSignupIdentity | None = None,
        google_identity: GoogleIdentity | None = None,
        telegram_identity: TelegramIdentity | None = None,
    ) -> AccountLinkResult:
        guest_user = await self._lock_required_user(guest_user_id)
        self._ensure_guest_can_link(guest_user)
        _ = await self._require_guest_favorites_only_collection(guest_user)

        _ = await self._user_service.upgrade_guest_to_full_account(
            user_id=guest_user.id,
            email=(
                signup_identity.email
                if signup_identity is not None
                else google_identity.email if google_identity is not None else None
            ),
            password_hash=signup_identity.password_hash if signup_identity is not None else None,
            google_id=google_identity.google_id if google_identity is not None else None,
            email_verified_at=google_identity.email_verified_at if google_identity is not None else None,
            telegram_id=telegram_identity.telegram_id if telegram_identity is not None else None,
            commit=False,
        )

        details = {
            "mode": "upgrade_in_place",
            "favorites_transferred": 0,
            "favorite_duplicates_skipped": 0,
            "analytics_events_transferred": 0,
            "views_transferred": 0,
            "inline_usage_events_transferred": 0,
        }
        await self._session.commit()
        return self._build_result(
            canonical_user=guest_user,
            merge_performed=False,
            merge_log_id=None,
            guest_user_id=guest_user.id,
            deleted_guest_user_id=None,
            favorites_transferred=0,
            duplicate_favorites_skipped=0,
            analytics_events_transferred=0,
            inline_usage_events_transferred=0,
            views_transferred=0,
            details=details,
        )

    async def _merge_guest_into_existing_full(
        self,
        *,
        guest_user_id: uuid.UUID,
        target_user_id: uuid.UUID,
        attach_google_identity: bool = False,
        google_identity: GoogleIdentity | None = None,
    ) -> AccountLinkResult:
        if guest_user_id == target_user_id:
            raise AccountLinkInvariantError("Guest account cannot merge into itself.")

        locked_users = await self._lock_users_in_order(guest_user_id, target_user_id)
        guest_user = locked_users[guest_user_id]
        target_user = locked_users[target_user_id]
        if guest_user is None:
            raise UserNotFoundError(f"User {guest_user_id} does not exist.")
        if target_user is None:
            raise UserNotFoundError(f"User {target_user_id} does not exist.")

        self._ensure_guest_can_link(guest_user)
        self._ensure_canonical_target(target_user)
        guest_favorites = await self._require_guest_favorites_only_collection(guest_user)
        target_favorites = await self._require_favorites_collection(target_user, owner_label="Canonical full account")

        if attach_google_identity:
            if google_identity is None:
                raise AccountLinkInvariantError("Google identity details are required for verified-email merges.")
            _ = await self._user_service.attach_google_identity(
                user_id=target_user.id,
                google_id=google_identity.google_id,
                email_verified_at=google_identity.email_verified_at,
                commit=False,
            )

        guest_favorite_rows = await self._count_collection_memes(guest_favorites.id)
        favorites_transferred = await self._transfer_favorites(
            guest_favorites=guest_favorites,
            target_favorites=target_favorites,
            target_user=target_user,
        )
        duplicate_favorites_skipped = guest_favorite_rows - favorites_transferred
        views_transferred = await self._count_meme_views(guest_user.id)
        analytics_events_transferred = await self._reassign_analytics_events(
            source_user_id=guest_user.id,
            target_user_id=target_user.id,
        )
        inline_usage_events_transferred = await self._reassign_inline_usage_events(
            source_user_id=guest_user.id,
            target_user_id=target_user.id,
        )

        details = {
            "mode": "merge_into_existing_full",
            "guest_favorites_collection_id": str(guest_favorites.id),
            "target_favorites_collection_id": str(target_favorites.id),
            "guest_favorite_rows": guest_favorite_rows,
            "favorites_transferred": favorites_transferred,
            "favorite_duplicates_skipped": duplicate_favorites_skipped,
            "analytics_events_transferred": analytics_events_transferred,
            "views_transferred": views_transferred,
            "inline_usage_events_transferred": inline_usage_events_transferred,
        }

        merge_log = AccountMergeLog(
            guest_account_id=guest_user.id,
            target_account_id=target_user.id,
            favorites_transferred=favorites_transferred,
            views_transferred=views_transferred,
            details=details,
        )
        self._session.add(merge_log)
        await self._session.flush()

        guest_user.active_save_collection_id = None
        await self._session.flush()
        await self._session.delete(guest_favorites)
        await self._session.flush()
        await self._session.delete(guest_user)
        await self._session.commit()

        return self._build_result(
            canonical_user=target_user,
            merge_performed=True,
            merge_log_id=merge_log.id,
            guest_user_id=guest_user.id,
            deleted_guest_user_id=guest_user.id,
            favorites_transferred=favorites_transferred,
            duplicate_favorites_skipped=duplicate_favorites_skipped,
            analytics_events_transferred=analytics_events_transferred,
            inline_usage_events_transferred=inline_usage_events_transferred,
            views_transferred=views_transferred,
            details=details,
        )

    async def _lock_telegram_link_code(self, code: str) -> TelegramLinkCode:
        code_hash = self._hash_telegram_link_code(code)
        result = await self._session.execute(
            select(TelegramLinkCode)
            .where(TelegramLinkCode.code_hash == code_hash)
            .with_for_update()
        )
        link_codes = result.scalars().all()
        if not link_codes:
            raise AccountLinkInvariantError("Telegram link code is invalid.")
        if len(link_codes) > 1:
            raise AccountLinkInvariantError("Telegram link code state is invalid.")
        return link_codes[0]

    async def _expire_active_telegram_link_codes(self, guest_user_id: uuid.UUID) -> None:
        now = utcnow()
        _ = await self._session.execute(
            update(TelegramLinkCode)
            .where(
                TelegramLinkCode.guest_user_id == guest_user_id,
                TelegramLinkCode.redeemed_at.is_(None),
                TelegramLinkCode.expires_at > now,
            )
            .values(expires_at=now)
        )

    async def _lock_required_user(self, user_id: uuid.UUID) -> User:
        user = await self._user_service.get_locked_user_record(user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")
        return user

    async def _lock_users_in_order(self, *user_ids: uuid.UUID) -> dict[uuid.UUID, User | None]:
        ordered_ids = list(dict.fromkeys(sorted(user_ids, key=lambda value: value.int)))
        locked_users: dict[uuid.UUID, User | None] = {}
        for user_id in ordered_ids:
            locked_users[user_id] = await self._user_service.get_locked_user_record(user_id)
        return locked_users

    def _ensure_guest_can_link(self, user: User) -> None:
        if user.account_type is not AccountType.GUEST:
            raise GuestAccountRequiredError("Only guest accounts can be linked.")
        if user.status is not AccountStatus.ACTIVE:
            raise AccountUnavailableError("Account is not available.")
        if (
            user.telegram_id is not None
            or user.google_id is not None
            or user.email is not None
            or user.password_hash is not None
        ):
            raise AccountLinkInvariantError("Guest account already carries linked identities.")

    def _ensure_canonical_target(self, user: User) -> None:
        if user.account_type is not AccountType.FULL:
            raise AccountLinkInvariantError("Canonical merge targets must be full accounts.")
        if user.status is not AccountStatus.ACTIVE:
            raise AccountUnavailableError("Account is not available.")

    async def _require_guest_favorites_only_collection(self, guest_user: User) -> Collection:
        owned_collections = await self._user_service.get_locked_owned_collections(guest_user.id)
        favorites = [
            collection
            for collection in owned_collections
            if collection.kind is CollectionKind.FAVORITES
        ]
        custom_collections = [
            collection
            for collection in owned_collections
            if collection.kind is not CollectionKind.FAVORITES
        ]

        if len(favorites) != 1:
            raise AccountLinkInvariantError(
                f"Guest user {guest_user.id} must own exactly one Favorites collection before linking.",
            )
        if custom_collections:
            raise AccountLinkInvariantError(
                f"Guest user {guest_user.id} owns unexpected non-Favorites collections.",
            )
        return favorites[0]

    async def _require_favorites_collection(self, user: User, *, owner_label: str) -> Collection:
        favorites = await self._user_service.get_locked_favorites_collection_record(user.id)
        if favorites is None:
            raise AccountLinkInvariantError(
                f"{owner_label} {user.id} is missing a Favorites collection.",
            )
        return favorites

    async def _count_collection_memes(self, collection_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(CollectionMeme)
            .where(CollectionMeme.collection_id == collection_id)
        )
        return int(result.scalar_one())

    async def _transfer_favorites(
        self,
        *,
        guest_favorites: Collection,
        target_favorites: Collection,
        target_user: User,
    ) -> int:
        insert_statement = (
            pg_insert(CollectionMeme)
            .from_select(
                ["collection_id", "meme_id", "added_by_user_id", "added_at"],
                select(
                    literal(target_favorites.id),
                    CollectionMeme.meme_id,
                    literal(target_user.id),
                    CollectionMeme.added_at,
                ).where(CollectionMeme.collection_id == guest_favorites.id),
            )
            .on_conflict_do_nothing(index_elements=[CollectionMeme.collection_id, CollectionMeme.meme_id])
        )
        result = await self._session.execute(insert_statement)
        return self._rowcount(result)

    async def _count_meme_views(self, user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(
                AnalyticsEvent.user_id == user_id,
                AnalyticsEvent.event_type == AnalyticsEventType.MEME_VIEW,
            )
        )
        return int(result.scalar_one())

    async def _reassign_analytics_events(self, *, source_user_id: uuid.UUID, target_user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            update(AnalyticsEvent)
            .where(AnalyticsEvent.user_id == source_user_id)
            .values(user_id=target_user_id)
        )
        return self._rowcount(result)

    async def _reassign_inline_usage_events(self, *, source_user_id: uuid.UUID, target_user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            update(InlineUsageEvent)
            .where(InlineUsageEvent.user_id == source_user_id)
            .values(user_id=target_user_id)
        )
        return self._rowcount(result)

    def _require_telegram_link_configuration(self) -> tuple[str, str]:
        bot_username = self._telegram_link_bot_username
        if bot_username is None:
            raise ProviderNotConfiguredError("Telegram link bot username is not configured.")

        return_url = self._telegram_link_return_url
        if return_url is None:
            raise AuthConfigurationError("Telegram link return URL is not configured.")

        return bot_username, return_url

    @staticmethod
    def _generate_telegram_link_code() -> str:
        return secrets.token_urlsafe(TELEGRAM_LINK_CODE_BYTES)

    @staticmethod
    def _hash_telegram_link_code(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_telegram_deep_link(*, bot_username: str, code: str) -> str:
        return f"https://t.me/{bot_username}?start={TELEGRAM_LINK_START_PREFIX}{code}"

    @staticmethod
    def _normalize_telegram_link_code(code: str) -> str:
        normalized_code = code.strip()
        if (
            not normalized_code
            or len(normalized_code) > MAX_TELEGRAM_LINK_CODE_LENGTH
            or TELEGRAM_LINK_CODE_PATTERN.fullmatch(normalized_code) is None
        ):
            raise AccountLinkInvariantError("Telegram link code is invalid.")
        return normalized_code

    @staticmethod
    def _rowcount(result: object) -> int:
        rowcount = getattr(result, "rowcount", 0)
        return rowcount if isinstance(rowcount, int) else 0

    def _build_result(
        self,
        *,
        canonical_user: User,
        merge_performed: bool,
        merge_log_id: uuid.UUID | None,
        guest_user_id: uuid.UUID,
        deleted_guest_user_id: uuid.UUID | None,
        favorites_transferred: int,
        duplicate_favorites_skipped: int,
        analytics_events_transferred: int,
        inline_usage_events_transferred: int,
        views_transferred: int,
        details: Mapping[str, object],
    ) -> AccountLinkResult:
        return AccountLinkResult(
            user=UserRead.model_validate(canonical_user),
            linked_providers=self._build_linked_provider_projection(canonical_user),
            merge_performed=merge_performed,
            merge_log_id=merge_log_id,
            guest_user_id=guest_user_id,
            canonical_user_id=canonical_user.id,
            deleted_guest_user_id=deleted_guest_user_id,
            favorites_transferred=favorites_transferred,
            duplicate_favorites_skipped=duplicate_favorites_skipped,
            analytics_events_transferred=analytics_events_transferred,
            inline_usage_events_transferred=inline_usage_events_transferred,
            views_transferred=views_transferred,
            details=details,
        )

    @staticmethod
    def _build_linked_provider_projection(user: User) -> LinkedProvidersProjection:
        return LinkedProvidersProjection(
            email=user.email,
            email_verified_at=user.email_verified_at,
            has_password=user.password_hash is not None and bool(user.password_hash.strip()),
            google_linked=user.google_id is not None,
            telegram_linked=user.telegram_id is not None,
        )


__all__ = [
    "AccountLinkResult",
    "AccountLinkService",
    "LinkedProvidersProjection",
    "TelegramLinkStartResult",
]
