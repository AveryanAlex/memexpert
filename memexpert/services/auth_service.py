# ruff: noqa: TC003
"""JWT-backed guest-session auth service and refresh-token rotation helpers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, Self, cast

import jwt
from jwt import ExpiredSignatureError
from jwt.exceptions import InvalidTokenError as PyJWTInvalidTokenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.enums import AccountStatus, AccountType
from memexpert.models.user import RefreshToken, User
from memexpert.schemas.auth import AuthSessionRead, GuestBootstrapRequest, RefreshCookieMetadata
from memexpert.schemas.user import UserRead
from memexpert.services._auth_validation import (
    normalize_optional_text,
    require_non_blank,
    require_positive_int,
)
from memexpert.services.errors import (
    AccountUnavailableError,
    AuthConfigurationError,
    AuthenticatedUserNotFoundError,
    AuthServiceError,
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
    RefreshTokenReuseError,
    UpgradeRequiredError,
    UserStateMismatchError,
)
from memexpert.services.user_service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ACCESS_TOKEN_TYPE: Final = "access"
DEFAULT_REFRESH_TOKEN_BYTES: Final = 32
HS256_ALGORITHM: Final = "HS256"
REQUIRED_ACCESS_TOKEN_CLAIMS: Final[tuple[str, ...]] = ("sub", "type", "exp", "iat")
SUPPORTED_SAMESITE_VALUES: Final = frozenset({"lax", "none", "strict"})

type SameSiteMode = Literal["lax", "strict", "none"]


@dataclass(slots=True)
class AuthSession:
    """Internal auth-session result that carries the raw refresh secret for cookie setting."""

    access_token: str
    refresh_token: str
    user: UserRead
    expires_in: int
    refresh_cookie: RefreshCookieMetadata
    issued_at: datetime
    refresh_expires_at: datetime

    def to_read(self) -> AuthSessionRead:
        """Convert the internal session result into the public response schema."""

        return AuthSessionRead(
            access_token=self.access_token,
            expires_in=self.expires_in,
            user=self.user,
            refresh_cookie=self.refresh_cookie,
        )


class AuthService:
    """Issue, verify, and rotate JWT-backed guest sessions with DB-backed refresh tokens."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jwt_secret: str,
        access_token_ttl: timedelta,
        refresh_token_ttl: timedelta,
        access_token_algorithm: str = HS256_ALGORITHM,
        refresh_cookie_name: str = "memexpert_refresh_token",
        refresh_cookie_secure: bool = True,
        refresh_cookie_httponly: bool = True,
        refresh_cookie_samesite: str = "lax",
        refresh_cookie_path: str = "/api/v1/auth/refresh",
        refresh_cookie_domain: str | None = None,
        refresh_token_bytes: int = DEFAULT_REFRESH_TOKEN_BYTES,
        user_service: UserService | None = None,
    ) -> None:
        self._session: AsyncSession = session
        self._user_service: UserService = user_service or UserService(session)
        self._jwt_secret: str = self._require_jwt_secret(jwt_secret)
        self._access_token_ttl: timedelta = self._require_positive_ttl("access_token_ttl", access_token_ttl)
        self._refresh_token_ttl: timedelta = self._require_positive_ttl("refresh_token_ttl", refresh_token_ttl)
        self._access_token_algorithm: str = self._require_algorithm(access_token_algorithm)
        self._refresh_cookie_name: str = require_non_blank("refresh_cookie_name", refresh_cookie_name)
        self._refresh_cookie_secure: bool = refresh_cookie_secure
        self._refresh_cookie_httponly: bool = refresh_cookie_httponly
        self._refresh_cookie_samesite: SameSiteMode = self._normalize_same_site(refresh_cookie_samesite)
        self._refresh_cookie_path: str = require_non_blank("refresh_cookie_path", refresh_cookie_path)
        self._refresh_cookie_domain: str | None = normalize_optional_text(refresh_cookie_domain)
        self._refresh_token_bytes: int = require_positive_int("refresh_token_bytes", refresh_token_bytes)

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
            refresh_token_ttl=resolved_settings.auth_refresh_token_ttl,
            access_token_algorithm=resolved_settings.auth_access_token_algorithm,
            refresh_cookie_name=resolved_settings.auth_refresh_cookie_name,
            refresh_cookie_secure=resolved_settings.auth_refresh_cookie_secure,
            refresh_cookie_httponly=resolved_settings.auth_refresh_cookie_httponly,
            refresh_cookie_samesite=resolved_settings.auth_refresh_cookie_samesite,
            refresh_cookie_path=resolved_settings.auth_refresh_cookie_path,
            refresh_cookie_domain=resolved_settings.auth_refresh_cookie_domain,
            user_service=user_service,
        )

    async def create_guest_session(
        self,
        request: GuestBootstrapRequest | None = None,
    ) -> AuthSession:
        """Create a guest user via UserService and immediately issue a session for it."""

        resolved_request = request or GuestBootstrapRequest()
        guest_user = await self._user_service.create_guest_user(
            language=resolved_request.language,
            nsfw_enabled=resolved_request.nsfw_enabled,
        )
        return await self.issue_session_for_user(guest_user, device_info=resolved_request.device_info)

    async def issue_session_for_user(
        self,
        user: UserRead,
        *,
        device_info: str | None = None,
        reload_user: bool = True,
    ) -> AuthSession:
        """Issue a new access token and persisted opaque refresh token for a user."""

        current_user = await self._get_user_by_id(user.id) if reload_user else user
        self._ensure_account_is_available(current_user)
        issued_at = utcnow()
        access_token = self._encode_access_token(current_user, issued_at=issued_at)
        refresh_token = self._generate_refresh_token()
        refresh_expires_at = issued_at + self._refresh_token_ttl

        self._session.add(
            RefreshToken(
                user_id=current_user.id,
                token_hash=self.hash_refresh_token(refresh_token),
                device_info=normalize_optional_text(device_info),
                expires_at=refresh_expires_at,
            )
        )

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AuthServiceError("Failed to persist the refresh token.") from exc

        return AuthSession(
            access_token=access_token,
            refresh_token=refresh_token,
            user=current_user,
            expires_in=int(self._access_token_ttl.total_seconds()),
            refresh_cookie=self._build_refresh_cookie_metadata(),
            issued_at=issued_at,
            refresh_expires_at=refresh_expires_at,
        )

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        *,
        device_info: str | None = None,
    ) -> AuthSession:
        """Rotate a persisted refresh token transactionally and mint replacement credentials."""

        token_hash = self.hash_refresh_token(refresh_token)
        now = utcnow()
        result = await self._session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        refresh_token_row = result.scalar_one_or_none()
        if refresh_token_row is None:
            raise InvalidTokenError("Refresh token is invalid.")

        refresh_token_row.last_used_at = now

        if refresh_token_row.revoked_at is not None:
            await self._commit_diagnostics("Failed to persist revoked refresh-token reuse diagnostics.")
            raise RefreshTokenReuseError("Refresh token has already been revoked.")

        if refresh_token_row.expires_at <= now:
            await self._commit_diagnostics("Failed to persist expired refresh-token diagnostics.")
            raise ExpiredTokenError("Refresh token has expired.")

        current_user = await self._get_user_by_id(refresh_token_row.user_id)
        try:
            self._ensure_account_is_available(current_user)
        except AccountUnavailableError:
            await self._commit_diagnostics("Failed to persist unavailable-account refresh diagnostics.")
            raise
        replacement_refresh_token = self._generate_refresh_token()
        refresh_token_row.revoked_at = now
        replacement_expires_at = now + self._refresh_token_ttl
        access_token = self._encode_access_token(current_user, issued_at=now)

        self._session.add(
            RefreshToken(
                user_id=current_user.id,
                token_hash=self.hash_refresh_token(replacement_refresh_token),
                device_info=normalize_optional_text(device_info) or refresh_token_row.device_info,
                expires_at=replacement_expires_at,
            )
        )

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AuthServiceError("Failed to rotate the refresh token.") from exc

        return AuthSession(
            access_token=access_token,
            refresh_token=replacement_refresh_token,
            user=current_user,
            expires_in=int(self._access_token_ttl.total_seconds()),
            refresh_cookie=self._build_refresh_cookie_metadata(),
            issued_at=now,
            refresh_expires_at=replacement_expires_at,
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

        current_user = await self._get_user_by_id(user_id)
        token_account_type = payload.get("account_type")
        if token_account_type is not None and token_account_type != current_user.account_type.value:
            raise UserStateMismatchError(
                "Access token no longer matches the current persisted account state.",
            )

        self._ensure_account_is_available(current_user)

        if require_full_account and current_user.account_type is not AccountType.FULL:
            raise UpgradeRequiredError("A full account is required for this operation.")

        return current_user

    @staticmethod
    def hash_refresh_token(refresh_token: str) -> str:
        """Return the SHA256 hex digest used for persisted refresh-token storage."""

        normalized_refresh_token = refresh_token.strip()
        if not normalized_refresh_token:
            raise MissingTokenError("Refresh token is required.")
        return hashlib.sha256(normalized_refresh_token.encode("utf-8")).hexdigest()

    async def _get_user_by_id(self, user_id: uuid.UUID) -> UserRead:
        result = await self._session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise AuthenticatedUserNotFoundError(
                f"Authenticated user {user_id} no longer exists.",
            )
        return UserRead.model_validate(user)

    async def _commit_diagnostics(self, message: str) -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AuthServiceError(message) from exc

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
            "account_type": user.account_type.value,
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

    def _build_refresh_cookie_metadata(self) -> RefreshCookieMetadata:
        return RefreshCookieMetadata(
            name=self._refresh_cookie_name,
            path=self._refresh_cookie_path,
            max_age=int(self._refresh_token_ttl.total_seconds()),
            secure=self._refresh_cookie_secure,
            http_only=self._refresh_cookie_httponly,
            same_site=self._refresh_cookie_samesite,
            domain=self._refresh_cookie_domain,
        )

    def _generate_refresh_token(self) -> str:
        return secrets.token_urlsafe(self._refresh_token_bytes)

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

    @staticmethod
    def _normalize_same_site(value: str) -> SameSiteMode:
        normalized_value = value.strip().lower()
        if normalized_value not in SUPPORTED_SAMESITE_VALUES:
            raise AuthConfigurationError(
                "refresh_cookie_samesite must be one of: lax, strict, none.",
            )
        return cast("SameSiteMode", normalized_value)


__all__ = ["ACCESS_TOKEN_TYPE", "AuthService", "AuthSession", "HS256_ALGORITHM"]
