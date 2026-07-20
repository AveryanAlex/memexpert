# ruff: noqa: TC001,TC003
"""PostgreSQL profile materialization and bounded online profile reads."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, delete, func, or_, select, text, update

from memexpert.core.config import Settings, get_settings
from memexpert.core.voyage import VoyageEmbeddingError, decode_embedding_bytes
from memexpert.models.base import utcnow
from memexpert.models.content import EmbeddingCache, Meme
from memexpert.models.enums import EmbeddingInputType
from memexpert.models.recommendation import (
    UserRecommendationProfile,
    UserRecommendationProfileSignal,
    UserRecommendationProfileStatus,
)
from memexpert.services.recommendations.interaction_state import mark_recommendation_profile_dirty
from memexpert.services.recommendations.math import decode_vector, encode_vector, weighted_centroid
from memexpert.services.recommendations.profiles import (
    ProfileSignalVector,
    build_profile_vectors,
    is_profile_materialization_current,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class MaterializedSignal:
    meme_id: uuid.UUID
    weight: float
    last_signal_at: datetime
    is_strong_positive: bool


@dataclass(frozen=True, slots=True)
class OnlineProfileBundle:
    short_term_vector: tuple[float, ...] | None
    long_term_vectors: tuple[tuple[float, ...], ...]
    recent_positive_file_ids: tuple[uuid.UUID, ...]
    profile_version: str | None
    strong_positive_count: int
    median_popularity_quantile: float | None


@dataclass(frozen=True, slots=True)
class ProfileRebuildResult:
    claimed_users: int
    rebuilt_users: int
    failed_users: int


class RecommendationProfileStore:
    """Build long-term vectors and expose bounded representations to serving."""

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()

    async def rebuild_user(self, user_id: uuid.UUID) -> int:
        """Replace one user's top signals and deterministic vector slots."""

        observed_at = utcnow()
        signals = await self._load_materialized_signals(
            user_id,
            half_life_days=self._settings.recommendation_long_term_half_life_days,
            limit=self._settings.recommendation_long_term_signal_limit,
            include_low_intent=False,
        )
        vectors_by_meme_id = await self._load_embedding_vectors(tuple(signal.meme_id for signal in signals))
        profile_inputs = [
            ProfileSignalVector(
                meme_id=signal.meme_id,
                vector=vectors_by_meme_id[signal.meme_id],
                weight=signal.weight,
                last_signal_at=signal.last_signal_at,
                is_strong_positive=signal.is_strong_positive,
            )
            for signal in signals
            if signal.meme_id in vectors_by_meme_id
        ]
        profiles = build_profile_vectors(
            profile_inputs,
            activation_threshold=self._settings.recommendation_cluster_activation_signals,
            min_cluster_items=self._settings.recommendation_cluster_min_items,
            max_iterations=self._settings.recommendation_cluster_iterations,
        )
        watermark = max((signal.last_signal_at for signal in signals), default=None)
        profile_version = _materialized_profile_version(
            base_version=self._settings.recommendation_profile_version,
            signals=signals,
            watermark=watermark,
        )

        await self._session.execute(
            delete(UserRecommendationProfileSignal).where(UserRecommendationProfileSignal.user_id == user_id)
        )
        await self._session.execute(
            delete(UserRecommendationProfile).where(UserRecommendationProfile.user_id == user_id)
        )
        self._session.add_all(
            [
                UserRecommendationProfileSignal(
                    user_id=user_id,
                    meme_id=signal.meme_id,
                    weight=signal.weight,
                    last_signal_at=signal.last_signal_at,
                    is_strong_positive=signal.is_strong_positive,
                )
                for signal in signals
            ]
        )
        self._session.add_all(
            [
                UserRecommendationProfile(
                    user_id=user_id,
                    profile_slot=profile.slot,
                    model_version=self._settings.pipeline_voyage_model,
                    profile_version=profile_version,
                    signal_count=profile.signal_count,
                    total_weight=profile.total_weight,
                    event_watermark=watermark,
                    vector=encode_vector(profile.vector),
                    generated_at=observed_at,
                )
                for profile in profiles
            ]
        )
        await self._session.execute(
            update(UserRecommendationProfileStatus)
            .where(UserRecommendationProfileStatus.user_id == user_id)
            .values(
                dirty_since=None,
                event_watermark=watermark,
                last_rebuilt_at=observed_at,
                updated_at=observed_at,
            )
        )
        await self._session.flush()
        return len(profiles)

    async def load_online_bundle(self, user_id: uuid.UUID) -> OnlineProfileBundle:
        """Load short, long, and direct-positive representations with bounded reads."""

        observed_at = utcnow()
        short_signals = await self._load_materialized_signals(
            user_id,
            half_life_days=self._settings.recommendation_short_term_half_life_hours / 24.0,
            limit=self._settings.recommendation_long_term_signal_limit,
            window_start=observed_at
            - timedelta(hours=self._settings.recommendation_short_term_window_hours),
            include_durable=True,
            include_low_intent=True,
        )
        short_vectors = await self._load_embedding_vectors(tuple(signal.meme_id for signal in short_signals))
        short_term_vector = weighted_centroid(
            (short_vectors[signal.meme_id], signal.weight)
            for signal in short_signals
            if signal.meme_id in short_vectors
        )

        persisted_profile_rows = (
            await self._session.scalars(
                select(UserRecommendationProfile)
                .where(UserRecommendationProfile.user_id == user_id)
                .order_by(UserRecommendationProfile.profile_slot)
            )
        ).all()
        profile_rows = [
            row
            for row in persisted_profile_rows
            if is_profile_materialization_current(
                model_version=row.model_version,
                profile_version=row.profile_version,
                expected_model_version=self._settings.pipeline_voyage_model,
                expected_profile_base_version=self._settings.recommendation_profile_version,
            )
        ]
        long_vectors: list[tuple[float, ...]] = []
        for row in profile_rows:
            try:
                long_vectors.append(
                    decode_vector(row.vector, dimensions=self._settings.pipeline_voyage_output_dimensions)
                )
            except ValueError:
                continue

        direct_signals = (
            await self._load_persisted_profile_signals(
                user_id,
                limit=self._settings.recommendation_long_term_signal_limit,
            )
            if profile_rows
            else await self._load_materialized_signals(
                user_id,
                half_life_days=self._settings.recommendation_long_term_half_life_days,
                limit=self._settings.recommendation_long_term_signal_limit,
                include_low_intent=False,
            )
        )
        if not long_vectors:
            direct_vectors = await self._load_embedding_vectors(
                tuple(signal.meme_id for signal in direct_signals)
            )
            on_demand_global = weighted_centroid(
                (direct_vectors[signal.meme_id], signal.weight)
                for signal in direct_signals
                if signal.meme_id in direct_vectors
            )
            if on_demand_global is not None:
                long_vectors.append(on_demand_global)

        recent_signals = await self._load_materialized_signals(
            user_id,
            half_life_days=self._settings.recommendation_long_term_half_life_days,
            limit=self._settings.recommendation_long_term_signal_limit,
            window_start=observed_at
            - timedelta(hours=self._settings.recommendation_positive_lookback_hours),
            include_low_intent=False,
        )
        strong_signals = [signal for signal in recent_signals if signal.is_strong_positive][
            : self._settings.recommendation_multi_positive_candidate_limit
        ]
        primary_file_rows = (
            await self._session.execute(
                select(Meme.id, Meme.primary_file_id).where(
                    Meme.id.in_(tuple(signal.meme_id for signal in strong_signals))
                )
            )
        ).all()
        primary_file_ids = {meme_id: file_id for meme_id, file_id in primary_file_rows}
        median_popularity = await self._load_median_positive_popularity(
            user_id,
            fallback_meme_ids=tuple(signal.meme_id for signal in strong_signals),
        )
        profile_version = (
            profile_rows[0].profile_version
            if profile_rows
            else f"{self._settings.recommendation_profile_version}:online" if long_vectors else None
        )
        return OnlineProfileBundle(
            short_term_vector=short_term_vector,
            long_term_vectors=tuple(long_vectors),
            recent_positive_file_ids=tuple(
                primary_file_ids[signal.meme_id]
                for signal in strong_signals
                if signal.meme_id in primary_file_ids
            ),
            profile_version=profile_version,
            strong_positive_count=sum(signal.is_strong_positive for signal in direct_signals),
            median_popularity_quantile=median_popularity,
        )

    async def _load_persisted_profile_signals(
        self,
        user_id: uuid.UUID,
        *,
        limit: int,
    ) -> list[MaterializedSignal]:
        """Read the scheduler's bounded top-signal snapshot for online metadata."""

        rows = (
            await self._session.scalars(
                select(UserRecommendationProfileSignal)
                .where(UserRecommendationProfileSignal.user_id == user_id)
                .order_by(
                    UserRecommendationProfileSignal.weight.desc(),
                    UserRecommendationProfileSignal.last_signal_at.desc(),
                    UserRecommendationProfileSignal.meme_id,
                )
                .limit(limit)
            )
        ).all()
        return [
            MaterializedSignal(
                meme_id=row.meme_id,
                weight=row.weight,
                last_signal_at=row.last_signal_at,
                is_strong_positive=row.is_strong_positive,
            )
            for row in rows
        ]

    async def _load_materialized_signals(
        self,
        user_id: uuid.UUID,
        *,
        half_life_days: float,
        limit: int,
        window_start: datetime | None = None,
        include_durable: bool = True,
        include_low_intent: bool = False,
    ) -> list[MaterializedSignal]:
        result = await self._session.execute(
            text(_PROFILE_SIGNALS_SQL),
            {
                "user_id": user_id,
                "half_life_days": half_life_days,
                "limit": limit,
                "window_start": window_start,
                "include_durable": include_durable,
                "include_low_intent": include_low_intent,
            },
        )
        return [
            MaterializedSignal(
                meme_id=cast("uuid.UUID", row.meme_id),
                weight=float(row.weight),
                last_signal_at=cast("datetime", row.last_signal_at),
                is_strong_positive=bool(row.is_strong_positive),
            )
            for row in result
            if float(row.weight) > 0.0
        ]

    async def _load_embedding_vectors(
        self,
        meme_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        if not meme_ids:
            return {}
        result = await self._session.execute(
            select(Meme.id, Meme.primary_file_id, EmbeddingCache.embedding)
            .join(
                EmbeddingCache,
                and_(
                    EmbeddingCache.source_file_id == Meme.primary_file_id,
                    EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
                ),
            )
            .where(Meme.id.in_(tuple(meme_ids)))
            .order_by(Meme.id, EmbeddingCache.created_at.desc())
        )
        vectors: dict[uuid.UUID, tuple[float, ...]] = {}
        for meme_id, _file_id, raw_vector in result:
            if meme_id in vectors:
                continue
            try:
                vectors[meme_id] = decode_embedding_bytes(
                    raw_vector,
                    dimensions=self._settings.pipeline_voyage_output_dimensions,
                )
            except VoyageEmbeddingError:
                continue
        return vectors

    async def _load_median_positive_popularity(
        self,
        user_id: uuid.UUID,
        *,
        fallback_meme_ids: tuple[uuid.UUID, ...],
    ) -> float | None:
        result = await self._session.scalar(
            text(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY feature.popularity_quantile)
                FROM user_recommendation_profile_signals signal
                JOIN public_meme_recommendation_features_mv feature ON feature.meme_id = signal.meme_id
                WHERE signal.user_id = :user_id
                  AND signal.is_strong_positive IS TRUE
                HAVING count(*) >= 5
                """
            ),
            {"user_id": user_id},
        )
        if result is not None:
            return float(result)
        if len(fallback_meme_ids) < 5:
            return None
        fallback = await self._session.scalar(
            text(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY popularity_quantile)
                FROM public_meme_recommendation_features_mv
                WHERE meme_id = ANY(CAST(:meme_ids AS uuid[]))
                """
            ),
            {"meme_ids": fallback_meme_ids},
        )
        return None if fallback is None else float(fallback)


