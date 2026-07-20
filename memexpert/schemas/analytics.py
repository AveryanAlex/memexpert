# ruff: noqa: TC001,TC003
"""Public-safe request and acknowledgement schemas for product telemetry."""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memexpert.models.enums import AnalyticsEventType

INTERACTION_BATCH_MAX_BYTES = 48 * 1024
INTERACTION_BATCH_MAX_EVENTS = 50
INTERACTION_PROPERTIES_MAX_DEPTH = 4
INTERACTION_PROPERTIES_MAX_CONTAINER_MEMBERS = 64
INTERACTION_PROPERTIES_MAX_TOTAL_MEMBERS = 128
INTERACTION_PROPERTIES_MAX_KEY_BYTES = 128
INTERACTION_PROPERTIES_MAX_STRING_BYTES = 8192


def _serialized_json_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _validate_bounded_properties(value: object) -> None:
    """Validate a small JSON object iteratively before it reaches analytics writers."""

    if not isinstance(value, dict):
        raise ValueError("properties must be a JSON object")

    total_members = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if item is None or isinstance(item, (bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("properties must not contain non-finite numbers")
            continue
        if isinstance(item, str):
            if len(item.encode("utf-8")) > INTERACTION_PROPERTIES_MAX_STRING_BYTES:
                raise ValueError(
                    f"properties strings may contain at most {INTERACTION_PROPERTIES_MAX_STRING_BYTES} UTF-8 bytes"
                )
            continue
        if not isinstance(item, (dict, list)):
            raise ValueError("properties must contain only JSON values")
        if depth > INTERACTION_PROPERTIES_MAX_DEPTH:
            raise ValueError(f"properties nesting may not exceed {INTERACTION_PROPERTIES_MAX_DEPTH} levels")
        if len(item) > INTERACTION_PROPERTIES_MAX_CONTAINER_MEMBERS:
            raise ValueError(
                "properties containers may contain at most "
                f"{INTERACTION_PROPERTIES_MAX_CONTAINER_MEMBERS} members"
            )

        total_members += len(item)
        if total_members > INTERACTION_PROPERTIES_MAX_TOTAL_MEMBERS:
            raise ValueError(
                f"properties may contain at most {INTERACTION_PROPERTIES_MAX_TOTAL_MEMBERS} total members"
            )

        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("properties keys must be strings")
                if len(key.encode("utf-8")) > INTERACTION_PROPERTIES_MAX_KEY_BYTES:
                    raise ValueError(
                        f"properties keys may contain at most {INTERACTION_PROPERTIES_MAX_KEY_BYTES} UTF-8 bytes"
                    )
                pending.append((child, depth + 1))
        else:
            pending.extend((child, depth + 1) for child in item)


class ConsumerPageSurface(StrEnum):
    """Approved route categories for first-party consumer page views.

    These values intentionally describe a broad surface rather than a route,
    URL, slug, query string, referrer, or other visitor-specific value.
    """

    WEB_ACCOUNT = "web_account"
    WEB_COLLECTION = "web_collection"
    WEB_HOME = "web_home"
    WEB_LIBRARY = "web_library"
    WEB_MEME_DETAIL = "web_meme_detail"
    WEB_PROFILE = "web_profile"
    WEB_SEARCH = "web_search"
    WEB_TAG = "web_tag"
    WEB_TEMPLATE = "web_template"
    WEB_TRENDS = "web_trends"


class PageViewCreateRequest(BaseModel):
    """One browser-reported first-party consumer route visit."""

    model_config = ConfigDict(extra="forbid")

    surface: ConsumerPageSurface


class PageViewRecordedRead(BaseModel):
    """Best-effort acknowledgement that deliberately exposes no event metadata."""

    ok: bool = True


class InteractionBatchEventCreate(BaseModel):
    """One idempotent trusted-attribution interaction in a browser batch."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: AnalyticsEventType
    meme_id: uuid.UUID
    occurred_at: datetime
    attribution_token: str | None = Field(default=None, min_length=1, max_length=8192)
    properties: dict[str, object] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, value: uuid.UUID) -> uuid.UUID:
        if value.version != 7:
            raise ValueError("event_id must be a UUIDv7")
        return value

    @field_validator("properties", mode="before")
    @classmethod
    def _validate_properties(cls, value: object) -> object:
        _validate_bounded_properties(value)
        return value

    @model_validator(mode="after")
    def _validate_event_contract(self) -> InteractionBatchEventCreate:
        if self.event_type not in {
            AnalyticsEventType.MEME_DETAIL_CLICK,
            AnalyticsEventType.MEME_ENGAGED_VIEW,
            AnalyticsEventType.MEME_IMPRESSION,
        }:
            raise ValueError("Batched interactions support impression, engaged-view, and detail-click events.")
        serialized_size = _serialized_json_size({"events": [self.model_dump(mode="json")]})
        if serialized_size > INTERACTION_BATCH_MAX_BYTES:
            raise ValueError(
                f"A serialized interaction event may contain at most {INTERACTION_BATCH_MAX_BYTES} bytes"
            )
        return self


class InteractionBatchCreateRequest(BaseModel):
    """A bounded analytics batch suitable for retry and keepalive delivery."""

    model_config = ConfigDict(extra="forbid")

    events: list[InteractionBatchEventCreate] = Field(
        min_length=1,
        max_length=INTERACTION_BATCH_MAX_EVENTS,
    )

    @model_validator(mode="after")
    def _validate_serialized_size(self) -> InteractionBatchCreateRequest:
        serialized_size = _serialized_json_size(self.model_dump(mode="json"))
        if serialized_size > INTERACTION_BATCH_MAX_BYTES:
            raise ValueError(
                f"A serialized interaction batch may contain at most {INTERACTION_BATCH_MAX_BYTES} bytes"
            )
        return self


class InteractionBatchRecordedRead(BaseModel):
    """Counts that let clients distinguish new writes from safe retries."""

    recorded: int = Field(ge=0)
    duplicates: int = Field(ge=0)


__all__ = [
    "ConsumerPageSurface",
    "INTERACTION_BATCH_MAX_BYTES",
    "INTERACTION_BATCH_MAX_EVENTS",
    "INTERACTION_PROPERTIES_MAX_CONTAINER_MEMBERS",
    "INTERACTION_PROPERTIES_MAX_DEPTH",
    "INTERACTION_PROPERTIES_MAX_KEY_BYTES",
    "INTERACTION_PROPERTIES_MAX_STRING_BYTES",
    "INTERACTION_PROPERTIES_MAX_TOTAL_MEMBERS",
    "InteractionBatchCreateRequest",
    "InteractionBatchEventCreate",
    "InteractionBatchRecordedRead",
    "PageViewCreateRequest",
    "PageViewRecordedRead",
]
