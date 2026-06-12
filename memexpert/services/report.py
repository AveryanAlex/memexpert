"""Service boundary for user-facing meme moderation reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.content import Meme, ModerationReport
from memexpert.models.enums import ModerationReportStatus
from memexpert.schemas.report import MemeReportCreateRequest, MemeReportRead

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


class MemeReportServiceError(Exception):
    """Base class for meme-report service failures."""


class MemeReportTargetNotVisibleError(MemeReportServiceError):
    """Raised when the meme is absent or filtered from the public reporting surface."""


@dataclass(slots=True)
class MemeReportService:
    """Create or reuse user-submitted moderation reports for public memes."""

    session: AsyncSession

    async def report_meme(
        self,
        meme_id: uuid.UUID,
        *,
        reporter_user_id: uuid.UUID,
        reporter_nsfw_enabled: bool,
        request: MemeReportCreateRequest,
    ) -> MemeReportRead:
        meme = await self.session.scalar(
            select(Meme).where(Meme.id == meme_id, Meme.is_public.is_(True)),
        )
        if meme is None or (meme.is_nsfw and not reporter_nsfw_enabled):
            raise MemeReportTargetNotVisibleError("Meme was not found or is not visible to this caller.")

        existing_report = await self.session.scalar(
            select(ModerationReport)
            .where(
                ModerationReport.meme_id == meme_id,
                ModerationReport.reporter_user_id == reporter_user_id,
                ModerationReport.status.in_(
                    (ModerationReportStatus.PENDING, ModerationReportStatus.IN_REVIEW),
                ),
            )
            .order_by(ModerationReport.created_at.asc()),
        )
        if existing_report is not None:
            return MemeReportRead.model_validate(existing_report)

        report = ModerationReport(
            meme_id=meme_id,
            reporter_user_id=reporter_user_id,
            reason=request.reason,
            note=request.note,
        )
        self.session.add(report)
        await self.session.commit()
        await self.session.refresh(report)
        return MemeReportRead.model_validate(report)


__all__ = ["MemeReportService", "MemeReportServiceError", "MemeReportTargetNotVisibleError"]
