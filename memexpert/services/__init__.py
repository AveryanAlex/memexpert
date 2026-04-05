"""Business services package."""

from memexpert.services.collection_service import CollectionService
from memexpert.services.errors import (
    CollectionNotFoundError,
    CollectionServiceError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    DuplicateFavoritesCollectionError,
    DuplicateIdentityError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    InvalidCollectionMembershipError,
    InvalidCollectionTitleError,
    InvalidIdentityError,
    ServiceError,
    ServiceValidationError,
    UserNotFoundError,
    UserServiceError,
)
from memexpert.services.user_service import DEFAULT_GUEST_LIFETIME, FAVORITES_TITLE, UserService

__all__ = [
    "CollectionNotFoundError",
    "CollectionService",
    "CollectionServiceError",
    "CollectionWriteAccessError",
    "DEFAULT_GUEST_LIFETIME",
    "DuplicateCollectionInviteError",
    "DuplicateFavoritesCollectionError",
    "DuplicateIdentityError",
    "FAVORITES_TITLE",
    "GuestCollectionAccessError",
    "InvalidCollectionInviteError",
    "InvalidCollectionMembershipError",
    "InvalidCollectionTitleError",
    "InvalidIdentityError",
    "ServiceError",
    "ServiceValidationError",
    "UserNotFoundError",
    "UserService",
    "UserServiceError",
]
