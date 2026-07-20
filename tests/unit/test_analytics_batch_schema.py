"""Bounded request-schema coverage for browser interaction telemetry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memexpert.models.enums import AnalyticsEventType
from memexpert.schemas.analytics import (
    INTERACTION_BATCH_MAX_EVENTS,
    INTERACTION_PROPERTIES_MAX_CONTAINER_MEMBERS,
    INTERACTION_PROPERTIES_MAX_DEPTH,
    INTERACTION_PROPERTIES_MAX_KEY_BYTES,
    INTERACTION_PROPERTIES_MAX_STRING_BYTES,
    InteractionBatchCreateRequest,
    InteractionBatchEventCreate,
)


def _event(*, properties: dict[str, object] | None = None) -> InteractionBatchEventCreate:
    return InteractionBatchEventCreate(
        event_id=uuid.uuid7(),
        event_type=AnalyticsEventType.MEME_IMPRESSION,
        meme_id=uuid.uuid7(),
        occurred_at=datetime.now(UTC),
        properties=properties or {},
    )


@pytest.mark.parametrize(
    ("properties", "message"),
    [
        (
            {"nested": {"value": {"value": {"value": {"value": {}}}}}},
            "properties nesting",
        ),
        (
            {str(index): index for index in range(INTERACTION_PROPERTIES_MAX_CONTAINER_MEMBERS + 1)},
            "properties containers",
        ),
        (
            {"a": [0] * 64, "b": [0] * 64},
            "properties may contain at most",
        ),
        (
            {"k" * (INTERACTION_PROPERTIES_MAX_KEY_BYTES + 1): True},
            "properties keys",
        ),
        (
            {"value": "x" * (INTERACTION_PROPERTIES_MAX_STRING_BYTES + 1)},
            "properties strings",
        ),
        (
            {"tuple": ("not", "json")},
            "only JSON values",
        ),
    ],
)
def test_interaction_batch_event_rejects_unbounded_properties(
    properties: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _ = _event(properties=properties)


def test_interaction_batch_event_enforces_serialized_event_limit() -> None:
    properties: dict[str, object] = {
        str(index): "x" * INTERACTION_PROPERTIES_MAX_STRING_BYTES
        for index in range(6)
    }

    with pytest.raises(ValidationError, match="serialized interaction event"):
        _ = _event(properties=properties)


def test_interaction_batch_enforces_serialized_request_limit() -> None:
    events = [
        _event(properties={"value": "x" * 7000})
        for _ in range(7)
    ]

    with pytest.raises(ValidationError, match="serialized interaction batch"):
        _ = InteractionBatchCreateRequest(events=events)


def test_interaction_batch_retains_fifty_event_limit() -> None:
    events = [_event() for _ in range(INTERACTION_BATCH_MAX_EVENTS + 1)]

    with pytest.raises(ValidationError, match="at most 50 items"):
        _ = InteractionBatchCreateRequest(events=events)


def test_interaction_batch_accepts_maximum_supported_property_depth() -> None:
    properties: dict[str, object] = {"value": True}
    for _ in range(INTERACTION_PROPERTIES_MAX_DEPTH):
        properties = {"nested": properties}

    event = _event(properties=properties)

    assert event.properties == properties
