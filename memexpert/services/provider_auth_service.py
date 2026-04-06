"""Provider-backed authentication flows layered on the shared JWT/session core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self, cast

import bcrypt
import httpx
from aiogram.utils import auth_widget
from aiogram.utils.web_app import safe_parse_webapp_init_data
from pydantic import ValidationError
from sqlalchemy import select

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.enums import AccountStatus
from memexpert.models.user import User
from memexpert.schemas.auth import (
    TelegramWidgetAuthRequest,
    normalize_auth_email,
    validate_auth_password,
)
from memexpert.schemas.user import UserRead
from memexpert.services.auth_service import AuthService, AuthSession
from memexpert.services.errors import (
    AccountUnavailableError,
    AuthConfigurationError,
    DuplicateIdentityError,
    EmailAlreadyInUseError,
    InvalidCredentialsError,
    ProviderAccessDeniedError,
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


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """Normalized Google identity data resolved from token and userinfo calls."""

    google_id: str
    email: str | None
    email_verified_at: datetime | None


@dataclass(frozen=True, slots=True)
class EmailSignupIdentity:
    """Validated email-signup identity data prepared for account-link upgrades."""

    email: str
    password_hash: str


@dataclass(frozen=True, slots=True)
class GoogleIdentityResolution:
    """Resolved Google identity state without coupling the caller to session issuance."""

    identity: GoogleIdentity
    user: UserRead | None
    matched_by: Literal["google_id", "verified_email", "none"]


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
        google_client_id: str | None = None,
        google_client_secret: str | None = None,
        google_redirect_uri: str | None = None,
        google_token_url: str = "https://oauth2.googleapis.com/token",
        google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo",
        google_timeout_seconds: float = 10.0,
        google_http_transport: httpx.AsyncBaseTransport | None = None,
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
        self._google_client_id: str | None = self._normalize_optional_text(google_client_id)
        self._google_client_secret: str | None = self._normalize_optional_text(google_client_secret)
        self._google_redirect_uri: str | None = self._normalize_optional_text(google_redirect_uri)
        self._google_token_url: str = self._require_non_blank("google_token_url", google_token_url)
        self._google_userinfo_url: str = self._require_non_blank("google_userinfo_url", google_userinfo_url)
        self._google_timeout_seconds: float = self._require_positive_float(
            "google_timeout_seconds",
            google_timeout_seconds,
        )
        self._google_http_transport: httpx.AsyncBaseTransport | None = google_http_transport
        self._auth_service: AuthService = auth_service or AuthService.from_settings(session)
        self._user_service: UserService = user_service or UserService(session)

    @classmethod
    def from_settings(
        cls,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        google_http_transport: httpx.AsyncBaseTransport | None = None,
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
        google_client_secret = (
            resolved_settings.auth_google_client_secret.get_secret_value()
            if resolved_settings.auth_google_client_secret is not None
            else None
        )
        return cls(
            session,
            password_hash_rounds=resolved_settings.auth_bcrypt_rounds,
            telegram_bot_token=telegram_bot_token,
            telegram_login_max_age_seconds=resolved_settings.auth_telegram_login_max_age_seconds,
            telegram_miniapp_max_age_seconds=resolved_settings.auth_telegram_miniapp_max_age_seconds,
            google_client_id=resolved_settings.auth_google_client_id,
            google_client_secret=google_client_secret,
            google_redirect_uri=resolved_settings.auth_google_redirect_uri,
            google_token_url=resolved_settings.auth_google_token_url,
            google_userinfo_url=resolved_settings.auth_google_userinfo_url,
            google_timeout_seconds=resolved_settings.auth_google_timeout_seconds,
            google_http_transport=google_http_transport,
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

        signup_identity = self.prepare_email_signup_identity(email=email, password=password)

        try:
            user = await self._user_service.create_full_user(
                email=signup_identity.email,
                password_hash=signup_identity.password_hash,
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

        user = await self.resolve_email_login_user(email=email, password=password)
        return await self._auth_service.issue_session_for_user(
            user,
            device_info=device_info,
            reload_user=False,
        )

    async def authenticate_with_google_code(
        self,
        *,
        code: str,
        device_info: str | None = None,
    ) -> AuthSession:
        """Exchange a Google OAuth code, resolve the full account, and issue a session."""

        google_identity = await self.resolve_google_identity_from_code(code=code)
        return await self._authenticate_with_google_identity(google_identity, device_info=device_info)

    async def authenticate_with_telegram_widget(
        self,
        *,
        payload: TelegramWidgetAuthRequest,
        device_info: str | None = None,
    ) -> AuthSession:
        """Validate Telegram Login Widget payloads and issue a full-account session."""

        identity = self.resolve_telegram_widget_identity(payload)
        return await self._authenticate_with_telegram_identity(identity, device_info=device_info)

    async def authenticate_with_telegram_miniapp(
        self,
        *,
        init_data: str | None,
        device_info: str | None = None,
    ) -> AuthSession:
        """Validate Telegram Mini App initData and issue a full-account session."""

        identity = self.resolve_telegram_miniapp_identity(init_data)
        return await self._authenticate_with_telegram_identity(identity, device_info=device_info)

    def prepare_email_signup_identity(self, *, email: str, password: str) -> EmailSignupIdentity:
        """Validate email-signup credentials and prepare a password hash without writing a session."""

        normalized_email = self._normalize_email(email)
        normalized_password = self._validate_password(password)
        return EmailSignupIdentity(
            email=normalized_email,
            password_hash=self._hash_password(normalized_password),
        )

    async def resolve_email_login_user(self, *, email: str, password: str) -> UserRead:
        """Validate email-login credentials and return the canonical full account."""

        normalized_email = self._normalize_email(email)
        normalized_password = self._validate_password(password)
        result = await self._session.execute(select(User).where(User.email == normalized_email))
        user = result.scalar_one_or_none()
        if user is None:
            raise InvalidCredentialsError("Email or password is invalid.")

        password_hash = self._require_stored_password_hash(user.password_hash)
        if not self._check_password(normalized_password, password_hash):
            raise InvalidCredentialsError("Email or password is invalid.")

        resolved_user = UserRead.model_validate(user)
        self._ensure_account_is_available(resolved_user)
        return resolved_user

    async def resolve_google_identity_from_code(self, *, code: str) -> GoogleIdentity:
        """Exchange a Google authorization code for a normalized identity payload."""

        return await self._exchange_google_code_for_identity(code)

    async def resolve_google_identity(self, identity: GoogleIdentity) -> GoogleIdentityResolution:
        """Resolve whether a Google identity already maps to an existing full account."""

        existing_google_user = await self._user_service.get_by_google_id(identity.google_id)
        if existing_google_user is not None:
            self._ensure_account_is_available(existing_google_user)
            return GoogleIdentityResolution(
                identity=identity,
                user=existing_google_user,
                matched_by="google_id",
            )

        if identity.email is not None and identity.email_verified_at is not None:
            reusable_user = await self._user_service.get_by_email(identity.email)
            if reusable_user is not None:
                self._ensure_google_reuse_is_allowed(reusable_user)
                return GoogleIdentityResolution(
                    identity=identity,
                    user=reusable_user,
                    matched_by="verified_email",
                )

        return GoogleIdentityResolution(identity=identity, user=None, matched_by="none")

    async def resolve_telegram_identity_user(self, identity: TelegramIdentity) -> UserRead | None:
        """Resolve whether a Telegram identity already maps to an existing account."""

        return await self._user_service.get_by_telegram_id(identity.telegram_id)

    def resolve_telegram_widget_identity(self, payload: TelegramWidgetAuthRequest) -> TelegramIdentity:
        """Validate a Telegram Login Widget payload without issuing a session."""

        return self._verify_telegram_widget_payload(payload)

    def resolve_telegram_miniapp_identity(self, init_data: str | None) -> TelegramIdentity:
        """Validate Telegram Mini App initData without issuing a session."""

        return self._verify_telegram_miniapp_payload(init_data)

    async def _authenticate_with_google_identity(
        self,
        identity: GoogleIdentity,
        *,
        device_info: str | None = None,
    ) -> AuthSession:
        resolution = await self.resolve_google_identity(identity)
        if resolution.user is not None:
            if resolution.matched_by == "verified_email":
                linked_user = await self._user_service.attach_google_identity(
                    user_id=resolution.user.id,
                    google_id=identity.google_id,
                    email_verified_at=identity.email_verified_at,
                    commit=False,
                )
                return await self._auth_service.issue_session_for_user(
                    linked_user,
                    device_info=device_info,
                )

            return await self._auth_service.issue_session_for_user(
                resolution.user,
                device_info=device_info,
            )

        try:
            created_user = await self._user_service.create_full_user(
                google_id=identity.google_id,
                email=identity.email,
                email_verified_at=identity.email_verified_at,
                commit=False,
            )
        except DuplicateIdentityError as exc:
            resolved_user = await self._user_service.get_by_google_id(identity.google_id)
            if resolved_user is not None:
                return await self._auth_service.issue_session_for_user(
                    resolved_user,
                    device_info=device_info,
                )
            if identity.email is not None:
                conflicting_user = await self._user_service.get_by_email(identity.email)
                if conflicting_user is not None:
                    self._ensure_google_reuse_is_allowed(conflicting_user)
                    raise EmailAlreadyInUseError(
                        "Verified Google email is already linked to another full account.",
                    ) from exc
            raise ProviderPayloadInvalidError("Google identity could not be resolved safely.") from exc

        return await self._auth_service.issue_session_for_user(
            created_user,
            device_info=device_info,
        )

    async def _authenticate_with_telegram_identity(
        self,
        identity: TelegramIdentity,
        *,
        device_info: str | None = None,
    ) -> AuthSession:
        resolved_user = await self.resolve_telegram_identity_user(identity)
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

    async def _exchange_google_code_for_identity(self, code: str) -> GoogleIdentity:
        normalized_code = self._require_google_code(code)
        google_client_id, google_client_secret, google_redirect_uri = self._require_google_configuration()

        async with httpx.AsyncClient(
            transport=self._google_http_transport,
            timeout=self._google_timeout_seconds,
        ) as client:
            token_response = await self._perform_google_request(
                client,
                method="POST",
                url=self._google_token_url,
                source="token exchange",
                data={
                    "code": normalized_code,
                    "client_id": google_client_id,
                    "client_secret": google_client_secret,
                    "redirect_uri": google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_payload = self._decode_google_json(token_response, source="token response")
            access_token = self._extract_google_access_token(token_payload)
            userinfo_response = await self._perform_google_request(
                client,
                method="GET",
                url=self._google_userinfo_url,
                source="userinfo request",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        userinfo_payload = self._decode_google_json(userinfo_response, source="userinfo response")
        return self._normalize_google_identity(userinfo_payload)

    async def _perform_google_request(
        self,
        client: httpx.AsyncClient,
        *,
        method: str,
        url: str,
        source: str,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                data=data,
            )
        except httpx.TimeoutException as exc:
            raise ProviderAccessDeniedError(f"Google {source} timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderAccessDeniedError(f"Google {source} failed.") from exc

        if response.is_success:
            return response

        if response.status_code in {400, 401, 403}:
            raise ProviderAccessDeniedError(f"Google {source} was denied.")
        raise ProviderAccessDeniedError(
            f"Google {source} failed with status {response.status_code}.",
        )

    def _normalize_google_identity(self, payload: dict[str, object]) -> GoogleIdentity:
        google_id = self._normalize_google_subject(payload.get("sub"))
        email_value = payload.get("email")
        normalized_email: str | None = None
        if email_value is not None:
            normalized_email = self._normalize_google_email(email_value)

        email_verified = self._coerce_google_email_verified(
            payload.get("email_verified"),
            require_field=normalized_email is not None,
        )
        if email_verified:
            if normalized_email is None:
                raise ProviderPayloadInvalidError("Google userinfo is missing a verified email address.")
            return GoogleIdentity(
                google_id=google_id,
                email=normalized_email,
                email_verified_at=utcnow(),
            )

        return GoogleIdentity(
            google_id=google_id,
            email=None,
            email_verified_at=None,
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

    def _require_google_configuration(self) -> tuple[str, str, str]:
        if (
            self._google_client_id is None
            or self._google_client_secret is None
            or self._google_redirect_uri is None
        ):
            raise ProviderNotConfiguredError("Google auth is not configured.")
        return self._google_client_id, self._google_client_secret, self._google_redirect_uri

    def _require_telegram_bot_token(self) -> str:
        if self._telegram_bot_token is None:
            raise ProviderNotConfiguredError("Telegram auth is not configured.")
        return self._telegram_bot_token

    @staticmethod
    def _ensure_account_is_available(user: UserRead) -> None:
        if user.status is not AccountStatus.ACTIVE:
            raise AccountUnavailableError("Account is not available.")

    def _ensure_google_reuse_is_allowed(self, user: UserRead) -> None:
        self._ensure_account_is_available(user)
        if user.telegram_id is not None:
            raise EmailAlreadyInUseError(
                "Verified Google email is already linked to a Telegram-backed account.",
            )
        if user.google_id is not None:
            raise EmailAlreadyInUseError(
                "Verified Google email is already linked to another Google-backed account.",
            )

    def _extract_google_access_token(self, payload: dict[str, object]) -> str:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ProviderPayloadInvalidError("Google token response is missing access_token.")
        return access_token.strip()

    def _normalize_google_subject(self, subject: object) -> str:
        if not isinstance(subject, str):
            raise ProviderPayloadInvalidError("Google userinfo is missing sub.")
        try:
            normalized_subject = self._user_service.normalize_google_id(subject)
        except Exception as exc:
            raise ProviderPayloadInvalidError("Google userinfo sub is invalid.") from exc
        if normalized_subject is None:  # pragma: no cover - defensive branch
            raise ProviderPayloadInvalidError("Google userinfo is missing sub.")
        return normalized_subject

    @staticmethod
    def _normalize_google_email(email: object) -> str:
        if not isinstance(email, str):
            raise ProviderPayloadInvalidError("Google userinfo email is invalid.")
        try:
            return normalize_auth_email(email)
        except ValueError as exc:
            raise ProviderPayloadInvalidError("Google userinfo email is invalid.") from exc

    @staticmethod
    def _coerce_google_email_verified(value: object, *, require_field: bool) -> bool:
        if value is None:
            if require_field:
                raise ProviderPayloadInvalidError("Google userinfo email_verified is missing.")
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized_value = value.strip().lower()
            if normalized_value in {"1", "true"}:
                return True
            if normalized_value in {"0", "false"}:
                return False
        raise ProviderPayloadInvalidError("Google userinfo email_verified is invalid.")

    @staticmethod
    def _require_google_code(code: str) -> str:
        normalized_code = code.strip()
        if not normalized_code:
            raise ProviderPayloadInvalidError("Google authorization code is required.")
        return normalized_code

    @staticmethod
    def _decode_google_json(response: httpx.Response, *, source: str) -> dict[str, object]:
        try:
            raw_payload = cast("object", response.json())
        except ValueError as exc:
            raise ProviderPayloadInvalidError(f"Google {source} is malformed.") from exc
        if not isinstance(raw_payload, dict):
            raise ProviderPayloadInvalidError(f"Google {source} is malformed.")

        typed_payload = cast("dict[object, object]", raw_payload)
        payload: dict[str, object] = {}
        for key, value in typed_payload.items():
            payload[str(key)] = value
        return payload

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
    def _require_positive_float(field_name: str, value: float) -> float:
        if value <= 0:
            raise AuthConfigurationError(f"{field_name} must be greater than zero.")
        return value

    @staticmethod
    def _require_non_blank(field_name: str, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise AuthConfigurationError(f"{field_name} must not be blank.")
        return normalized_value

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        if value is None:
            return None

        normalized_value = value.strip()
        return normalized_value or None


__all__ = [
    "EmailSignupIdentity",
    "GoogleIdentity",
    "GoogleIdentityResolution",
    "ProviderAuthService",
    "TelegramIdentity",
]
