# ruff: noqa: TC001,TC003
"""User-facing and service-facing user/account schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from memexpert.models.enums import (
    AccountDeletionAction,
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    ChannelSuggestionStatus,
    SourcePlatform,
    UserLanguage,
)
from memexpert.schemas.base import ORMSchema


class LoginEventRead(ORMSchema):
    """Durable audit row for a session issuance (login or guest bootstrap)."""

    id: uuid.UUID
    user_id: uuid.UUID
    ip_address: str | None
    user_agent: str | None
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime


class AccountMergeLogRead(ORMSchema):
    """DTO for guest-to-full account merge audit records."""

    id: uuid.UUID
    guest_account_id: uuid.UUID
    target_account_id: uuid.UUID
    favorites_transferred: int
    views_transferred: int
    details: dict[str, object]
    created_at: datetime


class AccountDeletionLogRead(ORMSchema):
    """DTO for account deletion audit events."""

    id: uuid.UUID
    user_id: uuid.UUID
    action: AccountDeletionAction
    details: dict[str, object]
    created_at: datetime


class AnalyticsEventRead(ORMSchema):
    """DTO for persisted analytics events."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    event_type: AnalyticsEventType
    payload: dict[str, object]
    occurred_at: datetime


class InlineUsageEventRead(ORMSchema):
    """DTO for inline usage telemetry rows."""

    id: uuid.UUID
    user_id: uuid.UUID
    group_hash: str
    occurred_at: datetime


class ChannelSuggestionRead(ORMSchema):
    """DTO for crawl-channel suggestion moderation state."""

    id: uuid.UUID
    user_id: uuid.UUID
    platform: SourcePlatform
    channel_url: str
    status: ChannelSuggestionStatus
    admin_note: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserRead(ORMSchema):
    """Primary account DTO for later auth and profile services."""

    id: uuid.UUID
    account_type: AccountType
    status: AccountStatus
    telegram_id: int | None
    google_id: str | None
    email: str | None
    email_verified_at: datetime | None
    active_save_collection_id: uuid.UUID | None
    nsfw_enabled: bool
    is_admin: bool
    language: UserLanguage
    last_active_at: datetime | None
    guest_expires_at: datetime | None
    deletion_requested_at: datetime | None
    deletion_due_at: datetime | None
    deleted_at: datetime | None
    token_nonce: int
    created_at: datetime
    updated_at: datetime


__all__ = [
    "AccountDeletionLogRead",
    "AccountMergeLogRead",
    "AnalyticsEventRead",
    "ChannelSuggestionRead",
    "InlineUsageEventRead",
    "LoginEventRead",
    "UserRead",
]
