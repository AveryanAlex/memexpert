# ruff: noqa: TC003
"""Deterministic spherical profile construction for sparse beta traffic."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from memexpert.services.recommendations.math import cosine_similarity, normalize_vector, weighted_centroid


@dataclass(frozen=True, slots=True)
class ProfileSignalVector:
    meme_id: uuid.UUID
    vector: tuple[float, ...]
    weight: float
    last_signal_at: datetime
    is_strong_positive: bool


@dataclass(frozen=True, slots=True)
class BuiltProfileVector:
    slot: int
    vector: tuple[float, ...]
    signal_count: int
    total_weight: float
    meme_ids: tuple[uuid.UUID, ...]


def is_profile_materialization_current(
    *,
    model_version: str,
    profile_version: str,
    expected_model_version: str,
    expected_profile_base_version: str,
) -> bool:
    """Return whether a persisted profile is compatible with current serving."""

    if model_version != expected_model_version:
        return False
    return profile_version == expected_profile_base_version or profile_version.startswith(
        f"{expected_profile_base_version}:"
    )


def build_profile_vectors(
    signals: list[ProfileSignalVector],
    *,
    activation_threshold: int = 20,
    min_cluster_items: int = 3,
    max_iterations: int = 5,
) -> tuple[BuiltProfileVector, ...]:
    """Return a global centroid and evidence-gated deterministic clusters."""

    normalized_signals = _normalized_signals(signals)
    if not normalized_signals:
        return ()
    global_profile = _profile_from_members(0, normalized_signals)
    if global_profile is None:
        return ()

    strong_signals = [signal for signal in normalized_signals if signal.is_strong_positive]
    if len({signal.meme_id for signal in strong_signals}) < activation_threshold:
        return (global_profile,)

    cluster_count = _cluster_count(len(strong_signals))
    centroids = _farthest_first_centroids(strong_signals, count=cluster_count)
    assignments: list[list[ProfileSignalVector]] = []
    for _ in range(max(1, max_iterations)):
        assignments = _assign(strong_signals, centroids)
        next_centroids = []
        for index, members in enumerate(assignments):
            centroid = weighted_centroid((member.vector, member.weight) for member in members)
            next_centroids.append(centroid or centroids[index])
        if all(cosine_similarity(old, new) >= 1.0 - 1e-9 for old, new in zip(centroids, next_centroids, strict=True)):
            centroids = next_centroids
            break
        centroids = next_centroids

    retained = [members for members in _assign(strong_signals, centroids) if len(members) >= min_cluster_items]
    retained.sort(key=lambda members: min(member.meme_id.int for member in members))
    clusters: list[BuiltProfileVector] = [global_profile]
    for slot, members in enumerate(retained[:4], start=1):
        built = _profile_from_members(slot, members)
        if built is not None:
            clusters.append(built)
    return tuple(clusters)


def _normalized_signals(signals: list[ProfileSignalVector]) -> list[ProfileSignalVector]:
    resolved: dict[uuid.UUID, ProfileSignalVector] = {}
    for signal in signals:
        normalized = normalize_vector(signal.vector)
        if normalized is None or signal.weight <= 0.0 or not math.isfinite(signal.weight):
            continue
        candidate = ProfileSignalVector(
            meme_id=signal.meme_id,
            vector=normalized,
            weight=signal.weight,
            last_signal_at=signal.last_signal_at,
            is_strong_positive=signal.is_strong_positive,
        )
        current = resolved.get(signal.meme_id)
        if current is None or (candidate.weight, candidate.last_signal_at) > (current.weight, current.last_signal_at):
            resolved[signal.meme_id] = candidate
    return sorted(resolved.values(), key=lambda signal: signal.meme_id.int)


def _cluster_count(signal_count: int) -> int:
    return min(4, max(2, round(math.sqrt(signal_count / 10.0))))


def _farthest_first_centroids(
    signals: list[ProfileSignalVector],
    *,
    count: int,
) -> list[tuple[float, ...]]:
    first = max(signals, key=lambda signal: (signal.weight, -signal.meme_id.int))
    selected = [first]
    while len(selected) < count:
        remaining = [signal for signal in signals if signal.meme_id not in {item.meme_id for item in selected}]
        if not remaining:
            break
        next_signal = max(
            remaining,
            key=lambda signal: (
                min(1.0 - cosine_similarity(signal.vector, chosen.vector) for chosen in selected),
                signal.weight,
                -signal.meme_id.int,
            ),
        )
        selected.append(next_signal)
    return [signal.vector for signal in selected]


def _assign(
    signals: list[ProfileSignalVector],
    centroids: list[tuple[float, ...]],
) -> list[list[ProfileSignalVector]]:
    clusters: list[list[ProfileSignalVector]] = [[] for _ in centroids]
    for signal in signals:
        best_index = max(
            range(len(centroids)),
            key=lambda index: (cosine_similarity(signal.vector, centroids[index]), -index),
        )
        clusters[best_index].append(signal)
    return clusters


def _profile_from_members(slot: int, members: list[ProfileSignalVector]) -> BuiltProfileVector | None:
    centroid = weighted_centroid((member.vector, member.weight) for member in members)
    if centroid is None:
        return None
    ordered = sorted(
        members,
        key=lambda member: (-member.weight, -member.last_signal_at.timestamp(), member.meme_id.int),
    )
    return BuiltProfileVector(
        slot=slot,
        vector=centroid,
        signal_count=len(members),
        total_weight=sum(member.weight for member in members),
        meme_ids=tuple(member.meme_id for member in ordered),
    )


__all__ = [
    "BuiltProfileVector",
    "ProfileSignalVector",
    "build_profile_vectors",
    "is_profile_materialization_current",
]
