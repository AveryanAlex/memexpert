"""Business services package with lazy public exports.

Keeping this package initializer import-light lets service-specific container
images avoid dependencies for unrelated services.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, str] = {
    "ACCESS_TOKEN_TYPE": "memexpert.services.auth_service",
    "AccountLinkAlreadyCompletedError": "memexpert.services.errors",
    "AccountLinkError": "memexpert.services.errors",
    "AccountLinkInvariantError": "memexpert.services.errors",
    "AccountLinkResult": "memexpert.services.account_link_service",
    "AccountLinkService": "memexpert.services.account_link_service",
    "AccountUnavailableError": "memexpert.services.errors",
    "AdminConflictError": "memexpert.services.admin",
    "AdminTelegramLoginService": "memexpert.services.admin_telegram_login",
    "AdminNotFoundError": "memexpert.services.admin",
    "AdminService": "memexpert.services.admin",
    "AdminServiceError": "memexpert.services.admin",
    "AuthenticatedUserNotFoundError": "memexpert.services.errors",
    "AuthConfigurationError": "memexpert.services.errors",
    "AuthService": "memexpert.services.auth_service",
    "AuthServiceError": "memexpert.services.errors",
    "AuthSession": "memexpert.services.auth_service",
    "ChannelSuggestionService": "memexpert.services.channel_suggestion_service",
    "ChannelSuggestionServiceError": "memexpert.services.channel_suggestion_service",
    "ChannelSuggestionSubmitResult": "memexpert.services.channel_suggestion_service",
    "CollectionNotFoundError": "memexpert.services.errors",
    "CollectionService": "memexpert.services.collection_service",
    "CollectionServiceError": "memexpert.services.errors",
    "CollectionVerificationRequiredError": "memexpert.services.errors",
    "CollectionWriteAccessError": "memexpert.services.errors",
    "CrawlerChannelNotFoundError": "memexpert.services.errors",
    "CrawlerChannelNotTrackedError": "memexpert.services.errors",
    "CrawlerInvalidSessionError": "memexpert.services.errors",
    "CrawlerPublishError": "memexpert.services.errors",
    "CrawlerSessionNotRunnableError": "memexpert.services.errors",
    "DEFAULT_GUEST_LIFETIME": "memexpert.services.user_service",
    "DuplicateCollectionInviteError": "memexpert.services.errors",
    "DuplicateFavoritesCollectionError": "memexpert.services.errors",
    "DuplicateIdentityError": "memexpert.services.errors",
    "EmailAlreadyInUseError": "memexpert.services.errors",
    "ExpiredTokenError": "memexpert.services.errors",
    "FAVORITES_TITLE": "memexpert.services.collection_service",
    "GuestAccountRequiredError": "memexpert.services.errors",
    "GuestCollectionAccessError": "memexpert.services.errors",
    "HS256_ALGORITHM": "memexpert.services.auth_service",
    "InvalidChannelSuggestionError": "memexpert.services.channel_suggestion_service",
    "InvalidCollectionInviteError": "memexpert.services.errors",
    "InvalidCollectionMembershipError": "memexpert.services.errors",
    "InvalidCollectionTitleError": "memexpert.services.errors",
    "InvalidCredentialsError": "memexpert.services.errors",
    "InvalidIdentityError": "memexpert.services.errors",
    "InvalidPinnedMemeOrderError": "memexpert.services.errors",
    "InvalidTokenError": "memexpert.services.errors",
    "LinkedProvidersProjection": "memexpert.services.account_link_service",
    "MemeNotFoundError": "memexpert.services.meme_search",
    "MemeOfTheDayService": "memexpert.services.meme_of_the_day",
    "MemeReportService": "memexpert.services.report",
    "MemeReportServiceError": "memexpert.services.report",
    "MemeReportTargetNotVisibleError": "memexpert.services.report",
    "MemeSearchFilters": "memexpert.services.meme_search",
    "MemeSearchScope": "memexpert.services.meme_search",
    "MemeSearchService": "memexpert.services.meme_search",
    "MemeSeoGenerationResult": "memexpert.services.meme_seo",
    "MemeSeoGenerationService": "memexpert.services.meme_seo",
    "MemeSeoProviderProtocol": "memexpert.services.meme_seo",
    "MemeSeoProviderResult": "memexpert.services.meme_seo",
    "MemeSeoStructuredOutput": "memexpert.services.meme_seo",
    "MissingTokenError": "memexpert.services.errors",
    "PinLimitExceededError": "memexpert.services.errors",
    "PipelineIngestError": "memexpert.services.errors",
    "PipelineItemNotFoundError": "memexpert.services.errors",
    "PipelineMergeTransactionError": "memexpert.services.errors",
    "PipelineOperatorTokenError": "memexpert.services.errors",
    "PipelinePayloadTooLargeError": "memexpert.services.errors",
    "PipelinePayloadValidationError": "memexpert.services.errors",
    "PipelinePublishError": "memexpert.services.errors",
    "PipelineReplayNotAllowedError": "memexpert.services.errors",
    "PipelineServiceError": "memexpert.services.errors",
    "PipelineSourceConflictError": "memexpert.services.errors",
    "PipelineStorageError": "memexpert.services.errors",
    "PipelineUnsupportedMediaTypeError": "memexpert.services.errors",
    "ProviderAccessDeniedError": "memexpert.services.errors",
    "ProviderAuthService": "memexpert.services.provider_auth_service",
    "ProviderNotConfiguredError": "memexpert.services.errors",
    "ProviderPayloadExpiredError": "memexpert.services.errors",
    "ProviderPayloadInvalidError": "memexpert.services.errors",
    "PydanticAIMemeSeoProvider": "memexpert.services.meme_seo",
    "SEO_PROMPT_BASELINE": "memexpert.services.meme_seo",
    "ServiceError": "memexpert.services.errors",
    "ServiceValidationError": "memexpert.services.errors",
    "SourceEngagementMetrics": "memexpert.services.source_engagement",
    "SourceEngagementScheduleSlot": "memexpert.services.source_engagement",
    "StaticMemeSeoProvider": "memexpert.services.meme_seo",
    "TelegramInlineMediaResult": "memexpert.services.telegram_inline",
    "TelegramInlineMediaUrlProvider": "memexpert.services.telegram_inline",
    "TelegramInlineSearchPage": "memexpert.services.telegram_inline",
    "TelegramInlineService": "memexpert.services.telegram_inline",
    "TelegramLinkStartResult": "memexpert.services.account_link_service",
    "UpgradeRequiredError": "memexpert.services.errors",
    "UserNotFoundError": "memexpert.services.errors",
    "UserService": "memexpert.services.user_service",
    "UserServiceError": "memexpert.services.errors",
    "add_source_engagement_snapshot": "memexpert.services.source_engagement",
    "build_meme_seo_provider": "memexpert.services.meme_seo",
    "next_source_engagement_schedule_slot": "memexpert.services.source_engagement",
    "normalize_channel_suggestion": "memexpert.services.channel_suggestion_service",
    "reaction_count_from_reactions": "memexpert.services.source_engagement",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORTS))


__all__ = sorted(_EXPORTS)
