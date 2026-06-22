"""Service layer for the browser-admin API surface."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import SecretStr
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.core.config import get_settings
from memexpert.core.perceptual_hashes import perceptual_hash_bit_size
from memexpert.crawlers.telegram.session_crypto import (
    TelegramStringSessionCipher,
    TelegramStringSessionDecryptError,
    TelegramStringSessionSecretError,
)
from memexpert.models.base import utcnow
from memexpert.models.collection import CollectionMeme, PinnedMeme
from memexpert.models.content import (
    AdminMemeDestructiveAuditLog,
    BlockedPerceptualHash,
    BlockedPerceptualHashAuditLog,
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeSeoPage,
    MemeSource,
    MemeTemplate,
    ModerationDecision,
    ModerationReport,
    PipelineStageJournal,
    SourceChannel,
    TelegramAdminAuditLog,
    TelegramFileIdCache,
    TelegramSession,
)
from memexpert.models.enums import ModerationAction, ModerationReportStatus, SourcePlatform, TelegramSessionStatus
from memexpert.models.user import ChannelSuggestion
from memexpert.schemas.admin import (
    AdminBlockedPerceptualHashActionRead,
    AdminBlockedPerceptualHashAuditRead,
    AdminBlockedPerceptualHashCreateRequest,
    AdminBlockedPerceptualHashDeactivateRequest,
    AdminBlockedPerceptualHashRead,
    AdminBlockedPerceptualHashUpdateRequest,
    AdminMemeDeleteRequest,
    AdminMemeDestructiveActionRead,
    AdminMemeDetailRead,
    AdminMemeMergeRequest,
    AdminMemeModerationUpdateRequest,
    AdminMemeRead,
    AdminMemeSeoEditRequest,
    AdminMemeSeoPageRead,
    AdminMemeSeoRegenerateRequest,
    AdminMemeSeoReviewRowRead,
    AdminMemeTemplateActionRead,
    AdminMemeTemplateCreateRequest,
    AdminMemeTemplateDeleteRequest,
    AdminMemeTemplateMergeRequest,
    AdminMemeTemplateRead,
    AdminMemeTemplateUpdateRequest,
    AdminModerationDecisionRead,
    AdminModerationReportRead,
    AdminModerationReportResolveRequest,
    AdminSourceChannelAssignRequest,
    AdminSourceChannelCreateRequest,
    AdminSourceChannelMarkDeadRequest,
    AdminSourceChannelOrphanRequest,
    AdminSourceChannelRead,
    AdminSourceChannelUpdateRequest,
    AdminTelegramChannelGroupRead,
    AdminTelegramSessionActionRead,
    AdminTelegramSessionCreateRequest,
    AdminTelegramSessionDeleteRequest,
    AdminTelegramSessionRead,
    AdminTelegramSessionUpdateRequest,
    AdminTelegramSessionValidateRead,
    AdminTelegramSessionValidateRequest,
)
from memexpert.schemas.user import ChannelSuggestionRead
from memexpert.services.content_merge import ContentMergeService
from memexpert.services.engagement_read_model import load_derived_popularity_scores
from memexpert.services.meme_seo import MemeSeoGenerationService

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from typing import Literal

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from memexpert.core.config import Settings
    from memexpert.models.enums import ChannelSuggestionStatus


MAX_AUDIT_SNAPSHOT_IDS = 25
ADMIN_MANUAL_SEO_PROVENANCE = "admin-manual"
MAX_SEO_TAG_LENGTH = 64
MAX_TELEGRAM_ERROR_TEXT_LENGTH = 4000
PENDING_TELEGRAM_SESSION_NAME_PREFIX = "pending_telegram_"
PENDING_TELEGRAM_SESSION_DISPLAY_NAME = "Pending Telegram login"


def pending_telegram_session_name() -> str:
    """Return a temporary unique key until login derives the Telegram user id."""

    return f"{PENDING_TELEGRAM_SESSION_NAME_PREFIX}{uuid.uuid7().hex}"


class AdminServiceError(Exception):
    """Base class for admin-service failures mapped at the route boundary."""


class AdminNotFoundError(AdminServiceError):
    """Raised when an admin target row does not exist."""


class AdminConflictError(AdminServiceError):
    """Raised when an admin mutation violates a durable uniqueness rule."""


class AdminTelegramValidationError(AdminServiceError):
    """Raised when a Telethon validation attempt cannot prove a session is usable."""

    def __init__(self, *, error_class: str, error_text: str) -> None:
        super().__init__(error_text)
        self.error_class = error_class[:128]
        self.error_text = error_text[:MAX_TELEGRAM_ERROR_TEXT_LENGTH]


@dataclass(frozen=True, slots=True)
class AdminTelegramAccountProjection:
    """Safe account fields extracted from Telegram ``get_me``."""

    user_id: int | None
    username: str | None
    phone_hint: str | None


@dataclass(frozen=True, slots=True)
class AdminTelegramValidationResult:
    """Secret-free validation result for a Telethon StringSession."""

    account: AdminTelegramAccountProjection
    channel_reference: str | None = None


async def validate_admin_telegram_string_session(
    *,
    settings: Settings,
    string_session: SecretStr,
    channel_reference: str | None = None,
) -> AdminTelegramValidationResult:
    """Validate StringSession material with Telethon without exposing the secret.

    The function is intentionally small and top-level so route/service tests can
    monkeypatch it instead of opening a real Telegram connection.
    """

    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if api_id is None or api_hash is None:
        raise AdminTelegramValidationError(
            error_class="TelegramConfigurationError",
            error_text="Telegram API credentials are not configured; set TELEGRAM_API_ID and TELEGRAM_API_HASH.",
        )

    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    try:
        client = TelegramClient(
            StringSession(string_session.get_secret_value()),
            api_id,
            api_hash.get_secret_value(),
        )
    except Exception as exc:
        raise AdminTelegramValidationError(
            error_class=type(exc).__name__,
            error_text="Provided Telegram StringSession material could not be loaded.",
        ) from exc

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise AdminTelegramValidationError(
                error_class="TelegramUnauthorizedError",
                error_text="Stored Telegram StringSession is not authorized.",
            )
        me = await client.get_me()
        if channel_reference is not None:
            await client.get_entity(channel_reference)
    except AdminTelegramValidationError:
        raise
    except Exception as exc:
        raise AdminTelegramValidationError(
            error_class=type(exc).__name__,
            error_text=f"Telegram validation failed with {type(exc).__name__}.",
        ) from exc
    finally:
        disconnect_result = client.disconnect()
        if inspect.isawaitable(disconnect_result):
            await disconnect_result

    user_id = getattr(me, "id", None)
    username = getattr(me, "username", None)
    phone = getattr(me, "phone", None)
    return AdminTelegramValidationResult(
        account=AdminTelegramAccountProjection(
            user_id=user_id if isinstance(user_id, int) else None,
            username=username.strip() if isinstance(username, str) and username.strip() else None,
            phone_hint=_phone_hint(phone if isinstance(phone, str) else None),
        ),
        channel_reference=channel_reference,
    )


def _phone_hint(phone: str | None) -> str | None:
    if phone is None:
        return None
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 4:
        return None
    return f"ending-{digits[-4:]}"


@dataclass(slots=True)
class AdminService:
    """Small admin orchestration service over current durable models."""

    session: AsyncSession

    async def list_channel_suggestions(
        self,
        *,
        status: ChannelSuggestionStatus | None = None,
    ) -> list[ChannelSuggestionRead]:
        stmt = select(ChannelSuggestion).order_by(ChannelSuggestion.created_at.desc())
        if status is not None:
            stmt = stmt.where(ChannelSuggestion.status == status)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [ChannelSuggestionRead.model_validate(row) for row in rows]

    async def review_channel_suggestion(
        self,
        suggestion_id: uuid.UUID,
        *,
        status: ChannelSuggestionStatus,
        admin_note: str | None,
    ) -> ChannelSuggestionRead:
        suggestion = await self.session.get(ChannelSuggestion, suggestion_id)
        if suggestion is None:
            raise AdminNotFoundError(f"Channel suggestion {suggestion_id} does not exist.")

        suggestion.status = status
        suggestion.admin_note = admin_note
        suggestion.reviewed_at = utcnow()
        await self.session.commit()
        await self.session.refresh(suggestion)
        return ChannelSuggestionRead.model_validate(suggestion)

    async def list_telegram_sessions(self) -> list[AdminTelegramSessionRead]:
        rows = (
            await self.session.execute(
                select(TelegramSession).order_by(TelegramSession.name.asc()),
            )
        ).scalars().all()
        counts_by_session = await self._count_source_channels_by_session()
        return [
            self._telegram_session_read(row, owned_channel_count=counts_by_session.get(row.id, 0))
            for row in rows
        ]

    async def create_telegram_session(
        self,
        request: AdminTelegramSessionCreateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramSessionRead:
        session_name = request.name or pending_telegram_session_name()
        display_name = request.display_name or request.name or PENDING_TELEGRAM_SESSION_DISPLAY_NAME
        row = TelegramSession(
            name=session_name,
            display_name=display_name,
            encrypted_string_session=None,
            account_user_id=None,
            account_username=None,
            account_phone_hint=None,
            status=TelegramSessionStatus.AUTH_REQUIRED,
            enabled=request.enabled,
            live_enabled=request.live_enabled,
            catchup_enabled=request.catchup_enabled,
            engagement_enabled=request.engagement_enabled,
            max_requests_per_second=request.max_requests_per_second,
            last_heartbeat_at=None,
        )
        self.session.add(row)
        try:
            await self.session.flush()
            self._add_telegram_admin_audit(
                admin_user_id=admin_user_id,
                action="session_create",
                telegram_session_id=row.id,
                source_channel_id=None,
                previous_values={},
                new_values=self._telegram_session_snapshot(row, owned_channel_count=0),
                note=request.note,
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError(f"Telegram session {session_name!r} already exists.") from exc
        await self.session.refresh(row)
        return self._telegram_session_read(row, owned_channel_count=0)

    async def update_telegram_session(
        self,
        session_id: uuid.UUID,
        request: AdminTelegramSessionUpdateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramSessionRead:
        row = await self.session.scalar(
            select(TelegramSession).where(TelegramSession.id == session_id).with_for_update(),
        )
        if row is None:
            raise AdminNotFoundError(f"Telegram session {session_id} does not exist.")

        previous_values = self._telegram_session_snapshot(row)
        for field_name in (
            "display_name",
            "enabled",
            "status",
            "live_enabled",
            "catchup_enabled",
            "engagement_enabled",
            "max_requests_per_second",
        ):
            if field_name in request.model_fields_set:
                setattr(row, field_name, getattr(request, field_name))
        if "flood_wait_until" in request.model_fields_set:
            row.flood_wait_until = request.flood_wait_until
        if "last_error_class" in request.model_fields_set:
            row.last_error_class = request.last_error_class
        if "last_error_text" in request.model_fields_set:
            row.last_error_text = request.last_error_text
        if request.clear_error:
            row.last_error_class = None
            row.last_error_text = None
            row.flood_wait_until = None
        if "status" in request.model_fields_set:
            if request.status is TelegramSessionStatus.ACTIVE:
                row.last_error_class = None
                row.last_error_text = None
                row.flood_wait_until = None
                row.quarantined_at = None
            elif request.status is TelegramSessionStatus.QUARANTINED and row.quarantined_at is None:
                row.quarantined_at = utcnow()
            elif request.status is not TelegramSessionStatus.QUARANTINED:
                row.quarantined_at = None

        self._add_telegram_admin_audit(
            admin_user_id=admin_user_id,
            action="session_patch",
            telegram_session_id=row.id,
            source_channel_id=None,
            previous_values=previous_values,
            new_values=self._telegram_session_snapshot(row),
            note=request.note,
        )
        await self.session.commit()
        await self.session.refresh(row)
        counts_by_session = await self._count_source_channels_by_session()
        return self._telegram_session_read(row, owned_channel_count=counts_by_session.get(row.id, 0))

    async def validate_telegram_session(
        self,
        session_id: uuid.UUID,
        request: AdminTelegramSessionValidateRequest,
    ) -> AdminTelegramSessionValidateRead:
        row = await self.session.scalar(
            select(TelegramSession).where(TelegramSession.id == session_id).with_for_update(),
        )
        if row is None:
            raise AdminNotFoundError(f"Telegram session {session_id} does not exist.")
        if not row.encrypted_string_session:
            row.status = TelegramSessionStatus.AUTH_REQUIRED
            row.last_error_class = "TelegramSessionMissingSecretError"
            row.last_error_text = "Telegram session has no stored StringSession material."
            await self.session.commit()
            raise AdminConflictError("Telegram session has no stored StringSession material.")

        channel_reference: str | None = None
        if request.source_channel_id is not None:
            channel = await self.session.get(SourceChannel, request.source_channel_id)
            if channel is None:
                raise AdminNotFoundError(f"Source channel {request.source_channel_id} does not exist.")
            if channel.platform is not SourcePlatform.TELEGRAM:
                raise AdminConflictError("Only Telegram source channels can be validated with Telegram sessions.")
            channel_reference = self._telegram_channel_reference(channel)

        try:
            string_session = self._decrypt_string_session(row.encrypted_string_session)
            validation = await validate_admin_telegram_string_session(
                settings=get_settings(),
                string_session=string_session,
                channel_reference=channel_reference,
            )
        except (TelegramStringSessionDecryptError, AdminTelegramValidationError) as exc:
            row.status = TelegramSessionStatus.AUTH_REQUIRED
            if isinstance(exc, AdminTelegramValidationError):
                row.last_error_class = exc.error_class
                row.last_error_text = exc.error_text[:MAX_TELEGRAM_ERROR_TEXT_LENGTH]
            else:
                row.last_error_class = type(exc).__name__
                row.last_error_text = str(exc)[:MAX_TELEGRAM_ERROR_TEXT_LENGTH]
            await self.session.commit()
            raise AdminConflictError(f"Telegram session validation failed: {row.last_error_class}.") from exc

        row.account_user_id = validation.account.user_id
        row.account_username = validation.account.username
        row.account_phone_hint = validation.account.phone_hint
        row.status = TelegramSessionStatus.ACTIVE
        row.last_error_class = None
        row.last_error_text = None
        row.flood_wait_until = None
        row.quarantined_at = None
        row.last_heartbeat_at = utcnow()
        await self.session.commit()
        await self.session.refresh(row)
        counts_by_session = await self._count_source_channels_by_session()
        return AdminTelegramSessionValidateRead(
            telegram_session=self._telegram_session_read(row, owned_channel_count=counts_by_session.get(row.id, 0)),
            channel_checked=channel_reference is not None,
            channel_reference=channel_reference,
        )

    async def delete_telegram_session(
        self,
        session_id: uuid.UUID,
        request: AdminTelegramSessionDeleteRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminTelegramSessionActionRead:
        row = await self.session.scalar(
            select(TelegramSession).where(TelegramSession.id == session_id).with_for_update(),
        )
        if row is None:
            raise AdminNotFoundError(f"Telegram session {session_id} does not exist.")
        if request.confirmation != str(session_id):
            raise AdminConflictError("Telegram session deletion confirmation must exactly match the session id.")

        previous_session_values = self._telegram_session_snapshot(row)
        channels = (
            await self.session.execute(
                select(SourceChannel)
                .where(SourceChannel.telegram_session_id == session_id)
                .order_by(SourceChannel.title.asc())
                .with_for_update(),
            )
        ).scalars().all()
        for channel in channels:
            previous_channel_values = self._source_channel_snapshot(channel)
            self._force_orphaned_channel_disabled(channel)
            self._add_telegram_admin_audit(
                admin_user_id=admin_user_id,
                action="channel_orphan",
                telegram_session_id=session_id,
                source_channel_id=channel.id,
                previous_values=previous_channel_values,
                new_values=self._source_channel_snapshot(channel),
                note="Orphaned by Telegram session deletion.",
            )
        self._add_telegram_admin_audit(
            admin_user_id=admin_user_id,
            action="session_delete",
            telegram_session_id=session_id,
            source_channel_id=None,
            previous_values={
                **previous_session_values,
                "orphaned_source_channel_ids": [str(channel.id) for channel in channels],
            },
            new_values={},
            note=request.note,
        )
        await self.session.delete(row)
        await self.session.commit()
        return AdminTelegramSessionActionRead(
            action="delete",
            telegram_session_id=session_id,
            orphaned_source_channel_count=len(channels),
            message="Telegram session deleted and assigned source channels orphaned.",
        )

    async def list_source_channels(
        self,
        *,
        platform: SourcePlatform | None = None,
        telegram_session_id: uuid.UUID | None = None,
        orphaned: bool | None = None,
    ) -> list[AdminSourceChannelRead]:
        if telegram_session_id is not None and await self.session.get(TelegramSession, telegram_session_id) is None:
            raise AdminNotFoundError(f"Telegram session {telegram_session_id} does not exist.")
        rows = (
            await self.session.execute(
                select(SourceChannel)
                .options(selectinload(SourceChannel.telegram_session))
                .where(
                    *self._source_channel_filters(
                        platform=platform,
                        telegram_session_id=telegram_session_id,
                        orphaned=orphaned,
                    ),
                )
                .order_by(SourceChannel.title.asc())
            )
        ).scalars().all()
        now = utcnow()
        return [self._source_channel_read(row, now=now) for row in rows]

    async def list_telegram_channel_groups(self) -> list[AdminTelegramChannelGroupRead]:
        sessions = (
            await self.session.execute(select(TelegramSession).order_by(TelegramSession.name.asc()))
        ).scalars().all()
        channels = (
            await self.session.execute(
                select(SourceChannel)
                .options(selectinload(SourceChannel.telegram_session))
                .where(SourceChannel.platform == SourcePlatform.TELEGRAM)
                .order_by(SourceChannel.title.asc()),
            )
        ).scalars().all()
        counts_by_session = await self._count_source_channels_by_session()
        now = utcnow()
        channels_by_session: dict[uuid.UUID, list[AdminSourceChannelRead]] = {row.id: [] for row in sessions}
        orphaned_channels: list[AdminSourceChannelRead] = []
        for channel in channels:
            channel_read = self._source_channel_read(channel, now=now)
            if channel.telegram_session_id is None:
                orphaned_channels.append(channel_read)
            else:
                channels_by_session.setdefault(channel.telegram_session_id, []).append(channel_read)
        groups = [
            AdminTelegramChannelGroupRead(
                telegram_session=self._telegram_session_read(
                    row,
                    owned_channel_count=counts_by_session.get(row.id, 0),
                ),
                is_orphaned=False,
                channels=channels_by_session.get(row.id, []),
            )
            for row in sessions
        ]
        groups.append(
            AdminTelegramChannelGroupRead(
                telegram_session=None,
                is_orphaned=True,
                channels=orphaned_channels,
            ),
        )
        return groups

    async def add_source_channel(
        self,
        request: AdminSourceChannelCreateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminSourceChannelRead:
        telegram_session = await self._resolve_telegram_session_target(
            telegram_session_id=request.telegram_session_id,
            telegram_session_name=request.telegram_session_name,
        )
        if telegram_session is not None and request.platform is not SourcePlatform.TELEGRAM:
            raise AdminConflictError("Only Telegram source channels can be assigned to Telegram sessions.")
        if telegram_session is None and not request.orphaned:
            raise AdminConflictError("Source channel creation requires telegram_session_id or orphaned=true.")
        catchup_enabled = request.catchup_enabled
        live_enabled = request.live_enabled
        engagement_enabled = request.engagement_enabled
        if request.orphaned:
            catchup_enabled = False
            live_enabled = False
            engagement_enabled = False
        channel = SourceChannel(
            platform=request.platform,
            platform_id=request.platform_id,
            username=request.username,
            title=request.title,
            subscriber_count=request.subscriber_count,
            telegram_session_id=None if telegram_session is None else telegram_session.id,
            catchup_enabled=catchup_enabled,
            live_enabled=live_enabled,
            engagement_enabled=engagement_enabled,
            catchup_message_limit=request.catchup_message_limit,
        )
        self.session.add(channel)
        try:
            await self.session.flush()
            self._add_telegram_admin_audit(
                admin_user_id=admin_user_id,
                action="channel_create",
                telegram_session_id=channel.telegram_session_id,
                source_channel_id=channel.id,
                previous_values={},
                new_values=self._source_channel_snapshot(channel),
                note=None,
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError(
                f"Source channel {request.platform.value}:{request.platform_id} already exists.",
            ) from exc
        await self.session.refresh(channel)
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def assign_source_channel(
        self,
        channel_id: uuid.UUID,
        request: AdminSourceChannelAssignRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminSourceChannelRead:
        telegram_session = await self.session.get(TelegramSession, request.telegram_session_id)
        if telegram_session is None:
            raise AdminNotFoundError(f"Telegram session {request.telegram_session_id} does not exist.")
        channel = await self.session.scalar(
            select(SourceChannel).where(SourceChannel.id == channel_id).with_for_update(),
        )
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if channel.platform is not SourcePlatform.TELEGRAM:
            raise AdminConflictError("Only Telegram source channels can be assigned to Telegram sessions.")
        previous_values = self._source_channel_snapshot(channel)
        channel.telegram_session_id = telegram_session.id
        self._add_telegram_admin_audit(
            admin_user_id=admin_user_id,
            action="channel_assign",
            telegram_session_id=telegram_session.id,
            source_channel_id=channel.id,
            previous_values=previous_values,
            new_values=self._source_channel_snapshot(channel),
            note=request.note,
        )
        await self.session.commit()
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def orphan_source_channel(
        self,
        channel_id: uuid.UUID,
        request: AdminSourceChannelOrphanRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminSourceChannelRead:
        channel = await self.session.scalar(
            select(SourceChannel).where(SourceChannel.id == channel_id).with_for_update(),
        )
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        previous_session_id = channel.telegram_session_id
        previous_values = self._source_channel_snapshot(channel)
        self._force_orphaned_channel_disabled(channel)
        self._add_telegram_admin_audit(
            admin_user_id=admin_user_id,
            action="channel_orphan",
            telegram_session_id=previous_session_id,
            source_channel_id=channel.id,
            previous_values=previous_values,
            new_values=self._source_channel_snapshot(channel),
            note=request.note,
        )
        await self.session.commit()
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def update_source_channel(
        self,
        channel_id: uuid.UUID,
        request: AdminSourceChannelUpdateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminSourceChannelRead:
        channel = await self.session.scalar(
            select(SourceChannel).where(SourceChannel.id == channel_id).with_for_update(),
        )
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if channel.telegram_session_id is None and any(
            getattr(request, field_name) is True
            for field_name in ("catchup_enabled", "live_enabled", "engagement_enabled")
            if field_name in request.model_fields_set
        ):
            raise AdminConflictError("Orphaned source channels cannot enable crawling or indexing controls.")
        previous_values = self._source_channel_snapshot(channel)
        for field_name in request.model_fields_set:
            setattr(channel, field_name, getattr(request, field_name))
        self._add_telegram_admin_audit(
            admin_user_id=admin_user_id,
            action="channel_update",
            telegram_session_id=channel.telegram_session_id,
            source_channel_id=channel.id,
            previous_values=previous_values,
            new_values=self._source_channel_snapshot(channel),
            note=None,
        )
        await self.session.commit()
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def set_source_channel_paused(
        self,
        channel_id: uuid.UUID,
        *,
        is_paused: bool,
        admin_user_id: uuid.UUID,
    ) -> AdminSourceChannelRead:
        channel = await self.session.get(SourceChannel, channel_id)
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if not channel.is_active:
            raise AdminConflictError(f"Source channel {channel_id} is marked dead and cannot be paused or resumed.")
        if channel.is_paused != is_paused:
            previous_values = self._source_channel_snapshot(channel)
            channel.is_paused = is_paused
            self._add_telegram_admin_audit(
                admin_user_id=admin_user_id,
                action="channel_pause" if is_paused else "channel_resume",
                telegram_session_id=channel.telegram_session_id,
                source_channel_id=channel.id,
                previous_values=previous_values,
                new_values=self._source_channel_snapshot(channel),
                note=None,
            )
            await self.session.commit()
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def mark_source_channel_dead(
        self,
        channel_id: uuid.UUID,
        request: AdminSourceChannelMarkDeadRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminSourceChannelRead:
        channel = await self.session.get(SourceChannel, channel_id)
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if request.confirmation != str(channel_id):
            raise AdminConflictError("Source channel mark-dead confirmation must exactly match the channel id.")
        if not channel.is_active:
            raise AdminConflictError(f"Source channel {channel_id} is already marked dead.")

        previous_values = self._source_channel_snapshot(channel)
        channel.is_active = False
        channel.is_paused = True
        self._add_telegram_admin_audit(
            admin_user_id=admin_user_id,
            action="channel_mark_dead",
            telegram_session_id=channel.telegram_session_id,
            source_channel_id=channel.id,
            previous_values=previous_values,
            new_values=self._source_channel_snapshot(channel),
            note=None,
        )
        await self.session.commit()
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def list_meme_templates(self) -> list[AdminMemeTemplateRead]:
        rows = (
            await self.session.execute(select(MemeTemplate).order_by(MemeTemplate.name.asc()))
        ).scalars().all()
        return [AdminMemeTemplateRead.model_validate(row) for row in rows]

    async def create_meme_template(self, request: AdminMemeTemplateCreateRequest) -> AdminMemeTemplateRead:
        template = MemeTemplate(
            slug=request.slug,
            name=request.name,
            description=request.description,
            is_curated=request.is_curated,
            base_image_url=request.base_image_url,
            text_regions=request.text_regions,
        )
        self.session.add(template)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError("Meme template slug already exists.") from exc
        await self.session.refresh(template)
        return AdminMemeTemplateRead.model_validate(template)

    async def update_meme_template(
        self,
        template_id: uuid.UUID,
        request: AdminMemeTemplateUpdateRequest,
    ) -> AdminMemeTemplateRead:
        template = await self.session.get(MemeTemplate, template_id)
        if template is None:
            raise AdminNotFoundError(f"Meme template {template_id} does not exist.")

        for field_name in request.model_fields_set:
            setattr(template, field_name, getattr(request, field_name))

        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError("Meme template slug already exists.") from exc
        await self.session.refresh(template)
        return AdminMemeTemplateRead.model_validate(template)

    async def merge_meme_template(
        self,
        template_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
        request: AdminMemeTemplateMergeRequest,
    ) -> AdminMemeTemplateActionRead:
        if request.confirmation != str(template_id):
            raise AdminConflictError("Template merge confirmation must exactly match the source template id.")
        if request.target_template_id == template_id:
            raise AdminConflictError("A meme template cannot be merged into itself.")

        source_template = await self.session.scalar(
            select(MemeTemplate).where(MemeTemplate.id == template_id).with_for_update(),
        )
        if source_template is None:
            raise AdminNotFoundError(f"Meme template {template_id} does not exist.")
        target_template = await self.session.scalar(
            select(MemeTemplate).where(MemeTemplate.id == request.target_template_id).with_for_update(),
        )
        if target_template is None:
            raise AdminNotFoundError(f"Target meme template {request.target_template_id} does not exist.")

        affected_memes = (
            await self.session.execute(select(Meme).where(Meme.template_id == template_id).with_for_update())
        ).scalars().all()
        source_label = f"{source_template.name} ({source_template.id})"
        target_label = f"{target_template.name} ({target_template.id})"
        decision_note = f"Template merge {source_label} -> {target_label}. {request.note}"

        for meme in affected_memes:
            previous_template_id = meme.template_id
            meme.template_id = request.target_template_id
            self.session.add(
                ModerationDecision(
                    meme=meme,
                    admin_user_id=admin_user_id,
                    action=ModerationAction.TEMPLATE_OVERRIDE,
                    reason=None,
                    note=decision_note,
                    previous_is_public=meme.is_public,
                    previous_is_nsfw=meme.is_nsfw,
                    new_is_public=meme.is_public,
                    new_is_nsfw=meme.is_nsfw,
                    previous_template_id=previous_template_id,
                    new_template_id=request.target_template_id,
                ),
            )

        affected_count = len(affected_memes)
        await self.session.flush()
        await self.session.delete(source_template)
        await self.session.commit()

        return AdminMemeTemplateActionRead(
            action="merge",
            source_template_id=template_id,
            target_template_id=request.target_template_id,
            affected_meme_count=affected_count,
            message="Template memes reassigned to target and duplicate template removed.",
        )

    async def delete_meme_template(
        self,
        template_id: uuid.UUID,
        request: AdminMemeTemplateDeleteRequest,
    ) -> AdminMemeTemplateActionRead:
        template = await self.session.scalar(
            select(MemeTemplate).where(MemeTemplate.id == template_id).with_for_update(),
        )
        if template is None:
            raise AdminNotFoundError(f"Meme template {template_id} does not exist.")
        if request.confirmation != str(template_id):
            raise AdminConflictError("Template deletion confirmation must exactly match the template id.")

        meme_count = await self.session.scalar(
            select(func.count()).select_from(Meme).where(Meme.template_id == template_id),
        )
        if meme_count:
            raise AdminConflictError(
                "Meme template is still referenced by memes; merge it into a target template first.",
            )
        decision_count = await self.session.scalar(
            select(func.count())
            .select_from(ModerationDecision)
            .where(
                or_(
                    ModerationDecision.previous_template_id == template_id,
                    ModerationDecision.new_template_id == template_id,
                ),
            ),
        )
        if decision_count:
            raise AdminConflictError(
                "Meme template is referenced by moderation decision history and cannot be deleted safely.",
            )

        await self.session.delete(template)
        await self.session.commit()
        return AdminMemeTemplateActionRead(
            action="delete",
            source_template_id=template_id,
            target_template_id=None,
            affected_meme_count=0,
            message="Unreferenced template deleted.",
        )

    async def list_blocked_perceptual_hashes(
        self,
        *,
        is_active: bool | None = None,
    ) -> list[AdminBlockedPerceptualHashRead]:
        stmt = select(BlockedPerceptualHash).order_by(BlockedPerceptualHash.created_at.desc())
        if is_active is not None:
            stmt = stmt.where(BlockedPerceptualHash.is_active.is_(is_active))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [AdminBlockedPerceptualHashRead.model_validate(row) for row in rows]

    async def create_blocked_perceptual_hash(
        self,
        request: AdminBlockedPerceptualHashCreateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminBlockedPerceptualHashRead:
        blocked_hash = BlockedPerceptualHash(
            perceptual_hash=request.perceptual_hash,
            hash_algorithm=request.hash_algorithm,
            hash_size=request.hash_size,
            max_hamming_distance=request.max_hamming_distance,
            reason=request.reason,
            note=request.note,
            is_active=request.is_active,
            created_by_admin_user_id=admin_user_id,
        )
        self.session.add(blocked_hash)
        try:
            await self.session.flush()
            self._add_blocked_hash_audit(
                blocked_hash,
                admin_user_id=admin_user_id,
                action="create",
                previous_values={},
                new_values=self._blocked_hash_snapshot(blocked_hash),
                note=request.note,
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError(
                "Blocked perceptual hash already exists for that algorithm and hash size.",
            ) from exc
        await self.session.refresh(blocked_hash)
        return AdminBlockedPerceptualHashRead.model_validate(blocked_hash)

    async def list_seo_review_rows(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminMemeSeoReviewRowRead]:
        missing_first = case((MemeSeoPage.meme_id.is_(None), 0), else_=1)
        newest_activity = func.coalesce(MemeSeoPage.edited_at, MemeSeoPage.generated_at, Meme.created_at)
        stmt = (
            select(Meme)
            .outerjoin(MemeSeoPage, MemeSeoPage.meme_id == Meme.id)
            .options(selectinload(Meme.seo_page))
            .where(Meme.is_public.is_(True), Meme.is_nsfw.is_(False))
            .order_by(missing_first.asc(), newest_activity.desc(), Meme.created_at.desc(), Meme.id.desc())
            .limit(limit)
            .offset(offset)
        )
        memes = (await self.session.execute(stmt)).scalars().all()
        popularity_scores = await self._load_admin_popularity_scores(memes)
        return [
            AdminMemeSeoReviewRowRead(
                meme=self._admin_meme_read(meme, popularity_score=popularity_scores.get(meme.id, 0.0)),
                seo_page=self._seo_page_read(meme.seo_page),
                status=self._seo_review_status(meme.seo_page),
            )
            for meme in memes
        ]

    async def edit_meme_seo_page(
        self,
        meme_id: uuid.UUID,
        request: AdminMemeSeoEditRequest,
    ) -> AdminMemeSeoPageRead:
        meme = await self.session.scalar(
            select(Meme)
            .where(Meme.id == meme_id)
            .options(selectinload(Meme.seo_page))
            .with_for_update(),
        )
        if meme is None:
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")

        now = utcnow()
        seo_page = meme.seo_page
        if seo_page is None:
            missing_fields = [
                field_name
                for field_name in ("slug", "page_title", "meta_description", "alt_text")
                if getattr(request, field_name) is None
            ]
            if missing_fields:
                raise AdminConflictError(
                    "Creating an SEO page requires slug, page_title, meta_description, and alt_text.",
                )
            seo_page = MemeSeoPage(
                meme_id=meme.id,
                slug=request.slug or "",
                page_title=request.page_title or "",
                meta_description=request.meta_description or "",
                alt_text=request.alt_text or "",
                caption=request.caption,
                body_text=request.body_text,
                tags=self._clean_seo_tags(request.tags or []),
                model_id=ADMIN_MANUAL_SEO_PROVENANCE,
                prompt_version=ADMIN_MANUAL_SEO_PROVENANCE,
                generated_at=now,
                edited_at=now,
            )
            meme.seo_page = seo_page
            self.session.add(seo_page)
        else:
            if "slug" in request.model_fields_set and request.slug is not None:
                seo_page.slug = request.slug
            if "page_title" in request.model_fields_set and request.page_title is not None:
                seo_page.page_title = request.page_title
            if "meta_description" in request.model_fields_set and request.meta_description is not None:
                seo_page.meta_description = request.meta_description
            if "alt_text" in request.model_fields_set and request.alt_text is not None:
                seo_page.alt_text = request.alt_text
            if "caption" in request.model_fields_set:
                seo_page.caption = request.caption
            if "body_text" in request.model_fields_set:
                seo_page.body_text = request.body_text
            if "tags" in request.model_fields_set:
                seo_page.tags = self._clean_seo_tags(request.tags or [])
            seo_page.edited_at = now

        if "tags" in request.model_fields_set:
            meme.tags = self._clean_seo_tags(request.tags or [])

        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError("SEO page slug already exists.") from exc
        await self.session.refresh(seo_page)
        return AdminMemeSeoPageRead.model_validate(seo_page)

    async def regenerate_meme_seo_page(
        self,
        meme_id: uuid.UUID,
        request: AdminMemeSeoRegenerateRequest,
    ) -> AdminMemeSeoPageRead:
        if request.confirmation != str(meme_id):
            raise AdminConflictError("SEO regeneration confirmation must exactly match the meme id.")

        generation_service = MemeSeoGenerationService(self.session)
        try:
            result = await generation_service.generate_for_meme_id(meme_id, force=True, commit=False)
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError("SEO page slug already exists.") from exc

        if result.status == "not_found":
            await self.session.rollback()
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")
        if result.status != "generated":
            await self.session.rollback()
            reason = f": {result.reason}" if result.reason else ""
            raise AdminConflictError(f"SEO regeneration {result.status}{reason}.")

        seo_page = await self.session.get(MemeSeoPage, meme_id)
        if seo_page is None:
            await self.session.rollback()
            raise AdminConflictError("SEO regeneration did not create an SEO page.")
        seo_page.edited_at = None
        await self.session.commit()
        await self.session.refresh(seo_page)
        return AdminMemeSeoPageRead.model_validate(seo_page)

    async def update_blocked_perceptual_hash(
        self,
        blocked_hash_id: uuid.UUID,
        request: AdminBlockedPerceptualHashUpdateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminBlockedPerceptualHashRead:
        blocked_hash = await self.session.scalar(
            select(BlockedPerceptualHash).where(BlockedPerceptualHash.id == blocked_hash_id).with_for_update(),
        )
        if blocked_hash is None:
            raise AdminNotFoundError(f"Blocked perceptual hash {blocked_hash_id} does not exist.")

        previous_values = self._blocked_hash_snapshot(blocked_hash)
        for field_name in request.model_fields_set:
            setattr(blocked_hash, field_name, getattr(request, field_name))
        if blocked_hash.hash_size != perceptual_hash_bit_size(blocked_hash.perceptual_hash):
            await self.session.rollback()
            raise AdminConflictError("hash_size must match perceptual_hash bit length.")
        if blocked_hash.max_hamming_distance > blocked_hash.hash_size:
            await self.session.rollback()
            raise AdminConflictError("max_hamming_distance cannot exceed hash_size.")

        try:
            self._add_blocked_hash_audit(
                blocked_hash,
                admin_user_id=admin_user_id,
                action="update",
                previous_values=previous_values,
                new_values=self._blocked_hash_snapshot(blocked_hash),
                note=request.note,
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError(
                "Blocked perceptual hash already exists for that algorithm and hash size.",
            ) from exc
        await self.session.refresh(blocked_hash)
        return AdminBlockedPerceptualHashRead.model_validate(blocked_hash)

    async def deactivate_blocked_perceptual_hash(
        self,
        blocked_hash_id: uuid.UUID,
        request: AdminBlockedPerceptualHashDeactivateRequest,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminBlockedPerceptualHashActionRead:
        blocked_hash = await self.session.scalar(
            select(BlockedPerceptualHash).where(BlockedPerceptualHash.id == blocked_hash_id).with_for_update(),
        )
        if blocked_hash is None:
            raise AdminNotFoundError(f"Blocked perceptual hash {blocked_hash_id} does not exist.")
        matched_count = await self._count_blocked_hash_matches(blocked_hash_id)
        if blocked_hash.is_active:
            previous_values = self._blocked_hash_snapshot(blocked_hash)
            blocked_hash.is_active = False
            self._add_blocked_hash_audit(
                blocked_hash,
                admin_user_id=admin_user_id,
                action="deactivate",
                previous_values=previous_values,
                new_values=self._blocked_hash_snapshot(blocked_hash),
                note=request.note,
            )
            await self.session.commit()
        return AdminBlockedPerceptualHashActionRead(
            action="deactivate",
            blocked_perceptual_hash_id=blocked_hash_id,
            matched_meme_file_count=matched_count,
            message="Blocked perceptual hash deactivated.",
        )

    async def delete_blocked_perceptual_hash_safe(
        self,
        blocked_hash_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
    ) -> AdminBlockedPerceptualHashActionRead:
        blocked_hash = await self.session.scalar(
            select(BlockedPerceptualHash).where(BlockedPerceptualHash.id == blocked_hash_id).with_for_update(),
        )
        if blocked_hash is None:
            raise AdminNotFoundError(f"Blocked perceptual hash {blocked_hash_id} does not exist.")
        matched_count = await self._count_blocked_hash_matches(blocked_hash_id)
        previous_values = self._blocked_hash_snapshot(blocked_hash)

        if matched_count:
            blocked_hash.is_active = False
            self._add_blocked_hash_audit(
                blocked_hash,
                admin_user_id=admin_user_id,
                action="deactivate",
                previous_values=previous_values,
                new_values=self._blocked_hash_snapshot(blocked_hash),
                note="Delete requested; deactivated because matched meme files still reference this blocked hash.",
            )
            await self.session.commit()
            return AdminBlockedPerceptualHashActionRead(
                action="deactivate",
                blocked_perceptual_hash_id=blocked_hash_id,
                matched_meme_file_count=matched_count,
                message=(
                    "Blocked perceptual hash is referenced by quarantined files, "
                    "so it was deactivated instead of deleted."
                ),
            )

        self._add_blocked_hash_audit(
            blocked_hash,
            admin_user_id=admin_user_id,
            action="delete",
            previous_values=previous_values,
            new_values={},
            note=None,
        )
        await self.session.flush()
        await self.session.delete(blocked_hash)
        await self.session.commit()
        return AdminBlockedPerceptualHashActionRead(
            action="delete",
            blocked_perceptual_hash_id=blocked_hash_id,
            matched_meme_file_count=0,
            message="Unreferenced blocked perceptual hash deleted; audit history preserved.",
        )

    async def list_blocked_perceptual_hash_audit(
        self,
        blocked_hash_id: uuid.UUID,
    ) -> list[AdminBlockedPerceptualHashAuditRead]:
        rows = (
            await self.session.execute(
                select(BlockedPerceptualHashAuditLog)
                .where(BlockedPerceptualHashAuditLog.blocked_perceptual_hash_id == blocked_hash_id)
                .order_by(BlockedPerceptualHashAuditLog.created_at.desc()),
            )
        ).scalars().all()
        return [AdminBlockedPerceptualHashAuditRead.model_validate(row) for row in rows]

    async def list_moderation_memes(
        self,
        *,
        is_nsfw: bool | None = None,
        is_public: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminMemeRead]:
        stmt = select(Meme).order_by(Meme.created_at.desc()).limit(limit).offset(offset)
        if is_nsfw is not None:
            stmt = stmt.where(Meme.is_nsfw.is_(is_nsfw))
        if is_public is not None:
            stmt = stmt.where(Meme.is_public.is_(is_public))
        rows = (await self.session.execute(stmt)).scalars().all()
        popularity_scores = await self._load_admin_popularity_scores(rows)
        return [self._admin_meme_read(row, popularity_score=popularity_scores.get(row.id, 0.0)) for row in rows]

    async def get_meme_detail(self, meme_id: uuid.UUID) -> AdminMemeDetailRead:
        meme = await self.session.get(Meme, meme_id)
        if meme is None:
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")

        reports = (
            await self.session.execute(
                select(ModerationReport)
                .options(selectinload(ModerationReport.meme))
                .where(ModerationReport.meme_id == meme_id)
                .order_by(ModerationReport.created_at.desc()),
            )
        ).scalars().all()
        decisions = (
            await self.session.execute(
                select(ModerationDecision)
                .where(ModerationDecision.meme_id == meme_id)
                .order_by(ModerationDecision.created_at.desc())
                .limit(100),
            )
        ).scalars().all()
        popularity_scores = await self._load_admin_popularity_scores([meme])
        meme_read = self._admin_meme_read(meme, popularity_score=popularity_scores.get(meme.id, 0.0))

        return AdminMemeDetailRead(
            meme=meme_read,
            reports=[self._admin_moderation_report_read(report, meme_read=meme_read) for report in reports],
            decisions=[AdminModerationDecisionRead.model_validate(decision) for decision in decisions],
        )

    async def delete_meme(
        self,
        meme_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
        request: AdminMemeDeleteRequest,
    ) -> AdminMemeDestructiveActionRead:
        meme = await self.session.scalar(select(Meme).where(Meme.id == meme_id).with_for_update())
        if meme is None:
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")
        if request.confirmation != str(meme_id):
            raise AdminConflictError("Meme deletion confirmation must exactly match the meme id.")

        affected_snapshot = await self._snapshot_meme_dependents(meme)
        audit_log = AdminMemeDestructiveAuditLog(
            admin_user_id=admin_user_id,
            source_meme_id=meme_id,
            target_meme_id=None,
            action="delete",
            note=request.note,
            affected_snapshot=affected_snapshot,
        )
        self.session.add(audit_log)
        await self.session.flush()

        audit_log_id = audit_log.id
        await self.session.execute(delete(Meme).where(Meme.id == meme_id))
        await self.session.commit()

        return AdminMemeDestructiveActionRead(
            action="delete",
            source_meme_id=meme_id,
            target_meme_id=None,
            audit_log_id=audit_log_id,
            affected_snapshot=affected_snapshot,
            message="Meme deleted and destructive audit log recorded.",
        )

    async def merge_meme(
        self,
        meme_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
        request: AdminMemeMergeRequest,
    ) -> AdminMemeDestructiveActionRead:
        source_meme = await self.session.scalar(select(Meme).where(Meme.id == meme_id).with_for_update())
        if source_meme is None:
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")
        if request.confirmation != str(meme_id):
            raise AdminConflictError("Meme merge confirmation must exactly match the source meme id.")
        if request.target_meme_id == meme_id:
            raise AdminConflictError("A meme cannot be merged into itself.")

        target_meme = await self.session.scalar(select(Meme).where(Meme.id == request.target_meme_id).with_for_update())
        if target_meme is None:
            raise AdminNotFoundError(f"Target meme {request.target_meme_id} does not exist.")

        affected_snapshot = await self._snapshot_meme_dependents(source_meme)
        affected_snapshot["target_meme_id"] = str(request.target_meme_id)
        audit_log = AdminMemeDestructiveAuditLog(
            admin_user_id=admin_user_id,
            source_meme_id=meme_id,
            target_meme_id=request.target_meme_id,
            action="merge",
            note=request.note,
            affected_snapshot=affected_snapshot,
        )
        self.session.add(audit_log)

        merge_service = ContentMergeService(self.session, similarity_threshold=1.0)
        await merge_service.merge_memes_for_admin(
            source_meme=source_meme,
            target_meme=target_meme,
            admin_user_id=admin_user_id,
            note=request.note,
            affected_snapshot=affected_snapshot,
        )
        await self.session.flush()
        audit_log_id = audit_log.id
        await self.session.commit()

        return AdminMemeDestructiveActionRead(
            action="merge",
            source_meme_id=meme_id,
            target_meme_id=request.target_meme_id,
            audit_log_id=audit_log_id,
            affected_snapshot=affected_snapshot,
            message="Meme merged into target and destructive audit log recorded.",
        )

    async def update_meme_moderation(
        self,
        meme_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
        request: AdminMemeModerationUpdateRequest,
    ) -> AdminMemeRead:
        meme = await self.session.get(Meme, meme_id)
        if meme is None:
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")

        previous_is_public = meme.is_public
        previous_is_nsfw = meme.is_nsfw
        previous_template_id = meme.template_id

        if "template_id" in request.model_fields_set and request.template_id is not None:
            template = await self.session.get(MemeTemplate, request.template_id)
            if template is None:
                raise AdminNotFoundError(f"Meme template {request.template_id} does not exist.")

        if request.is_nsfw is not None:
            meme.is_nsfw = request.is_nsfw
        if request.is_public is not None:
            meme.is_public = request.is_public
        if "template_id" in request.model_fields_set:
            meme.template_id = request.template_id

        flags_changed = previous_is_public != meme.is_public or previous_is_nsfw != meme.is_nsfw
        template_changed = previous_template_id != meme.template_id
        if flags_changed:
            self.session.add(
                ModerationDecision(
                    meme=meme,
                    admin_user_id=admin_user_id,
                    action=ModerationAction.OVERRIDE_FLAGS,
                    reason=request.reason,
                    note=request.note,
                    previous_is_public=previous_is_public,
                    previous_is_nsfw=previous_is_nsfw,
                    new_is_public=meme.is_public,
                    new_is_nsfw=meme.is_nsfw,
                    previous_template_id=previous_template_id,
                    new_template_id=meme.template_id,
                ),
            )
        if template_changed:
            self.session.add(
                ModerationDecision(
                    meme=meme,
                    admin_user_id=admin_user_id,
                    action=ModerationAction.TEMPLATE_OVERRIDE,
                    reason=request.reason,
                    note=request.note,
                    previous_is_public=meme.is_public,
                    previous_is_nsfw=meme.is_nsfw,
                    new_is_public=meme.is_public,
                    new_is_nsfw=meme.is_nsfw,
                    previous_template_id=previous_template_id,
                    new_template_id=meme.template_id,
                ),
            )
        await self.session.commit()
        await self.session.refresh(meme)
        popularity_scores = await self._load_admin_popularity_scores([meme])
        return self._admin_meme_read(meme, popularity_score=popularity_scores.get(meme.id, 0.0))

    async def list_moderation_reports(
        self,
        *,
        report_status: ModerationReportStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminModerationReportRead]:
        stmt = (
            select(ModerationReport)
            .options(selectinload(ModerationReport.meme))
            .order_by(ModerationReport.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        if report_status is None:
            stmt = stmt.where(
                ModerationReport.status.in_(
                    [ModerationReportStatus.PENDING, ModerationReportStatus.IN_REVIEW],
                ),
            )
        else:
            stmt = stmt.where(ModerationReport.status == report_status)
        rows = (await self.session.execute(stmt)).scalars().all()
        popularity_scores = await self._load_admin_popularity_scores([row.meme for row in rows])
        return [
            self._admin_moderation_report_read(
                row,
                meme_read=self._admin_meme_read(row.meme, popularity_score=popularity_scores.get(row.meme.id, 0.0)),
            )
            for row in rows
        ]

    async def resolve_moderation_report(
        self,
        report_id: uuid.UUID,
        *,
        admin_user_id: uuid.UUID,
        request: AdminModerationReportResolveRequest,
    ) -> AdminModerationReportRead:
        if request.action is ModerationAction.OVERRIDE_FLAGS:
            raise AdminConflictError("Use the direct meme moderation endpoint for override_flags decisions.")

        report = await self.session.scalar(
            select(ModerationReport)
            .options(selectinload(ModerationReport.meme))
            .where(ModerationReport.id == report_id),
        )
        if report is None:
            raise AdminNotFoundError(f"Moderation report {report_id} does not exist.")
        if report.status not in {ModerationReportStatus.PENDING, ModerationReportStatus.IN_REVIEW}:
            raise AdminConflictError(f"Moderation report {report_id} is already closed.")

        meme = report.meme
        previous_is_public = meme.is_public
        previous_is_nsfw = meme.is_nsfw
        self._apply_moderation_action(meme, request.action)

        report.status = (
            ModerationReportStatus.DISMISSED
            if request.action is ModerationAction.NO_ACTION
            else ModerationReportStatus.RESOLVED
        )
        report.resolved_by_admin_user_id = admin_user_id
        report.resolved_at = utcnow()
        decision = ModerationDecision(
            meme=meme,
            report=report,
            admin_user_id=admin_user_id,
            action=request.action,
            reason=request.reason or report.reason,
            note=request.note,
            previous_is_public=previous_is_public,
            previous_is_nsfw=previous_is_nsfw,
            new_is_public=meme.is_public,
            new_is_nsfw=meme.is_nsfw,
            previous_template_id=meme.template_id,
            new_template_id=meme.template_id,
        )
        self.session.add(decision)
        await self.session.commit()

        refreshed = await self.session.scalar(
            select(ModerationReport)
            .options(selectinload(ModerationReport.meme))
            .where(ModerationReport.id == report_id),
        )
        if refreshed is None:
            raise AdminNotFoundError(f"Moderation report {report_id} does not exist.")
        popularity_scores = await self._load_admin_popularity_scores([refreshed.meme])
        return self._admin_moderation_report_read(
            refreshed,
            meme_read=self._admin_meme_read(
                refreshed.meme,
                popularity_score=popularity_scores.get(refreshed.meme.id, 0.0),
            ),
        )

    async def list_moderation_decisions(
        self,
        *,
        meme_id: uuid.UUID | None = None,
        report_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminModerationDecisionRead]:
        stmt = select(ModerationDecision).order_by(ModerationDecision.created_at.desc()).limit(limit).offset(offset)
        if meme_id is not None:
            stmt = stmt.where(ModerationDecision.meme_id == meme_id)
        if report_id is not None:
            stmt = stmt.where(ModerationDecision.report_id == report_id)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [AdminModerationDecisionRead.model_validate(row) for row in rows]

    async def _snapshot_meme_dependents(self, meme: Meme) -> dict[str, object]:
        file_ids = tuple(
            (await self.session.execute(select(MemeFile.id).where(MemeFile.meme_id == meme.id))).scalars().all()
        )
        collection_ids = tuple(
            (
                await self.session.execute(
                    select(CollectionMeme.collection_id).where(CollectionMeme.meme_id == meme.id),
                )
            ).scalars().all()
        )
        pinned_user_ids = tuple(
            (
                await self.session.execute(select(PinnedMeme.user_id).where(PinnedMeme.meme_id == meme.id))
            ).scalars().all()
        )
        report_rows = (
            await self.session.execute(
                select(ModerationReport.id, ModerationReport.status).where(ModerationReport.meme_id == meme.id),
            )
        ).all()
        decision_rows = (
            await self.session.execute(
                select(ModerationDecision.id, ModerationDecision.action).where(ModerationDecision.meme_id == meme.id),
            )
        ).all()
        seo_slug = await self.session.scalar(select(MemeSeoPage.slug).where(MemeSeoPage.meme_id == meme.id))

        file_bound_counts = await self._snapshot_file_bound_counts(file_ids)
        return {
            "meme": {
                "id": str(meme.id),
                "primary_file_id": str(meme.primary_file_id),
                "media_type": meme.media_type.value,
                "language": meme.language.value,
                "is_public": meme.is_public,
                "is_nsfw": meme.is_nsfw,
                "template_id": None if meme.template_id is None else str(meme.template_id),
                "author_user_id": None if meme.author_user_id is None else str(meme.author_user_id),
                "like_count": meme.like_count,
                "tags": list(meme.tags[:MAX_AUDIT_SNAPSHOT_IDS]),
            },
            "meme_files": {"count": len(file_ids), "ids": self._bounded_uuid_strings(file_ids)},
            "seo_page": {"count": 0 if seo_slug is None else 1, "slug": seo_slug},
            "collection_saves": {
                "count": len(collection_ids),
                "collection_ids": self._bounded_uuid_strings(collection_ids),
            },
            "pins": {"count": len(pinned_user_ids), "user_ids": self._bounded_uuid_strings(pinned_user_ids)},
            "moderation_reports": {
                "count": len(report_rows),
                "items": [
                    {"id": str(report_id), "status": status.value}
                    for report_id, status in report_rows[:MAX_AUDIT_SNAPSHOT_IDS]
                ],
            },
            "moderation_decisions": {
                "count": len(decision_rows),
                "items": [
                    {"id": str(decision_id), "action": action.value}
                    for decision_id, action in decision_rows[:MAX_AUDIT_SNAPSHOT_IDS]
                ],
            },
            **file_bound_counts,
        }

    async def _load_admin_popularity_scores(self, memes: Iterable[Meme]) -> dict[uuid.UUID, float]:
        return await load_derived_popularity_scores(
            self.session,
            tuple(dict.fromkeys(meme.id for meme in memes)),
        )

    @staticmethod
    def _admin_meme_read(meme: Meme, *, popularity_score: float) -> AdminMemeRead:
        return AdminMemeRead(
            id=meme.id,
            media_type=meme.media_type,
            language=meme.language,
            is_nsfw=meme.is_nsfw,
            is_public=meme.is_public,
            popularity_score=popularity_score,
            like_count=meme.like_count,
            tags=list(meme.tags),
            template_id=meme.template_id,
            author_user_id=meme.author_user_id,
            created_at=meme.created_at,
            updated_at=meme.updated_at,
        )

    @staticmethod
    def _admin_moderation_report_read(
        report: ModerationReport,
        *,
        meme_read: AdminMemeRead,
    ) -> AdminModerationReportRead:
        return AdminModerationReportRead(
            id=report.id,
            meme_id=report.meme_id,
            reporter_user_id=report.reporter_user_id,
            status=report.status,
            reason=report.reason,
            note=report.note,
            resolved_by_admin_user_id=report.resolved_by_admin_user_id,
            resolved_at=report.resolved_at,
            created_at=report.created_at,
            updated_at=report.updated_at,
            meme=meme_read,
        )

    async def _count_blocked_hash_matches(self, blocked_hash_id: uuid.UUID) -> int:
        return await self.session.scalar(
            select(func.count()).select_from(MemeFile).where(MemeFile.blocked_perceptual_hash_id == blocked_hash_id),
        ) or 0

    def _add_blocked_hash_audit(
        self,
        blocked_hash: BlockedPerceptualHash,
        *,
        admin_user_id: uuid.UUID,
        action: str,
        previous_values: dict[str, object],
        new_values: dict[str, object],
        note: str | None,
    ) -> None:
        self.session.add(
            BlockedPerceptualHashAuditLog(
                blocked_perceptual_hash_id=blocked_hash.id,
                admin_user_id=admin_user_id,
                action=action,
                previous_values=previous_values,
                new_values=new_values,
                note=note,
            ),
        )

    @staticmethod
    def _blocked_hash_snapshot(blocked_hash: BlockedPerceptualHash) -> dict[str, object]:
        return {
            "id": str(blocked_hash.id),
            "perceptual_hash": blocked_hash.perceptual_hash,
            "hash_algorithm": blocked_hash.hash_algorithm,
            "hash_size": blocked_hash.hash_size,
            "max_hamming_distance": blocked_hash.max_hamming_distance,
            "reason": blocked_hash.reason.value,
            "note": blocked_hash.note,
            "is_active": blocked_hash.is_active,
            "created_by_admin_user_id": (
                None if blocked_hash.created_by_admin_user_id is None else str(blocked_hash.created_by_admin_user_id)
            ),
        }

    @staticmethod
    def _seo_page_read(seo_page: MemeSeoPage | None) -> AdminMemeSeoPageRead | None:
        if seo_page is None:
            return None
        return AdminMemeSeoPageRead.model_validate(seo_page)

    @staticmethod
    def _seo_review_status(seo_page: MemeSeoPage | None) -> Literal["missing", "generated", "edited"]:
        if seo_page is None:
            return "missing"
        return "edited" if seo_page.edited_at is not None else "generated"

    @staticmethod
    def _clean_seo_tags(tags: Iterable[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            normalized = "-".join(tag.strip().lower().split())[:MAX_SEO_TAG_LENGTH]
            if normalized and normalized not in seen:
                cleaned.append(normalized)
                seen.add(normalized)
        return cleaned

    async def _snapshot_file_bound_counts(
        self,
        file_ids: tuple[uuid.UUID, ...],
    ) -> dict[str, object]:
        if not file_ids:
            return {
                "meme_sources": {"count": 0},
                "ocr_results": {"count": 0},
                "pipeline_stage_journal": {"count": 0},
                "sync_target_snapshots": {"count": 0, "by_target": {}},
                "telegram_file_id_cache": {"count": 0},
                "embedding_cache_source_links": {"count": 0},
            }

        sync_rows = (
            await self.session.execute(
                select(MemeFileSyncTargetSnapshot.sync_target, func.count())
                .where(MemeFileSyncTargetSnapshot.meme_file_id.in_(file_ids))
                .group_by(MemeFileSyncTargetSnapshot.sync_target),
            )
        ).all()
        source_count = await self.session.scalar(
            select(func.count()).select_from(MemeSource).where(MemeSource.file_id.in_(file_ids)),
        )
        ocr_count = await self.session.scalar(
            select(func.count()).select_from(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id.in_(file_ids)),
        )
        stage_count = await self.session.scalar(
            select(func.count()).select_from(PipelineStageJournal).where(PipelineStageJournal.meme_file_id.in_(file_ids)),
        )
        telegram_count = await self.session.scalar(
            select(func.count()).select_from(TelegramFileIdCache).where(TelegramFileIdCache.meme_file_id.in_(file_ids)),
        )
        embedding_count = await self.session.scalar(
            select(func.count()).select_from(EmbeddingCache).where(EmbeddingCache.source_file_id.in_(file_ids)),
        )

        return {
            "meme_sources": {"count": source_count or 0},
            "ocr_results": {"count": ocr_count or 0},
            "pipeline_stage_journal": {"count": stage_count or 0},
            "sync_target_snapshots": {
                "count": sum(count for _, count in sync_rows),
                "by_target": {target.value: count for target, count in sync_rows},
            },
            "telegram_file_id_cache": {"count": telegram_count or 0},
            "embedding_cache_source_links": {"count": embedding_count or 0},
        }

    @staticmethod
    def _bounded_uuid_strings(values: Iterable[object]) -> list[str]:
        return [str(value) for value in tuple(values)[:MAX_AUDIT_SNAPSHOT_IDS]]

    def _apply_moderation_action(self, meme: Meme, action: ModerationAction) -> None:
        if action is ModerationAction.NO_ACTION:
            return
        if action is ModerationAction.MARK_NSFW:
            meme.is_nsfw = True
            return
        if action is ModerationAction.MARK_SFW:
            meme.is_nsfw = False
            return
        if action is ModerationAction.HIDE:
            meme.is_public = False
            return
        if action is ModerationAction.PUBLISH:
            meme.is_public = True
            return
        if action is ModerationAction.HIDE_AND_MARK_NSFW:
            meme.is_public = False
            meme.is_nsfw = True
            return
        raise AdminConflictError(f"Unsupported moderation action {action.value}.")

    @staticmethod
    def _source_channel_read(channel: SourceChannel, *, now: datetime | None = None) -> AdminSourceChannelRead:
        current_time = utcnow() if now is None else now
        operational_status: Literal["active", "inactive", "paused"] = (
            "inactive" if not channel.is_active else "paused" if channel.is_paused else "active"
        )
        is_orphaned = channel.telegram_session_id is None
        is_indexable = (
            not is_orphaned
            and channel.is_active
            and not channel.is_paused
            and (channel.catchup_enabled or channel.live_enabled or channel.engagement_enabled)
        )
        seconds_since_last_fetch: int | None = None
        if channel.last_fetched_at is None:
            freshness_status: Literal["checkpoint_only", "fresh", "never_fetched", "stale"] = (
                "checkpoint_only" if channel.last_read_post_id else "never_fetched"
            )
        else:
            seconds_since_last_fetch = max(0, int((current_time - channel.last_fetched_at).total_seconds()))
            freshness_status = "fresh" if seconds_since_last_fetch <= 24 * 60 * 60 else "stale"

        return AdminSourceChannelRead(
            id=channel.id,
            platform=channel.platform,
            platform_id=channel.platform_id,
            username=channel.username,
            title=channel.title,
            subscriber_count=channel.subscriber_count,
            is_active=channel.is_active,
            is_paused=channel.is_paused,
            catchup_enabled=channel.catchup_enabled,
            live_enabled=channel.live_enabled,
            engagement_enabled=channel.engagement_enabled,
            catchup_message_limit=channel.catchup_message_limit,
            telegram_session_id=channel.telegram_session_id,
            telegram_session_name=channel.telegram_session_name,
            is_orphaned=is_orphaned,
            is_indexable=is_indexable,
            last_read_post_id=channel.last_read_post_id,
            last_fetched_at=channel.last_fetched_at,
            operational_status=operational_status,
            freshness_status=freshness_status,
            seconds_since_last_fetch=seconds_since_last_fetch,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    async def _get_source_channel_for_read(self, channel_id: uuid.UUID) -> SourceChannel:
        channel = await self.session.scalar(
            select(SourceChannel)
            .options(selectinload(SourceChannel.telegram_session))
            .where(SourceChannel.id == channel_id)
            .execution_options(populate_existing=True)
            .limit(1),
        )
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        return channel

    async def _count_source_channels_by_session(self) -> dict[uuid.UUID, int]:
        rows = (
            await self.session.execute(
                select(SourceChannel.telegram_session_id, func.count(SourceChannel.id))
                .where(
                    SourceChannel.platform == SourcePlatform.TELEGRAM,
                    SourceChannel.telegram_session_id.is_not(None),
                )
                .group_by(SourceChannel.telegram_session_id),
            )
        ).all()
        return {telegram_session_id: count for telegram_session_id, count in rows if telegram_session_id is not None}

    @staticmethod
    def _source_channel_filters(
        *,
        platform: SourcePlatform | None,
        telegram_session_id: uuid.UUID | None,
        orphaned: bool | None,
    ) -> tuple[ColumnElement[bool], ...]:
        filters: list[ColumnElement[bool]] = []
        if platform is not None:
            filters.append(SourceChannel.platform == platform)
        if telegram_session_id is not None:
            filters.append(SourceChannel.telegram_session_id == telegram_session_id)
        if orphaned is True:
            filters.append(SourceChannel.telegram_session_id.is_(None))
        elif orphaned is False:
            filters.append(SourceChannel.telegram_session_id.is_not(None))
        return tuple(filters)

    async def _resolve_telegram_session_target(
        self,
        *,
        telegram_session_id: uuid.UUID | None,
        telegram_session_name: str | None,
    ) -> TelegramSession | None:
        if telegram_session_id is not None:
            telegram_session = await self.session.get(TelegramSession, telegram_session_id)
            if telegram_session is None:
                raise AdminNotFoundError(f"Telegram session {telegram_session_id} does not exist.")
            return telegram_session
        return await self._get_telegram_session_by_name(telegram_session_name)

    async def _get_telegram_session_by_name(self, name: str | None) -> TelegramSession | None:
        if name is None:
            return None
        telegram_session = await self.session.scalar(
            select(TelegramSession)
            .where(TelegramSession.name == name)
            .limit(1),
        )
        if telegram_session is None:
            raise AdminConflictError(f"Telegram session {name!r} does not exist.")
        return telegram_session

    @staticmethod
    def _telegram_session_read(
        row: TelegramSession,
        *,
        owned_channel_count: int,
    ) -> AdminTelegramSessionRead:
        return AdminTelegramSessionRead(
            id=row.id,
            name=row.name,
            display_name=row.display_name,
            owned_channel_count=owned_channel_count,
            status=row.status,
            enabled=row.enabled,
            flood_wait_until=row.flood_wait_until,
            live_listener_started_at=row.live_listener_started_at,
            last_heartbeat_at=row.last_heartbeat_at,
            last_error_class=row.last_error_class,
            last_error_text=row.last_error_text,
            quarantined_at=row.quarantined_at,
            live_enabled=row.live_enabled,
            catchup_enabled=row.catchup_enabled,
            engagement_enabled=row.engagement_enabled,
            max_requests_per_second=row.max_requests_per_second,
            account_user_id=row.account_user_id,
            account_username=row.account_username,
            account_phone_hint=row.account_phone_hint,
            has_string_session=bool((row.encrypted_string_session or "").strip()),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _telegram_session_snapshot(
        row: TelegramSession,
        *,
        owned_channel_count: int | None = None,
    ) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "id": str(row.id),
            "name": row.name,
            "display_name": row.display_name,
            "account_user_id": row.account_user_id,
            "account_username": row.account_username,
            "account_phone_hint": row.account_phone_hint,
            "status": row.status.value,
            "enabled": row.enabled,
            "last_error_class": row.last_error_class,
            "last_error_text": row.last_error_text,
            "flood_wait_until": None if row.flood_wait_until is None else row.flood_wait_until.isoformat(),
            "live_listener_started_at": (
                None if row.live_listener_started_at is None else row.live_listener_started_at.isoformat()
            ),
            "last_heartbeat_at": None if row.last_heartbeat_at is None else row.last_heartbeat_at.isoformat(),
            "quarantined_at": None if row.quarantined_at is None else row.quarantined_at.isoformat(),
            "live_enabled": row.live_enabled,
            "catchup_enabled": row.catchup_enabled,
            "engagement_enabled": row.engagement_enabled,
            "max_requests_per_second": row.max_requests_per_second,
            "has_string_session": bool((row.encrypted_string_session or "").strip()),
        }
        if owned_channel_count is not None:
            snapshot["owned_channel_count"] = owned_channel_count
        return snapshot

    @staticmethod
    def _source_channel_snapshot(channel: SourceChannel) -> dict[str, object]:
        return {
            "id": str(channel.id),
            "platform": channel.platform.value,
            "platform_id": channel.platform_id,
            "username": channel.username,
            "title": channel.title,
            "subscriber_count": channel.subscriber_count,
            "is_active": channel.is_active,
            "is_paused": channel.is_paused,
            "catchup_enabled": channel.catchup_enabled,
            "live_enabled": channel.live_enabled,
            "engagement_enabled": channel.engagement_enabled,
            "catchup_message_limit": channel.catchup_message_limit,
            "telegram_session_id": None if channel.telegram_session_id is None else str(channel.telegram_session_id),
            "is_orphaned": channel.telegram_session_id is None,
            "last_read_post_id": channel.last_read_post_id,
            "last_fetched_at": None if channel.last_fetched_at is None else channel.last_fetched_at.isoformat(),
        }

    @staticmethod
    def _force_orphaned_channel_disabled(channel: SourceChannel) -> None:
        channel.telegram_session_id = None
        channel.catchup_enabled = False
        channel.live_enabled = False
        channel.engagement_enabled = False

    @staticmethod
    def _telegram_channel_reference(channel: SourceChannel) -> str:
        username = (channel.username or "").strip()
        if username:
            return username if username.startswith("@") else f"@{username}"
        return channel.platform_id

    def _encrypt_string_session(self, string_session: SecretStr) -> str:
        try:
            return TelegramStringSessionCipher(get_settings().telegram_session_encryption_secret).encrypt(
                string_session,
            ).get_secret_value()
        except TelegramStringSessionSecretError as exc:
            raise AdminConflictError("Telegram StringSession material could not be encrypted.") from exc

    def _decrypt_string_session(self, encrypted_string_session: str) -> SecretStr:
        return TelegramStringSessionCipher(get_settings().telegram_session_encryption_secret).decrypt(
            SecretStr(encrypted_string_session),
        )

    def _add_telegram_admin_audit(
        self,
        *,
        admin_user_id: uuid.UUID,
        action: str,
        telegram_session_id: uuid.UUID | None,
        source_channel_id: uuid.UUID | None,
        previous_values: dict[str, object],
        new_values: dict[str, object],
        note: str | None,
    ) -> None:
        self.session.add(
            TelegramAdminAuditLog(
                admin_user_id=admin_user_id,
                action=action,
                telegram_session_id=telegram_session_id,
                source_channel_id=source_channel_id,
                previous_values=previous_values,
                new_values=new_values,
                note=note,
            ),
        )


__all__ = [
    "AdminConflictError",
    "AdminNotFoundError",
    "AdminService",
    "AdminServiceError",
    "AdminTelegramAccountProjection",
    "AdminTelegramValidationError",
    "AdminTelegramValidationResult",
    "validate_admin_telegram_string_session",
]
