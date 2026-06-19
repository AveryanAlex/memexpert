"""Logging helpers for the scheduler runtime."""

from __future__ import annotations

import json
import logging
import sys


class _SchedulerStructuredFormatter(logging.Formatter):
    _structured_fields = (
        "event",
        "event_type",
        "job_id",
        "status",
        "duration_seconds",
        "degraded_mode",
        "degraded_component",
        "reason",
        "exception_type",
        "jobs_registered",
        "advisory_lock_enabled",
        "advisory_lock_key",
        "captured_at",
        "public_meme_count",
        "snapshot_count",
        "updated_meme_count",
        "view_name",
        "scanned",
        "updated",
        "failed",
        "skipped",
        "claimed",
        "enqueued",
        "recovered",
        "published",
        "index_sync_unsynced_count",
        "index_sync_failed_count",
        "index_sync_processing_count",
        "index_sync_oldest_lag_seconds",
        "outbox_due_count",
        "outbox_pending_count",
        "outbox_failed_count",
        "outbox_publishing_count",
        "outbox_oldest_due_age_seconds",
        "route_tier",
        "rate_limit_tier",
        "request_id",
        "surface",
        "user_id",
        "source_algorithm",
        "algorithm_version",
        "fallback_reason",
        "payload_key_count",
        "payload_keys",
        "query_present",
        "query_length",
        "scope",
        "language",
        "media_type",
        "include_nsfw",
        "tag_count",
        "collection_count",
        "filter_count",
        "candidate_count",
        "text_candidate_count",
        "semantic_candidate_count",
        "visible_count",
        "result_count",
        "total",
        "embedding_latency_seconds",
        "text_latency_seconds",
        "semantic_latency_seconds",
        "index_latency_seconds",
        "db_latency_seconds",
        "total_latency_seconds",
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
