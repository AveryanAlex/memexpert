# ruff: noqa: TC001,TC002
"""Integration tests for cookie-authenticated browser-admin routes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid7

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import memexpert.services.admin as admin_service_module
import memexpert.services.admin_telegram_login as admin_telegram_login_module
from memexpert.models.collection import Collection, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    AdminMemeDestructiveAuditLog,
    BlockedPerceptualHash,
    BlockedPerceptualHashAuditLog,
    Meme,
    MemeFile,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemeSeoPage,
    MemeSource,
    MemeTemplate,
    ModerationDecision,
    ModerationReport,
    PipelineIngestRequest,
    PipelineStageJournal,
    SourceChannel,
    SourceChannelAudienceSnapshot,
    SourceChannelBackfillJob,
    SourceChannelPost,
    TelegramAdminAuditLog,
    TelegramSession,
    TelegramSessionLoginAttempt,
)
from memexpert.models.enums import (
    ChannelSuggestionStatus,
    ContentKind,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    MemeVisibilityMode,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    PipelineIngestRequestStatus,
    SourceAttachReason,
    SourceChannelAudienceCaptureReason,
    SourceChannelAudienceFetchStatus,
    SourceChannelBackfillJobStatus,
    SourceChannelPostStatus,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramSessionStatus,
)
from memexpert.models.user import ChannelSuggestion, User
from memexpert.schemas.admin import AdminSourceChannelCreateRequest
from memexpert.services import AuthService, UserService
from memexpert.services.admin_telegram_channel_resolver import (
    AdminTelegramChannelResolverError,
    ResolvedAdminTelegramChannel,
)
from tests.conftest import create_full_user_via_upgrade
from tests.integration.test_auth_routes import ACCESS_COOKIE_NAME, build_test_auth_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fastapi import FastAPI


async def _issue_user_cookie(
    session_factory: async_sessionmaker[AsyncSession],
    auth_settings_overrides: dict[str, str],
    *,
    email: str,
    is_admin: bool,
) -> str:
    async with session_factory() as session:
        user_service = UserService(session)
        auth_service: AuthService = build_test_auth_service(session, auth_settings_overrides)
        user = await create_full_user_via_upgrade(user_service, email=email)
        persisted_user = await session.get(User, user.id)
        assert persisted_user is not None
        persisted_user.is_admin = is_admin
        await session.commit()
        auth_session = await auth_service.issue_session_for_user(user)
        return auth_session.access_token


async def _issue_guest_admin_cookie(
    session_factory: async_sessionmaker[AsyncSession],
    auth_settings_overrides: dict[str, str],
) -> str:
    """Issue a token for an intentionally misconfigured guest admin fixture."""

    async with session_factory() as session:
        user_service = UserService(session)
        auth_service: AuthService = build_test_auth_service(session, auth_settings_overrides)
        guest = await user_service.create_guest_user()
        persisted_guest = await session.get(User, guest.id)
        assert persisted_guest is not None
        persisted_guest.is_admin = True
        await session.commit()
        auth_session = await auth_service.issue_session_for_user(guest)
        return auth_session.access_token


def _canonical_meme(
    *,
    file_key: str | None = None,
    file_quality: float = 0.9,
    **meme_kwargs: object,
) -> tuple[Meme, MemeFile]:
    meme_id = uuid7()
    file_id = uuid7()
    meme = Meme(id=meme_id, primary_file_id=file_id, **meme_kwargs)
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=file_key or f"admin/{meme_id}/primary.jpg",
        mime_type="image/jpeg",
        quality_score=file_quality,
    )
    return meme, file


async def _persist_canonical_meme(session: AsyncSession, meme: Meme, file: MemeFile) -> None:
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()


class _FakeSentCode:
    phone_code_hash = "fake-phone-code-hash"


class _FakeQrLogin:
    url = "tg://login?token=fake-qr-token"

    def __init__(
        self,
        *,
        require_password: bool = False,
        wait_event: asyncio.Event | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        self.require_password = require_password
        self.wait_event = wait_event
        self.expires = expires_at
        self.wait_started = asyncio.Event()
        self.wait_timeout: int | float | None = None

    async def wait(self, *, timeout: int | float | None = None) -> object:
        self.wait_timeout = timeout
        self.wait_started.set()
        if self.wait_event is not None:
            await self.wait_event.wait()
        if self.require_password:
            class SessionPasswordNeededError(Exception):
                pass

            raise SessionPasswordNeededError("password required")
        return object()


class _FakeTelegramUser:
    id = 777000
    username = "validated_admin_session"
    first_name = "Validated"
    last_name = "Admin"
    phone = "+10000007000"


class _FakeTelegramSessionStore:
    def __init__(self, client: _FakeTelegramLoginClient) -> None:
        self._client = client

    def save(self) -> str:
        return self._client.string_session


class _FakeTelegramLoginClient:
    def __init__(
        self,
        *,
        require_password: bool = False,
        invalid_code_attempts: int = 0,
        invalid_password_attempts: int = 0,
        qr_wait_event: asyncio.Event | None = None,
        qr_expires_at: datetime | None = None,
        get_me_started: asyncio.Event | None = None,
        get_me_release: asyncio.Event | None = None,
        disconnect_started: asyncio.Event | None = None,
        disconnect_release: asyncio.Event | None = None,
    ) -> None:
        self.require_password = require_password
        self.invalid_code_attempts = invalid_code_attempts
        self.invalid_password_attempts = invalid_password_attempts
        self.qr_wait_event = qr_wait_event
        self.qr_expires_at = qr_expires_at
        self.get_me_started = get_me_started
        self.get_me_release = get_me_release
        self.disconnect_started = disconnect_started
        self.disconnect_release = disconnect_release
        self.string_session = "temporary-telegram-login-session"
        self.session = _FakeTelegramSessionStore(self)
        self.sign_in_calls: list[dict[str, object]] = []
        self.disconnected = False
        self.logged_out = False
        self.disconnect_calls = 0
        self.disconnect_finished = asyncio.Event()
        self.qr_login_instance: _FakeQrLogin | None = None

    async def connect(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_started is not None:
            self.disconnect_started.set()
        if self.disconnect_release is not None:
            await self.disconnect_release.wait()
        self.disconnected = True
        self.disconnect_finished.set()

    async def is_user_authorized(self) -> bool:
        return self.string_session == "authorized-telegram-login-session"

    async def log_out(self) -> bool:
        self.logged_out = True
        await self.disconnect()
        return True

    async def send_code_request(self, phone: str) -> _FakeSentCode:
        self.string_session = f"temporary-telegram-login-session-{phone[-4:]}"
        return _FakeSentCode()

    async def sign_in(self, **kwargs: object) -> object:
        self.sign_in_calls.append(kwargs)
        if "code" in kwargs and self.invalid_code_attempts > 0:
            self.invalid_code_attempts -= 1

            class PhoneCodeInvalidError(Exception):
                pass

            raise PhoneCodeInvalidError("invalid code")
        if "password" in kwargs and self.invalid_password_attempts > 0:
            self.invalid_password_attempts -= 1
            class PasswordHashInvalidError(Exception):
                pass

            raise PasswordHashInvalidError("invalid password")
        if self.require_password and "code" in kwargs:
            class SessionPasswordNeededError(Exception):
                pass

            raise SessionPasswordNeededError("password required")
        self.string_session = "authorized-telegram-login-session"
        return object()

    async def get_me(self) -> _FakeTelegramUser:
        if self.get_me_started is not None:
            self.get_me_started.set()
        if self.get_me_release is not None:
            await self.get_me_release.wait()
        return _FakeTelegramUser()

    async def qr_login(self) -> _FakeQrLogin:
        self.string_session = "temporary-telegram-qr-login-session"
        self.qr_login_instance = _FakeQrLogin(
            require_password=self.require_password,
            wait_event=self.qr_wait_event,
            expires_at=self.qr_expires_at,
        )
        return self.qr_login_instance


class _MutableUtcClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _ManualQrExpiryHandle:
    def __init__(self, *, expires_at: datetime, callback: Callable[[], None]) -> None:
        self.expires_at = expires_at
        self._callback = callback
        self._cancelled = False
        self._fired = False

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def fire(self, *, even_if_cancelled: bool = False) -> bool:
        if self._fired or (self._cancelled and not even_if_cancelled):
            return False
        self._fired = True
        self._callback()
        return True


class _ManualQrExpiryScheduler:
    def __init__(self) -> None:
        self.handles: list[_ManualQrExpiryHandle] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def schedule(
            expires_at: datetime,
            callback: Callable[..., None],
            *args: object,
        ) -> asyncio.TimerHandle:
            handle = _ManualQrExpiryHandle(
                expires_at=expires_at,
                callback=lambda: callback(*args),
            )
            self.handles.append(handle)
            return cast("asyncio.TimerHandle", cast("object", handle))

        monkeypatch.setattr(
            admin_telegram_login_module,
            "_schedule_utc_callback",
            schedule,
        )

    def fire_due(self, now: datetime) -> list[_ManualQrExpiryHandle]:
        fired: list[_ManualQrExpiryHandle] = []
        for handle in list(self.handles):
            if handle.expires_at <= now and handle.fire():
                fired.append(handle)
        return fired


def _install_manual_qr_time(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_MutableUtcClock, _ManualQrExpiryScheduler]:
    clock = _MutableUtcClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    scheduler = _ManualQrExpiryScheduler()
    monkeypatch.setattr(admin_telegram_login_module, "utcnow", clock)
    scheduler.install(monkeypatch)
    return clock, scheduler


def _observe_qr_poll_wait(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    wait_started = asyncio.Event()
    original_wait = admin_telegram_login_module.AdminTelegramLoginService._wait_for_qr_completion  # noqa: SLF001

    async def observed_wait(
        service: admin_telegram_login_module.AdminTelegramLoginService,
        task: asyncio.Task[object],
    ) -> None:
        wait_started.set()
        await original_wait(service, task)

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_wait_for_qr_completion",
        observed_wait,
    )
    return wait_started


async def _disconnect_live_test_client(client: admin_telegram_login_module.TelegramLoginClient) -> object:
    return await admin_telegram_login_module._maybe_await(client.disconnect())  # noqa: SLF001


def _consume_timed_out_cleanup_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        _ = task.exception()


async def _wait_for_live_login_cleanup_tasks(
    tasks: list[asyncio.Task[Any]],
    *,
    deadline: float,
    operation: str,
    allow_cancelled: bool,
) -> tuple[dict[asyncio.Task[Any], object], list[BaseException]]:
    if not tasks:
        return {}, []

    timeout = max(0.0, deadline - asyncio.get_running_loop().time())
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    completed_results: dict[asyncio.Task[Any], object] = {}
    completed_errors: list[BaseException] = []
    for task in done:
        if task.cancelled():
            if not allow_cancelled:
                completed_errors.append(AssertionError(f"{operation} task was cancelled unexpectedly"))
            continue
        try:
            completed_results[task] = task.result()
        except BaseException as exc:  # noqa: BLE001
            completed_errors.append(exc)

    if pending:
        for task in pending:
            task.add_done_callback(_consume_timed_out_cleanup_task)
            task.cancel()
        timeout_error = TimeoutError(f"{operation} did not finish before the live-login cleanup deadline")
        if completed_errors:
            raise BaseExceptionGroup(
                "Live Telegram login cleanup timed out with completed task failures",
                [timeout_error, *completed_errors],
            )
        raise timeout_error

    return completed_results, completed_errors


@pytest.fixture(autouse=True)
async def _retire_live_admin_telegram_login_attempts() -> AsyncIterator[None]:
    """Keep process-global login clients from leaking across this file's tests."""

    yield

    leftovers = list(admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.items())  # noqa: SLF001
    live_attempts: list[admin_telegram_login_module._LiveLoginAttempt] = []  # noqa: SLF001
    for attempt_id, live_attempt in leftovers:
        if admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt:  # noqa: SLF001
            admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.pop(attempt_id, None)  # noqa: SLF001
            live_attempts.append(live_attempt)

    unexpected_errors: list[BaseException] = []
    cancelled_wait_tasks: list[asyncio.Task[object]] = []
    retirement_tasks: list[asyncio.Task[None]] = []
    retirement_owner_ids: list[int] = []
    cleanup_deadline = asyncio.get_running_loop().time() + 10.0

    try:
        for live_attempt in live_attempts:
            if live_attempt.qr_expiry_cleanup_handle is not None:
                live_attempt.qr_expiry_cleanup_handle.cancel()
                live_attempt.qr_expiry_cleanup_handle = None
            live_attempt.qr_expiry_cleanup_generation += 1
            if live_attempt.qr_wait_task is not None:
                if live_attempt.qr_wait_task.done():
                    if live_attempt.qr_wait_task.cancelled():
                        if live_attempt.retirement_task is None:
                            unexpected_errors.append(
                                asyncio.CancelledError("QR wait task was cancelled without retirement ownership"),
                            )
                    else:
                        wait_exception = live_attempt.qr_wait_task.exception()
                        if wait_exception is not None and type(wait_exception).__name__ != "SessionPasswordNeededError":
                            unexpected_errors.append(wait_exception)
                else:
                    live_attempt.qr_wait_task.cancel()
                    cancelled_wait_tasks.append(live_attempt.qr_wait_task)
            for release_attribute in ("get_me_release", "disconnect_release"):
                release_event = getattr(live_attempt.client, release_attribute, None)
                if isinstance(release_event, asyncio.Event):
                    release_event.set()
            if live_attempt.retirement_task is not None:
                retirement_tasks.append(live_attempt.retirement_task)
                retirement_owner_ids.append(id(live_attempt))

        wait_results, wait_errors = await _wait_for_live_login_cleanup_tasks(
            cancelled_wait_tasks,
            deadline=cleanup_deadline,
            operation="cancelled QR wait",
            allow_cancelled=True,
        )
        unexpected_errors.extend(wait_errors)
        for result in wait_results.values():
            unexpected_errors.append(
                AssertionError(f"cancelled QR wait task returned unexpectedly: {result!r}"),
            )

        retirement_results, retirement_errors = await _wait_for_live_login_cleanup_tasks(
            retirement_tasks,
            deadline=cleanup_deadline,
            operation="live-attempt retirement",
            allow_cancelled=False,
        )
        unexpected_errors.extend(retirement_errors)
        successful_retirement_owner_ids: set[int] = set()
        for owner_id, retirement_task in zip(retirement_owner_ids, retirement_tasks, strict=True):
            if retirement_task not in retirement_results:
                continue
            result = retirement_results[retirement_task]
            if result is None:
                successful_retirement_owner_ids.add(owner_id)
            else:
                unexpected_errors.append(
                    AssertionError(f"live-attempt retirement returned unexpectedly: {result!r}"),
                )

        clients_to_disconnect = [
            live_attempt.client
            for live_attempt in live_attempts
            if id(live_attempt) not in successful_retirement_owner_ids
            or getattr(live_attempt.client, "disconnected", True) is False
        ]
        disconnect_tasks = [
            asyncio.create_task(_disconnect_live_test_client(client))
            for client in clients_to_disconnect
        ]
        disconnect_results, disconnect_errors = await _wait_for_live_login_cleanup_tasks(
            disconnect_tasks,
            deadline=cleanup_deadline,
            operation="Telegram client disconnect",
            allow_cancelled=False,
        )
        unexpected_errors.extend(disconnect_errors)
        for result in disconnect_results.values():
            if result is not None:
                unexpected_errors.append(
                    AssertionError(f"Telegram client disconnect returned unexpectedly: {result!r}"),
                )

        background_tasks = list(admin_telegram_login_module._BACKGROUND_LOGIN_TASKS)  # noqa: SLF001
        _background_results, background_errors = await _wait_for_live_login_cleanup_tasks(
            background_tasks,
            deadline=cleanup_deadline,
            operation="Telegram login background cleanup",
            allow_cancelled=False,
        )
        unexpected_errors.extend(background_errors)
    finally:
        assert not admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
        assert not admin_telegram_login_module._BACKGROUND_LOGIN_TASKS  # noqa: SLF001

    if len(unexpected_errors) == 1:
        raise unexpected_errors[0]
    if unexpected_errors:
        raise BaseExceptionGroup("Unexpected live Telegram login cleanup failures", unexpected_errors)


async def test_live_login_cleanup_deadline_does_not_await_cancellation_resistant_task() -> None:
    task_started = asyncio.Event()
    initial_release = asyncio.Event()
    cancellation_seen = asyncio.Event()
    cancellation_release = asyncio.Event()

    async def cancellation_resistant_cleanup() -> None:
        task_started.set()
        try:
            await initial_release.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await cancellation_release.wait()

    cleanup_task = asyncio.create_task(cancellation_resistant_cleanup())
    await asyncio.wait_for(task_started.wait(), timeout=5)
    try:
        with pytest.raises(TimeoutError, match="cleanup deadline"):
            await _wait_for_live_login_cleanup_tasks(
                [cleanup_task],
                deadline=asyncio.get_running_loop().time(),
                operation="cancellation-resistant test cleanup",
                allow_cancelled=False,
            )

        assert cancellation_seen.is_set() is False
        assert cleanup_task.done() is False
        await asyncio.wait_for(cancellation_seen.wait(), timeout=5)
        assert cleanup_task.done() is False
    finally:
        initial_release.set()
        cancellation_release.set()
        await cleanup_task