async def rebuild_dirty_recommendation_profiles(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
) -> ProfileRebuildResult:
    """Claim and rebuild one bounded scheduler batch."""

    resolved = settings or get_settings()
    rebuilt = 0
    failed = 0
    async with session_factory() as session:
        current_profile_prefix = f"{resolved.recommendation_profile_version}:"
        stale_profile = or_(
            UserRecommendationProfile.model_version != resolved.pipeline_voyage_model,
            and_(
                UserRecommendationProfile.profile_version != resolved.recommendation_profile_version,
                func.substr(
                    UserRecommendationProfile.profile_version,
                    1,
                    len(current_profile_prefix),
                )
                != current_profile_prefix,
            ),
        )
        missing_status_user_ids = tuple(
            (
                await session.scalars(
                    select(UserRecommendationProfile.user_id)
                    .outerjoin(
                        UserRecommendationProfileStatus,
                        UserRecommendationProfileStatus.user_id == UserRecommendationProfile.user_id,
                    )
                    .where(
                        UserRecommendationProfileStatus.user_id.is_(None),
                        stale_profile,
                    )
                    .group_by(UserRecommendationProfile.user_id)
                    .order_by(UserRecommendationProfile.user_id)
                    .limit(resolved.scheduler_recommendation_profile_rebuild_batch_size)
                )
            ).all()
        )
        for user_id in missing_status_user_ids:
            await mark_recommendation_profile_dirty(session, user_id=user_id)

        stale_profile_exists = (
            select(UserRecommendationProfile.id)
            .where(
                UserRecommendationProfile.user_id == UserRecommendationProfileStatus.user_id,
                stale_profile,
            )
            .exists()
        )
        user_ids = tuple(
            (
                await session.scalars(
                    select(UserRecommendationProfileStatus.user_id)
                    .where(
                        or_(
                            UserRecommendationProfileStatus.dirty_since.is_not(None),
                            stale_profile_exists,
                        )
                    )
                    .order_by(
                        UserRecommendationProfileStatus.dirty_since.asc().nulls_last(),
                        UserRecommendationProfileStatus.updated_at,
                        UserRecommendationProfileStatus.user_id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(resolved.scheduler_recommendation_profile_rebuild_batch_size)
                )
            ).all()
        )
        store = RecommendationProfileStore(session, settings=resolved)
        for user_id in user_ids:
            try:
                async with session.begin_nested():
                    await store.rebuild_user(user_id)
            except Exception:
                failed += 1
            else:
                rebuilt += 1
        await session.commit()
    return ProfileRebuildResult(claimed_users=len(user_ids), rebuilt_users=rebuilt, failed_users=failed)


def _materialized_profile_version(
    *,
    base_version: str,
    signals: list[MaterializedSignal],
    watermark: datetime | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(base_version.encode())
    digest.update((watermark.isoformat() if watermark else "none").encode())
    for signal in signals:
        digest.update(signal.meme_id.bytes)
        digest.update(f"{signal.weight:.8f}".encode())
    return f"{base_version}:{digest.hexdigest()[:12]}"[:64]


_PROFILE_SIGNALS_SQL = """
WITH raw_events AS (
    SELECT
        ae.id AS event_id,
        CASE
            WHEN jsonb_typeof(ae.payload -> 'refs' -> 'meme_id') = 'string'
             AND ae.payload -> 'refs' ->> 'meme_id'
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (ae.payload -> 'refs' ->> 'meme_id')::uuid
            WHEN jsonb_typeof(ae.payload -> 'meme_id') = 'string'
             AND ae.payload ->> 'meme_id'
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (ae.payload ->> 'meme_id')::uuid
            ELSE NULL
        END AS meme_id,
        ae.event_type::text AS event_type,
        ae.occurred_at,
        NULLIF(btrim(ae.payload ->> 'impression_id'), '') AS impression_id,
        lower(COALESCE(ae.payload -> 'properties' ->> 'action', '')) AS action,
        CASE
            WHEN ae.event_type::text IN (
                'meme_download', 'meme_send', 'meme_share', 'share', 'inline_chosen', 'inline_sent'
            ) THEN 4.0
            WHEN ae.event_type::text = 'meme_engaged_view' THEN 2.0
            WHEN ae.event_type::text IN ('meme_detail_click', 'meme_view', 'view') THEN 1.0
            ELSE 0.0
        END AS base_weight,
        CASE
            WHEN ae.event_type::text IN (
                'meme_send', 'meme_share', 'share', 'inline_chosen', 'inline_sent'
            ) AND NULLIF(btrim(ae.payload ->> 'impression_id'), '') IS NOT NULL
            THEN 'send:' || (ae.payload ->> 'impression_id')
            WHEN ae.event_type::text IN ('meme_detail_click', 'meme_view', 'view')
              AND NULLIF(btrim(ae.payload ->> 'impression_id'), '') IS NOT NULL
            THEN 'detail:' || (ae.payload ->> 'impression_id')
            ELSE ae.id::text
        END AS dedupe_key
    FROM analytics_events ae
    WHERE ae.user_id = :user_id
      AND ae.event_type::text IN (
          'meme_download', 'meme_send', 'meme_share', 'share', 'inline_chosen', 'inline_sent',
          'meme_engaged_view', 'meme_detail_click', 'meme_view', 'view'
      )
      AND (
          CAST(:window_start AS timestamptz) IS NULL
          OR ae.occurred_at >= CAST(:window_start AS timestamptz)
      )
),
deduplicated_events AS (
    SELECT DISTINCT ON (meme_id, dedupe_key)
        meme_id,
        occurred_at,
        base_weight,
        base_weight >= 4.0 AS is_strong_positive
    FROM raw_events
    WHERE meme_id IS NOT NULL
      AND base_weight > 0
      AND (CAST(:include_low_intent AS boolean) OR base_weight >= 4.0)
      AND action NOT IN (
          'delete', 'remove', 'remove_save', 'reorder', 'reorder_pin',
          'unfavorite', 'unlike', 'unpin', 'unsave'
      )
    ORDER BY meme_id, dedupe_key, base_weight DESC, occurred_at DESC, event_id DESC
),
event_signals AS (
    SELECT
        meme_id,
        sum(
            base_weight * power(
                0.5,
                GREATEST(extract(epoch FROM (now() - occurred_at)), 0.0)
                    / (CAST(:half_life_days AS double precision) * 86400.0)
            )
        )::double precision AS weight,
        max(occurred_at) AS last_signal_at,
        bool_or(is_strong_positive) AS is_strong_positive
    FROM deduplicated_events
    GROUP BY meme_id
),
durable_kinds AS (
    SELECT
        cm.meme_id,
        CASE WHEN c.kind = 'favorites' THEN 'favorite' ELSE 'save' END AS durable_kind,
        max(cm.added_at) AS last_signal_at
    FROM collection_memes cm
    JOIN collections c ON c.id = cm.collection_id
    WHERE CAST(:include_durable AS boolean)
      AND COALESCE(cm.added_by_user_id, c.owner_id) = :user_id
      AND (
          CAST(:window_start AS timestamptz) IS NULL
          OR cm.added_at >= CAST(:window_start AS timestamptz)
      )
    GROUP BY cm.meme_id, CASE WHEN c.kind = 'favorites' THEN 'favorite' ELSE 'save' END
    UNION ALL
    SELECT pm.meme_id, 'pin', max(pm.pinned_at)
    FROM pinned_memes pm
    WHERE CAST(:include_durable AS boolean)
      AND pm.user_id = :user_id
      AND (
          CAST(:window_start AS timestamptz) IS NULL
          OR pm.pinned_at >= CAST(:window_start AS timestamptz)
      )
    GROUP BY pm.meme_id
),
durable_signals AS (
    SELECT
        meme_id,
        (count(*) * 5.0)::double precision AS weight,
        max(last_signal_at) AS last_signal_at,
        TRUE AS is_strong_positive
    FROM durable_kinds
    GROUP BY meme_id
),
combined AS (
    SELECT * FROM event_signals
    UNION ALL
    SELECT * FROM durable_signals
)
SELECT
    meme_id,
    sum(weight)::double precision AS weight,
    max(last_signal_at) AS last_signal_at,
    bool_or(is_strong_positive) AS is_strong_positive
FROM combined
GROUP BY meme_id
ORDER BY weight DESC, last_signal_at DESC, meme_id
LIMIT :limit
"""


__all__ = [
    "MaterializedSignal",
    "OnlineProfileBundle",
    "ProfileRebuildResult",
    "RecommendationProfileStore",
    "rebuild_dirty_recommendation_profiles",
]
