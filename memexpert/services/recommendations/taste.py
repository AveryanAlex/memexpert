# ruff: noqa: TC003
"""Light taste/quality scoring shared by Search and Similar surfaces."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, select, text

from memexpert.core.config import Settings, get_settings
from memexpert.core.voyage import VoyageEmbeddingError, decode_embedding_bytes
from memexpert.models.content import EmbeddingCache, Meme
from memexpert.models.enums import EmbeddingInputType
from memexpert.models.recommendation import UserRecommendationProfile
from memexpert.services.recommendations.features import freshness_score, quality_prior
from memexpert.services.recommendations.intent import RecommendationIntentStore
from memexpert.services.recommendations.math import clamp01, cosine_similarity, decode_vector
from memexpert.services.recommendations.profiles import is_profile_materialization_current

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ItemTasteScore:
    taste: float = 0.0
    quality: float = 0.5
    popularity: float = 0.5
    freshness: float = 0.5
    profile_version: str | None = None


class TastePersonalizationService:
    """Apply a small persisted-profile blend without changing candidate intent."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        intent_store: RecommendationIntentStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._intent_store = intent_store or RecommendationIntentStore(settings=self._settings)

    async def score_items(
        self,
        *,
        viewer_user_id: uuid.UUID | None,
        meme_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, ItemTasteScore]:
        if not meme_ids:
            return {}
        profile_vector: tuple[float, ...] | None = None
        profile_version: str | None = None
        if viewer_user_id is not None:
            profile = await self._session.scalar(
                select(UserRecommendationProfile).where(
                    UserRecommendationProfile.user_id == viewer_user_id,
                    UserRecommendationProfile.profile_slot == 0,
                )
            )
            if profile is not None and is_profile_materialization_current(
                model_version=profile.model_version,
                profile_version=profile.profile_version,
                expected_model_version=self._settings.pipeline_voyage_model,
                expected_profile_base_version=self._settings.recommendation_profile_version,
            ):
                try:
                    profile_vector = decode_vector(
                        profile.vector,
                        dimensions=self._settings.pipeline_voyage_output_dimensions,
                    )
                except ValueError:
                    profile_vector = None
                if profile_vector is not None:
                    profile_version = profile.profile_version

        feature_result = await self._session.execute(
            text(
                """
                SELECT
                    meme.id AS meme_id,
                    COALESCE(feature.latest_published_at, meme.created_at) AS published_at,
                    COALESCE(feature.source_quality_quantile, 0.5) AS source_quality,
                    COALESCE(feature.technical_quality, 0.5) AS technical_quality,
                    COALESCE(feature.platform_response, 0.5) AS platform_response,
                    COALESCE(feature.popularity_quantile, 0.5) AS popularity
                FROM memes meme
                LEFT JOIN public_meme_recommendation_features_mv feature ON feature.meme_id = meme.id
                WHERE meme.id = ANY(CAST(:meme_ids AS uuid[]))
                """
            ),
            {"meme_ids": meme_ids},
        )
        scores = {
            cast("uuid.UUID", row.meme_id): ItemTasteScore(
                quality=quality_prior(
                    source_quality=float(row.source_quality),
                    technical_quality=float(row.technical_quality),
                    platform_response=float(row.platform_response),
                ),
                popularity=clamp01(float(row.popularity)),
                freshness=freshness_score(
                    cast("datetime", row.published_at),
                    half_life_days=self._settings.recommendation_freshness_half_life_days,
                ),
                profile_version=profile_version,
            )
            for row in feature_result
        }
        if profile_vector is None:
            return scores

        embedding_result = await self._session.execute(
            select(Meme.id, EmbeddingCache.embedding)
            .join(
                EmbeddingCache,
                and_(
                    EmbeddingCache.source_file_id == Meme.primary_file_id,
                    EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
                ),
            )
            .where(Meme.id.in_(meme_ids))
            .order_by(Meme.id, EmbeddingCache.created_at.desc())
        )
        seen: set[uuid.UUID] = set()
        for meme_id, raw_vector in embedding_result:
            if meme_id in seen:
                continue
            seen.add(meme_id)
            try:
                vector = decode_embedding_bytes(
                    raw_vector,
                    dimensions=self._settings.pipeline_voyage_output_dimensions,
                )
            except VoyageEmbeddingError:
                continue
            current = scores.get(meme_id, ItemTasteScore(profile_version=profile_version))
            scores[meme_id] = ItemTasteScore(
                taste=clamp01(cosine_similarity(vector, profile_vector)),
                quality=current.quality,
                popularity=current.popularity,
                freshness=current.freshness,
                profile_version=profile_version,
            )
        return scores

    async def record_successful_search(
        self,
        *,
        viewer_user_id: uuid.UUID | None,
        query_vector: tuple[float, ...] | None,
    ) -> None:
        if viewer_user_id is None or query_vector is None:
            return
        await self._intent_store.record_successful_search(
            user_id=viewer_user_id,
            query_vector=query_vector,
        )


__all__ = ["ItemTasteScore", "TastePersonalizationService"]
