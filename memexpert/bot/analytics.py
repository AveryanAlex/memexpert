"""Best-effort strict analytics helpers for Telegram bot handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from memexpert.services.analytics import AnalyticsService, InteractionEventWrite, hash_external_identifier

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def telegram_user_hash(telegram_user_id: object) -> str:
    """Return the analytics-safe hash for a Telegram user identifier."""

    return hash_external_identifier("telegram_user", telegram_user_id)


async def record_telegram_interaction_event(
    session: AsyncSession,
    event: InteractionEventWrite | Mapping[str, Any],
    *,
    log_context: Mapping[str, object] | None = None,
) -> None:
    """Persist a strict interaction event without breaking bot responses."""

    try:
        await AnalyticsService(session).record_interaction_event(event)
    except Exception:
        if session.in_transaction():
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "Telegram analytics rollback failed.",
                    extra={"event": "telegram_analytics_rollback_failed", **dict(log_context or {})},
                )
        logger.exception(
            "Telegram analytics write failed.",
            extra={"event": "telegram_analytics_write_failed", **dict(log_context or {})},
        )


__all__ = ["record_telegram_interaction_event", "telegram_user_hash"]
