# ruff: noqa: TC001,TC002,TC003
"""DB-only public safe SEO catalog service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from memexpert.models.content import Meme, MemeFile, MemeSeoPage, MemeTemplate
from memexpert.schemas.meme import PublicMemeFileRead
from memexpert.schemas.seo import (
    SeoCatalogMemePageRead,
    SeoCatalogMemeRead,
    SeoCatalogMemeTemplateRefRead,
    SeoCatalogSummaryRead,
    SeoCatalogTagPageRead,
    SeoCatalogTagRead,
    SeoCatalogTemplatePageRead,
    SeoCatalogTemplateRead,
)
from memexpert.services.engagement_read_model import load_derived_popularity_scores
from memexpert.services.media_render_urls import MediaRenderUrlService, PublicMediaRenderContext

DEFAULT_CATALOG_LIMIT = 1_000
MAX_CATALOG_LIMIT = 50_000
DEFAULT_PINTEREST_FEED_LIMIT = 100
MAX_PINTEREST_FEED_LIMIT = 500
_TITLE_MAX_LENGTH = 160
_DESCRIPTION_MAX_LENGTH = 320
_ALT_TEXT_MAX_LENGTH = 250
_DERIVED_POPULARITY_ATTR = "_derived_popularity_score"


class SeoCatalogService:
    """Read public, SFW catalog data for frontend-owned XML/feed generation."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        media_render_service: MediaRenderUrlService | None = None,
    ) -> None:
        self._session = session
        self._media_render_service = media_render_service or MediaRenderUrlService()

    async def get_summary(self) -> SeoCatalogSummaryRead:
        """Return safe public catalog counts and latest related update timestamp."""

        meme_count = await self._scalar_int(select(func.count(Meme.id)).where(*_public_safe_filters()))
        tag_count = await self._scalar_int(select(func.count()).select_from(_tag_aggregation_stmt().subquery()))
        template_count = await self._scalar_int(
            select(func.count()).select_from(_template_aggregation_stmt().subquery())
        )
        updated_at = _latest_datetime(
            await self._session.scalar(select(func.max(Meme.updated_at)).where(*_public_safe_filters())),
            await self._latest_seo_updated_at(),
            await self._latest_file_updated_at(),
            await self._latest_template_updated_at(),
        )
        return SeoCatalogSummaryRead(
            public_safe_meme_count=meme_count,
            tag_count=tag_count,
            template_count=template_count,
            updated_at=updated_at,
        )

    async def list_memes(self, *, limit: int = DEFAULT_CATALOG_LIMIT, offset: int = 0) -> SeoCatalogMemePageRead:
        """Return public safe meme rows in stable ID order for full catalog scans."""

        return await self._list_memes(
            limit=limit,
            offset=offset,
            order_by=(Meme.id.asc(),),
        )

    async def list_pinterest_feed(
        self,
        *,
        limit: int = DEFAULT_PINTEREST_FEED_LIMIT,
        offset: int = 0,
    ) -> SeoCatalogMemePageRead:
        """Return a smaller feed-friendly page ordered by recent/popular safe public content."""

        return await self._list_memes(
            limit=_clamp_limit(limit, max_limit=MAX_PINTEREST_FEED_LIMIT),
            offset=offset,
            order_by=(
                Meme.updated_at.desc(),
                Meme.created_at.desc(),
                Meme.id.desc(),
            ),
        )

    async def list_tags(self, *, limit: int = DEFAULT_CATALOG_LIMIT, offset: int = 0) -> SeoCatalogTagPageRead:
        """Return public tag landing records derived only from safe public memes."""

        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        base_stmt = _tag_aggregation_stmt()
        total = await self._scalar_int(select(func.count()).select_from(base_stmt.subquery()))
        result = await self._session.execute(
            base_stmt.order_by("slug").limit(resolved_limit).offset(resolved_offset),
        )
        items = [
            SeoCatalogTagRead(
                slug=str(row["slug"]),
                title=_tag_title(str(row["slug"])),
                description=f"Public safe memes tagged {row['slug']}.",
                meme_count=int(row["meme_count"]),
                updated_at=row["updated_at"],
            )
            for row in result.mappings()
        ]
        return SeoCatalogTagPageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total,
            has_more=_has_more(total=total, limit=resolved_limit, offset=resolved_offset),
        )

    async def list_templates(
        self,
        *,
        limit: int = DEFAULT_CATALOG_LIMIT,
        offset: int = 0,
    ) -> SeoCatalogTemplatePageRead:
        """Return template landing records for templates with safe public memes."""

        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        base_stmt = _template_aggregation_stmt()
        total = await self._scalar_int(select(func.count()).select_from(base_stmt.subquery()))
        result = await self._session.execute(
            base_stmt.order_by("slug").limit(resolved_limit).offset(resolved_offset),
        )
        items = [
            SeoCatalogTemplateRead(
                slug=str(row["slug"]),
                name=str(row["name"]),
                title=_template_title(str(row["name"])),
                description=row["description"],
                meme_count=int(row["meme_count"]),
                updated_at=row["updated_at"],
            )
            for row in result.mappings()
        ]
        return SeoCatalogTemplatePageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total,
            has_more=_has_more(total=total, limit=resolved_limit, offset=resolved_offset),
        )

    async def _list_memes(
        self,
        *,
        limit: int,
        offset: int,
        order_by: tuple[ColumnElement[Any], ...],
    ) -> SeoCatalogMemePageRead:
        resolved_limit = _clamp_limit(limit)
        resolved_offset = max(0, offset)
        total = await self._scalar_int(select(func.count(Meme.id)).where(*_public_safe_filters()))
        result = await self._session.execute(
            _public_safe_meme_stmt().order_by(*order_by).limit(resolved_limit).offset(resolved_offset),
        )
        memes = list(result.scalars().unique())
        await self._attach_derived_popularity_scores(memes)
        items = [_to_meme_read(meme, media_render_service=self._media_render_service) for meme in memes]
        return SeoCatalogMemePageRead(
            items=items,
            limit=resolved_limit,
            offset=resolved_offset,
            total=total,
            has_more=_has_more(total=total, limit=resolved_limit, offset=resolved_offset),
        )

    async def _scalar_int(self, stmt: Select[tuple[int]]) -> int:
        return int(await self._session.scalar(stmt) or 0)

    async def _attach_derived_popularity_scores(self, memes: list[Meme]) -> None:
        scores = await load_derived_popularity_scores(self._session, tuple(dict.fromkeys(meme.id for meme in memes)))
        for meme in memes:
            setattr(meme, _DERIVED_POPULARITY_ATTR, scores.get(meme.id, 0.0))

    async def _latest_seo_updated_at(self) -> datetime | None:
        seo_updated_at = func.greatest(
            MemeSeoPage.generated_at,
            func.coalesce(MemeSeoPage.edited_at, MemeSeoPage.generated_at),
        )
        return await self._session.scalar(
            select(func.max(seo_updated_at)).join(Meme, Meme.id == MemeSeoPage.meme_id).where(*_public_safe_filters())
        )

    async def _latest_file_updated_at(self) -> datetime | None:
        return await self._session.scalar(
            select(func.max(MemeFile.updated_at)).join(Meme, Meme.id == MemeFile.meme_id).where(*_public_safe_filters())
        )

    async def _latest_template_updated_at(self) -> datetime | None:
        return await self._session.scalar(
            select(func.max(MemeTemplate.updated_at))
            .join(Meme, Meme.template_id == MemeTemplate.id)
            .where(*_public_safe_filters())
        )


