"""Shared configuration-time validators used by the auth service family."""

from __future__ import annotations

from memexpert.services.errors import AuthConfigurationError


def require_non_blank(field_name: str, value: str) -> str:
    """Return ``value.strip()`` or raise :class:`AuthConfigurationError` when empty."""

    normalized_value = value.strip()
    if not normalized_value:
        raise AuthConfigurationError(f"{field_name} must not be blank.")
    return normalized_value


def require_positive_int(field_name: str, value: int) -> int:
    """Return ``value`` unchanged or raise when it is not strictly positive."""

    if value <= 0:
        raise AuthConfigurationError(f"{field_name} must be greater than zero.")
    return value


def require_positive_float(field_name: str, value: float) -> float:
    """Return ``value`` unchanged or raise when it is not strictly positive."""

    if value <= 0:
        raise AuthConfigurationError(f"{field_name} must be greater than zero.")
    return value


def normalize_optional_text(value: str | None) -> str | None:
    """Return ``value.strip()`` or ``None`` for ``None``/blank inputs."""

    if value is None:
        return None

    normalized_value = value.strip()
    return normalized_value or None


__all__ = [
    "normalize_optional_text",
    "require_non_blank",
    "require_positive_float",
    "require_positive_int",
]
