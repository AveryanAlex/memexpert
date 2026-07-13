# ruff: noqa: TC001,TC003
"""Shared request and persistence helpers for meme interaction attribution."""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field

from memexpert.models.enums import AnalyticsEventType, ContentKind, ContentLanguage
from memexpert.schemas.user import UserRead
from memexpert.services.analytics import AnalyticsService, InteractionEventRefs, InteractionEventWrite

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

    attribution: MemeInteractionAttributionRequest | None = None


async def record_meme_interaction(
    analytics_service: AnalyticsService,
    event_type: AnalyticsEventType,
    *,
    meme_id: uuid.UUID,
    current_user: UserRead | None,
    attribution: MemeInteractionAttributionRequest | None,
    default_surface: str,
    properties: dict[str, object] | None = None,
    collection_id: uuid.UUID | None = None,
    report_id: uuid.UUID | None = None,
) -> None:
    """Persist a strict interaction event without making the product action fail on analytics errors."""

    attribution_properties = _attribution_properties(attribution)
    user_id = current_user.id if current_user else None
    surface = attribution.surface if attribution and attribution.surface else default_surface
    try:
        await analytics_service.record_interaction_event(
            InteractionEventWrite(
                event_type=event_type,
                user_id=user_id,
                surface=surface,
                refs=InteractionEventRefs(
                    collection_id=collection_id,
                    meme_id=meme_id,
                    report_id=report_id,
                    source_meme_id=attribution.source_meme_id if attribution else None,
                ),
                request_id=attribution.request_id if attribution else None,
                impression_id=attribution.impression_id if attribution else None,
                source_algorithm=attribution.source_algorithm if attribution else None,
                query=attribution.query if attribution else None,
                rank=attribution.rank if attribution else None,
                score=attribution.score if attribution else None,
                score_components=attribution.score_components if attribution else {},
                reason=attribution.reason if attribution else None,
                properties={**(properties or {}), **attribution_properties},
            )
        )
    except Exception:
        logger.exception(
            "Meme interaction analytics write failed.",
            extra={
                "analytics_event_type": event_type.value,
                "meme_id": str(meme_id),
                "user_id": str(user_id) if user_id else None,
                "request_id": attribution.request_id if attribution else None,
                "impression_id": attribution.impression_id if attribution else None,
                "surface": surface,
            },
        )


def payload_attribution(
    payload: MemeActionAttributionRequest | None,
) -> MemeInteractionAttributionRequest | None:
    """Return the optional nested attribution payload used by action endpoints."""

    return payload.attribution if payload else None


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
    "payload_attribution",
    "record_meme_interaction",
]
