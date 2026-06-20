"""Encrypted storage helpers for Telethon ``StringSession`` values."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class TelegramStringSessionSecretError(ValueError):
    """Raised when Telegram StringSession secret material cannot be processed."""


class TelegramStringSessionDecryptError(TelegramStringSessionSecretError):
    """Raised when encrypted Telegram StringSession material cannot be decrypted."""


@dataclass(frozen=True, slots=True)
class TelegramStringSessionCipher:
    """Encrypt and decrypt Telethon ``StringSession`` values with Fernet.

    The operator-provided secret is never used directly as a Fernet key. It is
    normalized, SHA-256 derived, and base64-url encoded so deployments can use
    any high-entropy string rather than managing Fernet key syntax.
    """

    encryption_secret: SecretStr = field(repr=False)
    _fernet: Fernet = field(init=False, repr=False)

    def __post_init__(self) -> None:
        secret = self.encryption_secret.get_secret_value().strip()
        if not secret:
            raise TelegramStringSessionSecretError("telegram_session_encryption_secret must not be blank.")
        object.__setattr__(self, "_fernet", Fernet(_derive_fernet_key(secret)))

    def encrypt(self, string_session: SecretStr) -> SecretStr:
        """Return encrypted, DB-safe ``StringSession`` material."""

        plaintext = string_session.get_secret_value().strip()
        if not plaintext:
            raise TelegramStringSessionSecretError("Telegram StringSession material must not be blank.")
        encrypted = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return SecretStr(encrypted)

    def decrypt(self, encrypted_string_session: SecretStr) -> SecretStr:
        """Return decrypted ``StringSession`` material as a redacted secret value."""

        token = encrypted_string_session.get_secret_value().strip()
        if not token:
            raise TelegramStringSessionDecryptError("Encrypted Telegram StringSession material must not be blank.")
        try:
            plaintext = self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise TelegramStringSessionDecryptError(
                "Encrypted Telegram StringSession material could not be decrypted.",
            ) from exc
        if not plaintext.strip():
            raise TelegramStringSessionDecryptError("Decrypted Telegram StringSession material must not be blank.")
        return SecretStr(plaintext)


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


__all__ = [
    "TelegramStringSessionCipher",
    "TelegramStringSessionDecryptError",
    "TelegramStringSessionSecretError",
]
