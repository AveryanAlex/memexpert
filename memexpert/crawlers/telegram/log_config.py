"""Logging helpers for the Telegram crawler process runtime."""

from __future__ import annotations

import json
import logging
import sys


class _TelegramCrawlerStructuredFormatter(logging.Formatter):
    _structured_fields = (
        "event",
        "signal",
        "catchup_reports",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for field_name in self._structured_fields:
            field_value = getattr(record, field_name, None)
            if field_value is not None:
                payload[field_name] = field_value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_telegram_crawler_logging() -> None:
    """Ensure the crawler process emits structured logs by default."""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_TelegramCrawlerStructuredFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


__all__ = ["configure_telegram_crawler_logging"]