def _public_safe_meme_stmt() -> Select[tuple[Meme]]:
    return select(Meme).options(
        selectinload(Meme.primary_file),
        selectinload(Meme.files),
        selectinload(Meme.seo_page),
        selectinload(Meme.template),
    ).where(*_public_safe_filters())


def _tag_aggregation_stmt() -> Select[tuple[str, int, datetime]]:
    tag_lateral = func.unnest(Meme.tags).table_valued("tag").render_derived().lateral("tag_lateral")
    slug = cast("ColumnElement[str]", func.lower(func.trim(tag_lateral.c.tag)).label("slug"))
    return (
        select(
            slug,
            func.count(func.distinct(Meme.id)).label("meme_count"),
            func.max(Meme.updated_at).label("updated_at"),
        )
        .select_from(Meme)
        .join(tag_lateral, true())
        .where(*_public_safe_filters(), slug != "")
        .group_by(slug)
    )


def _template_aggregation_stmt() -> Select[tuple[str, str, str | None, int, datetime | None]]:
    updated_at = func.max(func.greatest(Meme.updated_at, MemeTemplate.updated_at)).label("updated_at")
    return (
        select(
            MemeTemplate.slug.label("slug"),
            MemeTemplate.name.label("name"),
            MemeTemplate.description.label("description"),
            func.count(Meme.id).label("meme_count"),
            updated_at,
        )
        .select_from(MemeTemplate)
        .join(Meme, Meme.template_id == MemeTemplate.id)
        .where(*_public_safe_filters())
        .group_by(MemeTemplate.slug, MemeTemplate.name, MemeTemplate.description)
    )


def _public_safe_filters() -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (Meme.is_public.is_(True), Meme.is_nsfw.is_(False))


