"""Small text normalization helpers shared by Pydantic schemas."""

from __future__ import annotations


def normalize_optional_text(value: str | None) -> str | None:
    """Trim optional user text and collapse blank values to ``None``."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def normalize_required_text(value: str, *, error_message: str = "value must not be blank.") -> str:
    """Trim required user text and reject blank values."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(error_message)
    return normalized


__all__ = ["normalize_optional_text", "normalize_required_text"]
