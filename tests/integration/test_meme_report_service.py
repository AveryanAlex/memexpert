"""Service-level tests for user-facing moderation report creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from memexpert.models.content import Meme, ModerationReport
from memexpert.models.enums import ContentKind, ModerationReason
from memexpert.models.user import User
from memexpert.schemas.report import MemeReportCreateRequest
from memexpert.services.report import MemeReportService, MemeReportTargetNotVisibleError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_report_service_reuses_open_report_and_preserves_original_payload(
    migrated_db_session: AsyncSession,
) -> None:
    reporter = User()
    meme = Meme(media_type=ContentKind.IMAGE, is_public=True, is_nsfw=False)
    migrated_db_session.add_all([reporter, meme])
    await migrated_db_session.flush()
    reporter_id = reporter.id
    meme_id = meme.id
    await migrated_db_session.commit()
    service = MemeReportService(session=migrated_db_session)

    first = await service.report_meme(
        meme_id,
        reporter_user_id=reporter_id,
        reporter_nsfw_enabled=False,
        request=MemeReportCreateRequest(reason=ModerationReason.SPAM, note=" first "),
    )
    second = await service.report_meme(
        meme_id,
        reporter_user_id=reporter_id,
        reporter_nsfw_enabled=False,
        request=MemeReportCreateRequest(reason=ModerationReason.ILLEGAL, note="second"),
    )

    assert second.id == first.id
    assert second.reason is ModerationReason.SPAM
    assert second.note == "first"

    report_count = await migrated_db_session.scalar(select(func.count()).select_from(ModerationReport))
    assert report_count == 1


async def test_report_service_rejects_unavailable_public_target(
    migrated_db_session: AsyncSession,
) -> None:
    reporter = User()
    meme = Meme(media_type=ContentKind.IMAGE, is_public=False, is_nsfw=False)
    migrated_db_session.add_all([reporter, meme])
    await migrated_db_session.flush()
    reporter_id = reporter.id
    meme_id = meme.id
    await migrated_db_session.commit()
    service = MemeReportService(session=migrated_db_session)

    with pytest.raises(MemeReportTargetNotVisibleError):
        await service.report_meme(
            meme_id,
            reporter_user_id=reporter_id,
            reporter_nsfw_enabled=False,
            request=MemeReportCreateRequest(reason=ModerationReason.OTHER, note=None),
        )

    report_count = await migrated_db_session.scalar(select(func.count()).select_from(ModerationReport))
    assert report_count == 0
