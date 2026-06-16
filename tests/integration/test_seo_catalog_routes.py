"""Integration tests for public safe SEO catalog API routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from memexpert.api.dependencies.meme import get_seo_catalog_service
from memexpert.models.content import Meme, MemeFile, MemeSeoPage, MemeTemplate
from memexpert.models.enums import ContentKind, ContentLanguage
from memexpert.services.seo_catalog import SeoCatalogService

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _create_meme(
    session: AsyncSession,
    *,
    template: MemeTemplate | None = None,
    tags: list[str] | None = None,
    is_public: bool = True,
    is_nsfw: bool = False,
    popularity_score: float = 0.0,
    like_count: int = 0,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    seo_slug: str | None = None,
    seo_title: str | None = None,
    seo_description: str | None = None,
    seo_alt_text: str | None = None,
    ocr_text: str | None = None,
) -> Meme:
    meme_kwargs: dict[str, object] = {
        "media_type": ContentKind.IMAGE,
        "language": ContentLanguage.EN,
        "tags": tags or [],
        "is_public": is_public,
        "is_nsfw": is_nsfw,
        "popularity_score": popularity_score,
        "like_count": like_count,
        "template_id": template.id if template is not None else None,
        "ocr_text": ocr_text,
    }
    if created_at is not None:
        meme_kwargs["created_at"] = created_at
    if updated_at is not None:
        meme_kwargs["updated_at"] = updated_at
    meme = Meme(**meme_kwargs)
    session.add(meme)
    await session.flush()
    file_kwargs: dict[str, object] = {
        "meme_id": meme.id,
        "s3_original_key": f"memes/{meme.id}.jpg",
        "mime_type": "image/jpeg",
        "width": 640,
        "height": 480,
        "file_size_bytes": 12345,
        "blur_hash": "LKO2?U%2Tw=w]~RBVZRi};RPxuwH",
        "quality_score": 0.9,
        "is_primary": True,
    }
    if created_at is not None:
        file_kwargs["created_at"] = created_at
    if updated_at is not None:
        file_kwargs["updated_at"] = updated_at
    file = MemeFile(**file_kwargs)
    session.add(file)
    await session.flush()
    meme.primary_file_id = file.id
    if seo_slug is not None:
        session.add(
            MemeSeoPage(
                meme_id=meme.id,
                slug=seo_slug,
                page_title=seo_title or f"SEO title {seo_slug}",
                meta_description=seo_description or f"SEO description {seo_slug}",
                alt_text=seo_alt_text or f"SEO alt {seo_slug}",
                caption=f"Caption {seo_slug}",
                tags=tags or [],
                model_id="test-seo-model",
                prompt_version="test-v1",
                generated_at=updated_at or datetime.now(UTC),
            )
        )
    await session.flush()
    return meme


async def _create_template(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str | None = None,
    updated_at: datetime | None = None,
) -> MemeTemplate:
    template_kwargs: dict[str, object] = {"slug": slug, "name": name, "description": description}
    if updated_at is not None:
        template_kwargs["updated_at"] = updated_at
    template = MemeTemplate(**template_kwargs)
    session.add(template)
    await session.flush()
    return template


def _install_seo_route_overrides(app: FastAPI, session: AsyncSession) -> None:
    def override_seo_catalog_service() -> SeoCatalogService:
        return SeoCatalogService(session)

    app.dependency_overrides[get_seo_catalog_service] = override_seo_catalog_service


async def test_seo_meme_route_returns_only_public_sfw_with_safe_media_fields(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    template = await _create_template(
        migrated_db_session,
        slug="distracted-boyfriend",
        name="Distracted Boyfriend",
        description="Template description",
    )
    public_meme = await _create_meme(
        migrated_db_session,
        template=template,
        tags=["Funny", "cats"],
        popularity_score=42.0,
        like_count=7,
        seo_slug="funny-cat-meme",
        seo_title="Funny cat meme",
        seo_description="A public safe funny cat meme.",
        seo_alt_text="Cat looking surprised",
    )
    private_meme = await _create_meme(migrated_db_session, tags=["private"], is_public=False, seo_slug="private-meme")
    nsfw_meme = await _create_meme(migrated_db_session, tags=["adult"], is_nsfw=True, seo_slug="nsfw-meme")
    _install_seo_route_overrides(app, migrated_db_session)

    try:
        response = await client.get("/api/v1/seo/memes", params={"limit": 10})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["has_more"] is False
    assert [item["id"] for item in payload["items"]] == [str(public_meme.id)]
    assert str(private_meme.id) not in {item["id"] for item in payload["items"]}
    assert str(nsfw_meme.id) not in {item["id"] for item in payload["items"]}

    item = payload["items"][0]
    assert item["seo_slug"] == "funny-cat-meme"
    assert item["title"] == "Funny cat meme"
    assert item["description"] == "A public safe funny cat meme."
    assert item["alt_text"] == "Cat looking surprised"
    assert item["tags"] == ["funny", "cats"]
    assert item["template"] == {
        "slug": "distracted-boyfriend",
        "name": "Distracted Boyfriend",
        "title": "Distracted Boyfriend memes",
        "description": "Template description",
    }
    assert item["primary_file"]["mime_type"] == "image/jpeg"
    assert item["primary_file"]["file_size_bytes"] == 12345
    assert item["primary_file"]["render"]["display_url"].startswith("http://localhost:8080/unsafe/")
    assert item["primary_file"]["render"]["width"] == 640
    assert "s3_original_key" not in item["primary_file"]
    assert item["files"][0]["id"] == item["primary_file"]["id"]


async def test_seo_summary_tags_and_templates_count_only_public_sfw_memes(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    public_template = await _create_template(migrated_db_session, slug="public-template", name="Public Template")
    private_only_template = await _create_template(
        migrated_db_session,
        slug="private-template",
        name="Private Template",
    )
    await _create_meme(migrated_db_session, template=public_template, tags=["cats", "funny"], seo_slug="cat-one")
    await _create_meme(migrated_db_session, template=public_template, tags=["cats"], seo_slug="cat-two")
    await _create_meme(
        migrated_db_session,
        template=private_only_template,
        tags=["cats", "private"],
        is_public=False,
        seo_slug="private-cat",
    )
    await _create_meme(migrated_db_session, tags=["adult"], is_nsfw=True, seo_slug="adult-cat")
    _install_seo_route_overrides(app, migrated_db_session)

    try:
        summary_response = await client.get("/api/v1/seo/summary")
        tags_response = await client.get("/api/v1/seo/tags", params={"limit": 1})
        templates_response = await client.get("/api/v1/seo/templates", params={"limit": 10})
    finally:
        app.dependency_overrides.clear()

    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["public_safe_meme_count"] == 2
    assert summary["tag_count"] == 2
    assert summary["template_count"] == 1
    assert summary["updated_at"] is not None

    assert tags_response.status_code == 200
    tags_payload = tags_response.json()
    assert tags_payload["total"] == 2
    assert tags_payload["limit"] == 1
    assert tags_payload["has_more"] is True
    assert tags_payload["items"] == [
        {
            "slug": "cats",
            "title": "Cats memes",
            "description": "Public safe memes tagged cats.",
            "meme_count": 2,
            "updated_at": tags_payload["items"][0]["updated_at"],
        }
    ]

    assert templates_response.status_code == 200
    templates_payload = templates_response.json()
    assert templates_payload["total"] == 1
    assert templates_payload["has_more"] is False
    assert templates_payload["items"][0]["slug"] == "public-template"
    assert templates_payload["items"][0]["name"] == "Public Template"
    assert templates_payload["items"][0]["meme_count"] == 2


async def test_seo_pinterest_feed_uses_small_default_and_deterministic_recent_popular_order(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    older = await _create_meme(
        migrated_db_session,
        tags=["older"],
        popularity_score=100.0,
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        seo_slug="older-popular",
    )
    newest_low_popularity = await _create_meme(
        migrated_db_session,
        tags=["new"],
        popularity_score=1.0,
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
        seo_slug="new-low-popularity",
    )
    newest_high_popularity = await _create_meme(
        migrated_db_session,
        tags=["new"],
        popularity_score=10.0,
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
        seo_slug="new-high-popularity",
    )
    await _create_meme(
        migrated_db_session,
        tags=["hidden"],
        popularity_score=1000.0,
        is_nsfw=True,
        created_at=now,
        updated_at=now,
        seo_slug="hidden-nsfw",
    )
    _install_seo_route_overrides(app, migrated_db_session)

    try:
        response = await client.get("/api/v1/seo/pinterest-feed")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 100
    assert payload["total"] == 3
    assert [item["id"] for item in payload["items"]] == [
        str(newest_high_popularity.id),
        str(newest_low_popularity.id),
        str(older.id),
    ]


async def test_seo_meme_limit_is_bounded_by_route_validation(
    app: FastAPI,
    client: AsyncClient,
    migrated_db_session: AsyncSession,
) -> None:
    _install_seo_route_overrides(app, migrated_db_session)

    try:
        response = await client.get("/api/v1/seo/memes", params={"limit": 50001})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
