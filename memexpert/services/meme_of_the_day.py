# ruff: noqa: TC001
"""Meme of the Day durable selection and public read service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeOfTheDaySelection
from memexpert.schemas.meme import MemeResultAttributionRead, PublicMemeOfTheDayRead
from memexpert.services.media_render_urls import MediaRenderUrlService
from memexpert.services.meme_search import MemeSearchService

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory

DEFAULT_MOTD_SURFACE = "web_home"
MOTD_SOURCE_ALGORITHM = "motd"
NO_CANDIDATES_REASON = "no_candidates"
SELECTED_REASON = "selected"


@dataclass(frozen=True, slots=True)
class _MotdCandidate:
    meme_id: uuid.UUID
    created_at: datetime
    quality_score: float
    popularity_raw: float
    trending_growth_raw: float
    score: float
    score_components: dict[str, float]


class MemeOfTheDayService:
    """Select, cache, and read one public safe meme per UTC date."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        media_render_service: MediaRenderUrlService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._media_render_service = media_render_service or MediaRenderUrlService()

    async def get_today(
        self,
        *,
        surface: str = DEFAULT_MOTD_SURFACE,
        viewer_user_id: uuid.UUID | None = None,
    ) -> PublicMemeOfTheDayRead:
        """Return today's cached MOTD, refreshing if missing or no longer safe."""

        selected_for = utcnow().date()
        cached = await self._load_selection(selected_for)
        if cached is not None and self._selection_is_returnable(cached):
            return await self._to_read(cached, surface=surface, viewer_user_id=viewer_user_id)
        return await self.refresh(
            selected_for=selected_for,
            surface=surface,
            viewer_user_id=viewer_user_id,
        )

    async def get_cached(
        self,
        *,
        selected_for: date | None = None,
        surface: str = DEFAULT_MOTD_SURFACE,
        viewer_user_id: uuid.UUID | None = None,
    ) -> PublicMemeOfTheDayRead | None:
        """Return a cached MOTD row for the UTC date and configured algorithm."""

        selection = await self._load_selection(selected_for or utcnow().date())
        if selection is None:
            return None
        return await self._to_read(selection, surface=surface, viewer_user_id=viewer_user_id)

    async def refresh(
        self,
        *,
        selected_for: date | None = None,
        surface: str = DEFAULT_MOTD_SURFACE,
        viewer_user_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> PublicMemeOfTheDayRead:
        """Recompute and upsert the MOTD cache row for the requested UTC date."""

        resolved_date = selected_for or utcnow().date()
        candidates = await self._load_candidates(resolved_date)
        selected = candidates[0] if candidates else None
        now = utcnow()

        values = {
            "selected_for": resolved_date,
            "algorithm_version": self._settings.motd_algorithm_version,
            "meme_id": selected.meme_id if selected is not None else None,
            "score": selected.score if selected is not None else 0.0,
            "score_components": selected.score_components if selected is not None else {},
            "reason": SELECTED_REASON if selected is not None else NO_CANDIDATES_REASON,
            "candidate_count": len(candidates),
            "refreshed_at": now,
            "updated_at": now,
        }
        insert_stmt = pg_insert(MemeOfTheDaySelection).values(**values)
        upsert_stmt = (
            insert_stmt.on_conflict_do_update(
                index_elements=(
                    MemeOfTheDaySelection.selected_for,
                    MemeOfTheDaySelection.algorithm_version,
                ),
                set_={
                    "meme_id": insert_stmt.excluded.meme_id,
                    "score": insert_stmt.excluded.score,
                    "score_components": insert_stmt.excluded.score_components,
                    "reason": insert_stmt.excluded.reason,
                    "candidate_count": insert_stmt.excluded.candidate_count,
                    "refreshed_at": insert_stmt.excluded.refreshed_at,
                    "updated_at": insert_stmt.excluded.updated_at,
                },
            )
            .returning(MemeOfTheDaySelection.id)
        )
        selection_id = (await self._session.execute(upsert_stmt)).scalar_one()
        selection = await self._load_selection_by_id(selection_id)
        if commit:
            await self._session.commit()
        return await self._to_read(selection, surface=surface, viewer_user_id=viewer_user_id)

    async def _load_selection(self, selected_for: date) -> MemeOfTheDaySelection | None:
        stmt = (
            _selection_query_options(select(MemeOfTheDaySelection))
            .where(
                MemeOfTheDaySelection.selected_for == selected_for,
                MemeOfTheDaySelection.algorithm_version == self._settings.motd_algorithm_version,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def _load_selection_by_id(self, selection_id: uuid.UUID) -> MemeOfTheDaySelection:
        stmt = _selection_query_options(select(MemeOfTheDaySelection)).where(MemeOfTheDaySelection.id == selection_id)
        selection = (await self._session.execute(stmt)).scalar_one()
        return selection

    async def _load_candidates(self, selected_for: date) -> list[_MotdCandidate]:
        window_end = _candidate_window_end(selected_for)
        window_start = window_end - timedelta(days=self._settings.motd_candidate_lookback_days)
        result = await self._session.execute(
            text(
                """
                SELECT
                    m.id AS meme_id,
                    m.created_at AS created_at,
                    mf.quality_score AS quality_score,
                    COALESCE(mt.latest_popularity_score, 0.0)::double precision AS popularity_raw,
                    GREATEST(
                        (
                            COALESCE(mt.recent_view_count, 0)
                            + COALESCE(mt.recent_send_count, 0) * 3
                            + COALESCE(mt.recent_like_count, 0) * 5
                            + COALESCE(mt.recent_save_count, 0) * 4
                        )
                        -
                        (
                            COALESCE(mt.previous_view_count, 0)
                            + COALESCE(mt.previous_send_count, 0) * 3
                            + COALESCE(mt.previous_like_count, 0) * 5
                            + COALESCE(mt.previous_save_count, 0) * 4
                        ),
                        0
                    )::double precision AS trending_growth_raw
                FROM memes m
                JOIN meme_files mf ON mf.id = m.primary_file_id AND mf.meme_id = m.id
                LEFT JOIN public_meme_trends_mv mt ON mt.meme_id = m.id
                WHERE m.is_public IS TRUE
                  AND m.is_nsfw IS FALSE
                  AND mf.quality_score >= :min_quality_score
                  AND m.created_at >= :window_start
                  AND m.created_at < :window_end
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT :candidate_limit
                """
            ),
            {
                "min_quality_score": self._settings.motd_min_quality_score,
                "window_start": window_start,
                "window_end": window_end,
                "candidate_limit": self._settings.motd_candidate_limit,
            },
        )
        candidates = [
            self._candidate_from_row(dict(row), window_start=window_start, window_end=window_end)
            for row in result.mappings()
        ]
        return sorted(candidates, key=_candidate_sort_key, reverse=True)

    def _candidate_from_row(
        self,
        row: dict[str, object],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> _MotdCandidate:
        created_at = cast("datetime", row["created_at"])
        quality_score = _non_negative_float(row.get("quality_score"))
        popularity_raw = _non_negative_float(row.get("popularity_raw"))
        trending_growth_raw = _non_negative_float(row.get("trending_growth_raw"))
        popularity = math.log1p(popularity_raw)
        trending_growth = math.log1p(trending_growth_raw)
        novelty = _novelty_score(created_at, window_start=window_start, window_end=window_end)
        quality = max(0.0, min(1.0, quality_score))
        score_components = {
            "popularity": popularity * self._settings.motd_popularity_weight,
            "trending_growth": trending_growth * self._settings.motd_trending_growth_weight,
            "novelty": novelty * self._settings.motd_novelty_weight,
            "quality": quality * self._settings.motd_quality_weight,
            "popularity_raw": popularity_raw,
            "trending_growth_raw": trending_growth_raw,
        }
        score = sum(score_components[key] for key in ("popularity", "trending_growth", "novelty", "quality"))
        score_components["total"] = score
        return _MotdCandidate(
            meme_id=cast("uuid.UUID", row["meme_id"]),
            created_at=created_at,
            quality_score=quality_score,
            popularity_raw=popularity_raw,
            trending_growth_raw=trending_growth_raw,
            score=score,
            score_components=score_components,
        )

    def _selection_is_returnable(self, selection: MemeOfTheDaySelection) -> bool:
        if selection.meme_id is None:
            return True
        meme = selection.meme
        if meme is None or not meme.is_public or meme.is_nsfw:
            return False
        return bool(meme.primary_file and meme.primary_file.quality_score >= self._settings.motd_min_quality_score)

    async def _to_read(
        self,
        selection: MemeOfTheDaySelection,
        *,
        surface: str,
        viewer_user_id: uuid.UUID | None,
    ) -> PublicMemeOfTheDayRead:
        score_components = _score_components(selection.score_components)
        meme = selection.meme if self._selection_is_returnable(selection) and selection.meme is not None else None
        cards = (
            await MemeSearchService(
                self._session,
                media_render_service=self._media_render_service,
            ).get_public_meme_cards_by_ids(
                (meme.id,),
                viewer_user_id=viewer_user_id,
            )
            if meme is not None
            else []
        )
        card = cards[0] if cards else None
        attribution = (
            MemeResultAttributionRead(
                surface=surface,
                source_algorithm=MOTD_SOURCE_ALGORITHM,
                rank=1,
                collection_scope="public",
                algorithm_version=selection.algorithm_version,
                score=selection.score,
                score_components=score_components,
                reason=selection.reason,
            )
            if card is not None
            else None
        )
        return PublicMemeOfTheDayRead(
            meme=card,
            selected_for=selection.selected_for,
            refreshed_at=selection.refreshed_at,
            algorithm_version=selection.algorithm_version,
            score=selection.score,
            score_components=score_components,
            reason=selection.reason,
            candidate_count=selection.candidate_count,
            attribution=attribution,
        )


async def run_scheduler_meme_of_the_day_refresh(
    session_factory: AsyncSessionFactory,
    *,
    settings: Settings,
) -> PublicMemeOfTheDayRead:
    """Refresh today's MOTD selection from a scheduler-owned session."""

    async with session_factory() as session:
        return await MemeOfTheDayService(session, settings=settings).refresh()


def meme_of_the_day_result_log_extra(job_id: str, result: PublicMemeOfTheDayRead) -> dict[str, object]:
    """Return scheduler structured log fields for one MOTD refresh."""

    return {
        "event": "scheduler_job_batch_result",
        "job_id": job_id,
        "candidate_count": result.candidate_count,
        "selected_meme_id": str(result.meme.id) if result.meme is not None else None,
        "reason": result.reason,
        "algorithm_version": result.algorithm_version,
        "refreshed_at": result.refreshed_at.isoformat(),
    }


def _selection_query_options(stmt):
    return (
        stmt.options(
            selectinload(MemeOfTheDaySelection.meme).selectinload(Meme.primary_file),
            selectinload(MemeOfTheDaySelection.meme).selectinload(Meme.seo_page),
        )
        .execution_options(populate_existing=True)
    )


def _candidate_sort_key(candidate: _MotdCandidate) -> tuple[float, datetime, str]:
    return candidate.score, candidate.created_at, str(candidate.meme_id)


def _candidate_window_end(selected_for: date) -> datetime:
    now = utcnow()
    if selected_for == now.date():
        return now
    return datetime.combine(selected_for + timedelta(days=1), time.min, tzinfo=UTC)


def _novelty_score(created_at: datetime, *, window_start: datetime, window_end: datetime) -> float:
    total_seconds = (window_end - window_start).total_seconds()
    if total_seconds <= 0.0:
        return 0.0
    elapsed_seconds = (created_at - window_start).total_seconds()
    return max(0.0, min(1.0, elapsed_seconds / total_seconds))


def _score_components(values: Mapping[str, object]) -> dict[str, float]:
    return {str(key): converted for key, value in values.items() if (converted := _finite_float(value)) is not None}


def _non_negative_float(value: object) -> float:
    converted = _finite_float(value)
    return max(0.0, converted or 0.0)


def _finite_float(value: object) -> float | None:
    if not isinstance(value, int | float):
        return None
    converted = float(value)
    if not math.isfinite(converted):
        return None
    return converted


__all__ = [
    "DEFAULT_MOTD_SURFACE",
    "MOTD_SOURCE_ALGORITHM",
    "MemeOfTheDayService",
    "meme_of_the_day_result_log_extra",
    "run_scheduler_meme_of_the_day_refresh",
]