async def test_admin_routes_require_session_cookie_admin_flag_and_ignore_operator_header(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    meme_id = "11111111-1111-4111-8111-111111111111"
    transport = ASGITransport(app=auth_app)

    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        anonymous_session_response = await anonymous_client.get("/api/v1/admin/session")
        anonymous_telegram_sessions_response = await anonymous_client.get("/api/v1/admin/telegram/sessions")
        anonymous_add_reference_response = await anonymous_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json={
                "reference": "@public_channel",
                "telegram_session_id": meme_id,
            },
        )
        anonymous_detail_response = await anonymous_client.get(f"/api/v1/admin/memes/{meme_id}")
        anonymous_override_response = await anonymous_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={"is_nsfw": True},
        )
        anonymous_seo_pages_response = await anonymous_client.get("/api/v1/admin/seo-pages")
        anonymous_seo_edit_response = await anonymous_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"slug": "permission", "title": "Permission", "meta": "Permission", "alt": "Permission"},
        )
        anonymous_seo_regenerate_response = await anonymous_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": meme_id},
        )

    non_admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-denied@example.com",
        is_admin=False,
    )
    guest_admin_token = await _issue_guest_admin_cookie(
        postgres_session_factory,
        auth_settings_overrides,
    )
    async with AsyncClient(transport=transport, base_url="https://testserver") as non_admin_client:
        non_admin_client.cookies.set(ACCESS_COOKIE_NAME, non_admin_token)
        forbidden_session_response = await non_admin_client.get("/api/v1/admin/session")
        forbidden_detail_response = await non_admin_client.get(f"/api/v1/admin/memes/{meme_id}")
        forbidden_override_response = await non_admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={"is_nsfw": True},
        )
        forbidden_delete_response = await non_admin_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            json={"confirmation": meme_id, "note": "test"},
        )
        forbidden_merge_response = await non_admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/merge",
            json={"target_meme_id": meme_id, "confirmation": meme_id, "note": "test"},
        )
        forbidden_template_create_response = await non_admin_client.post(
            "/api/v1/admin/meme-templates",
            json={"slug": "permission-test", "name": "Permission Test"},
        )
        forbidden_source_mark_dead_response = await non_admin_client.post(
            f"/api/v1/admin/source-channels/{meme_id}/mark-dead",
            json={"confirmation": meme_id},
        )
        forbidden_seo_pages_response = await non_admin_client.get("/api/v1/admin/seo-pages")
        forbidden_seo_edit_response = await non_admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"slug": "permission", "title": "Permission", "meta": "Permission", "alt": "Permission"},
        )
        forbidden_seo_regenerate_response = await non_admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": meme_id},
        )
        forbidden_telegram_session_create_response = await non_admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={"name": "permission-test", "display_name": "Permission Test"},
        )
        forbidden_telegram_channels_response = await non_admin_client.get("/api/v1/admin/telegram/channels")
        forbidden_add_reference_response = await non_admin_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json={
                "reference": "@public_channel",
                "telegram_session_id": meme_id,
            },
        )
        forbidden_reports_response = await non_admin_client.get("/api/v1/admin/moderation-reports")
        forbidden_blocked_hashes_response = await non_admin_client.get("/api/v1/admin/blocked-perceptual-hashes")

    async with AsyncClient(transport=transport, base_url="https://testserver") as guest_admin_client:
        guest_admin_client.cookies.set(ACCESS_COOKIE_NAME, guest_admin_token)
        guest_admin_session_response = await guest_admin_client.get("/api/v1/admin/session")
        guest_admin_overview_response = await guest_admin_client.get("/api/v1/admin/overview")

    async with AsyncClient(transport=transport, base_url="https://testserver") as operator_header_client:
        operator_session_response = await operator_header_client.get(
            "/api/v1/admin/session",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )
        operator_detail_response = await operator_header_client.get(
            f"/api/v1/admin/memes/{meme_id}",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )
        operator_delete_response = await operator_header_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            headers={"X-Pipeline-Operator-Token": "anything"},
            json={"confirmation": meme_id, "note": "test"},
        )
        operator_seo_pages_response = await operator_header_client.get(
            "/api/v1/admin/seo-pages",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )
        operator_telegram_sessions_response = await operator_header_client.get(
            "/api/v1/admin/telegram/sessions",
            headers={"X-Pipeline-Operator-Token": "anything"},
        )

    assert anonymous_session_response.status_code == 401
    assert anonymous_telegram_sessions_response.status_code == 401
    assert anonymous_add_reference_response.status_code == 401
    assert anonymous_detail_response.status_code == 401
    assert anonymous_override_response.status_code == 401
    assert anonymous_seo_pages_response.status_code == 401
    assert anonymous_seo_edit_response.status_code == 401
    assert anonymous_seo_regenerate_response.status_code == 401
    assert forbidden_session_response.status_code == 403
    assert forbidden_detail_response.status_code == 403
    assert forbidden_override_response.status_code == 403
    assert forbidden_delete_response.status_code == 403
    assert forbidden_merge_response.status_code == 403
    assert forbidden_template_create_response.status_code == 403
    assert forbidden_source_mark_dead_response.status_code == 403
    assert forbidden_seo_pages_response.status_code == 403
    assert forbidden_seo_edit_response.status_code == 403
    assert forbidden_seo_regenerate_response.status_code == 403
    assert forbidden_telegram_session_create_response.status_code == 403
    assert forbidden_telegram_channels_response.status_code == 403
    assert forbidden_add_reference_response.status_code == 403
    assert forbidden_reports_response.status_code == 403
    assert forbidden_blocked_hashes_response.status_code == 403
    assert guest_admin_session_response.status_code == 403
    assert guest_admin_overview_response.status_code == 403
    assert forbidden_session_response.json()["code"] == "admin_required"
    assert forbidden_reports_response.json()["code"] == "admin_required"
    assert forbidden_blocked_hashes_response.json()["code"] == "admin_required"
    assert guest_admin_session_response.json()["code"] == "admin_required"
    assert guest_admin_overview_response.json()["code"] == "admin_required"
    assert operator_session_response.status_code == 401
    assert operator_detail_response.status_code == 401
    assert operator_delete_response.status_code == 401
    assert operator_seo_pages_response.status_code == 401
    assert operator_telegram_sessions_response.status_code == 401


async def test_admin_overview_returns_authorized_aggregate_counts_at_source_boundaries(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_email = "admin-overview@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    non_admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-overview-denied@example.com",
        is_admin=False,
    )
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    async with postgres_session_factory() as session:
        admin = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        ready_account = TelegramSession(
            name="overview-ready",
            display_name="Overview ready",
            encrypted_string_session="encrypted-ready-session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        missing_session_account = TelegramSession(
            name="overview-missing-session",
            display_name="Overview missing session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        disabled_account = TelegramSession(
            name="overview-disabled",
            display_name="Overview disabled",
            encrypted_string_session="encrypted-disabled-session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=False,
        )
        stopped_account = TelegramSession(
            name="overview-stopped",
            display_name="Overview stopped",
            encrypted_string_session="encrypted-stopped-session",
            status=TelegramSessionStatus.STOPPED,
            enabled=True,
        )
        blank_session_account = TelegramSession(
            name="overview-blank-session",
            display_name="Overview blank session",
            encrypted_string_session=" \t\n ",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        future_flood_wait_account = TelegramSession(
            name="overview-future-flood-wait",
            display_name="Overview future flood wait",
            encrypted_string_session="encrypted-future-flood-wait-session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
            flood_wait_until=now + timedelta(minutes=1),
        )
        expired_flood_wait_account = TelegramSession(
            name="overview-expired-flood-wait",
            display_name="Overview expired flood wait",
            encrypted_string_session="encrypted-expired-flood-wait-session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
            flood_wait_until=now - timedelta(microseconds=1),
        )
        quarantined_account = TelegramSession(
            name="overview-quarantined",
            display_name="Overview quarantined",
            encrypted_string_session="encrypted-quarantined-session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
            quarantined_at=now - timedelta(minutes=1),
        )
        session.add_all(
            [
                ready_account,
                missing_session_account,
                disabled_account,
                stopped_account,
                blank_session_account,
                future_flood_wait_account,
                expired_flood_wait_account,
                quarantined_account,
            ],
        )
        await session.flush()

        session.add_all(
            [
                ChannelSuggestion(
                    user_id=admin.id,
                    platform=SourcePlatform.TELEGRAM,
                    channel_url="https://t.me/pending-overview",
                    status="pending",
                ),
                ChannelSuggestion(
                    user_id=admin.id,
                    platform=SourcePlatform.TELEGRAM,
                    channel_url="https://t.me/approved-overview",
                    status="approved",
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-healthy",
                    title="Overview healthy",
                    telegram_session_id=ready_account.id,
                    last_fetched_at=now - timedelta(minutes=30),
                    created_at=now - timedelta(days=2),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-fresh-boundary",
                    title="Overview fresh boundary",
                    telegram_session_id=ready_account.id,
                    last_fetched_at=now - timedelta(days=1),
                    created_at=now - timedelta(days=2),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-stale",
                    title="Overview stale",
                    telegram_session_id=ready_account.id,
                    last_fetched_at=now - timedelta(days=1, microseconds=1),
                    created_at=now - timedelta(days=2),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-waiting",
                    title="Overview waiting",
                    telegram_session_id=ready_account.id,
                    created_at=now - timedelta(minutes=15) + timedelta(microseconds=1),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-grace-expired",
                    title="Overview grace expired",
                    telegram_session_id=ready_account.id,
                    created_at=now - timedelta(minutes=15),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-orphaned",
                    title="Overview orphaned",
                    last_fetched_at=now - timedelta(minutes=1),
                    created_at=now - timedelta(days=2),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-orphaned-waiting",
                    title="Overview orphaned waiting",
                    created_at=now - timedelta(minutes=15) + timedelta(microseconds=1),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-orphaned-stale",
                    title="Overview orphaned stale",
                    last_fetched_at=now - timedelta(days=2),
                    created_at=now - timedelta(days=2),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-paused",
                    title="Overview paused",
                    telegram_session_id=ready_account.id,
                    is_paused=True,
                    last_fetched_at=now - timedelta(days=2),
                    created_at=now - timedelta(days=2),
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="overview-removed",
                    title="Overview removed",
                    telegram_session_id=ready_account.id,
                    is_active=False,
                    last_fetched_at=now - timedelta(days=2),
                    created_at=now - timedelta(days=2),
                ),
                MemeTemplate(slug="overview-uncurated", name="Overview uncurated", is_curated=False),
                MemeTemplate(slug="overview-curated", name="Overview curated", is_curated=True),
            ],
        )
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        private_meme, private_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=False, is_nsfw=False)
        nsfw_meme, nsfw_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=True)
        covered_meme, covered_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        await _persist_canonical_meme(session, meme, meme_file)
        await _persist_canonical_meme(session, private_meme, private_file)
        await _persist_canonical_meme(session, nsfw_meme, nsfw_file)
        await _persist_canonical_meme(session, covered_meme, covered_file)
        session.add(
            MemeSeoPage(
                meme=covered_meme,
                slug="overview-covered",
                page_title="Overview covered",
                meta_description="Overview covered description",
                alt_text="Overview covered alt text",
                model_id="test-model",
                prompt_version="test-version",
            ),
        )
        session.add_all(
            [
                ModerationReport(meme=meme, status=ModerationReportStatus.PENDING),
                ModerationReport(meme=meme, status=ModerationReportStatus.IN_REVIEW),
                ModerationReport(meme=meme, status=ModerationReportStatus.RESOLVED),
            ],
        )
        await session.commit()

    monkeypatch.setattr(admin_service_module, "utcnow", lambda: now)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as anonymous_client:
        anonymous_response = await anonymous_client.get("/api/v1/admin/overview")
    async with AsyncClient(transport=transport, base_url="https://testserver") as non_admin_client:
        non_admin_client.cookies.set(ACCESS_COOKIE_NAME, non_admin_token)
        forbidden_response = await non_admin_client.get("/api/v1/admin/overview")
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.get("/api/v1/admin/overview")

    assert anonymous_response.status_code == 401
    assert forbidden_response.status_code == 403
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "open_report_count": 2,
        "pending_suggestion_count": 1,
        "source_attention_count": 4,
        "orphaned_source_count": 2,
        "stale_source_count": 3,
        "waiting_source_count": 2,
        "healthy_source_count": 2,
        "telegram_account_attention_count": 6,
        "ready_telegram_account_count": 2,
        "missing_seo_count": 1,
        "uncurated_template_count": 1,
    }
    assert payload["source_attention_count"] < payload["orphaned_source_count"] + payload["stale_source_count"]
    assert payload["missing_seo_count"] == 1


async def test_admin_can_approve_channel_suggestion_through_cookie_session(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-approve@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        owner = (await session.execute(select(User).where(User.email == "admin-approve@example.com"))).scalar_one()
        suggestion = ChannelSuggestion(
            user_id=owner.id,
            platform=SourcePlatform.TELEGRAM,
            channel_url="https://t.me/memexpert_source",
        )
        session.add(suggestion)
        await session.commit()
        await session.refresh(suggestion)
        suggestion_id = suggestion.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/channel-suggestions/{suggestion_id}/approve",
            json={"admin_note": "Looks relevant"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approved"
    assert payload["admin_note"] == "Looks relevant"
    assert payload["reviewed_at"] is not None

    async with postgres_session_factory() as session:
        persisted = await session.get(ChannelSuggestion, suggestion_id)
        assert persisted is not None
        assert persisted.status.value == "approved"


async def test_admin_can_list_read_and_resolve_moderation_report_with_audited_decision(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-resolve-report@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        user_service = UserService(session)
        reporter = await create_full_user_via_upgrade(user_service, email="reporter@example.com")
        admin = (
            await session.execute(select(User).where(User.email == "admin-resolve-report@example.com"))
        ).scalar_one()
        meme, meme_file = _canonical_meme(
            file_key="admin/private/moderation-primary.jpg",
            media_type=ContentKind.IMAGE,
            is_public=False,
            is_nsfw=False,
        )
        meme_file.width = 900
        meme_file.height = 600
        report = ModerationReport(
            meme=meme,
            reporter_user_id=reporter.id,
            reason=ModerationReason.NSFW,
            note="This should be marked nsfw",
        )
        await _persist_canonical_meme(session, meme, meme_file)
        session.add(
            PipelineStageJournal(
                meme_file_id=meme_file.id,
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.SUCCEEDED,
                attempt_count=2,
                last_event_id=uuid7(),
                is_retryable=False,
                finished_at=datetime.now(UTC),
            )
        )
        session.add(report)
        await session.commit()
        await session.refresh(report)
        report_id = report.id
        meme_id = meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        memes_response = await admin_client.get("/api/v1/admin/memes")
        list_response = await admin_client.get("/api/v1/admin/moderation-reports")
        detail_response = await admin_client.get(f"/api/v1/admin/memes/{meme_id}")
        resolve_response = await admin_client.post(
            f"/api/v1/admin/moderation-reports/{report_id}/resolve",
            json={"action": "mark_nsfw", "reason": "nsfw", "note": "Confirmed by moderator"},
        )
        history_response = await admin_client.get(f"/api/v1/admin/moderation-decisions?meme_id={meme_id}")

    assert memes_response.status_code == 200
    assert [item["id"] for item in memes_response.json()] == [str(meme_id)]
    expected_file_id = str(meme_file.id)
    expected_preview_url = f"/api/v1/media/files/{expected_file_id}/preview"
    assert memes_response.json()[0]["primary_file"]["render"]["preview_url"] == expected_preview_url
    assert "admin/private/moderation-primary.jpg" not in memes_response.text

    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [str(report_id)]
    assert list_response.json()[0]["meme"]["id"] == str(meme_id)
    assert list_response.json()[0]["meme"]["primary_file"]["render"]["preview_url"] == expected_preview_url
    assert "admin/private/moderation-primary.jpg" not in list_response.text

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["meme"]["id"] == str(meme_id)
    assert detail_payload["meme"]["is_public"] is False
    assert detail_payload["meme"]["primary_file"]["render"]["preview_url"] == expected_preview_url
    assert [item["id"] for item in detail_payload["reports"]] == [str(report_id)]
    assert detail_payload["decisions"] == []
    assert len(detail_payload["processing_files"]) == 1
    processing_file = detail_payload["processing_files"][0]
    assert processing_file["id"] == expected_file_id
    assert processing_file["is_primary"] is True
    assert processing_file["status"] == "ready"
    assert processing_file["width"] == 900
    assert processing_file["height"] == 600
    assert len(processing_file["stages"]) == 1
    processing_stage = processing_file["stages"][0]
    assert processing_stage["stage"] == "transcode"
    assert processing_stage["status"] == "succeeded"
    assert processing_stage["attempt_count"] == 2
    actions = {item["capability"]: item for item in processing_stage["actions"]}
    assert set(actions) == {"retry_stage", "replay_stage", "regenerate_derivatives"}
    assert actions["retry_stage"]["available"] is False
    assert actions["replay_stage"]["available"] is True
    assert actions["replay_stage"]["scopes"] == ["stage_only", "stage_and_dependents"]
    assert actions["regenerate_derivatives"]["available"] is False
    assert actions["regenerate_derivatives"]["scopes"] == ["stage_only"]
    assert actions["regenerate_derivatives"]["blocked_prerequisites"] == [
        "Derivative regeneration applies only to moving media."
    ]
    assert "admin/private/moderation-primary.jpg" not in detail_response.text

    assert resolve_response.status_code == 200
    resolved_payload = resolve_response.json()
    assert resolved_payload["status"] == "resolved"
    assert resolved_payload["resolved_by_admin_user_id"] == str(admin_id)
    assert resolved_payload["meme"]["is_nsfw"] is True
    assert resolved_payload["meme"]["primary_file"]["render"]["preview_url"] == expected_preview_url
    assert "admin/private/moderation-primary.jpg" not in resolve_response.text

    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert len(history_payload) == 1
    assert history_payload[0]["report_id"] == str(report_id)
    assert history_payload[0]["action"] == "mark_nsfw"
    assert history_payload[0]["previous_is_nsfw"] is False
    assert history_payload[0]["new_is_nsfw"] is True
    assert history_payload[0]["previous_template_id"] is None
    assert history_payload[0]["new_template_id"] is None

    async with postgres_session_factory() as session:
        persisted_report = await session.get(ModerationReport, report_id)
        persisted_meme = await session.get(Meme, meme_id)
        persisted_decision = await session.scalar(
            select(ModerationDecision).where(ModerationDecision.report_id == report_id),
        )

        assert persisted_report is not None
        assert persisted_report.status is ModerationReportStatus.RESOLVED
        assert persisted_report.resolved_by_admin_user_id == admin_id
        assert persisted_meme is not None
        assert persisted_meme.is_nsfw is True
        assert persisted_decision is not None
        assert persisted_decision.admin_user_id == admin_id
        assert persisted_decision.reason is ModerationReason.NSFW


async def test_admin_direct_meme_override_persists_template_and_decision_audit_records(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-direct-override@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (
            await session.execute(select(User).where(User.email == "admin-direct-override@example.com"))
        ).scalar_one()
        template = MemeTemplate(slug="new-template", name="New Template")
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        session.add(template)
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        await session.refresh(template)
        await session.refresh(meme)
        template_id = template.id
        meme_id = meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        override_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/moderation",
            json={
                "visibility_mode": "force_private",
                "is_nsfw": True,
                "template_id": str(template_id),
                "reason": "spam",
                "note": "Manual override from admin screen",
            },
        )
        detail_response = await admin_client.get(f"/api/v1/admin/memes/{meme_id}")

    assert override_response.status_code == 200
    payload = override_response.json()
    assert payload["is_public"] is False
    assert payload["visibility_mode"] == "force_private"
    assert payload["is_nsfw"] is True
    assert payload["template_id"] == str(template_id)

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    decision_actions = {decision["action"] for decision in detail_payload["decisions"]}
    assert decision_actions == {"template_override", "override_flags"}

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        persisted_decisions = (
            await session.execute(
                select(ModerationDecision)
                .where(ModerationDecision.meme_id == meme_id)
                .order_by(ModerationDecision.created_at.asc(), ModerationDecision.id.asc()),
            )
        ).scalars().all()

        assert persisted_meme is not None
        assert persisted_meme.is_public is False
        assert persisted_meme.is_nsfw is True
        assert persisted_meme.template_id == template_id
        decisions_by_action = {decision.action: decision for decision in persisted_decisions}
        assert set(decisions_by_action) == {ModerationAction.OVERRIDE_FLAGS, ModerationAction.TEMPLATE_OVERRIDE}
        flag_decision = decisions_by_action[ModerationAction.OVERRIDE_FLAGS]
        template_decision = decisions_by_action[ModerationAction.TEMPLATE_OVERRIDE]
        assert flag_decision.report_id is None
        assert flag_decision.admin_user_id == admin_id
        assert flag_decision.reason is ModerationReason.SPAM
        assert flag_decision.previous_is_public is True
        assert flag_decision.previous_is_nsfw is False
        assert flag_decision.new_is_public is False
        assert flag_decision.new_is_nsfw is True
        assert flag_decision.previous_template_id is None
        assert flag_decision.new_template_id == template_id
        assert template_decision.previous_template_id is None
        assert template_decision.new_template_id == template_id


async def test_admin_template_create_rejects_duplicate_slug(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-create-template@example.com",
        is_admin=True,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post(
            "/api/v1/admin/meme-templates",
            json={"slug": "launch-template", "name": "Launch Template", "is_curated": True},
        )
        duplicate_response = await admin_client.post(
            "/api/v1/admin/meme-templates",
            json={"slug": "launch-template", "name": "Duplicate Launch Template"},
        )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["slug"] == "launch-template"
    assert payload["name"] == "Launch Template"
    assert payload["is_curated"] is True
    assert duplicate_response.status_code == 409
    assert "slug" in duplicate_response.json()["detail"]


async def test_admin_can_manage_blocked_perceptual_hashes_with_audit_and_safe_delete(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-blocked-phash@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (
            await session.execute(select(User).where(User.email == "admin-blocked-phash@example.com"))
        ).scalar_one()
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        invalid_response = await admin_client.post(
            "/api/v1/admin/blocked-perceptual-hashes",
            json={"perceptual_hash": "not-hex", "reason": "spam"},
        )
        create_response = await admin_client.post(
            "/api/v1/admin/blocked-perceptual-hashes",
            json={
                "perceptual_hash": "ABCDEF1234567890",
                "max_hamming_distance": 2,
                "reason": "spam",
                "note": "seed ban",
            },
        )
        duplicate_response = await admin_client.post(
            "/api/v1/admin/blocked-perceptual-hashes",
            json={"perceptual_hash": "abcdef1234567890", "reason": "spam"},
        )
        blocked_hash_id = create_response.json()["id"]
        update_response = await admin_client.patch(
            f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}",
            json={
                "perceptual_hash": "abcdef1234567891",
                "max_hamming_distance": 3,
                "reason": "copyright",
                "note": "tightened pattern",
                "is_active": True,
            },
        )
        list_response = await admin_client.get("/api/v1/admin/blocked-perceptual-hashes?is_active=true")
        deactivate_response = await admin_client.post(
            f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}/deactivate",
            json={"note": "temporary pause"},
        )
        audit_response = await admin_client.get(
            f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}/audit-log",
        )
        delete_response = await admin_client.delete(f"/api/v1/admin/blocked-perceptual-hashes/{blocked_hash_id}")

    assert invalid_response.status_code == 422
    assert create_response.status_code == 201
    create_payload = create_response.json()
    assert create_payload["perceptual_hash"] == "abcdef1234567890"
    assert create_payload["hash_algorithm"] == "phash"
    assert create_payload["hash_size"] == 64
    assert create_payload["created_by_admin_user_id"] == str(admin_id)
    assert duplicate_response.status_code == 409
    assert update_response.status_code == 200
    assert update_response.json()["perceptual_hash"] == "abcdef1234567891"
    assert update_response.json()["max_hamming_distance"] == 3
    assert [item["id"] for item in list_response.json()] == [blocked_hash_id]
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["action"] == "deactivate"
    assert audit_response.status_code == 200
    assert [item["action"] for item in audit_response.json()] == ["deactivate", "update", "create"]
    assert delete_response.status_code == 200
    assert delete_response.json()["action"] == "delete"

    async with postgres_session_factory() as session:
        blocked_hash_uuid = UUID(blocked_hash_id)
        deleted = await session.get(BlockedPerceptualHash, blocked_hash_uuid)
        audit_rows = (
            await session.execute(
                select(BlockedPerceptualHashAuditLog)
                .where(BlockedPerceptualHashAuditLog.blocked_perceptual_hash_id == blocked_hash_uuid)
                .order_by(BlockedPerceptualHashAuditLog.created_at.asc(), BlockedPerceptualHashAuditLog.id.asc()),
            )
        ).scalars().all()

    assert deleted is None
    assert [row.action for row in audit_rows] == ["create", "update", "deactivate", "delete"]
    assert {row.admin_user_id for row in audit_rows} == {admin_id}


async def test_admin_template_merge_reassigns_memes_and_writes_template_override_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-merge-template@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (
            await session.execute(select(User).where(User.email == "admin-merge-template@example.com"))
        ).scalar_one()
        source_template = MemeTemplate(slug="duplicate-template", name="Duplicate Template")
        target_template = MemeTemplate(slug="canonical-template", name="Canonical Template")
        first_meme, first_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            template=source_template,
        )
        second_meme, second_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=False,
            is_nsfw=True,
            template=source_template,
        )
        session.add_all([source_template, target_template])
        await _persist_canonical_meme(session, first_meme, first_file)
        await _persist_canonical_meme(session, second_meme, second_file)
        await session.commit()
        source_template_id = source_template.id
        target_template_id = target_template.id
        first_meme_id = first_meme.id
        second_meme_id = second_meme.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/meme-templates/{source_template_id}/merge",
            json={
                "target_template_id": str(target_template_id),
                "confirmation": str(source_template_id),
                "note": "Canonical template selected by content ops",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "merge"
    assert payload["source_template_id"] == str(source_template_id)
    assert payload["target_template_id"] == str(target_template_id)
    assert payload["affected_meme_count"] == 2

    async with postgres_session_factory() as session:
        deleted_source = await session.get(MemeTemplate, source_template_id)
        persisted_first_meme = await session.get(Meme, first_meme_id)
        persisted_second_meme = await session.get(Meme, second_meme_id)
        decisions = (
            await session.execute(
                select(ModerationDecision)
                .where(ModerationDecision.meme_id.in_([first_meme_id, second_meme_id]))
                .order_by(ModerationDecision.created_at.asc(), ModerationDecision.id.asc()),
            )
        ).scalars().all()

        assert deleted_source is None
        assert persisted_first_meme is not None
        assert persisted_second_meme is not None
        assert persisted_first_meme.template_id == target_template_id
        assert persisted_second_meme.template_id == target_template_id
        assert len(decisions) == 2
        assert {decision.action for decision in decisions} == {ModerationAction.TEMPLATE_OVERRIDE}
        assert {decision.admin_user_id for decision in decisions} == {admin_id}
        assert {decision.new_template_id for decision in decisions} == {target_template_id}
        assert all("Canonical template selected" in (decision.note or "") for decision in decisions)


async def test_admin_template_delete_only_allows_unreferenced_templates(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-template@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        referenced_template = MemeTemplate(slug="referenced-template", name="Referenced Template")
        unreferenced_template = MemeTemplate(slug="unreferenced-template", name="Unreferenced Template")
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            template=referenced_template,
        )
        session.add_all([referenced_template, unreferenced_template])
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        referenced_template_id = referenced_template.id
        unreferenced_template_id = unreferenced_template.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        blocked_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/meme-templates/{referenced_template_id}",
            json={"confirmation": str(referenced_template_id), "note": "try referenced delete"},
        )
        deleted_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/meme-templates/{unreferenced_template_id}",
            json={"confirmation": str(unreferenced_template_id), "note": "safe cleanup"},
        )

    assert blocked_response.status_code == 409
    assert "referenced by memes" in blocked_response.json()["detail"]
    assert deleted_response.status_code == 200
    assert deleted_response.json()["action"] == "delete"

    async with postgres_session_factory() as session:
        persisted_referenced_template = await session.get(MemeTemplate, referenced_template_id)
        deleted_unreferenced_template = await session.get(MemeTemplate, unreferenced_template_id)
        assert persisted_referenced_template is not None
        assert deleted_unreferenced_template is None


