# ruff: noqa: TC001
"""Collection service dependency helpers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from memexpert.api.dependencies.auth import DbSessionDep
from memexpert.services.collection_service import CollectionService


def get_collection_service(session: DbSessionDep) -> CollectionService:
    """Build the collection service for the current request session."""

    return CollectionService(session)


CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]


__all__ = ["CollectionServiceDep", "get_collection_service"]
