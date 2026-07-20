# ruff: noqa: TC003
"""Privacy-bounded rolling search-intent vectors stored only in Redis."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, cast

from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import get_async_redis
from memexpert.services.recommendations.math import decode_vector, encode_vector, exponential_decay, weighted_centroid

logger = logging.getLogger(__name__)
_INTENT_KEY_PREFIX = "recommendation:intent"
_SEARCH_SIGNAL_WEIGHT = 3.0


class IntentRedisProtocol(Protocol):
    async def get(self, key: str) -> object: ...

    async def set(self, key: str, value: str, *, ex: int) -> object: ...

    async def delete(self, *keys: str) -> object: ...


@dataclass(frozen=True, slots=True)
class _StoredIntent:
    vector: tuple[float, ...]
    total_weight: float
    observed_at: float


class RecommendationIntentStore:
    """Maintain a rolling vector without storing raw search text."""

    def __init__(
        self,
        *,
        redis: IntentRedisProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._redis = redis or cast("IntentRedisProtocol", get_async_redis())
        self._settings = settings or get_settings()

    async def record_successful_search(
        self,
        *,
        user_id: uuid.UUID,
        query_vector: tuple[float, ...],
    ) -> bool:
        """Blend one successful query embedding; raw query text never enters Redis."""

        key = self._key(user_id)
        observed_at = time.time()
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                previous = self._decode(await self._redis.get(key))
                weighted_vectors: list[tuple[tuple[float, ...], float]] = [
                    (query_vector, _SEARCH_SIGNAL_WEIGHT)
                ]
                total_weight = _SEARCH_SIGNAL_WEIGHT
                if previous is not None:
                    decayed_weight = previous.total_weight * exponential_decay(
                        age_seconds=max(0.0, observed_at - previous.observed_at),
                        half_life_seconds=self._settings.recommendation_search_intent_half_life_minutes * 60.0,
                    )
                    if decayed_weight > 1e-6:
                        weighted_vectors.append((previous.vector, decayed_weight))
                        total_weight += decayed_weight
                vector = weighted_centroid(weighted_vectors)
                if vector is None:
                    return False
                await self._redis.set(
                    key,
                    self._encode(_StoredIntent(vector, total_weight, observed_at)),
                    ex=self._settings.recommendation_search_intent_ttl_seconds,
                )
        except Exception as exc:
            logger.warning(
                "recommendation_intent_write_failed",
                extra={"event": "recommendation_intent_write_failed", "exception_type": type(exc).__name__},
            )
            return False
        return True

    async def load(self, *, user_id: uuid.UUID) -> tuple[float, ...] | None:
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                stored = self._decode(await self._redis.get(self._key(user_id)))
        except Exception as exc:
            logger.warning(
                "recommendation_intent_read_failed",
                extra={"event": "recommendation_intent_read_failed", "exception_type": type(exc).__name__},
            )
            return None
        if stored is None:
            return None
        if time.time() - stored.observed_at > self._settings.recommendation_search_intent_ttl_seconds:
            return None
        return stored.vector

    async def invalidate(self, *, user_id: uuid.UUID) -> None:
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                await self._redis.delete(self._key(user_id))
        except Exception:
            return

    def _encode(self, stored: _StoredIntent) -> str:
        return json.dumps(
            {
                "v": 1,
                "vector": base64.urlsafe_b64encode(encode_vector(stored.vector)).decode("ascii"),
                "weight": stored.total_weight,
                "observed_at": stored.observed_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _decode(self, raw: object) -> _StoredIntent | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            payload = cast("dict[str, Any]", json.loads(raw))
            if payload.get("v") != 1:
                return None
            encoded_vector = payload["vector"]
            total_weight = float(payload["weight"])
            observed_at = float(payload["observed_at"])
            if not isinstance(encoded_vector, str) or total_weight <= 0.0:
                return None
            vector = decode_vector(
                base64.urlsafe_b64decode(encoded_vector.encode("ascii")),
                dimensions=self._settings.pipeline_voyage_output_dimensions,
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
        return _StoredIntent(vector=vector, total_weight=total_weight, observed_at=observed_at)

    @staticmethod
    def _key(user_id: uuid.UUID) -> str:
        return f"{_INTENT_KEY_PREFIX}:{user_id}"


__all__ = ["IntentRedisProtocol", "RecommendationIntentStore"]
