"""SEO generation boundary and writer service for meme landing pages."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol
from unicodedata import normalize

from openai import AsyncOpenAI
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from memexpert.core.config import Settings, get_settings
from memexpert.models.base import utcnow
from memexpert.models.content import Meme, MemeSeoPage, MemeTemplate

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MAX_TAG_LENGTH = 64
MAX_SLUG_LENGTH = 255
MAX_PAGE_TITLE_LENGTH = 255
MAX_PROMPT_VERSION_LENGTH = 64
DEFAULT_PROMPT_VERSION = "meme-seo-v1"

# Ported from the v0 Rust prompt baseline (`v0:prompts/meta.md`) and adapted to
# the current backend fields. Unlike v0, this service currently has no image
# bytes available during SEO generation, so the provider must stay grounded in
# OCR text, existing tags, and current template metadata only.
SEO_PROMPT_BASELINE = """
Role: experienced copywriter and SEO expert.

Task: write structured SEO content for a meme web page using the provided meme metadata.

Important constraints:
- You do not have the image bytes. Only use the provided OCR text, existing tags, language, and template metadata.
- If the metadata is insufficient to support a visual claim, stay generic and do not invent scene details.
- Preserve searchable original phrases from OCR text when they are likely what users would search for.
- Keep the language human, concise, and SEO-friendly.

Field guidance:
- `page_title`: concise, search-friendly page title. Prefer under 60 characters when practical. Do not leave it blank.
- `meta_description`: one or two natural sentences for search snippets. Do not leave it blank.
- `alt_text`: concise factual alt text based only on the available metadata. Do not leave it blank.
- `caption`: short caption or subtitle for the meme page.
- `body_text`: one or two short paragraphs that describe the meme and searchable context without filler.
- `slug`: short lowercase ASCII slug candidate using hyphens.
- `tags`: short tag candidates. Duplicates and formatting will be normalized downstream.
- `template_slug`, `template_name`, `template_description`: provide these only when the metadata strongly suggests the
  meme belongs to a reusable template. Otherwise use null.

