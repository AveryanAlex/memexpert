"""SEO generation boundary and writer service for meme landing pages."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from unicodedata import normalize

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeSeoPage, MemeTemplate

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DEFAULT_PROMPT_VERSION = "meme-seo-v1"


class MemeSeoProviderError(RuntimeError):
    """Raised when a provider cannot generate SEO data for a meme."""


class MemeSeoProviderProtocol(Protocol):
    """Fakeable provider boundary for SEO text generation."""

    model_id: str

    prompt_version: str

    async def generate(self, meme: Meme) -> MemeSeoProviderResult: ...


@dataclass(frozen=True, slots=True)
class MemeSeoProviderResult:
    """Provider-authored SEO payload before storage normalization."""

    page_title: str
    meta_description: str
    alt_text: str
    slug: str | None = None
    caption: str | None = None
    body_text: str | None = None
    tags: tuple[str, ...] = ()
    template_slug: str | None = None
    template_name: str | None = None
    template_description: str | None = None


@dataclass(frozen=True, slots=True)
class MemeSeoGenerationResult:
    """Per-meme generation outcome for batch callers and tests."""

    meme_id: uuid.UUID
    status: str
    slug: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    reason: str | None = None


class StaticMemeSeoProvider:
    """No-network fallback provider for local smoke tests and development."""

    model_id = "static-local"
    prompt_version = DEFAULT_PROMPT_VERSION

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        title_seed = _first_present(meme.tags) or _first_words(meme.ocr_text) or str(meme.id)[:8]
        title = f"{title_seed.title()} meme"
        return MemeSeoProviderResult(
            page_title=title,
            meta_description=f"Browse and share this {title.lower()} from the public MemeXpert catalog.",
            alt_text=title,
            slug=title_seed,
            caption=title,
            body_text=meme.ocr_text,
            tags=tuple(meme.tags),
        )


class MemeSeoGenerationService:
    """Generate and persist canonical SEO data for selected memes."""

    def __init__(self, session: AsyncSession, *, provider: MemeSeoProviderProtocol | None = None) -> None:
        self._session = session
        self._provider = provider or StaticMemeSeoProvider()

    async def generate_for_meme_ids(
        self,
        meme_ids: tuple[uuid.UUID, ...],
        *,
        force: bool = False,
        commit: bool = True,
    ) -> list[MemeSeoGenerationResult]:
        results: list[MemeSeoGenerationResult] = []
        for meme_id in meme_ids:
            results.append(await self.generate_for_meme_id(meme_id, force=force, commit=False))

        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return results

    async def generate_for_meme_id(
        self,
        meme_id: uuid.UUID,
        *,
        force: bool = False,
        commit: bool = True,
    ) -> MemeSeoGenerationResult:
        meme = await self._session.scalar(
            select(Meme)
            .where(Meme.id == meme_id)
            .options(selectinload(Meme.seo_page), selectinload(Meme.template)),
        )
        if meme is None:
            return MemeSeoGenerationResult(meme_id=meme_id, status="not_found", reason="meme_not_found")

        existing = meme.seo_page
        if existing is not None and existing.edited_at is not None and not force:
            return MemeSeoGenerationResult(
                meme_id=meme.id,
                status="skipped",
                slug=existing.slug,
                model_id=existing.model_id,
                prompt_version=existing.prompt_version,
                reason="manual_edit_present",
            )

        try:
            payload = await self._provider.generate(meme)
        except Exception:
            logger.exception("Meme SEO generation failed for meme %s", meme_id)
            if commit:
                await self._session.rollback()
            return MemeSeoGenerationResult(meme_id=meme.id, status="failed", reason="provider_error")

        model_id = getattr(self._provider, "model_id", "unknown")
        prompt_version = getattr(self._provider, "prompt_version", DEFAULT_PROMPT_VERSION)
        slug = await self._unique_slug(_slug_seeds(payload, meme), meme_id=meme.id)
        tags = _clean_tags(payload.tags) or _clean_tags(tuple(meme.tags))

        if existing is None:
            existing = MemeSeoPage(meme_id=meme.id)
            meme.seo_page = existing
            self._session.add(existing)

        now = utcnow()
        existing.slug = slug
        existing.page_title = _required_text(payload.page_title, fallback="Meme page")[:255]
        existing.meta_description = _required_text(payload.meta_description, fallback=existing.page_title)
        existing.alt_text = _required_text(payload.alt_text, fallback=existing.page_title)
        existing.caption = _optional_text(payload.caption)
        existing.body_text = _optional_text(payload.body_text)
        existing.tags = tags
        existing.model_id = model_id
        existing.prompt_version = prompt_version
        existing.generated_at = now

        if tags:
            meme.tags = tags
        await self._apply_template(meme, payload)

        await self._session.flush()
        if commit:
            await self._session.commit()
        return MemeSeoGenerationResult(
            meme_id=meme.id,
            status="generated",
            slug=slug,
            model_id=model_id,
            prompt_version=prompt_version,
        )

    async def _apply_template(self, meme: Meme, payload: MemeSeoProviderResult) -> None:
        template_slug = _slugify(payload.template_slug or payload.template_name or "")
        template_name = _optional_text(payload.template_name)
        if not template_slug or not template_name:
            return

        template = await self._session.scalar(select(MemeTemplate).where(MemeTemplate.slug == template_slug))
        if template is None:
            template = MemeTemplate(
                slug=template_slug,
                name=template_name,
                description=_optional_text(payload.template_description),
                is_curated=False,
            )
            self._session.add(template)
            await self._session.flush()
        meme.template_id = template.id

    async def _unique_slug(self, seeds: tuple[str, ...], *, meme_id: uuid.UUID) -> str:
        base = _slugify(_first_present(seeds) or str(meme_id)[:8]) or str(meme_id)[:8]
        result = await self._session.execute(
            select(MemeSeoPage.slug, MemeSeoPage.meme_id).where(MemeSeoPage.slug.like(f"{base}%")),
        )
        existing = {slug for slug, owner_id in result.all() if owner_id != meme_id}
        if base not in existing:
            return base

        suffix = 2
        while True:
            candidate = _with_suffix(base, suffix)
            if candidate not in existing:
                return candidate
            suffix += 1


def _slug_seeds(payload: MemeSeoProviderResult, meme: Meme) -> tuple[str, ...]:
    return (
        payload.slug or "",
        payload.page_title,
        *payload.tags,
        *meme.tags,
        meme.ocr_text or "",
        str(meme.id)[:8],
    )


def _slugify(value: str) -> str:
    ascii_value = normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)[:255]


def _with_suffix(base: str, suffix: int) -> str:
    suffix_text = f"-{suffix}"
    return f"{base[: 255 - len(suffix_text)]}{suffix_text}"


def _clean_tags(tags: tuple[str, ...]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = re.sub(r"\s+", "-", tag.strip().lower())[:64]
        if normalized and normalized not in seen:
            cleaned.append(normalized)
            seen.add(normalized)
    return cleaned


def _required_text(value: str | None, *, fallback: str) -> str:
    return (value or "").strip() or fallback


def _optional_text(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


def _first_present(values: tuple[str, ...] | list[str]) -> str | None:
    for value in values:
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _first_words(value: str | None) -> str | None:
    if not value:
        return None
    words = value.split()
    return " ".join(words[:6]) if words else None


__all__ = [
    "DEFAULT_PROMPT_VERSION",
    "MemeSeoGenerationResult",
    "MemeSeoGenerationService",
    "MemeSeoProviderError",
    "MemeSeoProviderProtocol",
    "MemeSeoProviderResult",
    "StaticMemeSeoProvider",
]
