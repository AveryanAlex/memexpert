"""Service layer for the browser-admin API surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.core.perceptual_hashes import perceptual_hash_bit_size
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
    TelegramFileIdCache,
    TelegramSession,
)
from memexpert.models.enums import ModerationAction, ModerationReportStatus
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
    AdminMemeTemplateActionRead,
    AdminMemeTemplateCreateRequest,
    AdminMemeTemplateDeleteRequest,
    AdminMemeTemplateMergeRequest,
    AdminMemeTemplateRead,
    AdminMemeTemplateUpdateRequest,
    AdminModerationDecisionRead,
    AdminModerationReportRead,
    AdminModerationReportResolveRequest,
    AdminSourceChannelCreateRequest,
    AdminSourceChannelRead,
)
from memexpert.schemas.user import ChannelSuggestionRead
from memexpert.services.content_merge import ContentMergeService

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable
    from datetime import datetime
    from typing import Literal

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.models.enums import ChannelSuggestionStatus


MAX_AUDIT_SNAPSHOT_IDS = 25


class AdminServiceError(Exception):
    """Base class for admin-service failures mapped at the route boundary."""


class AdminNotFoundError(AdminServiceError):
    """Raised when an admin target row does not exist."""


class AdminConflictError(AdminServiceError):
    """Raised when an admin mutation violates a durable uniqueness rule."""


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

    async def list_source_channels(self) -> list[AdminSourceChannelRead]:
        rows = (
            await self.session.execute(
                select(SourceChannel)
                .options(selectinload(SourceChannel.telegram_session))
                .order_by(SourceChannel.title.asc())
            )
        ).scalars().all()
        now = utcnow()
        return [self._source_channel_read(row, now=now) for row in rows]

    async def add_source_channel(self, request: AdminSourceChannelCreateRequest) -> AdminSourceChannelRead:
        telegram_session = await self._get_telegram_session_by_name(request.telegram_session_name)
        channel = SourceChannel(
            platform=request.platform,
            platform_id=request.platform_id,
            username=request.username,
            title=request.title,
            subscriber_count=request.subscriber_count,
            telegram_session_id=None if telegram_session is None else telegram_session.id,
            catchup_enabled=request.catchup_enabled,
            live_enabled=request.live_enabled,
            engagement_enabled=request.engagement_enabled,
            catchup_message_limit=request.catchup_message_limit,
        )
        self.session.add(channel)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AdminConflictError(
                f"Source channel {request.platform.value}:{request.platform_id} already exists.",
            ) from exc
        await self.session.refresh(channel)
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def set_source_channel_paused(self, channel_id: uuid.UUID, *, is_paused: bool) -> AdminSourceChannelRead:
        channel = await self.session.get(SourceChannel, channel_id)
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if not channel.is_active:
            raise AdminConflictError(f"Source channel {channel_id} is marked dead and cannot be paused or resumed.")
        if channel.is_paused != is_paused:
            channel.is_paused = is_paused
            await self.session.commit()
        return self._source_channel_read(await self._get_source_channel_for_read(channel.id))

    async def mark_source_channel_dead(self, channel_id: uuid.UUID) -> AdminSourceChannelRead:
        channel = await self.session.get(SourceChannel, channel_id)
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if not channel.is_active:
            raise AdminConflictError(f"Source channel {channel_id} is already marked dead.")

        channel.is_active = False
        channel.is_paused = True
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
        return [AdminMemeRead.model_validate(row) for row in rows]

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

        return AdminMemeDetailRead(
            meme=AdminMemeRead.model_validate(meme),
            reports=[AdminModerationReportRead.model_validate(report) for report in reports],
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
        return AdminMemeRead.model_validate(meme)

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
        return [AdminModerationReportRead.model_validate(row) for row in rows]

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
        return AdminModerationReportRead.model_validate(refreshed)

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


__all__ = ["AdminConflictError", "AdminNotFoundError", "AdminService", "AdminServiceError"]
