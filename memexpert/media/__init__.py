"""Import-safe media contract surface.

The heavy worker implementation lives in :mod:`memexpert.media.inspect` and is
loaded only when callers explicitly ask for it.
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
