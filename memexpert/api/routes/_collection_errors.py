"""Shared HTTP error mapping for collection-service route surfaces."""

from __future__ import annotations

from fastapi import HTTPException, status

from memexpert.services import (
    CollectionNotFoundError,
    CollectionServiceError,
    CollectionWriteAccessError,
    GuestCollectionAccessError,
    UserNotFoundError,
)

type CollectionServiceErrorType = type[CollectionServiceError]


def collection_service_http_error(
    exc: CollectionServiceError,
    *,
    forbidden_errors: tuple[CollectionServiceErrorType, ...] = (),
    conflict_errors: tuple[CollectionServiceErrorType, ...] = (),
) -> HTTPException:
    """Map collection-service failures to route-specific HTTP responses."""

    if isinstance(exc, (CollectionNotFoundError, UserNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (CollectionWriteAccessError, GuestCollectionAccessError, *forbidden_errors)):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if conflict_errors and isinstance(exc, conflict_errors):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


__all__ = ["collection_service_http_error"]
