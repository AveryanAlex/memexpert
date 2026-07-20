# ruff: noqa: TC003
"""Pure recommendation-policy tests that do not need provider containers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from memexpert.core.config import Settings
from memexpert.models.enums import AnalyticsEventType
from memexpert.services.recommendations.candidates import (
    CandidateHit,
    CandidateRanking,
    CandidateSource,
    fuse_candidate_rankings,
)
from memexpert.services.recommendations.evaluation import EvaluationItem, evaluate_ranking
from memexpert.services.recommendations.features import (
    RecommendationItemFeatures,
    bayesian_response_rate,
    percentile75,
    smoothed_source_quality_rate,
)
from memexpert.services.recommendations.profiles import ProfileSignalVector, build_profile_vectors
from memexpert.services.recommendations.ranking import RankableCandidate, diversity_rerank, score_home_candidates
from memexpert.services.recommendations.service import _personalized_v2_serving_enabled
from memexpert.services.recommendations.signals import RawRecommendationSignal, signal_policy_for, weight_signals

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def test_removal_cancels_durable_contribution_without_becoming_negative() -> None:
    assert signal_policy_for(AnalyticsEventType.MEME_SAVE, action="add") is not None
    assert signal_policy_for(AnalyticsEventType.MEME_SAVE, action="remove") is None
    assert signal_policy_for(AnalyticsEventType.MEME_PIN, action="reorder_pin") is None


def test_send_family_is_deduplicated_per_impression_before_decay() -> None:
    meme_id = uuid.uuid7()
    impression_id = "imp_shared"
    signals = [
        RawRecommendationSignal(
            event_id=uuid.uuid7(),
            meme_id=meme_id,
            event_type=event_type,
            occurred_at=NOW,
            impression_id=impression_id,
        )
        for event_type in (
            AnalyticsEventType.INLINE_CHOSEN,
            AnalyticsEventType.INLINE_SENT,
            AnalyticsEventType.MEME_SEND,
        )
    ]

    weighted = weight_signals(signals, now=NOW, half_life_seconds=86400)

    assert len(weighted) == 1
    assert weighted[0].weight == pytest.approx(4.0)
    assert weighted[0].is_strong_positive is True


def test_short_term_signal_weight_has_24_hour_half_life() -> None:
    signal = RawRecommendationSignal(
        event_id=uuid.uuid7(),
        meme_id=uuid.uuid7(),
        event_type=AnalyticsEventType.MEME_ENGAGED_VIEW,
        occurred_at=NOW - timedelta(hours=24),
    )
    weighted = weight_signals([signal], now=NOW, half_life_seconds=86400)
    assert weighted[0].weight == pytest.approx(1.0)


def test_cluster_profiles_are_deterministic_and_global_is_always_retained() -> None:
    signals: list[ProfileSignalVector] = []
    for index in range(24):
        vector = (1.0, 0.0) if index < 12 else (0.0, 1.0)
        signals.append(
            ProfileSignalVector(
                meme_id=uuid.UUID(int=index + 1),
                vector=vector,
                weight=5.0,
                last_signal_at=NOW - timedelta(minutes=index),
                is_strong_positive=True,
            )
        )

    first = build_profile_vectors(signals)
    second = build_profile_vectors(list(reversed(signals)))

    assert first == second
    assert [profile.slot for profile in first] == [0, 1, 2]
    assert first[0].signal_count == 24
    assert {profile.signal_count for profile in first[1:]} == {12}


def test_clustering_is_not_activated_below_twenty_strong_memes() -> None:
    signals = [
        ProfileSignalVector(
            meme_id=uuid.UUID(int=index + 1),
            vector=(1.0, float(index % 2)),
            weight=5.0,
            last_signal_at=NOW,
            is_strong_positive=True,
        )
        for index in range(19)
    ]
    assert len(build_profile_vectors(signals)) == 1


def test_rrf_normalizes_cluster_group_total_contribution() -> None:
    meme_id = uuid.uuid7()
    single = fuse_candidate_rankings(
        [
            CandidateRanking(
                source=CandidateSource.LONG_TERM_CLUSTER,
                source_key="cluster:0",
                normalization_group="long_term_clusters",
                hits=(CandidateHit(meme_id=meme_id, score=1.0),),
            )
        ]
    )[0]
    multiple = fuse_candidate_rankings(
        [
            CandidateRanking(
                source=CandidateSource.LONG_TERM_CLUSTER,
                source_key=f"cluster:{index}",
                normalization_group="long_term_clusters",
                hits=(CandidateHit(meme_id=meme_id, score=1.0),),
            )
            for index in range(4)
        ]
    )[0]
    assert multiple.fused_score == pytest.approx(single.fused_score)


def test_feature_priors_are_neutral_and_use_continuous_p75() -> None:
    assert bayesian_response_rate(strong_actions=0, impressions=0, surface_mean=0.5) == 0.5
    assert bayesian_response_rate(strong_actions=1, impressions=2, surface_mean=0.1) == pytest.approx(3 / 22)
    assert smoothed_source_quality_rate(
        views=100,
        reactions=10,
        forwards=2,
        comments=4,
        cohort_mean=0.1,
    ) == pytest.approx((10 + 6 + 2 + 10) / 200)
    assert percentile75([0.0, 1.0]) == pytest.approx(0.75)


def test_diversity_caps_source_and_template_and_reserves_exploration() -> None:
    source = uuid.uuid7()
    template = uuid.uuid7()
    candidates: list[RankableCandidate] = []
    for index in range(35):
        is_repeated = index < 10
        feature = RecommendationItemFeatures(
            meme_id=uuid.UUID(int=index + 1),
            latest_published_at=NOW,
            source_channel_ids=(source,) if is_repeated else (uuid.UUID(int=1000 + index),),
            representative_source_channel_id=source if is_repeated else uuid.UUID(int=1000 + index),
            template_id=template if is_repeated else uuid.UUID(int=2000 + index),
        )
        candidates.append(
            RankableCandidate(
                meme_id=feature.meme_id,
                fused_score=35 - index,
                personal_fit=1.0,
                current_intent=0.0,
                features=feature,
                embedding=(1.0, 0.0) if is_repeated else (0.0, 1.0),
                is_exploration=index == 34,
            )
        )

    scored = score_home_candidates(candidates)
    reranked = diversity_rerank(scored, limit=20)

    assert sum(item.features.representative_source_channel_id == source for item in reranked) <= 2
    assert sum(item.features.template_id == template for item in reranked) <= 2
    assert any(item.is_exploration for item in reranked)


def test_offline_evaluator_reports_recall_coverage_and_diversity() -> None:
    relevant = {uuid.UUID(int=1), uuid.UUID(int=3)}
    ranked = [
        EvaluationItem(meme_id=uuid.UUID(int=1), relevance=1.0, embedding=(1.0, 0.0)),
        EvaluationItem(meme_id=uuid.UUID(int=2), relevance=0.0, embedding=(0.0, 1.0)),
    ]
    result = evaluate_ranking(ranked, relevant_meme_ids=relevant, catalog_size=10, k=2)
    assert result.recall_at_k == 0.5
    assert result.catalog_coverage == 0.2
    assert result.intra_list_diversity == pytest.approx(1.0)


def test_personalized_v2_canary_is_deterministic_and_shadow_never_serves() -> None:
    viewer_id = uuid.uuid7()
    assert not _personalized_v2_serving_enabled(Settings(), viewer_id)
    assert _personalized_v2_serving_enabled(
        Settings(
            recommendation_enabled=True,
            recommendation_shadow_mode=False,
            recommendation_canary_percent=100,
        ),
        viewer_id,
    )
    assert not _personalized_v2_serving_enabled(
        Settings(
            recommendation_enabled=True,
            recommendation_shadow_mode=False,
            recommendation_canary_percent=0,
        ),
        viewer_id,
    )
    assert not _personalized_v2_serving_enabled(
        Settings(
            recommendation_enabled=False,
            recommendation_shadow_mode=False,
            recommendation_canary_percent=100,
        ),
        viewer_id,
    )
    assert not _personalized_v2_serving_enabled(
        Settings(
            recommendation_enabled=True,
            recommendation_canary_percent=100,
            recommendation_shadow_mode=True,
        ),
        viewer_id,
    )
    settings = Settings(
        recommendation_enabled=True,
        recommendation_shadow_mode=False,
        recommendation_canary_percent=37,
    )
    assert _personalized_v2_serving_enabled(settings, viewer_id) == _personalized_v2_serving_enabled(
        settings,
        viewer_id,
    )
