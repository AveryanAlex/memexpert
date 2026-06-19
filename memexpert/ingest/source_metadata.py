"""Helpers for optional source metadata stored on raw ingest requests."""

from __future__ import annotations

from datetime import datetime

from memexpert.models.enums import SourceEngagementCommentsState
from memexpert.services.source_engagement import SourceEngagementMetrics


def source_engagement_reactions(source_metadata: dict[str, object]) -> dict[str, int] | None:
    """Return canonical source reactions while preserving unknown-vs-known-zero."""

    if "reactions" not in source_metadata:
        return None
    raw_reactions = source_metadata.get("reactions")
    if raw_reactions is None:
        return None
    if not isinstance(raw_reactions, dict):
        return None

    reactions: dict[str, int] = {}
    for raw_key, raw_value in raw_reactions.items():
        if not isinstance(raw_key, str):
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
            continue
        reactions[raw_key] = raw_value
    return reactions


def source_view_count(source_metadata: dict[str, object]) -> int | None:
    """Return the canonical source view count while preserving unknown-vs-zero."""

    return _non_negative_int_or_none(source_metadata.get("view_count"))


def source_forward_count(source_metadata: dict[str, object]) -> int | None:
    """Return the Telegram forward/share count while preserving unknown-vs-zero."""

    return _non_negative_int_or_none(source_metadata.get("forward_count"))


def source_comment_count(source_metadata: dict[str, object]) -> int | None:
    """Return the Telegram comment/reply count while preserving unknown-vs-zero."""

    return _non_negative_int_or_none(source_metadata.get("comment_count"))


def source_comments_state(source_metadata: dict[str, object]) -> SourceEngagementCommentsState:
    raw_state = source_metadata.get("comments_state")
    if isinstance(raw_state, SourceEngagementCommentsState):
        return raw_state
    if isinstance(raw_state, str):
        try:
            return SourceEngagementCommentsState(raw_state)
        except ValueError:
            return SourceEngagementCommentsState.UNKNOWN
    return SourceEngagementCommentsState.UNKNOWN


def source_engagement_metrics(source_metadata: dict[str, object]) -> SourceEngagementMetrics:
    """Parse source metadata into canonical engagement snapshot metrics."""

    reactions = source_engagement_reactions(source_metadata)
    comments_state = source_comments_state(source_metadata)
    return SourceEngagementMetrics(
        view_count=source_view_count(source_metadata),
        reactions=reactions,
        comment_count=source_comment_count(source_metadata),
        forward_count=source_forward_count(source_metadata),
        comments_state=comments_state,
        raw_metrics=_source_raw_metrics(
            source_metadata,
            reactions=reactions,
            comments_state=comments_state,
        ),
    )


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


def _non_negative_int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _source_raw_metrics(
    source_metadata: dict[str, object],
    *,
    reactions: dict[str, int] | None,
    comments_state: SourceEngagementCommentsState,
) -> dict[str, object]:
    raw_metrics: dict[str, object] = {
        "view_count": source_view_count(source_metadata),
        "reactions": reactions,
        "comment_count": source_comment_count(source_metadata),
        "forward_count": source_forward_count(source_metadata),
        "comments_state": comments_state.value,
    }
    return raw_metrics


__all__ = [
    "source_comment_count",
    "source_comments_state",
    "source_engagement_metrics",
    "source_engagement_reactions",
    "source_forward_ids",
    "source_forward_count",
    "source_is_forwarded",
    "source_published_at",
    "source_view_count",
]
