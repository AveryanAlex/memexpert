"""Import the legacy RabbitMQ pipeline DLQ into the durable PostgreSQL ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING

import aio_pika

from memexpert.core.config import get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.services.pipeline_reliability import record_pipeline_dead_letter

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memexpert-import-pipeline-dlq", description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=10_000,
        help="maximum messages to import before stopping (default: 10000)",
    )
    parser.add_argument(
        "--queue",
        default=None,
        help="legacy queue name; defaults to PIPELINE_BROKER_DEAD_LETTER_QUEUE",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2

    settings = get_settings()
    queue_name = (args.queue or settings.pipeline_broker_dead_letter_queue).strip()
    if not queue_name:
        print("--queue must not be blank", file=sys.stderr)
        return 2

    session_factory = get_async_session_factory()
    imported = 0
    connection = await aio_pika.connect_robust(
        settings.rabbitmq_url,
        timeout=settings.pipeline_broker_connection_timeout_seconds,
    )
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True, passive=True)
        while imported < args.limit:
            message = await queue.get(fail=False)
            if message is None:
                break
            normalized_reason = str(
                (message.headers or {}).get("x-memexpert-failure-reason") or "legacy_dead_letter_import"
            )
            try:
                _ = await record_pipeline_dead_letter(
                    session_factory,
                    payload=_decode_body(message.body),
                    headers=dict(message.headers or {}),
                    broker_message_id=message.message_id,
                    normalized_reason=normalized_reason,
                )
            except Exception:
                await message.nack(requeue=True)
                raise
            await message.ack()
            imported += 1

    print(f"legacy pipeline DLQ import complete; queue={queue_name} imported={imported}")
    return 0


def _decode_body(body: bytes) -> object:
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
