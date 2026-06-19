# ruff: noqa: TC001,TC003
"""Generic transactional outbox support for RabbitMQ messages."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from memexpert.core.broker import ensure_pipeline_broker_started
from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.content import RabbitMQOutboxMessage
from memexpert.models.enums import RabbitMQOutboxMessageStatus

if TYPE_CHECKING:
    import logging

    from aio_pika.abc import HeadersType
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


DEFAULT_RABBITMQ_CONTENT_TYPE = "application/json"
_MAX_OUTBOX_ERROR_TEXT_LENGTH = 2000


class RabbitBrokerProtocol(Protocol):
    """Minimal async RabbitMQ broker surface used by outbox publishing."""

    async def publish(
        self,
        message: object,
        /,
        queue: str = "",
        exchange: str | None = None,
        *,
        routing_key: str = "",
        mandatory: bool = True,
        persist: bool = False,
        content_type: str | None = None,
        headers: HeadersType | None = None,
        message_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RabbitMessageSpec:
    """A stable RabbitMQ message contract before it becomes an outbox row."""

    exchange: str
    routing_key: str
    payload: dict[str, object]
    message_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str | uuid.UUID
    created_at: datetime
    headers: HeadersType = field(default_factory=dict)
    content_type: str = DEFAULT_RABBITMQ_CONTENT_TYPE
    ordering_key: str | None = None


@dataclass(frozen=True, slots=True)
class RabbitOutboxPublishBatchResult:
    """Counts returned after one outbox relay publish sweep."""

    claimed: int
    published: int
    failed: int


class RabbitOutboxError(RuntimeError):
    """Raised when generic RabbitMQ outbox metadata cannot be persisted or relayed."""


class RabbitPublisher:
    """Producer facade that defaults to durable outbox writes."""

    def __init__(
        self,
        *,
        broker: RabbitBrokerProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._broker = broker
        self._settings = settings or get_settings()

    async def publish(
        self,
        spec: RabbitMessageSpec,
        *,
        session: AsyncSession,
        outbox: bool = True,
    ) -> uuid.UUID:
        """Persist an outbox row by default, or explicitly publish directly."""

        if not session.in_transaction():
            raise RabbitOutboxError("RabbitPublisher.publish requires an active AsyncSession transaction.")

        if outbox:
            message = outbox_message_from_spec(spec)
            session.add(message)
            return message.id

        broker = self._broker or await ensure_pipeline_broker_started(settings=self._settings)
        await publish_rabbit_message_direct(spec, broker=broker, settings=self._settings)
        return outbox_message_id_from_message_id(spec.message_id)


class RabbitOutboxRelay:
    """Relay durable RabbitMQ outbox rows with leases, retries, and at-least-once semantics."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        broker: RabbitBrokerProtocol,
        settings: Settings | None = None,
        lock_owner: str | None = None,
    ) -> None:
        self._session = session
        self._broker = broker
        self._settings = settings or get_settings()
        self._lock_owner = lock_owner or _default_lock_owner()

    async def publish_ids(self, message_ids: Iterable[uuid.UUID]) -> RabbitOutboxPublishBatchResult:
        """Claim and publish specific just-created outbox message rows."""

        unique_ids = tuple(dict.fromkeys(message_ids))
        if not unique_ids:
            return RabbitOutboxPublishBatchResult(claimed=0, published=0, failed=0)

        messages = await self._claim_specific(unique_ids)
        return await self._publish_claimed(messages)

    async def publish_batch(self, *, limit: int = 100) -> RabbitOutboxPublishBatchResult:
        """Claim, publish, and mark one bounded batch of due outbox messages."""

        messages = await self.claim_due(limit=limit)
        return await self._publish_claimed(messages)

    async def claim_due(self, *, limit: int = 100) -> tuple[RabbitMQOutboxMessage, ...]:
        """Claim due pending/failed rows for this relay process."""

        now = utcnow()
        safe_limit = max(1, limit)
        return await self._claim_query(
            select(RabbitMQOutboxMessage)
            .where(
                RabbitMQOutboxMessage.status.in_(
                    (RabbitMQOutboxMessageStatus.PENDING, RabbitMQOutboxMessageStatus.FAILED)
                ),
                or_(
                    RabbitMQOutboxMessage.next_retry_at.is_(None),
                    RabbitMQOutboxMessage.next_retry_at <= now,
                ),
            )
            .order_by(RabbitMQOutboxMessage.created_at.asc(), RabbitMQOutboxMessage.id.asc())
            .with_for_update(skip_locked=True)
            .limit(safe_limit),
            failure_message="Failed to claim RabbitMQ outbox messages for publishing.",
        )

    async def recover_stale_publishing(self, *, stale_before: datetime) -> int:
        """Move stranded publishing rows back into retryable failed state."""

        now = utcnow()
        try:
            result = await self._session.execute(
                select(RabbitMQOutboxMessage)
                .where(
                    RabbitMQOutboxMessage.status == RabbitMQOutboxMessageStatus.PUBLISHING,
                    or_(
                        RabbitMQOutboxMessage.locked_at.is_(None),
                        RabbitMQOutboxMessage.locked_at < stale_before,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            messages = tuple(result.scalars().all())
            for message in messages:
                message.status = RabbitMQOutboxMessageStatus.FAILED
                message.next_retry_at = now
                message.locked_at = None
                message.lock_owner = None
                message.last_error_text = "RabbitMQ outbox message was recovered from a stale publishing lease."
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise RabbitOutboxError("Failed to recover stale RabbitMQ outbox publishing leases.") from exc
        return len(messages)

    async def _claim_specific(self, message_ids: tuple[uuid.UUID, ...]) -> tuple[RabbitMQOutboxMessage, ...]:
        return await self._claim_query(
            select(RabbitMQOutboxMessage)
            .where(
                RabbitMQOutboxMessage.id.in_(message_ids),
                RabbitMQOutboxMessage.status.in_(
                    (RabbitMQOutboxMessageStatus.PENDING, RabbitMQOutboxMessageStatus.FAILED)
                ),
            )
            .order_by(RabbitMQOutboxMessage.created_at.asc(), RabbitMQOutboxMessage.id.asc())
            .with_for_update(skip_locked=True),
            failure_message="Failed to claim specific RabbitMQ outbox messages for publishing.",
        )

    async def _claim_query(
        self,
        query: Select[tuple[RabbitMQOutboxMessage]],
        *,
        failure_message: str,
    ) -> tuple[RabbitMQOutboxMessage, ...]:
        now = utcnow()
        try:
            result = await self._session.execute(query)
            messages = tuple(result.scalars().all())
            for message in messages:
                message.status = RabbitMQOutboxMessageStatus.PUBLISHING
                message.attempt_count += 1
                message.next_retry_at = None
                message.locked_at = now
                message.lock_owner = self._lock_owner
                message.last_error_text = None
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise RabbitOutboxError(failure_message) from exc
        return messages

    async def _publish_claimed(
        self,
        messages: tuple[RabbitMQOutboxMessage, ...],
    ) -> RabbitOutboxPublishBatchResult:
        published = 0
        failed = 0
        for message in messages:
            try:
                await publish_rabbit_message_direct(
                    message_spec_from_outbox_message(message),
                    broker=self._broker,
                    settings=self._settings,
                )
            except Exception as exc:  # noqa: BLE001 - every broker failure becomes retry metadata.
                await self._mark_failed(message, error=exc)
                failed += 1
                continue
            await self._mark_published(message)
            published += 1
        return RabbitOutboxPublishBatchResult(claimed=len(messages), published=published, failed=failed)

    async def _mark_published(self, message: RabbitMQOutboxMessage) -> None:
        message.status = RabbitMQOutboxMessageStatus.PUBLISHED
        message.published_at = utcnow()
        message.next_retry_at = None
        message.locked_at = None
        message.lock_owner = None
        message.last_error_text = None
        await self._commit_message_update("Failed to mark RabbitMQ outbox message as published.")

    async def _mark_failed(self, message: RabbitMQOutboxMessage, *, error: Exception) -> None:
        message.status = RabbitMQOutboxMessageStatus.FAILED
        message.published_at = None
        message.next_retry_at = utcnow() + timedelta(seconds=self._settings.pipeline_broker_retry_backoff_seconds)
        message.locked_at = None
        message.lock_owner = None
        message.last_error_text = _trim_error_text(str(error) or error.__class__.__name__)
        await self._commit_message_update("Failed to mark RabbitMQ outbox message publish failure.")

    async def _commit_message_update(self, failure_message: str) -> None:
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise RabbitOutboxError(failure_message) from exc


async def publish_rabbit_message_direct(
    spec: RabbitMessageSpec,
    *,
    broker: RabbitBrokerProtocol,
    settings: Settings | None = None,
) -> None:
    """Publish one RabbitMQ message directly; use only for relay or documented exceptions."""

    resolved_settings = settings or get_settings()
    async with asyncio.timeout(resolved_settings.pipeline_broker_connection_timeout_seconds):
        _ = await broker.publish(
            spec.payload,
            exchange=spec.exchange,
            routing_key=spec.routing_key,
            persist=True,
            content_type=spec.content_type,
            headers=dict(spec.headers),
            message_id=spec.message_id,
            timestamp=spec.created_at,
            mandatory=True,
        )


async def relay_rabbitmq_outbox_messages_best_effort(
    session: AsyncSession,
    message_ids: Iterable[uuid.UUID],
    *,
    settings: Settings | None = None,
    broker: RabbitBrokerProtocol | None = None,
    logger: logging.Logger | None = None,
) -> RabbitOutboxPublishBatchResult | None:
    """Try to relay committed outbox rows without letting broker failures affect business state."""

    unique_ids = tuple(dict.fromkeys(message_ids))
    if not unique_ids:
        return RabbitOutboxPublishBatchResult(claimed=0, published=0, failed=0)

    try:
        resolved_settings = settings or get_settings()
        resolved_broker = broker or await ensure_pipeline_broker_started(settings=resolved_settings)
        relay = RabbitOutboxRelay(session, broker=resolved_broker, settings=resolved_settings)
        return await relay.publish_ids(unique_ids)
    except Exception:
        if logger is not None:
            logger.exception(
                "rabbitmq_outbox_immediate_relay_failed",
                extra={
                    "event": "rabbitmq_outbox_immediate_relay_failed",
                    "message_ids": [str(message_id) for message_id in unique_ids],
                },
            )
        return None


def outbox_message_from_spec(spec: RabbitMessageSpec) -> RabbitMQOutboxMessage:
    """Build a pending outbox row from a generic RabbitMQ message spec."""

    created_at = spec.created_at
    return RabbitMQOutboxMessage(
        id=outbox_message_id_from_message_id(spec.message_id),
        exchange=_require_text(spec.exchange, field_name="exchange"),
        routing_key=_require_text(spec.routing_key, field_name="routing_key"),
        payload=dict(spec.payload),
        headers=dict(spec.headers),
        content_type=_require_text(spec.content_type, field_name="content_type"),
        message_id=_require_text(spec.message_id, field_name="message_id"),
        event_type=_require_text(spec.event_type, field_name="event_type"),
        aggregate_type=_require_text(spec.aggregate_type, field_name="aggregate_type"),
        aggregate_id=_require_text(str(spec.aggregate_id), field_name="aggregate_id"),
        ordering_key=spec.ordering_key,
        status=RabbitMQOutboxMessageStatus.PENDING,
        attempt_count=0,
        next_retry_at=created_at,
        created_at=created_at,
    )


def message_spec_from_outbox_message(message: RabbitMQOutboxMessage) -> RabbitMessageSpec:
    """Rehydrate a generic RabbitMQ message spec from a durable outbox row."""

    return RabbitMessageSpec(
        exchange=message.exchange,
        routing_key=message.routing_key,
        payload=dict(message.payload),
        headers=cast("HeadersType", dict(message.headers)),
        content_type=message.content_type,
        message_id=message.message_id,
        event_type=message.event_type,
        aggregate_type=message.aggregate_type,
        aggregate_id=message.aggregate_id,
        ordering_key=message.ordering_key,
        created_at=message.created_at,
    )


def outbox_message_id_from_message_id(message_id: str) -> uuid.UUID:
    """Use UUID message ids as row ids; non-UUID producers still get a durable UUID row id."""

    try:
        return uuid.UUID(message_id)
    except ValueError:
        return uuid.uuid7()


def _default_lock_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _require_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RabbitOutboxError(f"RabbitMQ outbox {field_name} must not be blank.")
    return normalized


def _trim_error_text(value: str) -> str:
    return value.strip()[:_MAX_OUTBOX_ERROR_TEXT_LENGTH]


__all__ = [
    "RabbitBrokerProtocol",
    "RabbitMessageSpec",
    "RabbitOutboxError",
    "RabbitOutboxPublishBatchResult",
    "RabbitOutboxRelay",
    "RabbitPublisher",
    "message_spec_from_outbox_message",
    "outbox_message_from_spec",
    "outbox_message_id_from_message_id",
    "publish_rabbit_message_direct",
    "relay_rabbitmq_outbox_messages_best_effort",
]
