# ruff: noqa: TC003
"""Item feature formulas shared by online ranking and offline evaluation."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from memexpert.services.recommendations.math import clamp01, exponential_decay


@dataclass(frozen=True, slots=True)
class RecommendationItemFeatures:
    meme_id: uuid.UUID
    latest_published_at: datetime
    source_channel_ids: tuple[uuid.UUID, ...] = ()
    representative_source_channel_id: uuid.UUID | None = None
    source_popularity_quantile: float = 0.5
    source_quality_quantile: float = 0.5
    technical_quality: float = 0.5
    platform_response: float = 0.5
    popularity_quantile: float = 0.5
    trend_quantile: float = 0.5
    template_id: uuid.UUID | None = None
    coverage: dict[str, bool] = field(default_factory=dict)

    @property
    def quality_prior(self) -> float:
        return quality_prior(
            source_quality=self.source_quality_quantile,
            technical_quality=self.technical_quality,
            platform_response=self.platform_response,
        )


def quality_prior(*, source_quality: float, technical_quality: float, platform_response: float) -> float:
    return clamp01(
        0.40 * clamp01(source_quality)
        + 0.30 * clamp01(technical_quality)
        + 0.30 * clamp01(platform_response)
    )


def freshness_score(
    published_at: datetime,
    *,
    now: datetime | None = None,
    half_life_days: float = 45.0,
) -> float:
    resolved_now = now or datetime.now(UTC)
    age_seconds = max(0.0, (resolved_now - published_at).total_seconds())
    return exponential_decay(age_seconds=age_seconds, half_life_seconds=half_life_days * 86400.0)


def popularity_alignment(
    item_quantile: float,
    *,
    user_median_quantile: float | None,
    qualifying_positive_count: int,
) -> float:
    if user_median_quantile is None or qualifying_positive_count < 5:
        return 0.5
    return clamp01(1.0 - abs(clamp01(item_quantile) - clamp01(user_median_quantile)))


def bayesian_response_rate(
    *,
    strong_actions: int,
    impressions: int,
    surface_mean: float,
    prior_impressions: int = 20,
) -> float:
    if strong_actions < 0 or impressions < 0 or strong_actions > impressions:
        raise ValueError("response counts are inconsistent")
    if prior_impressions < 0:
        raise ValueError("prior_impressions must be non-negative")
    denominator = impressions + prior_impressions
    if denominator == 0:
        return 0.5
    return clamp01((strong_actions + prior_impressions * clamp01(surface_mean)) / denominator)


def smoothed_source_quality_rate(
    *,
    views: int,
    reactions: int,
    forwards: int,
    comments: int,
    cohort_mean: float,
    prior_views: int = 100,
) -> float:
    if min(views, reactions, forwards, comments, prior_views) < 0:
        raise ValueError("source metrics must be non-negative")
    denominator = views + prior_views
    if denominator == 0:
        return 0.0
    numerator = reactions + 3.0 * forwards + 0.5 * comments + prior_views * max(0.0, cohort_mean)
    return numerator / denominator


def percentile75(values: list[float]) -> float | None:
    """Return PostgreSQL-compatible continuous 75th percentile."""

    resolved = sorted(value for value in values if math.isfinite(value))
    if not resolved:
        return None
    position = (len(resolved) - 1) * 0.75
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return resolved[lower]
    fraction = position - lower
    return resolved[lower] + fraction * (resolved[upper] - resolved[lower])


__all__ = [
    "RecommendationItemFeatures",
    "bayesian_response_rate",
    "freshness_score",
    "percentile75",
    "popularity_alignment",
    "quality_prior",
    "smoothed_source_quality_rate",
]
