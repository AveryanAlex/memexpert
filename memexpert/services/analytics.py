# ruff: noqa: TC003
"""Best-effort product analytics helpers and launch KPI reporting."""

from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select

from memexpert.models.base import utcnow
from memexpert.models.content import MemePopularitySnapshot
from memexpert.models.enums import AccountType, AnalyticsEventType
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

INTERACTION_EVENT_SCHEMA_VERSION = 1


class InteractionActorType(StrEnum):
    """Audited actor kinds for strict interaction events."""

    USER = "user"
    ANONYMOUS = "anonymous"
    SYSTEM = "system"


_UNSAFE_PAYLOAD_KEYS = frozenset(
    {
        "authorization",
        "chatid",
        "cookie",
        "cookies",
        "groupid",
        "ipaddress",
        "requestheaders",
        "token",
        "useragent",
    }
)
_MEME_REF_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.FAVORITE,
        AnalyticsEventType.INLINE_CHOSEN,
        AnalyticsEventType.INLINE_SENT,
        AnalyticsEventType.INLINE_SERVED,
        AnalyticsEventType.MEME_DETAIL_CLICK,
        AnalyticsEventType.MEME_DOWNLOAD,
        AnalyticsEventType.MEME_IMPRESSION,
        AnalyticsEventType.MEME_LIKE,
        AnalyticsEventType.MEME_PIN,
        AnalyticsEventType.MEME_REPORT,
        AnalyticsEventType.MEME_SAVE,
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.MEME_VIEW,
        AnalyticsEventType.SAVE,
    }
)


def _normalize_payload_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def _validate_safe_json_value(value: object, *, path: tuple[str, ...]) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{'.'.join(path) or 'payload'} must not contain non-finite floats")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_safe_json_value(item, path=(*path, str(index)))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{'.'.join(path) or 'payload'} contains a non-string JSON key")
            normalized_key = _normalize_payload_key(key)
            if normalized_key in _UNSAFE_PAYLOAD_KEYS:
                raise ValueError(f"Unsafe analytics payload key '{key}' is not allowed")
            _validate_safe_json_value(item, path=(*path, key))
        return
    raise ValueError(f"{'.'.join(path) or 'payload'} contains a non-JSON-safe value of type {type(value).__name__}")


def _normalize_utc(value: datetime | None) -> datetime:
    resolved = value or utcnow()
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=UTC)
    return resolved.astimezone(UTC)


class InteractionEventRefs(BaseModel):
    """Typed internal refs allowed inside strict analytics payloads.

    Schema decision: refs use only internal UUIDs and never store raw external
    platform identifiers. Anything chat- or request-specific must already be
    reduced to safe attribution strings such as ``request_id`` or hashed values
    inside ``properties``.
    """

    model_config = ConfigDict(extra="forbid")

    account_merge_log_id: uuid.UUID | None = None
    channel_suggestion_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    meme_file_id: uuid.UUID | None = None
    meme_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    source_channel_id: uuid.UUID | None = None
    source_meme_id: uuid.UUID | None = None
    source_user_id: uuid.UUID | None = None
    target_user_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None


