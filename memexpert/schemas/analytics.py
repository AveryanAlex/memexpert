"""Public-safe request and acknowledgement schemas for product telemetry."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ConsumerPageSurface(StrEnum):
    """Approved route categories for first-party consumer page views.

    These values intentionally describe a broad surface rather than a route,
    URL, slug, query string, referrer, or other visitor-specific value.
    """

    WEB_ACCOUNT = "web_account"
    WEB_COLLECTION = "web_collection"
    WEB_HOME = "web_home"
    WEB_LIBRARY = "web_library"
    WEB_MEME_DETAIL = "web_meme_detail"
    WEB_PROFILE = "web_profile"
    WEB_SEARCH = "web_search"
    WEB_TAG = "web_tag"
    WEB_TEMPLATE = "web_template"
    WEB_TRENDS = "web_trends"


class PageViewCreateRequest(BaseModel):
    """One browser-reported first-party consumer route visit."""

    model_config = ConfigDict(extra="forbid")

    surface: ConsumerPageSurface


class PageViewRecordedRead(BaseModel):
    """Best-effort acknowledgement that deliberately exposes no event metadata."""

    ok: bool = True


__all__ = [
    "ConsumerPageSurface",
    "PageViewCreateRequest",
    "PageViewRecordedRead",
]
