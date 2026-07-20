"""Idempotent, privacy-bounded meme exposure and conversion facts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from memexpert.models.user import MemeExposure

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

type MemeExposureKind = Literal["web_card", "telegram_inline"]
type MemeExposureStage = Literal[
    "exposed_at",
    "detail_clicked_at",
    "high_intent_action_at",
    "inline_chosen_at",
    "inline_sent_at",
]


class MemeExposureService:
    """Upsert first-observed exposure stages without storing actor identity."""

    def __init__(self, session: AsyncSession, *, autocommit: bool = True) -> None:
        self._session = session
        self._autocommit = autocommit

    async def record_web_exposure(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        await self._record(
            meme_id=meme_id,
            exposure_key=exposure_key,
            kind="web_card",
            stage="exposed_at",
            occurred_at=occurred_at,
        )

    async def record_web_detail_click(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        await self._record(
            meme_id=meme_id,
            exposure_key=exposure_key,
            kind="web_card",
            stage="detail_clicked_at",
            occurred_at=occurred_at,
        )

    async def record_web_high_intent_action(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        await self._record(
            meme_id=meme_id,
            exposure_key=exposure_key,
            kind="web_card",
            stage="high_intent_action_at",
            occurred_at=occurred_at,
        )

    async def record_inline_exposure(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        await self._record(
            meme_id=meme_id,
            exposure_key=exposure_key,
            kind="telegram_inline",
            stage="exposed_at",
            occurred_at=occurred_at,
        )

    async def record_inline_chosen(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        await self._record(
            meme_id=meme_id,
            exposure_key=exposure_key,
            kind="telegram_inline",
            stage="inline_chosen_at",
            occurred_at=occurred_at,
        )

    async def record_inline_sent(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        await self._record(
            meme_id=meme_id,
            exposure_key=exposure_key,
            kind="telegram_inline",
            stage="inline_sent_at",
            occurred_at=occurred_at,
        )

    async def _record(
        self,
        *,
        meme_id: uuid.UUID,
        exposure_key: str,
        kind: MemeExposureKind,
        stage: MemeExposureStage,
        occurred_at: datetime | None,
    ) -> None:
        normalized_key = exposure_key.strip()
        if not normalized_key or len(normalized_key) > 255:
            return
        observed_at = _normalize_utc(occurred_at)
        values: dict[str, object] = {
            "meme_id": meme_id,
            "exposure_key": normalized_key,
            "kind": kind,
            stage: observed_at,
        }
        excluded_stage = getattr(insert(MemeExposure).excluded, stage)
        current_stage = getattr(MemeExposure, stage)
        statement = (
            insert(MemeExposure)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_meme_exposures_meme_kind_key",
                set_={stage: func.least(current_stage, excluded_stage), "updated_at": func.now()},
            )
        )
        await self._session.execute(statement)
        if self._autocommit:
            await self._session.commit()


def _normalize_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["MemeExposureKind", "MemeExposureService"]
