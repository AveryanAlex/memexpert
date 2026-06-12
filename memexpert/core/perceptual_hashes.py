"""Shared perceptual-hash normalization and distance helpers."""

from __future__ import annotations

import string

DEFAULT_PERCEPTUAL_HASH_ALGORITHM = "phash"
MAX_PERCEPTUAL_HASH_HEX_LENGTH = 64
_HEX_DIGITS = set(string.hexdigits.lower())


def normalize_perceptual_hash(value: str) -> str:
    """Return a lowercase hex pHash or raise ValueError for malformed input."""

    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("perceptual_hash must not be blank.")
    if len(normalized) > MAX_PERCEPTUAL_HASH_HEX_LENGTH:
        raise ValueError(f"perceptual_hash must be at most {MAX_PERCEPTUAL_HASH_HEX_LENGTH} hex characters.")
    if any(character not in _HEX_DIGITS for character in normalized):
        raise ValueError("perceptual_hash must contain only hexadecimal characters.")
    return normalized


def perceptual_hash_bit_size(value: str) -> int:
    """Return the bit size represented by a normalized hexadecimal pHash."""

    return len(normalize_perceptual_hash(value)) * 4


def normalize_hash_algorithm(value: str | None) -> str:
    """Normalize the hash-algorithm label stored with blocked hashes."""

    normalized = (value or DEFAULT_PERCEPTUAL_HASH_ALGORITHM).strip().lower()
    if not normalized:
        raise ValueError("hash_algorithm must not be blank.")
    return normalized


def hamming_distance_hex(left: str, right: str) -> int | None:
    """Return Hamming distance for equal-sized hex hashes, or None for mismatched sizes."""

    normalized_left = normalize_perceptual_hash(left)
    normalized_right = normalize_perceptual_hash(right)
    if len(normalized_left) != len(normalized_right):
        return None
    return (int(normalized_left, 16) ^ int(normalized_right, 16)).bit_count()


__all__ = [
    "DEFAULT_PERCEPTUAL_HASH_ALGORITHM",
    "MAX_PERCEPTUAL_HASH_HEX_LENGTH",
    "hamming_distance_hex",
    "normalize_hash_algorithm",
    "normalize_perceptual_hash",
    "perceptual_hash_bit_size",
]
