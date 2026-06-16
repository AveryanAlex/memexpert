"""Focused tests for live meme SEO provider image inputs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai import BinaryContent

from memexpert.core.config import Settings
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import ContentKind, ContentLanguage, ContentProcessingStatus
from memexpert.services import meme_seo as meme_seo_module
from memexpert.services.meme_seo import (
    MemeSeoImageInput,
    MemeSeoProviderResult,
    PydanticAIMemeSeoProvider,
    S3MemeSeoImageInputResolver,
)

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True, slots=True)
class FakeRunResult:
    output: object


class CapturingAgent:
    def __init__(self) -> None:
        self.prompts: list[object] = []

    async def run(self, prompt: object) -> FakeRunResult:
        self.prompts.append(prompt)
        return FakeRunResult(
            {
                "page_title": "Frog Wizard Reaction",
                "meta_description": "A frog wizard reaction meme.",
                "alt_text": "Frog wizard reaction image",
                "slug": "frog-wizard",
                "tags": ["frog", "wizard"],
            },
        )


class FixedImageResolver:
    def __init__(self, image_input: MemeSeoImageInput | None) -> None:
        self._image_input = image_input

    async def resolve(self, meme: Meme) -> MemeSeoImageInput | None:
        return self._image_input


class FailingImageResolver:
    async def resolve(self, meme: Meme) -> MemeSeoImageInput | None:
        raise RuntimeError("https://signed-storage.example/private?X-Amz-Signature=secret")


class RecordingDownloader:
    def __init__(self, payload: bytes | None = None, error: Exception | None = None) -> None:
        self._payload = payload or b""
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, client: Any, *, bucket: str, key: str) -> bytes:
        self.calls.append({"client": client, "bucket": bucket, "key": key})
        if self._error is not None:
            raise self._error
        return self._payload


async def _never_download(client: Any, *, bucket: str, key: str) -> bytes:
    raise AssertionError("download should not be called for ineligible SEO image inputs")


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "pipeline_seo_api_key": "openai-test-secret",
        "pipeline_seo_image_max_bytes": 16,
        "s3_endpoint": "https://storage.internal.example",
        "s3_access_key": "ACCESS-KEY-SECRET",
        "s3_secret_key": "SECRET-KEY-VALUE",
        "s3_bucket": "memexpert-test",
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def _meme(
    *,
    primary_file: bool = True,
    mime_type: str | None = "image/png",
    file_size_bytes: int | None = 8,
    s3_original_key: str = "pipeline/originals/private-object-key/original.png",
) -> Meme:
    meme = Meme(
        id=uuid.uuid4(),
        media_type=ContentKind.IMAGE,
        language=ContentLanguage.EN,
        tags=["frog", "wizard"],
        ocr_text="frog wizard text",
        is_public=True,
    )
    if not primary_file:
        return meme

    meme_file = MemeFile(
        id=uuid.uuid4(),
        meme_id=meme.id,
        status=ContentProcessingStatus.READY,
        s3_original_key=s3_original_key,
        mime_type=mime_type,
        file_size_bytes=file_size_bytes,
        width=640,
        height=480,
        quality_score=0.9,
        is_primary=True,
    )
    meme.primary_file_id = meme_file.id
    meme.primary_file = meme_file
    return meme


async def test_live_provider_attaches_image_bytes_without_leaking_storage_identifiers() -> None:
    agent = CapturingAgent()
    provider = PydanticAIMemeSeoProvider(
        settings=_settings(),
        image_input_resolver=FixedImageResolver(MemeSeoImageInput(data=b"image-bytes", media_type="image/png")),
        agent=agent,
    )

    result = await provider.generate(_meme())

    assert isinstance(result, MemeSeoProviderResult)
    assert len(agent.prompts) == 1
    prompt = agent.prompts[0]
    assert isinstance(prompt, list)
    assert len(prompt) == 2
    prompt_text, binary = prompt
    assert isinstance(prompt_text, str)
    assert isinstance(binary, BinaryContent)
    assert binary.data == b"image-bytes"
    assert binary.media_type == "image/png"
    assert '"image_input_status": "attached"' in prompt_text
    assert '"mime_type": "image/png"' in prompt_text
    assert '"width": 640' in prompt_text
    assert '"height": 480' in prompt_text

    model_input_text = repr(prompt)
    for forbidden in (
        "pipeline/originals",
        "private-object-key",
        "storage.internal.example",
        "ACCESS-KEY-SECRET",
        "SECRET-KEY-VALUE",
        "X-Amz-Signature",
        "signed-storage.example",
        "s3_original_key",
    ):
        assert forbidden not in model_input_text


@pytest.mark.parametrize(
    "resolver",
    [
        FixedImageResolver(None),
        FixedImageResolver(MemeSeoImageInput(data=b"image-bytes", media_type="image/svg+xml")),
        FixedImageResolver(MemeSeoImageInput(data=b"0123456789", media_type="image/png")),
        FailingImageResolver(),
    ],
)
async def test_live_provider_remains_text_only_when_image_input_is_unavailable_or_unsafe(
    resolver: FixedImageResolver | FailingImageResolver,
) -> None:
    agent = CapturingAgent()
    provider = PydanticAIMemeSeoProvider(
        settings=_settings(pipeline_seo_image_max_bytes=8),
        image_input_resolver=resolver,
        agent=agent,
    )

    result = await provider.generate(_meme())

    assert isinstance(result, MemeSeoProviderResult)
    assert len(agent.prompts) == 1
    prompt = agent.prompts[0]
    assert isinstance(prompt, str)
    assert '"image_input_status": "not_attached"' in prompt
    assert "pipeline/originals" not in prompt
    assert "X-Amz-Signature" not in prompt


async def test_s3_image_resolver_downloads_supported_primary_image_with_cap() -> None:
    s3_client = object()
    downloader = RecordingDownloader(payload=b"jpeg-bytes")
    resolver = S3MemeSeoImageInputResolver(settings=_settings(), s3_client=s3_client, downloader=downloader)

    image_input = await resolver.resolve(_meme(mime_type=" Image/JPEG ", file_size_bytes=10))

    assert image_input == MemeSeoImageInput(data=b"jpeg-bytes", media_type="image/jpeg")
    assert downloader.calls == [
        {
            "client": s3_client,
            "bucket": "memexpert-test",
            "key": "pipeline/originals/private-object-key/original.png",
        },
    ]


async def test_s3_image_resolver_builds_client_from_injected_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(s3_bucket="custom-bucket")
    s3_client = object()
    built_with_settings: list[Settings] = []

    def build_fake_s3_client(client_settings: Settings) -> object:
        built_with_settings.append(client_settings)
        return s3_client

    monkeypatch.setattr(meme_seo_module, "build_s3_client", build_fake_s3_client)
    downloader = RecordingDownloader(payload=b"png-bytes")
    resolver = S3MemeSeoImageInputResolver(settings=settings, downloader=downloader)

    image_input = await resolver.resolve(_meme(mime_type="image/png", file_size_bytes=8))

    assert image_input == MemeSeoImageInput(data=b"png-bytes", media_type="image/png")
    assert built_with_settings == [settings]
    assert downloader.calls == [
        {
            "client": s3_client,
            "bucket": "custom-bucket",
            "key": "pipeline/originals/private-object-key/original.png",
        },
    ]


@pytest.mark.parametrize(
    "meme",
    [
        _meme(primary_file=False),
        _meme(mime_type="video/mp4"),
        _meme(file_size_bytes=17),
    ],
)
async def test_s3_image_resolver_skips_missing_non_image_and_recorded_oversized_primary_media(meme: Meme) -> None:
    resolver = S3MemeSeoImageInputResolver(settings=_settings(), s3_client=object(), downloader=_never_download)

    assert await resolver.resolve(meme) is None


@pytest.mark.parametrize(
    "downloader",
    [
        RecordingDownloader(payload=b"downloaded-bytes-over-cap"),
        RecordingDownloader(error=RuntimeError("storage-secret-failure")),
    ],
)
async def test_s3_image_resolver_skips_downloaded_oversized_and_storage_failures(
    downloader: RecordingDownloader,
) -> None:
    resolver = S3MemeSeoImageInputResolver(settings=_settings(), s3_client=object(), downloader=downloader)

    assert await resolver.resolve(_meme(file_size_bytes=4)) is None
    assert len(downloader.calls) == 1
