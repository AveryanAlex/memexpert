"""Small shared network-adapter helpers used by the Qdrant and Meilisearch sync clients.

These helpers are deliberately scoped to "things both sync adapters need to
classify transport exceptions identically" and nothing else — they must not
accumulate unrelated utilities that would grow into a catch-all module.
"""

from __future__ import annotations

import asyncio

import httpx


def is_timeout_exception(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` wraps an underlying transport/async timeout.

    Shared between :mod:`memexpert.core.qdrant` and
    :mod:`memexpert.core.meilisearch` so both sync clients classify transport
    timeouts identically — we walk the ``__cause__`` chain because SDK
    wrappers tend to bury the real timeout two or three layers deep.
    """

    cause: BaseException | None = exc.__cause__
    while cause is not None:
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            return True
        cause = cause.__cause__
    return False


__all__ = ["is_timeout_exception"]
