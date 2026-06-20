"""Tests for encrypted Telethon StringSession storage helpers."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from memexpert.crawlers.telegram.session_crypto import (
    TelegramStringSessionCipher,
    TelegramStringSessionDecryptError,
    TelegramStringSessionSecretError,
)


def test_telegram_string_session_cipher_round_trips_and_redacts_material() -> None:
    cipher = TelegramStringSessionCipher(SecretStr("test-encryption-secret"))
    string_session = SecretStr("test-telethon-string-session")

    encrypted = cipher.encrypt(string_session)
    decrypted = cipher.decrypt(encrypted)

    assert encrypted.get_secret_value() != string_session.get_secret_value()
    assert decrypted.get_secret_value() == string_session.get_secret_value()
    assert string_session.get_secret_value() not in repr(encrypted)
    assert string_session.get_secret_value() not in repr(decrypted)


def test_telegram_string_session_cipher_rejects_blank_inputs() -> None:
    with pytest.raises(TelegramStringSessionSecretError):
        _ = TelegramStringSessionCipher(SecretStr("  "))

    cipher = TelegramStringSessionCipher(SecretStr("test-encryption-secret"))
    with pytest.raises(TelegramStringSessionSecretError):
        _ = cipher.encrypt(SecretStr("  "))
    with pytest.raises(TelegramStringSessionDecryptError):
        _ = cipher.decrypt(SecretStr("  "))


def test_telegram_string_session_cipher_rejects_wrong_secret() -> None:
    encrypted = TelegramStringSessionCipher(SecretStr("first-secret")).encrypt(
        SecretStr("test-telethon-string-session"),
    )

    with pytest.raises(TelegramStringSessionDecryptError):
        _ = TelegramStringSessionCipher(SecretStr("second-secret")).decrypt(encrypted)
