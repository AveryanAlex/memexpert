"""Integration coverage for privacy-bounded exposure and funnel facts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage, ContentProcessingStatus
from memexpert.models.user import MemeExposure
from memexpert.services.analytics import AnalyticsService, InteractionEventRefs, InteractionEventWrite
from memexpert.services.meme_exposure import MemeExposureService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.transactional_db]


async def test_exposure_stages_are_idempotent_and_keep_first_observation(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session)
    service = MemeExposureService(migrated_db_session)
    first = datetime(2026, 7, 20, 10, tzinfo=UTC)

    # Conversion can race ahead of the best-effort impression request.
    await service.record_web_detail_click(
        meme_id=meme.id,
        exposure_key=" imp_web_1 ",
        occurred_at=first + timedelta(seconds=2),
    )
    await service.record_web_exposure(
        meme_id=meme.id,
        exposure_key="imp_web_1",
        occurred_at=first,
    )
    await service.record_web_exposure(
        meme_id=meme.id,
        exposure_key="imp_web_1",
        occurred_at=first + timedelta(seconds=5),
    )
    await service.record_web_high_intent_action(
        meme_id=meme.id,
        exposure_key="imp_web_1",
        occurred_at=first + timedelta(seconds=3),
    )

    exposure = await migrated_db_session.scalar(
        select(MemeExposure).where(MemeExposure.exposure_key == "imp_web_1")
    )
    assert exposure is not None
    assert exposure.kind == "web_card"
    assert exposure.exposed_at == first
    assert exposure.detail_clicked_at == first + timedelta(seconds=2)
    assert exposure.high_intent_action_at == first + timedelta(seconds=3)
    assert exposure.inline_chosen_at is None
    assert exposure.inline_sent_at is None
    assert await migrated_db_session.scalar(select(func.count()).select_from(MemeExposure)) == 1


async def test_web_and_inline_placements_with_distinct_keys_remain_distinct(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session)
    service = MemeExposureService(migrated_db_session)

    await service.record_web_exposure(meme_id=meme.id, exposure_key="imp_web")
    await service.record_inline_exposure(meme_id=meme.id, exposure_key="imp_inline")
    await service.record_inline_chosen(meme_id=meme.id, exposure_key="imp_inline")
    await service.record_inline_sent(meme_id=meme.id, exposure_key="imp_inline")
    await service.record_web_exposure(meme_id=meme.id, exposure_key="")

    exposures = list(
        (
            await migrated_db_session.execute(
                select(MemeExposure).order_by(MemeExposure.kind, MemeExposure.exposure_key)
            )
        ).scalars()
    )
    assert [(item.kind, item.exposure_key) for item in exposures] == [
        ("telegram_inline", "imp_inline"),
        ("web_card", "imp_web"),
    ]
    assert exposures[0].inline_chosen_at is not None


async def test_strict_analytics_writer_projects_attributed_web_and_inline_funnels(
    migrated_db_session: AsyncSession,
) -> None:
    meme = await _create_meme(migrated_db_session)
    analytics = AnalyticsService(migrated_db_session)

    for event_type, surface, impression_id in (
        (AnalyticsEventType.MEME_IMPRESSION, "web_home", "imp_web"),
        (AnalyticsEventType.MEME_DETAIL_CLICK, "web_home", "imp_web"),
        (AnalyticsEventType.MEME_SAVE, "web_home", "imp_web"),
        (AnalyticsEventType.INLINE_SERVED, "telegram_inline", "imp_inline"),
        (AnalyticsEventType.INLINE_CHOSEN, "telegram_inline", "imp_inline"),
        (AnalyticsEventType.INLINE_SENT, "telegram_inline", "imp_inline"),
        # Compatibility mirrors must not create a web exposure.
        (AnalyticsEventType.MEME_SEND, "telegram_inline", "imp_inline"),
    ):
        await analytics.record_interaction_event(
            InteractionEventWrite(
                event_type=event_type,
                surface=surface,
                refs=InteractionEventRefs(meme_id=meme.id),
                impression_id=impression_id,
            )
        )

    web = await migrated_db_session.scalar(
        select(MemeExposure).where(MemeExposure.kind == "web_card")
    )
    inline = await migrated_db_session.scalar(
        select(MemeExposure).where(MemeExposure.kind == "telegram_inline")
    )
    assert web is not None
    assert web.exposed_at is not None
    assert web.detail_clicked_at is not None
    assert web.high_intent_action_at is not None
    assert inline is not None
    assert inline.exposed_at is not None
    assert inline.inline_chosen_at is not None
    assert inline.inline_sent_at is not None
    assert await migrated_db_session.scalar(select(func.count()).select_from(MemeExposure)) == 2


async def _create_meme(session: AsyncSession) -> Meme:
    meme_id = uuid.uuid7()
    file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=file_id,
        language=ContentLanguage.EN,
        tags=[],
        is_public=True,
    )
    file = MemeFile(
        id=file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"exposure/{meme_id}.jpg",
    )
    session.add(meme)
    await session.flush()
    session.add(file)
    await session.flush()
    return meme
