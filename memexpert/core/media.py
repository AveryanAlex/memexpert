"""Compatibility media exports that remain safe for API imports.

Use :mod:`memexpert.media.contracts` for import-safe contracts and
:mod:`memexpert.media.inspect` for the worker-only implementation.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from memexpert.media.contracts import (
    CommandResult,
    MediaCommandRunner,
    MediaProcessingError,
    MediaTimeoutError,
    MediaValidationError,
    NormalizedMediaResult,
    PipelineMediaProcessorProtocol,
    UploadMediaDetails,
)

_HEAVY_EXPORTS = frozenset({"PipelineMediaProcessor", "SubprocessMediaCommandRunner"})


def __getattr__(name: str) -> Any:
    if name not in _HEAVY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("memexpert.media.inspect"), name)
    globals()[name] = value
    return value


__all__ = [
    "CommandResult",
    "MediaCommandRunner",
    "MediaProcessingError",
    "MediaTimeoutError",
    "MediaValidationError",
    "NormalizedMediaResult",
    "PipelineMediaProcessorProtocol",
    "UploadMediaDetails",
]