async def test_admin_manual_seo_edit_creates_updates_and_rejects_slug_conflict(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-manual-seo@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            tags=["original"],
        )
        conflict_meme, conflict_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
        )
        await _persist_canonical_meme(session, meme, meme_file)
        await _persist_canonical_meme(session, conflict_meme, conflict_file)
        session.add(
            MemeSeoPage(
                meme=conflict_meme,
                slug="taken-slug",
                page_title="Taken slug",
                meta_description="Taken slug",
                alt_text="Taken slug",
                tags=["taken"],
                model_id="test-model",
                prompt_version="test-version",
            ),
        )
        await session.commit()
        meme_id = meme.id
        conflict_meme_id = conflict_meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={
                "slug": " Launch Slug! ",
                "title": " Launch Title ",
                "meta": " Launch meta description ",
                "alt": " Launch alt text ",
                "caption": " Launch caption ",
                "body": " Launch body text ",
                "tags": "Funny, FUNNY, Reaction Tag",
            },
        )
        list_response = await admin_client.get("/api/v1/admin/seo-pages?limit=10")
        update_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"caption": "Updated caption", "tags": ["Reaction Tag", "new tag", "reaction tag"]},
        )
        conflict_response = await admin_client.patch(
            f"/api/v1/admin/memes/{meme_id}/seo-page",
            json={"slug": "taken-slug"},
        )

    assert create_response.status_code == 200
    create_payload = create_response.json()
    assert create_payload["slug"] == "launch-slug"
    assert create_payload["page_title"] == "Launch Title"
    assert create_payload["meta_description"] == "Launch meta description"
    assert create_payload["alt_text"] == "Launch alt text"
    assert create_payload["caption"] == "Launch caption"
    assert create_payload["body_text"] == "Launch body text"
    assert create_payload["tags"] == ["funny", "reaction-tag"]
    assert create_payload["model_id"] == "admin-manual"
    assert create_payload["prompt_version"] == "admin-manual"
    assert create_payload["generated_at"] is not None
    assert create_payload["edited_at"] is not None

    assert list_response.status_code == 200
    review_rows = {item["meme"]["id"]: item for item in list_response.json()}
    assert review_rows[str(meme_id)]["status"] == "edited"
    assert review_rows[str(meme_id)]["seo_page"]["slug"] == "launch-slug"
    assert review_rows[str(meme_id)]["meme"]["popularity_score"] == 0.0
    assert review_rows[str(conflict_meme_id)]["status"] == "generated"

    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["slug"] == "launch-slug"
    assert update_payload["caption"] == "Updated caption"
    assert update_payload["tags"] == ["reaction-tag", "new-tag"]
    assert update_payload["edited_at"] is not None
    assert conflict_response.status_code == 409
    assert "slug" in conflict_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_page = await session.get(MemeSeoPage, meme_id)
        persisted_meme = await session.get(Meme, meme_id)
        assert persisted_page is not None
        assert persisted_page.slug == "launch-slug"
        assert persisted_page.caption == "Updated caption"
        assert persisted_page.tags == ["reaction-tag", "new-tag"]
        assert persisted_page.edited_at is not None
        assert persisted_meme is not None
        assert persisted_meme.tags == ["reaction-tag", "new-tag"]


async def test_admin_seo_regenerate_uses_static_provider_and_clears_edited_at(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-regenerate-seo@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            tags=["regen tag"],
            ocr_text="Regenerate this meme text",
        )
        await _persist_canonical_meme(session, meme, meme_file)
        session.add(
            MemeSeoPage(
                meme=meme,
                slug="manual-regenerate",
                page_title="Manual title",
                meta_description="Manual meta",
                alt_text="Manual alt",
                caption="Manual caption",
                body_text="Manual body",
                tags=["manual"],
                model_id="admin-manual",
                prompt_version="admin-manual",
                edited_at=datetime.now(UTC) - timedelta(days=1),
            ),
        )
        await session.commit()
        meme_id = meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        wrong_confirmation_response = await admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": "wrong-id"},
        )
        regenerate_response = await admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/seo-page/regenerate",
            json={"confirmation": str(meme_id)},
        )

    assert wrong_confirmation_response.status_code == 409
    assert "confirmation" in wrong_confirmation_response.json()["detail"]
    assert regenerate_response.status_code == 200
    payload = regenerate_response.json()
    assert payload["slug"] == "regen-tag"
    assert payload["page_title"] == "Regen Tag meme"
    assert payload["model_id"] == "static-local"
    assert payload["prompt_version"] == "meme-seo-v1"
    assert payload["edited_at"] is None

    async with postgres_session_factory() as session:
        persisted_page = await session.get(MemeSeoPage, meme_id)
        persisted_meme = await session.get(Meme, meme_id)
        assert persisted_page is not None
        assert persisted_page.model_id == "static-local"
        assert persisted_page.edited_at is None
        assert persisted_page.tags == ["regen-tag"]
        assert persisted_meme is not None
        assert persisted_meme.tags == ["regen-tag"]


async def test_admin_source_channel_mark_dead_requires_exact_confirmation_without_mutation(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-source-confirmation@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-confirmation",
            username="source_confirmation",
            title="Source Confirmation",
        )
        session.add(channel)
        await session.commit()
        channel_id = channel.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        missing_body_response = await admin_client.post(f"/api/v1/admin/source-channels/{channel_id}/mark-dead")
        wrong_confirmation_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{channel_id}/mark-dead",
            json={"confirmation": "wrong-id"},
        )

    assert missing_body_response.status_code == 422
    assert wrong_confirmation_response.status_code == 409
    assert "confirmation" in wrong_confirmation_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_channel = await session.get(SourceChannel, channel_id)
        assert persisted_channel is not None
        assert persisted_channel.is_active is True
        assert persisted_channel.is_paused is False


async def test_admin_source_channel_health_and_mark_dead_conflicts(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_email = "admin-source-health@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        stale_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-health-stale",
            username="source_health_stale",
            title="Source Health Stale",
            last_read_post_id="42",
            last_fetched_at=datetime.now(UTC) - timedelta(days=2),
        )
        checkpoint_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-health-checkpoint",
            username="source_health_checkpoint",
            title="Source Health Checkpoint",
            last_read_post_id="43",
        )
        session.add_all([stale_channel, checkpoint_channel])
        await session.commit()
        admin_user_id = admin_user.id
        stale_channel_id = stale_channel.id
        checkpoint_channel_id = checkpoint_channel.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        list_response = await admin_client.get("/api/v1/admin/source-channels")
        mark_dead_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{stale_channel_id}/mark-dead",
            json={"confirmation": str(stale_channel_id)},
        )
        resume_dead_response = await admin_client.post(f"/api/v1/admin/source-channels/{stale_channel_id}/resume")
        mark_dead_again_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{stale_channel_id}/mark-dead",
            json={"confirmation": str(stale_channel_id)},
        )

    assert list_response.status_code == 200
    channels = {item["id"]: item for item in list_response.json()}
    assert channels[str(stale_channel_id)]["operational_status"] == "active"
    assert channels[str(stale_channel_id)]["freshness_status"] == "stale"
    assert channels[str(stale_channel_id)]["seconds_since_last_fetch"] >= 2 * 24 * 60 * 60 - 10
    assert channels[str(checkpoint_channel_id)]["freshness_status"] == "checkpoint_only"

    assert mark_dead_response.status_code == 200
    mark_dead_payload = mark_dead_response.json()
    assert mark_dead_payload["is_active"] is False
    assert mark_dead_payload["is_paused"] is True
    assert mark_dead_payload["operational_status"] == "inactive"
    assert resume_dead_response.status_code == 409
    assert "marked dead" in resume_dead_response.json()["detail"]
    assert mark_dead_again_response.status_code == 409
    assert "already marked dead" in mark_dead_again_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_stale_channel = await session.get(SourceChannel, stale_channel_id)
        audit_row = (
            await session.execute(
                select(TelegramAdminAuditLog).where(
                    TelegramAdminAuditLog.source_channel_id == stale_channel_id,
                    TelegramAdminAuditLog.action == "channel_mark_dead",
                ),
            )
        ).scalar_one()
        assert persisted_stale_channel is not None
        assert persisted_stale_channel.is_active is False
        assert persisted_stale_channel.is_paused is True
        assert audit_row.admin_user_id == admin_user_id
        assert audit_row.previous_values["is_active"] is True
        assert audit_row.new_values["is_active"] is False


async def test_admin_source_channel_projects_populated_and_empty_inventory_metrics(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-source-metrics@example.com",
        is_admin=True,
    )
    latest_post_at = datetime.now(UTC).replace(microsecond=0)

    async with postgres_session_factory() as session:
        populated_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-metrics-populated",
            title="Source Metrics Populated",
        )
        empty_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-metrics-empty",
            title="Source Metrics Empty",
        )
        unrelated_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source-metrics-unrelated",
            title="Source Metrics Unrelated",
        )
        session.add_all([populated_channel, empty_channel, unrelated_channel])
        await session.flush()

        first_meme, first_file = _canonical_meme(media_type=ContentKind.IMAGE)
        second_meme, second_file = _canonical_meme(media_type=ContentKind.IMAGE)
        await _persist_canonical_meme(session, first_meme, first_file)
        await _persist_canonical_meme(session, second_meme, second_file)
        alternate_first_file = MemeFile(
            id=uuid7(),
            meme_id=first_meme.id,
            status=ContentProcessingStatus.READY,
            s3_original_key=f"admin/{first_meme.id}/alternate.jpg",
            mime_type="image/jpeg",
            quality_score=0.8,
        )
        session.add(alternate_first_file)
        await session.flush()
        session.add_all(
            [
                SourceChannelPost(
                    source_channel_id=populated_channel.id,
                    post_id="1",
                    published_at=latest_post_at - timedelta(minutes=2),
                ),
                SourceChannelPost(
                    source_channel_id=populated_channel.id,
                    post_id="2",
                    published_at=latest_post_at,
                ),
                SourceChannelPost(
                    source_channel_id=populated_channel.id,
                    post_id="3",
                    published_at=latest_post_at - timedelta(minutes=1),
                ),
                SourceChannelPost(
                    source_channel_id=populated_channel.id,
                    post_id="4",
                    published_at=latest_post_at - timedelta(minutes=3),
                ),
                SourceChannelPost(
                    source_channel_id=unrelated_channel.id,
                    post_id="9",
                    published_at=latest_post_at + timedelta(minutes=1),
                ),
                MemeSource(
                    file_id=first_file.id,
                    platform=populated_channel.platform,
                    source_id=populated_channel.platform_id,
                    post_id="1",
                ),
                MemeSource(
                    file_id=first_file.id,
                    platform=populated_channel.platform,
                    source_id=populated_channel.platform_id,
                    post_id="2",
                ),
                MemeSource(
                    file_id=second_file.id,
                    platform=populated_channel.platform,
                    source_id=populated_channel.platform_id,
                    post_id="3",
                ),
                MemeSource(
                    file_id=alternate_first_file.id,
                    platform=populated_channel.platform,
                    source_id=populated_channel.platform_id,
                    post_id="4",
                ),
                MemeSource(
                    file_id=second_file.id,
                    platform=unrelated_channel.platform,
                    source_id=unrelated_channel.platform_id,
                    post_id="9",
                ),
            ],
        )
        await session.commit()
        populated_channel_id = populated_channel.id
        empty_channel_id = empty_channel.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        list_response = await admin_client.get("/api/v1/admin/source-channels")
        pause_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{populated_channel_id}/pause",
        )

    assert list_response.status_code == 200
    channels = {item["id"]: item for item in list_response.json()}
    populated_payload = channels[str(populated_channel_id)]
    empty_payload = channels[str(empty_channel_id)]
    assert datetime.fromisoformat(populated_payload["latest_post_at"]) == latest_post_at
    assert populated_payload["observed_post_count"] == 4
    assert populated_payload["meme_count"] == 2
    assert empty_payload["latest_post_at"] is None
    assert empty_payload["observed_post_count"] == 0
    assert empty_payload["meme_count"] == 0

    assert pause_response.status_code == 200
    paused_payload = pause_response.json()
    assert datetime.fromisoformat(paused_payload["latest_post_at"]) == latest_post_at
    assert paused_payload["observed_post_count"] == 4
    assert paused_payload["meme_count"] == 2


async def test_admin_create_source_channel_uses_telegram_session_id_and_rejects_unknown_target(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-source-create@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        telegram_session = TelegramSession(
            name="session-a",
            display_name="Session A",
            status=TelegramSessionStatus.ACTIVE,
        )
        session.add(telegram_session)
        await session.commit()
        telegram_session_id = telegram_session.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        created_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "admin-created-source",
                "title": "Admin Created Source",
                "telegram_session_id": str(telegram_session_id),
                "live_enabled": False,
                "engagement_enabled": False,
            },
        )
        unknown_session_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "admin-created-source-unknown",
                "title": "Unknown Session Source",
                "telegram_session_id": str(uuid7()),
            },
        )

    assert created_response.status_code == 201
    payload = created_response.json()
    assert payload["telegram_session_id"] == str(telegram_session_id)
    assert payload["telegram_session_name"] == "session-a"
    assert payload["is_orphaned"] is False
    assert payload["live_enabled"] is False
    assert payload["engagement_enabled"] is False
    assert unknown_session_response.status_code == 404
    assert "Telegram session" in unknown_session_response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted = await session.scalar(
            select(SourceChannel).where(SourceChannel.platform_id == "admin-created-source"),
        )
        assert persisted is not None
        assert persisted.telegram_session_id == telegram_session_id


