# ruff: noqa: TC001,TC003
"""Signed, viewer-bound recommendation attribution tokens."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from memexpert.core.config import Settings, get_settings
from memexpert.schemas.meme import (
    MemeResultAttributionRead,
    RecommendationCandidateSourceContributionRead,
)

ATTRIBUTION_TOKEN_KIND = "meme_attribution"
ATTRIBUTION_TOKEN_VERSION = 1
ATTRIBUTION_TOKEN_ALGORITHM = "HS256"
_SIGNING_KEY_DOMAIN = b"memexpert:recommendation-attribution:v1\0"
_VIEWER_KEY_DOMAIN = b"memexpert:recommendation-viewer:v1\0"


class AttributionTokenError(ValueError):
    """Base error for an attribution token that cannot be trusted."""


class AttributionTokenExpiredError(AttributionTokenError):
    """Raised when otherwise valid attribution has passed its expiry."""


class AttributionTokenMismatchError(AttributionTokenError):
    """Raised when attribution is replayed for another meme or viewer."""


class AttributionTokenClaims(BaseModel):
    """Typed, privacy-bounded claims signed into one result attribution token."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["meme_attribution"] = ATTRIBUTION_TOKEN_KIND
    version: Literal[1] = ATTRIBUTION_TOKEN_VERSION
    meme_id: uuid.UUID
    viewer_key: str = Field(min_length=64, max_length=64)
    request_id: str | None = Field(default=None, max_length=255)
    impression_id: str = Field(min_length=1, max_length=255)
    surface: str = Field(min_length=1, max_length=120)
    source_algorithm: str | None = Field(default=None, max_length=120)
    rank: int | None = Field(default=None, ge=0)
    score: float | None = None
    candidate_sources: list[RecommendationCandidateSourceContributionRead] = Field(default_factory=list)
    algorithm_version: str | None = Field(default=None, max_length=120)
    profile_version: str | None = Field(default=None, max_length=120)
    source_meme_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=255)
    iat: datetime
    exp: datetime


class AttributionTokenService:
    """Issue and verify short-lived attribution without trusting browser-authored rank data."""

    def __init__(self, *, secret: str, ttl: timedelta) -> None:
        normalized_secret = secret.strip()
        if len(normalized_secret.encode("utf-8")) < 32:
            raise ValueError("Attribution signing secret must be at least 32 bytes long.")
        if ttl <= timedelta(0):
            raise ValueError("Attribution token TTL must be positive.")
        self._signing_key = hashlib.sha256(_SIGNING_KEY_DOMAIN + normalized_secret.encode("utf-8")).digest()
        self._viewer_key_secret = hashlib.sha256(
            _VIEWER_KEY_DOMAIN + normalized_secret.encode("utf-8")
        ).digest()
        self._ttl = ttl

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AttributionTokenService:
        resolved = settings or get_settings()
        return cls(
            secret=resolved.auth_jwt_secret.get_secret_value(),
            ttl=timedelta(seconds=resolved.recommendation_attribution_token_ttl_seconds),
        )

    def issue_for_result(
        self,
        *,
        meme_id: uuid.UUID,
        viewer_user_id: uuid.UUID | None,
        attribution: MemeResultAttributionRead,
        issued_at: datetime | None = None,
    ) -> str:
        observed_at = _normalize_utc(issued_at)
        claims = AttributionTokenClaims(
            meme_id=meme_id,
            viewer_key=self._viewer_key(viewer_user_id),
            request_id=attribution.request_id,
            impression_id=attribution.impression_id,
            surface=attribution.surface or "unknown",
            source_algorithm=attribution.source_algorithm,
            rank=attribution.rank,
            score=attribution.score,
            candidate_sources=attribution.candidate_sources,
            algorithm_version=attribution.algorithm_version,
            profile_version=attribution.profile_version,
            source_meme_id=attribution.source_meme_id,
            reason=attribution.reason,
            iat=observed_at,
            exp=observed_at + self._ttl,
        )
        payload = claims.model_dump(mode="json", exclude_none=True)
        # RFC 7519 NumericDate values are integer epoch seconds. Serializing
        # Pydantic datetimes directly would produce ISO strings, which PyJWT
        # correctly rejects while validating ``iat`` and ``exp``.
        payload["iat"] = int(claims.iat.timestamp())
        payload["exp"] = int(claims.exp.timestamp())
        return jwt.encode(
            payload,
            self._signing_key,
            algorithm=ATTRIBUTION_TOKEN_ALGORITHM,
        )

    def verify(
        self,
        token: str,
        *,
        expected_meme_id: uuid.UUID,
        viewer_user_id: uuid.UUID | None,
        allow_anonymous_viewer_transition: bool = False,
    ) -> AttributionTokenClaims:
        normalized_token = token.strip()
        if not normalized_token:
            raise AttributionTokenError("Attribution token is required.")
        try:
            payload = jwt.decode(
                normalized_token,
                self._signing_key,
                algorithms=[ATTRIBUTION_TOKEN_ALGORITHM],
                options={
                    "require": [
                        "kind",
                        "version",
                        "meme_id",
                        "viewer_key",
                        "impression_id",
                        "surface",
                        "iat",
                        "exp",
                    ]
                },
            )
        except ExpiredSignatureError as exc:
            raise AttributionTokenExpiredError("Attribution token has expired.") from exc
        except InvalidTokenError as exc:
            raise AttributionTokenError("Attribution token is invalid.") from exc

        try:
            claims = AttributionTokenClaims.model_validate(payload)
        except ValidationError as exc:
            raise AttributionTokenError("Attribution token claims are invalid.") from exc
        if claims.meme_id != expected_meme_id:
            raise AttributionTokenMismatchError("Attribution token does not match the meme.")
        expected_viewer_key = self._viewer_key(viewer_user_id)
        viewer_matches = hmac.compare_digest(claims.viewer_key, expected_viewer_key)
        anonymous_transition_matches = (
            allow_anonymous_viewer_transition
            and viewer_user_id is not None
            and hmac.compare_digest(claims.viewer_key, self._viewer_key(None))
        )
        if not viewer_matches and not anonymous_transition_matches:
            raise AttributionTokenMismatchError("Attribution token does not match the viewer.")
        return claims

    def _viewer_key(self, viewer_user_id: uuid.UUID | None) -> str:
        viewer_bytes = viewer_user_id.bytes if viewer_user_id is not None else b"anonymous"
        return hmac.new(self._viewer_key_secret, viewer_bytes, hashlib.sha256).hexdigest()


def sign_result_attribution(
    attribution: MemeResultAttributionRead,
    *,
    meme_id: uuid.UUID,
    viewer_user_id: uuid.UUID | None,
    settings: Settings | None = None,
) -> MemeResultAttributionRead:
    """Return attribution with a server-issued token bound to its meme and viewer."""

    token = AttributionTokenService.from_settings(settings).issue_for_result(
        meme_id=meme_id,
        viewer_user_id=viewer_user_id,
        attribution=attribution,
    )
    return attribution.model_copy(update={"attribution_token": token})


def _normalize_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "AttributionTokenClaims",
    "AttributionTokenError",
    "AttributionTokenExpiredError",
    "AttributionTokenMismatchError",
    "AttributionTokenService",
    "sign_result_attribution",
]
