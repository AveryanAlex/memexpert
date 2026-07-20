# ruff: noqa: TC003
"""Small dependency-free vector helpers for online recommendations."""

from __future__ import annotations

import array
import math
from collections.abc import Iterable, Sequence

_FLOAT32_TYPE_CODE = "f"
_FLOAT32_WIDTH = 4


def clamp01(value: float) -> float:
    """Clamp a finite score into the public ranking range."""

    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, treating malformed/zero vectors as neutral zero."""

    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def normalize_vector(vector: Sequence[float]) -> tuple[float, ...] | None:
    """Return a unit vector, or ``None`` for empty/non-finite/zero input."""

    if not vector or any(not math.isfinite(value) for value in vector):
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return None
    return tuple(value / norm for value in vector)


def weighted_centroid(
    vectors: Iterable[tuple[Sequence[float], float]],
) -> tuple[float, ...] | None:
    """Build a spherical weighted centroid from same-dimensional vectors."""

    total_weight = 0.0
    accumulator: list[float] | None = None
    for vector, weight in vectors:
        if weight <= 0.0 or not math.isfinite(weight):
            continue
        normalized = normalize_vector(vector)
        if normalized is None:
            continue
        if accumulator is None:
            accumulator = [0.0] * len(normalized)
        if len(normalized) != len(accumulator):
            continue
        for index, value in enumerate(normalized):
            accumulator[index] += value * weight
        total_weight += weight
    if accumulator is None or total_weight <= 0.0:
        return None
    return normalize_vector(tuple(value / total_weight for value in accumulator))


def exponential_decay(*, age_seconds: float, half_life_seconds: float) -> float:
    """Return an exponential half-life multiplier for a non-negative age."""

    if half_life_seconds <= 0.0 or not math.isfinite(half_life_seconds):
        raise ValueError("half_life_seconds must be finite and positive")
    return math.exp(-math.log(2.0) * max(0.0, age_seconds) / half_life_seconds)


def encode_vector(vector: Sequence[float]) -> bytes:
    """Encode a recommendation vector using the embedding cache float32 format."""

    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("vector must contain finite values")
    values = array.array(_FLOAT32_TYPE_CODE, vector)
    if values.itemsize != _FLOAT32_WIDTH:  # pragma: no cover - platform invariant
        raise RuntimeError("platform float array is not float32")
    return values.tobytes()


def decode_vector(value: bytes, *, dimensions: int) -> tuple[float, ...]:
    """Decode a stored recommendation vector with strict dimension validation."""

    if dimensions <= 0 or len(value) != dimensions * _FLOAT32_WIDTH:
        raise ValueError("stored vector has an unexpected dimension")
    values = array.array(_FLOAT32_TYPE_CODE)
    values.frombytes(value)
    if values.itemsize != _FLOAT32_WIDTH:  # pragma: no cover - platform invariant
        raise RuntimeError("platform float array is not float32")
    return tuple(float(component) for component in values)


__all__ = [
    "clamp01",
    "cosine_similarity",
    "decode_vector",
    "encode_vector",
    "exponential_decay",
    "normalize_vector",
    "weighted_centroid",
]
