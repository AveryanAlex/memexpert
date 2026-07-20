# ruff: noqa: TC003
"""Compact Redis-backed continuations for Telegram's 64-byte inline offset."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from memexpert.core.config import Settings, get_settings
from memexpert.core.redis import get_async_redis
from memexpert.schemas.meme import MemeResultAttributionRead

if TYPE_CHECKING:
    from memexpert.services.recommendations.feed_sessions import FeedRedisProtocol

_CURSOR_PREFIX = "ti1_"
_CURSOR_PATTERN = re.compile(r"^ti1_[0-9a-f]{32}$")
_KEY_PREFIX = "recommendation:telegram_inline"
_STATE_VERSION = 1


class TelegramInlineCursorError(ValueError):
    """Base error for an invalid or viewer-mismatched inline continuation."""


class TelegramInlineCursorExpiredError(TelegramInlineCursorError):
    """Raised when a compact continuation no longer has Redis state."""


class TelegramInlineCacheUnavailableError(RuntimeError):
    """Raised when Redis cannot persist or load an inline continuation."""


@dataclass(frozen=True, slots=True)
class PendingHomeRecommendation:
    """One unserved item fetched from a frozen recommendation pool."""

    meme_id: uuid.UUID
    attribution: MemeResultAttributionRead


@dataclass(frozen=True, slots=True)
class TelegramInlineFeedState:
    """Immutable start position referenced by one Telegram next-offset token."""

    telegram_user_id: int
    viewer_user_id: uuid.UUID
    include_nsfw: bool
    is_personal: bool
    request_id: str
    pinned_meme_ids: tuple[uuid.UUID, ...]
    next_pinned_index: int
    pending_home_items: tuple[PendingHomeRecommendation, ...]
    home_cursor: str | None
    home_started: bool
    home_exhausted: bool
    home_total: int
    next_rank: int
    expires_at: datetime


class TelegramInlineSessionStore:
    """Store opaque Telegram continuation state without exposing a long JWT."""

    def __init__(
        self,
        *,
        redis: FeedRedisProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._redis = redis or cast("FeedRedisProtocol", get_async_redis())

    async def save(self, state: TelegramInlineFeedState) -> str:
        """Persist a new immutable continuation and return its compact handle."""

        now = datetime.now(UTC)
        remaining_seconds = min(
            self._settings.recommendation_feed_pool_ttl_seconds,
            int((state.expires_at - now).total_seconds()),
        )
        if remaining_seconds <= 0:
            raise TelegramInlineCursorExpiredError("The Telegram inline feed has expired.")
        cursor = f"{_CURSOR_PREFIX}{uuid.uuid7().hex}"
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                await self._redis.set(
                    self._key(cursor),
                    _encode_state(state),
                    ex=remaining_seconds,
                )
        except Exception as exc:
            raise TelegramInlineCacheUnavailableError(
                "Unable to persist the Telegram inline continuation.",
            ) from exc
        return cursor

    async def load(
        self,
        cursor: str,
        *,
        telegram_user_id: int,
        viewer_user_id: uuid.UUID,
        include_nsfw: bool,
        is_personal: bool,
    ) -> TelegramInlineFeedState:
        """Load and validate a compact continuation for the current viewer."""

        normalized = cursor.strip()
        if _CURSOR_PATTERN.fullmatch(normalized) is None:
            raise TelegramInlineCursorError("The Telegram inline cursor is invalid.")
        try:
            async with asyncio.timeout(self._settings.recommendation_redis_timeout_seconds):
                raw = await self._redis.get(self._key(normalized))
        except Exception as exc:
            raise TelegramInlineCacheUnavailableError(
                "Unable to load the Telegram inline continuation.",
            ) from exc
        if raw is None:
            raise TelegramInlineCursorExpiredError("The Telegram inline feed has expired.")
        try:
            state = _decode_state(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TelegramInlineCursorExpiredError(
                "The Telegram inline continuation is unavailable.",
            ) from exc
        if state.expires_at <= datetime.now(UTC):
            raise TelegramInlineCursorExpiredError("The Telegram inline feed has expired.")
        if (
            state.telegram_user_id != telegram_user_id
            or state.viewer_user_id != viewer_user_id
            or state.include_nsfw is not include_nsfw
            or state.is_personal is not is_personal
        ):
            raise TelegramInlineCursorError(
                "The Telegram inline cursor does not match the active viewer.",
            )
        return state

    @staticmethod
    def _key(cursor: str) -> str:
        return f"{_KEY_PREFIX}:{cursor}"


def new_telegram_inline_feed_state(
    *,
    telegram_user_id: int,
    viewer_user_id: uuid.UUID,
    include_nsfw: bool,
    is_personal: bool,
    request_id: str,
    pinned_meme_ids: tuple[uuid.UUID, ...],
    settings: Settings | None = None,
) -> TelegramInlineFeedState:
    """Create the initial position for one empty-query inline feed."""

    resolved = settings or get_settings()
    return TelegramInlineFeedState(
        telegram_user_id=telegram_user_id,
        viewer_user_id=viewer_user_id,
        include_nsfw=include_nsfw,
        is_personal=is_personal,
        request_id=request_id,
        pinned_meme_ids=pinned_meme_ids,
        next_pinned_index=0,
        pending_home_items=(),
        home_cursor=None,
        home_started=False,
        home_exhausted=False,
        home_total=0,
        next_rank=1,
        expires_at=datetime.now(UTC)
        + timedelta(seconds=resolved.recommendation_feed_pool_ttl_seconds),
    )


def _encode_state(state: TelegramInlineFeedState) -> str:
    payload = {
        "v": _STATE_VERSION,
        "telegram_user_id": state.telegram_user_id,
        "viewer_user_id": str(state.viewer_user_id),
        "include_nsfw": state.include_nsfw,
        "is_personal": state.is_personal,
        "request_id": state.request_id,
        "pinned_meme_ids": [str(meme_id) for meme_id in state.pinned_meme_ids],
        "next_pinned_index": state.next_pinned_index,
        "pending_home_items": [
            {
                "meme_id": str(item.meme_id),
                "attribution": item.attribution.model_dump(mode="json"),
            }
            for item in state.pending_home_items
        ],
        "home_cursor": state.home_cursor,
        "home_started": state.home_started,
        "home_exhausted": state.home_exhausted,
        "home_total": state.home_total,
        "next_rank": state.next_rank,
        "expires_at": state.expires_at.isoformat(),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_state(raw: object) -> TelegramInlineFeedState:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise TypeError("Telegram inline state must be text.")
    payload = json.loads(raw)
    if payload.get("v") != _STATE_VERSION:
        raise ValueError("Unsupported Telegram inline state version.")
    pinned_meme_ids = tuple(uuid.UUID(value) for value in payload["pinned_meme_ids"])
    pending_home_items = tuple(
        PendingHomeRecommendation(
            meme_id=uuid.UUID(item["meme_id"]),
            attribution=MemeResultAttributionRead.model_validate(item["attribution"]),
        )
        for item in payload["pending_home_items"]
    )
    next_pinned_index = int(payload["next_pinned_index"])
    next_rank = int(payload["next_rank"])
    home_total = int(payload["home_total"])
    if not 0 <= next_pinned_index <= len(pinned_meme_ids):
        raise ValueError("Telegram inline pin position is invalid.")
    if next_rank < 1 or home_total < 0 or len(pending_home_items) > 200:
        raise ValueError("Telegram inline continuation counters are invalid.")
    return TelegramInlineFeedState(
        telegram_user_id=int(payload["telegram_user_id"]),
        viewer_user_id=uuid.UUID(payload["viewer_user_id"]),
        include_nsfw=bool(payload["include_nsfw"]),
        is_personal=bool(payload["is_personal"]),
        request_id=str(payload["request_id"]),
        pinned_meme_ids=pinned_meme_ids,
        next_pinned_index=next_pinned_index,
        pending_home_items=pending_home_items,
        home_cursor=str(payload["home_cursor"]) if payload.get("home_cursor") else None,
        home_started=bool(payload["home_started"]),
        home_exhausted=bool(payload["home_exhausted"]),
        home_total=home_total,
        next_rank=next_rank,
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


__all__ = [
    "PendingHomeRecommendation",
    "TelegramInlineCacheUnavailableError",
    "TelegramInlineCursorError",
    "TelegramInlineCursorExpiredError",
    "TelegramInlineFeedState",
    "TelegramInlineSessionStore",
    "new_telegram_inline_feed_state",
]
