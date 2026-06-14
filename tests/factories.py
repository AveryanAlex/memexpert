"""Small ORM factories for integration tests."""

from __future__ import annotations

import uuid

from memexpert.models.user import User


def build_full_user(**kwargs: object) -> User:
    """Build a User that derives to AccountType.FULL via a real login identity."""

    if not _has_login_identity(kwargs):
        kwargs["email"] = f"full-{uuid.uuid4().hex}@example.com"
    return User(**kwargs)


def _has_login_identity(kwargs: dict[str, object]) -> bool:
    password_hash = kwargs.get("password_hash")
    return (
        kwargs.get("telegram_id") is not None
        or kwargs.get("google_id") is not None
        or kwargs.get("email") is not None
        or (isinstance(password_hash, str) and bool(password_hash.strip()))
    )