async def test_admin_source_message_inventory_and_manual_backfill(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_email = "admin-source-history@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    now = datetime.now(UTC)
    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        telegram_session = TelegramSession(
            name="source-history-session",
            display_name="Source history session",
            encrypted_string_session="encrypted-source-history-session",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
            catchup_enabled=False,
        )
        session.add(telegram_session)
        await session.flush()
        channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="source_history_channel",
            username="source_history_channel",
            title="Source history channel",
            telegram_session_id=telegram_session.id,
            oldest_observed_post_id="99",
            history_cursor_post_id="100",
            initial_catchup_completed=True,
            last_read_post_id="103",
        )
        session.add(channel)
        await session.flush()

        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE)
        await _persist_canonical_meme(session, meme, meme_file)
        stage_meme, stage_meme_file = _canonical_meme(media_type=ContentKind.IMAGE)
        await _persist_canonical_meme(session, stage_meme, stage_meme_file)
        session.add_all(
            [
                SourceChannelPost(
                    source_channel_id=channel.id,
                    post_id="99",
                    published_at=now - timedelta(minutes=5),
                    media_type="photo",
                    status=SourceChannelPostStatus.ACCEPTED,
                    attempt_count=1,
                ),
                SourceChannelPost(
                    source_channel_id=channel.id,
                    post_id="100",
                    published_at=now - timedelta(minutes=4),
                    media_type="unsupported",
                    status=SourceChannelPostStatus.UNSUPPORTED,
                    attempt_count=1,
                ),
                SourceChannelPost(
                    source_channel_id=channel.id,
                    post_id="101",
                    published_at=now - timedelta(minutes=3),
                    media_type="photo",
                    status=SourceChannelPostStatus.ACCEPTED,
                    attempt_count=1,
                ),
                SourceChannelPost(
                    source_channel_id=channel.id,
                    post_id="102",
                    published_at=now - timedelta(minutes=2),
                    media_type="photo",
                    status=SourceChannelPostStatus.ACCEPTED,
                    attempt_count=1,
                ),
                SourceChannelPost(
                    source_channel_id=channel.id,
                    post_id="103",
                    published_at=now - timedelta(minutes=1),
                    media_type="photo",
                    status=SourceChannelPostStatus.FAILED,
                    last_error_code="download_unavailable",
                    last_error_text="provider disconnected",
                    attempt_count=1,
                    metadata_version=1,
                    metadata_first_observed_at=now - timedelta(minutes=1),
                    metadata_last_observed_at=now,
                    is_deleted=True,
                    deletion_observed_at=now,
                ),
                SourceChannelPost(
                    source_channel_id=channel.id,
                    post_id="104",
                    published_at=now,
                    media_type="photo",
                    status=SourceChannelPostStatus.ACCEPTED,
                    attempt_count=1,
                    first_observed_text="Original caption",
                    latest_text="x" * 600,
                    first_observed_text_entities=[],
                    latest_text_entities=[],
                    media_group_id="9007199254740993",
                    reply_to_post_id="103",
                    telegram_edited_at=now - timedelta(seconds=30),
                    metadata_first_observed_at=now - timedelta(minutes=1),
                    metadata_last_observed_at=now,
                    metadata_version=1,
                ),
                PipelineIngestRequest(
                    source_platform=SourcePlatform.TELEGRAM,
                    source_id=channel.platform_id,
                    post_id="99",
                    status=PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE,
                    source_attach_reason=SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE,
                ),
                PipelineIngestRequest(
                    source_platform=SourcePlatform.TELEGRAM,
                    source_id=channel.platform_id,
                    post_id="101",
                    status=PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
                ),
                PipelineIngestRequest(
                    source_platform=SourcePlatform.TELEGRAM,
                    source_id=channel.platform_id,
                    post_id="102",
                    status=PipelineIngestRequestStatus.MATERIALIZED,
                    materialized_meme_id=meme.id,
                    materialized_meme_file_id=meme_file.id,
                ),
                PipelineIngestRequest(
                    source_platform=SourcePlatform.TELEGRAM,
                    source_id=channel.platform_id,
                    post_id="104",
                    status=PipelineIngestRequestStatus.MATERIALIZED,
                    materialized_meme_id=stage_meme.id,
                    materialized_meme_file_id=stage_meme_file.id,
                ),
                MemeFileSyncTargetSnapshot(
                    meme_file_id=meme_file.id,
                    sync_target=SyncTargetKind.QDRANT,
                    status=SyncTargetStatus.SYNCED,
                ),
                MemeFileSyncTargetSnapshot(
                    meme_file_id=meme_file.id,
                    sync_target=SyncTargetKind.MEILISEARCH,
                    status=SyncTargetStatus.SYNCED,
                ),
                PipelineStageJournal(
                    meme_file_id=stage_meme_file.id,
                    stage=ContentPipelineStage.SYNC_QDRANT,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                ),
                PipelineStageJournal(
                    meme_file_id=stage_meme_file.id,
                    stage=ContentPipelineStage.SYNC_MEILI,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                ),
            ],
        )
        await session.commit()
        channel_id = channel.id
        admin_user_id = admin_user.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        page_response = await admin_client.get(
            f"/api/v1/admin/source-channels/{channel_id}/posts?limit=2&offset=0",
        )
        blocked_page_response = await admin_client.get(
            f"/api/v1/admin/source-channels/{channel_id}/posts?limit=1&offset=5",
        )
        snapshot_at = page_response.json()["snapshot_at"]
        async with postgres_session_factory() as session:
            session.add(
                SourceChannelPost(
                    source_channel_id=channel_id,
                    post_id="105",
                    published_at=datetime.now(UTC),
                    media_type="photo",
                    status=SourceChannelPostStatus.OBSERVED,
                    attempt_count=1,
                ),
            )
            await session.commit()
        stable_page_response = await admin_client.get(
            f"/api/v1/admin/source-channels/{channel_id}/posts",
            params={"limit": 2, "offset": 2, "snapshot_at": snapshot_at},
        )
        disabled_account_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{channel_id}/backfill",
            json={"message_limit": 16000},
        )
        async with postgres_session_factory() as session:
            assigned_session = await session.get(TelegramSession, telegram_session.id)
            assert assigned_session is not None
            assigned_session.catchup_enabled = True
            await session.commit()
        backfill_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{channel_id}/backfill",
            json={"message_limit": 16000},
        )
        duplicate_response = await admin_client.post(
            f"/api/v1/admin/source-channels/{channel_id}/backfill",
            json={"message_limit": 5000},
        )

    assert page_response.status_code == 200
    page = page_response.json()
    assert page["total"] == 6
    assert datetime.fromisoformat(page["snapshot_at"]) <= datetime.now(UTC)
    assert [item["post_id"] for item in page["items"]] == ["104", "103"]
    assert page["summary"] == {
        "observed_count": 6,
        "indexed_count": 2,
        "partially_indexed_count": 0,
        "processing_count": 1,
        "failed_count": 1,
        "not_indexable_count": 2,
        "metadata_captured_count": 2,
        "metadata_missing_count": 4,
    }
    assert page["items"][0]["index_status"] == "indexed"
    assert page["items"][0]["qdrant_status"] == "synced"
    assert page["items"][0]["meilisearch_status"] == "synced"
    assert page["items"][0]["metadata_state"] == "captured"
    assert page["items"][0]["text_excerpt"] == f"{'x' * 497}..."
    assert page["items"][0]["media_group_id"] == "9007199254740993"
    assert page["items"][0]["reply_to_post_id"] == "103"
    assert datetime.fromisoformat(page["items"][0]["telegram_edited_at"]) == now - timedelta(seconds=30)
    assert page["items"][1]["index_status"] == "failed"
    assert page["items"][1]["fetch_detail"] == "download_unavailable — provider disconnected"
    assert page["items"][1]["metadata_state"] == "captured"
    assert page["items"][1]["text_excerpt"] is None
    assert page["items"][1]["is_deleted"] is True
    assert datetime.fromisoformat(page["items"][1]["deletion_observed_at"]) == now
    assert stable_page_response.status_code == 200
    assert [item["post_id"] for item in stable_page_response.json()["items"]] == ["102", "101"]
    assert all(item["metadata_state"] == "missing" for item in stable_page_response.json()["items"])
    assert stable_page_response.json()["total"] == 6
    assert disabled_account_response.status_code == 409
    assert "assigned Telegram account" in disabled_account_response.json()["detail"]
    assert blocked_page_response.status_code == 200
    blocked_item = blocked_page_response.json()["items"][0]
    assert blocked_item["post_id"] == "99"
    assert blocked_item["ingest_outcome"] == "blocked_sha256_existing_file"
    assert blocked_item["index_status"] == "not_indexable"

    assert backfill_response.status_code == 202
    assert backfill_response.json()["backfill_status"] == "queued"
    assert backfill_response.json()["backfill_requested_count"] == 16000
    assert backfill_response.json()["backfill_scanned_count"] == 0
    assert duplicate_response.status_code == 409

    async with postgres_session_factory() as session:
        job = await session.scalar(
            select(SourceChannelBackfillJob).where(SourceChannelBackfillJob.source_channel_id == channel_id),
        )
        audit = await session.scalar(
            select(TelegramAdminAuditLog).where(
                TelegramAdminAuditLog.action == "channel_backfill_requested",
                TelegramAdminAuditLog.source_channel_id == channel_id,
            ),
        )
        assert job is not None
        assert job.status is SourceChannelBackfillJobStatus.QUEUED
        assert job.requested_message_count == 16000
        assert job.requested_by_admin_user_id == admin_user_id
        assert audit is not None
        assert audit.new_values["backfill_message_limit"] == 16000


async def test_admin_source_projection_loads_only_latest_backfill_job(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with postgres_session_factory() as session:
        channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="latest_backfill_projection",
            title="Latest backfill projection",
        )
        session.add(channel)
        await session.flush()
        session.add_all(
            [
                SourceChannelBackfillJob(
                    source_channel_id=channel.id,
                    status=SourceChannelBackfillJobStatus.COMPLETED,
                    requested_message_count=1000,
                    scanned_message_count=1000,
                    created_at=now - timedelta(minutes=2),
                    completed_at=now - timedelta(minutes=1),
                ),
                SourceChannelBackfillJob(
                    source_channel_id=channel.id,
                    status=SourceChannelBackfillJobStatus.FAILED,
                    requested_message_count=5000,
                    scanned_message_count=1250,
                    last_error_text="provider disconnected",
                    created_at=now - timedelta(minutes=1),
                    completed_at=now,
                ),
            ],
        )
        await session.commit()
        channel_id = channel.id

    async with postgres_session_factory() as session:
        service = admin_service_module.AdminService(session)
        rows = await service.list_source_channels(platform=SourcePlatform.TELEGRAM)
        source = next(row for row in rows if row.id == channel_id)
        tracked_channel = await session.get(SourceChannel, channel_id)

        assert source.backfill_status == "failed"
        assert source.backfill_requested_count == 5000
        assert source.backfill_scanned_count == 1250
        assert source.backfill_error == "provider disconnected"
        assert tracked_channel is not None
        assert "backfill_jobs" not in tracked_channel.__dict__


@pytest.mark.parametrize("platform", [SourcePlatform.REDDIT, SourcePlatform.VK])
async def test_browser_admin_source_creation_rejects_unsupported_platforms_in_route_and_service(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    platform: SourcePlatform,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=f"admin-{platform.value}-source-denied@example.com",
        is_admin=True,
    )
    request = AdminSourceChannelCreateRequest(
        platform=platform,
        platform_id=f"{platform.value}-source",
        title=f"{platform.value.title()} source",
        orphaned=True,
    )

    async with postgres_session_factory() as session:
        service = admin_service_module.AdminService(session)
        with pytest.raises(admin_service_module.AdminConflictError, match="Only Telegram sources"):
            await service.add_source_channel(request, admin_user_id=uuid7())

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": platform.value,
                "platform_id": request.platform_id,
                "title": request.title,
                "orphaned": True,
            },
        )

    assert response.status_code == 409
    assert "Only Telegram sources" in response.json()["detail"]
    async with postgres_session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(SourceChannel).where(SourceChannel.platform == platform),
        )
        assert count == 0


async def test_admin_manual_source_creation_normalizes_public_references_and_retains_exceptional_ids(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-manual-source-normalization@example.com",
        is_admin=True,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        public_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "https://telegram.me/Mixed_Public",
                "username": "ignored_name",
                "title": "Mixed public",
                "orphaned": True,
            },
        )
        duplicate_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "@mixed_public",
                "title": "Duplicate mixed public",
                "orphaned": True,
            },
        )
        public_username_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "@Username_Public",
                "title": "Public username source",
                "orphaned": True,
            },
        )
        exceptional_username_conflict = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "-100123450001",
                "username": "@Username_Public",
                "title": "Conflicting exceptional source",
                "orphaned": True,
            },
        )
        exceptional_public_username_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "-100123450002",
                "username": "@Exceptional_Public",
                "title": "Exceptional source with public username",
                "orphaned": True,
            },
        )
        exceptional_response = await admin_client.post(
            "/api/v1/admin/source-channels",
            json={
                "platform": "telegram",
                "platform_id": "-100987654321",
                "username": "private-source-hint",
                "title": "Exceptional private source",
                "orphaned": True,
            },
        )
        concurrent_responses = await asyncio.gather(
            admin_client.post(
                "/api/v1/admin/source-channels",
                json={
                    "platform": "telegram",
                    "platform_id": "@Concurrent_Public",
                    "title": "Concurrent public one",
                    "orphaned": True,
                },
            ),
            admin_client.post(
                "/api/v1/admin/source-channels",
                json={
                    "platform": "telegram",
                    "platform_id": "https://t.me/concurrent_public",
                    "title": "Concurrent public two",
                    "orphaned": True,
                },
            ),
        )

    assert public_response.status_code == 201
    assert public_response.json()["platform_id"] == "mixed_public"
    assert public_response.json()["username"] == "mixed_public"
    assert duplicate_response.status_code == 409
    assert public_username_response.status_code == 201
    assert public_username_response.json()["platform_id"] == "username_public"
    assert public_username_response.json()["username"] == "username_public"
    assert exceptional_username_conflict.status_code == 409
    assert "already exists" in exceptional_username_conflict.json()["detail"]
    assert exceptional_public_username_response.status_code == 201
    assert exceptional_public_username_response.json()["platform_id"] == "-100123450002"
    assert exceptional_public_username_response.json()["username"] == "exceptional_public"
    assert exceptional_response.status_code == 201
    assert exceptional_response.json()["platform_id"] == "-100987654321"
    assert exceptional_response.json()["username"] == "private-source-hint"
    assert sorted(response.status_code for response in concurrent_responses) == [201, 409]
    async with postgres_session_factory() as session:
        concurrent_count = await session.scalar(
            select(func.count()).select_from(SourceChannel).where(
                SourceChannel.platform_id == "concurrent_public",
            ),
        )
        assert concurrent_count == 1


async def test_admin_adds_public_telegram_reference_atomically_and_retry_converges(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_email = "admin-reference-source@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    raw_string_sessions = (
        "reference-source-server-secret-primary",
        "reference-source-server-secret-secondary",
    )
    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        accounts = [
            TelegramSession(
                name=f"reference-source-account-{index}",
                display_name=f"Reference source account {index}",
                encrypted_string_session=admin_service_module.AdminService(session)._encrypt_string_session(
                    SecretStr(raw_string_session),
                ),
                status=TelegramSessionStatus.ACTIVE,
                enabled=True,
            )
            for index, raw_string_session in enumerate(raw_string_sessions, start=1)
        ]
        suggestion = ChannelSuggestion(
            user_id=admin_user.id,
            platform=SourcePlatform.TELEGRAM,
            channel_url="https://t.me/public_channel",
        )
        session.add_all([*accounts, suggestion])
        await session.commit()
        account_ids = [account.id for account in accounts]
        suggestion_id = suggestion.id

    resolver_calls: list[tuple[str, str]] = []
    resolver_pair_ready = asyncio.Event()

    async def fake_resolve_admin_telegram_channel(
        *, settings: object, string_session: SecretStr, reference: str
    ) -> ResolvedAdminTelegramChannel:
        _ = settings
        resolver_calls.append((string_session.get_secret_value(), reference))
        if len(resolver_calls) <= 2:
            if len(resolver_calls) == 2:
                resolver_pair_ready.set()
            await asyncio.wait_for(resolver_pair_ready.wait(), timeout=2)
        return ResolvedAdminTelegramChannel(
            platform_id="public_channel",
            username="public_channel",
            title="Public channel",
            subscriber_count=1234,
        )

    monkeypatch.setattr(
        admin_service_module,
        "resolve_admin_telegram_channel",
        fake_resolve_admin_telegram_channel,
    )
    original_source_read = admin_service_module.AdminService._source_channel_read

    def oversized_internal_source_projection(
        channel: SourceChannel,
        *,
        aggregate: admin_service_module._SourceChannelAggregate,
        latest_backfill_job: SourceChannelBackfillJob | None = None,
        now: datetime | None = None,
    ) -> object:
        projection = original_source_read(
            channel,
            aggregate=aggregate,
            latest_backfill_job=latest_backfill_job,
            now=now,
        )
        return SimpleNamespace(
            **projection.model_dump(),
            encrypted_string_session="must-be-filtered-encrypted-material",
            phone="+15551234567",
            telegram_password="must-be-filtered-password",
        )

    monkeypatch.setattr(
        admin_service_module.AdminService,
        "_source_channel_read",
        staticmethod(oversized_internal_source_projection),
    )
    request_bodies = [
        {
            "reference": "https://telegram.me/Public_Channel",
            "telegram_session_id": str(account_id),
            "suggestion_id": str(suggestion_id),
        }
        for account_id in account_ids
    ]
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        first_response, duplicate_race_response = await asyncio.gather(
            admin_client.post(
                "/api/v1/admin/telegram/channels/from-reference",
                json=request_bodies[0],
            ),
            admin_client.post(
                "/api/v1/admin/telegram/channels/from-reference",
                json=request_bodies[1],
            ),
        )
        retry_response = await admin_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json=request_bodies[0],
        )

    assert first_response.status_code == 201
    assert duplicate_race_response.status_code == 201
    assert retry_response.status_code == 201
    first_payload = first_response.json()
    retry_payload = retry_response.json()
    assert retry_payload["id"] == first_payload["id"]
    assert duplicate_race_response.json()["id"] == first_payload["id"]
    assert first_payload == {
        **first_payload,
        "platform": "telegram",
        "platform_id": "public_channel",
        "username": "public_channel",
        "title": "Public channel",
        "subscriber_count": 1234,
        "catchup_message_limit": 5000,
        "catchup_enabled": True,
        "live_enabled": True,
        "engagement_enabled": True,
    }
    assert all(raw_string_session not in first_response.text for raw_string_session in raw_string_sessions)
    assert "encrypted_string_session" not in first_response.text
    assert "+15551234567" not in first_response.text
    assert "must-be-filtered-password" not in first_response.text
    assert len(resolver_calls) == 3
    assert {call[0] for call in resolver_calls[:2]} == set(raw_string_sessions)
    assert {call[1] for call in resolver_calls} == {"public_channel"}

    async with postgres_session_factory() as session:
        source_count = await session.scalar(
            select(func.count()).select_from(SourceChannel).where(SourceChannel.platform_id == "public_channel"),
        )
        persisted_suggestion = await session.get(ChannelSuggestion, suggestion_id)
        audience_snapshot = await session.scalar(
            select(SourceChannelAudienceSnapshot).where(
                SourceChannelAudienceSnapshot.source_channel_id == UUID(first_payload["id"]),
                SourceChannelAudienceSnapshot.capture_reason
                == SourceChannelAudienceCaptureReason.INITIAL_RESOLUTION,
            )
        )
        persisted_source = await session.get(SourceChannel, UUID(first_payload["id"]))
        creation_audit = await session.scalar(
            select(TelegramAdminAuditLog).where(
                TelegramAdminAuditLog.action == "channel_create_from_reference",
                TelegramAdminAuditLog.source_channel_id == UUID(first_payload["id"]),
            ),
        )
        assert source_count == 1
        assert first_payload["telegram_session_id"] in {str(account_id) for account_id in account_ids}
        assert persisted_suggestion is not None
        assert persisted_suggestion.status is ChannelSuggestionStatus.APPROVED
        assert persisted_suggestion.reviewed_at is not None
        assert creation_audit is not None
        assert creation_audit.new_values["suggestion_id"] == str(suggestion_id)
        assert creation_audit.new_values["suggestion_status"] == "approved"
        assert audience_snapshot is not None
        assert audience_snapshot.fetch_status is SourceChannelAudienceFetchStatus.SUCCESS
        assert audience_snapshot.subscriber_count == 1234
        assert persisted_source is not None
        assert persisted_source.subscriber_count_updated_at is not None
        assert persisted_source.next_audience_capture_at is not None


