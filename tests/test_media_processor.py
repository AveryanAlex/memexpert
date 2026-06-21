"""Tests for worker media normalization behavior."""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from memexpert.core.config import Settings
from memexpert.media.inspect import PipelineMediaProcessor

if TYPE_CHECKING:
    from memexpert.media.contracts import CommandResult


@dataclass(slots=True)
class RecordingCommandRunner:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, args: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        _ = timeout_seconds
        self.calls.append(args)
        raise AssertionError(f"static image normalization must not run media command: {args!r}")


def _jpeg_bytes() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (32, 24), color=(120, 80, 40))
    image.save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_static_jpeg_normalization_does_not_create_web_video_or_run_ffmpeg() -> None:
    runner = RecordingCommandRunner()
    processor = PipelineMediaProcessor(settings=Settings(), command_runner=runner)

    result = await processor.normalize_for_web(
        meme_file_id=uuid.UUID("11111111-1111-7111-8111-111111111111"),
        filename="static.jpg",
        content_type="image/jpeg",
        media_bytes=_jpeg_bytes(),
    )

    assert result.web_video_object_key is None
    assert result.web_video_bytes is None
    assert 0.0 <= result.quality_score <= 1.0
    assert result.blur_hash is not None
    assert runner.calls == []
