"""Focused coverage for signed viewer-bound result attribution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from memexpert.schemas.meme import (
    MemeResultAttributionFiltersRead,
    MemeResultAttributionRead,
    RecommendationCandidateSource,
    RecommendationCandidateSourceContributionRead,
)
from memexpert.services.recommendations.attribution import (
    AttributionTokenExpiredError,
    AttributionTokenMismatchError,
    AttributionTokenService,
)

SECRET = "recommendation-attribution-test-secret-32-bytes-minimum"


def _attribution() -> MemeResultAttributionRead:
    return MemeResultAttributionRead(
        request_id="req-1",
        impression_id="imp-1",
        surface="web_home",
        source_algorithm="personalized",
        rank=3,
        query="private raw query",
        filters=MemeResultAttributionFiltersRead(
            scope="collections",
            collection_ids=[str(uuid.uuid7())],
        ),
        collection_scope="collections",
        collection_ids=["private-collection"],
        algorithm_version="personalized_v2",
        profile_version="taste_v2-a1",
        score=0.75,
        candidate_sources=[
            RecommendationCandidateSourceContributionRead(
                source=RecommendationCandidateSource.SHORT_TERM,
                rank=2,
                score=0.88,
                contribution=0.016,
            )
        ],
        reason="ranked",
    )


def test_token_round_trip_binds_meme_and_viewer_and_omits_private_context() -> None:
    service = AttributionTokenService(secret=SECRET, ttl=timedelta(hours=1))
    viewer_id = uuid.uuid7()
    meme_id = uuid.uuid7()
    issued_at = datetime.now(UTC)

    token = service.issue_for_result(
        meme_id=meme_id,
        viewer_user_id=viewer_id,
        attribution=_attribution(),
        issued_at=issued_at,
    )
    claims = service.verify(token, expected_meme_id=meme_id, viewer_user_id=viewer_id)
    unsigned_payload = jwt.decode(token, options={"verify_signature": False})

    assert claims.meme_id == meme_id
    assert claims.request_id == "req-1"
    assert claims.impression_id == "imp-1"
    assert claims.profile_version == "taste_v2-a1"
    assert claims.candidate_sources[0].source is RecommendationCandidateSource.SHORT_TERM
    assert claims.candidate_sources[0].score == 0.88
    assert "query" not in unsigned_payload
    assert "filters" not in unsigned_payload
    assert "collection_ids" not in unsigned_payload
    assert str(viewer_id) not in token


def test_token_rejects_another_meme_viewer_and_expiry() -> None:
    service = AttributionTokenService(secret=SECRET, ttl=timedelta(seconds=1))
    viewer_id = uuid.uuid7()
    meme_id = uuid.uuid7()
    token = service.issue_for_result(
        meme_id=meme_id,
        viewer_user_id=viewer_id,
        attribution=_attribution(),
        issued_at=datetime.now(UTC) - timedelta(seconds=2),
    )

    with pytest.raises(AttributionTokenExpiredError):
        service.verify(token, expected_meme_id=meme_id, viewer_user_id=viewer_id)

    fresh_service = AttributionTokenService(secret=SECRET, ttl=timedelta(hours=1))
    fresh_token = fresh_service.issue_for_result(
        meme_id=meme_id,
        viewer_user_id=viewer_id,
        attribution=_attribution(),
    )
    with pytest.raises(AttributionTokenMismatchError):
        fresh_service.verify(fresh_token, expected_meme_id=uuid.uuid7(), viewer_user_id=viewer_id)
    with pytest.raises(AttributionTokenMismatchError):
        fresh_service.verify(fresh_token, expected_meme_id=meme_id, viewer_user_id=uuid.uuid7())


def test_anonymous_token_can_transition_without_weakening_concrete_viewer_binding() -> None:
    service = AttributionTokenService(secret=SECRET, ttl=timedelta(hours=1))
    meme_id = uuid.uuid7()
    original_viewer_id = uuid.uuid7()
    bootstrapped_guest_id = uuid.uuid7()
    another_viewer_id = uuid.uuid7()
    anonymous_token = service.issue_for_result(
        meme_id=meme_id,
        viewer_user_id=None,
        attribution=_attribution(),
    )

    claims = service.verify(
        anonymous_token,
        expected_meme_id=meme_id,
        viewer_user_id=bootstrapped_guest_id,
        allow_anonymous_viewer_transition=True,
    )

    assert claims.impression_id == "imp-1"
    with pytest.raises(AttributionTokenMismatchError):
        service.verify(
            anonymous_token,
            expected_meme_id=meme_id,
            viewer_user_id=bootstrapped_guest_id,
        )

    concrete_token = service.issue_for_result(
        meme_id=meme_id,
        viewer_user_id=original_viewer_id,
        attribution=_attribution(),
    )
    with pytest.raises(AttributionTokenMismatchError):
        service.verify(
            concrete_token,
            expected_meme_id=meme_id,
            viewer_user_id=another_viewer_id,
            allow_anonymous_viewer_transition=True,
        )