async def test_admin_reference_source_reactivates_exact_canonical_source_with_safe_defaults(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_email = "admin-reference-reactivate@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        account = TelegramSession(
            name="reactivate-reference-account",
            display_name="Reactivate reference account",
            encrypted_string_session=admin_service_module.AdminService(session)._encrypt_string_session(
                SecretStr("reactivate-reference-secret"),
            ),
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        old_account = TelegramSession(
            name="reactivate-old-account",
            display_name="Reactivate old account",
            status=TelegramSessionStatus.STOPPED,
            enabled=False,
        )
        canonical_source = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="reactivate_public",
            username="reactivate_public",
            title="Old reactivate public",
            telegram_session=old_account,
            catchup_enabled=False,
            live_enabled=False,
            engagement_enabled=False,
            catchup_message_limit=12,
        )
        suggestion = ChannelSuggestion(
            user_id=admin_user.id,
            platform=SourcePlatform.TELEGRAM,
            channel_url="https://t.me/reactivate_public",
        )
        session.add_all([account, old_account, canonical_source, suggestion])
        await session.commit()
        account_id = account.id
        old_account_id = old_account.id
        canonical_source_id = canonical_source.id
        suggestion_id = suggestion.id

    async def fake_resolve(*, reference: str, **_kwargs) -> ResolvedAdminTelegramChannel:
        return ResolvedAdminTelegramChannel(
            platform_id=reference,
            username=reference,
            title=f"Updated {reference.replace('_', ' ')}",
            subscriber_count=900,
        )

    monkeypatch.setattr(admin_service_module, "resolve_admin_telegram_channel", fake_resolve)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json={
                "reference": "@reactivate_public",
                "telegram_session_id": str(account_id),
                "suggestion_id": str(suggestion_id),
                "catchup_message_limit": 321,
            },
        )

    assert response.status_code == 201
    assert response.json()["id"] == str(canonical_source_id)
    assert response.json()["platform_id"] == "reactivate_public"
    assert response.json()["username"] == "reactivate_public"
    assert response.json()["title"] == "Updated reactivate public"
    assert response.json()["subscriber_count"] == 900
    assert response.json()["telegram_session_id"] == str(account_id)
    assert response.json()["catchup_message_limit"] == 321
    assert response.json()["catchup_enabled"] is True
    assert response.json()["live_enabled"] is True
    assert response.json()["engagement_enabled"] is True

    async with postgres_session_factory() as session:
        persisted_source = await session.get(SourceChannel, canonical_source_id)
        persisted_suggestion = await session.get(ChannelSuggestion, suggestion_id)
        reuse_audit = await session.scalar(
            select(TelegramAdminAuditLog).where(
                TelegramAdminAuditLog.action == "channel_reuse_from_reference",
                TelegramAdminAuditLog.source_channel_id == canonical_source_id,
            ),
        )
        assert persisted_source is not None
        assert persisted_source.platform_id == "reactivate_public"
        assert persisted_suggestion is not None
        assert persisted_suggestion.status is ChannelSuggestionStatus.APPROVED
        assert reuse_audit is not None
        assert reuse_audit.previous_values["platform_id"] == "reactivate_public"
        assert reuse_audit.new_values["platform_id"] == "reactivate_public"
        assert reuse_audit.previous_values["telegram_session_id"] == str(old_account_id)
        assert reuse_audit.new_values["telegram_session_id"] == str(account_id)
        assert reuse_audit.previous_values["catchup_enabled"] is False
        assert reuse_audit.new_values["catchup_enabled"] is True
        assert reuse_audit.previous_values["live_enabled"] is False
        assert reuse_audit.new_values["live_enabled"] is True
        assert reuse_audit.previous_values["engagement_enabled"] is False
        assert reuse_audit.new_values["engagement_enabled"] is True
        assert reuse_audit.new_values["catchup_message_limit"] == 321
        assert reuse_audit.new_values["username"] == "reactivate_public"
        assert reuse_audit.new_values["title"] == "Updated reactivate public"
        assert reuse_audit.new_values["subscriber_count"] == 900


async def test_admin_reference_source_rejects_paused_dead_and_noncanonical_username_conflicts(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_email = "admin-reference-existing-conflicts@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        account = TelegramSession(
            name="existing-conflict-reference-account",
            display_name="Existing conflict reference account",
            encrypted_string_session=admin_service_module.AdminService(session)._encrypt_string_session(
                SecretStr("existing-conflict-reference-secret"),
            ),
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        paused_source = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="paused_public",
            username="paused_public",
            title="Paused public",
            is_paused=True,
        )
        dead_source = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="dead_public",
            username="dead_public",
            title="Dead public",
            is_active=False,
            is_paused=True,
        )
        noncanonical_source = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="-1007003",
            username="duplicate_public",
            title="Non-canonical duplicate public",
        )
        suggestions = {
            username: ChannelSuggestion(
                user_id=admin_user.id,
                platform=SourcePlatform.TELEGRAM,
                channel_url=f"https://t.me/{username}",
            )
            for username in ("paused_public", "dead_public", "duplicate_public")
        }
        session.add_all([account, paused_source, dead_source, noncanonical_source, *suggestions.values()])
        await session.commit()
        account_id = account.id
        suggestion_ids = {username: suggestion.id for username, suggestion in suggestions.items()}

    async def fake_resolve(*, reference: str, **_kwargs) -> ResolvedAdminTelegramChannel:
        return ResolvedAdminTelegramChannel(
            platform_id=reference,
            username=reference,
            title=f"Resolved {reference}",
            subscriber_count=100,
        )

    monkeypatch.setattr(admin_service_module, "resolve_admin_telegram_channel", fake_resolve)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        responses = {
            username: await admin_client.post(
                "/api/v1/admin/telegram/channels/from-reference",
                json={
                    "reference": f"@{username}",
                    "telegram_session_id": str(account_id),
                    "suggestion_id": str(suggestion_ids[username]),
                },
            )
            for username in suggestion_ids
        }

    assert responses["paused_public"].status_code == 409
    assert "paused" in responses["paused_public"].json()["detail"].lower()
    assert responses["dead_public"].status_code == 409
    assert "removed" in responses["dead_public"].json()["detail"].lower()
    assert responses["duplicate_public"].status_code == 409
    assert "non-canonical" in responses["duplicate_public"].json()["detail"].lower()
    assert "remove and recreate" in responses["duplicate_public"].json()["detail"].lower()

    async with postgres_session_factory() as session:
        persisted_suggestions = [
            await session.get(ChannelSuggestion, suggestion_id)
            for suggestion_id in suggestion_ids.values()
        ]
        persisted_paused = await session.get(SourceChannel, paused_source.id)
        persisted_dead = await session.get(SourceChannel, dead_source.id)
        assert all(suggestion is not None for suggestion in persisted_suggestions)
        assert all(
            suggestion.status is ChannelSuggestionStatus.PENDING
            for suggestion in persisted_suggestions
            if suggestion
        )
        assert persisted_paused is not None
        assert persisted_paused.is_paused is True
        assert persisted_paused.telegram_session_id is None
        assert persisted_dead is not None
        assert persisted_dead.is_active is False
        assert persisted_dead.telegram_session_id is None


async def test_admin_reference_source_rejects_unavailable_account_and_mismatched_suggestion_without_io(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_email = "admin-reference-validation@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        encrypted_session = admin_service_module.AdminService(session)._encrypt_string_session(
            SecretStr("unavailable-account-secret"),
        )
        unavailable_accounts = [
            TelegramSession(
                name="missing-secret-reference-account",
                display_name="Missing secret reference account",
                encrypted_string_session=None,
                status=TelegramSessionStatus.ACTIVE,
                enabled=True,
            ),
            TelegramSession(
                name="disabled-reference-account",
                display_name="Disabled reference account",
                encrypted_string_session=encrypted_session,
                status=TelegramSessionStatus.ACTIVE,
                enabled=False,
            ),
            TelegramSession(
                name="auth-required-reference-account",
                display_name="Auth required reference account",
                encrypted_string_session=encrypted_session,
                status=TelegramSessionStatus.AUTH_REQUIRED,
                enabled=True,
            ),
            TelegramSession(
                name="quarantined-reference-account",
                display_name="Quarantined reference account",
                encrypted_string_session=encrypted_session,
                status=TelegramSessionStatus.ACTIVE,
                enabled=True,
                quarantined_at=datetime.now(UTC),
            ),
            TelegramSession(
                name="flood-wait-reference-account",
                display_name="Flood wait reference account",
                encrypted_string_session=encrypted_session,
                status=TelegramSessionStatus.ACTIVE,
                enabled=True,
                flood_wait_until=datetime.now(UTC) + timedelta(minutes=5),
            ),
        ]
        ready_account = TelegramSession(
            name="mismatch-reference-account",
            display_name="Mismatch reference account",
            encrypted_string_session=admin_service_module.AdminService(session)._encrypt_string_session(
                SecretStr("mismatch-secret"),
            ),
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        suggestion = ChannelSuggestion(
            user_id=admin_user.id,
            platform=SourcePlatform.TELEGRAM,
            channel_url="https://t.me/different_channel",
        )
        session.add_all([*unavailable_accounts, ready_account, suggestion])
        await session.commit()
        unavailable_account_ids = [account.id for account in unavailable_accounts]
        ready_account_id = ready_account.id
        suggestion_id = suggestion.id

    resolver = monkeypatch.setattr(
        admin_service_module,
        "resolve_admin_telegram_channel",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Telegram I/O must not run")),
    )
    _ = resolver
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        unavailable_responses = [
            await admin_client.post(
                "/api/v1/admin/telegram/channels/from-reference",
                json={"reference": "@public_channel", "telegram_session_id": str(account_id)},
            )
            for account_id in unavailable_account_ids
        ]
        invalid_reference_responses = [
            await admin_client.post(
                "/api/v1/admin/telegram/channels/from-reference",
                json={"reference": reference, "telegram_session_id": str(ready_account_id)},
            )
            for reference in ("https://t.me/+private-invite", "https://example.com/not-telegram")
        ]
        mismatch_response = await admin_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json={
                "reference": "@public_channel",
                "telegram_session_id": str(ready_account_id),
                "suggestion_id": str(suggestion_id),
            },
        )

    assert all(response.status_code == 409 for response in unavailable_responses)
    assert all(
        response.json()["detail"].startswith("The selected Telegram account is not ready")
        for response in unavailable_responses
    )
    assert all(response.status_code == 409 for response in invalid_reference_responses)
    assert all("public" in response.json()["detail"].lower() for response in invalid_reference_responses)
    assert mismatch_response.status_code == 409
    assert mismatch_response.json()["detail"] == "The channel reference does not match the selected source suggestion."
    async with postgres_session_factory() as session:
        persisted_suggestion = await session.get(ChannelSuggestion, suggestion_id)
        assert persisted_suggestion is not None
        assert persisted_suggestion.status is ChannelSuggestionStatus.PENDING


async def test_admin_reference_source_rolls_back_suggestion_and_source_on_persistence_failure(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_email = "admin-reference-rollback@example.com"
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=admin_email,
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        admin_user = (await session.execute(select(User).where(User.email == admin_email))).scalar_one()
        account = TelegramSession(
            name="rollback-reference-account",
            display_name="Rollback reference account",
            encrypted_string_session=admin_service_module.AdminService(session)._encrypt_string_session(
                SecretStr("rollback-secret"),
            ),
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        suggestion = ChannelSuggestion(
            user_id=admin_user.id,
            platform=SourcePlatform.TELEGRAM,
            channel_url="https://t.me/rollback_channel",
        )
        session.add_all([account, suggestion])
        await session.commit()
        account_id = account.id
        suggestion_id = suggestion.id

    async def fake_resolve(**_kwargs) -> ResolvedAdminTelegramChannel:
        return ResolvedAdminTelegramChannel(
            platform_id="rollback_channel",
            username="rollback_channel",
            title="Rollback channel",
            subscriber_count=None,
        )

    original_insert = admin_service_module.AdminService._add_source_channel_no_commit

    async def fail_after_insert(self, *args, **kwargs):
        _ = await original_insert(self, *args, **kwargs)
        raise admin_service_module.AdminConflictError("Simulated persistence failure.")

    monkeypatch.setattr(admin_service_module, "resolve_admin_telegram_channel", fake_resolve)
    monkeypatch.setattr(admin_service_module.AdminService, "_add_source_channel_no_commit", fail_after_insert)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json={
                "reference": "@rollback_channel",
                "telegram_session_id": str(account_id),
                "suggestion_id": str(suggestion_id),
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Simulated persistence failure."
    async with postgres_session_factory() as session:
        source_count = await session.scalar(
            select(func.count()).select_from(SourceChannel).where(SourceChannel.platform_id == "rollback_channel"),
        )
        persisted_suggestion = await session.get(ChannelSuggestion, suggestion_id)
        assert source_count == 0
        assert persisted_suggestion is not None
        assert persisted_suggestion.status is ChannelSuggestionStatus.PENDING


async def test_admin_reference_source_translates_resolver_failure_safely(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-reference-provider-error@example.com",
        is_admin=True,
    )
    async with postgres_session_factory() as session:
        account = TelegramSession(
            name="provider-error-reference-account",
            display_name="Provider error reference account",
            encrypted_string_session=admin_service_module.AdminService(session)._encrypt_string_session(
                SecretStr("provider-error-secret"),
            ),
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
        )
        session.add(account)
        await session.commit()
        account_id = account.id

    async def fail_resolver(**_kwargs):
        raise AdminTelegramChannelResolverError("Telegram did not respond in time. Try again.")

    monkeypatch.setattr(admin_service_module, "resolve_admin_telegram_channel", fail_resolver)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            "/api/v1/admin/telegram/channels/from-reference",
            json={"reference": "@public_channel", "telegram_session_id": str(account_id)},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Telegram did not respond in time. Try again."
    assert "provider-error-secret" not in response.text


async def test_admin_telegram_session_lifecycle_validates_without_leaking_string_session(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-session@example.com",
        is_admin=True,
    )
    raw_string_session = "authorized-telegram-login-session"
    full_phone_number = "+10000007000"
    checked_channel_references: list[str | None] = []
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient()
        fake_clients.append(client)
        return client

    async def fake_validate_admin_telegram_string_session(
        *,
        settings: object,
        string_session: SecretStr,
        channel_reference: str | None = None,
    ) -> admin_service_module.AdminTelegramValidationResult:
        _ = settings
        assert hasattr(string_session, "get_secret_value")
        assert string_session.get_secret_value() == raw_string_session
        checked_channel_references.append(channel_reference)
        return admin_service_module.AdminTelegramValidationResult(
            account=admin_service_module.AdminTelegramAccountProjection(
                user_id=777000,
                username="validated_admin_session",
                phone_hint="ending-7000",
            ),
            channel_reference=channel_reference,
        )

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    monkeypatch.setattr(
        admin_service_module,
        "validate_admin_telegram_string_session",
        fake_validate_admin_telegram_string_session,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={
                "max_requests_per_second": 2.5,
            },
        )
        create_auth_required_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={},
        )
        session_id = create_response.json()["id"]
        phone_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/phone",
            json={"telegram_session_id": session_id, "phone_number": full_phone_number},
        )
        phone_code_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{phone_start_response.json()['attempt_id']}/phone/code",
            json={"code": "12345", "note": "login by phone"},
        )
        patch_response = await admin_client.patch(
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={
                "status": "quarantined",
                "enabled": False,
                "last_error_class": "ManualPark",
                "last_error_text": "Parked by test admin",
                "note": "park for maintenance",
            },
        )
        clear_response = await admin_client.patch(
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={"status": "active", "enabled": True, "clear_error": True},
        )
        channel_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={
                "platform": "telegram",
                "platform_id": "validated-channel-id",
                "username": "validated_channel",
                "title": "Validated Channel",
                "telegram_session_id": session_id,
            },
        )
        validate_response = await admin_client.post(
            f"/api/v1/admin/telegram/sessions/{session_id}/validate",
            json={"source_channel_id": channel_response.json()["id"]},
        )
        list_response = await admin_client.get("/api/v1/admin/telegram/sessions")

    assert create_response.status_code == 201
    create_payload = create_response.json()
    assert create_payload["status"] == "auth_required"
    assert create_payload["name"].startswith("pending_telegram_")
    assert create_payload["display_name"] == "Pending Telegram login"
    assert create_payload["has_string_session"] is False
    assert create_payload["account_user_id"] is None
    assert create_payload["owned_channel_count"] == 0
    assert raw_string_session not in create_response.text
    assert full_phone_number not in create_response.text
    assert "encrypted_string_session" not in create_response.text

    assert create_auth_required_response.status_code == 201
    auth_required_payload = create_auth_required_response.json()
    assert auth_required_payload["status"] == "auth_required"
    assert auth_required_payload["name"].startswith("pending_telegram_")
    assert auth_required_payload["has_string_session"] is False

    assert phone_start_response.status_code == 200
    phone_start_payload = phone_start_response.json()
    assert phone_start_payload["phone_number_hint"] == "ending-7000"
    assert full_phone_number not in phone_start_response.text
    assert raw_string_session not in phone_start_response.text

    assert phone_code_response.status_code == 200
    phone_code_payload = phone_code_response.json()
    assert phone_code_payload["password_required"] is False
    assert phone_code_payload["telegram_session"]["status"] == "active"
    assert phone_code_payload["telegram_session"]["name"] == "telegram_777000"
    assert phone_code_payload["telegram_session"]["display_name"] == "Validated Admin"
    assert phone_code_payload["telegram_session"]["has_string_session"] is True
    assert phone_code_payload["telegram_session"]["account_user_id"] == 777000
    assert phone_code_payload["telegram_session"]["account_username"] == "validated_admin_session"
    assert phone_code_payload["telegram_session"]["account_phone_hint"] == "ending-7000"
    assert raw_string_session not in phone_code_response.text
    assert full_phone_number not in phone_code_response.text
    assert fake_clients[0].sign_in_calls == [
        {"code": "12345", "phone": full_phone_number, "phone_code_hash": "fake-phone-code-hash"},
    ]
    assert fake_clients[0].logged_out is False

    assert patch_response.status_code == 200
    patch_payload = patch_response.json()
    assert patch_payload["status"] == "quarantined"
    assert patch_payload["enabled"] is False
    assert patch_payload["last_error_class"] == "ManualPark"
    assert patch_payload["quarantined_at"] is not None
    assert raw_string_session not in patch_response.text

    assert clear_response.status_code == 200
    assert clear_response.json()["status"] == "active"
    assert clear_response.json()["last_error_class"] is None
    assert clear_response.json()["last_error_text"] is None

    assert channel_response.status_code == 201
    assert channel_response.json()["is_indexable"] is True
    assert validate_response.status_code == 200
    validate_payload = validate_response.json()
    assert validate_payload["channel_checked"] is True
    assert validate_payload["channel_reference"] == "@validated_channel"
    assert validate_payload["telegram_session"]["account_user_id"] == 777000
    assert validate_payload["telegram_session"]["account_username"] == "validated_admin_session"
    assert raw_string_session not in validate_response.text
    assert checked_channel_references == ["@validated_channel"]

    assert list_response.status_code == 200
    list_payload = {item["id"]: item for item in list_response.json()}
    assert list_payload[session_id]["name"] == "telegram_777000"
    assert list_payload[session_id]["display_name"] == "Validated Admin"
    assert list_payload[session_id]["owned_channel_count"] == 1
    assert list_payload[session_id]["has_string_session"] is True
    assert list_payload[auth_required_payload["id"]]["has_string_session"] is False
    assert raw_string_session not in list_response.text
    assert full_phone_number not in list_response.text
    assert "encrypted_string_session" not in list_response.text

    async with postgres_session_factory() as session:
        persisted = await session.get(TelegramSession, UUID(session_id))
        login_attempt = await session.scalar(
            select(TelegramSessionLoginAttempt).where(
                TelegramSessionLoginAttempt.id == UUID(phone_start_payload["attempt_id"]),
            ),
        )
        audit_rows = (
            (
                await session.execute(
                    select(TelegramAdminAuditLog).order_by(
                        TelegramAdminAuditLog.created_at.asc(),
                        TelegramAdminAuditLog.id.asc(),
                    ),
                )
            )
            .scalars()
            .all()
        )

    assert persisted is not None
    assert persisted.encrypted_string_session is not None
    assert persisted.encrypted_string_session != raw_string_session
    assert persisted.name == "telegram_777000"
    assert persisted.display_name == "Validated Admin"
    assert persisted.account_user_id == 777000
    assert persisted.account_username == "validated_admin_session"
    assert login_attempt is not None
    assert login_attempt.status == "completed"
    assert login_attempt.cleanup_status == "promoted"
    assert login_attempt.encrypted_temp_string_session is None
    assert login_attempt.phone_number_hint == "ending-7000"
    assert login_attempt.phone_code_hash is None
    assert login_attempt.qr_url is None
    assert [row.action for row in audit_rows if row.telegram_session_id == persisted.id] == [
        "session_create",
        "session_login",
        "session_patch",
        "session_patch",
        "channel_create",
    ]
    for row in audit_rows:
        audit_text = f"{row.previous_values} {row.new_values}"
        assert raw_string_session not in audit_text
        assert full_phone_number not in audit_text
        assert "encrypted_string_session" not in audit_text


