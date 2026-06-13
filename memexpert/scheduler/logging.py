"""Logging helpers for the scheduler runtime."""

from __future__ import annotations

import json
import logging
import sys


class _SchedulerStructuredFormatter(logging.Formatter):
    _structured_fields = (
        "event",
        "job_id",
        "duration_seconds",
        "jobs_registered",
        "advisory_lock_enabled",
        "advisory_lock_key",
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


def configure_scheduler_logging() -> None:
    """Ensure the scheduler emits structured logs by default."""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_SchedulerStructuredFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


__all__ = ["configure_scheduler_logging"]
