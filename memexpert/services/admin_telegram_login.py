"""Browser-admin Telegram login orchestration using QR or phone-code auth."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import SecretStr
from sqlalchemy import select

from memexpert.core.config import get_settings
from memexpert.crawlers.telegram.session_crypto import TelegramStringSessionDecryptError
from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSession, TelegramSessionLoginAttempt
from memexpert.models.enums import TelegramSessionStatus
from memexpert.schemas.admin import (
    MAX_SOURCE_TITLE_LENGTH,
    AdminTelegramLoginCompleteRead,
    AdminTelegramLoginPasswordRequest,
    AdminTelegramLoginPhoneCodeRequest,
    AdminTelegramLoginPhoneStartRead,
    AdminTelegramLoginPhoneStartRequest,
    AdminTelegramLoginQrCompleteRequest,
    AdminTelegramLoginQrStartRead,
    AdminTelegramLoginQrStatusRead,
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
QR_LOGIN_POLL_TIMEOUT_SECONDS = 30.0
_PASSWORD_REQUIRED_ERROR_CLASS = "SessionPasswordNeededError"
_RETRYABLE_PHONE_CODE_ERROR_CLASSES = frozenset({"PhoneCodeEmptyError", "PhoneCodeInvalidError"})
_RETRYABLE_PASSWORD_ERROR_CLASS = "PasswordHashInvalidError"
_DERIVED_SESSION_NAME_PREFIX = "telegram_"


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
    qr_wait_task: asyncio.Task[Any] | None = None
    qr_expiry_cleanup_handle: asyncio.TimerHandle | None = None
    qr_completion_expires_at: datetime | None = None
    phone_number: str | None = None
    retirement_task: asyncio.Task[None] | None = None


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
            self._take_live_attempt(attempt.id)
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
            if type(exc).__name__ in _RETRYABLE_PHONE_CODE_ERROR_CLASSES:
                _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
                raise AdminConflictError("The Telegram code was incorrect. Try again.") from None
            self._take_live_attempt(attempt.id)
            await self._disconnect_client(live_attempt.client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram code login failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            row,
            attempt,
            live_attempt.client,
            expected_live_attempt=live_attempt,
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
            allowed_methods={"phone", "qr"},
            allowed_statuses={"password_required"},
        )
        live_attempt = await self._live_or_restored_attempt(attempt)
        try:
            await _maybe_await(live_attempt.client.sign_in(password=request.password.get_secret_value()))
        except Exception as exc:
            if type(exc).__name__ == _RETRYABLE_PASSWORD_ERROR_CLASS:
                _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
                raise AdminConflictError("The Telegram password was incorrect. Try again.") from None
            popped_attempt = self._take_live_attempt(attempt.id)
            if popped_attempt is not None:
                self._cancel_qr_wait_task(popped_attempt)
            await self._disconnect_client(live_attempt.client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram password login failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            row,
            attempt,
            live_attempt.client,
            expected_live_attempt=live_attempt,
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
            attempt.expires_at = _qr_login_expiry(qr_login, now)
            attempt.encrypted_temp_string_session = await self._encrypt_client_session_if_present(client)
            qr_wait_timeout = max(0.0, (attempt.expires_at - now).total_seconds())
            qr_wait_task = asyncio.create_task(_maybe_await(qr_login.wait(timeout=qr_wait_timeout)))
            live_attempt = _LiveLoginAttempt(
                client=client,
                qr_login=qr_login,
                qr_wait_task=qr_wait_task,
            )
            _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
            live_attempt.qr_expiry_cleanup_handle = self._schedule_qr_expiry_cleanup(
                attempt.id,
                live_attempt,
                expires_at=attempt.expires_at,
            )
            qr_wait_task.add_done_callback(
                lambda task, attempt_id=attempt.id, expected_attempt=live_attempt: self._qr_wait_finished(
                    attempt_id,
                    expected_attempt,
                    task,
                ),
            )
            await self._expire_other_pending_qr_attempts(row.id, attempt.id, now=now)
            await self.session.commit()
        except AdminConflictError:
            live_attempt = self._take_live_attempt(attempt.id)
            if live_attempt is not None:
                self._cancel_qr_wait_task(live_attempt)
            await self._disconnect_client(client)
            raise
        except Exception as exc:
            live_attempt = self._take_live_attempt(attempt.id)
            if live_attempt is not None:
                self._cancel_qr_wait_task(live_attempt)
            await self._disconnect_client(client)
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            raise AdminConflictError(f"Telegram QR login failed: {type(exc).__name__}.") from exc

        return AdminTelegramLoginQrStartRead(
            attempt_id=attempt.id,
            qr_url=qr_url,
            expires_at=attempt.expires_at,
            message="Telegram QR login started. Scan this URL with Telegram; MemeExpert is waiting automatically.",
        )

    async def complete_qr_login(
        self,
        telegram_session_id: uuid.UUID,
        request: AdminTelegramLoginQrCompleteRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginQrStatusRead:
        _row, attempt, live_attempt = await self._get_qr_attempt_for_poll(
            telegram_session_id,
            request.attempt_id,
        )
        if live_attempt is None or live_attempt.qr_login is None or live_attempt.qr_wait_task is None:
            if live_attempt is not None:
                self._take_live_attempt(attempt.id)
                self._cancel_qr_wait_task(live_attempt)
                await self._disconnect_client(live_attempt.client)
            attempt.status = "failed"
            attempt.error_class = "TelegramQrLoginStateLost"
            attempt.error_text = "QR login state was lost; start a new QR login attempt."
            attempt.completed_at = utcnow()
            await self.session.commit()
            raise AdminConflictError("QR login state was lost; start a new QR login attempt.")

        # The Telegram wait can last up to the long-poll timeout. Release row locks
        # before awaiting it so a concurrent QR refresh can supersede this attempt.
        await self.session.rollback()

        try:
            await asyncio.wait_for(
                asyncio.shield(live_attempt.qr_wait_task),
                timeout=QR_LOGIN_POLL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return AdminTelegramLoginQrStatusRead(
                status="pending",
                telegram_session=None,
                password_required=False,
                message="Still waiting for Telegram QR scan.",
            )
        except asyncio.CancelledError:
            if _LIVE_LOGIN_ATTEMPTS.get(request.attempt_id) is not live_attempt:
                raise AdminConflictError(
                    "QR login attempt expired or was replaced. Start a new QR login.",
                ) from None
            raise
        except Exception as exc:
            password_required = type(exc).__name__ == _PASSWORD_REQUIRED_ERROR_CLASS
            if not password_required:
                row, locked_attempt = await self._relock_qr_attempt_after_wait(
                    telegram_session_id,
                    request.attempt_id,
                    live_attempt,
                )
                self._take_live_attempt(locked_attempt.id, expected=live_attempt)
                self._cancel_qr_wait_task(live_attempt)
                await self._disconnect_client(live_attempt.client)
                await self._mark_attempt_and_session_failed(row, locked_attempt, exc)
                raise AdminConflictError(f"Telegram QR login completion failed: {type(exc).__name__}.") from exc
        else:
            password_required = False

        completion_expires_at = self._promote_qr_completion_cleanup(request.attempt_id, live_attempt)
        if completion_expires_at is None:
            raise AdminConflictError("QR login attempt expired or was replaced. Start a new QR login.")
        row, locked_attempt = await self._relock_qr_attempt_after_wait(
            telegram_session_id,
            request.attempt_id,
            live_attempt,
        )
        locked_attempt.expires_at = completion_expires_at

        if password_required:
            locked_attempt.status = "password_required"
            locked_attempt.encrypted_temp_string_session = await self._encrypt_required_client_session(
                live_attempt.client,
            )
            await self.session.commit()
            await self.session.refresh(row)
            counts_by_session = await self._admin_service._count_source_channels_by_session()
            return AdminTelegramLoginQrStatusRead(
                status="password_required",
                telegram_session=self._admin_service._telegram_session_read(
                    row,
                    owned_channel_count=counts_by_session.get(row.id, 0),
                ),
                password_required=True,
                message="Telegram requires the account 2FA password to finish login.",
            )

        try:
            completed = await self._finalize_authorized_client(
                row,
                locked_attempt,
                live_attempt.client,
                expected_live_attempt=live_attempt,
                admin_user_id=admin_user_id,
                note=request.note,
            )
        except asyncio.CancelledError:
            # Keep the fresh completion cleanup active; it will release an
            # accepted client if request cancellation abandons finalization.
            raise
        return AdminTelegramLoginQrStatusRead(
            status="completed",
            telegram_session=completed.telegram_session,
            password_required=False,
            message=completed.message,
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
        method: str | None = None,
        allowed_methods: set[str] | None = None,
        allowed_statuses: set[str],
    ) -> tuple[TelegramSession, TelegramSessionLoginAttempt]:
        row = await self._get_session_for_login(telegram_session_id)
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None or attempt.telegram_session_id != row.id:
            raise AdminConflictError("Login attempt is invalid for this Telegram session.")
        if method is not None and attempt.method != method:
            raise AdminConflictError("Login attempt is invalid for this Telegram session.")
        if allowed_methods is not None and attempt.method not in allowed_methods:
            raise AdminConflictError("Login attempt is invalid for this Telegram session.")
        if attempt.status not in allowed_statuses:
            raise AdminConflictError(f"Login attempt is already {attempt.status}.")
        now = utcnow()
        if attempt.expires_at <= now:
            attempt.status = "expired"
            attempt.error_class = "TelegramLoginAttemptExpired"
            attempt.error_text = "Telegram login attempt expired."
            attempt.completed_at = now
            live_attempt = self._take_live_attempt(attempt.id)
            if live_attempt is not None:
                self._cancel_qr_wait_task(live_attempt)
                await self._disconnect_client(live_attempt.client)
            await self.session.commit()
            raise AdminConflictError("Telegram login attempt expired; start a new login attempt.")
        return row, attempt

    async def _get_qr_attempt_for_poll(
        self,
        telegram_session_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> tuple[TelegramSession, TelegramSessionLoginAttempt, _LiveLoginAttempt | None]:
        row = await self._get_session_for_login(telegram_session_id)
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None or attempt.telegram_session_id != row.id or attempt.method != "qr":
            raise AdminConflictError("Login attempt is invalid for this Telegram session.")
        if attempt.status != "pending":
            raise AdminConflictError(f"Login attempt is already {attempt.status}.")

        live_attempt = _LIVE_LOGIN_ATTEMPTS.get(attempt_id)
        now = utcnow()
        if (
            live_attempt is not None
            and live_attempt.qr_completion_expires_at is not None
            and live_attempt.qr_completion_expires_at > now
        ):
            attempt.expires_at = live_attempt.qr_completion_expires_at
        elif attempt.expires_at <= now:
            attempt.status = "expired"
            attempt.error_class = "TelegramLoginAttemptExpired"
            attempt.error_text = "Telegram login attempt expired."
            attempt.completed_at = now
            removed_attempt = self._take_live_attempt(attempt.id, expected=live_attempt)
            if removed_attempt is not None:
                self._cancel_qr_wait_task(removed_attempt)
                await self._disconnect_client(removed_attempt.client)
            await self.session.commit()
            raise AdminConflictError("Telegram login attempt expired; start a new login attempt.")
        return row, attempt, live_attempt

    async def _relock_qr_attempt_after_wait(
        self,
        telegram_session_id: uuid.UUID,
        attempt_id: uuid.UUID,
        expected_live_attempt: _LiveLoginAttempt,
    ) -> tuple[TelegramSession, TelegramSessionLoginAttempt]:
        row = await self.session.scalar(
            select(TelegramSession).where(TelegramSession.id == telegram_session_id).with_for_update(),
        )
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if (
            row is None
            or attempt is None
            or attempt.telegram_session_id != telegram_session_id
            or attempt.method != "qr"
            or attempt.status != "pending"
            or _LIVE_LOGIN_ATTEMPTS.get(attempt_id) is not expected_live_attempt
        ):
            await self.session.rollback()
            raise AdminConflictError("QR login attempt expired or was replaced. Start a new QR login.")
        return row, attempt

    async def _expire_other_pending_qr_attempts(
        self,
        telegram_session_id: uuid.UUID,
        active_attempt_id: uuid.UUID,
        *,
        now: datetime,
    ) -> None:
        stale_attempts = (
            await self.session.execute(
                select(TelegramSessionLoginAttempt)
                .where(
                    TelegramSessionLoginAttempt.telegram_session_id == telegram_session_id,
                    TelegramSessionLoginAttempt.method == "qr",
                    TelegramSessionLoginAttempt.status == "pending",
                    TelegramSessionLoginAttempt.id != active_attempt_id,
                )
                .with_for_update(),
            )
        ).scalars().all()
        for stale_attempt in stale_attempts:
            stale_attempt.status = "expired"
            stale_attempt.error_class = "TelegramQrLoginSuperseded"
            stale_attempt.error_text = "QR login attempt was replaced by a new QR code."
            stale_attempt.completed_at = now
            live_attempt = self._take_live_attempt(stale_attempt.id)
            if live_attempt is not None:
                self._cancel_qr_wait_task(live_attempt)
                await self._disconnect_client(live_attempt.client)

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
        expected_live_attempt: _LiveLoginAttempt,
        admin_user_id: uuid.UUID,
        note: str | None,
    ) -> AdminTelegramLoginCompleteRead:
        try:
            previous_values = self._admin_service._telegram_session_snapshot(row)
            me = await _maybe_await(client.get_me())
            account = _account_projection_from_me(me)
            derived_name = _session_name_from_account(account)
            await self._ensure_session_name_available(row, derived_name)
            row.encrypted_string_session = await self._encrypt_required_client_session(client)
            row.name = derived_name
            row.display_name = _display_name_from_me(me, account)
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
        except AdminConflictError as exc:
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            await self._retire_live_attempt(attempt.id, expected_live_attempt)
            raise
        except Exception as exc:
            await self._mark_attempt_and_session_failed(row, attempt, exc)
            await self._retire_live_attempt(attempt.id, expected_live_attempt)
            raise AdminConflictError(f"Telegram login finalization failed: {type(exc).__name__}.") from exc
        await self._retire_live_attempt(attempt.id, expected_live_attempt)
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

    async def _ensure_session_name_available(self, row: TelegramSession, derived_name: str) -> None:
        existing = await self.session.scalar(
            select(TelegramSession)
            .where(TelegramSession.name == derived_name, TelegramSession.id != row.id)
            .with_for_update(),
        )
        if existing is not None:
            raise AdminConflictError(
                f"Telegram account user id {derived_name.removeprefix(_DERIVED_SESSION_NAME_PREFIX)} "
                f"is already stored in session {existing.display_name!r}.",
            )

    @staticmethod
    def _cancel_qr_wait_task(live_attempt: _LiveLoginAttempt) -> None:
        task = live_attempt.qr_wait_task
        if task is None:
            return
        if task.done():
            if not task.cancelled():
                _ = task.exception()
            return
        task.cancel()

    @staticmethod
    def _cancel_qr_expiry_cleanup(live_attempt: _LiveLoginAttempt) -> None:
        handle = live_attempt.qr_expiry_cleanup_handle
        if handle is not None:
            handle.cancel()
            live_attempt.qr_expiry_cleanup_handle = None

    def _take_live_attempt(
        self,
        attempt_id: uuid.UUID,
        *,
        expected: _LiveLoginAttempt | None = None,
    ) -> _LiveLoginAttempt | None:
        live_attempt = _LIVE_LOGIN_ATTEMPTS.get(attempt_id)
        if live_attempt is None or (expected is not None and live_attempt is not expected):
            return None
        _LIVE_LOGIN_ATTEMPTS.pop(attempt_id, None)
        if live_attempt is not None:
            self._cancel_qr_expiry_cleanup(live_attempt)
        return live_attempt

    def _launch_live_attempt_retirement(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
    ) -> asyncio.Task[None] | None:
        if expected_attempt.retirement_task is not None:
            return expected_attempt.retirement_task
        if _LIVE_LOGIN_ATTEMPTS.get(attempt_id) is not expected_attempt:
            return None
        retirement_task = asyncio.create_task(self._retire_exact_live_attempt(attempt_id, expected_attempt))
        expected_attempt.retirement_task = retirement_task
        return retirement_task

    async def _retire_live_attempt(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
    ) -> None:
        retirement_task = self._launch_live_attempt_retirement(attempt_id, expected_attempt)
        if retirement_task is None:
            return
        try:
            await asyncio.shield(retirement_task)
        except asyncio.CancelledError:
            retirement_task.add_done_callback(_consume_background_task_exception)
            raise

    async def _retire_exact_live_attempt(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
    ) -> None:
        self._cancel_qr_wait_task(expected_attempt)
        try:
            await self._disconnect_client(expected_attempt.client)
        finally:
            self._take_live_attempt(attempt_id, expected=expected_attempt)
            self._cancel_qr_expiry_cleanup(expected_attempt)
            expected_attempt.retirement_task = None

    def _qr_wait_finished(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
        task: asyncio.Task[Any],
    ) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is None or type(exception).__name__ == _PASSWORD_REQUIRED_ERROR_CLASS:
            self._promote_qr_completion_cleanup(attempt_id, expected_attempt)

    def _promote_qr_completion_cleanup(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
    ) -> datetime | None:
        if _LIVE_LOGIN_ATTEMPTS.get(attempt_id) is not expected_attempt:
            return None
        if expected_attempt.qr_completion_expires_at is not None:
            return expected_attempt.qr_completion_expires_at
        self._cancel_qr_expiry_cleanup(expected_attempt)
        completion_expires_at = utcnow() + LOGIN_ATTEMPT_TTL
        expected_attempt.qr_completion_expires_at = completion_expires_at
        expected_attempt.qr_expiry_cleanup_handle = self._schedule_qr_expiry_cleanup(
            attempt_id,
            expected_attempt,
            expires_at=completion_expires_at,
        )
        return completion_expires_at

    def _schedule_qr_expiry_cleanup(
        self,
        attempt_id: uuid.UUID,
        live_attempt: _LiveLoginAttempt,
        *,
        expires_at: datetime,
    ) -> asyncio.TimerHandle:
        delay = max(0.0, (expires_at - utcnow()).total_seconds())
        return asyncio.get_running_loop().call_later(delay, self._cleanup_expired_qr_attempt, attempt_id, live_attempt)

    def _cleanup_expired_qr_attempt(self, attempt_id: uuid.UUID, expected_attempt: _LiveLoginAttempt) -> None:
        retirement_task = self._launch_live_attempt_retirement(attempt_id, expected_attempt)
        if retirement_task is None:
            return
        retirement_task.add_done_callback(_consume_background_task_exception)

    @staticmethod
    async def _disconnect_client(client: TelegramLoginClient) -> None:
        disconnect = getattr(client, "disconnect", None)
        if disconnect is not None:
            await _maybe_await(disconnect())


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _consume_background_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    _ = task.exception()


async def _client_string_session(client: TelegramLoginClient) -> str:
    save = getattr(getattr(client, "session", None), "save", None)
    if save is None:
        raise AdminConflictError("Telegram client did not expose StringSession material.")
    raw_string_session = await _maybe_await(save())
    return raw_string_session.strip() if isinstance(raw_string_session, str) else ""


def _qr_login_expiry(qr_login: object, now: datetime) -> datetime:
    """Use Telegram's QR-token deadline when it is sooner than our bounded attempt TTL."""
    fallback = now + LOGIN_ATTEMPT_TTL
    expires = getattr(qr_login, "expires", None)
    if not isinstance(expires, datetime):
        return fallback
    normalized = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
    return min(fallback, normalized)


