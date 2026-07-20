"""Conservative redaction for operator-visible asynchronous failure text."""

from __future__ import annotations

import re
from typing import Final

_URL_RE: Final = re.compile(r"(?i)\b(?:https?|s3)://[^\s<>]+")
_OBJECT_KEY_RE: Final = re.compile(
    r"(?i)(?<![\w.-])(?:pipeline|uploads?|media)/"
    r"(?:originals?|derived|temporary|temp|generations?)/[^\s,;<>\]\[{}()]+"
)
_BEARER_RE: Final = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE: Final = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|authorization|credential|password|secret|signature|token)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_PROVIDER_PAYLOAD_RE: Final = re.compile(
    r"(?is)\b(provider[_ -]?(?:payload|response)|payload|response[_ -]?body)\s*[:=]\s*"
    r"(?:\{.*\}|\[.*\]|.+)$"
)
_STRUCTURED_PAYLOAD_RE: Final = re.compile(r"(?s)(?:\{.*\}|\[.*\])")


def sanitize_operational_error(value: BaseException | str | None, *, max_length: int = 2000) -> str | None:
    """Return bounded diagnostic text without storage identifiers or credentials."""

    if value is None:
        return None
    rendered = str(value)
    rendered = _PROVIDER_PAYLOAD_RE.sub("provider_payload=<redacted>", rendered)
    rendered = _STRUCTURED_PAYLOAD_RE.sub("<redacted-payload>", rendered)
    rendered = _URL_RE.sub("<redacted-url>", rendered)
    rendered = _OBJECT_KEY_RE.sub("<redacted-object-key>", rendered)
    rendered = _BEARER_RE.sub("Bearer <redacted>", rendered)
    rendered = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", rendered)
    normalized = " ".join(rendered.split())
    return normalized[: max(max_length, 0)] or None


__all__ = ["sanitize_operational_error"]
