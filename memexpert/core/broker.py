"""Lazy RabbitMQ/FastStream helpers for the content pipeline runtime."""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlparse, urlunparse

import aio_pika
from aio_pika import ExchangeType
from faststream.rabbit import RabbitBroker

from memexpert.core.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable

DEFAULT_RABBITMQ_CONNECTION_TIMEOUT_SECONDS: Final = 5.0
SUPPORTED_RABBITMQ_SCHEMES: Final[frozenset[str]] = frozenset({"amqp", "amqps"})
_BROKER_TOPOLOGY_NAME_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class PipelineBrokerSettings:
    """Normalized RabbitMQ topology used by the content pipeline runtime."""

    url: str
    exchange: str
    routing_key_prefix: str
    transcode_queue: str
    dead_letter_exchange: str
    dead_letter_queue: str
    retry_max_attempts: int
    retry_backoff_seconds: float
    connection_timeout: float

    @property
    def meme_created_routing_key(self) -> str:
        """Return the routing key used for durable post-upload dispatches."""

        return f"{self.routing_key_prefix}.meme_created"

    @property
    def dead_letter_routing_key(self) -> str:
        """Return the routing key used when retries are exhausted."""

        return f"{self.routing_key_prefix}.dead_letter"


class BrokerConfigurationError(ValueError):
    """Raised when RabbitMQ/FastStream settings cannot produce a broker contract."""


class BrokerConnectionError(RuntimeError):
    """Raised when the RabbitMQ runtime cannot establish a real broker connection."""


_rabbitmq_broker: RabbitBroker | None = None


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        _ = await cast("Awaitable[object]", result)