async def test_admin_telegram_phone_login_supports_2fa_password_without_leaking_secret(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-2fa@example.com",
        is_admin=True,
    )
    full_phone_number = "+10000007111"
    password = "very secret telegram password"
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(require_password=True)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        phone_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/phone",
            json={"phone_number": full_phone_number},
        )
        attempt_id = phone_start_response.json()["attempt_id"]
        phone_code_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/phone/code",
            json={"code": "24680"},
        )
        password_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/password",
            json={"password": password},
        )

    assert phone_start_response.status_code == 200
    assert phone_code_response.status_code == 200
    code_payload = phone_code_response.json()
    assert code_payload["password_required"] is True
    assert code_payload["telegram_session"] is None
    assert password not in phone_code_response.text
    assert full_phone_number not in phone_code_response.text

    assert password_response.status_code == 200
    password_payload = password_response.json()
    assert password_payload["password_required"] is False
    assert password_payload["telegram_session"]["status"] == "active"
    assert password_payload["telegram_session"]["name"] == "telegram_777000"
    assert password_payload["telegram_session"]["display_name"] == "Validated Admin"
    assert password_payload["telegram_session"]["has_string_session"] is True
    assert password_payload["telegram_session"]["account_phone_hint"] == "ending-7000"
    session_id = password_payload["telegram_session"]["id"]
    assert password not in password_response.text
    assert full_phone_number not in password_response.text
    assert fake_clients[0].sign_in_calls == [
        {"code": "24680", "phone": full_phone_number, "phone_code_hash": "fake-phone-code-hash"},
        {"password": password},
    ]
    assert fake_clients[0].logged_out is False

    async with postgres_session_factory() as session:
        persisted = await session.get(TelegramSession, UUID(session_id))
        login_attempt = await session.scalar(
            select(TelegramSessionLoginAttempt).where(
                TelegramSessionLoginAttempt.id == UUID(attempt_id),
            ),
        )
        audit_rows = (
            (
                await session.execute(
                    select(TelegramAdminAuditLog).where(TelegramAdminAuditLog.telegram_session_id == UUID(session_id)),
                )
            )
            .scalars()
            .all()
        )

    assert persisted is not None
    assert persisted.encrypted_string_session is not None
    assert persisted.encrypted_string_session != "authorized-telegram-login-session"
    assert persisted.name == "telegram_777000"
    assert persisted.display_name == "Validated Admin"
    assert login_attempt is not None
    assert login_attempt.status == "completed"
    assert login_attempt.cleanup_status == "promoted"
    assert login_attempt.encrypted_temp_string_session is None
    assert login_attempt.phone_code_hash is None
    for row in audit_rows:
        audit_text = f"{row.previous_values} {row.new_values} {row.note}"
        assert password not in audit_text
        assert full_phone_number not in audit_text
        assert "encrypted_string_session" not in audit_text


async def test_admin_telegram_phone_code_retry_keeps_attempt_live_until_a_later_success(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-code-retry@example.com",
        is_admin=True,
    )
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(invalid_code_attempts=1)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        phone_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/phone",
            json={"telegram_session_id": session_id, "phone_number": "+10000007333"},
        )
        attempt_id = phone_start_response.json()["attempt_id"]
        invalid_code_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/phone/code",
            json={"code": "00000"},
        )
        assert fake_clients[0].disconnected is False
        successful_code_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/phone/code",
            json={"code": "12345"},
        )

    assert invalid_code_response.status_code == 409
    assert invalid_code_response.json()["detail"] == "The Telegram code was incorrect. Try again."
    assert "00000" not in invalid_code_response.text
    assert successful_code_response.status_code == 200
    assert successful_code_response.json()["telegram_session"]["status"] == "active"
    assert fake_clients[0].sign_in_calls == [
        {"code": "00000", "phone": "+10000007333", "phone_code_hash": "fake-phone-code-hash"},
        {"code": "12345", "phone": "+10000007333", "phone_code_hash": "fake-phone-code-hash"},
    ]

    async with postgres_session_factory() as session:
        attempt = await session.get(TelegramSessionLoginAttempt, UUID(attempt_id))
    assert attempt is not None
    assert attempt.status == "completed"


async def test_admin_telegram_password_retry_keeps_2fa_attempt_live_until_a_later_success(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-password-retry@example.com",
        is_admin=True,
    )
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(require_password=True, invalid_password_attempts=1)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        phone_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/phone",
            json={"telegram_session_id": session_id, "phone_number": "+10000007444"},
        )
        attempt_id = phone_start_response.json()["attempt_id"]
        password_required_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/phone/code",
            json={"code": "24680"},
        )
        invalid_password_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/password",
            json={"password": "wrong-password"},
        )
        assert fake_clients[0].disconnected is False
        successful_password_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{attempt_id}/password",
            json={"password": "correct-password"},
        )

    assert password_required_response.status_code == 200
    assert invalid_password_response.status_code == 409
    assert invalid_password_response.json()["detail"] == "The Telegram password was incorrect. Try again."
    assert "wrong-password" not in invalid_password_response.text
    assert successful_password_response.status_code == 200
    assert successful_password_response.json()["telegram_session"]["status"] == "active"

    async with postgres_session_factory() as session:
        attempt = await session.get(TelegramSessionLoginAttempt, UUID(attempt_id))
    assert attempt is not None
    assert attempt.status == "completed"


async def test_admin_telegram_qr_login_pending_poll_does_not_fail_attempt(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-pending@example.com",
        is_admin=True,
    )
    qr_wait_event = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(qr_wait_event=qr_wait_event)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    monkeypatch.setattr(admin_telegram_login_module, "QR_LOGIN_POLL_TIMEOUT_SECONDS", 0.0)

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={},
        )
        session_id = create_response.json()["id"]
        qr_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr",
            json={"telegram_session_id": session_id},
        )
        assert fake_clients[0].qr_login_instance is not None
        await asyncio.wait_for(fake_clients[0].qr_login_instance.wait_started.wait(), timeout=5)
        assert (
            fake_clients[0].qr_login_instance.wait_timeout
            == admin_telegram_login_module.LOGIN_ATTEMPT_TTL.total_seconds()
        )
        attempt_id = UUID(qr_start_response.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        qr_wait_task = live_attempt.qr_wait_task
        assert qr_wait_task is not None
        assert qr_wait_task.done() is False
        qr_complete_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
            json={},
        )

    assert create_response.status_code == 201
    assert qr_start_response.status_code == 200
    assert qr_complete_response.status_code == 200
    qr_complete_payload = qr_complete_response.json()
    assert qr_complete_payload == {
        "status": "pending",
        "telegram_session": None,
        "password_required": False,
        "message": "Still waiting for Telegram QR scan.",
    }

    async with postgres_session_factory() as session:
        persisted = await session.get(TelegramSession, UUID(session_id))
        login_attempt = await session.scalar(
            select(TelegramSessionLoginAttempt).where(
                TelegramSessionLoginAttempt.id == UUID(qr_start_response.json()["attempt_id"]),
            ),
        )

    assert persisted is not None
    assert persisted.last_error_class is None
    assert persisted.last_error_text is None
    assert login_attempt is not None
    assert login_attempt.status == "pending"
    assert login_attempt.error_class is None
    assert login_attempt.error_text is None
    assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt  # noqa: SLF001
    assert live_attempt.qr_wait_task is qr_wait_task
    assert qr_wait_task.done() is False
    assert qr_wait_task.cancelled() is False
    assert fake_clients[0].disconnected is False


async def test_admin_telegram_starting_second_qr_attempt_keeps_first_attempt_live(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-concurrent-refresh@example.com",
        is_admin=True,
    )
    qr_wait_events = [asyncio.Event(), asyncio.Event()]
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(qr_wait_event=qr_wait_events[len(fake_clients)])
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    monkeypatch.setattr(admin_telegram_login_module, "QR_LOGIN_POLL_TIMEOUT_SECONDS", 0.0)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        first_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        old_attempt_id = first_start.json()["attempt_id"]
        assert fake_clients[0].qr_login_instance is not None
        await asyncio.wait_for(fake_clients[0].qr_login_instance.wait_started.wait(), timeout=5)

        refreshed = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        old_poll = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{old_attempt_id}/qr/complete",
            json={},
        )

    assert refreshed.status_code == 200
    new_attempt_id = refreshed.json()["attempt_id"]
    assert new_attempt_id != old_attempt_id
    assert old_poll.status_code == 200
    assert old_poll.json()["status"] == "pending"
    assert UUID(old_attempt_id) in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert UUID(new_attempt_id) in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert fake_clients[0].disconnected is False
    assert fake_clients[1].disconnected is False


async def test_admin_telegram_qr_poll_caller_cancellation_keeps_live_wait(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-poll-cancel@example.com",
        is_admin=True,
    )
    wait_event = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(qr_wait_event=wait_event)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    poll_wait_started = _observe_qr_poll_wait(monkeypatch)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        qr_wait_task = live_attempt.qr_wait_task
        assert qr_wait_task is not None

        poll_task = asyncio.create_task(
            admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
                json={},
            ),
        )
        await asyncio.wait_for(poll_wait_started.wait(), timeout=5)
        poll_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poll_task

    assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt  # noqa: SLF001
    assert live_attempt.retirement_task is None
    assert live_attempt.qr_wait_task is qr_wait_task
    assert qr_wait_task.done() is False
    assert qr_wait_task.cancelled() is False
    assert fake_clients[0].disconnected is False


async def test_admin_telegram_active_qr_poll_crossing_expiry_returns_controlled_conflict(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-poll-expiry@example.com",
        is_admin=True,
    )
    wait_event = asyncio.Event()
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            qr_wait_event=wait_event,
            qr_expires_at=clock.now + timedelta(minutes=1),
            disconnect_started=disconnect_started,
            disconnect_release=disconnect_release,
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    poll_wait_started = _observe_qr_poll_wait(monkeypatch)
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        poll_finished = asyncio.Event()
        active_poll_task = asyncio.create_task(
            admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
                json={},
            ),
        )
        active_poll_task.add_done_callback(lambda _task: poll_finished.set())
        await asyncio.wait_for(poll_wait_started.wait(), timeout=5)

        clock.advance(timedelta(minutes=2))
        assert scheduler.fire_due(clock.now) == [scheduler.handles[0]]
        retirement_task = live_attempt.retirement_task
        assert retirement_task is not None
        await asyncio.wait_for(disconnect_started.wait(), timeout=5)
        await asyncio.wait_for(poll_finished.wait(), timeout=5)

        assert disconnect_release.is_set() is False
        assert retirement_task.done() is False
        assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt  # noqa: SLF001
        assert fake_clients[0].disconnected is False
        active_poll = await active_poll_task
        assert active_poll.status_code == 409
        assert active_poll.json()["detail"] == "QR login attempt expired or was replaced. Start a new QR login."

        disconnect_release.set()
        await retirement_task

    assert fake_clients[0].disconnected is True
    assert fake_clients[0].disconnect_calls == 1
    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001


async def test_admin_telegram_qr_login_uses_earlier_token_expiry_for_response_and_wait(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, _scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-expiry@example.com",
        is_admin=True,
    )
    token_expires_at = clock.now + timedelta(seconds=30)
    wait_event = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(qr_wait_event=wait_event, qr_expires_at=token_expires_at)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )

    assert qr_start_response.status_code == 200
    attempt_id = UUID(qr_start_response.json()["attempt_id"])
    returned_expiry = datetime.fromisoformat(qr_start_response.json()["expires_at"])
    assert returned_expiry == token_expires_at
    assert fake_clients[0].qr_login_instance is not None
    await asyncio.wait_for(fake_clients[0].qr_login_instance.wait_started.wait(), timeout=5)
    assert fake_clients[0].qr_login_instance.wait_timeout == 30.0
    async with postgres_session_factory() as session:
        attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
    assert attempt is not None
    assert attempt.expires_at == clock.now + admin_telegram_login_module.LOGIN_ATTEMPT_TTL
    assert attempt.expires_at > returned_expiry


async def test_admin_telegram_accepted_qr_deadline_survives_token_expiry_and_scheduler_cleanup(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-durable-completion-deadline@example.com",
        is_admin=True,
    )
    qr_wait_event = asyncio.Event()
    token_expires_at = clock.now + timedelta(minutes=1)
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            qr_wait_event=qr_wait_event,
            qr_expires_at=token_expires_at,
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        qr_start = await admin_client.post("/api/v1/admin/telegram/login-attempts/qr", json={})
        attempt_id = UUID(qr_start.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        assert live_attempt.qr_wait_task is not None

        qr_wait_event.set()
        await live_attempt.qr_wait_task
        for _ in range(10):
            if live_attempt.qr_completion_persist_task is not None:
                break
            await asyncio.sleep(0)
        persistence_task = live_attempt.qr_completion_persist_task
        assert persistence_task is not None
        await persistence_task
        completion_expires_at = live_attempt.qr_completion_expires_at
        assert completion_expires_at == clock.now + admin_telegram_login_module.LOGIN_ATTEMPT_TTL

        async with postgres_session_factory() as session:
            attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "pending"
        assert attempt.expires_at == completion_expires_at
        assert attempt.expires_at > token_expires_at

        clock.advance(timedelta(minutes=2))
        assert scheduler.fire_due(clock.now) == []
        cleanup_result = await admin_telegram_login_module.run_telegram_login_cleanup_batch(
            postgres_session_factory,
            batch_size=100,
        )
        assert cleanup_result.scanned == 0
        assert cleanup_result.expired == 0
        assert cleanup_result.cleaned == 0
        assert cleanup_result.failed == 0
        assert fake_clients[0].logged_out is False
        assert fake_clients[0].disconnected is False

        qr_complete = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
            json={},
        )

    assert qr_complete.status_code == 200
    assert qr_complete.json()["status"] == "completed"
    assert fake_clients[0].logged_out is False
    assert fake_clients[0].disconnected is True


