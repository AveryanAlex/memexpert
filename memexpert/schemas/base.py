"""Shared base schemas for the public API and service DTO layer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ORMSchema(BaseModel):
    """Base schema that can validate directly from ORM instances."""

    model_config = ConfigDict(from_attributes=True)


__all__ = ["ORMSchema"]