class _InteractionEventPayloadFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str = Field(min_length=1, max_length=120)
    refs: InteractionEventRefs = Field(default_factory=InteractionEventRefs)
    request_id: str | None = Field(default=None, max_length=255)
    impression_id: str | None = Field(default=None, max_length=255)
    source_algorithm: str | None = Field(default=None, max_length=120)
    query: str | None = Field(default=None, max_length=500)
    rank: int | None = Field(default=None, ge=0)
    score: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=255)
    properties: dict[str, object] = Field(default_factory=dict)

    @field_validator("surface")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("surface must not be blank")
        return normalized

    @field_validator("request_id", "impression_id", "source_algorithm", "query", "reason")
    @classmethod
    def _validate_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("score")
    @classmethod
    def _validate_score(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value

    @field_validator("score_components")
    @classmethod
    def _validate_score_components(cls, value: dict[str, float]) -> dict[str, float]:
        _validate_safe_json_value(value, path=("score_components",))
        for component_value in value.values():
            if not math.isfinite(component_value):
                raise ValueError("score_components values must be finite")
        return value

    @field_validator("properties")
    @classmethod
    def _validate_properties(cls, value: dict[str, object]) -> dict[str, object]:
        _validate_safe_json_value(value, path=("properties",))
        return value


class InteractionEventWrite(_InteractionEventPayloadFields):
    """Canonical strict writer input for the shared analytics stream.

    Schema decision: strict interaction events stay in ``analytics_events`` and
    write a versioned payload envelope with top-level ``schema_version``,
    ``actor_type``, ``actor_account_type``, ``surface``, ``refs``, and a small
    JSON-safe ``properties`` bag. Legacy event names stay valid; new canonical
    meme-scoped names use the ``meme_*`` family, while broader flows use
    ``collection_action``, ``auth_event``, ``account_merge``, ``miniapp_open``,
    and ``channel_suggest``.
    """

    event_type: AnalyticsEventType
    user_id: uuid.UUID | None = None
    actor_type: InteractionActorType | None = None
    actor_account_type: AccountType | None = None
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_contract(self) -> InteractionEventWrite:
        if self.user_id is None and self.actor_account_type is not None:
            raise ValueError("actor_account_type requires a non-null user_id")
        if self.user_id is None and self.actor_type == InteractionActorType.USER:
            raise ValueError("actor_type='user' requires a non-null user_id")
        if self.user_id is not None and self.actor_type in {
            InteractionActorType.ANONYMOUS,
            InteractionActorType.SYSTEM,
        }:
            raise ValueError("anonymous or system actor types cannot carry user_id")
        if self.event_type in _MEME_REF_EVENT_TYPES and self.refs.meme_id is None:
            raise ValueError(f"{self.event_type.value} events require refs.meme_id")
        if self.event_type == AnalyticsEventType.COLLECTION_ACTION and self.refs.collection_id is None:
            raise ValueError("collection_action events require refs.collection_id")
        if self.event_type == AnalyticsEventType.ACCOUNT_MERGE and (
            self.refs.source_user_id is None or self.refs.target_user_id is None
        ):
            raise ValueError("account_merge events require refs.source_user_id and refs.target_user_id")
        if self.event_type in {AnalyticsEventType.AUTH_EVENT, AnalyticsEventType.COLLECTION_ACTION}:
            action = self.properties.get("action")
            if not isinstance(action, str) or not action.strip():
                raise ValueError(f"{self.event_type.value} events require properties.action")
        return self


class InteractionEventPayload(_InteractionEventPayloadFields):
    """Persisted strict payload envelope written into ``analytics_events.payload``."""

    schema_version: int = INTERACTION_EVENT_SCHEMA_VERSION
    actor_type: InteractionActorType
    actor_account_type: AccountType | None = None


class LaunchKPIRead(BaseModel):
    """Small operator-facing launch metrics derived from analytics and source snapshots."""

    lookback_hours: int
    since: datetime
    searches: int
    views: int
    sends: int
    active_users: int
    likes: int
    saves: int
    guest_to_full_conversions: int
    source_views: int
    source_reactions: int
    source_reposts: int


class AnalyticsService:
    """Write product events without making user-facing paths depend on analytics health."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_interaction_event(
        self,
        event: InteractionEventWrite | Mapping[str, Any],
    ) -> AnalyticsEvent:
        """Persist one strict interaction event in the shared analytics stream.

        This writer is intended for API routes, shared services, bot handlers,
        scheduler jobs, and frontend-backed endpoints that need durable,
        auditable interaction telemetry without opening a second event stream.
        Invalid payloads raise before the DB write.
        """

        write = event if isinstance(event, InteractionEventWrite) else InteractionEventWrite.model_validate(event)
        actor_type, actor_account_type = await self._resolve_actor_context(write)
        payload = InteractionEventPayload(
            actor_account_type=actor_account_type,
            actor_type=actor_type,
            impression_id=write.impression_id,
            properties=write.properties,
            query=write.query,
            rank=write.rank,
            reason=write.reason,
            refs=write.refs,
            request_id=write.request_id,
            score=write.score,
            score_components=write.score_components,
            source_algorithm=write.source_algorithm,
            surface=write.surface,
        )
        analytics_event = AnalyticsEvent(
            user_id=write.user_id,
            event_type=write.event_type,
            payload=payload.model_dump(mode="json", exclude_none=True),
            occurred_at=_normalize_utc(write.occurred_at),
        )
        try:
            self._session.add(analytics_event)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return analytics_event

    async def record_event(
        self,
        event_type: AnalyticsEventType,
        *,
        user_id: uuid.UUID | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Persist one event and swallow failures after rolling back the session.

        Callers should pass only product identifiers and coarse context. External
        identifiers must be hashed before they reach this method.
        """

        try:
            self._session.add(
                AnalyticsEvent(
                    user_id=user_id,
                    event_type=event_type,
                    payload=payload or {},
                )
            )
            await self._session.commit()
        except Exception:
            logger.exception("Analytics event write failed.")
            await self._session.rollback()

    async def _resolve_actor_context(
        self,
        write: InteractionEventWrite,
    ) -> tuple[InteractionActorType, AccountType | None]:
        if write.user_id is None:
            actor_type = write.actor_type or InteractionActorType.ANONYMOUS
            return actor_type, None

        actor_type = write.actor_type or InteractionActorType.USER
        user = await self._session.scalar(select(User).where(User.id == write.user_id))
        if user is None:
            raise ValueError(f"Unknown analytics user_id '{write.user_id}'")
        account_type = user.account_type
        if actor_type != InteractionActorType.USER:
            raise ValueError("Non-user actor types cannot carry user_id")
        if write.actor_account_type is not None and write.actor_account_type != account_type:
            raise ValueError(
                "actor_account_type does not match the persisted user account_type "
                f"({write.actor_account_type.value} != {account_type.value})"
            )
        return actor_type, account_type

    async def launch_kpis(self, *, lookback_hours: int = 168) -> LaunchKPIRead:
        """Return launch KPI counts for a bounded recent window.

        Event counts come from ``AnalyticsEvent``. Source counters use the latest
        ``MemePopularitySnapshot`` per meme captured in the same window so source
        crawler metrics can be compared with platform activity.
        """

        resolved_hours = max(1, lookback_hours)
        since = utcnow() - timedelta(hours=resolved_hours)
        event_counts = await self._event_counts_since(since)
        source_views, source_reactions, source_reposts = await self._latest_source_metric_totals_since(since)
        conversions = await self._session.scalar(
            select(func.count()).select_from(AccountMergeLog).where(AccountMergeLog.created_at >= since)
        )
        active_users = await self._session.scalar(
            select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                AnalyticsEvent.occurred_at >= since,
                AnalyticsEvent.user_id.is_not(None),
            )
        )
        return LaunchKPIRead(
            lookback_hours=resolved_hours,
            since=since,
            searches=event_counts.get(AnalyticsEventType.SEARCH_QUERY, 0),
            views=event_counts.get(AnalyticsEventType.MEME_VIEW, 0),
            sends=event_counts.get(AnalyticsEventType.MEME_SEND, 0),
            active_users=active_users or 0,
            likes=event_counts.get(AnalyticsEventType.MEME_LIKE, 0),
            saves=event_counts.get(AnalyticsEventType.MEME_SAVE, 0) + event_counts.get(AnalyticsEventType.SAVE, 0),
            guest_to_full_conversions=conversions or 0,
            source_views=source_views,
            source_reactions=source_reactions,
            source_reposts=source_reposts,
        )

    async def _event_counts_since(self, since: datetime) -> dict[AnalyticsEventType, int]:
        result = await self._session.execute(
            select(AnalyticsEvent.event_type, func.count())
            .where(AnalyticsEvent.occurred_at >= since)
            .group_by(AnalyticsEvent.event_type)
        )
        return {event_type: count for event_type, count in result.all()}

    async def _latest_source_metric_totals_since(self, since: datetime) -> tuple[int, int, int]:
        result = await self._session.execute(
            select(MemePopularitySnapshot)
            .where(MemePopularitySnapshot.captured_at >= since)
            .order_by(MemePopularitySnapshot.meme_id, MemePopularitySnapshot.captured_at.desc())
        )
        latest_by_meme: dict[uuid.UUID, MemePopularitySnapshot] = {}
        for snapshot in result.scalars():
            latest_by_meme.setdefault(snapshot.meme_id, snapshot)
        return (
            sum(snapshot.source_views for snapshot in latest_by_meme.values()),
            sum(snapshot.source_reactions for snapshot in latest_by_meme.values()),
            sum(snapshot.source_reposts for snapshot in latest_by_meme.values()),
        )


def hash_external_identifier(namespace: str, value: object) -> str:
    """Hash an external platform identifier before it enters analytics payloads."""

    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()


__all__ = [
    "AnalyticsService",
    "InteractionActorType",
    "InteractionEventPayload",
    "InteractionEventRefs",
    "InteractionEventWrite",
    "LaunchKPIRead",
    "hash_external_identifier",
]
