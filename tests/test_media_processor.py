"""Tests for worker media normalization behavior."""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from memexpert.core.config import Settings
from memexpert.media.contracts import CommandResult
from memexpert.media.inspect import PipelineMediaProcessor


@dataclass(slots=True)
class RecordingCommandRunner:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, args: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        _ = timeout_seconds
        self.calls.append(args)
        raise AssertionError(f"static image normalization must not run media command: {args!r}")


@dataclass(slots=True)
class MovingMediaCommandRunner:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, args: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        _ = timeout_seconds
        self.calls.append(args)
        if "-show_entries" in args:
            return CommandResult(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [{"codec_type": "video", "width": 32, "height": 24}],
                        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                    }
                ).encode(),
                stderr=b"",
            )

        Path(args[-1]).write_bytes(b"normalized-web-video")
        return CommandResult(args=args, returncode=0, stdout=b"", stderr=b"")


def _jpeg_bytes() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (32, 24), color=(120, 80, 40))
    image.save(output, format="JPEG")
    return output.getvalue()


def _gif_bytes() -> bytes:
    output = io.BytesIO()
    first = Image.new("RGB", (32, 24), color=(120, 80, 40))
    second = Image.new("RGB", (32, 24), color=(40, 80, 120))
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
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


@pytest.mark.asyncio
async def test_gif_normalization_keeps_first_frame_as_durable_preview_image() -> None:
    runner = MovingMediaCommandRunner()
    processor = PipelineMediaProcessor(settings=Settings(), command_runner=runner)
    meme_file_id = uuid.UUID("11111111-1111-7111-8111-111111111112")

    result = await processor.normalize_for_web(
        meme_file_id=meme_file_id,
        filename="animated.gif",
        content_type="image/gif",
        media_bytes=_gif_bytes(),
    )

    assert result.preview_image_object_key == f"pipeline/derived/{meme_file_id}/preview.png"
    assert result.preview_image_bytes is not None
    with Image.open(io.BytesIO(result.preview_image_bytes)) as preview:
        assert preview.format == "PNG"
        assert preview.size == (32, 24)
    assert result.web_video_object_key == f"pipeline/derived/{meme_file_id}/web.mp4"
    assert result.web_video_bytes == b"normalized-web-video"
    assert len(runner.calls) == 2
