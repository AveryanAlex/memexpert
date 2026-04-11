"""Shared helpers for extracting PostgreSQL details out of SQLAlchemy errors."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.exc import IntegrityError


def integrity_constraint_name(exc: IntegrityError) -> str | None:
    """Extract a PostgreSQL constraint name from a SQLAlchemy integrity error.

    Service-layer code maps constraint names (``uq_users_email_not_null``,
    ``uq_collections_one_favorites_per_owner`` and friends) to typed domain
    exceptions. The lookup walks both ``exc.orig`` and its ``__cause__`` so
    it survives the asyncpg → SQLAlchemy wrapping performed by the driver,
    and it checks both the modern ``constraint_name`` attribute and the
    legacy ``diag.constraint_name`` fallback.
    """

    candidates = [exc.orig, getattr(exc.orig, "__cause__", None)]
    for candidate in candidates:
        if candidate is None:
            continue

        constraint_name = getattr(candidate, "constraint_name", None)
        if isinstance(constraint_name, str):
            return constraint_name

        diag = getattr(candidate, "diag", None)
        diag_constraint_name = getattr(diag, "constraint_name", None)
        if isinstance(diag_constraint_name, str):
            return diag_constraint_name

    return None


__all__ = ["integrity_constraint_name"]
