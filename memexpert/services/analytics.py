# ruff: noqa: TC003
"""Best-effort product analytics helpers and launch KPI reporting."""

from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, text

from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeTemplate
from memexpert.models.enums import AccountType, AnalyticsEventType
from memexpert.models.user import AccountMergeLog, AnalyticsEvent, User
from memexpert.schemas.auth import (
    ProfileStatsMetadataRead,
    ProfileStatsRead,
    ProfileStatsTagRead,
    ProfileStatsTemplateRead,
)
from memexpert.services.meme_exposure import MemeExposureService

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
_HIGH_INTENT_EXPOSURE_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.FAVORITE,
        AnalyticsEventType.MEME_DOWNLOAD,
        AnalyticsEventType.MEME_LIKE,
        AnalyticsEventType.MEME_SAVE,
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.MEME_SHARE,
        AnalyticsEventType.SAVE,
        AnalyticsEventType.SHARE,
    }
)
_PROFILE_VIEW_EVENT_TYPES = frozenset({AnalyticsEventType.MEME_VIEW, AnalyticsEventType.VIEW})
_PROFILE_SENT_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.MEME_SEND,
        AnalyticsEventType.INLINE_SENT,
        AnalyticsEventType.SHARE,
    }
)
_PROFILE_SAVED_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.MEME_SAVE,
        AnalyticsEventType.SAVE,
        AnalyticsEventType.FAVORITE,
    }
)
_PROFILE_DOWNLOADED_EVENT_TYPES = frozenset({AnalyticsEventType.MEME_DOWNLOAD})
_PROFILE_NON_ACTIVITY_EVENT_TYPES = frozenset(
    {
        AnalyticsEventType.ACCOUNT_MERGE,
        AnalyticsEventType.AUTH_EVENT,
        AnalyticsEventType.MINIAPP_OPEN,
        AnalyticsEventType.PAGE_VIEW,
    }
)
_PROFILE_NO_INTERACTIONS_NOTE = "No interactions yet; stats are zero until this user interacts with memes."
_PROFILE_TAGS_REQUIREMENTS_NOTE = "Top tags require analytics events with payload.refs.meme_id and tagged meme rows."
_PROFILE_TEMPLATES_REQUIREMENTS_NOTE = (
    "Top templates require analytics events with payload.refs.meme_id and classified template ids."
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


def _strict_payload_meme_ref(payload: Mapping[str, object]) -> uuid.UUID | None:
    refs = payload.get("refs")
    if not isinstance(refs, Mapping):
        return None

    meme_id = refs.get("meme_id")
    if not isinstance(meme_id, str):
        return None

    try:
        return uuid.UUID(meme_id)
    except ValueError:
        return None


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
    """Read and write product analytics without inventing user-facing metrics."""

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
        occurred_at = _normalize_utc(write.occurred_at)
        analytics_event = AnalyticsEvent(
            user_id=write.user_id,
            event_type=write.event_type,
            payload=payload.model_dump(mode="json", exclude_none=True),
            occurred_at=occurred_at,
        )
        try:
            self._session.add(analytics_event)
            await self._record_exposure_fact(write, occurred_at=occurred_at)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return analytics_event

    async def _record_exposure_fact(self, write: InteractionEventWrite, *, occurred_at: datetime) -> None:
        """Project attributed exposure stages into an idempotent public-safe fact."""

        meme_id = write.refs.meme_id
        exposure_key = write.impression_id
        if meme_id is None or exposure_key is None:
            return
        if await self._session.scalar(select(Meme.id).where(Meme.id == meme_id).limit(1)) is None:
            # Strict analytics refs intentionally support events whose target
            # has already been removed; the public exposure projection is
            # narrower and only retains currently catalogued memes.
            return

        service = MemeExposureService(self._session, autocommit=False)
        if write.event_type in (AnalyticsEventType.IMPRESSION, AnalyticsEventType.MEME_IMPRESSION):
            await service.record_web_exposure(
                meme_id=meme_id,
                exposure_key=exposure_key,
                occurred_at=occurred_at,
            )
            return
        if write.event_type in (AnalyticsEventType.CLICK, AnalyticsEventType.MEME_DETAIL_CLICK):
            await service.record_web_detail_click(
                meme_id=meme_id,
                exposure_key=exposure_key,
                occurred_at=occurred_at,
            )
            return
        if write.event_type is AnalyticsEventType.INLINE_SERVED:
            await service.record_inline_exposure(
                meme_id=meme_id,
                exposure_key=exposure_key,
                occurred_at=occurred_at,
            )
            return
        if write.event_type is AnalyticsEventType.INLINE_CHOSEN:
            await service.record_inline_chosen(
                meme_id=meme_id,
                exposure_key=exposure_key,
                occurred_at=occurred_at,
            )
            return
        if write.event_type is AnalyticsEventType.INLINE_SENT:
            await service.record_inline_sent(
                meme_id=meme_id,
                exposure_key=exposure_key,
                occurred_at=occurred_at,
            )
            return
        if write.event_type in _HIGH_INTENT_EXPOSURE_EVENT_TYPES and not write.surface.startswith("telegram_inline"):
            await service.record_web_high_intent_action(
                meme_id=meme_id,
                exposure_key=exposure_key,
                occurred_at=occurred_at,
            )

    async def record_interaction_event_best_effort(
        self,
        event: InteractionEventWrite | Mapping[str, Any],
    ) -> bool:
        """Persist a strict interaction event without affecting the caller's outcome.

        Product telemetry is useful but must not turn a successful page render,
        search, or interaction into an application error. This wrapper keeps
        the strict payload validation used by :meth:`record_interaction_event`
        while swallowing persistence failures after that method rolls back its
        transaction. Its log metadata intentionally excludes payload values,
        user identifiers, URLs, and raw queries.
        """

        event_type = event.event_type.value if isinstance(event, InteractionEventWrite) else "unvalidated"
        try:
            await self.record_interaction_event(event)
        except Exception as exc:
            logger.exception(
                "analytics_interaction_event_write_failed",
                exc_info=False,
                extra={
                    "event": "analytics_interaction_event_write_failed",
                    "event_type": event_type,
                    "exception_type": type(exc).__name__,
                },
            )
            return False
        return True

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
        except Exception as exc:
            logger.exception(
                "analytics_event_write_failed",
                exc_info=False,
                extra={
                    "event": "analytics_event_write_failed",
                    "event_type": event_type.value,
                    "user_id": str(user_id) if user_id is not None else None,
                    "payload_key_count": len(payload or {}),
                    "payload_keys": sorted((payload or {}).keys()),
                    "exception_type": type(exc).__name__,
                },
            )
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
        successful source engagement snapshot per source post captured in the
        same window so crawler metrics can be compared with platform activity.
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

    async def profile_stats(self, *, user_id: uuid.UUID, top_limit: int = 5) -> ProfileStatsRead:
        """Return one user's profile stats from persisted analytics events only."""

        events = list(
            (
                await self._session.execute(
                    select(AnalyticsEvent)
                    .where(AnalyticsEvent.user_id == user_id)
                    .order_by(AnalyticsEvent.occurred_at.asc(), AnalyticsEvent.id.asc())
                )
            ).scalars()
        )
        profile_events = [
            event
            for event in events
            if event.event_type not in _PROFILE_NON_ACTIVITY_EVENT_TYPES
        ]
        event_counts = Counter(event.event_type for event in profile_events)
        active_days = {_normalize_utc(event.occurred_at).date() for event in profile_events}
        meme_event_counts: Counter[uuid.UUID] = Counter()
        for event in profile_events:
            meme_id = _strict_payload_meme_ref(event.payload)
            if meme_id is not None:
                meme_event_counts[meme_id] += 1

        top_tags, top_templates = await self._top_profile_interaction_metadata(
            meme_event_counts,
            limit=top_limit,
        )
        notes: list[str] = []
        if not profile_events:
            notes.append(_PROFILE_NO_INTERACTIONS_NOTE)
        notes.extend([_PROFILE_TAGS_REQUIREMENTS_NOTE, _PROFILE_TEMPLATES_REQUIREMENTS_NOTE])

        return ProfileStatsRead(
            viewed=sum(event_counts[event_type] for event_type in _PROFILE_VIEW_EVENT_TYPES),
            sent=sum(event_counts[event_type] for event_type in _PROFILE_SENT_EVENT_TYPES),
            saved=sum(event_counts[event_type] for event_type in _PROFILE_SAVED_EVENT_TYPES),
            downloaded=sum(event_counts[event_type] for event_type in _PROFILE_DOWNLOADED_EVENT_TYPES),
            days_active=len(active_days),
            top_tags=top_tags,
            top_templates=top_templates,
            metadata=ProfileStatsMetadataRead(notes=notes),
        )

    async def _top_profile_interaction_metadata(
        self,
        meme_event_counts: Counter[uuid.UUID],
        *,
        limit: int,
    ) -> tuple[list[ProfileStatsTagRead], list[ProfileStatsTemplateRead]]:
        if not meme_event_counts or limit <= 0:
            return [], []

        result = await self._session.execute(
            select(Meme.id, Meme.tags, Meme.template_id, MemeTemplate.slug, MemeTemplate.name)
            .outerjoin(MemeTemplate, Meme.template_id == MemeTemplate.id)
            .where(Meme.id.in_(list(meme_event_counts)))
        )
        tag_counts: Counter[str] = Counter()
        template_counts: Counter[uuid.UUID] = Counter()
        template_labels: dict[uuid.UUID, tuple[str, str]] = {}
        for meme_id, tags, template_id, template_slug, template_name in result.all():
            event_count = meme_event_counts[meme_id]
            for tag in dict.fromkeys(tag.strip() for tag in tags or [] if tag.strip()):
                tag_counts[tag] += event_count
            if template_id is not None and template_slug is not None and template_name is not None:
                template_counts[template_id] += event_count
                template_labels[template_id] = (template_slug, template_name)

        top_tags = [
            ProfileStatsTagRead(tag=tag, count=count)
            for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
        ]
        top_templates = [
            ProfileStatsTemplateRead(
                template_id=template_id,
                slug=template_labels[template_id][0],
                name=template_labels[template_id][1],
                count=count,
            )
            for template_id, count in sorted(
                template_counts.items(),
                key=lambda item: (-item[1], template_labels[item[0]][1], str(item[0])),
            )[:limit]
        ]
        return top_tags, top_templates

    async def _event_counts_since(self, since: datetime) -> dict[AnalyticsEventType, int]:
        result = await self._session.execute(
            select(AnalyticsEvent.event_type, func.count())
            .where(AnalyticsEvent.occurred_at >= since)
            .group_by(AnalyticsEvent.event_type)
        )
        return {event_type: count for event_type, count in result.all()}

    async def _latest_source_metric_totals_since(self, since: datetime) -> tuple[int, int, int]:
        result = await self._session.execute(
            text(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (meme_source_id)
                        meme_source_id,
                        view_count,
                        reaction_count,
                        forward_count
                    FROM meme_source_engagement_snapshots
                    WHERE fetch_status::text = 'success'
                      AND captured_at >= :since
                    ORDER BY meme_source_id, captured_at DESC, id DESC
                )
                SELECT
                    COALESCE(sum(COALESCE(view_count, 0)), 0)::integer AS source_views,
                    COALESCE(sum(COALESCE(reaction_count, 0)), 0)::integer AS source_reactions,
                    COALESCE(sum(COALESCE(forward_count, 0)), 0)::integer AS source_reposts
                FROM latest
                """
            ),
            {"since": since},
        )
        row = result.mappings().one()
        return int(row["source_views"] or 0), int(row["source_reactions"] or 0), int(row["source_reposts"] or 0)


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
