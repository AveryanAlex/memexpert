# ruff: noqa: TC003
"""Recommendation signal policy, decay, and cross-event de-duplication."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from memexpert.models.enums import AnalyticsEventType
from memexpert.services.recommendations.math import exponential_decay


class RecommendationSignalKind(StrEnum):
    DURABLE = "durable"
    ENGAGED_VIEW = "engaged_view"
    DETAIL = "detail"
    HIGH_INTENT = "high_intent"
    IMPRESSION = "impression"


@dataclass(frozen=True, slots=True)
class SignalPolicy:
    weight: float
    kind: RecommendationSignalKind
    is_strong_positive: bool = False


DEFAULT_SIGNAL_POLICY: dict[AnalyticsEventType, SignalPolicy] = {
    AnalyticsEventType.FAVORITE: SignalPolicy(5.0, RecommendationSignalKind.DURABLE, True),
    AnalyticsEventType.MEME_LIKE: SignalPolicy(5.0, RecommendationSignalKind.DURABLE, True),
    AnalyticsEventType.MEME_SAVE: SignalPolicy(5.0, RecommendationSignalKind.DURABLE, True),
    AnalyticsEventType.SAVE: SignalPolicy(5.0, RecommendationSignalKind.DURABLE, True),
    AnalyticsEventType.MEME_PIN: SignalPolicy(5.0, RecommendationSignalKind.DURABLE, True),
    AnalyticsEventType.MEME_DOWNLOAD: SignalPolicy(4.0, RecommendationSignalKind.HIGH_INTENT, True),
    AnalyticsEventType.MEME_SEND: SignalPolicy(4.0, RecommendationSignalKind.HIGH_INTENT, True),
    AnalyticsEventType.MEME_SHARE: SignalPolicy(4.0, RecommendationSignalKind.HIGH_INTENT, True),
    AnalyticsEventType.SHARE: SignalPolicy(4.0, RecommendationSignalKind.HIGH_INTENT, True),
    AnalyticsEventType.INLINE_CHOSEN: SignalPolicy(4.0, RecommendationSignalKind.HIGH_INTENT, True),
    AnalyticsEventType.INLINE_SENT: SignalPolicy(4.0, RecommendationSignalKind.HIGH_INTENT, True),
    AnalyticsEventType.MEME_ENGAGED_VIEW: SignalPolicy(2.0, RecommendationSignalKind.ENGAGED_VIEW),
    AnalyticsEventType.MEME_DETAIL_CLICK: SignalPolicy(1.0, RecommendationSignalKind.DETAIL),
    AnalyticsEventType.MEME_VIEW: SignalPolicy(1.0, RecommendationSignalKind.DETAIL),
    AnalyticsEventType.VIEW: SignalPolicy(1.0, RecommendationSignalKind.DETAIL),
    AnalyticsEventType.MEME_IMPRESSION: SignalPolicy(0.0, RecommendationSignalKind.IMPRESSION),
    AnalyticsEventType.IMPRESSION: SignalPolicy(0.0, RecommendationSignalKind.IMPRESSION),
    AnalyticsEventType.INLINE_SERVED: SignalPolicy(0.0, RecommendationSignalKind.IMPRESSION),
}

_REMOVE_ACTIONS = frozenset(
    {
        "delete",
        "remove",
        "remove_save",
        "reorder",
        "reorder_pin",
        "unfavorite",
        "unlike",
        "unpin",
        "unsave",
    }
)
_SEND_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.INLINE_CHOSEN,
        AnalyticsEventType.INLINE_SENT,
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.SHARE,
    }
)


@dataclass(frozen=True, slots=True)
class RawRecommendationSignal:
    event_id: uuid.UUID
    meme_id: uuid.UUID
    event_type: AnalyticsEventType
    occurred_at: datetime
    impression_id: str | None = None
    action: str | None = None


@dataclass(frozen=True, slots=True)
class WeightedRecommendationSignal:
    meme_id: uuid.UUID
    occurred_at: datetime
    weight: float
    is_strong_positive: bool
    source_event_id: uuid.UUID


def is_remove_action(action: str | None) -> bool:
    return bool(action and action.strip().lower() in _REMOVE_ACTIONS)


def signal_policy_for(
    event_type: AnalyticsEventType,
    *,
    action: str | None = None,
) -> SignalPolicy | None:
    """Resolve an event policy without ever turning removal into a negative."""

    policy = DEFAULT_SIGNAL_POLICY.get(event_type)
    if policy is None or policy.weight <= 0.0 or is_remove_action(action):
        return None
    return policy


def deduplicate_signals(
    signals: list[RawRecommendationSignal],
) -> list[RawRecommendationSignal]:
    """Collapse multi-row Telegram sends to one strongest logical interaction.

    Event UUIDs still provide write idempotency. This second layer handles the
    deliberate INLINE_CHOSEN + INLINE_SENT + MEME_SEND rows emitted for one
    Telegram result by keying send-family signals to their impression.
    """

    selected: dict[tuple[object, ...], RawRecommendationSignal] = {}
    for signal in sorted(signals, key=lambda item: (item.occurred_at, item.event_id.int)):
        if signal.event_type in _SEND_EVENT_TYPES and signal.impression_id:
            key: tuple[object, ...] = ("send", signal.meme_id, signal.impression_id)
        else:
            key = ("event", signal.event_id)
        current = selected.get(key)
        if current is None:
            selected[key] = signal
            continue
        current_weight = (signal_policy_for(current.event_type, action=current.action) or SignalPolicy(
            0.0,
            RecommendationSignalKind.IMPRESSION,
        )).weight
        candidate_weight = (signal_policy_for(signal.event_type, action=signal.action) or SignalPolicy(
            0.0,
            RecommendationSignalKind.IMPRESSION,
        )).weight
        if (candidate_weight, signal.occurred_at, signal.event_id.int) > (
            current_weight,
            current.occurred_at,
            current.event_id.int,
        ):
            selected[key] = signal
    return sorted(selected.values(), key=lambda item: (item.occurred_at, item.event_id.int))


def weight_signals(
    signals: list[RawRecommendationSignal],
    *,
    now: datetime,
    half_life_seconds: float,
) -> list[WeightedRecommendationSignal]:
    """Apply policy and time decay after logical-event de-duplication."""

    weighted: list[WeightedRecommendationSignal] = []
    for signal in deduplicate_signals(signals):
        policy = signal_policy_for(signal.event_type, action=signal.action)
        if policy is None:
            continue
        age_seconds = max(0.0, (now - signal.occurred_at).total_seconds())
        weighted.append(
            WeightedRecommendationSignal(
                meme_id=signal.meme_id,
                occurred_at=signal.occurred_at,
                weight=policy.weight
                * exponential_decay(age_seconds=age_seconds, half_life_seconds=half_life_seconds),
                is_strong_positive=policy.is_strong_positive,
                source_event_id=signal.event_id,
            )
        )
    return weighted


__all__ = [
    "DEFAULT_SIGNAL_POLICY",
    "RawRecommendationSignal",
    "RecommendationSignalKind",
    "SignalPolicy",
    "WeightedRecommendationSignal",
    "deduplicate_signals",
    "is_remove_action",
    "signal_policy_for",
    "weight_signals",
]
