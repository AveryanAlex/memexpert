"""Privacy-bounded structured logging for the API process."""

from __future__ import annotations

import json
import logging
import sys
from copy import deepcopy
from typing import Any, Final

_APPLICATION_LOGGER_NAME: Final = "memexpert"
_HANDLER_NAME: Final = "memexpert-api-structured"
_FORMATTER_NAME: Final = "memexpert-api-structured"
_FILTER_NAME: Final = "memexpert-api-application"


class _ApiStructuredFormatter(logging.Formatter):
    """Serialize only the operational fields approved for API logs."""

    _structured_fields = (
        "event",
        "event_type",
        "request_id",
        "feed_session_id",
        "surface",
        "source_algorithm",
        "algorithm_version",
        "configured_algorithm_version",
        "profile_version",
        "cache_status",
        "page_mode",
        "degraded_mode",
        "degraded_component",
        "reason",
        "fallback_reason",
        "fallback_category",
        "exception_type",
        "error_code",
        "timeout_seconds",
        "route_tier",
        "rate_limit_tier",
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
        "limit",
        "offset",
        "candidate_count",
        "candidate_source_counts",
        "candidate_union_count",
        "post_filter_count",
        "filtered_ratio",
        "rerank_count",
        "pool_count",
        "text_candidate_count",
        "semantic_candidate_count",
        "visible_count",
        "result_count",
        "returned_count",
        "scanned_count",
        "selected_count",
        "source_count",
        "total",
        "cold_start",
        "qdrant_degraded",
        "has_more",
        "next_index",
        "embedding_latency_seconds",
        "text_latency_seconds",
        "semantic_latency_seconds",
        "index_latency_seconds",
        "db_latency_seconds",
        "qdrant_latency_seconds",
        "postgres_candidate_latency_seconds",
        "fusion_latency_seconds",
        "filter_feature_latency_seconds",
        "ranking_diversity_latency_seconds",
        "redis_preflight_latency_seconds",
        "redis_pool_latency_seconds",
        "authorization_latency_seconds",
        "hydration_latency_seconds",
        "total_latency_seconds",
    )

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            # Structured events use their fixed event name. This deliberately
            # avoids interpolating arbitrary logging arguments into API output.
            "message": event if isinstance(event, str) else str(record.msg),
        }

        for field_name in self._structured_fields:
            field_value = getattr(record, field_name, None)
            if field_value is not None:
                payload[field_name] = field_value

        if record.exc_info and "exception_type" not in payload:
            exception = record.exc_info[1]
            if exception is not None:
                payload["exception_type"] = type(exception).__name__

        return json.dumps(payload, default=_safe_json_default)


class _ApiApplicationLogFilter(logging.Filter):
    """Admit structured INFO events and retain all warning/error records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or isinstance(getattr(record, "event", None), str)


def _safe_json_default(value: object) -> str:
    """Avoid serializing arbitrary object representations into operational logs."""

    return f"<{type(value).__name__}>"


def configure_api_logging() -> None:
    """Configure application logging for runtimes that already own logging."""

    application_logger = logging.getLogger(_APPLICATION_LOGGER_NAME)
    if not any(handler.get_name() == _HANDLER_NAME for handler in application_logger.handlers):
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.set_name(_HANDLER_NAME)
        handler.setLevel(logging.INFO)
        handler.addFilter(_ApiApplicationLogFilter())
        handler.setFormatter(_ApiStructuredFormatter())
        application_logger.addHandler(handler)

    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False


def build_uvicorn_logging_config() -> dict[str, Any]:
    """Extend Uvicorn's defaults with the structured application handler."""

    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config.setdefault("formatters", {})[_FORMATTER_NAME] = {"()": _ApiStructuredFormatter}
    config.setdefault("filters", {})[_FILTER_NAME] = {"()": _ApiApplicationLogFilter}
    config.setdefault("handlers", {})[_HANDLER_NAME] = {
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "level": "INFO",
        "formatter": _FORMATTER_NAME,
        "filters": [_FILTER_NAME],
    }
    config.setdefault("loggers", {})[_APPLICATION_LOGGER_NAME] = {
        "handlers": [_HANDLER_NAME],
        "level": "INFO",
        "propagate": False,
    }
    return config


__all__ = ["build_uvicorn_logging_config", "configure_api_logging"]