def _account_projection_from_me(me: object) -> AdminTelegramAccountProjection:
    user_id = getattr(me, "id", None)
    username = getattr(me, "username", None)
    phone = getattr(me, "phone", None)
    return AdminTelegramAccountProjection(
        user_id=user_id if isinstance(user_id, int) else None,
        username=username.strip() if isinstance(username, str) and username.strip() else None,
        phone_hint=_phone_hint(phone if isinstance(phone, str) else None),
    )


def _session_name_from_account(account: AdminTelegramAccountProjection) -> str:
    if account.user_id is None:
        raise AdminConflictError("Telegram did not return a user id for session naming.")
    return f"{_DERIVED_SESSION_NAME_PREFIX}{account.user_id}"


def _display_name_from_me(me: object, account: AdminTelegramAccountProjection) -> str:
    first_name = _clean_telegram_profile_text(getattr(me, "first_name", None))
    last_name = _clean_telegram_profile_text(getattr(me, "last_name", None))
    full_name = " ".join(part for part in (first_name, last_name) if part)
    if full_name:
        return full_name[:MAX_SOURCE_TITLE_LENGTH]
    if account.username:
        return f"@{account.username}"[:MAX_SOURCE_TITLE_LENGTH]
    if account.user_id is not None:
        return f"Telegram user {account.user_id}"[:MAX_SOURCE_TITLE_LENGTH]
    return "Telegram account"


def _clean_telegram_profile_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "AdminTelegramLoginService",
    "LOGIN_ATTEMPT_TTL",
    "TelegramLoginClient",
]
