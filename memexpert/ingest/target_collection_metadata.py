"""Typed helpers for upload target-collection metadata."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

TARGET_COLLECTION_ID_METADATA_KEY: Final = "target_collection_id"


class TargetCollectionMetadataError(ValueError):
    """Raised when target-collection metadata is malformed."""


def user_metadata_with_target_collection(
    user_metadata: Mapping[str, object] | None = None,
    *,
    target_collection_id: uuid.UUID | None,
) -> dict[str, object]:
    """Return JSON-safe user metadata with an optional target collection id."""

    metadata = dict(user_metadata or {})
    if target_collection_id is None:
        metadata.pop(TARGET_COLLECTION_ID_METADATA_KEY, None)
        return metadata
    metadata[TARGET_COLLECTION_ID_METADATA_KEY] = str(target_collection_id)
    return metadata


def parse_target_collection_id(user_metadata: Mapping[str, object] | None) -> uuid.UUID | None:
    """Parse the optional target collection id from ingest user metadata."""

    if not user_metadata or TARGET_COLLECTION_ID_METADATA_KEY not in user_metadata:
        return None

    raw_value = user_metadata[TARGET_COLLECTION_ID_METADATA_KEY]
    if isinstance(raw_value, uuid.UUID):
        return raw_value
    if isinstance(raw_value, str):
        normalized_value = raw_value.strip()
        if not normalized_value:
            raise TargetCollectionMetadataError("target_collection_id must not be blank.")
        try:
            return uuid.UUID(normalized_value)
        except ValueError as exc:
            raise TargetCollectionMetadataError("target_collection_id must be a valid UUID.") from exc

    raise TargetCollectionMetadataError("target_collection_id must be a UUID string.")


__all__ = [
    "TARGET_COLLECTION_ID_METADATA_KEY",
    "TargetCollectionMetadataError",
    "parse_target_collection_id",
    "user_metadata_with_target_collection",
]
