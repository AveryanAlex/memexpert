"""User-facing source-channel suggestion submission service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import func, select

from memexpert.models.enums import SourcePlatform
from memexpert.models.user import ChannelSuggestion, User
from memexpert.schemas.user import ChannelSuggestionRead
from memexpert.services.errors import ServiceError, ServiceValidationError, UserNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_REDDIT_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{3,21}$")
_VK_SLUG_RE = re.compile(r"^[A-Za-z0-9_.]{3,64}$")
_MAX_CHANNEL_INPUT_LENGTH = 256


class ChannelSuggestionServiceError(ServiceError):
    """Base error for channel suggestion submission failures."""


class InvalidChannelSuggestionError(ServiceValidationError, ChannelSuggestionServiceError):
    """Raised when a submitted source channel cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class ChannelSuggestionSubmitResult:
    """Submission result, including whether a new moderation row was created."""

    suggestion: ChannelSuggestionRead
    created: bool


@dataclass(slots=True)
class ChannelSuggestionService:
    """Validate and persist user-submitted crawl-channel suggestions."""

    session: AsyncSession

    async def submit_channel_suggestion(
        self,
        *,
        user_id: object,
        channel: str,
        commit: bool = True,
    ) -> ChannelSuggestionSubmitResult:
        user = await self.session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        normalized = normalize_channel_suggestion(channel)
        existing = await self.session.scalar(
            select(ChannelSuggestion).where(
                ChannelSuggestion.platform == normalized.platform,
                func.lower(ChannelSuggestion.channel_url) == normalized.channel_url.lower(),
            )
        )
        if existing is not None:
            return ChannelSuggestionSubmitResult(
                suggestion=ChannelSuggestionRead.model_validate(existing),
                created=False,
            )

        suggestion = ChannelSuggestion(
            user_id=user.id,
            platform=normalized.platform,
            channel_url=normalized.channel_url,
        )
        self.session.add(suggestion)
        if commit:
            await self.session.commit()
            await self.session.refresh(suggestion)
        else:
            await self.session.flush()
        return ChannelSuggestionSubmitResult(
            suggestion=ChannelSuggestionRead.model_validate(suggestion),
            created=True,
        )


@dataclass(frozen=True, slots=True)
class NormalizedChannelSuggestion:
    platform: SourcePlatform
    channel_url: str


def normalize_channel_suggestion(raw_channel: str) -> NormalizedChannelSuggestion:
    """Normalize supported Telegram/Reddit/VK channel inputs to canonical URLs."""

    channel = raw_channel.strip()
    if not channel:
        raise InvalidChannelSuggestionError("Channel suggestion cannot be blank.")
    if len(channel) > _MAX_CHANNEL_INPUT_LENGTH:
        raise InvalidChannelSuggestionError("Channel suggestion is too long.")

    if channel.startswith("@"):
        return _normalize_telegram_username(channel[1:])
    if _TELEGRAM_USERNAME_RE.fullmatch(channel) is not None and "." not in channel and "/" not in channel:
        return _normalize_telegram_username(channel)

    parsed = urlparse(channel if "://" in channel else f"https://{channel}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in {"t.me", "telegram.me"}:
        if not path_parts:
            raise InvalidChannelSuggestionError("Telegram channel URL must include a public handle.")
        return _normalize_telegram_username(path_parts[0].removeprefix("@"))

    if host == "reddit.com":
        if len(path_parts) < 2 or path_parts[0].lower() != "r":
            raise InvalidChannelSuggestionError("Reddit suggestions must use /r/<subreddit> URLs.")
        subreddit = path_parts[1]
        if _REDDIT_SUBREDDIT_RE.fullmatch(subreddit) is None:
            raise InvalidChannelSuggestionError("Reddit subreddit names must be 3-21 letters, digits, or underscores.")
        return NormalizedChannelSuggestion(
            platform=SourcePlatform.REDDIT,
            channel_url=f"https://www.reddit.com/r/{subreddit}",
        )

    if host == "vk.com":
        if not path_parts:
            raise InvalidChannelSuggestionError("VK channel URL must include a public handle.")
        slug = path_parts[0]
        if _VK_SLUG_RE.fullmatch(slug) is None:
            raise InvalidChannelSuggestionError("VK handles must be 3-64 letters, digits, underscores, or dots.")
        return NormalizedChannelSuggestion(platform=SourcePlatform.VK, channel_url=f"https://vk.com/{slug}")

    raise InvalidChannelSuggestionError("Supported suggestions are Telegram handles/URLs, Reddit /r URLs, or VK URLs.")


def _normalize_telegram_username(username: str) -> NormalizedChannelSuggestion:
    if _TELEGRAM_USERNAME_RE.fullmatch(username) is None:
        raise InvalidChannelSuggestionError("Telegram handles must be 5-32 letters, digits, or underscores.")
    return NormalizedChannelSuggestion(
        platform=SourcePlatform.TELEGRAM,
        channel_url=f"https://t.me/{username}",
    )


__all__ = [
    "ChannelSuggestionService",
    "ChannelSuggestionServiceError",
    "ChannelSuggestionSubmitResult",
    "InvalidChannelSuggestionError",
    "NormalizedChannelSuggestion",
    "normalize_channel_suggestion",
]