def _normalize_topology_name(value: str, *, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise BrokerConfigurationError(f"{field_name} must not be blank.")
    if _BROKER_TOPOLOGY_NAME_RE.fullmatch(normalized_value) is None:
        raise BrokerConfigurationError(
            f"{field_name} may contain only letters, numbers, dots, underscores, and hyphens.",
        )
    return normalized_value


def normalize_rabbitmq_url(rabbitmq_url: str) -> str:
    """Normalize and validate RabbitMQ URLs before constructing broker clients."""

    normalized_input = rabbitmq_url.strip()
    if not normalized_input:
        raise BrokerConfigurationError(
            "RabbitMQ URL is required before constructing the pipeline broker.",
        )

    parsed_url = urlparse(normalized_input)
    if parsed_url.scheme not in SUPPORTED_RABBITMQ_SCHEMES:
        raise BrokerConfigurationError("RabbitMQ URL must use amqp:// or amqps://.")

    try:
        port = parsed_url.port
    except ValueError as exc:
        raise BrokerConfigurationError("RabbitMQ URL contains an invalid port.") from exc

    if parsed_url.hostname is None:
        raise BrokerConfigurationError("RabbitMQ URL must include a hostname.")
    if port is not None and port <= 0:
        raise BrokerConfigurationError("RabbitMQ URL port must be greater than zero.")

    return normalized_input


def _redact_rabbitmq_url(rabbitmq_url: str) -> str:
    parsed_url = urlparse(rabbitmq_url)
    hostname = parsed_url.hostname or ""
    port_suffix = f":{parsed_url.port}" if parsed_url.port is not None else ""
    if parsed_url.username is None and parsed_url.password is None:
        netloc = f"{hostname}{port_suffix}"
    else:
        netloc = f"****:****@{hostname}{port_suffix}"
    return urlunparse(parsed_url._replace(netloc=netloc))


def get_pipeline_broker_settings(settings: Settings | None = None) -> PipelineBrokerSettings:
    """Return the normalized RabbitMQ topology for pipeline dispatch and replay."""

    resolved_settings = settings or get_settings()
    return PipelineBrokerSettings(
        url=normalize_rabbitmq_url(resolved_settings.rabbitmq_url),
        exchange=_normalize_topology_name(
            resolved_settings.pipeline_broker_exchange,
            field_name="pipeline_broker_exchange",
        ),
        routing_key_prefix=_normalize_topology_name(
            resolved_settings.pipeline_broker_routing_key_prefix,
            field_name="pipeline_broker_routing_key_prefix",
        ),
        transcode_queue=_normalize_topology_name(
            resolved_settings.pipeline_broker_transcode_queue,
            field_name="pipeline_broker_transcode_queue",
        ),
        dead_letter_exchange=_normalize_topology_name(
            resolved_settings.pipeline_broker_dead_letter_exchange,
            field_name="pipeline_broker_dead_letter_exchange",
        ),
        dead_letter_queue=_normalize_topology_name(
            resolved_settings.pipeline_broker_dead_letter_queue,
            field_name="pipeline_broker_dead_letter_queue",
        ),
        retry_max_attempts=resolved_settings.pipeline_broker_retry_max_attempts,
        retry_backoff_seconds=resolved_settings.pipeline_broker_retry_backoff_seconds,
        connection_timeout=resolved_settings.pipeline_broker_connection_timeout_seconds,
    )


def build_pipeline_broker(settings: Settings | None = None) -> RabbitBroker:
    """Build a lazy FastStream Rabbit broker without performing network I/O."""

    broker_settings = get_pipeline_broker_settings(settings)
    return RabbitBroker(broker_settings.url)


def get_pipeline_broker() -> RabbitBroker:
    """Return the process-wide FastStream broker, creating it lazily."""

    global _rabbitmq_broker
    if _rabbitmq_broker is None:
        _rabbitmq_broker = build_pipeline_broker()
    return _rabbitmq_broker


def is_pipeline_broker_initialized() -> bool:
    """Expose whether the process-wide FastStream broker has been created yet."""

    return _rabbitmq_broker is not None


async def verify_pipeline_broker(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
) -> PipelineBrokerSettings:
    """Verify RabbitMQ connectivity and durable topology declaration with a bounded timeout."""

    broker_settings = get_pipeline_broker_settings(settings)
    resolved_timeout = timeout or broker_settings.connection_timeout
    redacted_url = _redact_rabbitmq_url(broker_settings.url)

    try:
        async with asyncio.timeout(resolved_timeout):
            connection = await aio_pika.connect_robust(
                broker_settings.url,
                timeout=resolved_timeout,
            )
            try:
                channel = await connection.channel()
                exchange = await channel.declare_exchange(
                    broker_settings.exchange,
                    ExchangeType.TOPIC,
                    durable=True,
                )
                dead_letter_exchange = await channel.declare_exchange(
                    broker_settings.dead_letter_exchange,
                    ExchangeType.TOPIC,
                    durable=True,
                )
                transcode_queue = await channel.declare_queue(
                    broker_settings.transcode_queue,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": broker_settings.dead_letter_exchange,
                        "x-dead-letter-routing-key": broker_settings.dead_letter_routing_key,
                    },
                )
                dead_letter_queue = await channel.declare_queue(
                    broker_settings.dead_letter_queue,
                    durable=True,
                )
                _ = await transcode_queue.bind(exchange, routing_key=broker_settings.meme_created_routing_key)
                _ = await dead_letter_queue.bind(
                    dead_letter_exchange,
                    routing_key=broker_settings.dead_letter_routing_key,
                )
            finally:
                await connection.close()
    except TimeoutError as exc:
        raise BrokerConnectionError(
            f"Timed out after {resolved_timeout:.2f}s while connecting to RabbitMQ {redacted_url}.",
        ) from exc
    except BrokerConfigurationError:
        raise
    except Exception as exc:
        raise BrokerConnectionError(f"Unable to verify RabbitMQ broker {redacted_url}: {exc}") from exc

    return broker_settings


async def reset_pipeline_broker_state() -> None:
    """Dispose the cached FastStream broker so test/runtime state cannot leak."""

    global _rabbitmq_broker

    cached_broker = _rabbitmq_broker
    _rabbitmq_broker = None

    if cached_broker is None:
        return

    close_method = getattr(cached_broker, "close", None)
    if close_method is None:
        return

    close_result = cast("object", close_method())
    await _maybe_await(close_result)


__all__ = [
    "BrokerConfigurationError",
    "BrokerConnectionError",
    "DEFAULT_RABBITMQ_CONNECTION_TIMEOUT_SECONDS",
    "PipelineBrokerSettings",
    "build_pipeline_broker",
    "get_pipeline_broker",
    "get_pipeline_broker_settings",
    "is_pipeline_broker_initialized",
    "normalize_rabbitmq_url",
    "reset_pipeline_broker_state",
    "verify_pipeline_broker",
]
