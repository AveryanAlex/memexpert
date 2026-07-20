"""Durable Telegram channel audience observations and daily scheduling helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, SourceChannelAudienceSnapshot
from memexpert.models.enums import (
    SourceChannelAudienceCaptureReason,
    SourceChannelAudienceFetchStatus,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class SourceChannelAudienceObservation:
    """Normalized provider result ready for durable persistence."""

    fetch_status: SourceChannelAudienceFetchStatus
    subscriber_count: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.subscriber_count is not None and self.subscriber_count < 0:
            raise ValueError("source channel audience subscriber_count must be non-negative.")
        if self.fetch_status is SourceChannelAudienceFetchStatus.SUCCESS and self.subscriber_count is None:
            raise ValueError("successful source channel audience observations require subscriber_count.")
        if self.fetch_status is not SourceChannelAudienceFetchStatus.SUCCESS and self.subscriber_count is not None:
            raise ValueError("non-successful source channel audience observations cannot include subscriber_count.")


def source_channel_audience_observation_from_count(
    subscriber_count: int | None,
) -> SourceChannelAudienceObservation:
    """Map a Telegram count, preserving known zero and explicit non-exposure."""

    if subscriber_count is None:
        return SourceChannelAudienceObservation(
            fetch_status=SourceChannelAudienceFetchStatus.NOT_EXPOSED,
        )
    if subscriber_count < 0:
        raise ValueError("Telegram subscriber_count must be non-negative when exposed.")
    return SourceChannelAudienceObservation(
        fetch_status=SourceChannelAudienceFetchStatus.SUCCESS,
        subscriber_count=subscriber_count,
    )


def next_daily_source_channel_audience_capture_at(
    source_channel_id: uuid.UUID,
    *,
    after: datetime,
) -> datetime:
    """Return the next UTC daily slot with stable per-channel rollout jitter."""

    captured_after = _as_utc(after)
    next_day = captured_after.date() + timedelta(days=1)
    digest = hashlib.sha256(source_channel_id.bytes).digest()
    jitter_seconds = int.from_bytes(digest[:4], "big") % 86_400
    return datetime.combine(next_day, time.min, tzinfo=UTC) + timedelta(seconds=jitter_seconds)


async def record_source_channel_audience_observation(
    session: AsyncSession,
    source_channel: SourceChannel,
    observation: SourceChannelAudienceObservation,
    *,
    capture_reason: SourceChannelAudienceCaptureReason,
    telegram_session_id: uuid.UUID | None = None,
    captured_at: datetime | None = None,
    capture_slot: date | None = None,
    advance_daily_schedule: bool = False,
) -> SourceChannelAudienceSnapshot:
    """Upsert one reason/slot observation and update latest channel state.

    Retries may replace a failed original slot instead of inventing duplicate
    samples. The first success/not-exposed outcome is immutable, and only a
    successful observation replaces the channel's latest-count cache.
    """

    resolved_captured_at = _as_utc(captured_at or utcnow())
    resolved_capture_slot = capture_slot or resolved_captured_at.date()
    snapshot = await session.scalar(
        select(SourceChannelAudienceSnapshot)
        .where(
            SourceChannelAudienceSnapshot.source_channel_id == source_channel.id,
            SourceChannelAudienceSnapshot.capture_slot == resolved_capture_slot,
            SourceChannelAudienceSnapshot.capture_reason == capture_reason,
        )
        .with_for_update(of=SourceChannelAudienceSnapshot)
    )
    if snapshot is not None and snapshot.fetch_status is not SourceChannelAudienceFetchStatus.FAILED:
        # A successful or explicitly not-exposed observation is immutable
        # history for its channel/day/reason slot. Opportunistic crawler
        # refreshes can run more than once per day; a later provider failure
        # must not erase an earlier public observation or destabilize a
        # previously issued analytics cutoff.
        if advance_daily_schedule:
            source_channel.next_audience_capture_at = next_daily_source_channel_audience_capture_at(
                source_channel.id,
                after=resolved_captured_at,
            )
            source_channel.audience_capture_locked_at = None
            source_channel.audience_capture_lock_owner = None
        await session.flush()
        return snapshot

    if snapshot is None:
        snapshot = SourceChannelAudienceSnapshot(
            source_channel_id=source_channel.id,
            telegram_session_id=telegram_session_id or source_channel.telegram_session_id,
            captured_at=resolved_captured_at,
            capture_slot=resolved_capture_slot,
            capture_reason=capture_reason,
            fetch_status=observation.fetch_status,
            subscriber_count=observation.subscriber_count,
            error_code=observation.error_code,
        )
        session.add(snapshot)
    else:
        snapshot.telegram_session_id = telegram_session_id or source_channel.telegram_session_id
        snapshot.captured_at = resolved_captured_at
        snapshot.fetch_status = observation.fetch_status
        snapshot.subscriber_count = observation.subscriber_count
        snapshot.error_code = observation.error_code

    source_channel.last_audience_capture_at = resolved_captured_at
    source_channel.last_audience_error_code = observation.error_code
    if observation.fetch_status is SourceChannelAudienceFetchStatus.SUCCESS:
        source_channel.subscriber_count = observation.subscriber_count
        source_channel.subscriber_count_updated_at = resolved_captured_at

    if advance_daily_schedule:
        source_channel.next_audience_capture_at = next_daily_source_channel_audience_capture_at(
            source_channel.id,
            after=resolved_captured_at,
        )
        source_channel.audience_capture_locked_at = None
        source_channel.audience_capture_lock_owner = None

    await session.flush()
    return snapshot


async def terminal_source_channel_audience_snapshot_for_slot(
    session: AsyncSession,
    source_channel_id: uuid.UUID,
    *,
    capture_slot: date,
    capture_reason: SourceChannelAudienceCaptureReason,
) -> SourceChannelAudienceSnapshot | None:
    """Return an immutable terminal observation for one capture slot, if any.

    Failed observations deliberately do not match so callers can retry the
    provider request and let :func:`record_source_channel_audience_observation`
    replace the retryable row.
    """

    return await session.scalar(
        select(SourceChannelAudienceSnapshot).where(
            SourceChannelAudienceSnapshot.source_channel_id == source_channel_id,
            SourceChannelAudienceSnapshot.capture_slot == capture_slot,
            SourceChannelAudienceSnapshot.capture_reason == capture_reason,
            SourceChannelAudienceSnapshot.fetch_status.in_(
                (
                    SourceChannelAudienceFetchStatus.SUCCESS,
                    SourceChannelAudienceFetchStatus.NOT_EXPOSED,
                )
            ),
        )
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SourceChannelAudienceObservation",
    "next_daily_source_channel_audience_capture_at",
    "record_source_channel_audience_observation",
    "source_channel_audience_observation_from_count",
    "terminal_source_channel_audience_snapshot_for_slot",
]
