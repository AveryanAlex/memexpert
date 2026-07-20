# ruff: noqa: TC001,TC003
"""Shared request and persistence helpers for meme interaction attribution."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memexpert.models.enums import AccountType, AnalyticsEventType, ContentKind, ContentLanguage
from memexpert.schemas.user import UserRead
from memexpert.services.analytics import (
    AnalyticsService,
    InteractionEventIdConflictError,
    InteractionEventRefs,
    InteractionEventWrite,
)
from memexpert.services.recommendations.attribution import (
    AttributionTokenClaims,
    AttributionTokenError,
    AttributionTokenService,
)

logger = logging.getLogger(__name__)


class MemeInteractionAttributionFiltersRequest(BaseModel):
    """Public-safe discovery filters forwarded into interaction analytics properties."""

    model_config = ConfigDict(extra="forbid")

    language: ContentLanguage | None = None
    media_type: ContentKind | None = None
    include_nsfw: bool | None = None
    tags: list[str] = Field(default_factory=list)
    scope: str | None = Field(default=None, max_length=120)
    collection_ids: list[str] = Field(default_factory=list)


class MemeInteractionAttributionRequest(BaseModel):
    """Strict event attribution fields accepted from detail links and action bodies."""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, max_length=255)
    impression_id: str | None = Field(default=None, max_length=255)
    surface: str | None = Field(default=None, max_length=120)
    source_algorithm: str | None = Field(default=None, max_length=120)
    rank: int | None = Field(default=None, ge=0)
    query: str | None = Field(default=None, max_length=500)
    filters: MemeInteractionAttributionFiltersRequest | None = None
    collection_scope: str | None = Field(default=None, max_length=120)
    collection_ids: list[str] = Field(default_factory=list)
    source_meme_id: uuid.UUID | None = None
    algorithm_version: str | None = Field(default=None, max_length=120)
    score: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=255)


class MemeActionAttributionRequest(BaseModel):
    """Optional action body wrapper for discovery attribution."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID | None = None
    attribution_token: str | None = Field(default=None, min_length=1, max_length=8192)
    attribution: MemeInteractionAttributionRequest | None = None

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, value: uuid.UUID | None) -> uuid.UUID | None:
        if value is not None and value.version != 7:
            raise ValueError("event_id must be a UUIDv7")
        return value


@dataclass(frozen=True, slots=True)
class ResolvedMemeInteractionRequest:
    """Server-resolved attribution; signed claims always win over legacy fields."""

    event_id: uuid.UUID | None = None
    claims: AttributionTokenClaims | None = None
    legacy_attribution: MemeInteractionAttributionRequest | None = None

    @property
    def trusted(self) -> bool:
        return self.claims is not None


def resolve_meme_interaction_request(
    payload: MemeActionAttributionRequest | None,
    *,
    meme_id: uuid.UUID,
    current_user: UserRead | None,
    token_service: AttributionTokenService | None = None,
) -> ResolvedMemeInteractionRequest:
    """Verify signed attribution before a route performs a product mutation."""

    if payload is None:
        return ResolvedMemeInteractionRequest()
    claims = None
    if payload.attribution_token is not None:
        try:
            claims = (token_service or AttributionTokenService.from_settings()).verify(
                payload.attribution_token,
                expected_meme_id=meme_id,
                viewer_user_id=current_user.id if current_user else None,
                # Anonymous browse/search results can immediately trigger an
                # AutoGuestUserDep mutation. That first write creates the
                # concrete guest before token verification runs, so accept
                # only the signed anonymous viewer key as the transition
                # source. A token for any other UUID still fails verification.
                allow_anonymous_viewer_transition=(
                    current_user is not None and current_user.account_type is AccountType.GUEST
                ),
            )
        except AttributionTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Attribution token is invalid or expired.",
            ) from exc
    return ResolvedMemeInteractionRequest(
        event_id=payload.event_id,
        claims=claims,
        legacy_attribution=None if claims is not None else payload.attribution,
    )


async def record_meme_interaction(
    analytics_service: AnalyticsService,
    event_type: AnalyticsEventType,
    *,
    meme_id: uuid.UUID,
    current_user: UserRead | None,
    interaction: ResolvedMemeInteractionRequest,
    default_surface: str,
    properties: dict[str, object] | None = None,
    collection_id: uuid.UUID | None = None,
    report_id: uuid.UUID | None = None,
) -> None:
    """Persist a strict interaction event without making the product action fail on analytics errors."""

    write = build_meme_interaction_write(
        event_type,
        meme_id=meme_id,
        current_user=current_user,
        interaction=interaction,
        default_surface=default_surface,
        properties=properties,
        collection_id=collection_id,
        report_id=report_id,
    )
    try:
        await analytics_service.record_interaction_event(write)
    except InteractionEventIdConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="event_id is already assigned to another interaction.",
        ) from exc
    except Exception:
        logger.exception(
            "Meme interaction analytics write failed.",
            extra={
                "analytics_event_type": event_type.value,
                "meme_id": str(meme_id),
                "user_id": str(write.user_id) if write.user_id else None,
                "request_id": write.request_id,
                "impression_id": write.impression_id,
                "surface": write.surface,
            },
        )


