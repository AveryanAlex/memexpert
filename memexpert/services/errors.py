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


class ProviderNotConfiguredError(AuthConfigurationError):
    """Raised when a provider flow is invoked without the required runtime config."""

    error_code: ClassVar[str] = "provider_not_configured"


class InvalidTokenError(ServiceValidationError, AuthServiceError):
    """Raised when an access or refresh token is missing, malformed, or unusable."""

    error_code: ClassVar[str] = "invalid_token"


class MissingTokenError(InvalidTokenError):
    """Raised when a required access or refresh token input is blank or absent."""


class ExpiredTokenError(AuthServiceError):
    """Raised when an otherwise valid token is past its expiry timestamp."""

    error_code: ClassVar[str] = "expired_token"


class ProviderPayloadInvalidError(ServiceValidationError, AuthServiceError):
    """Raised when a provider payload is malformed, tampered with, or incomplete."""

    error_code: ClassVar[str] = "provider_payload_invalid"


class ProviderPayloadExpiredError(AuthServiceError):
    """Raised when a provider payload is well-formed but outside the allowed age window."""

    error_code: ClassVar[str] = "provider_payload_expired"


class ProviderAccessDeniedError(AuthServiceError):
    """Raised when an upstream identity provider refuses the presented credentials."""

    error_code: ClassVar[str] = "provider_access_denied"


class InvalidCredentialsError(AuthServiceError):
    """Raised when a first-party credential set is blank, malformed, or incorrect."""

    error_code: ClassVar[str] = "invalid_credentials"


class EmailAlreadyInUseError(AuthServiceError):
    """Raised when signup attempts to create a full account for an existing email."""

    error_code: ClassVar[str] = "email_already_in_use"


class AccountUnavailableError(AuthServiceError):
    """Raised when a non-active user attempts to authenticate or rotate a session."""

    error_code: ClassVar[str] = "account_unavailable"


class AuthenticatedUserNotFoundError(InvalidTokenError):
    """Raised when token claims refer to a user row that no longer exists."""


class RefreshTokenReuseError(InvalidTokenError):
    """Raised when a refresh token has already been revoked and is replayed."""


class UserStateMismatchError(InvalidTokenError):
    """Raised when bearer claims no longer match the current persisted user state."""


class UpgradeRequiredError(AuthServiceError):
    """Raised when a guest account attempts a full-account-only operation."""

    error_code: ClassVar[str] = "upgrade_required"


class AccountLinkError(AuthServiceError):
    """Base error for guest-to-full account-link and merge failures."""

    error_code: ClassVar[str] = "account_link_error"


class GuestAccountRequiredError(AccountLinkError):
    """Raised when a caller tries to link an account that is no longer a guest."""

    error_code: ClassVar[str] = "guest_account_required"


class AccountLinkInvariantError(AccountLinkError):
    """Raised when merge prerequisites or audit invariants do not hold."""

    error_code: ClassVar[str] = "account_link_invariant_error"


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


class CollectionVerificationRequiredError(CollectionServiceError):
    """Raised when collection collaboration requires a verified or provider-backed identity."""


class InvalidCollectionTitleError(ServiceValidationError, CollectionServiceError):
    """Raised when a collection title is blank or exceeds allowed limits."""


class InvalidCollectionMembershipError(ServiceValidationError, CollectionServiceError):
    """Raised when a membership role payload is malformed or violates ownership rules."""


class InvalidCollectionInviteError(ServiceValidationError, CollectionServiceError):
    """Raised when an invite payload is malformed or semantically invalid."""


class DuplicateCollectionInviteError(CollectionServiceError):
    """Raised when an invite token collides with an existing invite record."""


__all__ = [
    "AccountLinkError",
    "AccountLinkInvariantError",
    "AccountUnavailableError",
    "AuthenticatedUserNotFoundError",
    "AuthConfigurationError",
    "AuthServiceError",
    "CollectionNotFoundError",
    "CollectionServiceError",
    "CollectionVerificationRequiredError",
    "CollectionWriteAccessError",
    "DuplicateCollectionInviteError",
    "DuplicateFavoritesCollectionError",
    "DuplicateIdentityError",
    "EmailAlreadyInUseError",
    "ExpiredTokenError",
    "GuestCollectionAccessError",
    "GuestAccountRequiredError",
    "InvalidCollectionInviteError",
    "InvalidCollectionMembershipError",
    "InvalidCollectionTitleError",
    "InvalidCredentialsError",
    "InvalidIdentityError",
    "InvalidTokenError",
    "MissingTokenError",
    "ProviderAccessDeniedError",
    "ProviderNotConfiguredError",
    "ProviderPayloadExpiredError",
    "ProviderPayloadInvalidError",
    "RefreshTokenReuseError",
    "ServiceError",
    "ServiceValidationError",
    "UpgradeRequiredError",
    "UserNotFoundError",
    "UserServiceError",
    "UserStateMismatchError",
]
