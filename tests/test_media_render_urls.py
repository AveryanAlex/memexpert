"""Tests for public media render URL contract construction."""

from __future__ import annotations

import uuid

from memexpert.core.config import Settings
from memexpert.models.content import MemeFile
from memexpert.services.media_render_urls import MediaRenderUrlService, PublicMediaRenderContext


def test_imgproxy_signed_image_urls_have_safe_shape_and_download_filename() -> None:
    file_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    settings = Settings.model_validate(
        {
            "imgproxy_base_url": "https://img.memexpert.test/",
            "imgproxy_key": "00112233445566778899aabbccddeeff",
            "imgproxy_salt": "ffeeddccbbaa99887766554433221100",
            "s3_bucket": "private-media-bucket",
        }
    )
    file = MemeFile(
        id=file_id,
        meme_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        s3_original_key="pipeline/originals/secret/raw-object.jpg",
        mime_type="image/jpeg",
        width=640,
        height=480,
        blur_hash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        quality_score=0.9,
        is_primary=True,
    )

    render = MediaRenderUrlService(settings).build_render(
        file,
        context=PublicMediaRenderContext(
            meme_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            seo_slug="Frog Wizard",
        ),
    )

    assert render.thumbnail_url is not None
    assert render.thumbnail_url.startswith("https://img.memexpert.test/")
    assert "/unsafe/" not in render.thumbnail_url
    assert "/rs:fill:360:360/" in render.thumbnail_url
    assert render.thumbnail_url.endswith(".webp")
    assert "@webp" not in render.thumbnail_url
    assert render.preview_url == render.display_url
    assert render.download_url is not None
    assert "att:1" in render.download_url
    assert "fn:ZnJvZy13aXphcmQuanBn:1" in render.download_url
    assert render.download_url.endswith(".jpg")
    assert "@jpg" not in render.download_url
    serialized_urls = " ".join(
        url
        for url in [render.thumbnail_url, render.preview_url, render.original_url, render.download_url]
        if url is not None
    )
    assert "private-media-bucket" not in serialized_urls
    assert "pipeline/originals/secret/raw-object.jpg" not in serialized_urls


def test_imgproxy_unsigned_dev_mode_only_when_key_and_salt_are_absent() -> None:
    settings = Settings.model_validate({"imgproxy_base_url": "http://localhost:8080"})
    file = MemeFile(
        id=uuid.UUID("11111111-1111-4111-8111-111111111112"),
        meme_id=uuid.UUID("22222222-2222-4222-8222-222222222223"),
        s3_original_key="memes/dev.png",
        mime_type="image/png",
        quality_score=0.8,
        is_primary=True,
    )

    render = MediaRenderUrlService(settings).build_render(
        file,
        context=PublicMediaRenderContext(meme_id=uuid.UUID("22222222-2222-4222-8222-222222222223")),
    )

    assert render.thumbnail_url is not None
    assert render.thumbnail_url.startswith("http://localhost:8080/unsafe/")


def test_web_video_uses_public_media_base_and_never_imgproxy() -> None:
    file_id = uuid.UUID("11111111-1111-4111-8111-111111111113")
    settings = Settings.model_validate(
        {
            "imgproxy_base_url": "https://img.memexpert.test",
            "media_public_base_url": "https://media.memexpert.test/files/",
        }
    )
    file = MemeFile(
        id=file_id,
        meme_id=uuid.UUID("22222222-2222-4222-8222-222222222224"),
        s3_original_key="pipeline/originals/video-source.mov",
        s3_web_video_key="pipeline/derived/secret/web.mp4",
        mime_type="video/mp4",
        quality_score=0.7,
        is_primary=True,
    )

    render = MediaRenderUrlService(settings).build_render(
        file,
        context=PublicMediaRenderContext(meme_id=uuid.UUID("22222222-2222-4222-8222-222222222224")),
    )

    assert render.thumbnail_url is None
    assert render.preview_url is None
    assert render.web_video_url == f"https://media.memexpert.test/files/{file_id}/web-video.mp4"
    assert render.download_url == render.web_video_url
    web_video_url = render.web_video_url
    assert web_video_url is not None
    assert "img.memexpert.test" not in web_video_url
    assert "pipeline/derived/secret/web.mp4" not in web_video_url


def test_web_video_returns_null_without_public_media_base() -> None:
    file = MemeFile(
        id=uuid.UUID("11111111-1111-4111-8111-111111111114"),
        meme_id=uuid.UUID("22222222-2222-4222-8222-222222222225"),
        s3_original_key="pipeline/originals/video-source.mov",
        s3_web_video_key="pipeline/derived/secret/web.mp4",
        mime_type="video/mp4",
        quality_score=0.7,
        is_primary=True,
    )

    render = MediaRenderUrlService(Settings()).build_render(
        file,
        context=PublicMediaRenderContext(meme_id=uuid.UUID("22222222-2222-4222-8222-222222222225")),
    )

    assert render.web_video_url is None
    assert render.download_url is None


def test_private_image_render_uses_authenticated_api_variants() -> None:
    file_id = uuid.UUID("11111111-1111-4111-8111-111111111115")
    file = MemeFile(
        id=file_id,
        meme_id=uuid.UUID("22222222-2222-4222-8222-222222222226"),
        s3_original_key="pipeline/originals/private/upload.png",
        mime_type="image/png",
        width=800,
        height=600,
        blur_hash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        quality_score=0.8,
        is_primary=True,
    )

    render = MediaRenderUrlService(Settings()).build_private_render(file)

    assert render.thumbnail_url == f"/api/v1/media/files/{file_id}/thumbnail"
    assert render.preview_url == f"/api/v1/media/files/{file_id}/preview"
    assert render.display_url == render.preview_url
    assert render.original_url == f"/api/v1/media/files/{file_id}/original"
    assert render.download_url == f"/api/v1/media/files/{file_id}/download"
    assert render.width == 800
    assert render.height == 600
    assert render.blur_hash == "LEHV6nWB2yk8pyo0adR*.7kCMdnj"
    serialized_urls = " ".join(
        url
        for url in [render.thumbnail_url, render.preview_url, render.original_url, render.download_url]
        if url is not None
    )
    assert "pipeline/originals/private/upload.png" not in serialized_urls


def test_private_web_video_uses_authenticated_direct_variant_without_imgproxy() -> None:
    file_id = uuid.UUID("11111111-1111-4111-8111-111111111116")
    file = MemeFile(
        id=file_id,
        meme_id=uuid.UUID("22222222-2222-4222-8222-222222222227"),
        s3_original_key="pipeline/originals/private/source.mov",
        s3_web_video_key="pipeline/derived/private/web.mp4",
        mime_type="video/mp4",
        quality_score=0.7,
        is_primary=True,
    )

    render = MediaRenderUrlService(Settings.model_validate({"imgproxy_base_url": "https://img.memexpert.test"})).build_private_render(file)

    assert render.thumbnail_url is None
    assert render.preview_url is None
    assert render.web_video_url == f"/api/v1/media/files/{file_id}/web-video.mp4"
    assert render.download_url == render.web_video_url
    web_video_url = render.web_video_url
    assert web_video_url is not None
    assert "img.memexpert.test" not in web_video_url
    assert "pipeline/derived/private/web.mp4" not in web_video_url
