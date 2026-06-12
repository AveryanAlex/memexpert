"""Service layer for the browser-admin API surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from memexpert.models.base import utcnow
from memexpert.models.collection import CollectionMeme, PinnedMeme
from memexpert.models.content import (
    AdminMemeDestructiveAuditLog,
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemePopularitySnapshot,
    MemeSeoPage,
    MemeSource,
    MemeTemplate,
    ModerationDecision,
    ModerationReport,
    PipelineStageJournal,
    SourceChannel,
    TelegramFileIdCache,
)
from memexpert.models.enums import ModerationAction, ModerationReportStatus
from memexpert.models.user import ChannelSuggestion
from memexpert.schemas.admin import (
    AdminMemeDeleteRequest,
    AdminMemeDestructiveActionRead,
    AdminMemeDetailRead,
    AdminMemeMergeRequest,
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
from memexpert.services.content_merge import ContentMergeService

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

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

        # Avoid primary-file FK ordering surprises before ORM/database cascades remove file rows.
        meme.primary_file_id = None
        await self.session.flush()
        audit_log_id = audit_log.id
        await self.session.delete(meme)
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

        file_bound_counts = await self._snapshot_file_bound_counts(meme.id, file_ids)
        return {
            "meme": {
                "id": str(meme.id),
                "primary_file_id": None if meme.primary_file_id is None else str(meme.primary_file_id),
                "media_type": meme.media_type.value,
                "language": meme.language.value,
                "is_public": meme.is_public,
                "is_nsfw": meme.is_nsfw,
                "template_id": None if meme.template_id is None else str(meme.template_id),
                "author_user_id": None if meme.author_user_id is None else str(meme.author_user_id),
                "like_count": meme.like_count,
                "popularity_score": meme.popularity_score,
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

    async def _snapshot_file_bound_counts(
        self,
        meme_id: uuid.UUID,
        file_ids: tuple[uuid.UUID, ...],
    ) -> dict[str, object]:
        popularity_count = await self.session.scalar(
            select(func.count()).select_from(MemePopularitySnapshot).where(MemePopularitySnapshot.meme_id == meme_id),
        )
        if not file_ids:
            return {
                "meme_sources": {"count": 0},
                "ocr_results": {"count": 0},
                "pipeline_stage_journal": {"count": 0},
                "popularity_snapshots": {"count": popularity_count or 0},
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
            "popularity_snapshots": {"count": popularity_count or 0},
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


__all__ = ["AdminConflictError", "AdminNotFoundError", "AdminService", "AdminServiceError"]