async def test_admin_telegram_cancel_standalone_live_attempt_discards_credential_without_creating_session(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-cancel-standalone@example.com",
        is_admin=True,
    )
    wait_event = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            qr_wait_event=wait_event,
            qr_expires_at=clock.now + timedelta(minutes=5),
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    async with postgres_session_factory() as session:
        session_count_before = await session.scalar(select(func.count()).select_from(TelegramSession))

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        qr_start = await admin_client.post("/api/v1/admin/telegram/login-attempts/qr", json={})
        attempt_id = UUID(qr_start.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        qr_wait_task = live_attempt.qr_wait_task
        assert qr_wait_task is not None
        assert fake_clients[0].qr_login_instance is not None
        await asyncio.wait_for(fake_clients[0].qr_login_instance.wait_started.wait(), timeout=5)

        async with postgres_session_factory() as session:
            pending_attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
        assert pending_attempt is not None
        assert pending_attempt.telegram_session_id is None
        assert pending_attempt.encrypted_temp_string_session is not None

        cancel_response = await admin_client.delete(f"/api/v1/admin/telegram/login-attempts/{attempt_id}")

    with suppress(asyncio.CancelledError):
        await qr_wait_task

    assert qr_start.status_code == 200
    assert cancel_response.status_code == 200
    assert cancel_response.json()["attempt_id"] == str(attempt_id)
    assert cancel_response.json()["status"] == "cancelled"
    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert qr_wait_task.cancelled() is True
    assert fake_clients[0].logged_out is False
    assert fake_clients[0].disconnected is True
    assert fake_clients[0].disconnect_calls == 1
    assert all(handle.cancelled() for handle in scheduler.handles)

    async with postgres_session_factory() as session:
        cancelled_attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
        session_count_after = await session.scalar(select(func.count()).select_from(TelegramSession))
    assert cancelled_attempt is not None
    assert cancelled_attempt.telegram_session_id is None
    assert cancelled_attempt.status == "cancelled"
    assert cancelled_attempt.cleanup_status == "discarded"
    assert cancelled_attempt.cleanup_attempts == 1
    assert cancelled_attempt.cleanup_completed_at is not None
    assert cancelled_attempt.encrypted_temp_string_session is None
    assert cancelled_attempt.phone_code_hash is None
    assert cancelled_attempt.qr_url is None
    assert session_count_after == session_count_before


async def test_telegram_login_cleanup_batch_revokes_authorized_terminal_temp_credential(
    auth_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _ = auth_app
    raw_temp_session = "authorized-telegram-login-session"
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self
        assert string_session is not None
        assert string_session.get_secret_value() == raw_temp_session
        client = _FakeTelegramLoginClient()
        client.string_session = string_session.get_secret_value()
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    completed_at = datetime.now(UTC) - timedelta(minutes=2)
    async with postgres_session_factory() as session:
        encrypted_temp = admin_service_module.AdminService(session)._encrypt_string_session(
            SecretStr(raw_temp_session),
        )
        attempt = TelegramSessionLoginAttempt(
            method="qr",
            status="expired",
            cleanup_status="pending",
            encrypted_temp_string_session=encrypted_temp,
            qr_url="tg://login?token=terminal-cleanup",
            expires_at=completed_at - timedelta(minutes=1),
            completed_at=completed_at,
        )
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id

    cleanup_result = await admin_telegram_login_module.run_telegram_login_cleanup_batch(
        postgres_session_factory,
        batch_size=10,
    )

    assert cleanup_result.scanned == 1
    assert cleanup_result.expired == 0
    assert cleanup_result.cleaned == 1
    assert cleanup_result.failed == 0
    assert len(fake_clients) == 1
    assert fake_clients[0].logged_out is True
    assert fake_clients[0].disconnected is True
    assert fake_clients[0].disconnect_calls == 1

    async with postgres_session_factory() as session:
        cleaned_attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
    assert cleaned_attempt is not None
    assert cleaned_attempt.status == "expired"
    assert cleaned_attempt.cleanup_status == "discarded"
    assert cleaned_attempt.cleanup_attempts == 1
    assert cleaned_attempt.cleanup_completed_at is not None
    assert cleaned_attempt.encrypted_temp_string_session is None
    assert cleaned_attempt.phone_code_hash is None
    assert cleaned_attempt.qr_url is None


async def test_telegram_login_cleanup_batch_retries_failed_logout_without_losing_credential(
    auth_app: FastAPI,
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _ = auth_app
    raw_temp_session = "authorized-telegram-login-session"
    logout_error = "simulated temporary Telegram logout failure"
    fake_clients: list[_FakeTelegramLoginClient] = []

    class _FailingLogoutTelegramLoginClient(_FakeTelegramLoginClient):
        async def log_out(self) -> bool:
            raise RuntimeError(logout_error)

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self
        assert string_session is not None
        assert string_session.get_secret_value() == raw_temp_session
        client = _FakeTelegramLoginClient() if fake_clients else _FailingLogoutTelegramLoginClient()
        client.string_session = string_session.get_secret_value()
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    completed_at = datetime.now(UTC) - timedelta(minutes=2)
    async with postgres_session_factory() as session:
        encrypted_temp = admin_service_module.AdminService(session)._encrypt_string_session(
            SecretStr(raw_temp_session),
        )
        attempt = TelegramSessionLoginAttempt(
            method="phone",
            status="cancelled",
            cleanup_status="pending",
            encrypted_temp_string_session=encrypted_temp,
            phone_code_hash="retry-cleanup-phone-code-hash",
            expires_at=completed_at - timedelta(minutes=1),
            completed_at=completed_at,
        )
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id

    first_cleanup_result = await admin_telegram_login_module.run_telegram_login_cleanup_batch(
        postgres_session_factory,
        batch_size=10,
    )

    assert first_cleanup_result.scanned == 1
    assert first_cleanup_result.expired == 0
    assert first_cleanup_result.cleaned == 0
    assert first_cleanup_result.failed == 1
    assert len(fake_clients) == 1
    assert fake_clients[0].logged_out is False
    assert fake_clients[0].disconnected is True
    assert fake_clients[0].disconnect_calls == 1

    async with postgres_session_factory() as session:
        failed_attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
    assert failed_attempt is not None
    assert failed_attempt.cleanup_status == "failed"
    assert failed_attempt.cleanup_attempts == 1
    assert failed_attempt.cleanup_error_class == "RuntimeError"
    assert failed_attempt.cleanup_error_text == logout_error
    assert failed_attempt.cleanup_completed_at is None
    assert failed_attempt.encrypted_temp_string_session == encrypted_temp
    assert failed_attempt.phone_code_hash == "retry-cleanup-phone-code-hash"

    second_cleanup_result = await admin_telegram_login_module.run_telegram_login_cleanup_batch(
        postgres_session_factory,
        batch_size=10,
    )

    assert second_cleanup_result.scanned == 1
    assert second_cleanup_result.expired == 0
    assert second_cleanup_result.cleaned == 1
    assert second_cleanup_result.failed == 0
    assert len(fake_clients) == 2
    assert fake_clients[1].logged_out is True
    assert fake_clients[1].disconnected is True
    assert fake_clients[1].disconnect_calls == 1

    async with postgres_session_factory() as session:
        cleaned_attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
    assert cleaned_attempt is not None
    assert cleaned_attempt.status == "cancelled"
    assert cleaned_attempt.cleanup_status == "discarded"
    assert cleaned_attempt.cleanup_attempts == 2
    assert cleaned_attempt.cleanup_error_class is None
    assert cleaned_attempt.cleanup_error_text is None
    assert cleaned_attempt.cleanup_completed_at is not None
    assert cleaned_attempt.encrypted_temp_string_session is None
    assert cleaned_attempt.phone_code_hash is None
    assert cleaned_attempt.qr_url is None


async def test_admin_telegram_qr_login_expiry_cleanup_releases_abandoned_client(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-abandoned@example.com",
        is_admin=True,
    )
    wait_event = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            qr_wait_event=wait_event,
            qr_expires_at=clock.now + timedelta(minutes=1),
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )

    attempt_id = UUID(qr_start_response.json()["attempt_id"])
    assert fake_clients[0].qr_login_instance is not None
    await asyncio.wait_for(fake_clients[0].qr_login_instance.wait_started.wait(), timeout=5)
    live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
    fake_clients[0].string_session = "authorized-telegram-login-session"
    clock.advance(timedelta(minutes=2))
    assert scheduler.fire_due(clock.now) == [scheduler.handles[0]]
    retirement_task = live_attempt.retirement_task
    assert retirement_task is not None
    await retirement_task
    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert fake_clients[0].disconnected is True
    assert fake_clients[0].logged_out is True


async def test_admin_telegram_qr_login_completion_cancels_expiry_cleanup(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-complete-before-expiry@example.com",
        is_admin=True,
    )
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(qr_expires_at=clock.now + timedelta(minutes=1))
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start_response.json()["attempt_id"])
        qr_complete_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
            json={},
        )

    assert qr_complete_response.status_code == 200
    assert qr_complete_response.json()["status"] == "completed"
    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert fake_clients[0].disconnected is True
    assert fake_clients[0].logged_out is False
    assert fake_clients[0].disconnect_calls == 1
    assert all(handle.cancelled() for handle in scheduler.handles)
    clock.advance(admin_telegram_login_module.LOGIN_ATTEMPT_TTL + timedelta(minutes=2))
    assert scheduler.fire_due(clock.now) == []
    assert fake_clients[0].disconnect_calls == 1


async def test_admin_telegram_qr_finalization_cancellation_retires_client_after_blocked_disconnect(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _clock, _scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-cancel-finalization@example.com",
        is_admin=True,
    )
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            disconnect_started=disconnect_started,
            disconnect_release=disconnect_release,
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start_response.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001

        completion_task = asyncio.create_task(
            admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
                json={},
            ),
        )
        await asyncio.wait_for(disconnect_started.wait(), timeout=5)
        cleanup_handle = live_attempt.qr_expiry_cleanup_handle
        retirement_task = live_attempt.retirement_task
        assert cleanup_handle is not None
        assert retirement_task is not None
        completion_task.cancel()
        with suppress(asyncio.CancelledError):
            await completion_task

        assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt  # noqa: SLF001
        assert live_attempt.qr_expiry_cleanup_handle is cleanup_handle
        assert cleanup_handle.cancelled() is False

        disconnect_release.set()
        await retirement_task

    assert fake_clients[0].disconnected is True
    assert fake_clients[0].disconnect_finished.is_set()
    assert fake_clients[0].disconnect_calls == 1
    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert live_attempt.qr_expiry_cleanup_handle is None
    assert cleanup_handle.cancelled() is True


async def test_admin_telegram_reconnect_cancellation_still_revokes_rotated_credential(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _clock, _scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-reconnect-cancel-revoke@example.com",
        is_admin=True,
    )
    new_disconnect_started = asyncio.Event()
    new_disconnect_release = asyncio.Event()
    old_revoke_started = asyncio.Event()
    old_revoke_release = asyncio.Event()
    old_revoke_finished = asyncio.Event()
    new_clients: list[_FakeTelegramLoginClient] = []
    old_clients: list[_FakeTelegramLoginClient] = []

    class _ObservedOldCredentialClient(_FakeTelegramLoginClient):
        async def log_out(self) -> bool:
            old_revoke_started.set()
            await old_revoke_release.wait()
            result = await super().log_out()
            old_revoke_finished.set()
            return result

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self
        if string_session is not None:
            old_client = _ObservedOldCredentialClient()
            old_client.string_session = string_session.get_secret_value()
            old_clients.append(old_client)
            return old_client
        new_client = _FakeTelegramLoginClient(
            disconnect_started=new_disconnect_started,
            disconnect_release=new_disconnect_release,
        )
        new_clients.append(new_client)
        return new_client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = UUID(create_response.json()["id"])
        async with postgres_session_factory() as session:
            telegram_session = await session.get(TelegramSession, session_id)
            assert telegram_session is not None
            login_service = admin_telegram_login_module.AdminTelegramLoginService(session=session)
            telegram_session.encrypted_string_session = login_service._admin_service._encrypt_string_session(  # noqa: SLF001
                SecretStr("authorized-telegram-login-session"),
            )
            telegram_session.account_user_id = _FakeTelegramUser.id
            telegram_session.account_username = _FakeTelegramUser.username
            telegram_session.status = TelegramSessionStatus.ACTIVE
            await session.commit()

        qr_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr",
            json={"telegram_session_id": str(session_id)},
        )
        attempt_id = UUID(qr_start.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        completion_task = asyncio.create_task(
            admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
                json={},
            ),
        )
        await asyncio.wait_for(new_disconnect_started.wait(), timeout=5)
        await asyncio.wait_for(old_revoke_started.wait(), timeout=5)
        retirement_task = live_attempt.retirement_task
        assert retirement_task is not None

        completion_task.cancel()
        with suppress(asyncio.CancelledError):
            await completion_task
        assert old_revoke_finished.is_set() is False
        old_revoke_release.set()
        await asyncio.wait_for(old_revoke_finished.wait(), timeout=5)

        assert len(old_clients) == 1
        assert old_clients[0].logged_out is True
        assert old_clients[0].disconnected is True
        assert retirement_task.done() is False

        new_disconnect_release.set()
        await retirement_task

    assert len(new_clients) == 1
    assert new_clients[0].logged_out is False
    assert new_clients[0].disconnected is True
    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001


@pytest.mark.parametrize("require_password", [False, True], ids=["success", "password-required"])
async def test_admin_telegram_qr_accepted_wait_survives_expiry_before_done_callback(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
    *,
    require_password: bool,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email=f"admin-telegram-qr-expiry-race-{require_password}@example.com",
        is_admin=True,
    )
    token_expires_at = clock.now + timedelta(minutes=1)
    qr_wait_event = asyncio.Event()
    done_callback_started = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            require_password=require_password,
            qr_wait_event=qr_wait_event,
            qr_expires_at=token_expires_at,
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    poll_wait_started = _observe_qr_poll_wait(monkeypatch)
    original_wait_finished = admin_telegram_login_module.AdminTelegramLoginService._qr_wait_finished  # noqa: SLF001

    def observed_wait_finished(
        service: admin_telegram_login_module.AdminTelegramLoginService,
        attempt_id: UUID,
        live_attempt: admin_telegram_login_module._LiveLoginAttempt,  # noqa: SLF001
        task: asyncio.Task[object],
    ) -> None:
        done_callback_started.set()
        original_wait_finished(service, attempt_id, live_attempt, task)

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_qr_wait_finished",
        observed_wait_finished,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start.json()["attempt_id"])
        assert fake_clients[0].qr_login_instance is not None
        await asyncio.wait_for(fake_clients[0].qr_login_instance.wait_started.wait(), timeout=5)
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        qr_wait_task = live_attempt.qr_wait_task
        assert qr_wait_task is not None
        assert qr_wait_task.done() is False
        token_cleanup_handle = scheduler.handles[0]

        qr_complete_task = asyncio.create_task(
            admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
                json={},
            ),
        )
        await asyncio.wait_for(poll_wait_started.wait(), timeout=5)

        clock.advance(timedelta(minutes=2))
        expiry_callback_ran = asyncio.Event()
        race_observations: list[tuple[bool, bool, bool]] = []

        def fire_token_expiry() -> None:
            race_observations.append(
                (
                    qr_wait_task.done(),
                    done_callback_started.is_set(),
                    token_cleanup_handle.fire(),
                ),
            )
            expiry_callback_ran.set()

        qr_wait_event.set()
        asyncio.get_running_loop().call_soon(fire_token_expiry)
        await asyncio.wait_for(expiry_callback_ran.wait(), timeout=5)

        assert race_observations == [(True, False, True)]
        assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt  # noqa: SLF001
        assert fake_clients[0].disconnected is False
        assert token_cleanup_handle.cancelled() is True
        completion_expires_at = live_attempt.qr_completion_expires_at
        assert completion_expires_at == clock.now + admin_telegram_login_module.LOGIN_ATTEMPT_TTL

        qr_complete = await qr_complete_task
        assert qr_complete.status_code == 200
        if require_password:
            assert qr_complete.json()["status"] == "password_required"
            assert fake_clients[0].disconnected is False
            password_complete = await admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/password",
                json={"password": "correct-password"},
            )
            assert password_complete.status_code == 200
        else:
            assert qr_complete.json()["status"] == "completed"

    async with postgres_session_factory() as session:
        attempt = await session.get(TelegramSessionLoginAttempt, attempt_id)
    assert attempt is not None
    assert attempt.status == "completed"
    assert attempt.expires_at == completion_expires_at
    assert attempt.expires_at > token_expires_at
    assert fake_clients[0].disconnected is True


async def test_admin_telegram_qr_promoted_deadline_retires_abandoned_password_client(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-promoted-expiry@example.com",
        is_admin=True,
    )
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            require_password=True,
            qr_expires_at=clock.now + timedelta(minutes=1),
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start.json()["attempt_id"])
        token_cleanup_handle = scheduler.handles[0]
        qr_complete = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
            json={},
        )

    assert qr_complete.status_code == 200
    assert qr_complete.json()["status"] == "password_required"
    live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
    completion_cleanup_handle = scheduler.handles[-1]
    assert completion_cleanup_handle is not token_cleanup_handle
    assert token_cleanup_handle.cancelled() is True
    assert completion_cleanup_handle.cancelled() is False

    assert token_cleanup_handle.fire(even_if_cancelled=True) is True
    assert fake_clients[0].disconnected is False
    assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is live_attempt  # noqa: SLF001

    clock.advance(admin_telegram_login_module.LOGIN_ATTEMPT_TTL + timedelta(seconds=1))
    assert scheduler.fire_due(clock.now) == [completion_cleanup_handle]
    retirement_task = live_attempt.retirement_task
    assert retirement_task is not None
    await retirement_task

    assert attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert fake_clients[0].disconnected is True


async def test_admin_telegram_qr_success_survives_token_expiry_during_delayed_finalization(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-delayed-finalization@example.com",
        is_admin=True,
    )
    get_me_started = asyncio.Event()
    get_me_release = asyncio.Event()
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            qr_expires_at=clock.now + timedelta(minutes=1),
            get_me_started=get_me_started,
            get_me_release=get_me_release,
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        qr_start = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        attempt_id = UUID(qr_start.json()["attempt_id"])
        live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id]  # noqa: SLF001
        qr_complete_task = asyncio.create_task(
            admin_client.post(
                f"/api/v1/admin/telegram/login-attempts/{str(attempt_id)}/qr/complete",
                json={},
            ),
        )
        await asyncio.wait_for(get_me_started.wait(), timeout=5)

        assert live_attempt.qr_completion_expires_at == clock.now + admin_telegram_login_module.LOGIN_ATTEMPT_TTL
        clock.advance(timedelta(minutes=2))
        assert scheduler.fire_due(clock.now) == []
        assert qr_complete_task.done() is False
        assert fake_clients[0].disconnected is False

        get_me_release.set()
        qr_complete = await qr_complete_task

    assert qr_complete.status_code == 200
    assert qr_complete.json()["status"] == "completed"
    assert qr_complete.json()["telegram_session"]["status"] == "active"
    assert fake_clients[0].disconnected is True


async def test_admin_telegram_qr_expiry_cleanup_is_scoped_to_one_standalone_attempt(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    clock, scheduler = _install_manual_qr_time(monkeypatch)
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-replacement@example.com",
        is_admin=True,
    )
    expiries = [clock.now + timedelta(minutes=1), clock.now + timedelta(minutes=5)]
    wait_events = [asyncio.Event(), asyncio.Event()]
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(
            qr_wait_event=wait_events[len(fake_clients)],
            qr_expires_at=expiries[len(fake_clients)],
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post("/api/v1/admin/telegram/sessions", json={})
        session_id = create_response.json()["id"]
        first_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )
        first_attempt_id = UUID(first_start_response.json()["attempt_id"])
        first_live_attempt = admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[first_attempt_id]  # noqa: SLF001
        first_cleanup_handle = scheduler.handles[0]
        second_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr", json={"telegram_session_id": session_id}
        )

    second_attempt_id = UUID(second_start_response.json()["attempt_id"])
    assert first_start_response.status_code == 200
    assert second_start_response.status_code == 200
    assert first_cleanup_handle.cancelled() is False
    assert first_live_attempt.retirement_task is None
    assert first_cleanup_handle.fire() is True
    retirement_task = first_live_attempt.retirement_task
    assert retirement_task is not None
    await retirement_task
    assert first_attempt_id not in admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS  # noqa: SLF001
    assert fake_clients[0].disconnected is True
    assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(second_attempt_id) is not None  # noqa: SLF001
    assert fake_clients[1].disconnected is False


async def test_admin_telegram_qr_cleanup_callback_is_exact_object_race_safe(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = uuid7()
    old_client = _FakeTelegramLoginClient()
    replacement_client = _FakeTelegramLoginClient()
    old_attempt = admin_telegram_login_module._LiveLoginAttempt(client=old_client)  # noqa: SLF001
    replacement_attempt = admin_telegram_login_module._LiveLoginAttempt(client=replacement_client)  # noqa: SLF001
    old_attempt.qr_expiry_cleanup_generation = 1

    async with postgres_session_factory() as session:
        service = admin_telegram_login_module.AdminTelegramLoginService(session)
        admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS[attempt_id] = replacement_attempt  # noqa: SLF001
        service._cleanup_expired_qr_attempt(attempt_id, old_attempt, 1)  # noqa: SLF001

    assert admin_telegram_login_module._LIVE_LOGIN_ATTEMPTS.get(attempt_id) is replacement_attempt  # noqa: SLF001
    assert old_client.disconnected is False
    assert replacement_client.disconnected is False


async def test_admin_telegram_qr_login_supports_2fa_password_without_leaking_secret(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-qr-2fa@example.com",
        is_admin=True,
    )
    password = "very secret qr telegram password"
    fake_clients: list[_FakeTelegramLoginClient] = []

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        client = _FakeTelegramLoginClient(require_password=True, invalid_password_attempts=1)
        fake_clients.append(client)
        return client

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        create_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={},
        )
        session_id = create_response.json()["id"]
        qr_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/qr",
            json={"telegram_session_id": session_id},
        )
        qr_complete_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{qr_start_response.json()['attempt_id']}/qr/complete",
            json={},
        )
        invalid_password_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{qr_start_response.json()['attempt_id']}/password",
            json={"password": "wrong-qr-password"},
        )
        assert fake_clients[0].disconnected is False
        password_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{qr_start_response.json()['attempt_id']}/password",
            json={"password": password},
        )

    assert create_response.status_code == 201
    assert qr_start_response.status_code == 200
    assert qr_start_response.json()["qr_url"] == "tg://login?token=fake-qr-token"
    assert qr_complete_response.status_code == 200
    qr_complete_payload = qr_complete_response.json()
    assert qr_complete_payload["status"] == "password_required"
    assert qr_complete_payload["password_required"] is True
    assert qr_complete_payload["telegram_session"]["status"] == "auth_required"
    assert qr_complete_payload["telegram_session"]["has_string_session"] is False
    assert password not in qr_complete_response.text
    assert "temporary-telegram-qr-login-session" not in qr_complete_response.text

    assert invalid_password_response.status_code == 409
    assert invalid_password_response.json()["detail"] == "The Telegram password was incorrect. Try again."
    assert "wrong-qr-password" not in invalid_password_response.text
    assert password_response.status_code == 200
    password_payload = password_response.json()
    assert password_payload["password_required"] is False
    assert password_payload["telegram_session"]["status"] == "active"
    assert password_payload["telegram_session"]["name"] == "telegram_777000"
    assert password_payload["telegram_session"]["display_name"] == "Validated Admin"
    assert password_payload["telegram_session"]["has_string_session"] is True
    assert password not in password_response.text
    assert "authorized-telegram-login-session" not in password_response.text
    assert fake_clients[0].sign_in_calls == [
        {"password": "wrong-qr-password"},
        {"password": password},
    ]

    async with postgres_session_factory() as session:
        persisted = await session.get(TelegramSession, UUID(session_id))
        login_attempt = await session.scalar(
            select(TelegramSessionLoginAttempt).where(
                TelegramSessionLoginAttempt.id == UUID(qr_start_response.json()["attempt_id"]),
            ),
        )
        audit_rows = (
            (
                await session.execute(
                    select(TelegramAdminAuditLog).where(TelegramAdminAuditLog.telegram_session_id == UUID(session_id)),
                )
            )
            .scalars()
            .all()
        )

    assert persisted is not None
    assert persisted.encrypted_string_session is not None
    assert persisted.encrypted_string_session != "authorized-telegram-login-session"
    assert persisted.name == "telegram_777000"
    assert persisted.display_name == "Validated Admin"
    assert login_attempt is not None
    assert login_attempt.method == "qr"
    assert login_attempt.status == "completed"
    assert login_attempt.cleanup_status == "promoted"
    assert login_attempt.encrypted_temp_string_session is None
    assert login_attempt.phone_code_hash is None
    assert login_attempt.qr_url is None
    assert fake_clients[0].logged_out is False
    for row in audit_rows:
        audit_text = f"{row.previous_values} {row.new_values} {row.note}"
        assert password not in audit_text
        assert "temporary-telegram-qr-login-session" not in audit_text
        assert "encrypted_string_session" not in audit_text