def _to_meme_read(meme: Meme, *, media_render_service: MediaRenderUrlService) -> SeoCatalogMemeRead:
    seo_page = meme.seo_page
    seo_slug = _clean_text(seo_page.slug) if seo_page is not None else None
    caption = _clean_text(seo_page.caption) if seo_page is not None else None
    tags = _tag_slugs(meme.tags)
    title = _excerpt(
        _first_text(
            seo_page.page_title if seo_page is not None else None,
            caption,
            meme.ocr_text,
            _tag_title(tags[0]) if tags else None,
            f"Meme {str(meme.id)[:8]}",
        ),
        max_length=_TITLE_MAX_LENGTH,
    )
    description_source = _first_text(
        seo_page.meta_description if seo_page is not None else None,
        seo_page.body_text if seo_page is not None else None,
        caption,
        meme.ocr_text,
    )
    description = _excerpt(description_source, max_length=_DESCRIPTION_MAX_LENGTH) if description_source else None
    alt_text = _excerpt(
        _first_text(
            seo_page.alt_text if seo_page is not None else None,
            title,
            caption,
            meme.ocr_text,
        ),
        max_length=_ALT_TEXT_MAX_LENGTH,
    )
    context = PublicMediaRenderContext(meme_id=meme.id, seo_slug=seo_slug, caption=caption)
    ordered_files = _ordered_files(meme)
    return SeoCatalogMemeRead(
        id=meme.id,
        seo_slug=seo_slug,
        title=title,
        description=description,
        alt_text=alt_text,
        caption=caption,
        tags=tags,
        media_type=meme.media_type,
        language=meme.language,
        popularity_score=_derived_popularity_score(meme),
        like_count=meme.like_count,
        template=_template_ref_read(meme.template) if meme.template is not None else None,
        primary_file=(
            _to_public_file_read(meme.primary_file, context=context, media_render_service=media_render_service)
            if meme.primary_file is not None
            else None
        ),
        files=[
            _to_public_file_read(file, context=context, media_render_service=media_render_service)
            for file in ordered_files
        ],
        created_at=meme.created_at,
        updated_at=_latest_datetime(
            meme.updated_at,
            seo_page.generated_at if seo_page is not None else None,
            seo_page.edited_at if seo_page is not None else None,
            meme.template.updated_at if meme.template is not None else None,
            *(file.updated_at for file in ordered_files),
        )
        or meme.updated_at,
    )


def _to_public_file_read(
    file: MemeFile,
    *,
    context: PublicMediaRenderContext,
    media_render_service: MediaRenderUrlService,
) -> PublicMemeFileRead:
    return PublicMemeFileRead(
        id=file.id,
        mime_type=file.mime_type,
        width=file.width,
        height=file.height,
        file_size_bytes=file.file_size_bytes,
        blur_hash=file.blur_hash,
        quality_score=file.quality_score,
        render=media_render_service.build_render(file, context=context),
    )


def _template_ref_read(template: MemeTemplate) -> SeoCatalogMemeTemplateRefRead:
    return SeoCatalogMemeTemplateRefRead(
        slug=template.slug,
        name=template.name,
        title=_template_title(template.name),
        description=template.description,
    )


def _ordered_files(meme: Meme) -> list[MemeFile]:
    by_id = {file.id: file for file in meme.files}
    if meme.primary_file is not None:
        by_id[meme.primary_file.id] = meme.primary_file
    return sorted(by_id.values(), key=lambda file: (file.id != meme.primary_file_id, str(file.id)))


def _derived_popularity_score(meme: Meme) -> float:
    raw_value = getattr(meme, _DERIVED_POPULARITY_ATTR, 0.0)
    return float(raw_value or 0.0)


def _tag_slugs(tags: list[str]) -> list[str]:
    slugs: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        slug = _clean_text(tag)
        if slug is None:
            continue
        slug = slug.lower()
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def _tag_title(slug: str) -> str:
    return f"{slug.replace('-', ' ').replace('_', ' ').title()} memes"


def _template_title(name: str) -> str:
    return f"{name} memes"


def _first_text(*values: str | None) -> str:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return "Meme"


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _excerpt(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    trimmed = value[: max_length - 3].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", maxsplit=1)[0]
    return f"{trimmed}..."


def _latest_datetime(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _clamp_limit(limit: int, *, max_limit: int = MAX_CATALOG_LIMIT) -> int:
    return min(max_limit, max(1, limit))


def _has_more(*, total: int, limit: int, offset: int) -> bool:
    return offset + limit < total


__all__ = [
    "DEFAULT_CATALOG_LIMIT",
    "DEFAULT_PINTEREST_FEED_LIMIT",
    "MAX_CATALOG_LIMIT",
    "MAX_PINTEREST_FEED_LIMIT",
    "SeoCatalogService",
]
