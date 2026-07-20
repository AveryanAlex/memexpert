# ruff: noqa: TC003
"""Frozen Redis feed pools and signed opaque continuation cursors."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import get_async_redis
from memexpert.services.recommendations.candidates import CandidateContribution, CandidateSource

logger = logging.getLogger(__name__)
_CURSOR_KIND = "recommendation_feed"
_CURSOR_ALGORITHM = "HS256"
_CURSOR_SIGNING_DOMAIN = b"memexpert:recommendation-cursor:v1\0"
_VIEWER_BINDING_DOMAIN = b"memexpert:recommendation-cursor-viewer:v1\0"
_POOL_KEY_PREFIX = "recommendation:feed_pool"
_REDIS_PREFLIGHT_KEY = "recommendation:feed_preflight"
_LEGACY_VIEWER_POOLS_KEY_PREFIX = "recommendation:viewer_pools"
_VIEWER_POOLS_KEY_PREFIX = "recommendation:viewer_pools:v2"
MAX_FEED_CURSOR_LENGTH = 8192
_FREEZE_POOL_SCRIPT = """
redis.call("SET", KEYS[1], ARGV[1], "EX", ARGV[2])
redis.call("ZADD", KEYS[2], ARGV[3], ARGV[4])
redis.call("EXPIRE", KEYS[2], ARGV[2])

local excess = redis.call("ZCARD", KEYS[2]) - tonumber(ARGV[5])
if excess <= 0 then
    return {}
end

local evicted = redis.call("ZRANGE", KEYS[2], 0, excess - 1)
for _, pool_id in ipairs(evicted) do
    redis.call("ZREM", KEYS[2], pool_id)
    redis.call("DEL", ARGV[6] .. pool_id)
