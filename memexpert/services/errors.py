"""Shared service-layer exceptions for domain invariants and validation failures."""

from __future__ import annotations


class ServiceError(Exception):
    """Base error for application service-layer failures."""


class ServiceValidationError(ServiceError):
    """Base error for malformed or incomplete service inputs."""


class UserServiceError(ServiceError):
    """Base error for user/account service failures."""


class UserNotFoundError(UserServiceError):
    """Raised when a requested user does not exist."""


class InvalidIdentityError(ServiceValidationError, UserServiceError):
    """Raised when an account identity payload is malformed or incomplete."""


class DuplicateIdentityError(UserServiceError):
    """Raised when a user identity is already bound to an account."""


class DuplicateFavoritesCollectionError(UserServiceError):
    """Raised when a user would end up with multiple Favorites collections."""


class CollectionServiceError(ServiceError):
    """Base error for collection service failures."""


class CollectionNotFoundError(CollectionServiceError):
    """Raised when a requested collection does not exist."""


class CollectionWriteAccessError(CollectionServiceError):
    """Raised when a user cannot write to the target collection."""


class GuestCollectionAccessError(CollectionServiceError):
    """Raised when a guest attempts a full-account-only collection operation."""


class InvalidCollectionTitleError(ServiceValidationError, CollectionServiceError):
    """Raised when a collection title is blank or exceeds allowed limits."""


class InvalidCollectionMembershipError(ServiceValidationError, CollectionServiceError):
    """Raised when a membership role payload is malformed or violates ownership rules."""


class InvalidCollectionInviteError(ServiceValidationError, CollectionServiceError):
    """Raised when an invite payload is malformed or semantically invalid."""


class DuplicateCollectionInviteError(CollectionServiceError):
    """Raised when an invite token collides with an existing invite record."""


__all__ = [
    "CollectionNotFoundError",
    "CollectionServiceError",
    "CollectionWriteAccessError",
    "DuplicateCollectionInviteError",
    "DuplicateFavoritesCollectionError",
    "DuplicateIdentityError",
    "GuestCollectionAccessError",
    "InvalidCollectionInviteError",
    "InvalidCollectionMembershipError",
    "InvalidCollectionTitleError",
    "InvalidIdentityError",
    "ServiceError",
    "ServiceValidationError",
    "UserNotFoundError",
    "UserServiceError",
]
