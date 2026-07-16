# ruff: noqa: TC001,TC003
"""Typed admin API contracts for versioned search synonym management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from memexpert.models.enums import (
    SearchSynonymLocale,
    SearchSynonymRevisionStatus,
    SearchSynonymSyncStatus,
)
from memexpert.schemas._text import normalize_required_text


class SearchSynonymValidationIssueRead(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    line_number: int | None = None
    term: str | None = None


class SearchSynonymValidationRead(BaseModel):
    valid: bool
    group_count: int = Field(ge=0)
    compiled_key_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    issues: list[SearchSynonymValidationIssueRead] = Field(default_factory=list)


class SearchSynonymRevisionRead(BaseModel):
    id: uuid.UUID
    revision_number: int = Field(ge=1)
    status: SearchSynonymRevisionStatus
    source_text: str
    compiler_version: str
    compiled_hash: str | None = None
    validation: SearchSynonymValidationRead
    change_note: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    version: str


class SearchSynonymCatalogRead(BaseModel):
    locale: SearchSynonymLocale
    draft: SearchSynonymRevisionRead
    published: SearchSynonymRevisionRead | None = None
    history: list[SearchSynonymRevisionRead] = Field(default_factory=list)


class SearchSynonymSyncStateRead(BaseModel):
    index_name: str
    status: SearchSynonymSyncStatus
    desired_hash: str | None = None
    applied_hash: str | None = None
    actual_hash: str | None = None
    desired_revisions: dict[str, int] = Field(default_factory=dict)
    last_task_uid: int | None = None
    requested_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_applied_at: datetime | None = None
    safe_error: str | None = None
    updated_at: datetime | None = None
    version: str


class _SearchSynonymMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    version: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("version", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_required_text(value)


class SearchSynonymDraftUpdateRequest(_SearchSynonymMutationRequest):
    source_text: str = Field(max_length=1_000_000)


class SearchSynonymMutationRequest(_SearchSynonymMutationRequest):
    pass


class SearchSynonymPublishRequest(_SearchSynonymMutationRequest):
    confirm_destructive: StrictBool = False


class SearchSynonymResetRequest(_SearchSynonymMutationRequest):
    revision_id: uuid.UUID | None = None


class SearchSynonymSyncRetryRequest(_SearchSynonymMutationRequest):
    pass


__all__ = [
    "SearchSynonymCatalogRead",
    "SearchSynonymDraftUpdateRequest",
    "SearchSynonymMutationRequest",
    "SearchSynonymPublishRequest",
    "SearchSynonymResetRequest",
    "SearchSynonymRevisionRead",
    "SearchSynonymSyncRetryRequest",
    "SearchSynonymSyncStateRead",
    "SearchSynonymValidationIssueRead",
    "SearchSynonymValidationRead",
]
