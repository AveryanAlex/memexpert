"""Best-effort product analytics helpers and launch KPI reporting."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import func, select

from memexpert.models.base import utcnow
from memexpert.models.content import MemePopularitySnapshot
from memexpert.models.enums import AnalyticsEventType
from memexpert.models.user import AccountMergeLog, AnalyticsEvent

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class LaunchKPIRead(BaseModel):
    """Small operator-facing launch metrics derived from analytics and source snapshots."""

    lookback_hours: int
    since: datetime
    searches: int
    views: int
    sends: int
    active_users: int
    likes: int
    saves: int
    guest_to_full_conversions: int
    source_views: int
    source_reactions: int
    source_reposts: int


class AnalyticsService:
    """Write product events without making user-facing paths depend on analytics health."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_event(
        self,
        event_type: AnalyticsEventType,
        *,
        user_id: uuid.UUID | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Persist one event and swallow failures after rolling back the session.

        Callers should pass only product identifiers and coarse context. External
        identifiers must be hashed before they reach this method.
        """

        try:
            self._session.add(
                AnalyticsEvent(
                    user_id=user_id,
                    event_type=event_type,
                    payload=payload or {},
                )
            )
            await self._session.commit()
        except Exception:
            logger.exception("Analytics event write failed.")
            await self._session.rollback()

    async def launch_kpis(self, *, lookback_hours: int = 168) -> LaunchKPIRead:
        """Return launch KPI counts for a bounded recent window.

        Event counts come from ``AnalyticsEvent``. Source counters use the latest
        ``MemePopularitySnapshot`` per meme captured in the same window so source
        crawler metrics can be compared with platform activity.
        """

        resolved_hours = max(1, lookback_hours)
        since = utcnow() - timedelta(hours=resolved_hours)
        event_counts = await self._event_counts_since(since)
        source_views, source_reactions, source_reposts = await self._latest_source_metric_totals_since(since)
        conversions = await self._session.scalar(
            select(func.count()).select_from(AccountMergeLog).where(AccountMergeLog.created_at >= since)
        )
        active_users = await self._session.scalar(
            select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                AnalyticsEvent.occurred_at >= since,
                AnalyticsEvent.user_id.is_not(None),
            )
        )
        return LaunchKPIRead(
            lookback_hours=resolved_hours,
            since=since,
            searches=event_counts.get(AnalyticsEventType.SEARCH_QUERY, 0),
            views=event_counts.get(AnalyticsEventType.MEME_VIEW, 0),
            sends=event_counts.get(AnalyticsEventType.MEME_SEND, 0),
            active_users=active_users or 0,
            likes=event_counts.get(AnalyticsEventType.MEME_LIKE, 0),
            saves=event_counts.get(AnalyticsEventType.MEME_SAVE, 0) + event_counts.get(AnalyticsEventType.SAVE, 0),
            guest_to_full_conversions=conversions or 0,
            source_views=source_views,
            source_reactions=source_reactions,
            source_reposts=source_reposts,
        )

    async def _event_counts_since(self, since: datetime) -> dict[AnalyticsEventType, int]:
        result = await self._session.execute(
            select(AnalyticsEvent.event_type, func.count())
            .where(AnalyticsEvent.occurred_at >= since)
            .group_by(AnalyticsEvent.event_type)
        )
        return {event_type: count for event_type, count in result.all()}

    async def _latest_source_metric_totals_since(self, since: datetime) -> tuple[int, int, int]:
        result = await self._session.execute(
            select(MemePopularitySnapshot)
            .where(MemePopularitySnapshot.captured_at >= since)
            .order_by(MemePopularitySnapshot.meme_id, MemePopularitySnapshot.captured_at.desc())
        )
        latest_by_meme: dict[uuid.UUID, MemePopularitySnapshot] = {}
        for snapshot in result.scalars():
            latest_by_meme.setdefault(snapshot.meme_id, snapshot)
        return (
            sum(snapshot.source_views for snapshot in latest_by_meme.values()),
            sum(snapshot.source_reactions for snapshot in latest_by_meme.values()),
            sum(snapshot.source_reposts for snapshot in latest_by_meme.values()),
        )


def hash_external_identifier(namespace: str, value: object) -> str:
    """Hash an external platform identifier before it enters analytics payloads."""

    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()


__all__ = ["AnalyticsService", "LaunchKPIRead", "hash_external_identifier"]
