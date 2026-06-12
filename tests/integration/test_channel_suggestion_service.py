"""Focused tests for user-submitted channel suggestions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from memexpert.models.enums import ChannelSuggestionStatus, SourcePlatform
from memexpert.models.user import ChannelSuggestion
from memexpert.services import ChannelSuggestionService, InvalidChannelSuggestionError, UserService
from memexpert.services.channel_suggestion_service import normalize_channel_suggestion
from tests.conftest import create_full_user_via_upgrade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def test_normalize_channel_suggestion_accepts_supported_platforms() -> None:
    telegram = normalize_channel_suggestion("@memexpert_source")
    reddit = normalize_channel_suggestion("https://reddit.com/r/memes")
    vk = normalize_channel_suggestion("vk.com/meme.group")

    assert telegram.platform is SourcePlatform.TELEGRAM
    assert telegram.channel_url == "https://t.me/memexpert_source"
    assert reddit.platform is SourcePlatform.REDDIT
    assert reddit.channel_url == "https://www.reddit.com/r/memes"
    assert vk.platform is SourcePlatform.VK
    assert vk.channel_url == "https://vk.com/meme.group"


def test_normalize_channel_suggestion_rejects_unsupported_urls() -> None:
    with pytest.raises(InvalidChannelSuggestionError, match="Supported suggestions"):
        normalize_channel_suggestion("https://example.com/memes")


@pytest.mark.asyncio
async def test_submit_channel_suggestion_creates_pending_row_and_deduplicates(
    migrated_db_session: AsyncSession,
) -> None:
    user_service = UserService(migrated_db_session)
    linked_user = await create_full_user_via_upgrade(user_service, telegram_id=991_100_200)
    service = ChannelSuggestionService(migrated_db_session)

    first = await service.submit_channel_suggestion(user_id=linked_user.id, channel="@memexpert_source")
    duplicate = await service.submit_channel_suggestion(user_id=linked_user.id, channel="https://t.me/memexpert_source")

    row_count = await migrated_db_session.scalar(select(func.count()).select_from(ChannelSuggestion))
    persisted = await migrated_db_session.get(ChannelSuggestion, first.suggestion.id)
    assert first.created is True
    assert duplicate.created is False
    assert duplicate.suggestion.id == first.suggestion.id
    assert row_count == 1
    assert persisted is not None
    assert persisted.status is ChannelSuggestionStatus.PENDING
    assert persisted.platform is SourcePlatform.TELEGRAM
    assert persisted.channel_url == "https://t.me/memexpert_source"
