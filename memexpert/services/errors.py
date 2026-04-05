"""Shared service-layer exceptions for domain invariants and validation failures."""

from __future__ import annotations

from typing import ClassVar


class ServiceError(Exception):
    """Base error for application service-layer failures."""


class ServiceValidationError(ServiceError):
    """Base error for malformed or incomplete service inputs."""


class AuthServiceError(ServiceError):
    """Base error for authentication and session service failures."""

    error_code: ClassVar[str] = "auth_error"


class AuthConfigurationError(AuthServiceError):
    """Raised when auth settings would produce unverifiable or unsafe tokens."""

    error_code: ClassVar[str] = "auth_configuration_error"


class InvalidTokenError(ServiceValidationError, AuthServiceError):
    """Raised when an access or refresh token is missing, malformed, or unusable."""

    error_code: ClassVar[str] = "invalid_token"


class MissingTokenError(InvalidTokenError):
    """Raised when a required access or refresh token input is blank or absent."""


class ExpiredTokenError(AuthServiceError):
    """Raised when an otherwise valid token is past its expiry timestamp."""

    error_code: ClassVar[str] = "expired_token"


class AuthenticatedUserNotFoundError(InvalidTokenError):
    """Raised when token claims refer to a user row that no longer exists."""


class RefreshTokenReuseError(InvalidTokenError):
    """Raised when a refresh token has already been revoked and is replayed."""


class UserStateMismatchError(InvalidTokenError):
    """Raised when bearer claims no longer match the current persisted user state."""


class UpgradeRequiredError(AuthServiceError):
    """Raised when a guest account attempts a full-account-only operation."""

    error_code: ClassVar[str] = "upgrade_required"


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
    "AuthenticatedUserNotFoundError",
    "AuthConfigurationError",
    "AuthServiceError",
    "CollectionNotFoundError",
    "CollectionServiceError",
    "CollectionWriteAccessError",
    "DuplicateCollectionInviteError",
    "DuplicateFavoritesCollectionError",
    "DuplicateIdentityError",
    "ExpiredTokenError",
    "GuestCollectionAccessError",
    "InvalidCollectionInviteError",
    "InvalidCollectionMembershipError",
    "InvalidCollectionTitleError",
    "InvalidIdentityError",
    "InvalidTokenError",
    "MissingTokenError",
    "RefreshTokenReuseError",
    "ServiceError",
    "ServiceValidationError",
    "UpgradeRequiredError",
    "UserNotFoundError",
    "UserServiceError",
    "UserStateMismatchError",
]
