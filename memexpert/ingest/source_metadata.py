"""Helpers for optional source metadata stored on raw ingest requests."""

from __future__ import annotations

from datetime import datetime


def source_reactions(source_metadata: dict[str, object]) -> dict[str, int]:
    raw_reactions = source_metadata.get("reactions")
    if not isinstance(raw_reactions, dict):
        return {}

    reactions: dict[str, int] = {}
    for raw_key, raw_value in raw_reactions.items():
        if not isinstance(raw_key, str):
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
            continue
        reactions[raw_key] = raw_value
    return reactions


def source_published_at(source_metadata: dict[str, object]) -> datetime | None:
    raw_published_at = source_metadata.get("published_at")
    if not isinstance(raw_published_at, str):
        return None
    try:
        return datetime.fromisoformat(raw_published_at)
    except ValueError:
        return None


def source_forward_ids(source_metadata: dict[str, object]) -> tuple[str | None, str | None]:
    raw_forward = source_metadata.get("forward")
    if not isinstance(raw_forward, dict):
        return None, None
    source_id = raw_forward.get("source_id")
    post_id = raw_forward.get("post_id")
    return (
        source_id if isinstance(source_id, str) and source_id else None,
        post_id if isinstance(post_id, str) and post_id else None,
    )


def source_is_forwarded(source_metadata: dict[str, object]) -> bool:
    source_id, post_id = source_forward_ids(source_metadata)
    return source_id is not None and post_id is not None


__all__ = [
    "source_forward_ids",
    "source_is_forwarded",
    "source_published_at",
    "source_reactions",
]
