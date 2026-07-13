"""Two-phase browser-admin Telegram login orchestration.

Login attempts are provisional resources.  A durable ``TelegramSession`` is
created (or an existing account credential is rotated) only after Telegram has
authorized the temporary Telethon client and the database transaction commits.
Abandoned authorized credentials are explicitly logged out instead of merely
being disconnected.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import SecretStr
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from memexpert.core.config import get_settings
from memexpert.crawlers.telegram.session_crypto import TelegramStringSessionDecryptError
from memexpert.models.base import utcnow
from memexpert.models.content import TelegramSession, TelegramSessionLoginAttempt
from memexpert.models.enums import TelegramSessionStatus
from memexpert.schemas.admin import (
    MAX_SOURCE_TITLE_LENGTH,
    AdminTelegramLoginCancelRead,
    AdminTelegramLoginCompleteRead,
    AdminTelegramLoginPasswordRequest,
    AdminTelegramLoginPhoneCodeRequest,
    AdminTelegramLoginPhoneStartRead,
    AdminTelegramLoginPhoneStartRequest,
    AdminTelegramLoginQrCompleteRequest,
    AdminTelegramLoginQrStartRead,
    AdminTelegramLoginQrStartRequest,
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
    from collections.abc import Callable, Coroutine

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory


logger = logging.getLogger(__name__)

LOGIN_ATTEMPT_TTL = timedelta(minutes=10)
QR_LOGIN_POLL_TIMEOUT_SECONDS = 30.0
_PASSWORD_REQUIRED_ERROR_CLASS = "SessionPasswordNeededError"
_RETRYABLE_PHONE_CODE_ERROR_CLASSES = frozenset({"PhoneCodeEmptyError", "PhoneCodeInvalidError"})
_RETRYABLE_PASSWORD_ERROR_CLASS = "PasswordHashInvalidError"
_DERIVED_SESSION_NAME_PREFIX = "telegram_"
_ACTIVE_ATTEMPT_STATUSES = frozenset({"pending", "password_required"})
_TERMINAL_ATTEMPT_STATUSES = frozenset({"completed", "failed", "expired", "cancelled"})


class TelegramLoginClient(Protocol):
    """Small Telethon client surface used by login orchestration and tests."""

    session: Any

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool: ...

    async def log_out(self) -> bool: ...

    async def send_code_request(self, phone: str) -> Any: ...

    async def sign_in(self, *args: Any, **kwargs: Any) -> Any: ...

    async def get_me(self) -> Any: ...

    async def qr_login(self) -> Any: ...


@dataclass(slots=True)
class _LiveLoginAttempt:
    client: TelegramLoginClient
    qr_login: Any | None = None
    qr_wait_task: asyncio.Task[Any] | None = None
    qr_expiry_cleanup_handle: asyncio.TimerHandle | None = None
    qr_expiry_cleanup_generation: int = 0
    qr_completion_expires_at: datetime | None = None
    qr_completion_persist_task: asyncio.Task[None] | None = None
    phone_number: str | None = None
    retirement_task: asyncio.Task[None] | None = None
    credential_promoted: bool = False


_LIVE_LOGIN_ATTEMPTS: dict[uuid.UUID, _LiveLoginAttempt] = {}
_BACKGROUND_LOGIN_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True, slots=True)
class TelegramLoginCleanupBatchResult:
    scanned: int
    expired: int
    cleaned: int
    failed: int


@dataclass(slots=True)
class AdminTelegramLoginService:
    """Authenticate crawler accounts without creating durable shells first."""

    session: AsyncSession

    @property
    def _admin_service(self) -> AdminService:
        return AdminService(session=self.session)

    async def start_phone_login(
        self,
        request: AdminTelegramLoginPhoneStartRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginPhoneStartRead:
        await self._require_optional_target(request.telegram_session_id)
        now = utcnow()
        attempt = TelegramSessionLoginAttempt(
            telegram_session_id=request.telegram_session_id,
            created_by_admin_user_id=admin_user_id,
            method="phone",
            status="pending",
            cleanup_status="pending",
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
            live_attempt = _LiveLoginAttempt(client=client, phone_number=request.phone_number)
            _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
            live_attempt.qr_expiry_cleanup_handle = self._schedule_attempt_expiry(
                attempt.id,
                live_attempt,
                expires_at=attempt.expires_at,
            )
            await self.session.commit()
        except Exception as exc:
            await self.session.rollback()
            live_attempt = self._take_live_attempt(attempt.id)
            if live_attempt is not None:
                client = live_attempt.client
            await self._persist_start_failure(attempt.id, exc, client=client)
            raise AdminConflictError(f"Telegram phone login failed: {type(exc).__name__}.") from exc

        return AdminTelegramLoginPhoneStartRead(
            attempt_id=attempt.id,
            phone_number_hint=attempt.phone_number_hint,
            expires_at=attempt.expires_at,
            message="Telegram login code sent. Enter the code from Telegram to finish login.",
        )

    async def complete_phone_code_login(
        self,
        attempt_id: uuid.UUID,
        request: AdminTelegramLoginPhoneCodeRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginCompleteRead:
        attempt = await self._get_valid_attempt(
            attempt_id,
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
                await self._mark_password_required(attempt, live_attempt)
                return await self._password_required_read(attempt)
            if type(exc).__name__ in _RETRYABLE_PHONE_CODE_ERROR_CLASSES:
                _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
                raise AdminConflictError("The Telegram code was incorrect. Try again.") from None
            await self._fail_and_cleanup(attempt.id, exc, expected_live_attempt=live_attempt)
            raise AdminConflictError(f"Telegram code login failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            attempt,
            live_attempt.client,
            expected_live_attempt=live_attempt,
            admin_user_id=admin_user_id,
            note=request.note,
        )

    async def complete_password_login(
        self,
        attempt_id: uuid.UUID,
        request: AdminTelegramLoginPasswordRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginCompleteRead:
        attempt = await self._get_valid_attempt(
            attempt_id,
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
            await self._fail_and_cleanup(attempt.id, exc, expected_live_attempt=live_attempt)
            raise AdminConflictError(f"Telegram password login failed: {type(exc).__name__}.") from exc

        return await self._finalize_authorized_client(
            attempt,
            live_attempt.client,
            expected_live_attempt=live_attempt,
            admin_user_id=admin_user_id,
            note=request.note,
        )

    async def start_qr_login(
        self,
        request: AdminTelegramLoginQrStartRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginQrStartRead:
        await self._require_optional_target(request.telegram_session_id)
        now = utcnow()
        attempt = TelegramSessionLoginAttempt(
            telegram_session_id=request.telegram_session_id,
            created_by_admin_user_id=admin_user_id,
            method="qr",
            status="pending",
            cleanup_status="pending",
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
            qr_token_expires_at = _qr_login_expiry(qr_login, now)
            attempt.encrypted_temp_string_session = await self._encrypt_client_session_if_present(client)
            qr_wait_timeout = max(0.0, (qr_token_expires_at - now).total_seconds())
            live_attempt = _LiveLoginAttempt(client=client, qr_login=qr_login)
            _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
            live_attempt.qr_expiry_cleanup_handle = self._schedule_attempt_expiry(
                attempt.id,
                live_attempt,
                expires_at=qr_token_expires_at,
            )
            await self.session.commit()
            qr_wait_task = asyncio.create_task(_maybe_await(qr_login.wait(timeout=qr_wait_timeout)))
            live_attempt.qr_wait_task = qr_wait_task
            qr_wait_task.add_done_callback(
                lambda task, attempt_id=attempt.id, expected_attempt=live_attempt: self._qr_wait_finished(
                    attempt_id,
                    expected_attempt,
                    task,
                ),
            )
        except Exception as exc:
            await self.session.rollback()
            live_attempt = self._take_live_attempt(attempt.id)
            if live_attempt is not None:
                client = live_attempt.client
            await self._persist_start_failure(attempt.id, exc, client=client)
            if isinstance(exc, AdminConflictError):
                raise
            raise AdminConflictError(f"Telegram QR login failed: {type(exc).__name__}.") from exc

        return AdminTelegramLoginQrStartRead(
            attempt_id=attempt.id,
            qr_url=qr_url,
            expires_at=qr_token_expires_at,
            message="Telegram QR login started. Scan it with Telegram; MemeExpert is waiting automatically.",
        )

    async def complete_qr_login(
        self,
        attempt_id: uuid.UUID,
        request: AdminTelegramLoginQrCompleteRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginQrStatusRead:
        attempt, live_attempt = await self._get_qr_attempt_for_poll(attempt_id)
        if live_attempt is None or live_attempt.qr_login is None or live_attempt.qr_wait_task is None:
            await self._fail_and_cleanup(
                attempt.id,
                AdminConflictError("QR login state was lost; start a new QR login attempt."),
            )
            raise AdminConflictError("QR login state was lost; start a new QR login attempt.")

        await self.session.rollback()
        try:
            await self._wait_for_qr_completion(live_attempt.qr_wait_task)
        except TimeoutError:
            return AdminTelegramLoginQrStatusRead(
                status="pending",
                telegram_session=None,
                password_required=False,
                message="Still waiting for Telegram QR scan.",
            )
        except asyncio.CancelledError:
            current = _LIVE_LOGIN_ATTEMPTS.get(attempt_id)
            if current is not live_attempt or live_attempt.retirement_task is not None:
                raise AdminConflictError("QR login attempt expired or was replaced. Start a new QR login.") from None
            raise
        except Exception as exc:
            if type(exc).__name__ != _PASSWORD_REQUIRED_ERROR_CLASS:
                await self._fail_and_cleanup(attempt.id, exc, expected_live_attempt=live_attempt)
                raise AdminConflictError(f"Telegram QR login completion failed: {type(exc).__name__}.") from exc
            password_required = True
        else:
            password_required = False

        persistence_task = self._ensure_qr_completion_deadline_persistence(attempt_id, live_attempt)
        if persistence_task is not None:
            try:
                await asyncio.shield(persistence_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._fail_and_cleanup(attempt.id, exc, expected_live_attempt=live_attempt)
                raise AdminConflictError(
                    "QR login completion state could not be persisted; start a new login.",
                ) from exc

        attempt = await self._relock_attempt(attempt_id, allowed_statuses={"pending"})
        if password_required:
            await self._mark_password_required(attempt, live_attempt)
            password_read = await self._password_required_read(attempt)
            return AdminTelegramLoginQrStatusRead(
                status="password_required",
                telegram_session=password_read.telegram_session,
                password_required=True,
                message=password_read.message,
            )

        completed = await self._finalize_authorized_client(
            attempt,
            live_attempt.client,
            expected_live_attempt=live_attempt,
            admin_user_id=admin_user_id,
            note=request.note,
        )
        return AdminTelegramLoginQrStatusRead(
            status="completed",
            telegram_session=completed.telegram_session,
            password_required=False,
            message=completed.message,
        )

    async def cancel_login_attempt(
        self,
        attempt_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramLoginCancelRead:
        _ = admin_user_id
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None:
            raise AdminNotFoundError(f"Telegram login attempt {attempt_id} does not exist.")
        if attempt.status == "completed" or attempt.cleanup_status == "promoted":
            raise AdminConflictError("A completed Telegram login attempt cannot be cancelled.")
        if attempt.status != "cancelled":
            attempt.status = "cancelled"
            attempt.error_class = "TelegramLoginAttemptCancelled"
            attempt.error_text = "Telegram login attempt was cancelled by an administrator."
            attempt.completed_at = utcnow()
            await self.session.commit()
        await self._cleanup_attempt_credentials(attempt_id)
        return AdminTelegramLoginCancelRead(
            attempt_id=attempt_id,
            status="cancelled",
            message="Telegram login attempt cancelled and temporary authorization discarded.",
        )

    async def expire_or_retry_cleanup(self, attempt_id: uuid.UUID, *, now: datetime) -> tuple[bool, bool]:
        """Expire one due attempt and retry its durable credential cleanup."""

        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None:
            return False, False
        expired = False
        if attempt.status in _ACTIVE_ATTEMPT_STATUSES and attempt.expires_at <= now:
            attempt.status = "expired"
            attempt.error_class = "TelegramLoginAttemptExpired"
            attempt.error_text = "Telegram login attempt expired."
            attempt.completed_at = now
            expired = True
            await self.session.commit()
        if attempt.status == "completed" or attempt.cleanup_status in {"promoted", "discarded"}:
            return expired, False
        cleaned = await self._cleanup_attempt_credentials(attempt_id)
        return expired, cleaned

    async def _require_optional_target(self, telegram_session_id: uuid.UUID | None) -> TelegramSession | None:
        if telegram_session_id is None:
            return None
        row = await self.session.get(TelegramSession, telegram_session_id)
        if row is None:
            raise AdminNotFoundError(f"Telegram session {telegram_session_id} does not exist.")
        return row

    async def _get_valid_attempt(
        self,
        attempt_id: uuid.UUID,
        *,
        method: str | None = None,
        allowed_methods: set[str] | None = None,
        allowed_statuses: set[str],
    ) -> TelegramSessionLoginAttempt:
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None:
            raise AdminNotFoundError(f"Telegram login attempt {attempt_id} does not exist.")
        if method is not None and attempt.method != method:
            raise AdminConflictError("Login attempt method does not match this operation.")
        if allowed_methods is not None and attempt.method not in allowed_methods:
            raise AdminConflictError("Login attempt method does not match this operation.")
        if attempt.status not in allowed_statuses:
            raise AdminConflictError(f"Login attempt is already {attempt.status}.")
        if attempt.expires_at <= utcnow():
            attempt.status = "expired"
            attempt.error_class = "TelegramLoginAttemptExpired"
            attempt.error_text = "Telegram login attempt expired."
            attempt.completed_at = utcnow()
            await self.session.commit()
            await self._cleanup_attempt_credentials(attempt.id)
            raise AdminConflictError("Telegram login attempt expired; start a new login attempt.")
        return attempt

    async def _get_qr_attempt_for_poll(
        self,
        attempt_id: uuid.UUID,
    ) -> tuple[TelegramSessionLoginAttempt, _LiveLoginAttempt | None]:
        attempt = await self._get_valid_attempt(attempt_id, method="qr", allowed_statuses={"pending"})
        live_attempt = _LIVE_LOGIN_ATTEMPTS.get(attempt_id)
        if (
            live_attempt is not None
            and live_attempt.qr_completion_expires_at is not None
            and live_attempt.qr_completion_expires_at > utcnow()
        ):
            attempt.expires_at = live_attempt.qr_completion_expires_at
        return attempt, live_attempt

    async def _relock_attempt(
        self,
        attempt_id: uuid.UUID,
        *,
        allowed_statuses: set[str],
    ) -> TelegramSessionLoginAttempt:
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None or attempt.status not in allowed_statuses:
            await self.session.rollback()
            raise AdminConflictError("Telegram login attempt expired or was replaced. Start a new login.")
        return attempt

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
        live_attempt.qr_expiry_cleanup_handle = self._schedule_attempt_expiry(
            attempt.id,
            live_attempt,
            expires_at=attempt.expires_at,
        )
        return live_attempt

    async def _mark_password_required(
        self,
        attempt: TelegramSessionLoginAttempt,
        live_attempt: _LiveLoginAttempt,
    ) -> None:
        attempt.status = "password_required"
        attempt.encrypted_temp_string_session = await self._encrypt_required_client_session(live_attempt.client)
        attempt.expires_at = utcnow() + LOGIN_ATTEMPT_TTL
        _LIVE_LOGIN_ATTEMPTS[attempt.id] = live_attempt
        self._cancel_attempt_expiry(live_attempt)
        live_attempt.qr_completion_expires_at = attempt.expires_at
        live_attempt.qr_expiry_cleanup_handle = self._schedule_attempt_expiry(
            attempt.id,
            live_attempt,
            expires_at=attempt.expires_at,
        )
        await self.session.commit()

    async def _password_required_read(
        self,
        attempt: TelegramSessionLoginAttempt,
    ) -> AdminTelegramLoginCompleteRead:
        row = None
        if attempt.telegram_session_id is not None:
            row = await self.session.get(TelegramSession, attempt.telegram_session_id)
        telegram_session = None
        if row is not None:
            counts_by_session = await self._admin_service._count_source_channels_by_session()
            telegram_session = self._admin_service._telegram_session_read(
                row,
                owned_channel_count=counts_by_session.get(row.id, 0),
            )
        return AdminTelegramLoginCompleteRead(
            telegram_session=telegram_session,
            password_required=True,
            message="Telegram requires the account 2FA password to finish login.",
        )

    async def _finalize_authorized_client(
        self,
        attempt: TelegramSessionLoginAttempt,
        client: TelegramLoginClient,
        *,
        expected_live_attempt: _LiveLoginAttempt,
        admin_user_id: uuid.UUID,
        note: str | None,
    ) -> AdminTelegramLoginCompleteRead:
        attempt_id = attempt.id
        old_encrypted_string_session: str | None = None
        new_raw_string_session: SecretStr | None = None
        try:
            me = await _maybe_await(client.get_me())
            account = _account_projection_from_me(me)
            derived_name = _session_name_from_account(account)
            if account.user_id is None:
                raise AdminConflictError("Telegram did not return a user id for the authorized account.")

            await self.session.execute(select(_telegram_account_login_lock(account.user_id)))
            target = None
            if attempt.telegram_session_id is not None:
                target = await self.session.scalar(
                    select(TelegramSession)
                    .where(TelegramSession.id == attempt.telegram_session_id)
                    .with_for_update(),
                )
                if target is None:
                    raise AdminNotFoundError("The Telegram account selected for reconnect no longer exists.")

            existing = await self.session.scalar(
                select(TelegramSession)
                .where(TelegramSession.account_user_id == account.user_id)
                .with_for_update(),
            )
            if target is not None and existing is not None and existing.id != target.id:
                raise AdminConflictError("This Telegram account is already connected to another crawler account.")
            if target is not None and target.account_user_id not in {None, account.user_id}:
                raise AdminConflictError(
                    "Reconnect must authorize the same Telegram account as the selected crawler account.",
                )

            row = target or existing
            created = row is None
            if row is None:
                row = TelegramSession(
                    name=derived_name,
                    display_name=_display_name_from_me(me, account),
                    encrypted_string_session=None,
                    account_user_id=account.user_id,
                    account_username=account.username,
                    account_phone_hint=account.phone_hint,
                    status=TelegramSessionStatus.AUTH_REQUIRED,
                    enabled=True,
                    live_enabled=True,
                    catchup_enabled=True,
                    engagement_enabled=True,
                    max_requests_per_second=1.0,
                )
                self.session.add(row)
                await self.session.flush()

            previous_values = {} if created else self._admin_service._telegram_session_snapshot(row)
            old_encrypted_string_session = row.encrypted_string_session
            new_raw_string_session = SecretStr(await _client_string_session(client))
            if not new_raw_string_session.get_secret_value():
                raise AdminConflictError("Telegram did not return StringSession material.")
            row.encrypted_string_session = self._admin_service._encrypt_string_session(new_raw_string_session)
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

            attempt.telegram_session_id = row.id
            attempt.status = "completed"
            attempt.cleanup_status = "promoted"
            attempt.cleanup_error_class = None
            attempt.cleanup_error_text = None
            attempt.cleanup_completed_at = utcnow()
            attempt.encrypted_temp_string_session = None
            attempt.phone_code_hash = None
            attempt.qr_url = None
            attempt.completed_at = utcnow()
            if created:
                self._admin_service._add_telegram_admin_audit(
                    admin_user_id=admin_user_id,
                    action="session_create",
                    telegram_session_id=row.id,
                    source_channel_id=None,
                    previous_values={},
                    new_values=self._admin_service._telegram_session_snapshot(row, owned_channel_count=0),
                    note=note,
                )
            self._admin_service._add_telegram_admin_audit(
                admin_user_id=admin_user_id,
                action="session_login",
                telegram_session_id=row.id,
                source_channel_id=None,
                previous_values=previous_values,
                new_values=self._admin_service._telegram_session_snapshot(row),
                note=note,
            )
            # Transfer ownership before entering the commit await. If request
            # cancellation makes the commit result ambiguous, local timer
            # cleanup must never revoke a credential that may already be the
            # durable crawler credential. The DB-backed sweeper will inspect
            # the committed attempt state and revoke it later if promotion did
            # not actually commit.
            expected_live_attempt.credential_promoted = True
            await self.session.commit()
        except (AdminConflictError, AdminNotFoundError) as exc:
            expected_live_attempt.credential_promoted = False
            await self.session.rollback()
            await self._fail_and_cleanup(attempt_id, exc, expected_live_attempt=expected_live_attempt)
            raise
        except Exception as exc:
            expected_live_attempt.credential_promoted = False
            await self.session.rollback()
            await self._fail_and_cleanup(attempt_id, exc, expected_live_attempt=expected_live_attempt)
            raise AdminConflictError(f"Telegram login finalization failed: {type(exc).__name__}.") from exc

        rotated_credential_revoke_task: asyncio.Task[None] | None = None
        if old_encrypted_string_session and new_raw_string_session is not None:
            rotated_credential_revoke_task = _launch_background_login_task(
                self._best_effort_revoke_rotated_credential(
                    old_encrypted_string_session,
                    replacement=new_raw_string_session,
                ),
                name=f"telegram-rotated-credential-revoke-{attempt_id}",
            )
        await self._retire_live_attempt(attempt_id, expected_live_attempt, revoke=False)
        if rotated_credential_revoke_task is not None:
            await asyncio.shield(rotated_credential_revoke_task)

        row = await self.session.get(TelegramSession, attempt.telegram_session_id)
        if row is None:  # pragma: no cover - committed FK invariant
            raise AdminConflictError("Telegram account disappeared after login completion.")
        counts_by_session = await self._admin_service._count_source_channels_by_session()
        return AdminTelegramLoginCompleteRead(
            telegram_session=self._admin_service._telegram_session_read(
                row,
                owned_channel_count=counts_by_session.get(row.id, 0),
            ),
            password_required=False,
            message="Telegram account logged in and stored securely.",
        )

    async def _persist_start_failure(
        self,
        attempt_id: uuid.UUID,
        exc: Exception,
        *,
        client: TelegramLoginClient,
    ) -> None:
        with suppress(Exception):
            await self._dispose_client(client, revoke=True)
        attempt = await self.session.get(TelegramSessionLoginAttempt, attempt_id)
        if attempt is None:
            return
        self._mark_attempt_failed_fields(attempt, exc)
        attempt.cleanup_status = "discarded"
        attempt.cleanup_completed_at = utcnow()
        attempt.encrypted_temp_string_session = None
        attempt.phone_code_hash = None
        attempt.qr_url = None
        await self.session.commit()

    async def _fail_and_cleanup(
        self,
        attempt_id: uuid.UUID,
        exc: Exception,
        *,
        expected_live_attempt: _LiveLoginAttempt | None = None,
    ) -> None:
        await self.session.rollback()
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is not None and attempt.status != "completed":
            self._mark_attempt_failed_fields(attempt, exc)
            await self.session.commit()
        await self._cleanup_attempt_credentials(attempt_id, expected_live_attempt=expected_live_attempt)

    @staticmethod
    def _mark_attempt_failed_fields(attempt: TelegramSessionLoginAttempt, exc: Exception) -> None:
        error_class = type(exc).__name__[:128]
        attempt.status = "failed"
        attempt.error_class = error_class
        attempt.error_text = f"Telegram login failed with {error_class}."[:MAX_TELEGRAM_ERROR_TEXT_LENGTH]
        attempt.completed_at = utcnow()

    async def _cleanup_attempt_credentials(
        self,
        attempt_id: uuid.UUID,
        *,
        expected_live_attempt: _LiveLoginAttempt | None = None,
    ) -> bool:
        attempt = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if attempt is None:
            live = self._take_live_attempt(attempt_id, expected=expected_live_attempt)
            if live is not None:
                await self._dispose_client(live.client, revoke=True)
            return live is not None
        if attempt.status == "completed" or attempt.cleanup_status == "promoted":
            live = self._take_live_attempt(attempt_id, expected=expected_live_attempt)
            if live is not None:
                await self._dispose_client(live.client, revoke=False)
            return False
        if attempt.cleanup_status == "discarded":
            live = self._take_live_attempt(attempt_id, expected=expected_live_attempt)
            if live is not None:
                await self._dispose_client(live.client, revoke=False)
            return False

        attempt.cleanup_attempts += 1
        encrypted_temp = attempt.encrypted_temp_string_session
        await self.session.commit()
        live_attempt = self._take_live_attempt(attempt_id, expected=expected_live_attempt)
        client = live_attempt.client if live_attempt is not None else None
        try:
            if client is None and encrypted_temp:
                temp_session = self._admin_service._decrypt_string_session(encrypted_temp)
                client = self._build_telegram_client(temp_session)
                await _maybe_await(client.connect())
            if client is not None:
                await self._dispose_client(client, revoke=True)
        except Exception as exc:
            await self.session.rollback()
            locked = await self.session.scalar(
                select(TelegramSessionLoginAttempt)
                .where(TelegramSessionLoginAttempt.id == attempt_id)
                .with_for_update(),
            )
            if locked is not None and locked.cleanup_status != "promoted":
                locked.cleanup_status = "failed"
                locked.cleanup_error_class = type(exc).__name__[:128]
                locked.cleanup_error_text = str(exc)[:MAX_TELEGRAM_ERROR_TEXT_LENGTH]
                await self.session.commit()
            if client is not None:
                with suppress(Exception):
                    await self._disconnect_client(client)
            logger.warning(
                "telegram_login_cleanup_failed",
                extra={"event": "telegram_login_cleanup_failed", "attempt_id": str(attempt_id)},
                exc_info=exc,
            )
            return False

        await self.session.rollback()
        locked = await self.session.scalar(
            select(TelegramSessionLoginAttempt)
            .where(TelegramSessionLoginAttempt.id == attempt_id)
            .with_for_update(),
        )
        if locked is not None and locked.cleanup_status != "promoted":
            locked.cleanup_status = "discarded"
            locked.cleanup_error_class = None
            locked.cleanup_error_text = None
            locked.cleanup_completed_at = utcnow()
            locked.encrypted_temp_string_session = None
            locked.phone_code_hash = None
            locked.qr_url = None
            await self.session.commit()
        return True

    async def _best_effort_revoke_rotated_credential(
        self,
        encrypted_string_session: str,
        *,
        replacement: SecretStr,
    ) -> None:
        try:
            old_session = self._admin_service._decrypt_string_session(encrypted_string_session)
            if old_session.get_secret_value() == replacement.get_secret_value():
                return
            old_client = self._build_telegram_client(old_session)
            await _maybe_await(old_client.connect())
            await self._dispose_client(old_client, revoke=True)
        except Exception:
            logger.warning(
                "telegram_login_rotated_credential_revoke_failed",
                extra={"event": "telegram_login_rotated_credential_revoke_failed"},
                exc_info=True,
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

    async def _wait_for_qr_completion(self, task: asyncio.Task[Any]) -> None:
        await asyncio.wait_for(asyncio.shield(task), timeout=QR_LOGIN_POLL_TIMEOUT_SECONDS)

    @staticmethod
    def _cancel_qr_wait_task(live_attempt: _LiveLoginAttempt) -> None:
        task = live_attempt.qr_wait_task
        if task is None or task.done():
            if task is not None and not task.cancelled():
                _ = task.exception()
            return
        task.cancel()

    @staticmethod
    def _cancel_attempt_expiry(live_attempt: _LiveLoginAttempt) -> None:
        handle = live_attempt.qr_expiry_cleanup_handle
        if handle is not None:
            handle.cancel()
            live_attempt.qr_expiry_cleanup_handle = None

    _cancel_qr_expiry_cleanup = _cancel_attempt_expiry

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
        self._cancel_attempt_expiry(live_attempt)
        self._cancel_qr_wait_task(live_attempt)
        return live_attempt

    def _launch_live_attempt_retirement(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
        *,
        revoke: bool,
    ) -> asyncio.Task[None] | None:
        if expected_attempt.retirement_task is not None:
            return expected_attempt.retirement_task
        if _LIVE_LOGIN_ATTEMPTS.get(attempt_id) is not expected_attempt:
            return None
        task = asyncio.create_task(self._retire_exact_live_attempt(attempt_id, expected_attempt, revoke=revoke))
        expected_attempt.retirement_task = task
        return task

    async def _retire_live_attempt(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
        *,
        revoke: bool,
    ) -> None:
        task = self._launch_live_attempt_retirement(attempt_id, expected_attempt, revoke=revoke)
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_consume_background_task_exception)
            raise

    async def _retire_exact_live_attempt(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
        *,
        revoke: bool,
    ) -> None:
        self._cancel_qr_wait_task(expected_attempt)
        try:
            await self._dispose_client(
                expected_attempt.client,
                revoke=revoke and not expected_attempt.credential_promoted,
            )
        finally:
            self._take_live_attempt(attempt_id, expected=expected_attempt)
            expected_attempt.retirement_task = None

    def _qr_wait_finished(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
        task: asyncio.Task[Any],
    ) -> None:
        if self._qr_wait_was_accepted(task):
            self._ensure_qr_completion_deadline_persistence(attempt_id, expected_attempt)

    @staticmethod
    def _qr_wait_was_accepted(task: asyncio.Task[Any] | None) -> bool:
        if task is None or not task.done() or task.cancelled():
            return False
        exception = task.exception()
        return exception is None or type(exception).__name__ == _PASSWORD_REQUIRED_ERROR_CLASS

    def _promote_qr_completion_cleanup(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
    ) -> datetime | None:
        if _LIVE_LOGIN_ATTEMPTS.get(attempt_id) is not expected_attempt:
            return None
        if expected_attempt.qr_completion_expires_at is not None:
            return expected_attempt.qr_completion_expires_at
        self._cancel_attempt_expiry(expected_attempt)
        completion_expires_at = utcnow() + LOGIN_ATTEMPT_TTL
        expected_attempt.qr_completion_expires_at = completion_expires_at
        expected_attempt.qr_expiry_cleanup_handle = self._schedule_attempt_expiry(
            attempt_id,
            expected_attempt,
            expires_at=completion_expires_at,
        )
        return completion_expires_at

    def _ensure_qr_completion_deadline_persistence(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
    ) -> asyncio.Task[None] | None:
        if not self._qr_wait_was_accepted(expected_attempt.qr_wait_task):
            return None
        completion_expires_at = self._promote_qr_completion_cleanup(attempt_id, expected_attempt)
        if completion_expires_at is None:
            return None
        existing_task = expected_attempt.qr_completion_persist_task
        if existing_task is not None:
            return existing_task
        task = _launch_background_login_task(
            self._persist_qr_completion_deadline(attempt_id, completion_expires_at),
            name=f"telegram-qr-completion-deadline-{attempt_id}",
        )
        expected_attempt.qr_completion_persist_task = task
        return task

    async def _persist_qr_completion_deadline(
        self,
        attempt_id: uuid.UUID,
        completion_expires_at: datetime,
    ) -> None:
        bind = self.session.bind
        if bind is None:  # pragma: no cover - request sessions are always bound
            raise RuntimeError("Telegram login request session is not bound to a database engine.")
        session_factory = async_sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
        try:
            async with session_factory() as session:
                attempt = await session.scalar(
                    select(TelegramSessionLoginAttempt)
                    .where(TelegramSessionLoginAttempt.id == attempt_id)
                    .with_for_update(),
                )
                if attempt is None or attempt.status != "pending":
                    return
                if attempt.expires_at < completion_expires_at:
                    attempt.expires_at = completion_expires_at
                await session.commit()
        except Exception:
            logger.exception(
                "telegram_qr_completion_deadline_persist_failed",
                extra={"event": "telegram_qr_completion_deadline_persist_failed", "attempt_id": str(attempt_id)},
            )
            raise

    def _schedule_attempt_expiry(
        self,
        attempt_id: uuid.UUID,
        live_attempt: _LiveLoginAttempt,
        *,
        expires_at: datetime,
    ) -> asyncio.TimerHandle:
        live_attempt.qr_expiry_cleanup_generation += 1
        generation = live_attempt.qr_expiry_cleanup_generation
        return _schedule_utc_callback(
            expires_at,
            self._cleanup_expired_live_attempt,
            attempt_id,
            live_attempt,
            generation,
        )

    _schedule_qr_expiry_cleanup = _schedule_attempt_expiry

    def _cleanup_expired_live_attempt(
        self,
        attempt_id: uuid.UUID,
        expected_attempt: _LiveLoginAttempt,
        cleanup_generation: int,
    ) -> None:
        if (
            _LIVE_LOGIN_ATTEMPTS.get(attempt_id) is not expected_attempt
            or expected_attempt.qr_expiry_cleanup_generation != cleanup_generation
        ):
            return
        if expected_attempt.qr_completion_expires_at is None and self._qr_wait_was_accepted(
            expected_attempt.qr_wait_task,
        ):
            self._ensure_qr_completion_deadline_persistence(attempt_id, expected_attempt)
            return
        task = self._launch_live_attempt_retirement(
            attempt_id,
            expected_attempt,
            revoke=not expected_attempt.credential_promoted,
        )
        if task is not None:
            task.add_done_callback(_consume_background_task_exception)

    _cleanup_expired_qr_attempt = _cleanup_expired_live_attempt

    @staticmethod
    async def _dispose_client(client: TelegramLoginClient, *, revoke: bool) -> None:
        if revoke:
            is_authorized = getattr(client, "is_user_authorized", None)
            authorized = bool(await _maybe_await(is_authorized())) if is_authorized is not None else False
            if authorized:
                log_out = getattr(client, "log_out", None)
                if log_out is None:
                    raise RuntimeError("Authorized Telegram login client does not support log_out().")
                logged_out = await _maybe_await(log_out())
                if logged_out is False:
                    raise RuntimeError("Telegram rejected temporary authorization revocation.")
                return
        await AdminTelegramLoginService._disconnect_client(client)

    @staticmethod
    async def _disconnect_client(client: TelegramLoginClient) -> None:
        disconnect = getattr(client, "disconnect", None)
        if disconnect is not None:
            await _maybe_await(disconnect())


async def run_telegram_login_cleanup_batch(
    session_factory: AsyncSessionFactory,
    *,
    batch_size: int,
) -> TelegramLoginCleanupBatchResult:
    """Expire due attempts and retry failed temporary-credential disposal."""

    now = utcnow()
    async with session_factory() as session:
        attempt_ids = list(
            (
                await session.execute(
                    select(TelegramSessionLoginAttempt.id)
                    .where(
                        or_(
                            (
                                TelegramSessionLoginAttempt.status.in_(tuple(_ACTIVE_ATTEMPT_STATUSES))
                                & (TelegramSessionLoginAttempt.expires_at <= now)
                            ),
                            (
                                TelegramSessionLoginAttempt.status.in_(
                                    tuple(_TERMINAL_ATTEMPT_STATUSES - {"completed"}),
                                )
                                & TelegramSessionLoginAttempt.cleanup_status.in_(("pending", "failed"))
                            ),
                        ),
                    )
                    .order_by(TelegramSessionLoginAttempt.expires_at.asc())
                    .limit(batch_size),
                )
            ).scalars(),
        )

    expired = 0
    cleaned = 0
    failed = 0
    for attempt_id in attempt_ids:
        async with session_factory() as session:
            service = AdminTelegramLoginService(session=session)
            was_expired, was_cleaned = await service.expire_or_retry_cleanup(attempt_id, now=now)
            expired += int(was_expired)
            cleaned += int(was_cleaned)
            attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
            failed += int(attempt is not None and attempt.cleanup_status == "failed")
    return TelegramLoginCleanupBatchResult(
        scanned=len(attempt_ids),
        expired=expired,
        cleaned=cleaned,
        failed=failed,
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _schedule_utc_callback(
    expires_at: datetime,
    callback: Callable[..., None],
    *args: Any,
) -> asyncio.TimerHandle:
    delay = max(0.0, (expires_at - utcnow()).total_seconds())
    return asyncio.get_running_loop().call_later(delay, callback, *args)


def _consume_background_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        _ = task.exception()


def _launch_background_login_task(
    awaitable: Coroutine[Any, Any, None],
    *,
    name: str,
) -> asyncio.Task[None]:
    task = asyncio.create_task(awaitable, name=name)
    _BACKGROUND_LOGIN_TASKS.add(task)

    def _retire_background_task(completed_task: asyncio.Task[Any]) -> None:
        _BACKGROUND_LOGIN_TASKS.discard(completed_task)
        _consume_background_task_exception(completed_task)

    task.add_done_callback(_retire_background_task)
    return task


async def _client_string_session(client: TelegramLoginClient) -> str:
    save = getattr(getattr(client, "session", None), "save", None)
    if save is None:
        raise AdminConflictError("Telegram client did not expose StringSession material.")
    raw_string_session = await _maybe_await(save())
    return raw_string_session.strip() if isinstance(raw_string_session, str) else ""


def _qr_login_expiry(qr_login: object, now: datetime) -> datetime:
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


def _telegram_account_login_lock(account_user_id: int) -> Any:
    """Return the PostgreSQL advisory-lock expression for one Telegram user id."""

    from sqlalchemy import func  # noqa: PLC0415

    return func.pg_advisory_xact_lock(account_user_id)


__all__ = [
    "AdminTelegramLoginService",
    "LOGIN_ATTEMPT_TTL",
    "TelegramLoginCleanupBatchResult",
    "TelegramLoginClient",
    "run_telegram_login_cleanup_batch",
]
