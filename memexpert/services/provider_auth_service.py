"""Provider-backed authentication flows layered on the shared JWT/session core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import bcrypt
from aiogram.utils import auth_widget
from aiogram.utils.web_app import safe_parse_webapp_init_data
from pydantic import ValidationError
from sqlalchemy import select

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.user import User
from memexpert.schemas.auth import (
    TelegramWidgetAuthRequest,
    normalize_auth_email,
    validate_auth_password,
)
from memexpert.schemas.user import UserRead
from memexpert.services.auth_service import AuthService, AuthSession
from memexpert.services.errors import (
    AuthConfigurationError,
    DuplicateIdentityError,
    EmailAlreadyInUseError,
    InvalidCredentialsError,
    ProviderNotConfiguredError,
    ProviderPayloadExpiredError,
    ProviderPayloadInvalidError,
)
from memexpert.services.user_service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    """Verified Telegram identity data shared by widget and Mini App auth flows."""

    telegram_id: int
    auth_date: datetime


class ProviderAuthService:
    """Resolve provider credentials into full accounts, then delegate session issuance."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        password_hash_rounds: int,
        telegram_bot_token: str | None = None,
        telegram_login_max_age_seconds: int = 300,
        telegram_miniapp_max_age_seconds: int = 300,
        auth_service: AuthService | None = None,
        user_service: UserService | None = None,
    ) -> None:
        self._session: AsyncSession = session
        self._password_hash_rounds: int = self._require_bcrypt_rounds(password_hash_rounds)
        self._telegram_bot_token: str | None = self._normalize_optional_text(telegram_bot_token)
        self._telegram_login_max_age_seconds: int = self._require_positive_int(
            "telegram_login_max_age_seconds",
            telegram_login_max_age_seconds,
        )
        self._telegram_miniapp_max_age_seconds: int = self._require_positive_int(
            "telegram_miniapp_max_age_seconds",
            telegram_miniapp_max_age_seconds,
        )
        self._auth_service: AuthService = auth_service or AuthService.from_settings(session)
        self._user_service: UserService = user_service or UserService(session)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        auth_service: AuthService | None = None,
        user_service: UserService | None = None,
    ) -> Self:
        """Build a provider-auth service from the shared runtime settings surface."""

        resolved_settings = settings or get_settings()
        telegram_bot_token = (
            resolved_settings.auth_telegram_bot_token.get_secret_value()
            if resolved_settings.auth_telegram_bot_token is not None
            else None
        )
        return cls(
            session,
            password_hash_rounds=resolved_settings.auth_bcrypt_rounds,
            telegram_bot_token=telegram_bot_token,
            telegram_login_max_age_seconds=resolved_settings.auth_telegram_login_max_age_seconds,
            telegram_miniapp_max_age_seconds=resolved_settings.auth_telegram_miniapp_max_age_seconds,
            auth_service=auth_service,
            user_service=user_service,
        )

    async def signup_with_email(
        self,
        *,
        email: str,
        password: str,
        device_info: str | None = None,
    ) -> AuthSession:
        """Create a full account with a bcrypt password hash and issue a session."""

        normalized_email = self._normalize_email(email)
        normalized_password = self._validate_password(password)
        password_hash = self._hash_password(normalized_password)

        try:
            user = await self._user_service.create_full_user(
                email=normalized_email,
                password_hash=password_hash,
                commit=False,
            )
        except DuplicateIdentityError as exc:
            raise EmailAlreadyInUseError("Email is already in use.") from exc

        return await self._auth_service.issue_session_for_user(
            user,
            device_info=device_info,
            reload_user=False,
        )

    async def login_with_email(
        self,
        *,
        email: str,
        password: str,
        device_info: str | None = None,
    ) -> AuthSession:
        """Authenticate an existing full account via normalized email and bcrypt."""

        normalized_email = self._normalize_email(email)
        normalized_password = self._validate_password(password)
        result = await self._session.execute(select(User).where(User.email == normalized_email))
        user = result.scalar_one_or_none()
        if user is None:
            raise InvalidCredentialsError("Email or password is invalid.")

        password_hash = self._require_stored_password_hash(user.password_hash)
        if not self._check_password(normalized_password, password_hash):
            raise InvalidCredentialsError("Email or password is invalid.")

        return await self._auth_service.issue_session_for_user(
            UserRead.model_validate(user),
            device_info=device_info,
            reload_user=False,
        )

    async def authenticate_with_telegram_widget(
        self,
        *,
        payload: TelegramWidgetAuthRequest,
        device_info: str | None = None,
    ) -> AuthSession:
        """Validate Telegram Login Widget payloads and issue a full-account session."""

        identity = self._verify_telegram_widget_payload(payload)
        return await self._authenticate_with_telegram_identity(identity, device_info=device_info)

    async def authenticate_with_telegram_miniapp(
        self,
        *,
        init_data: str | None,
        device_info: str | None = None,
    ) -> AuthSession:
        """Validate Telegram Mini App initData and issue a full-account session."""

        identity = self._verify_telegram_miniapp_payload(init_data)
        return await self._authenticate_with_telegram_identity(identity, device_info=device_info)

    async def _authenticate_with_telegram_identity(
        self,
        identity: TelegramIdentity,
        *,
        device_info: str | None = None,
    ) -> AuthSession:
        resolved_user = await self._user_service.get_by_telegram_id(identity.telegram_id)
        if resolved_user is None:
            try:
                resolved_user = await self._user_service.create_full_user(
                    telegram_id=identity.telegram_id,
                    commit=False,
                )
            except DuplicateIdentityError as exc:
                existing_user = await self._user_service.get_by_telegram_id(identity.telegram_id)
                if existing_user is None:
                    raise ProviderPayloadInvalidError(
                        "Telegram identity could not be resolved safely.",
                    ) from exc
                resolved_user = existing_user

        return await self._auth_service.issue_session_for_user(
            resolved_user,
            device_info=device_info,
            reload_user=False,
        )

    def _verify_telegram_widget_payload(
        self,
        payload: TelegramWidgetAuthRequest,
    ) -> TelegramIdentity:
        bot_token = self._require_telegram_bot_token()
        if payload.id is None:
            raise ProviderPayloadInvalidError("Telegram login payload is missing a user identifier.")
        if payload.auth_date is None:
            raise ProviderPayloadInvalidError("Telegram login payload is missing auth_date.")
        if payload.hash is None or not payload.hash.strip():
            raise ProviderPayloadInvalidError("Telegram login payload is missing hash.")

        widget_data = payload.model_dump(mode="python", exclude_none=True)
        if not auth_widget.check_integrity(bot_token, widget_data):
            raise ProviderPayloadInvalidError("Telegram login payload signature is invalid.")

        auth_date = self._coerce_auth_date(payload.auth_date, source="Telegram login")
        self._ensure_payload_is_fresh(
            auth_date=auth_date,
            max_age_seconds=self._telegram_login_max_age_seconds,
            source="Telegram login",
        )
        return TelegramIdentity(telegram_id=payload.id, auth_date=auth_date)

    def _verify_telegram_miniapp_payload(self, init_data: str | None) -> TelegramIdentity:
        bot_token = self._require_telegram_bot_token()
        if init_data is None or not init_data.strip():
            raise ProviderPayloadInvalidError("Telegram Mini App init data is required.")

        try:
            parsed_init_data = safe_parse_webapp_init_data(bot_token, init_data)
        except ValidationError as exc:
            raise ProviderPayloadInvalidError("Telegram Mini App init data is malformed.") from exc
        except ValueError as exc:
            raise ProviderPayloadInvalidError("Telegram Mini App init data is invalid.") from exc

        if parsed_init_data.user is None:
            raise ProviderPayloadInvalidError("Telegram Mini App init data is missing a user.")

        auth_date = self._coerce_auth_date(parsed_init_data.auth_date, source="Telegram Mini App")
        self._ensure_payload_is_fresh(
            auth_date=auth_date,
            max_age_seconds=self._telegram_miniapp_max_age_seconds,
            source="Telegram Mini App",
        )
        return TelegramIdentity(
            telegram_id=parsed_init_data.user.id,
            auth_date=auth_date,
        )

    @staticmethod
    def _normalize_email(email: str) -> str:
        try:
            return normalize_auth_email(email)
        except ValueError as exc:
            raise InvalidCredentialsError("Email or password is invalid.") from exc

    @staticmethod
    def _validate_password(password: str) -> str:
        try:
            return validate_auth_password(password)
        except ValueError as exc:
            raise InvalidCredentialsError("Email or password is invalid.") from exc

    def _require_telegram_bot_token(self) -> str:
        if self._telegram_bot_token is None:
            raise ProviderNotConfiguredError("Telegram auth is not configured.")
        return self._telegram_bot_token

    @staticmethod
    def _coerce_auth_date(auth_date: int | datetime, *, source: str) -> datetime:
        if isinstance(auth_date, datetime):
            resolved_auth_date = auth_date
            if resolved_auth_date.tzinfo is None:
                resolved_auth_date = resolved_auth_date.replace(tzinfo=UTC)
            return resolved_auth_date.astimezone(UTC)

        try:
            return datetime.fromtimestamp(auth_date, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ProviderPayloadInvalidError(f"{source} auth_date is invalid.") from exc

    @staticmethod
    def _ensure_payload_is_fresh(
        *,
        auth_date: datetime,
        max_age_seconds: int,
        source: str,
    ) -> None:
        age_seconds = (utcnow() - auth_date).total_seconds()
        if age_seconds < 0:
            raise ProviderPayloadInvalidError(f"{source} auth_date is invalid.")
        if age_seconds > max_age_seconds:
            raise ProviderPayloadExpiredError(f"{source} payload has expired.")

    def _hash_password(self, password: str) -> str:
        password_bytes = password.encode("utf-8")
        try:
            return bcrypt.hashpw(
                password_bytes,
                bcrypt.gensalt(rounds=self._password_hash_rounds),
            ).decode("utf-8")
        except ValueError as exc:
            raise AuthConfigurationError("Password hashing configuration is invalid.") from exc

    @staticmethod
    def _check_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except ValueError as exc:
            raise InvalidCredentialsError("Password login is not available for this account.") from exc

    @staticmethod
    def _require_stored_password_hash(password_hash: str | None) -> str:
        if password_hash is None or not password_hash.strip():
            raise InvalidCredentialsError("Password login is not available for this account.")
        return password_hash

    @staticmethod
    def _require_bcrypt_rounds(rounds: int) -> int:
        if not 4 <= rounds <= 31:
            raise AuthConfigurationError("password_hash_rounds must be between 4 and 31.")
        return rounds

    @staticmethod
    def _require_positive_int(field_name: str, value: int) -> int:
        if value <= 0:
            raise AuthConfigurationError(f"{field_name} must be greater than zero.")
        return value

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        if value is None:
            return None

        normalized_value = value.strip()
        return normalized_value or None


__all__ = ["ProviderAuthService"]
