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


class UpgradeRequiredError(AuthServiceError):
    """Raised when a guest account attempts a full-account-only operation."""

    error_code: ClassVar[str] = "upgrade_required"


class AccountLinkError(AuthServiceError):
    """Base error for guest-to-full account-link and merge failures."""

    error_code: ClassVar[str] = "account_link_error"


class GuestAccountRequiredError(AccountLinkError):
    """Raised when a caller tries to link an account that is no longer a guest."""

    error_code: ClassVar[str] = "guest_account_required"


class AccountLinkAlreadyCompletedError(AccountLinkError):
    """Raised when the requested guest-link already finished on another request or surface."""

    error_code: ClassVar[str] = "account_link_already_completed"


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


class PinLimitExceededError(CollectionServiceError):
    """Raised when a user attempts to pin more memes than the quick-access surface allows."""


class InvalidPinnedMemeOrderError(ServiceValidationError, CollectionServiceError):
    """Raised when a pin reorder payload is duplicated, incomplete, or out of range."""


class PipelineServiceError(ServiceError):
    """Base error for operator-managed content-pipeline failures."""

    error_code: ClassVar[str] = "pipeline_error"


class PipelineOperatorTokenError(PipelineServiceError):
    """Raised when the operator token header is missing, blank, or incorrect."""

    error_code: ClassVar[str] = "invalid_operator_token"


class PipelineItemNotFoundError(PipelineServiceError):
    """Raised when a requested pipeline item does not exist."""

    error_code: ClassVar[str] = "pipeline_item_not_found"


class PipelinePayloadValidationError(ServiceValidationError, PipelineServiceError):
    """Raised when uploaded bytes or provenance metadata are malformed."""

    error_code: ClassVar[str] = "pipeline_payload_invalid"


class PipelinePayloadTooLargeError(PipelinePayloadValidationError):
    """Raised when an uploaded file exceeds the configured size limit."""

    error_code: ClassVar[str] = "pipeline_payload_too_large"


class PipelineUnsupportedMediaTypeError(PipelinePayloadValidationError):
    """Raised when upload bytes do not match the supported image contract."""

    error_code: ClassVar[str] = "pipeline_unsupported_media_type"


class PipelineSourceConflictError(PipelineServiceError):
    """Raised when source provenance collides with existing durable state."""

    error_code: ClassVar[str] = "pipeline_source_conflict"


class PipelineStorageError(PipelineServiceError):
    """Raised when the S3-compatible original write cannot complete."""

    error_code: ClassVar[str] = "pipeline_storage_failure"


class PipelineIngestError(PipelineServiceError):
    """Raised when PostgreSQL persistence cannot finalize an ingest transaction."""

    error_code: ClassVar[str] = "pipeline_ingest_failure"


class PipelineMergeTransactionError(PipelineIngestError):
    """Raised when the post-embed auto-merge transaction fails transiently.

    Unlike generic ``PipelineIngestError`` (which signals a contract violation that
    cannot be recovered by retrying — e.g. malformed vectors, missing OCR rows,
    impossible state transitions), a merge-transaction failure represents a
    recoverable runtime condition: a Qdrant outage during similarity lookup, a
    database row-lock conflict between two concurrent merges, or any transient
    mid-merge exception. The embed stage row therefore stays replayable so the
    runtime can retry without operator intervention.
    """

    error_code: ClassVar[str] = "pipeline_merge_transaction_failure"


class PipelinePublishError(PipelineServiceError):
    """Raised when downstream dispatch fails after durable writes succeed."""

    error_code: ClassVar[str] = "pipeline_publish_failure"


class PipelineReplayNotAllowedError(PipelineServiceError):
    """Raised when durable state does not allow replaying a pipeline stage."""

    error_code: ClassVar[str] = "pipeline_replay_not_allowed"


class CrawlerChannelNotTrackedError(PipelineIngestError):
    """Raised when the crawler tries to ingest a post from an unknown channel.

    Curated S04 crawlers must only consume from channels that exist as
    ``source_channels`` rows. An ingest call for a channel with no row is a
    genuine configuration bug (a curated channel got removed, or the
    crawler is pointed at something it should not be), not a silent
    fallthrough the service should ignore.
    """

    error_code: ClassVar[str] = "crawler_channel_not_tracked"


class CrawlerPublishError(PipelineIngestError):
    """Raised when a crawler ingest commits durably but downstream publish fails.

    Parallel to :class:`PipelinePublishError` but scoped to the crawler
    entrypoint so the runtime dispatcher can distinguish operator-upload
    publish failures from crawler-ingest publish failures without
    stringifying either exception.
    """

    error_code: ClassVar[str] = "crawler_publish_failure"


class CrawlerSessionNotRunnableError(PipelineIngestError):
    """Raised when the crawler runtime is asked to work a non-runnable session.

    A session is "runnable" only when its durable ``TelegramSessionState``
    row is in the ``active`` status. Stopped, flood-waiting, and
    quarantined sessions must not accept catch-up or live-listener work —
    the operator surface is responsible for re-arming the session after a
    cooldown or ban clears. Raising this error (instead of silently
    skipping) makes mis-routed catch-up calls a loud configuration bug.
    """

    error_code: ClassVar[str] = "crawler_session_not_runnable"


class CrawlerChannelNotFoundError(PipelineIngestError):
    """Raised when an operator references a source channel id that does not exist.

    Distinct from :class:`CrawlerChannelNotTrackedError`: that error fires
    when the crawler ingest path receives a post for a channel it cannot
    find by ``(platform, platform_id)``; this one fires in the operator
    surface when an operator pokes a ``source_channel_id`` that does not
    resolve to any row at all. Keeping them separate lets the route
    dispatcher map each to a different error code without stringifying
    the exception.
    """

    error_code: ClassVar[str] = "crawler_channel_not_found"


class CrawlerInvalidSessionError(PipelineIngestError):
    """Raised when the reassignment target session does not exist.

    Distinct from :class:`CrawlerSessionNotRunnableError` — that error
    fires when a session is known but is in a flood-wait, stopped, or
    quarantined state; this one fires when the operator points the
    reassignment call at a session name that has no
    :class:`memexpert.models.content.TelegramSessionState` row at all.
    Operator tooling relies on the distinction because the recovery path
    differs: "unknown target" is a typo, "not runnable" is a cooldown.
    """

    error_code: ClassVar[str] = "crawler_invalid_session"


__all__ = [
    "AccountLinkAlreadyCompletedError",
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
    "CrawlerChannelNotFoundError",
    "CrawlerChannelNotTrackedError",
    "CrawlerInvalidSessionError",
    "CrawlerPublishError",
    "CrawlerSessionNotRunnableError",
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
    "PipelineIngestError",
    "PipelineItemNotFoundError",
    "PipelineMergeTransactionError",
    "PipelineOperatorTokenError",
    "PipelinePayloadTooLargeError",
    "PipelinePayloadValidationError",
    "PipelinePublishError",
    "PipelineReplayNotAllowedError",
    "PipelineServiceError",
    "PipelineSourceConflictError",
    "PipelineStorageError",
    "PipelineUnsupportedMediaTypeError",
    "ProviderAccessDeniedError",
    "ProviderNotConfiguredError",
    "ProviderPayloadExpiredError",
    "ProviderPayloadInvalidError",
    "ServiceError",
    "ServiceValidationError",
    "UpgradeRequiredError",
    "UserNotFoundError",
    "UserServiceError",
]
