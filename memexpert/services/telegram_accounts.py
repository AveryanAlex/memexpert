"""Shared Telegram account resolution helpers for bot surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from memexpert.models.enums import AccountStatus, AccountType
from memexpert.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class TelegramAccountResolutionStatus(StrEnum):
    """Outcomes for Telegram account lookup/create flows."""

    ACTIVE = "active"
    INVALID_TELEGRAM_ID = "invalid_telegram_id"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TelegramAccountResolution:
    """Resolved Telegram user plus the reason if no active user is available."""

    status: TelegramAccountResolutionStatus
    user: User | None = None

    @property
    def is_active(self) -> bool:
        return self.status is TelegramAccountResolutionStatus.ACTIVE and self.user is not None


async def resolve_or_create_active_telegram_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> TelegramAccountResolution:
    """Resolve or create an active full account for a positive Telegram user id."""

    if telegram_user_id <= 0:
        return TelegramAccountResolution(TelegramAccountResolutionStatus.INVALID_TELEGRAM_ID)

    user = await load_telegram_user(session, telegram_user_id=telegram_user_id)
    if user is not None:
        return _resolution_for_existing_user(user)

    user = User(
        telegram_id=telegram_user_id,
        status=AccountStatus.ACTIVE,
        nsfw_enabled=False,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        user = await load_telegram_user(session, telegram_user_id=telegram_user_id)
        if user is None:
            raise
        return _resolution_for_existing_user(user)
    return TelegramAccountResolution(TelegramAccountResolutionStatus.ACTIVE, user=user)


async def load_telegram_user(session: AsyncSession, *, telegram_user_id: int) -> User | None:
    """Load the user row currently bound to a Telegram id, if any."""

    return await session.scalar(select(User).where(User.telegram_id == telegram_user_id))


def _resolution_for_existing_user(user: User) -> TelegramAccountResolution:
    if _is_active_full_user(user):
        return TelegramAccountResolution(TelegramAccountResolutionStatus.ACTIVE, user=user)
    return TelegramAccountResolution(TelegramAccountResolutionStatus.UNAVAILABLE, user=user)


def _is_active_full_user(user: User) -> bool:
    return user.account_type is AccountType.FULL and user.status is AccountStatus.ACTIVE


__all__ = [
    "TelegramAccountResolution",
    "TelegramAccountResolutionStatus",
    "load_telegram_user",
    "resolve_or_create_active_telegram_user",
]
