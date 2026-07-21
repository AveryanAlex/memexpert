"""Structured logging coverage for the FastAPI process."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from memexpert.api.logging import build_uvicorn_logging_config, configure_api_logging

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture


def test_api_logging_emits_privacy_bounded_recommendation_info(
    capsys: CaptureFixture[str],
) -> None:
    application_logger = logging.getLogger("memexpert")
    stage_logger = logging.getLogger("memexpert.services.recommendations.service")
    original_handlers = list(application_logger.handlers)
    original_level = application_logger.level
    original_propagate = application_logger.propagate
    original_stage_handlers = list(stage_logger.handlers)
    original_stage_level = stage_logger.level
    original_stage_propagate = stage_logger.propagate
    original_stage_disabled = stage_logger.disabled
    application_logger.handlers.clear()
    application_logger.setLevel(logging.NOTSET)
    application_logger.propagate = True
    stage_logger.handlers.clear()
    stage_logger.setLevel(logging.NOTSET)
    stage_logger.propagate = True
    stage_logger.disabled = False

    try:
        configure_api_logging()
        configure_api_logging()

        assert len(application_logger.handlers) == 1
        handler = application_logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout
        assert application_logger.level == logging.INFO
        assert application_logger.propagate is False

        stage_logger.info(
            "private argument must not be interpolated: %s",
            "raw-query-secret",
            extra={
                "event": "recommendation_candidate_generation_completed",
                "request_id": "request-1",
                "surface": "web_home",
                "algorithm_version": "personalized_v2",
                "profile_version": "profile-4",
                "candidate_source_counts": {"trending": 80, "exploration": 40},
                "candidate_union_count": 120,
                "post_filter_count": 90,
                "qdrant_latency_seconds": 0.012,
                "postgres_candidate_latency_seconds": 0.34,
                "total_latency_seconds": 0.5,
                "raw_query": "raw-query-secret",
                "attribution_token": "signed-token-secret",
                "cursor": "signed-cursor-secret",
                "collection_ids": ["private-collection"],
                "profile_vector": [0.1, 0.2],
            },
        )

        captured = capsys.readouterr()
        assert captured.err == ""
        assert "raw-query-secret" not in captured.out
        assert "signed-token-secret" not in captured.out
        assert "signed-cursor-secret" not in captured.out
        assert "private-collection" not in captured.out
        payload = json.loads(captured.out)
        assert payload == {
            "level": "INFO",
            "logger": "memexpert.services.recommendations.service",
            "message": "recommendation_candidate_generation_completed",
            "event": "recommendation_candidate_generation_completed",
            "request_id": "request-1",
            "surface": "web_home",
            "algorithm_version": "personalized_v2",
            "profile_version": "profile-4",
            "candidate_source_counts": {"trending": 80, "exploration": 40},
            "candidate_union_count": 120,
            "post_filter_count": 90,
            "qdrant_latency_seconds": 0.012,
            "postgres_candidate_latency_seconds": 0.34,
            "total_latency_seconds": 0.5,
        }

        stage_logger.info("unstructured info remains suppressed")
        assert capsys.readouterr().out == ""
    finally:
        for handler in application_logger.handlers:
            if handler not in original_handlers:
                handler.close()
        application_logger.handlers[:] = original_handlers
        application_logger.setLevel(original_level)
        application_logger.propagate = original_propagate
        stage_logger.handlers[:] = original_stage_handlers
        stage_logger.setLevel(original_stage_level)
        stage_logger.propagate = original_stage_propagate
        stage_logger.disabled = original_stage_disabled


def test_api_logging_retains_warning_without_exception_details(
    capsys: CaptureFixture[str],
) -> None:
    application_logger = logging.getLogger("memexpert")
    stage_logger = logging.getLogger("memexpert.services.recommendations.service")
    original_handlers = list(application_logger.handlers)
    original_level = application_logger.level
    original_propagate = application_logger.propagate
    original_stage_handlers = list(stage_logger.handlers)
    original_stage_level = stage_logger.level
    original_stage_propagate = stage_logger.propagate
    original_stage_disabled = stage_logger.disabled
    application_logger.handlers.clear()
    stage_logger.handlers.clear()
    stage_logger.setLevel(logging.NOTSET)
    stage_logger.propagate = True
    stage_logger.disabled = False

    try:
        configure_api_logging()
        try:
            raise RuntimeError("provider response secret")
        except RuntimeError:
            stage_logger.exception("recommendation provider failed")

        captured = capsys.readouterr()
        assert captured.err == ""
        assert "provider response secret" not in captured.out
        assert json.loads(captured.out) == {
            "level": "ERROR",
            "logger": "memexpert.services.recommendations.service",
            "message": "recommendation provider failed",
            "exception_type": "RuntimeError",
        }
    finally:
        for handler in application_logger.handlers:
            if handler not in original_handlers:
                handler.close()
        application_logger.handlers[:] = original_handlers
        application_logger.setLevel(original_level)
        application_logger.propagate = original_propagate
        stage_logger.handlers[:] = original_stage_handlers
        stage_logger.setLevel(original_stage_level)
        stage_logger.propagate = original_stage_propagate
        stage_logger.disabled = original_stage_disabled


def test_api_logging_does_not_replace_uvicorn_loggers() -> None:
    application_logger = logging.getLogger("memexpert")
    original_application_handlers = list(application_logger.handlers)
    original_application_level = application_logger.level
    original_application_propagate = application_logger.propagate
    access_logger = logging.getLogger("uvicorn.access")
    error_logger = logging.getLogger("uvicorn.error")
    access_state = (list(access_logger.handlers), access_logger.level, access_logger.propagate)
    error_state = (list(error_logger.handlers), error_logger.level, error_logger.propagate)
    application_logger.handlers.clear()

    try:
        configure_api_logging()

        assert (list(access_logger.handlers), access_logger.level, access_logger.propagate) == access_state
        assert (list(error_logger.handlers), error_logger.level, error_logger.propagate) == error_state
    finally:
        for handler in application_logger.handlers:
            if handler not in original_application_handlers:
                handler.close()
        application_logger.handlers[:] = original_application_handlers
        application_logger.setLevel(original_application_level)
        application_logger.propagate = original_application_propagate


def test_uvicorn_logging_bootstrap_preserves_structured_application_and_server_logs() -> None:
    script = textwrap.dedent(
        """
        import json
        import logging
        import sys

        import uvicorn

        from memexpert.api.logging import build_uvicorn_logging_config

        uvicorn.Config(
            "memexpert.api.app:create_app",
            factory=True,
            log_config=build_uvicorn_logging_config(),
        )

        application_logger = logging.getLogger("memexpert.services.recommendations.service")
        application_logger.info(
            "private argument must not be interpolated: %s",
            "raw-query-secret",
            extra={
                "event": "recommendation_home_page_completed",
                "request_id": "request-uvicorn",
                "total_latency_seconds": 0.25,
                "raw_query": "raw-query-secret",
            },
        )
        logging.getLogger("uvicorn.error").info("uvicorn-error-sentinel")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            "/health",
            "1.1",
            200,
        )

        application_root = logging.getLogger("memexpert")
        application_handler = application_root.handlers[0]
        state = {
            "application_handler_count": len(application_root.handlers),
            "application_handler_closed": getattr(application_handler, "_closed", None),
            "application_handler_name": application_handler.get_name(),
            "access_handler_count": len(logging.getLogger("uvicorn.access").handlers),
            "error_effective_level": logging.getLogger("uvicorn.error").getEffectiveLevel(),
        }
        print("STATE:" + json.dumps(state), file=sys.stderr)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "raw-query-secret" not in completed.stdout
    stdout_lines = completed.stdout.splitlines()
    payload = json.loads(stdout_lines[0])
    assert payload == {
        "level": "INFO",
        "logger": "memexpert.services.recommendations.service",
        "message": "recommendation_home_page_completed",
        "event": "recommendation_home_page_completed",
        "request_id": "request-uvicorn",
        "total_latency_seconds": 0.25,
    }
    assert any('GET /health HTTP/1.1" 200' in line for line in stdout_lines[1:])
    assert "uvicorn-error-sentinel" in completed.stderr
    state_line = next(line for line in completed.stderr.splitlines() if line.startswith("STATE:"))
    assert json.loads(state_line.removeprefix("STATE:")) == {
        "application_handler_count": 1,
        "application_handler_closed": False,
        "application_handler_name": "memexpert-api-structured",
        "access_handler_count": 1,
        "error_effective_level": logging.INFO,
    }


def test_uvicorn_logging_config_keeps_default_access_and_error_handlers() -> None:
    config = build_uvicorn_logging_config()

    assert config["disable_existing_loggers"] is False
    assert config["loggers"]["uvicorn.access"] == {
        "handlers": ["access"],
        "level": "INFO",
        "propagate": False,
    }
    assert config["loggers"]["uvicorn.error"] == {"level": "INFO"}
    assert config["loggers"]["memexpert"] == {
        "handlers": ["memexpert-api-structured"],
        "level": "INFO",
        "propagate": False,
    }
