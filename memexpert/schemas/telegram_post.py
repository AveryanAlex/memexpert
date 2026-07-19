# ruff: noqa: TC003
"""Normalized Telegram post context safe for durable storage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

TELEGRAM_POST_METADATA_VERSION = 1

type TelegramTextEntityType = Literal[
    "bank_card",
    "blockquote",
    "bold",
    "bot_command",
    "cashtag",
    "code",
    "custom_emoji",
    "email",
    "hashtag",
    "italic",
    "mention",
    "mention_name",
    "phone",
    "pre",
    "spoiler",
    "strikethrough",
    "text_url",
    "underline",
    "url",
]


class TelegramTextEntity(BaseModel):
    """Allowlisted, SDK-independent projection of one Telegram text entity.

    ``offset`` and ``length`` retain Telegram's UTF-16 code-unit coordinates;
    consumers must not reinterpret them as Python string indexes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: TelegramTextEntityType
    offset: StrictInt = Field(ge=0)
    length: StrictInt = Field(ge=0)
    url: str | None = None
    user_id: StrictInt | None = None
    language: str | None = None
    document_id: StrictInt | None = None
    collapsed: bool | None = None


class TelegramPostMetadata(BaseModel):
    """Textual and relational context captured from one Telegram message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = TELEGRAM_POST_METADATA_VERSION
    # Text is intentionally not stripped or otherwise normalized. Telegram's
    # exact Unicode payload is the durable source value.
    text: str | None = None
    text_entities: tuple[TelegramTextEntity, ...] = ()
    media_group_id: str | None = Field(default=None, max_length=255)
    reply_to_post_id: str | None = Field(default=None, max_length=255)
    edited_at: datetime | None = None

    def json_snapshot(self) -> dict[str, object]:
        """Return the allowlisted JSON shape copied into ingest metadata."""

        snapshot = self.model_dump(mode="json", exclude={"text_entities"})
        snapshot["text_entities"] = self.entity_json()
        return snapshot

    def entity_json(self) -> list[dict[str, object]]:
        """Return normalized entities without retaining Pydantic/SDK objects."""

        return [entity.model_dump(mode="json", exclude_none=True) for entity in self.text_entities]


__all__ = [
    "TELEGRAM_POST_METADATA_VERSION",
    "TelegramPostMetadata",
    "TelegramTextEntity",
    "TelegramTextEntityType",
]
