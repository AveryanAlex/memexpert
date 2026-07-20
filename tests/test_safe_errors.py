"""Tests for bounded operator-visible error redaction."""

from __future__ import annotations

import pytest

from memexpert.services.safe_errors import sanitize_operational_error


@pytest.mark.parametrize(
    ("unsafe", "secret"),
    [
        (
            "download failed for pipeline/originals/private/source.webm",
            "pipeline/originals/private/source.webm",
        ),
        (
            "GET https://alice:password@example.test/object?X-Amz-Signature=secret",
            "X-Amz-Signature",
        ),
        ("Authorization: Bearer ey.secret.token", "ey.secret.token"),
        ("provider payload={\"prompt\":\"private meme text\"}", "private meme text"),
        ("provider rejected {\"input\":\"private catalog text\"}", "private catalog text"),
        ("api_key=sk-private-value request failed", "sk-private-value"),
    ],
)
def test_sanitize_operational_error_redacts_sensitive_diagnostics(unsafe: str, secret: str) -> None:
    sanitized = sanitize_operational_error(unsafe)

    assert sanitized is not None
    assert secret not in sanitized
    assert "redacted" in sanitized


def test_sanitize_operational_error_preserves_bounded_safe_context() -> None:
    sanitized = sanitize_operational_error(RuntimeError("  provider timeout\nwhile retrying  "), max_length=24)

    assert sanitized == "provider timeout while r"


def test_sanitize_operational_error_handles_empty_values() -> None:
    assert sanitize_operational_error(None) is None
    assert sanitize_operational_error(" \n ") is None
