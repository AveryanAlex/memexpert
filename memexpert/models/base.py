"""Shared SQLAlchemy declarative primitives for PostgreSQL-backed models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar, Final

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base declarative model with stable naming conventions for Alembic."""

    metadata: ClassVar[MetaData] = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[object, object]] = {
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


class UUIDPrimaryKeyMixin:
    """Provide a UUIDv7 primary key for domain models."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid7,
    )


class TimestampMixin:
    """Provide creation and update timestamps for persistent models."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


__all__ = [
    "Base",
    "NAMING_CONVENTION",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "utcnow",
]
