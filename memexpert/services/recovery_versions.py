"""Canonical version fences shared by Replay & Repair projections."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid

    from memexpert.models.content import MemeFile, PipelineStageJournal


def recovery_row_version(row: object, event_id: uuid.UUID | None = None) -> str:
    """Return the canonical timestamp/event fence for one durable work row."""

    updated_at = getattr(row, "updated_at", None)
    stamp = updated_at.isoformat() if updated_at is not None else ""
    return f"{stamp}:{event_id or ''}"


def media_recovery_version(stage: PipelineStageJournal, meme_file: MemeFile) -> str:
    """Fence moving-media work against both its journal and active file state."""

    payload = json.dumps(
        {
            "schema": "media-recovery-v1",
            "stage_version": recovery_row_version(stage, stage.last_event_id),
            "file": {
                "id": str(meme_file.id),
                "updated_at": meme_file.updated_at.isoformat(),
                "status": meme_file.status.value,
                "mime_type": meme_file.mime_type,
                "s3_original_key": meme_file.s3_original_key,
                "s3_web_video_key": meme_file.s3_web_video_key,
                "active_media_generation_id": (
                    str(meme_file.active_media_generation_id)
                    if meme_file.active_media_generation_id is not None
                    else None
                ),
                "web_video_profile": meme_file.web_video_profile,
                "web_video_verified_at": (
                    meme_file.web_video_verified_at.isoformat()
                    if meme_file.web_video_verified_at is not None
                    else None
                ),
                "source_has_audio": meme_file.source_has_audio,
                "web_video_has_audio": meme_file.web_video_has_audio,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"media-v1:{hashlib.sha256(payload.encode()).hexdigest()}"


__all__ = ["media_recovery_version", "recovery_row_version"]
