# ruff: noqa: TC001,TC003
"""Schemas for user-facing moderation report submission."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memexpert.models.enums import ModerationReason, ModerationReportStatus
from memexpert.schemas.base import ORMSchema

MAX_REPORT_NOTE_LENGTH = 2048


class MemeReportCreateRequest(BaseModel):
    """User-submitted report payload for a visible meme."""

    model_config = ConfigDict(extra="forbid")

    reason: ModerationReason
    note: str | None = Field(default=None, max_length=MAX_REPORT_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class MemeReportRead(ORMSchema):
    """Public report receipt returned to the reporting user."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    meme_id: uuid.UUID
    status: ModerationReportStatus
    reason: ModerationReason
    note: str | None
    created_at: datetime
    updated_at: datetime


__all__ = ["MAX_REPORT_NOTE_LENGTH", "MemeReportCreateRequest", "MemeReportRead"]
