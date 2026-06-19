# ruff: noqa: TC002
"""Focused integration tests for the strict analytics interaction writer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.models.content import Meme, MemeFile, MemeTemplate
from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage, ContentProcessingStatus
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, User
from memexpert.services.analytics import (
    AnalyticsService,
    InteractionActorType,
    InteractionEventRefs,
    InteractionEventWrite,
)
from tests.factories import build_full_user

pytestmark = pytest.mark.asyncio


async def _create_profile_stats_meme(
    session: AsyncSession,
    *,
    tags: list[str] | None = None,
    template: MemeTemplate | None = None,
) -> Meme:
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    meme = Meme(
        id=meme_id,
        media_type=ContentKind.IMAGE,
        primary_file_id=meme_file_id,
        language=ContentLanguage.EN,
        tags=tags or [],
        template_id=template.id if template else None,
        is_public=True,
    )
    meme_file = MemeFile(
        id=meme_file_id,
        meme_id=meme_id,
        status=ContentProcessingStatus.READY,
        s3_original_key=f"profile-stats/originals/{meme_id}.jpg",
    )
    session.add(meme)
    await session.flush()
    session.add(meme_file)
    await session.flush()
    return meme


def _profile_stats_event(
    event_type: AnalyticsEventType,
    *,
    user_id: uuid.UUID,
    occurred_at: datetime,
    meme_id: uuid.UUID | None = None,
) -> AnalyticsEvent:
    payload: dict[str, object] = {"refs": {"meme_id": str(meme_id)}} if meme_id is not None else {}
    return AnalyticsEvent(user_id=user_id, event_type=event_type, payload=payload, occurred_at=occurred_at)


async def test_record_interaction_event_persists_full_user_refs_and_utc_timestamp(
    migrated_db_session: AsyncSession,
) -> None:
    user = build_full_user()
    migrated_db_session.add(user)
    await migrated_db_session.flush()

    service = AnalyticsService(migrated_db_session)
    meme_id = uuid.uuid7()
    source_meme_id = uuid.uuid7()
    collection_id = uuid.uuid7()
    occurred_at = datetime(2026, 1, 8, 15, 30, tzinfo=timezone(timedelta(hours=3)))

    await service.record_interaction_event(
        InteractionEventWrite(
            event_type=AnalyticsEventType.MEME_DOWNLOAD,
            user_id=user.id,
            surface="public_api",
            refs=InteractionEventRefs(
                meme_id=meme_id,
                source_meme_id=source_meme_id,
                collection_id=collection_id,
            ),
            request_id="req-download-1",
            impression_id="imp-download-1",
            source_algorithm="trending",
            rank=2,
            score=1.25,
            score_components={"event_boost": 0.5, "popularity": 0.75},
            reason="detail_download",
            properties={
                "chat_hash": "hashed-chat",
                "result_count": 1,
                "filters": {"tags": ["frog"]},
            },
            occurred_at=occurred_at,
        )
    )

    event = await migrated_db_session.scalar(
        select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.MEME_DOWNLOAD)
    )

    assert event is not None
    assert event.user_id == user.id
    assert event.occurred_at == occurred_at.astimezone(UTC)
    assert event.payload == {
        "schema_version": 1,
        "actor_type": "user",
        "actor_account_type": "full",
        "surface": "public_api",
        "refs": {
            "meme_id": str(meme_id),
            "source_meme_id": str(source_meme_id),
            "collection_id": str(collection_id),
        },
        "request_id": "req-download-1",
        "impression_id": "imp-download-1",
        "source_algorithm": "trending",
        "rank": 2,
        "score": 1.25,
        "score_components": {"event_boost": 0.5, "popularity": 0.75},
        "reason": "detail_download",
        "properties": {
            "chat_hash": "hashed-chat",
            "result_count": 1,
            "filters": {"tags": ["frog"]},
        },
    }


async def test_record_interaction_event_supports_guest_anonymous_and_system_actor_contexts(
    migrated_db_session: AsyncSession,
) -> None:
    guest_user = User()
    full_user = build_full_user()
    merge_log = AccountMergeLog(
        guest_account_id=uuid.uuid7(),
        target_account_id=uuid.uuid7(),
    )
    migrated_db_session.add_all([guest_user, full_user, merge_log])
    await migrated_db_session.flush()

    service = AnalyticsService(migrated_db_session)
    meme_id = uuid.uuid7()

    await service.record_interaction_event(
        {
            "event_type": AnalyticsEventType.MEME_VIEW,
            "user_id": guest_user.id,
            "surface": "web_detail",
            "refs": {"meme_id": meme_id},
        }
    )
    await service.record_interaction_event(
        {
            "event_type": AnalyticsEventType.MEME_IMPRESSION,
            "surface": "web_home",
            "refs": {"meme_id": meme_id},
            "properties": {"chat_hash": "anonymous-hash"},
        }
    )
    await service.record_interaction_event(
        {
            "event_type": AnalyticsEventType.ACCOUNT_MERGE,
            "actor_type": InteractionActorType.SYSTEM,
            "surface": "account_merge_job",
            "refs": {
                "account_merge_log_id": merge_log.id,
                "source_user_id": guest_user.id,
                "target_user_id": full_user.id,
            },
        }
    )

    events = list(
        (
            await migrated_db_session.execute(
                select(AnalyticsEvent).order_by(AnalyticsEvent.occurred_at.asc(), AnalyticsEvent.id.asc())
            )
        ).scalars()
    )

    assert [event.event_type for event in events] == [
        AnalyticsEventType.MEME_VIEW,
        AnalyticsEventType.MEME_IMPRESSION,
        AnalyticsEventType.ACCOUNT_MERGE,
    ]
    assert events[0].user_id == guest_user.id
    assert events[0].payload["actor_type"] == "user"
    assert events[0].payload["actor_account_type"] == "guest"
    assert events[1].user_id is None
    assert events[1].payload["actor_type"] == "anonymous"
    assert "actor_account_type" not in events[1].payload
    assert events[2].user_id is None
    assert events[2].payload["actor_type"] == "system"
    assert events[2].payload["refs"] == {
        "account_merge_log_id": str(merge_log.id),
        "source_user_id": str(guest_user.id),
        "target_user_id": str(full_user.id),
    }


async def test_record_interaction_event_rejects_unsafe_payloads_before_insert(
    migrated_db_session: AsyncSession,
) -> None:
    user = build_full_user()
    migrated_db_session.add(user)
    await migrated_db_session.flush()

    service = AnalyticsService(migrated_db_session)
    meme_id = uuid.uuid7()

    with pytest.raises(ValidationError):
        await service.record_interaction_event(
            {
                "event_type": AnalyticsEventType.MEME_DOWNLOAD,
                "user_id": user.id,
                "surface": "public_api",
                "refs": {"meme_id": meme_id},
                "properties": {"chat_id": "raw-chat-id"},
            }
        )

    with pytest.raises(ValidationError):
        await service.record_interaction_event(
            {
                "event_type": AnalyticsEventType.MEME_DOWNLOAD,
                "user_id": user.id,
                "surface": "public_api",
                "properties": {"chat_hash": "hashed-chat"},
            }
        )

    events = list((await migrated_db_session.execute(select(AnalyticsEvent))).scalars())
    assert events == []


async def test_record_interaction_event_requires_meme_ref_for_inline_result_events(
    migrated_db_session: AsyncSession,
) -> None:
    service = AnalyticsService(migrated_db_session)
    meme_id = uuid.uuid7()

    with pytest.raises(ValidationError):
        await service.record_interaction_event(
            {
                "event_type": AnalyticsEventType.INLINE_CHOSEN,
                "surface": "telegram_inline",
                "properties": {"chat_hash": "hashed-chat"},
            }
        )

    await service.record_interaction_event(
        {
            "event_type": AnalyticsEventType.INLINE_SENT,
            "surface": "telegram_inline",
            "refs": {"meme_id": meme_id},
            "properties": {"chat_hash": "hashed-chat"},
        }
    )

    event = await migrated_db_session.scalar(
        select(AnalyticsEvent).where(AnalyticsEvent.event_type == AnalyticsEventType.INLINE_SENT)
    )
    assert event is not None
    assert event.payload["refs"] == {"meme_id": str(meme_id)}


async def test_profile_stats_aggregates_counts_active_days_and_real_meme_metadata(
    migrated_db_session: AsyncSession,
) -> None:
    user = build_full_user()
    other_user = build_full_user()
    template = MemeTemplate(slug="frog-template", name="Frog Template")
    migrated_db_session.add_all([user, other_user, template])
    await migrated_db_session.flush()
    primary_meme = await _create_profile_stats_meme(
        migrated_db_session,
        tags=["frog", "reaction"],
        template=template,
    )
    secondary_meme = await _create_profile_stats_meme(migrated_db_session, tags=["frog"])
    migrated_db_session.add_all(
        [
            _profile_stats_event(
                AnalyticsEventType.MEME_VIEW,
                user_id=user.id,
                meme_id=primary_meme.id,
                occurred_at=datetime(2026, 1, 1, 23, 30, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.MEME_SEND,
                user_id=user.id,
                meme_id=primary_meme.id,
                occurred_at=datetime(2026, 1, 1, 23, 35, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.MEME_SAVE,
                user_id=user.id,
                meme_id=primary_meme.id,
                occurred_at=datetime(2026, 1, 1, 23, 40, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.MEME_DOWNLOAD,
                user_id=user.id,
                meme_id=primary_meme.id,
                occurred_at=datetime(2026, 1, 1, 23, 45, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.VIEW,
                user_id=user.id,
                occurred_at=datetime(2026, 1, 2, 0, 5, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.INLINE_SENT,
                user_id=user.id,
                meme_id=primary_meme.id,
                occurred_at=datetime(2026, 1, 2, 0, 10, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.SHARE,
                user_id=user.id,
                occurred_at=datetime(2026, 1, 2, 0, 15, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.SAVE,
                user_id=user.id,
                meme_id=secondary_meme.id,
                occurred_at=datetime(2026, 1, 2, 0, 20, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.FAVORITE,
                user_id=user.id,
                meme_id=secondary_meme.id,
                occurred_at=datetime(2026, 1, 2, 0, 25, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.SEARCH_QUERY,
                user_id=user.id,
                occurred_at=datetime(2026, 1, 3, 1, 0, tzinfo=UTC),
            ),
            _profile_stats_event(
                AnalyticsEventType.MEME_DOWNLOAD,
                user_id=other_user.id,
                meme_id=primary_meme.id,
                occurred_at=datetime(2026, 1, 1, 23, 50, tzinfo=UTC),
            ),
        ]
    )
    await migrated_db_session.flush()

    stats = await AnalyticsService(migrated_db_session).profile_stats(user_id=user.id)

    assert stats.viewed == 2
    assert stats.sent == 3
    assert stats.saved == 3
    assert stats.downloaded == 1
    assert stats.days_active == 3
    assert [(tag.tag, tag.count) for tag in stats.top_tags] == [("frog", 7), ("reaction", 5)]
    assert [(template.name, template.slug, template.count) for template in stats.top_templates] == [
        ("Frog Template", "frog-template", 5)
    ]
    assert any("Top tags require" in note for note in stats.metadata.notes)
    assert any("Top templates require" in note for note in stats.metadata.notes)


async def test_profile_stats_empty_history_returns_zero_counts_and_degradation_notes(
    migrated_db_session: AsyncSession,
) -> None:
    user = build_full_user()
    migrated_db_session.add(user)
    await migrated_db_session.flush()

    stats = await AnalyticsService(migrated_db_session).profile_stats(user_id=user.id)

    assert stats.viewed == 0
    assert stats.sent == 0
    assert stats.saved == 0
    assert stats.downloaded == 0
    assert stats.days_active == 0
    assert stats.top_tags == []
    assert stats.top_templates == []
    assert "No interactions yet" in stats.metadata.notes[0]
    assert any("Top tags require" in note for note in stats.metadata.notes)
    assert any("Top templates require" in note for note in stats.metadata.notes)
