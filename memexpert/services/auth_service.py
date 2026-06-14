# ruff: noqa: TC003
"""JWT-backed auth service with per-user nonce revocation and login-event audit."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Self

import jwt
from jwt import ExpiredSignatureError
from jwt.exceptions import InvalidTokenError as PyJWTInvalidTokenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.enums import AccountStatus, AccountType
from memexpert.models.user import AccountMergeLog, LoginEvent, User
from memexpert.schemas.auth import AuthSessionRead, GuestBootstrapRequest
from memexpert.schemas.user import UserRead
from memexpert.services.errors import (
    AccountUnavailableError,
    AuthConfigurationError,
    AuthenticatedUserNotFoundError,
    AuthServiceError,
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
    UpgradeRequiredError,
)
from memexpert.services.user_service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ACCESS_TOKEN_TYPE: Final = "access"
HS256_ALGORITHM: Final = "HS256"
REQUIRED_ACCESS_TOKEN_CLAIMS: Final[tuple[str, ...]] = ("sub", "type", "exp", "iat", "nonce")


@dataclass(slots=True)
class AuthSession:
    """Internal auth-session result returned by every auth write path."""

    access_token: str
    user: UserRead
    expires_in: int
    issued_at: datetime

    def to_read(self) -> AuthSessionRead:
        """Convert the internal session result into the public response schema.

        The raw access token stays on the ``AuthSession`` dataclass so
        routes can attach it to the outgoing ``Set-Cookie`` header; the
        public ``AuthSessionRead`` schema deliberately omits it so the
        token never leaks into response bodies or access logs.
        """

        return AuthSessionRead(user=self.user)


class AuthService:
    """Issue and verify JWT-backed sessions with nonce-based revocation.

    Every access token carries a ``nonce`` claim snapshot of the user's
    ``token_nonce`` column at issue time. Verification compares the claim
    against the live column; a mismatch invalidates the token. "Log out
    everywhere" bumps the column, which atomically kills every outstanding
    JWT for that user without any server-side session store.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        jwt_secret: str,
        access_token_ttl: timedelta,
        access_token_algorithm: str = HS256_ALGORITHM,
        user_service: UserService | None = None,
    ) -> None:
        self._session: AsyncSession = session
        self._user_service: UserService = user_service or UserService(session)
        self._jwt_secret: str = self._require_jwt_secret(jwt_secret)
        self._access_token_ttl: timedelta = self._require_positive_ttl("access_token_ttl", access_token_ttl)
        self._access_token_algorithm: str = self._require_algorithm(access_token_algorithm)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        user_service: UserService | None = None,
    ) -> Self:
        """Build an auth service from the shared runtime settings surface."""

        resolved_settings = settings or get_settings()
        return cls(
            session,
            jwt_secret=resolved_settings.auth_jwt_secret.get_secret_value(),
            access_token_ttl=resolved_settings.auth_access_token_ttl,
            access_token_algorithm=resolved_settings.auth_access_token_algorithm,
            user_service=user_service,
        )

    async def create_guest_session(
        self,
        request: GuestBootstrapRequest | None = None,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthSession:
        """Create a guest user via UserService and immediately issue a session for it."""

        resolved_request = request or GuestBootstrapRequest()
        guest_user = await self._user_service.create_guest_user(
            language=resolved_request.language,
            nsfw_enabled=resolved_request.nsfw_enabled,
        )
        return await self.issue_session_for_user(
            guest_user,
            ip_address=ip_address,
            user_agent=user_agent or resolved_request.device_info,
        )

    async def issue_session_for_user(
        self,
        user: UserRead,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        reload_user: bool = True,
    ) -> AuthSession:
        """Mint a fresh access token and record a login-event audit row."""

        current_user = await self._get_user_by_id(user.id) if reload_user else user
        self._ensure_account_is_available(current_user)
        issued_at = utcnow()
        access_token = self._encode_access_token(current_user, issued_at=issued_at)

        self._session.add(
            LoginEvent(
                user_id=current_user.id,
                ip_address=_truncate(ip_address, 45),
                user_agent=_truncate(user_agent, 2048),
                occurred_at=issued_at,
            )
        )

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AuthServiceError("Failed to persist the login event.") from exc

        return AuthSession(
            access_token=access_token,
            user=current_user,
            expires_in=int(self._access_token_ttl.total_seconds()),
            issued_at=issued_at,
        )

    async def verify_access_token(
        self,
        access_token: str,
        *,
        require_full_account: bool = False,
    ) -> UserRead:
        """Decode an access token, reload the current user row, and enforce auth rules."""

        payload = self._decode_access_token(access_token)
        subject = payload.get("sub")
        if not isinstance(subject, str):
            raise InvalidTokenError("Access token subject is invalid.")

        try:
            user_id = uuid.UUID(subject)
        except ValueError as exc:
            raise InvalidTokenError("Access token subject is invalid.") from exc

        claimed_nonce = payload.get("nonce")
        if not isinstance(claimed_nonce, int):
            raise InvalidTokenError("Access token nonce is invalid.")

        current_user = await self._get_user_by_id(user_id)
        if current_user.token_nonce != claimed_nonce:
            raise InvalidTokenError(
                "Session has been revoked; please sign in again.",
            )

        self._ensure_account_is_available(current_user)

        if require_full_account and current_user.account_type is not AccountType.FULL:
            raise UpgradeRequiredError("A full account is required for this operation.")

        return current_user

    async def refresh_session_from_access_token(
        self,
        access_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthSession:
        """Issue a fresh cookie session for a browser returning from an external link flow.

        Normal verification reloads the current user row and derives account type from
        linked identities instead of trusting a stale token claim. This refresh path is
        narrower: it still requires a valid, unexpired cookie token and matching nonce
        for surviving users, while also handling guest rows merged into existing full accounts.
        When the guest was merged into an existing full account, the merge audit log is
        used to move the browser session to the canonical target account.
        """

        user_id, claimed_nonce = self._decode_subject_and_nonce(access_token)
        current_user = await self._get_user_by_id_or_none(user_id)

        if current_user is None:
            current_user = await self._get_latest_merge_target_for_guest(user_id)
            if current_user is None:
                raise AuthenticatedUserNotFoundError(
                    f"Authenticated user {user_id} no longer exists.",
                )
        elif current_user.token_nonce != claimed_nonce:
            raise InvalidTokenError(
                "Session has been revoked; please sign in again.",
            )

        self._ensure_account_is_available(current_user)
        return await self.issue_session_for_user(
            current_user,
            ip_address=ip_address,
            user_agent=user_agent,
            reload_user=False,
        )

    async def _get_user_by_id(self, user_id: uuid.UUID) -> UserRead:
        user = await self._get_user_by_id_or_none(user_id)
        if user is None:
            raise AuthenticatedUserNotFoundError(
                f"Authenticated user {user_id} no longer exists.",
            )
        return user

    async def _get_user_by_id_or_none(self, user_id: uuid.UUID) -> UserRead | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return None
        return UserRead.model_validate(user)

    async def _get_latest_merge_target_for_guest(self, guest_user_id: uuid.UUID) -> UserRead | None:
        result = await self._session.execute(
            select(User)
            .join(AccountMergeLog, AccountMergeLog.target_account_id == User.id)
            .where(AccountMergeLog.guest_account_id == guest_user_id)
            .order_by(AccountMergeLog.created_at.desc())
            .limit(1)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        return UserRead.model_validate(user)

    def _decode_subject_and_nonce(self, access_token: str) -> tuple[uuid.UUID, int]:
        payload = self._decode_access_token(access_token)
        subject = payload.get("sub")
        if not isinstance(subject, str):
            raise InvalidTokenError("Access token subject is invalid.")

        try:
            user_id = uuid.UUID(subject)
        except ValueError as exc:
            raise InvalidTokenError("Access token subject is invalid.") from exc

        claimed_nonce = payload.get("nonce")
        if not isinstance(claimed_nonce, int):
            raise InvalidTokenError("Access token nonce is invalid.")

        return user_id, claimed_nonce

    @staticmethod
    def _ensure_account_is_available(user: UserRead) -> None:
        if user.status is not AccountStatus.ACTIVE:
            raise AccountUnavailableError("Account is not available.")

    def _encode_access_token(self, user: UserRead, *, issued_at: datetime) -> str:
        expires_at = issued_at + self._access_token_ttl
        payload: dict[str, object] = {
            "sub": str(user.id),
            "type": ACCESS_TOKEN_TYPE,
            "iat": issued_at,
            "exp": expires_at,
            "nonce": user.token_nonce,
        }
        return jwt.encode(
            payload,
            self._jwt_secret,
            algorithm=self._access_token_algorithm,
        )

    def _decode_access_token(self, access_token: str) -> dict[str, object]:
        normalized_access_token = access_token.strip()
        if not normalized_access_token:
            raise MissingTokenError("Access token is required.")

        try:
            payload: dict[str, object] = jwt.decode(
                normalized_access_token,
                self._jwt_secret,
                algorithms=[self._access_token_algorithm],
                options={"require": list(REQUIRED_ACCESS_TOKEN_CLAIMS)},
            )
        except ExpiredSignatureError as exc:
            raise ExpiredTokenError("Access token has expired.") from exc
        except PyJWTInvalidTokenError as exc:
            raise InvalidTokenError("Access token is invalid.") from exc

        if payload.get("type") != ACCESS_TOKEN_TYPE:
            raise InvalidTokenError("Access token has an unexpected type.")

        return payload

    @staticmethod
    def _require_jwt_secret(value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise AuthConfigurationError("jwt_secret must not be blank.")
        if len(normalized_value.encode("utf-8")) < 32:
            raise AuthConfigurationError("jwt_secret must be at least 32 bytes long.")
        return normalized_value

    @classmethod
    def _require_positive_ttl(cls, field_name: str, value: timedelta) -> timedelta:
        if value.total_seconds() <= 0:
            raise AuthConfigurationError(f"{field_name} must be greater than zero.")
        return value

    @staticmethod
    def _require_algorithm(value: str) -> str:
        normalized_value = value.strip()
        if normalized_value != HS256_ALGORITHM:
            raise AuthConfigurationError(
                f"access_token_algorithm must be {HS256_ALGORITHM} for this auth flow.",
            )
        return normalized_value


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:limit]


__all__ = ["ACCESS_TOKEN_TYPE", "AuthService", "AuthSession", "HS256_ALGORITHM"]
