# ruff: noqa: TC003
"""Durable recommendation state and profile materializations."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from memexpert.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class UserMemeRecommendationState(TimestampMixin, Base):
    """Exact per-viewer cooldown state projected from idempotent interactions."""

    __tablename__ = "user_meme_recommendation_state"
    __table_args__ = (
        CheckConstraint("impression_count >= 0", name="recommendation_state_impression_count_non_negative"),
        Index(
            "ix_user_meme_recommendation_state_user_impression",
            "user_id",
            "latest_impression_at",
        ),
        Index(
            "ix_user_meme_recommendation_state_user_strong_action",
            "user_id",
            "latest_strong_action_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    latest_impression_at: Mapped[datetime | None] = mapped_column(nullable=True)
    latest_engaged_view_at: Mapped[datetime | None] = mapped_column(nullable=True)
    latest_strong_action_at: Mapped[datetime | None] = mapped_column(nullable=True)
    impression_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserRecommendationProfileStatus(TimestampMixin, Base):
    """One dirty/rebuild watermark row per user with recommendation history."""

    __tablename__ = "user_recommendation_profile_status"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dirty_since: Mapped[datetime | None] = mapped_column(nullable=True, default=utcnow)
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    event_watermark: Mapped[datetime | None] = mapped_column(nullable=True)


class UserRecommendationProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """PostgreSQL-authoritative long-term global or clustered taste vector."""

    __tablename__ = "user_recommendation_profiles"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "profile_slot",
            name="uq_user_recommendation_profiles_user_slot",
        ),
        CheckConstraint("profile_slot >= 0 AND profile_slot <= 4", name="recommendation_profile_slot_range"),
        CheckConstraint("signal_count >= 0", name="recommendation_profile_signal_count_non_negative"),
        CheckConstraint("total_weight >= 0", name="recommendation_profile_weight_non_negative"),
        CheckConstraint("model_version <> ''", name="recommendation_profile_model_version_not_blank"),
        CheckConstraint("profile_version <> ''", name="recommendation_profile_version_not_blank"),
        Index("ix_user_recommendation_profiles_user_generated", "user_id", "generated_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Slot zero is the global centroid. Slots one through four are stable
    # deterministic clusters generated from the same signal snapshot.
    profile_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_weight: Mapped[float] = mapped_column(Float, nullable=False)
    event_watermark: Mapped[datetime | None] = mapped_column(nullable=True)
    vector: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


class UserRecommendationProfileSignal(TimestampMixin, Base):
    """Top bounded long-term item signals used to reproduce a profile."""

    __tablename__ = "user_recommendation_profile_signals"
    __table_args__ = (
        CheckConstraint("weight > 0", name="recommendation_profile_signal_weight_positive"),
        Index(
            "ix_user_recommendation_profile_signals_user_weight",
            "user_id",
            "weight",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    meme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    last_signal_at: Mapped[datetime] = mapped_column(nullable=False)
    is_strong_positive: Mapped[bool] = mapped_column(nullable=False, default=False)


class RecommendationDailyAggregate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Bounded daily recommendation metrics used by operator dashboards."""

    __tablename__ = "recommendation_daily_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "metric_date",
            "surface",
            "algorithm_version",
            "profile_version",
            "candidate_source",
            name="uq_recommendation_daily_aggregate_dimensions",
        ),
        CheckConstraint("impression_count >= 0", name="recommendation_daily_impressions_non_negative"),
        CheckConstraint("strong_action_count >= 0", name="recommendation_daily_actions_non_negative"),
        CheckConstraint("attributed_send_count >= 0", name="recommendation_daily_sends_non_negative"),
        CheckConstraint("result_count >= 0", name="recommendation_daily_results_non_negative"),
        CheckConstraint("exploration_count >= 0", name="recommendation_daily_exploration_non_negative"),
        CheckConstraint("cache_expiry_count >= 0", name="recommendation_daily_cache_expiry_non_negative"),
        CheckConstraint("fallback_count >= 0", name="recommendation_daily_fallback_non_negative"),
        Index("ix_recommendation_daily_aggregates_date_surface", "metric_date", "surface"),
    )

    metric_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    surface: Mapped[str] = mapped_column(String(120), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(120), nullable=False, default="none")
    candidate_source: Mapped[str] = mapped_column(String(120), nullable=False, default="unknown")
    impression_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strong_action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributed_send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exploration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_expiry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)


__all__ = [
    "RecommendationDailyAggregate",
    "UserMemeRecommendationState",
    "UserRecommendationProfile",
    "UserRecommendationProfileSignal",
    "UserRecommendationProfileStatus",
]