General recommendations:
- Do not include commentary outside the structured response.
- Do not censor swear or sexualized words when they are materially relevant to how the meme would be searched.
""".strip()


class MemeSeoStructuredOutput(BaseModel):
    """Validated provider output before any persistence-side normalization."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page_title: str = Field(
        min_length=1,
        max_length=MAX_PAGE_TITLE_LENGTH,
        validation_alias=AliasChoices("page_title", "title"),
    )
    meta_description: str = Field(
        min_length=1,
        validation_alias=AliasChoices("meta_description", "description"),
    )
    alt_text: str = Field(min_length=1)
    slug: str | None = Field(default=None, max_length=MAX_SLUG_LENGTH)
    caption: str | None = None
    body_text: str | None = None
    tags: tuple[str, ...] = ()
    template_slug: str | None = Field(default=None, max_length=MAX_SLUG_LENGTH)
    template_name: str | None = Field(default=None, max_length=MAX_SLUG_LENGTH)
    template_description: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _promote_v0_aliases(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value

        payload = dict(value)
        subtitle = payload.pop("subtitle", None)
        if isinstance(subtitle, str):
            payload.setdefault("alt_text", subtitle)
            payload.setdefault("caption", subtitle)
        payload.pop("text_on_meme", None)
        return payload

    @field_validator(
        "slug",
        "caption",
        "body_text",
        "template_slug",
        "template_name",
        "template_description",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        normalized_value = value.strip()
        return normalized_value or None

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_tags(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("tags must be provided as a sequence of strings.")

        normalized_tags: list[str] = []
        for tag in value:
            if not isinstance(tag, str):
                raise ValueError("tags must contain only strings.")
            normalized_tags.append(tag)
        return tuple(normalized_tags)

    @model_validator(mode="after")
    def _validate_template_consistency(self) -> MemeSeoStructuredOutput:
        if (self.template_slug is not None or self.template_description is not None) and self.template_name is None:
            raise ValueError("template_name is required when template_slug or template_description is provided.")
        return self

    def to_provider_result(self) -> MemeSeoProviderResult:
        return MemeSeoProviderResult(
            page_title=self.page_title,
            meta_description=self.meta_description,
            alt_text=self.alt_text,
            slug=self.slug,
            caption=self.caption,
            body_text=self.body_text,
            tags=self.tags,
            template_slug=self.template_slug,
            template_name=self.template_name,
            template_description=self.template_description,
        )


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

    def __init__(self, *, prompt_version: str = DEFAULT_PROMPT_VERSION) -> None:
        self.model_id = "static-local"
        self.prompt_version = prompt_version

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


class PydanticAIMemeSeoProvider:
    """Live OpenAI-compatible SEO provider backed by a typed PydanticAI agent."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.model_id = self._settings.pipeline_seo_model
        self.prompt_version = self._settings.pipeline_seo_prompt_version
        self._agent: Any | None = None

    async def generate(self, meme: Meme) -> MemeSeoProviderResult:
        agent = self._get_agent()
        result = await agent.run(_build_meme_seo_prompt(meme))
        output = MemeSeoStructuredOutput.model_validate(result.output)
        return output.to_provider_result()

    def _get_agent(self) -> Any:
        if self._agent is None:
            openai_client = AsyncOpenAI(
                api_key=self._resolve_api_key(),
                base_url=self._settings.pipeline_seo_api_base_url,
                timeout=self._settings.pipeline_seo_timeout_seconds,
                max_retries=0,
            )
            model = OpenAIChatModel(
                self._settings.pipeline_seo_model,
                provider=OpenAIProvider(openai_client=openai_client),
            )
            self._agent = Agent(
                model,
                output_type=MemeSeoStructuredOutput,
                instructions=SEO_PROMPT_BASELINE,
                retries=0,
                defer_model_check=True,
            )
        return self._agent

    def _resolve_api_key(self) -> str:
        configured_key = self._settings.pipeline_seo_api_key
        if configured_key is None:
            raise MemeSeoProviderError("SEO API key is not configured for live mode.")
        raw_api_key = configured_key.get_secret_value().strip()
        if not raw_api_key:
            raise MemeSeoProviderError("SEO API key is blank for live mode.")
        return raw_api_key


def build_meme_seo_provider(*, settings: Settings | None = None) -> MemeSeoProviderProtocol:
    """Return the configured SEO provider while keeping static mode as the default."""

    resolved_settings = settings or get_settings()
    if resolved_settings.pipeline_seo_provider_mode == "live":
        return PydanticAIMemeSeoProvider(settings=resolved_settings)
    return StaticMemeSeoProvider(prompt_version=resolved_settings.pipeline_seo_prompt_version)


class MemeSeoGenerationService:
    """Generate and persist canonical SEO data for selected memes."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: MemeSeoProviderProtocol | None = None,
        settings: Settings | None = None,
        provider_max_attempts: int | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._provider = provider or build_meme_seo_provider(settings=self._settings)
        self._provider_max_attempts = provider_max_attempts or self._settings.pipeline_seo_max_attempts

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
            raw_payload = await self._generate_with_retries(meme)
            payload = _validate_provider_payload(raw_payload)
            model_id, prompt_version = _validated_provider_metadata(self._provider)
        except ValidationError:
            logger.warning("Meme SEO provider returned invalid structured output for meme %s", meme.id)
            return MemeSeoGenerationResult(meme_id=meme.id, status="failed", reason="invalid_output")
        except MemeSeoProviderError:
            logger.warning("Meme SEO provider configuration failed for meme %s", meme.id)
            return MemeSeoGenerationResult(meme_id=meme.id, status="failed", reason="provider_error")
        except Exception as exc:
            logger.warning(
                "Meme SEO generation failed for meme %s after %s attempt(s) with %s",
                meme.id,
                self._provider_max_attempts,
                type(exc).__name__,
            )
            return MemeSeoGenerationResult(meme_id=meme.id, status="failed", reason="provider_error")

        slug = await self._unique_slug(_slug_seeds(payload, meme), meme_id=meme.id)
        tags = _clean_tags(payload.tags) or _clean_tags(tuple(meme.tags))

        if existing is None:
            existing = MemeSeoPage(meme_id=meme.id)
            meme.seo_page = existing
            self._session.add(existing)

        now = utcnow()
        existing.slug = slug
        existing.page_title = payload.page_title
        existing.meta_description = payload.meta_description
        existing.alt_text = payload.alt_text
        existing.caption = payload.caption
        existing.body_text = payload.body_text
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

    async def _generate_with_retries(self, meme: Meme) -> MemeSeoProviderResult:
        last_error: Exception | None = None
        for attempt in range(1, self._provider_max_attempts + 1):
            try:
                return await self._provider.generate(meme)
            except (MemeSeoProviderError, ValidationError):
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Meme SEO provider attempt %s/%s failed for meme %s with %s",
                    attempt,
                    self._provider_max_attempts,
                    meme.id,
                    type(exc).__name__,
                )
                if attempt >= self._provider_max_attempts:
                    break

        if last_error is None:
            raise MemeSeoProviderError("SEO provider did not return a result.")
        raise last_error

    async def _apply_template(self, meme: Meme, payload: MemeSeoStructuredOutput) -> None:
        template_slug = _slugify(payload.template_slug or payload.template_name or "")
        template_name = payload.template_name
        if not template_slug or template_name is None:
            return

        template = await self._session.scalar(select(MemeTemplate).where(MemeTemplate.slug == template_slug))
        if template is None:
            template = MemeTemplate(
                slug=template_slug,
                name=template_name,
                description=payload.template_description,
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


def _build_meme_seo_prompt(meme: Meme) -> str:
    template = meme.template
    prompt_context = {
        "meme_id": str(meme.id),
        "language": getattr(meme.language, "value", str(meme.language)),
        "ocr_text": _optional_text(meme.ocr_text),
        "existing_tags": list(meme.tags),
        "template": {
            "slug": template.slug,
            "name": template.name,
            "description": template.description,
            "is_curated": template.is_curated,
        }
        if template is not None
        else None,
    }
    return "Meme metadata:\n" + json.dumps(prompt_context, ensure_ascii=True, indent=2)


def _validate_provider_payload(payload: object) -> MemeSeoStructuredOutput:
    if isinstance(payload, MemeSeoStructuredOutput):
        return payload
    if isinstance(payload, BaseModel):
        return MemeSeoStructuredOutput.model_validate(payload.model_dump(mode="python"))
    if isinstance(payload, MemeSeoProviderResult):
        return MemeSeoStructuredOutput.model_validate(asdict(payload))
    if isinstance(payload, Mapping):
        return MemeSeoStructuredOutput.model_validate(dict(payload))
    return MemeSeoStructuredOutput.model_validate(payload)


def _validated_provider_metadata(provider: MemeSeoProviderProtocol) -> tuple[str, str]:
    model_id = _validated_limited_text(
        getattr(provider, "model_id", None),
        field_name="model_id",
        max_length=MAX_SLUG_LENGTH,
    )
    prompt_version = _validated_limited_text(
        getattr(provider, "prompt_version", None),
        field_name="prompt_version",
        max_length=MAX_PROMPT_VERSION_LENGTH,
    )
    return model_id, prompt_version


def _validated_limited_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise MemeSeoProviderError(f"SEO provider {field_name} must be a string.")

    normalized_value = value.strip()
    if not normalized_value:
        raise MemeSeoProviderError(f"SEO provider {field_name} must not be blank.")
    if len(normalized_value) > max_length:
        raise MemeSeoProviderError(f"SEO provider {field_name} exceeds {max_length} characters.")
    return normalized_value


def _slug_seeds(payload: MemeSeoStructuredOutput, meme: Meme) -> tuple[str, ...]:
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
    return re.sub(r"-{2,}", "-", slug)[:MAX_SLUG_LENGTH]


def _with_suffix(base: str, suffix: int) -> str:
    suffix_text = f"-{suffix}"
    return f"{base[: MAX_SLUG_LENGTH - len(suffix_text)]}{suffix_text}"


def _clean_tags(tags: tuple[str, ...]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = re.sub(r"\s+", "-", tag.strip().lower())[:MAX_TAG_LENGTH]
        if normalized and normalized not in seen:
            cleaned.append(normalized)
            seen.add(normalized)
    return cleaned


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
    "MemeSeoStructuredOutput",
    "PydanticAIMemeSeoProvider",
    "SEO_PROMPT_BASELINE",
    "StaticMemeSeoProvider",
    "build_meme_seo_provider",
]
