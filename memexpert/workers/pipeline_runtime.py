# ruff: noqa: TC002
"""FastStream RabbitMQ runtime for the stub transcode worker."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast

from faststream import AckPolicy
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage
from pydantic import ValidationError

from memexpert.core.broker import PipelineBrokerSettings, get_pipeline_broker, get_pipeline_broker_settings
from memexpert.core.config import Settings, get_settings
from memexpert.core.database import AsyncSessionFactory, get_async_session_factory
from memexpert.models.enums import ContentPipelineStage
from memexpert.schemas.content_pipeline import ContentPipelineDispatchEvent
from memexpert.services import ContentPipelineService

PIPELINE_REASON_FORCED_TRANSCODE_FAILURE = "forced_transcode_failure"
PIPELINE_REASON_MALFORMED_EVENT = "malformed_dispatch_event"
PIPELINE_REASON_TRANSCODE_FAILED = "transcode_stage_failed"
PIPELINE_REASON_UNSUPPORTED_STAGE = "unsupported_stage"

type DeadLetterPayload = str | bytes | bytearray | int | float | bool | None


class RabbitMessageLike(Protocol):
    """Minimal RabbitMQ message surface used by the runtime handler and tests."""

    headers: dict[str, Any]
    content_type: str | None
    message_id: str | None

    async def ack(self, multiple: bool = False) -> None: ...

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None: ...

    async def reject(self, requeue: bool = False) -> None: ...


class ForcedTranscodeFailure(RuntimeError):
    """Raised when the dev/test-only failure-injection knob forces one transcode attempt to fail."""


@dataclass(slots=True)
class PipelineRuntime:
    """RabbitMQ-backed runtime that consumes the stub transcode stage."""

    settings: Settings
    broker: RabbitBroker
    session_factory: AsyncSessionFactory
    broker_settings: PipelineBrokerSettings
    pipeline_exchange: RabbitExchange
    retry_exchange: RabbitExchange
    dead_letter_exchange: RabbitExchange
    transcode_queue: RabbitQueue
    retry_queue: RabbitQueue
    dead_letter_queue: RabbitQueue

    async def declare_topology(self) -> None:
        """Declare the transcode queue, retry queue, and DLQ topology explicitly."""

        exchange = await self.broker.declare_exchange(self.pipeline_exchange)
        retry_exchange = await self.broker.declare_exchange(self.retry_exchange)
        dead_letter_exchange = await self.broker.declare_exchange(self.dead_letter_exchange)
        transcode_queue = await self.broker.declare_queue(self.transcode_queue)
        retry_queue = await self.broker.declare_queue(self.retry_queue)
        dead_letter_queue = await self.broker.declare_queue(self.dead_letter_queue)

        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.meme_created_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.stage_replay_routing_key)
        _ = await transcode_queue.bind(exchange, routing_key=self.broker_settings.transcode_retry_routing_key)
        _ = await retry_queue.bind(retry_exchange, routing_key=self.broker_settings.retry_routing_key)
        _ = await dead_letter_queue.bind(
            dead_letter_exchange,
            routing_key=self.broker_settings.dead_letter_routing_key,
        )

    async def handle_transcode_message(self, payload: object, message: RabbitMessageLike) -> None:
        """Consume one transcode-stage dispatch, persisting durable stage truth as it changes."""

        dispatch_event = self._validate_event_payload(payload)
        if dispatch_event is None:
            await self._record_malformed_event_failure(payload)
            await self._dead_letter_or_requeue(
                self._coerce_dead_letter_payload(payload),
                message=message,
                normalized_reason=PIPELINE_REASON_MALFORMED_EVENT,
            )
            return

        effective_attempt = self._effective_attempt(dispatch_event, message)
        if dispatch_event.stage is not ContentPipelineStage.TRANSCODE:
            await self._record_terminal_failure(
                dispatch_event,
                attempt=effective_attempt,
                normalized_reason=PIPELINE_REASON_UNSUPPORTED_STAGE,
                last_error_text=(
                    "The stub runtime only handles the transcode stage, "
                    f"but received {dispatch_event.stage.value!r}."
                ),
            )
            await self._dead_letter_or_requeue(
                self._coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
                message=message,
                normalized_reason=PIPELINE_REASON_UNSUPPORTED_STAGE,
            )
            return

        async with self.session_factory() as session:
            service = ContentPipelineService(session, settings=self.settings)
            try:
                await service.mark_stage_processing(
                    meme_file_id=dispatch_event.meme_file_id,
                    stage=dispatch_event.stage,
                    attempt=effective_attempt,
                    event_id=dispatch_event.event_id,
                )
                self._maybe_force_transcode_failure(dispatch_event)
                await service.mark_stage_succeeded(
                    meme_file_id=dispatch_event.meme_file_id,
                    stage=dispatch_event.stage,
                    attempt=effective_attempt,
                    event_id=dispatch_event.event_id,
                )
            except Exception as exc:
                retryable = effective_attempt < self.broker_settings.retry_max_attempts
                with suppress(Exception):
                    await service.mark_stage_failed(
                        meme_file_id=dispatch_event.meme_file_id,
                        stage=dispatch_event.stage,
                        attempt=effective_attempt,
                        event_id=dispatch_event.event_id,
                        normalized_reason=self._normalize_failure_reason(exc),
                        last_error_text=self._render_error_text(exc),
                        retryable=retryable,
                    )

                if retryable:
                    await message.reject(requeue=False)
                    return

                await self._dead_letter_or_requeue(
                    self._coerce_dead_letter_payload(dispatch_event.model_dump(mode="json")),
                    message=message,
                    normalized_reason=self._normalize_failure_reason(exc),
                )
                return

        await message.ack()

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Start the FastStream broker, declare topology, and block until shutdown."""

        resolved_stop_event = stop_event or asyncio.Event()
        await self.broker.start()
        try:
            await self.declare_topology()
            await resolved_stop_event.wait()
        finally:
            await self.broker.stop()

    def _maybe_force_transcode_failure(self, dispatch_event: ContentPipelineDispatchEvent) -> None:
        forced_target = self.settings.pipeline_worker_fail_transcode_for_meme_file_id
        if forced_target is None:
            return
        if forced_target == str(dispatch_event.meme_file_id):
            raise ForcedTranscodeFailure(
                "Forced transcode failure requested by pipeline_worker_fail_transcode_for_meme_file_id.",
            )

    @staticmethod
    def _validate_event_payload(payload: object) -> ContentPipelineDispatchEvent | None:
        try:
            return ContentPipelineDispatchEvent.model_validate(payload)
        except ValidationError:
            return None

    def _effective_attempt(
        self,
        dispatch_event: ContentPipelineDispatchEvent,
        message: RabbitMessageLike,
    ) -> int:
        retry_count = self._retry_cycle_count(message.headers)
        return max(dispatch_event.attempt + retry_count, 1)

    def _retry_cycle_count(self, headers: dict[str, Any]) -> int:
        raw_x_death = headers.get("x-death")
        if not isinstance(raw_x_death, list):
            return 0

        for death_entry in raw_x_death:
            if not isinstance(death_entry, dict):
                continue
            if death_entry.get("queue") != self.broker_settings.retry_queue:
                continue
            if death_entry.get("reason") != "expired":
                continue

            raw_count = death_entry.get("count")
            if isinstance(raw_count, int):
                return max(raw_count, 0)
            if isinstance(raw_count, float):
                return max(int(raw_count), 0)
            if isinstance(raw_count, str):
                try:
                    return max(int(raw_count), 0)
                except ValueError:
                    return 0
            return 0

        return 0

    async def _record_malformed_event_failure(self, payload: object) -> None:
        reference = self._extract_event_reference(payload)
        if reference is None:
            return

        meme_file_id, stage, attempt, event_id = reference
        async with self.session_factory() as session:
            service = ContentPipelineService(session, settings=self.settings)
            try:
                await service.mark_stage_failed(
                    meme_file_id=meme_file_id,
                    stage=stage,
                    attempt=attempt,
                    event_id=event_id,
                    normalized_reason=PIPELINE_REASON_MALFORMED_EVENT,
                    last_error_text="Worker received a malformed content-pipeline dispatch payload.",
                    retryable=False,
                )
            except Exception:
                return

    async def _record_terminal_failure(
        self,
        dispatch_event: ContentPipelineDispatchEvent,
        *,
        attempt: int,
        normalized_reason: str,
        last_error_text: str,
    ) -> None:
        async with self.session_factory() as session:
            service = ContentPipelineService(session, settings=self.settings)
            try:
                await service.mark_stage_failed(
                    meme_file_id=dispatch_event.meme_file_id,
                    stage=dispatch_event.stage,
                    attempt=attempt,
                    event_id=dispatch_event.event_id,
                    normalized_reason=normalized_reason,
                    last_error_text=last_error_text,
                    retryable=False,
                )
            except Exception:
                return

    async def _dead_letter_or_requeue(
        self,
        payload: DeadLetterPayload,
        *,
        message: RabbitMessageLike,
        normalized_reason: str,
    ) -> None:
        try:
            _ = await self.broker.publish(
                payload,
                exchange=self.dead_letter_exchange,
                routing_key=self.broker_settings.dead_letter_routing_key,
                persist=True,
                content_type=message.content_type,
                headers={
                    **message.headers,
                    "x-memexpert-failure-reason": normalized_reason,
                },
                message_id=message.message_id,
                mandatory=True,
            )
        except Exception:
            await message.nack(requeue=True)
            return

        await message.ack()

    @staticmethod
    def _coerce_dead_letter_payload(payload: object) -> DeadLetterPayload:
        if payload is None:
            return None
        if isinstance(payload, (str, bytes, bytearray, int, float, bool)):
            return payload
        if isinstance(payload, dict):
            return json.dumps(payload, sort_keys=True)
        return str(payload)

    @staticmethod
    def _normalize_failure_reason(exc: Exception) -> str:
        if isinstance(exc, ForcedTranscodeFailure):
            return PIPELINE_REASON_FORCED_TRANSCODE_FAILURE
        return PIPELINE_REASON_TRANSCODE_FAILED

    @staticmethod
    def _render_error_text(exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            return message
        return exc.__class__.__name__

    @staticmethod
    def _extract_event_reference(
        payload: object,
    ) -> tuple[uuid.UUID, ContentPipelineStage, int, uuid.UUID] | None:
        if not isinstance(payload, dict):
            return None

        raw_meme_file_id = payload.get("meme_file_id")
        raw_stage = payload.get("stage")
        if not isinstance(raw_meme_file_id, str) or not isinstance(raw_stage, str):
            return None

        try:
            meme_file_id = uuid.UUID(raw_meme_file_id)
            stage = ContentPipelineStage(raw_stage)
        except (ValueError, TypeError):
            return None

        raw_attempt = payload.get("attempt")
        attempt = raw_attempt if isinstance(raw_attempt, int) and raw_attempt >= 1 else 1

        raw_event_id = payload.get("event_id")
        try:
            event_id = uuid.UUID(raw_event_id) if isinstance(raw_event_id, str) else uuid.uuid7()
        except ValueError:
            event_id = uuid.uuid7()

        return meme_file_id, stage, attempt, event_id


def _build_pipeline_exchange(broker_settings: PipelineBrokerSettings) -> RabbitExchange:
    return RabbitExchange(
        broker_settings.exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )


def _build_retry_exchange(broker_settings: PipelineBrokerSettings) -> RabbitExchange:
    return RabbitExchange(
        broker_settings.retry_exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )


def _build_dead_letter_exchange(broker_settings: PipelineBrokerSettings) -> RabbitExchange:
    return RabbitExchange(
        broker_settings.dead_letter_exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )


def _build_transcode_queue(broker_settings: PipelineBrokerSettings) -> RabbitQueue:
    return RabbitQueue(
        broker_settings.transcode_queue,
        durable=True,
        routing_key=broker_settings.meme_created_routing_key,
        arguments={
            "x-dead-letter-exchange": broker_settings.retry_exchange,
            "x-dead-letter-routing-key": broker_settings.retry_routing_key,
        },
    )


def _build_retry_queue(broker_settings: PipelineBrokerSettings) -> RabbitQueue:
    return RabbitQueue(
        broker_settings.retry_queue,
        durable=True,
        arguments={
            "x-message-ttl": broker_settings.retry_backoff_milliseconds,
            "x-dead-letter-exchange": broker_settings.exchange,
            "x-dead-letter-routing-key": broker_settings.transcode_retry_routing_key,
        },
    )


def _build_dead_letter_queue(broker_settings: PipelineBrokerSettings) -> RabbitQueue:
    return RabbitQueue(
        broker_settings.dead_letter_queue,
        durable=True,
    )


def build_pipeline_runtime(
    *,
    settings: Settings | None = None,
    broker: RabbitBroker | None = None,
    session_factory: AsyncSessionFactory | None = None,
) -> PipelineRuntime:
    """Build the RabbitMQ transcode runtime and register its FastStream subscriber."""

    resolved_settings = settings or get_settings()
    resolved_broker_settings = get_pipeline_broker_settings(resolved_settings)
    resolved_broker = broker or get_pipeline_broker()
    resolved_session_factory = session_factory or get_async_session_factory()

    runtime = PipelineRuntime(
        settings=resolved_settings,
        broker=resolved_broker,
        session_factory=resolved_session_factory,
        broker_settings=resolved_broker_settings,
        pipeline_exchange=_build_pipeline_exchange(resolved_broker_settings),
        retry_exchange=_build_retry_exchange(resolved_broker_settings),
        dead_letter_exchange=_build_dead_letter_exchange(resolved_broker_settings),
        transcode_queue=_build_transcode_queue(resolved_broker_settings),
        retry_queue=_build_retry_queue(resolved_broker_settings),
        dead_letter_queue=_build_dead_letter_queue(resolved_broker_settings),
    )

    @resolved_broker.subscriber(
        runtime.transcode_queue,
        runtime.pipeline_exchange,
        ack_policy=AckPolicy.MANUAL,
    )
    async def _consume_transcode(payload: object, message: RabbitMessage) -> None:
        rabbit_message = cast("RabbitMessageLike", cast("object", message))
        await runtime.handle_transcode_message(payload, rabbit_message)

    return runtime


async def run_pipeline_runtime(*, settings: Settings | None = None) -> None:
    """Start the real RabbitMQ-backed content-pipeline worker runtime."""

    runtime = build_pipeline_runtime(settings=settings)
    await runtime.run()


__all__ = [
    "PIPELINE_REASON_FORCED_TRANSCODE_FAILURE",
    "PIPELINE_REASON_MALFORMED_EVENT",
    "PIPELINE_REASON_TRANSCODE_FAILED",
    "PIPELINE_REASON_UNSUPPORTED_STAGE",
    "ForcedTranscodeFailure",
    "PipelineRuntime",
    "RabbitMessageLike",
    "build_pipeline_runtime",
    "run_pipeline_runtime",
]