def build_meme_interaction_write(
    event_type: AnalyticsEventType,
    *,
    meme_id: uuid.UUID,
    current_user: UserRead | None,
    interaction: ResolvedMemeInteractionRequest,
    default_surface: str,
    properties: dict[str, object] | None = None,
    collection_id: uuid.UUID | None = None,
    report_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> InteractionEventWrite:
    """Build the canonical event write from either signed claims or legacy attribution."""

    claims = interaction.claims
    attribution = interaction.legacy_attribution
    attribution_properties = _resolved_attribution_properties(interaction)
    event_properties = dict(properties or {})
    client_impression_id = event_properties.pop("impression_id", None)
    attributed_impression_id = (
        claims.impression_id
        if claims is not None
        else attribution.impression_id
        if attribution and attribution.impression_id is not None
        else None
    )
    if attributed_impression_id is None:
        attributed_impression_id = _validate_client_impression_id(client_impression_id)
    user_id = current_user.id if current_user else None
    surface = (
        claims.surface
        if claims is not None
        else attribution.surface if attribution and attribution.surface else default_surface
    )
    return InteractionEventWrite(
        event_type=event_type,
        event_id=interaction.event_id,
        user_id=user_id,
        occurred_at=occurred_at,
        surface=surface,
        refs=InteractionEventRefs(
            collection_id=collection_id,
            meme_id=meme_id,
            report_id=report_id,
            source_meme_id=(
                claims.source_meme_id
                if claims is not None
                else attribution.source_meme_id if attribution else None
            ),
        ),
        request_id=claims.request_id if claims is not None else attribution.request_id if attribution else None,
        impression_id=attributed_impression_id,
        source_algorithm=(
            claims.source_algorithm
            if claims is not None
            else attribution.source_algorithm if attribution else None
        ),
        query=None if claims is not None else attribution.query if attribution else None,
        rank=claims.rank if claims is not None else attribution.rank if attribution else None,
        score=claims.score if claims is not None else attribution.score if attribution else None,
        score_components={} if claims is not None else attribution.score_components if attribution else {},
        reason=claims.reason if claims is not None else attribution.reason if attribution else None,
        properties={**event_properties, **attribution_properties},
    )


def payload_attribution(
    payload: MemeActionAttributionRequest | None,
) -> MemeInteractionAttributionRequest | None:
    """Return the optional nested attribution payload used by action endpoints."""

    return payload.attribution if payload else None


def _resolved_attribution_properties(interaction: ResolvedMemeInteractionRequest) -> dict[str, object]:
    claims = interaction.claims
    if claims is None:
        return {
            **_attribution_properties(interaction.legacy_attribution),
            "attribution_trusted": False,
        }
    properties: dict[str, object] = {
        "attribution_trusted": True,
        "attribution_token_version": claims.version,
    }
    if claims.algorithm_version:
        properties["algorithm_version"] = claims.algorithm_version
    if claims.profile_version:
        properties["profile_version"] = claims.profile_version
    if claims.candidate_sources:
        properties["candidate_sources"] = [
            source.model_dump(mode="json", exclude_none=True) for source in claims.candidate_sources
        ]
    return properties


def _validate_client_impression_id(value: object) -> str | None:
    """Promote the MemeCard transport property into one canonical identity.

    Tokenless cards (for example, private library and collection cards) have
    no server-issued placement claim, so the browser carries its generated
    placement ID in the bounded properties bag. Remove that transport-only
    duplicate and validate it before using it for projection and logical
    deduplication. Signed and legacy attribution still win in the caller.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("properties.impression_id must be a string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("properties.impression_id must be a canonical UUIDv7") from exc
    if parsed.version != 7 or str(parsed) != value:
        raise ValueError("properties.impression_id must be a canonical UUIDv7")
    return value


def _attribution_properties(attribution: MemeInteractionAttributionRequest | None) -> dict[str, object]:
    if attribution is None:
        return {}

    properties: dict[str, object] = {}
    if attribution.algorithm_version:
        properties["algorithm_version"] = attribution.algorithm_version
    if attribution.filters is not None:
        properties["filters"] = attribution.filters.model_dump(mode="json", exclude_none=True)
    if attribution.collection_scope:
        properties["collection_scope"] = attribution.collection_scope
    if attribution.collection_ids:
        properties["collection_ids"] = list(attribution.collection_ids)
    return properties


__all__ = [
    "MemeActionAttributionRequest",
    "MemeInteractionAttributionFiltersRequest",
    "MemeInteractionAttributionRequest",
    "ResolvedMemeInteractionRequest",
    "build_meme_interaction_write",
    "payload_attribution",
    "record_meme_interaction",
    "resolve_meme_interaction_request",
]
