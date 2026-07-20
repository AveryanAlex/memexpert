"""Transport-level limits for the public interaction batch endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from memexpert.schemas.analytics import (
    INTERACTION_BATCH_MAX_BYTES,
    INTERACTION_PROPERTIES_MAX_DEPTH,
    INTERACTION_PROPERTIES_MAX_STRING_BYTES,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_BATCH_PATH = "/api/v1/analytics/interactions/batch"


def _event_payload(*, properties: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "event_id": str(uuid.uuid7()),
        "event_type": "meme_impression",
        "meme_id": str(uuid.uuid7()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "properties": properties or {},
    }


async def test_interaction_batch_rejects_declared_oversized_body(client: AsyncClient) -> None:
    response = await client.post(
        _BATCH_PATH,
        content=b" " * (INTERACTION_BATCH_MAX_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": f"Interaction batch requests may contain at most {INTERACTION_BATCH_MAX_BYTES} bytes."
    }


async def test_interaction_batch_rejects_chunked_oversized_body(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"events":['
        yield b" " * INTERACTION_BATCH_MAX_BYTES

    response = await client.post(
        _BATCH_PATH,
        content=chunks(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413


async def test_interaction_batch_rejects_excessive_json_nesting_before_decode(
    client: AsyncClient,
) -> None:
    depth = INTERACTION_PROPERTIES_MAX_DEPTH + 5
    payload = b"[" * depth + b"null" + b"]" * depth

    response = await client.post(
        _BATCH_PATH,
        content=payload,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "maximum supported nesting depth" in response.json()["detail"]


async def test_interaction_batch_rejects_malformed_json(client: AsyncClient) -> None:
    response = await client.post(
        _BATCH_PATH,
        content=b'{"events":[',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


async def test_interaction_batch_rejects_oversized_property_string(client: AsyncClient) -> None:
    response = await client.post(
        _BATCH_PATH,
        json={
            "events": [
                _event_payload(
                    properties={"value": "x" * (INTERACTION_PROPERTIES_MAX_STRING_BYTES + 1)}
                )
            ]
        },
    )

    assert response.status_code == 422
    assert "properties strings" in str(response.json()["detail"])
