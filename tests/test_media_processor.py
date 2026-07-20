"""Tests for worker media normalization behavior."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import struct
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

import memexpert.media.inspect as media_inspect_module
from memexpert.core.config import Settings
from memexpert.media.contracts import (
    WEB_VIDEO_PROFILE_ID,
    CommandResult,
    MediaTimeoutError,
    WebVideoFrameRateMode,
)
from memexpert.media.inspect import PipelineMediaProcessor, SubprocessMediaCommandRunner


class PendingSubprocess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.communicate_started = asyncio.Event()
        self._communicate_release = asyncio.Event()
        self.kill_calls = 0
        self.wait_calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        await self._communicate_release.wait()
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = -9
        return self.returncode


class UnreapableSubprocess(PendingSubprocess):
    async def wait(self) -> int:
        self.wait_calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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
    source_average_frame_rate: str = "10/1"
    source_real_frame_rate: str = "10/1"
    output_frame_rate: str = "10/1"
    output_video_packets: tuple[tuple[str, str], ...] = (("0.0", "1000"), ("0.1", "1000"))
    include_audio: bool = False
    include_output_video_timing: bool = True
    include_output_audio_timing: bool = True
    output_audio_start_time: str = "0.0"
    output_audio_duration: str = "0.2"

    async def run(self, args: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        _ = timeout_seconds
        self.calls.append(args)
        target = Path(args[-1])
        if "-show_packets" in args:
            return CommandResult(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "packets": [
                            {"dts_time": timestamp, "pts_time": timestamp, "size": size}
                            for timestamp, size in self.output_video_packets
                        ]
                    }
                ).encode(),
                stderr=b"",
            )
        if "-show_streams" in args:
            is_output = target.name == "web.mp4"
            video_stream = {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264" if is_output else "gif",
                "profile": "High" if is_output else None,
                "level": 41 if is_output else None,
                "pix_fmt": "yuv420p" if is_output else "bgra",
                "width": 32,
                "height": 24,
                "avg_frame_rate": self.output_frame_rate if is_output else self.source_average_frame_rate,
                "r_frame_rate": self.output_frame_rate if is_output else self.source_real_frame_rate,
                "bit_rate": "100000",
                "nb_frames": "2",
            }
            if not is_output or self.include_output_video_timing:
                video_stream.update({"start_time": "0.0", "duration": "0.2"})
            streams = [video_stream]
            if self.include_audio:
                audio_stream = {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac" if is_output else "opus",
                    "profile": "LC" if is_output else None,
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "bit_rate": "128000" if is_output else "96000",
                }
                if not is_output or self.include_output_audio_timing:
                    audio_stream.update(
                        {
                            "start_time": self.output_audio_start_time if is_output else "0.0",
                            "duration": self.output_audio_duration if is_output else "0.2",
                        }
                    )
                streams.append(audio_stream)
            return CommandResult(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": streams,
                        "format": {
                            "format_name": "mov,mp4,m4a,3gp,3g2,mj2" if is_output else "gif",
                            "format_long_name": "QuickTime / MOV"
                            if is_output
                            else "CompuServe Graphics Interchange Format",
                            "start_time": "0.0",
                            "duration": "0.2",
                            "bit_rate": "110000",
                        },
                        "chapters": [],
                    }
                ).encode(),
                stderr=b"",
            )

        if target.name == "web.mp4":
            target.write_bytes(_minimal_faststart_mp4())
        elif target.name == "preview.png":
            target.write_bytes(_png_bytes())
        else:
            raise AssertionError(f"unexpected media output path: {target}")
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


def _png_bytes(*, width: int = 32, height: int = 24) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(120, 80, 40)).save(output, format="PNG")
    return output.getvalue()


def _minimal_faststart_mp4() -> bytes:
    def box(name: bytes, payload: bytes = b"") -> bytes:
        return struct.pack(">I4s", len(payload) + 8, name) + payload

    return box(b"ftyp", b"isom0000") + box(b"moov") + box(b"mdat")


@pytest.mark.asyncio
async def test_media_command_timeout_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = PendingSubprocess()

    async def create_subprocess(*_args: object, **_kwargs: object) -> PendingSubprocess:
        return process

    monkeypatch.setattr(media_inspect_module.asyncio, "create_subprocess_exec", create_subprocess)

    with pytest.raises(MediaTimeoutError):
        await SubprocessMediaCommandRunner().run(("ffmpeg", "input.mp4"), timeout_seconds=0.01)

    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_media_command_cancellation_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = PendingSubprocess()

    async def create_subprocess(*_args: object, **_kwargs: object) -> PendingSubprocess:
        return process

    monkeypatch.setattr(media_inspect_module.asyncio, "create_subprocess_exec", create_subprocess)
    command_task = asyncio.create_task(SubprocessMediaCommandRunner().run(("ffmpeg", "input.mp4"), timeout_seconds=5.0))
    await asyncio.wait_for(process.communicate_started.wait(), timeout=1.0)

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command_task

    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_media_command_cancellation_bounds_reaping_an_unresponsive_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = UnreapableSubprocess()

    async def create_subprocess(*_args: object, **_kwargs: object) -> UnreapableSubprocess:
        return process

    monkeypatch.setattr(media_inspect_module.asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(media_inspect_module, "_SUBPROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    command_task = asyncio.create_task(SubprocessMediaCommandRunner().run(("ffmpeg", "input.mp4"), timeout_seconds=5.0))
    await asyncio.wait_for(process.communicate_started.wait(), timeout=1.0)

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(command_task, timeout=0.2)

    assert process.kill_calls == 1
    assert process.wait_calls == 1


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
    generation_id = uuid.UUID("22222222-2222-7222-8222-222222222222")

    result = await processor.normalize_for_web(
        meme_file_id=meme_file_id,
        filename="animated.gif",
        content_type="image/gif",
        media_bytes=_gif_bytes(),
        generation_id=generation_id,
    )

    generation_prefix = f"pipeline/derived/{meme_file_id}/generations/{generation_id}"
    assert result.preview_image_object_key == f"{generation_prefix}/preview.png"
    assert result.preview_image_bytes is not None
    with Image.open(io.BytesIO(result.preview_image_bytes)) as preview:
        assert preview.format == "PNG"
        assert preview.size == (32, 24)
    assert result.web_video_object_key == f"{generation_prefix}/web.mp4"
    assert result.web_video_bytes == _minimal_faststart_mp4()
    assert result.generation_id == generation_id
    assert result.web_video_profile == WEB_VIDEO_PROFILE_ID
    assert result.frame_rate_mode is WebVideoFrameRateMode.PRESERVE
    assert result.source_has_audio is False
    assert result.web_video_has_audio is False
    assert result.web_video_verified_at is not None
    assert result.output_observations is not None
    assert len(runner.calls) == 5
    packet_probe_call = next(call for call in runner.calls if "-show_packets" in call)
    assert packet_probe_call[packet_probe_call.index("-select_streams") + 1] == "v:0"
    assert packet_probe_call[packet_probe_call.index("-show_entries") + 1] == "packet=dts_time,pts_time,size"
    encode_call = next(call for call in runner.calls if call[-1].endswith("web.mp4") and "-c:v" in call)
    video_filter = encode_call[encode_call.index("-vf") + 1]
    assert "fps=30" not in video_filter
    assert encode_call[encode_call.index("-map") + 1] == "0:v:0"
    assert "0:a:0?" in encode_call
    assert encode_call[encode_call.index("-crf") + 1] == "21"
    assert encode_call[encode_call.index("-preset") + 1] == "medium"
    assert encode_call[encode_call.index("-pix_fmt") + 1] == "yuv420p"
    assert encode_call[encode_call.index("-maxrate") + 1] == "6M"
    assert encode_call[encode_call.index("-bufsize") + 1] == "12M"
    assert encode_call[encode_call.index("-profile:v") + 1] == "high"
    assert encode_call[encode_call.index("-level:v") + 1] == "4.1"
    assert encode_call[encode_call.index("-g") + 1] == "20"
    assert encode_call[encode_call.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)"
    assert encode_call[encode_call.index("-profile:a") + 1] == "aac_low"
    assert encode_call[encode_call.index("-b:a") + 1] == "128k"
    assert encode_call[encode_call.index("-ar") + 1] == "48000"
    assert encode_call[encode_call.index("-ac") + 1] == "2"
    assert encode_call[encode_call.index("-map_metadata") + 1] == "-1"
    assert encode_call[encode_call.index("-map_chapters") + 1] == "-1"
    assert encode_call[encode_call.index("-movflags") + 1] == "+faststart"


@pytest.mark.asyncio
async def test_invalid_source_frame_rate_is_conservatively_normalized_to_30() -> None:
    runner = MovingMediaCommandRunner(
        source_average_frame_rate="0/0",
        source_real_frame_rate="N/A",
        output_frame_rate="30/1",
    )
    result = await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="animated.gif",
        content_type="image/gif",
        media_bytes=_gif_bytes(),
    )

    assert result.frame_rate_mode is WebVideoFrameRateMode.NORMALIZE_30
    encode_call = next(call for call in runner.calls if call[-1].endswith("web.mp4") and "-c:v" in call)
    assert "fps=30" in encode_call[encode_call.index("-vf") + 1]
    assert encode_call[encode_call.index("-fps_mode") + 1] == "cfr"


@pytest.mark.asyncio
async def test_capped_output_is_rejected_when_it_is_not_30_fps() -> None:
    runner = MovingMediaCommandRunner(
        source_average_frame_rate="60/1",
        source_real_frame_rate="60/1",
        output_frame_rate="24/1",
    )

    with pytest.raises(media_inspect_module.MediaValidationError, match="must use 30 FPS"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_preserved_output_is_rejected_when_it_downsamples_source_frame_rate() -> None:
    runner = MovingMediaCommandRunner(
        source_average_frame_rate="24/1",
        source_real_frame_rate="24/1",
        output_frame_rate="15/1",
    )

    with pytest.raises(media_inspect_module.MediaValidationError, match="preserve the source frame rate"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_preserved_ntsc_output_is_rejected_when_rounded_up_to_30_fps() -> None:
    runner = MovingMediaCommandRunner(
        source_average_frame_rate="30000/1001",
        source_real_frame_rate="30000/1001",
        output_frame_rate="30/1",
    )

    with pytest.raises(media_inspect_module.MediaValidationError, match="preserve the source frame rate"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_output_packets_must_respect_vbv_token_bucket() -> None:
    runner = MovingMediaCommandRunner(
        output_video_packets=(("0.0", "2000000"),),
    )

    with pytest.raises(media_inspect_module.MediaValidationError, match="VBV token bucket"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_output_video_timing_must_not_fall_back_to_container_timing() -> None:
    runner = MovingMediaCommandRunner(include_output_video_timing=False)

    with pytest.raises(media_inspect_module.MediaValidationError, match="video stream duration"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_output_audio_timing_must_not_fall_back_to_container_timing() -> None:
    runner = MovingMediaCommandRunner(
        include_audio=True,
        include_output_audio_timing=False,
    )

    with pytest.raises(media_inspect_module.MediaValidationError, match="audio stream duration"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_output_audio_and_video_endpoints_must_stay_within_one_frame() -> None:
    runner = MovingMediaCommandRunner(
        source_average_frame_rate="30/1",
        source_real_frame_rate="30/1",
        output_frame_rate="30/1",
        include_audio=True,
        output_audio_start_time="0.03",
        output_audio_duration="0.23",
    )

    with pytest.raises(media_inspect_module.MediaValidationError, match="end times"):
        await PipelineMediaProcessor(settings=Settings(), command_runner=runner).normalize_for_web(
            meme_file_id=uuid.uuid7(),
            filename="animated.gif",
            content_type="image/gif",
            media_bytes=_gif_bytes(),
        )


@pytest.mark.asyncio
async def test_real_gif_like_input_generates_verified_silent_mp4() -> None:
    _require_media_tools()
    result = await PipelineMediaProcessor(settings=Settings()).normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="animated.gif",
        content_type="image/gif",
        media_bytes=_gif_bytes(),
    )

    assert result.source_has_audio is False
    assert result.web_video_has_audio is False
    assert result.web_video_profile == WEB_VIDEO_PROFILE_ID
    assert result.output_observations is not None
    assert result.output_observations.audio_streams == ()
    assert result.output_observations.primary_video is not None
    assert result.preview_image_bytes is not None
    with Image.open(io.BytesIO(result.preview_image_bytes)) as preview:
        assert preview.format == "PNG"


@pytest.mark.parametrize(
    ("source_rate", "expected_rate", "expected_mode"),
    [
        ("15", 15.0, WebVideoFrameRateMode.PRESERVE),
        ("24", 24.0, WebVideoFrameRateMode.PRESERVE),
        ("30000/1001", 30000 / 1001, WebVideoFrameRateMode.PRESERVE),
        ("60", 30.0, WebVideoFrameRateMode.CAP_30),
        ("120", 30.0, WebVideoFrameRateMode.CAP_30),
    ],
)
@pytest.mark.asyncio
async def test_real_video_frame_rates_are_preserved_through_30_and_capped_above(
    tmp_path: Path,
    source_rate: str,
    expected_rate: float,
    expected_mode: WebVideoFrameRateMode,
) -> None:
    _require_media_tools()
    source_path = tmp_path / "source.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=160x120:rate={source_rate}",
        "-t",
        "0.8",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        "-an",
        str(source_path),
    )

    result = await PipelineMediaProcessor(settings=Settings()).normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="source.mp4",
        content_type="video/mp4",
        media_bytes=source_path.read_bytes(),
    )

    assert result.frame_rate_mode is expected_mode
    assert result.output_observations is not None
    output_video = result.output_observations.primary_video
    assert output_video is not None and output_video.average_frame_rate is not None
    assert output_video.average_frame_rate.frames_per_second == pytest.approx(expected_rate, abs=0.02)
    assert output_video.width == 160
    assert output_video.height == 120


@pytest.mark.asyncio
async def test_real_webm_opus_audio_becomes_aac_lc_and_silent_input_stays_silent(tmp_path: Path) -> None:
    _require_media_tools()
    audible_path = tmp_path / "audible.webm"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=60",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=44100",
        "-t",
        "1.0",
        "-c:v",
        "libvpx",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-c:a",
        "libopus",
        str(audible_path),
    )
    processor = PipelineMediaProcessor(settings=Settings())

    audible = await processor.normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="audible.webm",
        content_type="video/webm",
        media_bytes=audible_path.read_bytes(),
    )

    assert audible.source_has_audio is True
    assert audible.web_video_has_audio is True
    assert audible.output_observations is not None
    assert len(audible.output_observations.audio_streams) == 1
    audio = audible.output_observations.audio_streams[0]
    assert audio.codec_name == "aac"
    assert audio.profile == "LC"
    assert audio.sample_rate == 48_000
    assert audio.channels == 2

    silent_path = tmp_path / "silent.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=15",
        "-t",
        "0.8",
        "-c:v",
        "mpeg4",
        "-an",
        str(silent_path),
    )
    silent = await processor.normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="silent.mp4",
        content_type="video/mp4",
        media_bytes=silent_path.read_bytes(),
    )
    assert silent.source_has_audio is False
    assert silent.web_video_has_audio is False
    assert silent.output_observations is not None
    assert silent.output_observations.audio_streams == ()


@pytest.mark.parametrize(
    ("source_size", "expected_size"),
    [
        ("1280x720", (1280, 720)),
        ("2200x1200", (1920, 1048)),
        ("1200x2200", (1048, 1920)),
        ("1200x1200", (1080, 1080)),
    ],
)
@pytest.mark.asyncio
async def test_real_video_never_upscales_and_fits_orientation_envelope(
    tmp_path: Path,
    source_size: str,
    expected_size: tuple[int, int],
) -> None:
    _require_media_tools()
    source_path = tmp_path / "source.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:size={source_size}:rate=5",
        "-t",
        "1.0",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        "-an",
        str(source_path),
    )

    result = await PipelineMediaProcessor(settings=Settings()).normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="source.mp4",
        content_type="video/mp4",
        media_bytes=source_path.read_bytes(),
    )

    assert result.output_observations is not None
    output_video = result.output_observations.primary_video
    assert output_video is not None
    assert (output_video.width, output_video.height) == expected_size


@pytest.mark.asyncio
async def test_real_vfr_timestamps_and_frame_count_are_preserved_without_duplication(tmp_path: Path) -> None:
    _require_media_tools()
    source_path = tmp_path / "vfr.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=30",
        "-t",
        "2.0",
        "-vf",
        r"select=if(lt(n\,30)\,not(mod(n\,2))\,not(mod(n\,3)))",
        "-fps_mode",
        "vfr",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        "-an",
        str(source_path),
    )

    result = await PipelineMediaProcessor(settings=Settings()).normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="vfr.mp4",
        content_type="video/mp4",
        media_bytes=source_path.read_bytes(),
    )

    assert result.frame_rate_mode is WebVideoFrameRateMode.PRESERVE
    assert result.source_observations is not None
    assert result.output_observations is not None
    source_video = result.source_observations.primary_video
    output_video = result.output_observations.primary_video
    assert source_video is not None and output_video is not None
    assert output_video.frame_count == source_video.frame_count
    assert output_video.average_frame_rate == source_video.average_frame_rate
    assert output_video.duration_seconds == pytest.approx(source_video.duration_seconds, abs=0.002)


@pytest.mark.asyncio
async def test_real_vfr_uses_sub_30_average_rate_when_nominal_rate_is_60(tmp_path: Path) -> None:
    _require_media_tools()
    source_path = tmp_path / "vfr-nominal-60.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=60",
        "-t",
        "2.0",
        "-vf",
        r"select=if(lt(n\,60)\,not(mod(n\,3))\,not(mod(n\,4)))",
        "-fps_mode",
        "vfr",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        "-an",
        str(source_path),
    )

    result = await PipelineMediaProcessor(settings=Settings()).normalize_for_web(
        meme_file_id=uuid.uuid7(),
        filename="vfr-nominal-60.mp4",
        content_type="video/mp4",
        media_bytes=source_path.read_bytes(),
    )

    assert result.source_observations is not None
    assert result.output_observations is not None
    source_video = result.source_observations.primary_video
    output_video = result.output_observations.primary_video
    assert source_video is not None and output_video is not None
    assert source_video.average_frame_rate is not None
    assert source_video.real_frame_rate is not None
    assert source_video.average_frame_rate.frames_per_second < 30
    assert source_video.real_frame_rate.frames_per_second == pytest.approx(60)
    assert result.frame_rate_mode is WebVideoFrameRateMode.PRESERVE
    assert output_video.frame_count == source_video.frame_count
    assert output_video.average_frame_rate is not None
    assert (
        output_video.average_frame_rate.frames_per_second
        <= source_video.average_frame_rate.frames_per_second + 0.01
    )
    assert output_video.duration_seconds == pytest.approx(source_video.duration_seconds, abs=0.002)


def _require_media_tools() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and FFprobe are required for real media-profile tests.")


def _run_ffmpeg(*args: str) -> None:
    result = subprocess.run(
        ("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.fail(result.stderr.decode("utf-8", errors="replace"))