async def test_admin_telegram_login_rejects_expired_and_wrong_method_attempts(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-expired@example.com",
        is_admin=True,
    )
    full_phone_number = "+10000007222"

    def fake_build_telegram_client(
        self: admin_telegram_login_module.AdminTelegramLoginService,
        string_session: SecretStr | None = None,
    ) -> _FakeTelegramLoginClient:
        _ = self, string_session
        return _FakeTelegramLoginClient()

    monkeypatch.setattr(
        admin_telegram_login_module.AdminTelegramLoginService,
        "_build_telegram_client",
        fake_build_telegram_client,
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        first_create_response = await admin_client.post(
            "/api/v1/admin/telegram/sessions",
            json={"name": "expired-login-session", "display_name": "Expired Login Session"},
        )
        first_session_id = first_create_response.json()["id"]
        expired_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/phone",
            json={"telegram_session_id": first_session_id, "phone_number": full_phone_number},
        )
        expired_attempt_id = expired_start_response.json()["attempt_id"]

        async with postgres_session_factory() as session:
            expired_attempt = await session.get(TelegramSessionLoginAttempt, UUID(expired_attempt_id))
            assert expired_attempt is not None
            expired_attempt.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()

        expired_code_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{expired_attempt_id}/phone/code",
            json={"code": "13579"},
        )
        wrong_start_response = await admin_client.post(
            "/api/v1/admin/telegram/login-attempts/phone",
            json={"telegram_session_id": first_session_id, "phone_number": full_phone_number},
        )
        wrong_method_response = await admin_client.post(
            f"/api/v1/admin/telegram/login-attempts/{wrong_start_response.json()['attempt_id']}/qr/complete",
            json={},
        )

    assert first_create_response.status_code == 201
    assert expired_start_response.status_code == 200
    assert expired_code_response.status_code == 409
    assert "expired" in expired_code_response.json()["detail"]
    assert wrong_start_response.status_code == 200
    assert wrong_method_response.status_code == 409
    assert "method does not match" in wrong_method_response.json()["detail"]
    assert full_phone_number not in expired_code_response.text
    assert full_phone_number not in wrong_method_response.text

    async with postgres_session_factory() as session:
        expired_attempt = await session.get(TelegramSessionLoginAttempt, UUID(expired_attempt_id))
        wrong_attempt = await session.get(TelegramSessionLoginAttempt, UUID(wrong_start_response.json()["attempt_id"]))

    assert expired_attempt is not None
    assert expired_attempt.status == "expired"
    assert expired_attempt.phone_number_hint == "ending-7222"
    assert wrong_attempt is not None
    assert wrong_attempt.status == "pending"


async def test_admin_telegram_channel_assignment_orphan_filters_and_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-telegram-channel@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        telegram_session = TelegramSession(
            name="assignment-session",
            display_name="Assignment Session",
            status=TelegramSessionStatus.ACTIVE,
        )
        session.add(telegram_session)
        await session.commit()
        telegram_session_id = telegram_session.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        missing_target_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={"platform": "telegram", "platform_id": "missing-target", "title": "Missing Target"},
        )
        unknown_target_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={
                "platform": "telegram",
                "platform_id": "unknown-target",
                "title": "Unknown Target",
                "telegram_session_id": str(uuid7()),
            },
        )
        orphan_create_response = await admin_client.post(
            "/api/v1/admin/telegram/channels",
            json={
                "platform": "telegram",
                "platform_id": "orphan-create",
                "title": "Orphan Create",
                "orphaned": True,
                "catchup_enabled": True,
                "live_enabled": True,
                "engagement_enabled": True,
            },
        )
        channel_id = orphan_create_response.json()["id"]
        enable_orphan_response = await admin_client.patch(
            f"/api/v1/admin/telegram/channels/{channel_id}",
            json={"catchup_enabled": True},
        )
        assign_unknown_response = await admin_client.post(
            f"/api/v1/admin/telegram/channels/{channel_id}/assign",
            json={"telegram_session_id": str(uuid7())},
        )
        assign_response = await admin_client.post(
            f"/api/v1/admin/telegram/channels/{channel_id}/assign",
            json={"telegram_session_id": str(telegram_session_id), "note": "move to live session"},
        )
        update_response = await admin_client.patch(
            f"/api/v1/admin/telegram/channels/{channel_id}",
            json={
                "catchup_enabled": True,
                "live_enabled": True,
                "engagement_enabled": True,
                "catchup_message_limit": 123,
            },
        )
        by_session_response = await admin_client.get(
            f"/api/v1/admin/telegram/channels?telegram_session_id={telegram_session_id}",
        )
        grouped_response = await admin_client.get("/api/v1/admin/telegram/channels/grouped")
        orphan_response = await admin_client.post(
            f"/api/v1/admin/telegram/channels/{channel_id}/orphan",
            json={"note": "explicit orphan"},
        )
        orphaned_list_response = await admin_client.get("/api/v1/admin/telegram/channels?orphaned=true")

    assert missing_target_response.status_code == 409
    assert "telegram_session_id or orphaned=true" in missing_target_response.json()["detail"]
    assert unknown_target_response.status_code == 404

    assert orphan_create_response.status_code == 201
    orphan_payload = orphan_create_response.json()
    assert orphan_payload["telegram_session_id"] is None
    assert orphan_payload["is_orphaned"] is True
    assert orphan_payload["is_indexable"] is False
    assert orphan_payload["catchup_enabled"] is False
    assert orphan_payload["live_enabled"] is False
    assert orphan_payload["engagement_enabled"] is False

    assert enable_orphan_response.status_code == 409
    assert "Orphaned source channels" in enable_orphan_response.json()["detail"]
    assert assign_unknown_response.status_code == 404

    assert assign_response.status_code == 200
    assert assign_response.json()["telegram_session_id"] == str(telegram_session_id)
    assert assign_response.json()["is_orphaned"] is False
    assert assign_response.json()["is_indexable"] is False

    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["catchup_message_limit"] == 123
    assert update_payload["is_indexable"] is True

    assert by_session_response.status_code == 200
    assert [item["id"] for item in by_session_response.json()] == [channel_id]

    assert grouped_response.status_code == 200
    session_groups = {group["telegram_session"]["id"] for group in grouped_response.json() if group["telegram_session"]}
    assert str(telegram_session_id) in session_groups

    assert orphan_response.status_code == 200
    orphan_after_assign_payload = orphan_response.json()
    assert orphan_after_assign_payload["telegram_session_id"] is None
    assert orphan_after_assign_payload["catchup_enabled"] is False
    assert orphan_after_assign_payload["live_enabled"] is False
    assert orphan_after_assign_payload["engagement_enabled"] is False
    assert orphan_after_assign_payload["is_indexable"] is False

    assert orphaned_list_response.status_code == 200
    assert channel_id in {item["id"] for item in orphaned_list_response.json()}

    async with postgres_session_factory() as session:
        persisted = await session.get(SourceChannel, UUID(channel_id))
        audit_actions = (
            (
                await session.execute(
                    select(TelegramAdminAuditLog.action)
                    .where(TelegramAdminAuditLog.source_channel_id == UUID(channel_id))
                    .order_by(TelegramAdminAuditLog.created_at.asc(), TelegramAdminAuditLog.id.asc()),
                )
            )
            .scalars()
            .all()
        )

    assert persisted is not None
    assert persisted.telegram_session_id is None
    assert persisted.catchup_enabled is False
    assert persisted.live_enabled is False
    assert persisted.engagement_enabled is False
    assert audit_actions == ["channel_create", "channel_assign", "channel_update", "channel_orphan"]


async def test_admin_delete_telegram_session_orphans_channels_and_audits_delete(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-telegram-session@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        telegram_session = TelegramSession(
            name="delete-session",
            display_name="Delete Session",
            status=TelegramSessionStatus.ACTIVE,
            encrypted_string_session="encrypted-not-raw",
        )
        keep_session = TelegramSession(
            name="keep-session",
            display_name="Keep Session",
            status=TelegramSessionStatus.ACTIVE,
        )
        first_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="delete-session-first",
            title="Delete Session First",
            telegram_session=telegram_session,
            catchup_enabled=True,
            live_enabled=True,
            engagement_enabled=True,
        )
        second_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="delete-session-second",
            title="Delete Session Second",
            telegram_session=telegram_session,
            catchup_enabled=True,
            live_enabled=True,
            engagement_enabled=True,
        )
        keep_channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="keep-session-channel",
            title="Keep Session Channel",
            telegram_session=keep_session,
        )
        session.add_all([telegram_session, keep_session, first_channel, second_channel, keep_channel])
        await session.commit()
        session_id = telegram_session.id
        first_channel_id = first_channel.id
        second_channel_id = second_channel.id
        keep_channel_id = keep_channel.id
        keep_session_id = keep_session.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        bad_confirmation_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={"confirmation": str(uuid7()), "note": "try wrong delete"},
        )
        delete_response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/telegram/sessions/{session_id}",
            json={"confirmation": str(session_id), "note": "retire account"},
        )

    assert bad_confirmation_response.status_code == 409
    assert delete_response.status_code == 200
    assert delete_response.json()["orphaned_source_channel_count"] == 2

    async with postgres_session_factory() as session:
        deleted_session = await session.get(TelegramSession, session_id)
        first_channel = await session.get(SourceChannel, first_channel_id)
        second_channel = await session.get(SourceChannel, second_channel_id)
        keep_channel = await session.get(SourceChannel, keep_channel_id)
        audit_rows = (
            (
                await session.execute(
                    select(TelegramAdminAuditLog)
                    .where(TelegramAdminAuditLog.telegram_session_id == session_id)
                    .order_by(TelegramAdminAuditLog.created_at.asc(), TelegramAdminAuditLog.id.asc()),
                )
            )
            .scalars()
            .all()
        )

    assert deleted_session is None
    assert first_channel is not None
    assert second_channel is not None
    for channel in (first_channel, second_channel):
        assert channel.telegram_session_id is None
        assert channel.catchup_enabled is False
        assert channel.live_enabled is False
        assert channel.engagement_enabled is False
    assert keep_channel is not None
    assert keep_channel.telegram_session_id == keep_session_id
    assert [row.action for row in audit_rows] == ["channel_orphan", "channel_orphan", "session_delete"]
    delete_audit = audit_rows[-1]
    assert delete_audit.note == "retire account"
    assert "encrypted_string_session" not in str(delete_audit.previous_values)


async def test_admin_can_delete_meme_with_durable_destructive_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-meme@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (await session.execute(select(User).where(User.email == "admin-delete-meme@example.com"))).scalar_one()
        meme, meme_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            like_count=7,
            file_key="admin/delete/original.jpg",
        )
        await _persist_canonical_meme(session, meme, meme_file)
        collection = Collection(owner_id=admin.id, title="Admin saved memes")
        report = ModerationReport(meme=meme, reporter_user_id=admin.id, reason=ModerationReason.SPAM)
        decision = ModerationDecision(
            meme=meme,
            report=report,
            admin_user_id=admin.id,
            action=ModerationAction.HIDE,
            reason=ModerationReason.SPAM,
            note="Prior hide",
            previous_is_public=True,
            previous_visibility_mode=MemeVisibilityMode.AUTO,
            previous_is_nsfw=False,
            new_is_public=False,
            new_visibility_mode=MemeVisibilityMode.FORCE_PRIVATE,
            new_is_nsfw=False,
        )
        seo_page = MemeSeoPage(
            meme=meme,
            slug="admin-delete-test",
            page_title="Delete test",
            meta_description="Delete test",
            alt_text="Delete test",
            tags=["delete"],
            model_id="test-model",
            prompt_version="v1",
        )
        session.add_all([admin, collection, report, decision, seo_page])
        await session.flush()
        session.add_all(
            [
                CollectionMeme(collection=collection, meme=meme, added_by_user_id=admin.id),
                PinnedMeme(user_id=admin.id, meme=meme, position=1),
            ]
        )
        await session.commit()
        meme_id = meme.id
        file_id = meme_file.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            json={"confirmation": str(meme_id), "note": "Unsafe duplicate should be removed"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "delete"
    assert payload["source_meme_id"] == str(meme_id)
    assert payload["target_meme_id"] is None
    assert payload["affected_snapshot"]["meme_files"]["count"] == 1
    assert payload["affected_snapshot"]["seo_page"]["count"] == 1
    assert payload["affected_snapshot"]["collection_saves"]["count"] == 1
    assert payload["affected_snapshot"]["pins"]["count"] == 1
    assert payload["affected_snapshot"]["moderation_reports"]["count"] == 1
    assert payload["affected_snapshot"]["moderation_decisions"]["count"] == 1

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        persisted_file = await session.get(MemeFile, file_id)
        audit_log = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == meme_id),
        )

        assert persisted_meme is None
        assert persisted_file is None
        assert audit_log is not None
        assert audit_log.admin_user_id == admin_id
        assert audit_log.action == "delete"
        assert audit_log.note == "Unsafe duplicate should be removed"
        affected_snapshot = cast("dict[str, dict[str, object]]", audit_log.affected_snapshot)
        assert affected_snapshot["meme_files"]["ids"] == [str(file_id)]


async def test_admin_delete_requires_exact_confirmation_without_partial_delete(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-delete-blocked@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        meme_id = meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.request(
            "DELETE",
            f"/api/v1/admin/memes/{meme_id}",
            json={"confirmation": "wrong-id", "note": "try delete"},
        )

    assert response.status_code == 409
    assert "confirmation" in response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        audit_count = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == meme_id),
        )

        assert persisted_meme is not None
        assert audit_count is None


async def test_admin_can_merge_meme_with_shared_lineage_transfer_and_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-merge-meme@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        admin = (await session.execute(select(User).where(User.email == "admin-merge-meme@example.com"))).scalar_one()
        source_meme, source_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            like_count=2,
            file_key="admin/merge/source.jpg",
            file_quality=0.5,
        )
        target_meme, target_file = _canonical_meme(
            media_type=ContentKind.IMAGE,
            is_public=True,
            is_nsfw=False,
            like_count=5,
            file_key="admin/merge/target.jpg",
            file_quality=1.0,
        )
        collection = Collection(owner_id=admin.id, title="Merge collection")
        await _persist_canonical_meme(session, source_meme, source_file)
        await _persist_canonical_meme(session, target_meme, target_file)
        session.add(collection)
        await session.flush()
        session.add_all(
            [
                CollectionMeme(collection=collection, meme=source_meme, added_by_user_id=admin.id),
                PinnedMeme(user_id=admin.id, meme=source_meme, position=1),
            ]
        )
        await session.commit()
        source_meme_id = source_meme.id
        target_meme_id = target_meme.id
        source_file_id = source_file.id
        target_file_id = target_file.id
        collection_id = collection.id
        admin_id = admin.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/memes/{source_meme_id}/merge",
            json={
                "target_meme_id": str(target_meme_id),
                "confirmation": str(source_meme_id),
                "note": "Confirmed duplicate canonical merge",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "merge"
    assert payload["source_meme_id"] == str(source_meme_id)
    assert payload["target_meme_id"] == str(target_meme_id)
    assert payload["affected_snapshot"]["meme_files"]["ids"] == [str(source_file_id)]

    async with postgres_session_factory() as session:
        deleted_source = await session.get(Meme, source_meme_id)
        target = await session.get(Meme, target_meme_id)
        moved_file = await session.get(MemeFile, source_file_id)
        collection_link = await session.get(CollectionMeme, (collection_id, target_meme_id))
        pin_link = await session.get(PinnedMeme, (admin_id, target_meme_id))
        audit_log = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == source_meme_id),
        )
        merge_log = await session.scalar(select(MemeMergeLog).where(MemeMergeLog.source_meme_id == source_meme_id))

        assert deleted_source is None
        assert target is not None
        assert target.like_count == 7
        assert target.primary_file_id == target_file_id
        assert moved_file is not None
        assert moved_file.meme_id == target_meme_id
        assert collection_link is not None
        assert pin_link is not None
        assert audit_log is not None
        assert audit_log.admin_user_id == admin_id
        assert audit_log.action == "merge"
        assert audit_log.note == "Confirmed duplicate canonical merge"
        assert merge_log is not None
        assert merge_log.merge_reason == "admin_destructive_merge"
        assert merge_log.details["admin_user_id"] == str(admin_id)
        assert merge_log.details["admin_note"] == "Confirmed duplicate canonical merge"


async def test_admin_merge_self_is_blocked_without_partial_delete_or_audit(
    auth_app: FastAPI,
    auth_settings_overrides: dict[str, str],
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_token = await _issue_user_cookie(
        postgres_session_factory,
        auth_settings_overrides,
        email="admin-merge-blocked@example.com",
        is_admin=True,
    )

    async with postgres_session_factory() as session:
        meme, meme_file = _canonical_meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
        await _persist_canonical_meme(session, meme, meme_file)
        await session.commit()
        meme_id = meme.id

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as admin_client:
        admin_client.cookies.set(ACCESS_COOKIE_NAME, admin_token)
        response = await admin_client.post(
            f"/api/v1/admin/memes/{meme_id}/merge",
            json={"target_meme_id": str(meme_id), "confirmation": str(meme_id), "note": "try self merge"},
        )

    assert response.status_code == 409
    assert "cannot be merged into itself" in response.json()["detail"]

    async with postgres_session_factory() as session:
        persisted_meme = await session.get(Meme, meme_id)
        audit_log = await session.scalar(
            select(AdminMemeDestructiveAuditLog).where(AdminMemeDestructiveAuditLog.source_meme_id == meme_id),
        )

        assert persisted_meme is not None
        assert audit_log is None
