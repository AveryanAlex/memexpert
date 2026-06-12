"""Unit tests for deterministic fake pipeline providers."""

from __future__ import annotations

from io import BytesIO
from typing import Any, cast

import pytest
from PIL import Image

from memexpert.api.dependencies.meme import get_meme_search_service
from memexpert.core.classification import (
    FakeClassificationClient,
    PipelineClassificationClient,
    build_pipeline_classification_client,
)
from memexpert.core.config import Settings, get_settings
from memexpert.core.ocr import FakeOCRProcessor, PipelineOCRProcessor, build_pipeline_ocr_processor
from memexpert.core.voyage import FakeVoyageClient, PipelineVoyageClient, build_pipeline_voyage_client
from memexpert.models.enums import ContentLanguage
from memexpert.services.query_embedding import CachedTextQueryEmbeddingService


@pytest.mark.asyncio
async def test_fake_ocr_factory_returns_deterministic_english_result() -> None:
    settings = Settings(
        pipeline_ocr_provider_mode="fake",
        pipeline_fake_ocr_text="cat fixture text",
    )

    processor = build_pipeline_ocr_processor(settings=settings)
    result = await processor.extract_text(
        filename="cat.png",
        mime_type="image/png",
        media_bytes=_png_bytes((255, 0, 0)),
        source_object_key="pipeline/originals/cat.png",
    )

    assert isinstance(processor, FakeOCRProcessor)
    assert result.extracted_text == "cat fixture text"
    assert result.confidence == 1.0
    assert result.language is ContentLanguage.EN


@pytest.mark.asyncio
async def test_fake_voyage_maps_text_and_png_markers_to_stable_vectors() -> None:
    settings = Settings(
        pipeline_voyage_provider_mode="fake",
        pipeline_voyage_output_dimensions=5,
    )
    client = build_pipeline_voyage_client(settings=settings)

    cat = await client.embed_text(text="cat query")
    dog = await client.embed_image(image_bytes=_png_bytes((0, 0, 255)), mime_type="image/png")
    frog = await client.embed_image(image_bytes=_png_bytes((0, 255, 0)), mime_type="image/png")
    other = await client.embed_text(text="other query")
    unknown_a = await client.embed_text(text="mystery query")
    unknown_b = await client.embed_text(text="mystery query")

    assert isinstance(client, FakeVoyageClient)
    assert cat.vector == (1.0, 0.0, 0.0, 0.0, 0.0)
    assert dog.vector == (0.0, 1.0, 0.0, 0.0, 0.0)
    assert frog.vector == (0.0, 0.0, 1.0, 0.0, 0.0)
    assert other.vector == frog.vector
    assert unknown_a.vector == unknown_b.vector
    assert sum(unknown_a.vector) == 1.0


@pytest.mark.asyncio
async def test_fake_classification_factory_returns_safe_score() -> None:
    settings = Settings(
        pipeline_classification_provider_mode="fake",
        pipeline_fake_classification_nsfw_score=0.2,
        pipeline_classification_nsfw_threshold=0.5,
    )

    client = build_pipeline_classification_client(settings=settings)
    result = await client.classify_image(image_bytes=_png_bytes((255, 0, 0)), mime_type="image/png")

    assert isinstance(client, FakeClassificationClient)
    assert result.nsfw_score == 0.2
    assert result.is_nsfw is False


def test_meme_search_dependency_uses_fake_voyage_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PIPELINE_VOYAGE_PROVIDER_MODE", "fake")
    try:
        service = get_meme_search_service(cast("Any", object()))

        assert isinstance(service._query_embedding_client, CachedTextQueryEmbeddingService)
        assert isinstance(service._query_embedding_client._provider, FakeVoyageClient)
    finally:
        get_settings.cache_clear()


def test_provider_factories_keep_live_defaults() -> None:
    settings = Settings(pipeline_voyage_output_dimensions=7)

    voyage_client = build_pipeline_voyage_client(settings=settings)

    assert isinstance(build_pipeline_ocr_processor(settings=settings), PipelineOCRProcessor)
    assert isinstance(voyage_client, PipelineVoyageClient)
    assert isinstance(build_pipeline_classification_client(settings=settings), PipelineClassificationClient)
    assert voyage_client._build_text_request_payload(text="cat query")["input_type"] == "query"


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", (8, 8), color=color)
    image.save(output, format="PNG")
    return output.getvalue()
