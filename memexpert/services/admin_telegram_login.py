"""Browser-admin Telegram login orchestration using QR or phone-code auth."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import SecretStr
from sqlalchemy import select

from memexpert.core.config import get_settings
from memexpert.crawlers.telegram.session_crypto import TelegramStringSessionDecryptError
from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSession, TelegramSessionLoginAttempt
from memexpert.models.enums import TelegramSessionStatus
from memexpert.schemas.admin import (
    AdminTelegramLoginCompleteRead,
    AdminTelegramLoginPasswordRequest,
    AdminTelegramLoginPhoneCodeRequest,
    AdminTelegramLoginPhoneStartRead,
    AdminTelegramLoginPhoneStartRequest,
    AdminTelegramLoginQrCompleteRequest,
    AdminTelegramLoginQrStartRead,
)
from memexpert.services.admin import (
    MAX_TELEGRAM_ERROR_TEXT_LENGTH,
    AdminConflictError,
    AdminNotFoundError,
    AdminService,
    AdminTelegramAccountProjection,
    _phone_hint,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


LOGIN_ATTEMPT_TTL = timedelta(minutes=10)
_PASSWORD_REQUIRED_ERROR_CLASS = "SessionPasswordNeededError"


class TelegramLoginClient(Protocol):
    """Small Telethon client surface used by browser-admin login tests."""

    session: Any

    async def connect(self) -> None:
        raise NotImplementedError

    async def disconnect(self) -> None:
        raise NotImplementedError

    async def send_code_request(self, phone: str) -> Any:
        raise NotImplementedError

    async def sign_in(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def get_me(self) -> Any:
        raise NotImplementedError

    async def qr_login(self) -> Any:
        raise NotImplementedError


@dataclass(slots=True)
class _LiveLoginAttempt:
    client: TelegramLoginClient
    qr_login: Any | None = None
    phone_number: str | None = None


_LIVE_LOGIN_ATTEMPTS: dict[uuid.UUID, _LiveLoginAttempt] = {}


@dataclass(slots=True)
class AdminTelegramLoginService:
    """Authenticate DB-backed Telegram crawler sessions without manual StringSession import."""

    session: AsyncSession


    @property
    def _admin_service(self) -> AdminService:
        return AdminService(session=self.session)

    async def start_phone_login(
        self,
        telegram_session_id: uuid.UUID,
        request: AdminTelegramLoginPhoneStartRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginPhoneStartRead:
        row = await self._get_session_for_login(telegram_session_id)
        now = utcnow()
        attempt = TelegramSessionLoginAttempt(
            telegram_session_id=row.id,
            method="phone",
            status="pending",
            phone_number_hint=_phone_hint(request.phone_number),
            expires_at=now + LOGIN_ATTEMPT_TTL,
        )
        self.session.add(attempt)
        await self.session.flush()

        client = self._build_telegram_client()
        try:
            await _maybe_await(client.connect())
            sent_code = await _maybe_await(client.send_code_request(request.phone_number))
            phone_code_hash = getattr(sent_code, "phone_code_hash", None)
            attempt.phone_code_hash = phone_code_hash if isinstance(phone_code_hash, str) else None
            attempt.encrypted_temp_string_session = await self._encrypt_client_session_if_present(client)
            _LIVE_LOGIN_ATTEMPTS[attempt.id] = _LiveLoginAttempt(client=client, phone_number=request.phone_number)
            await self.session.commit()
        except Exception as exc:
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram phone login failed: {type(exc).__name__}.") from exc

        return AdminTelegramLoginPhoneStartRead(
            attempt_id=attempt.id,
            phone_number_hint=attempt.phone_number_hint,
            expires_at=attempt.expires_at,
            message="Telegram login code sent. Enter the code from Telegram to finish login.",
        )

    async def complete_phone_code_login(
        self,
        telegram_session_id: uuid.UUID,
        request: AdminTelegramLoginPhoneCodeRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginCompleteRead:
        row, attempt = await self._get_valid_attempt(
            telegram_session_id,
            request.attempt_id,
            method="phone",
            allowed_statuses={"pending"},
        )
        live_attempt = await self._live_or_restored_attempt(attempt)
        sign_in_kwargs: dict[str, Any] = {"code": request.code}
        if live_attempt.phone_number is not None:
            sign_in_kwargs["phone"] = live_attempt.phone_number
        if attempt.phone_code_hash is not None:
            sign_in_kwargs["phone_code_hash"] = attempt.phone_code_hash

        try:
            await _maybe_await(live_attempt.client.sign_in(**sign_in_kwargs))
        except Exception as exc:
            if type(exc).__name__ == _PASSWORD_REQUIRED_ERROR_CLASS:
                attempt.status = "password_required"
                attempt.encrypted_temp_string_session = await self._encrypt_required_client_session(live_attempt.client)
                _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
                await self.session.commit()
                await self.session.refresh(row)
                counts_by_session = await self._admin_service._count_source_channels_by_session()
                return AdminTelegramLoginCompleteRead(
                    telegram_session=self._admin_service._telegram_session_read(
                        row,
                        owned_channel_count=counts_by_session.get(row.id, 0),
                    ),
                    password_required=True,
                    message="Telegram requires the account 2FA password to finish login.",
                )
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(live_attempt.client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram code login failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            row,
            attempt,
            live_attempt.client,
            admin_user_id=admin_user_id,
            note=request.note,
        )

    async def complete_password_login(
        self,
        telegram_session_id: uuid.UUID,
        request: AdminTelegramLoginPasswordRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginCompleteRead:
        row, attempt = await self._get_valid_attempt(
            telegram_session_id,
            request.attempt_id,
            method="phone",
            allowed_statuses={"password_required"},
        )
        live_attempt = await self._live_or_restored_attempt(attempt)
        try:
            await _maybe_await(live_attempt.client.sign_in(password=request.password.get_secret_value()))
        except Exception as exc:
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(live_attempt.client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram password login failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            row,
            attempt,
            live_attempt.client,
            admin_user_id=admin_user_id,
            note=request.note,
        )

    async def start_qr_login(
        self,
        telegram_session_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginQrStartRead:
        row = await self._get_session_for_login(telegram_session_id)
        now = utcnow()
        attempt = TelegramSessionLoginAttempt(
            telegram_session_id=row.id,
            method="qr",
            status="pending",
            expires_at=now + LOGIN_ATTEMPT_TTL,
        )
        self.session.add(attempt)
        await self.session.flush()

        client = self._build_telegram_client()
        try:
            await _maybe_await(client.connect())
            qr_login = await _maybe_await(client.qr_login())
            qr_url = getattr(qr_login, "url", None)
            if not isinstance(qr_url, str) or not qr_url.strip():
                raise AdminConflictError("Telegram did not return a QR login URL.")
            qr_url = qr_url.strip()
            attempt.qr_url = qr_url
            attempt.encrypted_temp_string_session = await self._encrypt_client_session_if_present(client)
            _LIVE_LOGIN_ATTEMPTS[attempt.id] = _LiveLoginAttempt(client=client, qr_login=qr_login)
            await self.session.commit()
        except AdminConflictError:
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(client)
            raise
        except Exception as exc:
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram QR login failed: {type(exc).__name__}.") from exc

        return AdminTelegramLoginQrStartRead(
            attempt_id=attempt.id,
            qr_url=qr_url,
            expires_at=attempt.expires_at,
            message="Telegram QR login started. Scan this URL with Telegram, then complete the attempt.",
        )

    async def complete_qr_login(
        self,
        telegram_session_id: uuid.UUID,
        request: AdminTelegramLoginQrCompleteRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginCompleteRead:
        row, attempt = await self._get_valid_attempt(
            telegram_session_id,
            request.attempt_id,
            method="qr",
            allowed_statuses={"pending"},
        )
        live_attempt = _LIVE_LOGIN_ATTEMPTS.get(attempt.id)
        if live_attempt is None or live_attempt.qr_login is None:
            if live_attempt is not None:
                _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
                await self._disconnect_client(live_attempt.client)
            attempt.status = "failed"
            attempt.error_class = "TelegramQrLoginStateLost"
            attempt.error_text = "QR login state was lost; start a new QR login attempt."
            attempt.completed_at = utcnow()
            await self.session.commit()
            raise AdminConflictError("QR login state was lost; start a new QR login attempt.")

        try:
            await _maybe_await(live_attempt.qr_login.wait(timeout=5))
        except Exception as exc:
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(live_attempt.client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram QR login completion failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            row,
            attempt,
            live_attempt.client,
            admin_user_id=admin_user_id,
            note=request.note,
        )

    async def _get_session_for_login(self, telegram_session_id: uuid.UUID) -> TelegramSession:
        row = await self.session.scalar(
            select(TelegramSession).where(TelegramSession.id == telegram_session_id).with_for_update(),
        )
        if row is None:
            raise AdminNotFoundError(f"Telegram session {telegram_session_id} does not exist.")
        return row

    async def _get_valid_attempt(
        self,
        telegram_session_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        method: str,
        allowed_statuses: set[str],
    ) -> tuple[TelegramSession, TelegramSessionLoginAttempt]:
        row = await self._get_session_for_login(telegram_session_id)
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None or attempt.telegram_session_id != row.id or attempt.method != method:
            raise AdminConflictError("Login attempt is invalid for this Telegram session.")
        if attempt.status not in allowed_statuses:
            raise AdminConflictError(f"Login attempt is already {attempt.status}.")
        now = utcnow()
        if attempt.expires_at <= now:
            attempt.status = "expired"
            attempt.error_class = "TelegramLoginAttemptExpired"
            attempt.error_text = "Telegram login attempt expired."
            attempt.completed_at = now
            live_attempt = _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            if live_attempt is not None:
                await self._disconnect_client(live_attempt.client)
            await self.session.commit()
            raise AdminConflictError("Telegram login attempt expired; start a new login attempt.")
        return row, attempt

    async def _live_or_restored_attempt(self, attempt: TelegramSessionLoginAttempt) -> _LiveLoginAttempt:
        live_attempt = _LIVE_LOGIN_ATTEMPTS.get(attempt.id)
        if live_attempt is not None:
            return live_attempt
        if attempt.encrypted_temp_string_session is None:
            raise AdminConflictError("Login attempt state was lost; start a new login attempt.")
        try:
            temp_string_session = self._admin_service._decrypt_string_session(attempt.encrypted_temp_string_session)
        except TelegramStringSessionDecryptError as exc:
            raise AdminConflictError("Login attempt state could not be restored; start a new login attempt.") from exc
        client = self._build_telegram_client(temp_string_session)
        await _maybe_await(client.connect())
        live_attempt = _LiveLoginAttempt(client=client)
        _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
        return live_attempt

    async def _finalize_authorized_client(
        self,
        row: TelegramSession,
        attempt: TelegramSessionLoginAttempt,
        client: TelegramLoginClient,
        *,
        admin_user_id: uuid.UUID,
        note: str | None,
    ) -> AdminTelegramLoginCompleteRead:
        try:
            previous_values = self._admin_service._telegram_session_snapshot(row)
            me = await _maybe_await(client.get_me())
            account = _account_projection_from_me(me)
            row.encrypted_string_session = await self._encrypt_required_client_session(client)
            row.account_user_id = account.user_id
            row.account_username = account.username
            row.account_phone_hint = account.phone_hint
            row.status = TelegramSessionStatus.ACTIVE
            row.last_error_class = None
            row.last_error_text = None
            row.flood_wait_until = None
            row.quarantined_at = None
            row.last_heartbeat_at = utcnow()
            attempt.status = "completed"
            attempt.encrypted_temp_string_session = None
            attempt.completed_at = utcnow()
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            self._admin_service._add_telegram_admin_audit(
                admin_user_id=admin_user_id,
                action="session_login",
                telegram_session_id=row.id,
                source_channel_id=None,
                previous_values=previous_values,
                new_values=self._admin_service._telegram_session_snapshot(row),
                note=note,
            )
            await self.session.commit()
        except Exception as exc:
            _LIVE_LOGIN_ATTEMPTS.pop(attempt.id, None)
            await self._disconnect_client(client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram login finalization failed: {type(exc).__name__}.") from exc
        await self._disconnect_client(client)
        await self.session.refresh(row)
        counts_by_session = await self._admin_service._count_source_channels_by_session()
        return AdminTelegramLoginCompleteRead(
            telegram_session=self._admin_service._telegram_session_read(
                row,
                owned_channel_count=counts_by_session.get(row.id, 0),
            ),
            password_required=False,
            message="Telegram session logged in and stored securely.",
        )

    def _build_telegram_client(self, string_session: SecretStr | None = None) -> TelegramLoginClient:
        settings = get_settings()
        api_id = settings.telegram_api_id
        api_hash = settings.telegram_api_hash
        if api_id is None or api_hash is None:
            raise AdminConflictError(
                "Telegram API credentials are not configured; set TELEGRAM_API_ID and TELEGRAM_API_HASH.",
            )

        from telethon import TelegramClient  # noqa: PLC0415
        from telethon.sessions import StringSession  # noqa: PLC0415

        session_text = "" if string_session is None else string_session.get_secret_value()
        return TelegramClient(StringSession(session_text), api_id, api_hash.get_secret_value())

    async def _encrypt_client_session_if_present(self, client: TelegramLoginClient) -> str | None:
        raw_string_session = await _client_string_session(client)
        if not raw_string_session:
            return None
        return self._admin_service._encrypt_string_session(SecretStr(raw_string_session))

    async def _encrypt_required_client_session(self, client: TelegramLoginClient) -> str:
        raw_string_session = await _client_string_session(client)
        if not raw_string_session:
            raise AdminConflictError("Telegram did not return StringSession material.")
        return self._admin_service._encrypt_string_session(SecretStr(raw_string_session))

    async def _mark_attempt_and_session_failed(
        self,
        row: TelegramSession,
        attempt: TelegramSessionLoginAttempt,
        exc: Exception,
    ) -> None:
        error_class = type(exc).__name__[:128]
        error_text = f"Telegram login failed with {error_class}."[:MAX_TELEGRAM_ERROR_TEXT_LENGTH]
        attempt.status = "failed"
        attempt.error_class = error_class
        attempt.error_text = error_text
        attempt.completed_at = utcnow()
        if row.encrypted_string_session is None:
            row.status = TelegramSessionStatus.AUTH_REQUIRED
        row.last_error_class = error_class
        row.last_error_text = error_text
        row.flood_wait_until = None
        row.quarantined_at = None
        await self.session.commit()

    @staticmethod
    async def _disconnect_client(client: TelegramLoginClient) -> None:
        disconnect = getattr(client, "disconnect", None)
        if disconnect is not None:
            await _maybe_await(disconnect())


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _client_string_session(client: TelegramLoginClient) -> str:
    save = getattr(getattr(client, "session", None), "save", None)
    if save is None:
        raise AdminConflictError("Telegram client did not expose StringSession material.")
    raw_string_session = await _maybe_await(save())
    return raw_string_session.strip() if isinstance(raw_string_session, str) else ""


def _account_projection_from_me(me: object) -> AdminTelegramAccountProjection:
    user_id = getattr(me, "id", None)
    username = getattr(me, "username", None)
    phone = getattr(me, "phone", None)
    return AdminTelegramAccountProjection(
        user_id=user_id if isinstance(user_id, int) else None,
        username=username.strip() if isinstance(username, str) and username.strip() else None,
        phone_hint=_phone_hint(phone if isinstance(phone, str) else None),
    )


__all__ = [
    "AdminTelegramLoginService",
    "LOGIN_ATTEMPT_TTL",
    "TelegramLoginClient",
]
