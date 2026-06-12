"""Service layer for the browser-admin API surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeTemplate, ModerationDecision, ModerationReport, SourceChannel
from memexpert.models.enums import ModerationAction, ModerationReportStatus
from memexpert.models.user import ChannelSuggestion
from memexpert.schemas.admin import (
    AdminMemeDetailRead,
    AdminMemeModerationUpdateRequest,
    AdminMemeRead,
    AdminMemeTemplateRead,
    AdminMemeTemplateUpdateRequest,
    AdminModerationDecisionRead,
    AdminModerationReportRead,
    AdminModerationReportResolveRequest,
    AdminSourceChannelCreateRequest,
    AdminSourceChannelRead,
)
from memexpert.schemas.user import ChannelSuggestionRead

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.models.enums import ChannelSuggestionStatus


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
            await self.session.execute(select(SourceChannel).order_by(SourceChannel.title.asc()))
        ).scalars().all()
        return [AdminSourceChannelRead.model_validate(row) for row in rows]

    async def add_source_channel(self, request: AdminSourceChannelCreateRequest) -> AdminSourceChannelRead:
        channel = SourceChannel(
            platform=request.platform,
            platform_id=request.platform_id,
            username=request.username,
            title=request.title,
            subscriber_count=request.subscriber_count,
            session_id=request.session_id,
            catchup_enabled=request.catchup_enabled,
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
        return AdminSourceChannelRead.model_validate(channel)

    async def set_source_channel_paused(self, channel_id: uuid.UUID, *, is_paused: bool) -> AdminSourceChannelRead:
        channel = await self.session.get(SourceChannel, channel_id)
        if channel is None:
            raise AdminNotFoundError(f"Source channel {channel_id} does not exist.")
        if channel.is_paused != is_paused:
            channel.is_paused = is_paused
            await self.session.commit()
            await self.session.refresh(channel)
        return AdminSourceChannelRead.model_validate(channel)

    async def list_meme_templates(self) -> list[AdminMemeTemplateRead]:
        rows = (
            await self.session.execute(select(MemeTemplate).order_by(MemeTemplate.name.asc()))
        ).scalars().all()
        return [AdminMemeTemplateRead.model_validate(row) for row in rows]

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


__all__ = ["AdminConflictError", "AdminNotFoundError", "AdminService", "AdminServiceError"]
