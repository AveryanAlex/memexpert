# ruff: noqa: TC003
"""Exact per-user recommendation-state projection and profile invalidation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert

from memexpert.models.base import utcnow
from memexpert.models.content import Meme
from memexpert.models.enums import AnalyticsEventType
from memexpert.models.recommendation import (
    UserMemeRecommendationState,
    UserRecommendationProfile,
    UserRecommendationProfileSignal,
    UserRecommendationProfileStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

_IMPRESSION_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.IMPRESSION,
        AnalyticsEventType.INLINE_SERVED,
        AnalyticsEventType.MEME_IMPRESSION,
    }
)
_ENGAGED_VIEW_EVENT_TYPES = frozenset({AnalyticsEventType.MEME_ENGAGED_VIEW})
_DURABLE_PREFERENCE_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.FAVORITE,
        AnalyticsEventType.MEME_LIKE,
        AnalyticsEventType.MEME_PIN,
        AnalyticsEventType.MEME_SAVE,
        AnalyticsEventType.SAVE,
    }
)
_HIGH_INTENT_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.INLINE_CHOSEN,
        AnalyticsEventType.INLINE_SENT,
        AnalyticsEventType.MEME_DOWNLOAD,
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.SHARE,
    }
)
_PROFILE_SIGNAL_EVENT_TYPES = frozenset(
    {
        *_DURABLE_PREFERENCE_EVENT_TYPES,
        *_HIGH_INTENT_EVENT_TYPES,
        AnalyticsEventType.MEME_DETAIL_CLICK,
        AnalyticsEventType.MEME_ENGAGED_VIEW,
        AnalyticsEventType.MEME_VIEW,
        AnalyticsEventType.VIEW,
    }
)
_REMOVE_ACTIONS = frozenset(
    {
        "delete",
        "deleted",
        "remove",
        "removed",
        "remove_save",
        "unfavorite",
        "unlike",
        "unpin",
        "unsave",
    }
)
_NEUTRAL_ACTIONS = frozenset({"reorder", "reorder_pin"})


async def project_recommendation_interaction(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    meme_id: uuid.UUID | None,
    event_type: AnalyticsEventType,
    properties: Mapping[str, object],
    occurred_at: datetime,
) -> None:
    """Project one newly inserted event; callers must not invoke this for duplicates."""

    if user_id is None or meme_id is None:
        return
    if await session.scalar(select(Meme.id).where(Meme.id == meme_id).limit(1)) is None:
        # The shared analytics stream deliberately permits refs to deleted or
        # externally supplied meme IDs; the FK-backed serving projection does not.
        return
    is_impression = event_type in _IMPRESSION_EVENT_TYPES
    is_engaged_view = event_type in _ENGAGED_VIEW_EVENT_TYPES
    is_strong_action = _is_strong_action(event_type, properties)
    is_profile_signal = _is_profile_signal(event_type, properties)
    if not (is_impression or is_engaged_view or is_strong_action or is_profile_signal):
        return

    await _upsert_recommendation_state(
        session,
        user_id=user_id,
        meme_id=meme_id,
        occurred_at=occurred_at,
        is_impression=is_impression,
        is_engaged_view=is_engaged_view,
        is_strong_action=is_strong_action,
    )
    if is_profile_signal:
        await mark_recommendation_profile_dirty(session, user_id=user_id, dirty_at=occurred_at)


async def project_durable_preference_change(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    meme_id: uuid.UUID,
    is_add: bool,
    occurred_at: datetime | None = None,
) -> None:
    """Project authoritative collection/pin state in the mutation transaction.

    Adds are strong positives for cooldown purposes. Removes retain an exact
    first-seen row and invalidate the profile without creating a negative or a
    new strong-positive timestamp.
    """

    observed_at = occurred_at or utcnow()
    await _upsert_recommendation_state(
        session,
        user_id=user_id,
        meme_id=meme_id,
        occurred_at=observed_at,
        is_impression=False,
        is_engaged_view=False,
        is_strong_action=is_add,
    )
    if is_add:
        await mark_recommendation_profile_dirty(session, user_id=user_id, dirty_at=observed_at)
    else:
        # A stale centroid cannot subtract a removed durable preference. Drop
        # it transactionally so newly generated Home/Search/Similar results do
        # not keep serving the removed taste until the scheduler catches up.
        await invalidate_recommendation_profile(
            session,
            user_id=user_id,
            dirty_at=observed_at,
        )


async def _upsert_recommendation_state(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    meme_id: uuid.UUID,
    occurred_at: datetime,
    is_impression: bool,
    is_engaged_view: bool,
    is_strong_action: bool,
) -> None:
    statement = insert(UserMemeRecommendationState).values(
        user_id=user_id,
        meme_id=meme_id,
        first_seen_at=occurred_at,
        latest_impression_at=occurred_at if is_impression else None,
        latest_engaged_view_at=occurred_at if is_engaged_view else None,
        latest_strong_action_at=occurred_at if is_strong_action else None,
        impression_count=1 if is_impression else 0,
    )
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[UserMemeRecommendationState.user_id, UserMemeRecommendationState.meme_id],
        set_={
            "first_seen_at": func.least(
                UserMemeRecommendationState.first_seen_at,
                excluded.first_seen_at,
            ),
            "latest_impression_at": _latest_timestamp(
                UserMemeRecommendationState.latest_impression_at,
                excluded.latest_impression_at,
            ),
            "latest_engaged_view_at": _latest_timestamp(
                UserMemeRecommendationState.latest_engaged_view_at,
                excluded.latest_engaged_view_at,
            ),
            "latest_strong_action_at": _latest_timestamp(
                UserMemeRecommendationState.latest_strong_action_at,
                excluded.latest_strong_action_at,
            ),
            "impression_count": (
                UserMemeRecommendationState.impression_count + excluded.impression_count
            ),
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)


async def mark_recommendation_profile_dirty(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    dirty_at: datetime | None = None,
) -> None:
    """Retain the earliest outstanding dirty watermark for one user."""

    observed_at = dirty_at or utcnow()
    statement = insert(UserRecommendationProfileStatus).values(
        user_id=user_id,
        dirty_since=observed_at,
    )
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[UserRecommendationProfileStatus.user_id],
        set_={
            "dirty_since": func.least(
                func.coalesce(UserRecommendationProfileStatus.dirty_since, excluded.dirty_since),
                excluded.dirty_since,
            ),
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)


async def invalidate_recommendation_profile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    dirty_at: datetime | None = None,
) -> None:
    """Delete stale materializations and make a bounded scheduler rebuild mandatory."""

    await session.execute(
        delete(UserRecommendationProfileSignal).where(UserRecommendationProfileSignal.user_id == user_id)
    )
    await session.execute(delete(UserRecommendationProfile).where(UserRecommendationProfile.user_id == user_id))
    await mark_recommendation_profile_dirty(session, user_id=user_id, dirty_at=dirty_at)


async def merge_user_recommendation_state(
    session: AsyncSession,
    *,
    source_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    merged_at: datetime | None = None,
) -> None:
    """Combine guest state into its canonical user without losing exact cooldowns."""

    if source_user_id == target_user_id:
        return
    source_rows = select(
        literal(target_user_id),
        UserMemeRecommendationState.meme_id,
        UserMemeRecommendationState.first_seen_at,
        UserMemeRecommendationState.latest_impression_at,
        UserMemeRecommendationState.latest_engaged_view_at,
        UserMemeRecommendationState.latest_strong_action_at,
        UserMemeRecommendationState.impression_count,
    ).where(UserMemeRecommendationState.user_id == source_user_id)
    statement = insert(UserMemeRecommendationState).from_select(
        [
            "user_id",
            "meme_id",
            "first_seen_at",
            "latest_impression_at",
            "latest_engaged_view_at",
            "latest_strong_action_at",
            "impression_count",
        ],
        source_rows,
    )
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[UserMemeRecommendationState.user_id, UserMemeRecommendationState.meme_id],
        set_={
            "first_seen_at": func.least(
                UserMemeRecommendationState.first_seen_at,
                excluded.first_seen_at,
            ),
            "latest_impression_at": _latest_timestamp(
                UserMemeRecommendationState.latest_impression_at,
                excluded.latest_impression_at,
            ),
            "latest_engaged_view_at": _latest_timestamp(
                UserMemeRecommendationState.latest_engaged_view_at,
                excluded.latest_engaged_view_at,
            ),
            "latest_strong_action_at": _latest_timestamp(
                UserMemeRecommendationState.latest_strong_action_at,
                excluded.latest_strong_action_at,
            ),
            "impression_count": (
                UserMemeRecommendationState.impression_count + excluded.impression_count
            ),
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)
    await session.execute(
        delete(UserMemeRecommendationState).where(UserMemeRecommendationState.user_id == source_user_id)
    )
    await invalidate_recommendation_profile(
        session,
        user_id=target_user_id,
        dirty_at=merged_at or utcnow(),
    )


def _is_profile_signal(event_type: AnalyticsEventType, properties: Mapping[str, object]) -> bool:
    if event_type not in _PROFILE_SIGNAL_EVENT_TYPES:
        return False
    if event_type not in _DURABLE_PREFERENCE_EVENT_TYPES:
        return True
    return _normalized_action(properties) not in _NEUTRAL_ACTIONS


def _is_strong_action(event_type: AnalyticsEventType, properties: Mapping[str, object]) -> bool:
    if event_type in _HIGH_INTENT_EVENT_TYPES:
        return True
    if event_type not in _DURABLE_PREFERENCE_EVENT_TYPES:
        return False
    action = _normalized_action(properties)
    # Legacy events commonly omitted an action. They remain positive for the
    # historical backfill, while explicit removals and reorder-only events do not.
    return action is None or action not in (_REMOVE_ACTIONS | _NEUTRAL_ACTIONS)


def _normalized_action(properties: Mapping[str, object]) -> str | None:
    value = properties.get("action")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    return normalized or None


def _latest_timestamp(current: object, incoming: object) -> object:
    return func.greatest(func.coalesce(current, incoming), incoming)


__all__ = [
    "invalidate_recommendation_profile",
    "mark_recommendation_profile_dirty",
    "merge_user_recommendation_state",
    "project_durable_preference_change",
    "project_recommendation_interaction",
]
