"""Service layer for the browser-admin API surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeTemplate, SourceChannel
from memexpert.models.user import ChannelSuggestion
from memexpert.schemas.admin import (
    AdminMemeRead,
    AdminMemeTemplateRead,
    AdminMemeTemplateUpdateRequest,
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

    async def update_meme_moderation(
        self,
        meme_id: uuid.UUID,
        *,
        is_nsfw: bool | None,
        is_public: bool | None,
    ) -> AdminMemeRead:
        meme = await self.session.get(Meme, meme_id)
        if meme is None:
            raise AdminNotFoundError(f"Meme {meme_id} does not exist.")
        if is_nsfw is not None:
            meme.is_nsfw = is_nsfw
        if is_public is not None:
            meme.is_public = is_public
        await self.session.commit()
        await self.session.refresh(meme)
        return AdminMemeRead.model_validate(meme)


__all__ = ["AdminConflictError", "AdminNotFoundError", "AdminService", "AdminServiceError"]
