"""Fail-safe personalized-v2 serving eligibility."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid

    from memexpert.core.config import Settings


def personalized_v2_serving_enabled(settings: Settings, viewer_user_id: uuid.UUID) -> bool:
    """Return whether one stable viewer may receive personalized-v2 ranking."""

    if not settings.recommendation_enabled or settings.recommendation_shadow_mode:
        return False
    canary_percent = settings.recommendation_canary_percent
    if canary_percent <= 0:
        return False
    if canary_percent >= 100:
        return True
    bucket = int.from_bytes(
        hashlib.sha256(b"memexpert:personalized-v2-canary\0" + viewer_user_id.bytes).digest()[:8],
        byteorder="big",
    ) % 100
    return bucket < canary_percent


__all__ = ["personalized_v2_serving_enabled"]