end
return evicted
"""


class FeedCursorError(ValueError):
    """Base error for malformed or mismatched feed continuation."""


class FeedCursorExpiredError(FeedCursorError):
    """Raised when a signed cursor or its frozen Redis pool has expired."""


class FeedCacheUnavailableError(RuntimeError):
    """Raised when Redis cannot safely freeze or continue a personalized pool."""


class FeedRedisProtocol(Protocol):
    async def get(self, key: str) -> object: ...

    async def set(self, key: str, value: str, *, ex: int) -> object: ...

    async def sadd(self, key: str, *values: str) -> object: ...

    async def smembers(self, key: str) -> object: ...

    async def expire(self, key: str, seconds: int) -> object: ...

    async def delete(self, *keys: str) -> object: ...


class FeedPoolRedisProtocol(FeedRedisProtocol, Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object: ...

    async def zrange(self, key: str, start: int, end: int) -> object: ...


@dataclass(frozen=True, slots=True)
class CachedFeedCandidate:
    meme_id: uuid.UUID
    score: float
    score_components: dict[str, float]
    contributions: tuple[CandidateContribution, ...]
    reason: str
    is_exploration: bool = False


@dataclass(frozen=True, slots=True)
class FrozenFeedPool:
    pool_id: uuid.UUID
    request_id: str
    viewer_key: str
    filter_key: str
    algorithm_version: str
    profile_version: str | None
    candidates: tuple[CachedFeedCandidate, ...]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class FeedCursorClaims:
    mode: Literal["pool", "trending"]
    viewer_key: str
    filter_key: str
    algorithm_version: str
    next_index: int = 0
    pool_id: uuid.UUID | None = None
    last_score: float | None = None
    last_meme_id: uuid.UUID | None = None
    served_meme_ids: tuple[uuid.UUID, ...] = ()
    expires_at: datetime | None = None


class FeedSessionStore:
    """Persist ordered pools and validate privacy/safety bindings on every page."""

    def __init__(
        self,
        *,
        redis: FeedPoolRedisProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._redis = redis or cast("FeedPoolRedisProtocol", get_async_redis())
        secret = self._settings.auth_jwt_secret.get_secret_value().encode("utf-8")
        self._signing_key = hashlib.sha256(_CURSOR_SIGNING_DOMAIN + secret).digest()
        self._viewer_secret = hashlib.sha256(_VIEWER_BINDING_DOMAIN + secret).digest()

    async def ensure_available(self) -> None:
        """Fail quickly when Redis cannot persist a first-page pool."""

        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                await self._redis.get(_REDIS_PREFLIGHT_KEY)
        except Exception as exc:
            raise FeedCacheUnavailableError("Redis is unavailable for personalized feed sessions.") from exc

    async def freeze(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filter_key: str,
        request_id: str,
        algorithm_version: str,
        profile_version: str | None,
        candidates: list[CachedFeedCandidate],
    ) -> FrozenFeedPool:
        now = datetime.now(UTC)
        pool = FrozenFeedPool(
            pool_id=uuid.uuid7(),
            request_id=request_id,
            viewer_key=self.viewer_key(viewer_user_id),
            filter_key=filter_key,
            algorithm_version=algorithm_version,
            profile_version=profile_version,
            candidates=tuple(candidates[: self._settings.recommendation_feed_pool_limit]),
            created_at=now,
            expires_at=now + timedelta(seconds=self._settings.recommendation_feed_pool_ttl_seconds),
        )
        pool_key = self._pool_key(pool.pool_id)
        viewer_pools_key = self._viewer_pools_key(viewer_user_id)
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                await self._redis.eval(
                    _FREEZE_POOL_SCRIPT,
                    2,
                    pool_key,
                    viewer_pools_key,
                    _encode_pool(pool),
                    self._settings.recommendation_feed_pool_ttl_seconds,
                    now.timestamp(),
                    str(pool.pool_id),
                    self._settings.recommendation_feed_pool_max_per_viewer,
                    f"{_POOL_KEY_PREFIX}:",
                )
        except Exception as exc:
            raise FeedCacheUnavailableError("Unable to freeze a personalized feed pool.") from exc
        return pool

    async def load_pool(
        self,
        claims: FeedCursorClaims,
        *,
        viewer_user_id: uuid.UUID,
        filter_key: str,
    ) -> FrozenFeedPool:
        if claims.mode != "pool" or claims.pool_id is None:
            raise FeedCursorError("Cursor does not reference a personalized pool.")
        self._verify_binding(claims, viewer_user_id=viewer_user_id, filter_key=filter_key)
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                raw = await self._redis.get(self._pool_key(claims.pool_id))
        except Exception as exc:
            raise FeedCacheUnavailableError("Unable to load the personalized feed pool.") from exc
        if raw is None:
            raise FeedCursorExpiredError("The personalized feed pool has expired.")
        try:
            pool = _decode_pool(raw)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise FeedCursorExpiredError("The personalized feed pool is unavailable.") from exc
        if pool.expires_at <= datetime.now(UTC):
            raise FeedCursorExpiredError("The personalized feed pool has expired.")
        if (
            pool.pool_id != claims.pool_id
            or pool.viewer_key != claims.viewer_key
            or pool.filter_key != claims.filter_key
            or pool.algorithm_version != claims.algorithm_version
        ):
            raise FeedCursorError("Feed pool binding does not match its cursor.")
        return pool

    def issue_pool_cursor(
        self,
        pool: FrozenFeedPool,
        *,
        next_index: int,
        served_meme_ids: tuple[uuid.UUID, ...] = (),
    ) -> str:
        return self._issue(
            FeedCursorClaims(
                mode="pool",
                pool_id=pool.pool_id,
                viewer_key=pool.viewer_key,
                filter_key=pool.filter_key,
                algorithm_version=pool.algorithm_version,
                next_index=max(0, next_index),
                served_meme_ids=self._normalize_served_meme_ids(served_meme_ids),
                expires_at=pool.expires_at,
            )
        )

    def issue_trending_cursor(
        self,
        *,
        viewer_user_id: uuid.UUID,
        filter_key: str,
        algorithm_version: str,
        last_score: float,
        last_meme_id: uuid.UUID,
        next_index: int,
        served_meme_ids: tuple[uuid.UUID, ...] = (),
    ) -> str:
        return self._issue(
            FeedCursorClaims(
                mode="trending",
                viewer_key=self.viewer_key(viewer_user_id),
                filter_key=filter_key,
                algorithm_version=algorithm_version,
                last_score=last_score,
                last_meme_id=last_meme_id,
                next_index=max(0, next_index),
                served_meme_ids=self._normalize_served_meme_ids(served_meme_ids),
                expires_at=datetime.now(UTC) + timedelta(seconds=self._settings.recommendation_feed_pool_ttl_seconds),
            )
        )

    def verify_cursor(
        self,
        cursor: str,
        *,
        viewer_user_id: uuid.UUID,
        filter_key: str,
    ) -> FeedCursorClaims:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_FEED_CURSOR_LENGTH:
            raise FeedCursorError("The feed cursor is invalid.")
        try:
            payload = jwt.decode(
                cursor.strip(),
                self._signing_key,
                algorithms=[_CURSOR_ALGORITHM],
                options={"require": ["kind", "version", "mode", "viewer_key", "filter_key", "exp"]},
            )
        except ExpiredSignatureError as exc:
            raise FeedCursorExpiredError("The feed cursor has expired.") from exc
        except (InvalidTokenError, AttributeError) as exc:
            raise FeedCursorError("The feed cursor is invalid.") from exc
        if payload.get("kind") != _CURSOR_KIND or payload.get("version") != 1:
            raise FeedCursorError("The feed cursor kind or version is invalid.")
        try:
            mode = payload["mode"]
            if mode not in {"pool", "trending"}:
                raise ValueError("unsupported cursor mode")
            claims = FeedCursorClaims(
                mode=mode,
                viewer_key=str(payload["viewer_key"]),
                filter_key=str(payload["filter_key"]),
                algorithm_version=str(payload["algorithm_version"]),
                next_index=int(payload.get("next_index", 0)),
                pool_id=uuid.UUID(payload["pool_id"]) if payload.get("pool_id") else None,
                last_score=float(payload["last_score"]) if payload.get("last_score") is not None else None,
                last_meme_id=uuid.UUID(payload["last_meme_id"]) if payload.get("last_meme_id") else None,
                served_meme_ids=_decode_uuid_set(
                    payload.get("served"),
                    max_count=self._settings.recommendation_feed_pool_limit,
                ),
                expires_at=datetime.fromtimestamp(float(payload["exp"]), tz=UTC),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedCursorError("The feed cursor claims are invalid.") from exc
        if claims.next_index < 0:
            raise FeedCursorError("The feed cursor index is invalid.")
        if claims.mode == "pool" and claims.pool_id is None:
            raise FeedCursorError("The feed cursor does not identify a pool.")
        if claims.mode == "trending" and (claims.last_score is None or claims.last_meme_id is None):
            raise FeedCursorError("The trending cursor is incomplete.")
        self._verify_binding(claims, viewer_user_id=viewer_user_id, filter_key=filter_key)
        return claims

    async def invalidate_viewer(self, viewer_user_id: uuid.UUID) -> None:
        index_key = self._viewer_pools_key(viewer_user_id)
        legacy_index_key = self._legacy_viewer_pools_key(viewer_user_id)
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                sorted_ids = await self._redis.zrange(index_key, 0, -1)
                legacy_ids = await self._redis.smembers(legacy_index_key)
                pool_keys = self._pool_keys_from_members(sorted_ids, legacy_ids)
                await self._redis.delete(index_key, legacy_index_key, *pool_keys)
        except Exception:
            return

    def viewer_key(self, viewer_user_id: uuid.UUID) -> str:
        return hmac.new(self._viewer_secret, viewer_user_id.bytes, hashlib.sha256).hexdigest()

    def _issue(self, claims: FeedCursorClaims) -> str:
        if claims.expires_at is None:
            raise ValueError("cursor expiry is required")
        payload = {
            "kind": _CURSOR_KIND,
            "version": 1,
            "mode": claims.mode,
            "viewer_key": claims.viewer_key,
            "filter_key": claims.filter_key,
            "algorithm_version": claims.algorithm_version,
            "next_index": claims.next_index,
            "pool_id": str(claims.pool_id) if claims.pool_id else None,
            "last_score": claims.last_score,
            "last_meme_id": str(claims.last_meme_id) if claims.last_meme_id else None,
            "iat": int(time.time()),
            "exp": int(claims.expires_at.timestamp()),
        }
        if claims.served_meme_ids:
            payload["served"] = _encode_uuid_set(claims.served_meme_ids)
        cursor = jwt.encode(payload, self._signing_key, algorithm=_CURSOR_ALGORITHM)
        if len(cursor) > MAX_FEED_CURSOR_LENGTH:
            raise ValueError("feed cursor exceeds its bounded wire size")
        return cursor

    def _normalize_served_meme_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
    ) -> tuple[uuid.UUID, ...]:
        normalized = tuple(dict.fromkeys(meme_ids))
        if len(normalized) > self._settings.recommendation_feed_pool_limit:
            raise ValueError("served meme IDs exceed the bounded feed pool")
        return normalized

    def _verify_binding(
        self,
        claims: FeedCursorClaims,
        *,
        viewer_user_id: uuid.UUID,
        filter_key: str,
    ) -> None:
        if not hmac.compare_digest(claims.viewer_key, self.viewer_key(viewer_user_id)):
            raise FeedCursorError("The feed cursor belongs to another viewer.")
        if not hmac.compare_digest(claims.filter_key, filter_key):
            raise FeedCursorError("The feed cursor does not match the active filters.")
        if claims.algorithm_version != self._settings.recommendation_algorithm_version:
            raise FeedCursorError("The feed cursor uses another algorithm version.")

    @staticmethod
    def _pool_key(pool_id: uuid.UUID) -> str:
        return f"{_POOL_KEY_PREFIX}:{pool_id}"

    @staticmethod
    def _viewer_pools_key(viewer_user_id: uuid.UUID) -> str:
        return f"{_VIEWER_POOLS_KEY_PREFIX}:{viewer_user_id}"

    @staticmethod
    def _legacy_viewer_pools_key(viewer_user_id: uuid.UUID) -> str:
        return f"{_LEGACY_VIEWER_POOLS_KEY_PREFIX}:{viewer_user_id}"

    @classmethod
    def _pool_keys_from_members(cls, *member_groups: object) -> list[str]:
        pool_keys: list[str] = []
        seen: set[uuid.UUID] = set()
        for raw_group in member_groups:
            members = raw_group if isinstance(raw_group, set | list | tuple) else ()
            for raw in members:
                try:
                    value = raw.decode("ascii") if isinstance(raw, bytes) else str(raw)
                    pool_id = uuid.UUID(value)
                except UnicodeDecodeError, ValueError:
                    continue
                if pool_id not in seen:
                    seen.add(pool_id)
                    pool_keys.append(cls._pool_key(pool_id))
        return pool_keys


def recommendation_filter_key(filters: object) -> str:
    """Build a stable hash from normalized output-safety and taxonomy filters."""

    values = vars(filters) if hasattr(filters, "__dict__") else filters
    encoded = json.dumps(values, default=str, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _encode_uuid_set(meme_ids: tuple[uuid.UUID, ...]) -> str:
    packed = b"".join(meme_id.bytes for meme_id in meme_ids)
    return base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")


def _decode_uuid_set(raw: object, *, max_count: int) -> tuple[uuid.UUID, ...]:
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, str):
        raise ValueError("served meme IDs must be compact text")
    try:
        encoded = raw.encode("ascii")
        packed = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("served meme IDs are malformed") from exc
    if len(packed) % 16 != 0 or len(packed) // 16 > max_count:
        raise ValueError("served meme ID count is invalid")
    meme_ids = tuple(uuid.UUID(bytes=packed[index : index + 16]) for index in range(0, len(packed), 16))
    if len(set(meme_ids)) != len(meme_ids):
        raise ValueError("served meme IDs must be unique")
    return meme_ids


def _encode_pool(pool: FrozenFeedPool) -> str:
    payload = {
        "v": 1,
        "pool_id": str(pool.pool_id),
        "request_id": pool.request_id,
        "viewer_key": pool.viewer_key,
        "filter_key": pool.filter_key,
        "algorithm_version": pool.algorithm_version,
        "profile_version": pool.profile_version,
        "created_at": pool.created_at.isoformat(),
        "expires_at": pool.expires_at.isoformat(),
        "candidates": [
            {
                "meme_id": str(candidate.meme_id),
                "score": candidate.score,
                "score_components": candidate.score_components,
                "reason": candidate.reason,
                "is_exploration": candidate.is_exploration,
                "contributions": [
                    {
                        **asdict(contribution),
                        "source": contribution.source.value,
                    }
                    for contribution in candidate.contributions
                ],
            }
            for candidate in pool.candidates
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_pool(raw: object) -> FrozenFeedPool:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise TypeError("pool payload must be text")
    payload = json.loads(raw)
    if payload.get("v") != 1:
        raise ValueError("unsupported pool version")
    candidates = []
    for raw_candidate in payload["candidates"]:
        contributions = tuple(
            CandidateContribution(
                source=CandidateSource(raw_contribution["source"]),
                source_key=str(raw_contribution["source_key"]),
                rank=int(raw_contribution["rank"]),
                source_score=float(raw_contribution["source_score"]),
                rrf_contribution=float(raw_contribution["rrf_contribution"]),
            )
            for raw_contribution in raw_candidate["contributions"]
        )
        candidates.append(
            CachedFeedCandidate(
                meme_id=uuid.UUID(raw_candidate["meme_id"]),
                score=float(raw_candidate["score"]),
                score_components={str(key): float(value) for key, value in raw_candidate["score_components"].items()},
                contributions=contributions,
                reason=str(raw_candidate["reason"]),
                is_exploration=bool(raw_candidate.get("is_exploration", False)),
            )
        )
    return FrozenFeedPool(
        pool_id=uuid.UUID(payload["pool_id"]),
        request_id=str(payload["request_id"]),
        viewer_key=str(payload["viewer_key"]),
        filter_key=str(payload["filter_key"]),
        algorithm_version=str(payload["algorithm_version"]),
        profile_version=str(payload["profile_version"]) if payload.get("profile_version") else None,
        candidates=tuple(candidates),
        created_at=datetime.fromisoformat(payload["created_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


__all__ = [
    "CachedFeedCandidate",
    "FeedCacheUnavailableError",
    "FeedCursorClaims",
    "FeedCursorError",
    "FeedCursorExpiredError",
    "FeedPoolRedisProtocol",
    "FeedRedisProtocol",
    "FeedSessionStore",
    "FrozenFeedPool",
    "MAX_FEED_CURSOR_LENGTH",
    "recommendation_filter_key",
]
