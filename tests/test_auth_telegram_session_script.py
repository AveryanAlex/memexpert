"""Unit tests for the DB-backed Telegram StringSession import helper."""

from __future__ import annotations

from argparse import Namespace

import pytest

from scripts import auth_telegram_session


def test_load_string_session_reads_env_without_printing_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "  test-string-session  ")

    secret = auth_telegram_session._load_string_session(
        Namespace(string_session=None, string_session_file=None),
    )

    assert secret.get_secret_value() == "test-string-session"
    captured = capsys.readouterr()
    assert "test-string-session" not in captured.out
    assert "test-string-session" not in captured.err


def test_load_string_session_rejects_multiple_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "test-string-session")

    with pytest.raises(SystemExit) as exc_info:
        _ = auth_telegram_session._load_string_session(
            Namespace(string_session="other-session", string_session_file=None),
        )

    assert exc_info.value.code == auth_telegram_session._EXIT_INVALID_INPUT


def test_load_string_session_rejects_missing_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_STRING_SESSION", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        _ = auth_telegram_session._load_string_session(
            Namespace(string_session=None, string_session_file=None),
        )

    assert exc_info.value.code == auth_telegram_session._EXIT_MISSING_STRING_SESSION
