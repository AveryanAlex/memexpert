"""Heavy media inspection, normalization, and preview helpers for worker runtimes."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import struct
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path, PurePosixPath

import imagehash
from PIL import Image, ImageFilter, ImageSequence, ImageStat, UnidentifiedImageError

from memexpert.core.config import Settings, get_settings
from memexpert.core.media_blurhash import encode_blur_hash
from memexpert.core.storage import (
    build_preview_image_generation_object_key,
    build_web_video_generation_object_key,
)
from memexpert.media.contracts import (
    SUPPORTED_MOVING_MEDIA_MIME_TYPES,
    WEB_VIDEO_PROFILE,
    WEB_VIDEO_PROFILE_ID,
    AudioStreamObservation,
    CommandResult,
    MediaCommandRunner,
    MediaFrameRate,
    MediaProbeObservations,
    MediaProcessingError,
    MediaTimeoutError,
    MediaValidationError,
    NormalizedMediaResult,
    PipelineMediaProcessorProtocol,
    UploadMediaDetails,
    VideoStreamObservation,
    WebVideoFrameRateMode,
)
from memexpert.models.enums import ContentKind

logger = logging.getLogger(__name__)

_SUBPROCESS_REAP_TIMEOUT_SECONDS = 5.0

_IMAGE_FORMAT_TO_MIME_TYPE: dict[str, str] = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_MIME_TYPE_TO_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/gif": frozenset({"gif"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/png": frozenset({"png"}),
    "image/webp": frozenset({"webp"}),
    "video/mp4": frozenset({"mp4"}),
    "video/quicktime": frozenset({"mov"}),
    "video/webm": frozenset({"webm"}),
}
_VIDEO_MIME_TYPES = SUPPORTED_MOVING_MEDIA_MIME_TYPES - {"image/gif"}
_STATIC_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_WEB_VIDEO_MIME_TYPE = "video/mp4"
_MAX_REASONABLE_FRAME_RATE = Fraction(1000, 1)
_VIDEO_BIT_RATE_VALIDATION_TOLERANCE = 1.05
_AUDIO_BIT_RATE_VALIDATION_TOLERANCE = 0.20
_FRAME_RATE_VALIDATION_TOLERANCE = 0.05
_CFR_FRAME_RATE_VALIDATION_TOLERANCE = 0.001


class SubprocessMediaCommandRunner:
    """Default async subprocess runner used for FFmpeg/FFprobe execution."""

    async def run(self, args: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError as exc:
            await _kill_and_reap_process(process)
            raise MediaTimeoutError(f"Timed out after {timeout_seconds:.2f}s while running {' '.join(args)}.") from exc
        except BaseException:
            await _kill_and_reap_process(process)
            raise

        returncode = process.returncode if process.returncode is not None else -1
        return CommandResult(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


async def _kill_and_reap_process(process: asyncio.subprocess.Process) -> None:
    """Kill a command child and bound reaping so shutdown cannot hang forever."""

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()

    try:
        async with asyncio.timeout(_SUBPROCESS_REAP_TIMEOUT_SECONDS):
            await process.wait()
    except TimeoutError:
        logger.error(
            "media_subprocess_reap_timed_out",
            extra={"event": "media_subprocess_reap_timed_out"},
        )
    except asyncio.CancelledError:
        logger.warning(
            "media_subprocess_reap_cancelled",
            extra={"event": "media_subprocess_reap_cancelled"},
        )
    except Exception:
        logger.exception(
            "media_subprocess_reap_failed",
            extra={"event": "media_subprocess_reap_failed"},
        )


class PipelineMediaProcessor:
    """Real media boundary that validates uploads and normalizes assets with FFmpeg."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        command_runner: MediaCommandRunner | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._command_runner = command_runner or SubprocessMediaCommandRunner()

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        normalized_content_type = content_type.strip().lower()
        if normalized_content_type not in self._settings.pipeline_allowed_mime_types:
            raise MediaValidationError("Uploaded media type is not enabled for ingest.")

        if normalized_content_type in _VIDEO_MIME_TYPES:
            return await self._inspect_video_upload(
                filename=filename,
                content_type=normalized_content_type,
                media_bytes=media_bytes,
            )

        return await asyncio.to_thread(
            self._inspect_raster_upload,
            filename,
            normalized_content_type,
            media_bytes,
        )

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
        generation_id: uuid.UUID | None = None,
    ) -> NormalizedMediaResult:
        inspected_media = await self.inspect_upload(
            filename=filename,
            content_type=content_type,
            media_bytes=media_bytes,
        )

        if inspected_media.mime_type in _STATIC_IMAGE_MIME_TYPES:
            preview_frame_bytes = await self.extract_preview_frame(
                filename=filename,
                content_type=inspected_media.mime_type,
                media_bytes=media_bytes,
            )
            preview_image = await asyncio.to_thread(_load_image_from_bytes, preview_frame_bytes)
            quality_score = await asyncio.to_thread(
                _compute_quality_score,
                preview_image,
                inspected_media.width,
                inspected_media.height,
            )
            blur_hash = await asyncio.to_thread(encode_blur_hash, preview_image)
            return NormalizedMediaResult(
                quality_score=quality_score,
                blur_hash=blur_hash,
            )

        resolved_generation_id = generation_id or uuid.uuid7()
        with tempfile.TemporaryDirectory(prefix="memexpert-media-") as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / f"input{_suffix_for_filename(filename)}"
            output_path = temp_path / "web.mp4"
            preview_path = temp_path / "preview.png"
            input_path.write_bytes(media_bytes)

            source_observations = inspected_media.source_observations
            if source_observations is None:
                source_observations = await self._probe_media_file(
                    input_path,
                    expected_mime_type=inspected_media.mime_type,
                )
            frame_rate_plan = _select_frame_rate_plan(source_observations)
            ffmpeg_args = self._ffmpeg_web_video_command(
                input_path=input_path,
                output_path=output_path,
                frame_rate_plan=frame_rate_plan,
            )
            result = await self._command_runner.run(
                ffmpeg_args,
                timeout_seconds=self._settings.pipeline_transcode_timeout_seconds,
            )
            if result.returncode != 0:
                raise MediaProcessingError(
                    _render_tool_failure("FFmpeg failed to normalize the uploaded media", result.stderr)
                )
            web_video_bytes = output_path.read_bytes()
            if not web_video_bytes:
                raise MediaProcessingError("FFmpeg produced an empty normalized web video.")

            output_observations = await self._probe_media_file(
                output_path,
                expected_mime_type=_WEB_VIDEO_MIME_TYPE,
            )
            source_video_packets = (
                await self._probe_video_packets(input_path)
                if _video_stream_is_vfr(source_observations.primary_video)
                else None
            )
            output_video_packets = await self._probe_video_packets(output_path)
            _validate_web_video_output(
                source=source_observations,
                output=output_observations,
                frame_rate_plan=frame_rate_plan,
                output_bytes=web_video_bytes,
                source_video_packets=source_video_packets,
                video_packets=output_video_packets,
            )
            preview_frame_bytes = await self._extract_preview_frame_from_path(
                input_path=output_path,
                output_path=preview_path,
            )
            preview_image = await asyncio.to_thread(_validate_and_load_preview_png, preview_frame_bytes)
            quality_score = await asyncio.to_thread(
                _compute_quality_score,
                preview_image,
                inspected_media.width,
                inspected_media.height,
            )
            blur_hash = await asyncio.to_thread(encode_blur_hash, preview_image)

        return NormalizedMediaResult(
            quality_score=quality_score,
            blur_hash=blur_hash,
            preview_image_object_key=build_preview_image_generation_object_key(
                meme_file_id,
                resolved_generation_id,
                settings=self._settings,
            ),
            preview_image_bytes=preview_frame_bytes,
            web_video_object_key=build_web_video_generation_object_key(
                meme_file_id,
                resolved_generation_id,
                settings=self._settings,
            ),
            web_video_bytes=web_video_bytes,
            generation_id=resolved_generation_id,
            web_video_profile=WEB_VIDEO_PROFILE_ID,
            frame_rate_mode=frame_rate_plan.mode,
            source_has_audio=source_observations.has_audio,
            web_video_has_audio=output_observations.has_audio,
            source_observations=source_observations,
            output_observations=output_observations,
            web_video_verified_at=datetime.now(UTC),
        )

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes:
        normalized_content_type = content_type.strip().lower()
        if normalized_content_type in _VIDEO_MIME_TYPES:
            with tempfile.TemporaryDirectory(prefix="memexpert-media-preview-") as temp_dir:
                temp_path = Path(temp_dir)
                input_path = temp_path / f"input{_suffix_for_filename(filename)}"
                output_path = temp_path / "preview.png"
                input_path.write_bytes(media_bytes)
                return await self._extract_preview_frame_from_path(
                    input_path=input_path,
                    output_path=output_path,
                )

        return await asyncio.to_thread(self._extract_raster_preview_frame, media_bytes)

    async def _extract_preview_frame_from_path(
        self,
        *,
        input_path: Path,
        output_path: Path,
    ) -> bytes:
        frame_args = (
            self._settings.pipeline_ffmpeg_binary,
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-vf",
            "select=eq(n\\,0)",
            "-frames:v",
            "1",
            "-f",
            "image2",
            str(output_path),
        )
        result = await self._command_runner.run(
            frame_args,
            timeout_seconds=self._settings.pipeline_transcode_timeout_seconds,
        )
        if result.returncode != 0:
            raise MediaProcessingError(
                _render_tool_failure("FFmpeg failed to extract an OCR preview frame", result.stderr)
            )
        preview_bytes = output_path.read_bytes()
        if not preview_bytes:
            raise MediaProcessingError("FFmpeg did not produce an OCR preview frame.")
        return preview_bytes

    def _inspect_raster_upload(
        self,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        try:
            with Image.open(io.BytesIO(media_bytes)) as image:
                detected_format = image.format
                image.load()
                width = image.width
                height = image.height
                if image.format is not None and image.format.upper() == "GIF":
                    frame_image = ImageSequence.Iterator(image).__next__().convert("RGB")
                    media_type = ContentKind.GIF
                else:
                    frame_image = image.convert("RGB")
                    media_type = ContentKind.IMAGE
        except Image.DecompressionBombError as exc:
            raise MediaValidationError("Uploaded image exceeds the configured pixel budget.") from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise MediaValidationError("Uploaded file is not a readable image payload.") from exc

        if detected_format is None:
            raise MediaValidationError("Uploaded file format could not be detected.")
        detected_mime_type = _IMAGE_FORMAT_TO_MIME_TYPE.get(detected_format.upper())
        if detected_mime_type is None:
            raise MediaValidationError("Uploaded media type is not supported.")
        if detected_mime_type != content_type:
            raise MediaValidationError(
                f"Uploaded content type {content_type!r} does not match detected media type {detected_mime_type!r}."
            )
        _validate_filename_extension(filename=filename, mime_type=detected_mime_type)
        _validate_dimensions(
            width=width,
            height=height,
            max_pixels=self._settings.pipeline_image_max_pixels,
        )

        perceptual_hash = str(imagehash.phash(frame_image, hash_size=self._settings.pipeline_phash_size))
        return UploadMediaDetails(
            media_type=media_type,
            mime_type=detected_mime_type,
            width=width,
            height=height,
            file_size_bytes=len(media_bytes),
            perceptual_hash=perceptual_hash,
        )

    async def _inspect_video_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails:
        _validate_filename_extension(filename=filename, mime_type=content_type)
        with tempfile.TemporaryDirectory(prefix="memexpert-video-probe-") as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / f"input{_suffix_for_filename(filename)}"
            input_path.write_bytes(media_bytes)
            observations = await self._probe_media_file(input_path, expected_mime_type=content_type)

        video_stream = observations.primary_video
        if video_stream is None or video_stream.width is None or video_stream.height is None:
            raise MediaValidationError("Uploaded file does not contain a readable video stream.")

        preview_frame_bytes = await self.extract_preview_frame(
            filename=filename,
            content_type=content_type,
            media_bytes=media_bytes,
        )
        preview_image = await asyncio.to_thread(_load_image_from_bytes, preview_frame_bytes)
        perceptual_hash = await asyncio.to_thread(
            lambda: str(imagehash.phash(preview_image, hash_size=self._settings.pipeline_phash_size))
        )

        return UploadMediaDetails(
            media_type=ContentKind.VIDEO,
            mime_type=content_type,
            width=video_stream.display_width or video_stream.width,
            height=video_stream.display_height or video_stream.height,
            file_size_bytes=len(media_bytes),
            perceptual_hash=perceptual_hash,
            source_has_audio=observations.has_audio,
            source_observations=observations,
        )

    async def _probe_media_file(self, path: Path, *, expected_mime_type: str) -> MediaProbeObservations:
        args = (
            self._settings.pipeline_ffprobe_binary,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        )
        result = await self._command_runner.run(
            args,
            timeout_seconds=self._settings.pipeline_transcode_timeout_seconds,
        )
        if result.returncode != 0:
            raise MediaProcessingError(
                _render_tool_failure("FFprobe failed to inspect the uploaded media", result.stderr)
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaValidationError("FFprobe returned malformed media metadata.") from exc

        if not isinstance(payload, dict):
            raise MediaValidationError("FFprobe returned malformed media metadata.")
        observations = _parse_probe_observations(payload, byte_size=path.stat().st_size)
        if observations.primary_video is None:
            raise MediaValidationError("Uploaded file does not contain a readable video stream.")
        if not observations.format_names:
            raise MediaValidationError("FFprobe did not report a recognizable video container.")
        if expected_mime_type == "video/webm" and "webm" not in observations.format_names:
            raise MediaValidationError("Uploaded media bytes do not match the declared video/webm content type.")
        if expected_mime_type in {"video/mp4", "video/quicktime"} and not set(observations.format_names).intersection(
            {"mp4", "mov", "m4a", "3gp", "3g2", "mj2"}
        ):
            raise MediaValidationError(
                f"Uploaded media bytes do not match the declared {expected_mime_type!r} content type."
            )
        if expected_mime_type == "image/gif" and "gif" not in observations.format_names:
            raise MediaValidationError("Uploaded media bytes do not match the declared image/gif content type.")
        return observations

    async def _probe_video_packets(self, path: Path) -> tuple[_VideoPacketObservation, ...]:
        args = (
            self._settings.pipeline_ffprobe_binary,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=dts_time,pts_time,size",
            "-of",
            "json",
            str(path),
        )
        result = await self._command_runner.run(
            args,
            timeout_seconds=self._settings.pipeline_transcode_timeout_seconds,
        )
        if result.returncode != 0:
            raise MediaProcessingError(
                _render_tool_failure("FFprobe failed to inspect normalized video packets", result.stderr)
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaValidationError("FFprobe returned malformed normalized packet metadata.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("packets"), list):
            raise MediaValidationError("FFprobe returned malformed normalized packet metadata.")
        packets = tuple(_parse_video_packet(entry) for entry in payload["packets"])
        if not packets:
            raise MediaValidationError("FFprobe did not report normalized video packets.")
        return packets

    def _ffmpeg_web_video_command(
        self,
        *,
        input_path: Path,
        output_path: Path,
        frame_rate_plan: _FrameRatePlan,
    ) -> tuple[str, ...]:
        scale_filter = (
            "scale="
            "w='max(2,trunc(min(iw,if(gt(iw,ih),1920,1080))/2)*2)':"
            "h='max(2,trunc(min(ih,if(gt(iw,ih),1080,1920))/2)*2)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        video_filters = [scale_filter]
        if frame_rate_plan.mode is not WebVideoFrameRateMode.PRESERVE:
            video_filters.append("fps=30")
        video_filters.append("format=yuv420p")
        return (
            self._settings.pipeline_ffmpeg_binary,
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            ",".join(video_filters),
            "-fps_mode",
            "passthrough" if frame_rate_plan.mode is WebVideoFrameRateMode.PRESERVE else "cfr",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-level:v",
            WEB_VIDEO_PROFILE.video_level,
            "-pix_fmt",
            WEB_VIDEO_PROFILE.pixel_format,
            "-preset",
            WEB_VIDEO_PROFILE.preset,
            "-crf",
            str(WEB_VIDEO_PROFILE.crf),
            "-maxrate",
            "6M",
            "-bufsize",
            "12M",
            "-g",
            str(frame_rate_plan.maximum_gop_frames),
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-af",
            "aresample=async=1:first_pts=0,apad",
            "-shortest",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output_path),
        )

    @staticmethod
    def _extract_raster_preview_frame(media_bytes: bytes) -> bytes:
        with Image.open(io.BytesIO(media_bytes)) as image:
            if image.format is not None and image.format.upper() == "GIF":
                frame_image = ImageSequence.Iterator(image).__next__().convert("RGB")
            else:
                frame_image = image.convert("RGB")
            output = io.BytesIO()
            frame_image.save(output, format="PNG")
            return output.getvalue()


@dataclass(frozen=True, slots=True)
class _FrameRatePlan:
    mode: WebVideoFrameRateMode
    source_rates: tuple[MediaFrameRate, ...]
    maximum_gop_frames: int


@dataclass(frozen=True, slots=True)
class _VideoPacketObservation:
    timestamp_seconds: float
    size_bytes: int


def _select_frame_rate_plan(observations: MediaProbeObservations) -> _FrameRatePlan:
    video_stream = observations.primary_video
    if video_stream is None:
        raise MediaValidationError("Source media does not contain a video stream.")
    source_rates = tuple(
        rate for rate in (video_stream.average_frame_rate, video_stream.real_frame_rate) if rate is not None
    )
    selected_rate = video_stream.average_frame_rate or video_stream.real_frame_rate
    if selected_rate is None:
        return _FrameRatePlan(
            mode=WebVideoFrameRateMode.NORMALIZE_30,
            source_rates=(),
            maximum_gop_frames=60,
        )
    effective_rate = _as_fraction(selected_rate)
    if effective_rate > WEB_VIDEO_PROFILE.maximum_frame_rate:
        return _FrameRatePlan(
            mode=WebVideoFrameRateMode.CAP_30,
            source_rates=source_rates,
            maximum_gop_frames=60,
        )
    maximum_gop_frames = max(
        1,
        math.ceil(float(effective_rate) * WEB_VIDEO_PROFILE.maximum_gop_seconds),
    )
    return _FrameRatePlan(
        mode=WebVideoFrameRateMode.PRESERVE,
        source_rates=source_rates,
        maximum_gop_frames=maximum_gop_frames,
    )


def _parse_probe_observations(payload: dict[str, object], *, byte_size: int) -> MediaProbeObservations:
    raw_streams = payload.get("streams")
    raw_format = payload.get("format")
    raw_chapters = payload.get("chapters", [])
    if not isinstance(raw_streams, list) or not isinstance(raw_format, dict):
        raise MediaValidationError("FFprobe returned malformed media metadata.")
    if not isinstance(raw_chapters, list):
        raise MediaValidationError("FFprobe returned malformed chapter metadata.")

    video_streams: list[VideoStreamObservation] = []
    audio_streams: list[AudioStreamObservation] = []
    stream_type_counts = {"subtitle": 0, "data": 0, "attachment": 0}
    unknown_stream_types: list[str] = []
    for raw_stream in raw_streams:
        if not isinstance(raw_stream, dict):
            raise MediaValidationError("FFprobe returned a malformed stream entry.")
        codec_type = _optional_text(raw_stream.get("codec_type"))
        if codec_type == "video":
            video_streams.append(_parse_video_stream(raw_stream))
        elif codec_type == "audio":
            audio_streams.append(_parse_audio_stream(raw_stream))
        elif codec_type in stream_type_counts:
            stream_type_counts[codec_type] += 1
        else:
            unknown_stream_types.append(codec_type or "unknown")

    raw_format_name = _optional_text(raw_format.get("format_name"))
    format_names = (
        tuple(part.strip().lower() for part in raw_format_name.split(",") if part.strip())
        if raw_format_name is not None
        else ()
    )
    return MediaProbeObservations(
        format_names=format_names,
        format_long_name=_optional_text(raw_format.get("format_long_name")),
        start_time_seconds=_optional_finite_float(raw_format.get("start_time")),
        duration_seconds=_optional_positive_float(raw_format.get("duration")),
        bit_rate=_optional_positive_int(raw_format.get("bit_rate")),
        byte_size=byte_size,
        video_streams=tuple(video_streams),
        audio_streams=tuple(audio_streams),
        subtitle_stream_count=stream_type_counts["subtitle"],
        data_stream_count=stream_type_counts["data"],
        attachment_stream_count=stream_type_counts["attachment"],
        unknown_stream_types=tuple(unknown_stream_types),
        chapter_count=len(raw_chapters),
    )


def _parse_video_stream(payload: object) -> VideoStreamObservation:
    if not isinstance(payload, dict):
        raise MediaValidationError("FFprobe returned a malformed video stream entry.")
    return VideoStreamObservation(
        index=_required_non_negative_int(payload.get("index"), field_name="video stream index"),
        codec_name=_optional_text(payload.get("codec_name")),
        profile=_optional_text(payload.get("profile")),
        level=_optional_positive_int(payload.get("level")),
        pixel_format=_optional_text(payload.get("pix_fmt")),
        width=_optional_positive_int(payload.get("width")),
        height=_optional_positive_int(payload.get("height")),
        average_frame_rate=_optional_frame_rate(payload.get("avg_frame_rate")),
        real_frame_rate=_optional_frame_rate(payload.get("r_frame_rate")),
        start_time_seconds=_optional_finite_float(payload.get("start_time")),
        duration_seconds=_optional_positive_float(payload.get("duration")),
        bit_rate=_optional_positive_int(payload.get("bit_rate")),
        frame_count=_optional_positive_int(payload.get("nb_frames")),
        rotation_degrees=_rotation_degrees(payload),
    )


def _parse_audio_stream(payload: object) -> AudioStreamObservation:
    if not isinstance(payload, dict):
        raise MediaValidationError("FFprobe returned a malformed audio stream entry.")
    return AudioStreamObservation(
        index=_required_non_negative_int(payload.get("index"), field_name="audio stream index"),
        codec_name=_optional_text(payload.get("codec_name")),
        profile=_optional_text(payload.get("profile")),
        sample_rate=_optional_positive_int(payload.get("sample_rate")),
        channels=_optional_positive_int(payload.get("channels")),
        channel_layout=_optional_text(payload.get("channel_layout")),
        start_time_seconds=_optional_finite_float(payload.get("start_time")),
        duration_seconds=_optional_positive_float(payload.get("duration")),
        bit_rate=_optional_positive_int(payload.get("bit_rate")),
    )


def _parse_video_packet(payload: object) -> _VideoPacketObservation:
    if not isinstance(payload, dict):
        raise MediaValidationError("FFprobe returned a malformed normalized video packet.")
    timestamp = _optional_finite_float(payload.get("dts_time"))
    if timestamp is None:
        timestamp = _optional_finite_float(payload.get("pts_time"))
    size_bytes = _optional_positive_int(payload.get("size"))
    if timestamp is None or size_bytes is None:
        raise MediaValidationError("FFprobe returned incomplete normalized video packet metadata.")
    return _VideoPacketObservation(timestamp_seconds=timestamp, size_bytes=size_bytes)


def _rotation_degrees(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    side_data = payload.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            rotation = _optional_int(entry.get("rotation"))
            if rotation is not None:
                return rotation % 360
    tags = payload.get("tags")
    if isinstance(tags, dict):
        rotation = _optional_int(tags.get("rotate"))
        if rotation is not None:
            return rotation % 360
    return 0


def _validate_web_video_output(
    *,
    source: MediaProbeObservations,
    output: MediaProbeObservations,
    frame_rate_plan: _FrameRatePlan,
    output_bytes: bytes,
    source_video_packets: tuple[_VideoPacketObservation, ...] | None,
    video_packets: tuple[_VideoPacketObservation, ...],
) -> None:
    source_video = source.primary_video
    if source_video is None:
        raise MediaValidationError("Source media does not contain a video stream.")
    if len(output.video_streams) != 1:
        raise MediaValidationError("Normalized media must contain exactly one video stream.")
    output_video = output.video_streams[0]
    if not set(output.format_names).intersection({"mp4", "mov"}):
        raise MediaValidationError("Normalized media is not an MP4 container.")
    if output_video.codec_name != WEB_VIDEO_PROFILE.video_codec:
        raise MediaValidationError("Normalized media video codec is not H.264.")
    if (output_video.profile or "").casefold() != WEB_VIDEO_PROFILE.video_profile.casefold():
        raise MediaValidationError("Normalized media does not use the H.264 High profile.")
    if output_video.level != 41:
        raise MediaValidationError("Normalized media does not use H.264 Level 4.1.")
    if output_video.pixel_format != WEB_VIDEO_PROFILE.pixel_format:
        raise MediaValidationError("Normalized media pixel format is not yuv420p.")
    _validate_output_dimensions(source_video, output_video)
    output_frame_rate = _validate_output_frame_rate(
        source_video=source_video,
        output_video=output_video,
        frame_rate_plan=frame_rate_plan,
        source_video_packets=source_video_packets,
        output_video_packets=video_packets,
    )
    frame_tolerance = (1.0 / output_frame_rate) + 0.002
    source_duration = _video_duration(source_video, source)
    output_duration = output_video.duration_seconds
    if source_duration is None:
        raise MediaValidationError("FFprobe did not report source video duration.")
    if output_duration is None:
        raise MediaValidationError("FFprobe did not report normalized video stream duration.")
    if output_video.start_time_seconds is None:
        raise MediaValidationError("FFprobe did not report normalized video stream start time.")
    if abs(source_duration - output_duration) > frame_tolerance:
        raise MediaValidationError("Normalized media duration differs from the source by more than one frame.")
    _validate_output_audio(
        source=source,
        output=output,
        video_start_time=output_video.start_time_seconds,
        video_duration=output_duration,
        frame_tolerance=frame_tolerance,
    )
    if output.subtitle_stream_count or output.data_stream_count or output.attachment_stream_count:
        raise MediaValidationError("Normalized media contains an unexpected non-playback stream.")
    if output.unknown_stream_types:
        raise MediaValidationError("Normalized media contains an unknown stream type.")
    if output.chapter_count:
        raise MediaValidationError("Normalized media contains chapters.")
    if output_video.bit_rate is None:
        raise MediaValidationError("FFprobe did not report normalized video bitrate.")
    maximum_measured_rate = int(WEB_VIDEO_PROFILE.maximum_video_bit_rate * _VIDEO_BIT_RATE_VALIDATION_TOLERANCE)
    if output_video.bit_rate > maximum_measured_rate:
        raise MediaValidationError("Normalized media exceeds the configured video-rate profile.")
    _validate_video_vbv_packets(video_packets)
    if output.byte_size != len(output_bytes):
        raise MediaValidationError("Normalized media byte-size observation is inconsistent.")
    if not _mp4_has_faststart(output_bytes):
        raise MediaValidationError("Normalized MP4 does not place metadata before media data.")


def _validate_video_vbv_packets(packets: tuple[_VideoPacketObservation, ...]) -> None:
    """Verify the encoded packets fit the configured max-rate/buffer token bucket."""

    ordered = sorted(packets, key=lambda packet: packet.timestamp_seconds)
    first_timestamp = ordered[0].timestamp_seconds
    cumulative_bits = 0
    minimum_adjusted_prefix = 0.0
    buffer_limit = WEB_VIDEO_PROFILE.video_buffer_size * _VIDEO_BIT_RATE_VALIDATION_TOLERANCE
    for packet in ordered:
        elapsed = max(packet.timestamp_seconds - first_timestamp, 0.0)
        cumulative_bits += packet.size_bytes * 8
        adjusted_prefix = cumulative_bits - WEB_VIDEO_PROFILE.maximum_video_bit_rate * elapsed
        if adjusted_prefix - minimum_adjusted_prefix > buffer_limit:
            raise MediaValidationError("Normalized media packets exceed the configured VBV token bucket.")
        minimum_adjusted_prefix = min(minimum_adjusted_prefix, adjusted_prefix)


def _validate_output_dimensions(
    source_video: VideoStreamObservation,
    output_video: VideoStreamObservation,
) -> None:
    source_width = source_video.display_width
    source_height = source_video.display_height
    width = output_video.width
    height = output_video.height
    if source_width is None or source_height is None or width is None or height is None:
        raise MediaValidationError("Normalized media dimensions are missing.")
    if min(source_width, source_height, width, height) < 2:
        raise MediaValidationError("Moving-media dimensions must be at least two pixels per side.")
    if width % 2 or height % 2:
        raise MediaValidationError("Normalized media dimensions must be even.")
    if width > source_width or height > source_height:
        raise MediaValidationError("Normalized media must not upscale the source.")
    if width > height:
        within_envelope = width <= 1920 and height <= 1080
    elif height > width:
        within_envelope = width <= 1080 and height <= 1920
    else:
        within_envelope = width <= 1080 and height <= 1080
    if not within_envelope:
        raise MediaValidationError("Normalized media dimensions exceed the mobile envelope.")


def _validate_output_frame_rate(
    *,
    source_video: VideoStreamObservation,
    output_video: VideoStreamObservation,
    frame_rate_plan: _FrameRatePlan,
    source_video_packets: tuple[_VideoPacketObservation, ...] | None,
    output_video_packets: tuple[_VideoPacketObservation, ...],
) -> float:
    output_rates = tuple(
        rate for rate in (output_video.average_frame_rate, output_video.real_frame_rate) if rate is not None
    )
    if not output_rates:
        raise MediaValidationError("FFprobe did not report normalized frame-rate metadata.")
    selected_output_rate = output_video.average_frame_rate or output_video.real_frame_rate
    if selected_output_rate is None:
        raise MediaValidationError("FFprobe did not report normalized frame-rate metadata.")
    average_output_rate = selected_output_rate.frames_per_second
    if average_output_rate > WEB_VIDEO_PROFILE.maximum_frame_rate + _FRAME_RATE_VALIDATION_TOLERANCE:
        raise MediaValidationError("Normalized media exceeds 30 FPS.")
    if frame_rate_plan.mode is WebVideoFrameRateMode.PRESERVE:
        source_average_rate = source_video.average_frame_rate or source_video.real_frame_rate
        if source_average_rate is None:
            raise MediaValidationError("Preserved media is missing source frame-rate metadata.")
        if _video_stream_is_vfr(source_video):
            if source_video_packets is None:
                raise MediaValidationError("Preserved VFR media is missing source packet observations.")
            if len(output_video_packets) != len(source_video_packets):
                raise MediaValidationError("Normalized VFR media does not preserve the source frame count.")
            if average_output_rate > source_average_rate.frames_per_second + _FRAME_RATE_VALIDATION_TOLERANCE:
                raise MediaValidationError("Normalized media artificially increases source frame rate.")
        elif (
            abs(average_output_rate - source_average_rate.frames_per_second)
            > _CFR_FRAME_RATE_VALIDATION_TOLERANCE
        ):
            raise MediaValidationError("Normalized media does not preserve the source frame rate.")
    elif abs(average_output_rate - WEB_VIDEO_PROFILE.maximum_frame_rate) > _FRAME_RATE_VALIDATION_TOLERANCE:
        raise MediaValidationError("Capped or normalized media must use 30 FPS.")
    return average_output_rate


def _video_stream_is_vfr(video: VideoStreamObservation | None) -> bool:
    return bool(
        video is not None
        and video.average_frame_rate is not None
        and video.real_frame_rate is not None
        and _as_fraction(video.average_frame_rate) != _as_fraction(video.real_frame_rate)
    )


def _validate_output_audio(
    *,
    source: MediaProbeObservations,
    output: MediaProbeObservations,
    video_start_time: float | None,
    video_duration: float,
    frame_tolerance: float,
) -> None:
    expected_audio_count = 1 if source.has_audio else 0
    if len(output.audio_streams) != expected_audio_count:
        raise MediaValidationError("Normalized media audio presence does not match the source.")
    if not output.audio_streams:
        return
    audio = output.audio_streams[0]
    if audio.codec_name != WEB_VIDEO_PROFILE.audio_codec:
        raise MediaValidationError("Normalized media audio codec is not AAC.")
    normalized_profile = (audio.profile or "").replace("AAC", "").strip().casefold()
    if normalized_profile not in {"lc", "low complexity"}:
        raise MediaValidationError("Normalized media audio profile is not AAC-LC.")
    if audio.sample_rate != WEB_VIDEO_PROFILE.audio_sample_rate or audio.channels != WEB_VIDEO_PROFILE.audio_channels:
        raise MediaValidationError("Normalized media audio format is not 48 kHz stereo.")
    if audio.bit_rate is None:
        raise MediaValidationError("FFprobe did not report normalized audio bitrate.")
    audio_rate_delta = abs(audio.bit_rate - WEB_VIDEO_PROFILE.audio_bit_rate)
    if audio_rate_delta > WEB_VIDEO_PROFILE.audio_bit_rate * _AUDIO_BIT_RATE_VALIDATION_TOLERANCE:
        raise MediaValidationError("Normalized media audio bitrate does not match the profile.")
    audio_duration = audio.duration_seconds
    if audio_duration is None:
        raise MediaValidationError("FFprobe did not report normalized audio stream duration.")
    audio_start_time = audio.start_time_seconds
    if video_start_time is None:
        raise MediaValidationError("FFprobe did not report normalized video stream start time.")
    if audio_start_time is None:
        raise MediaValidationError("FFprobe did not report normalized audio stream start time.")
    if abs(audio_start_time - video_start_time) > frame_tolerance:
        raise MediaValidationError("Normalized audio and video start times differ by more than one frame.")
    if abs(audio_duration - video_duration) > frame_tolerance:
        raise MediaValidationError("Normalized audio and video are out of sync by more than one frame.")
    video_end_time = video_start_time + video_duration
    audio_end_time = audio_start_time + audio_duration
    if abs(audio_end_time - video_end_time) > frame_tolerance:
        raise MediaValidationError("Normalized audio and video end times differ by more than one frame.")


def _video_duration(
    video: VideoStreamObservation,
    observations: MediaProbeObservations,
) -> float | None:
    return video.duration_seconds or observations.duration_seconds


def _mp4_has_faststart(payload: bytes) -> bool:
    moov_offset: int | None = None
    mdat_offset: int | None = None
    offset = 0
    while offset + 8 <= len(payload):
        size = struct.unpack_from(">I", payload, offset)[0]
        box_type = payload[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(payload):
                return False
            size = struct.unpack_from(">Q", payload, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = len(payload) - offset
        if size < header_size or offset + size > len(payload):
            return False
        if box_type == b"moov" and moov_offset is None:
            moov_offset = offset
        elif box_type == b"mdat" and mdat_offset is None:
            mdat_offset = offset
        offset += size
    return moov_offset is not None and mdat_offset is not None and moov_offset < mdat_offset


def _optional_frame_rate(value: object) -> MediaFrameRate | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        rate = Fraction(str(value).strip())
    except ValueError, ZeroDivisionError:
        return None
    if rate <= 0 or rate > _MAX_REASONABLE_FRAME_RATE:
        return None
    return MediaFrameRate(numerator=rate.numerator, denominator=rate.denominator)


def _as_fraction(rate: MediaFrameRate) -> Fraction:
    return Fraction(rate.numerator, rate.denominator)


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.upper() == "N/A":
        return None
    return normalized


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _optional_positive_int(value: object) -> int | None:
    resolved = _optional_int(value)
    return resolved if resolved is not None and resolved > 0 else None


def _required_non_negative_int(value: object, *, field_name: str) -> int:
    resolved = _optional_int(value)
    if resolved is None or resolved < 0:
        raise MediaValidationError(f"FFprobe returned malformed {field_name} metadata.")
    return resolved


def _optional_finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        resolved = float(value)
    except TypeError, ValueError:
        return None
    return resolved if math.isfinite(resolved) else None


def _optional_positive_float(value: object) -> float | None:
    resolved = _optional_finite_float(value)
    return resolved if resolved is not None and resolved > 0 else None


def _render_tool_failure(prefix: str, stderr: bytes) -> str:
    rendered_stderr = stderr.decode("utf-8", errors="replace").strip()
    if rendered_stderr:
        return f"{prefix}: {rendered_stderr}"
    return prefix


def _coerce_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MediaValidationError(f"FFprobe returned malformed {field_name} metadata.")
    try:
        resolved = int(value)
    except ValueError as exc:
        raise MediaValidationError(f"FFprobe returned malformed {field_name} metadata.") from exc
    if resolved <= 0:
        raise MediaValidationError(f"FFprobe returned invalid {field_name} metadata.")
    return resolved


def _validate_dimensions(*, width: int, height: int, max_pixels: int) -> None:
    if width <= 0 or height <= 0:
        raise MediaValidationError("Uploaded image dimensions are invalid.")
    if width * height > max_pixels:
        raise MediaValidationError("Uploaded image exceeds the configured pixel budget.")


def _validate_filename_extension(*, filename: str, mime_type: str) -> None:
    extension = PurePosixPath(filename).suffix.lstrip(".").lower()
    if not extension:
        raise MediaValidationError("Uploaded filename must include an extension.")
    allowed_extensions = _MIME_TYPE_TO_EXTENSIONS.get(mime_type)
    if allowed_extensions is None or extension not in allowed_extensions:
        raise MediaValidationError(f"Filename extension .{extension} does not match uploaded media type {mime_type!r}.")


def _suffix_for_filename(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix
    if suffix:
        return suffix.lower()
    return ".bin"


def _load_image_from_bytes(image_bytes: bytes) -> Image.Image:
    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.convert("RGB")


def _validate_and_load_preview_png(image_bytes: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format != "PNG" or image.width <= 0 or image.height <= 0:
                raise MediaValidationError("Generated preview is not a valid PNG image.")
            image.load()
            return image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise MediaValidationError("Generated preview is not a readable PNG image.") from exc


def _compute_quality_score(image: Image.Image, width: int, height: int) -> float:
    grayscale = image.convert("L").resize((128, 128), Image.Resampling.LANCZOS)
    edges = grayscale.filter(ImageFilter.FIND_EDGES)
    grayscale_stats = ImageStat.Stat(grayscale)
    edge_stats = ImageStat.Stat(edges)
    resolution_score = min((width * height) / float(1920 * 1080), 1.0)
    contrast_score = min(grayscale_stats.stddev[0] / 72.0, 1.0)
    sharpness_score = min(edge_stats.mean[0] / 96.0, 1.0)
    combined = (0.45 * resolution_score) + (0.25 * contrast_score) + (0.30 * sharpness_score)
    return round(max(0.0, min(combined, 1.0)), 4)


__all__ = [
    "MediaCommandRunner",
    "MediaProcessingError",
    "MediaTimeoutError",
    "MediaValidationError",
    "NormalizedMediaResult",
    "PipelineMediaProcessor",
    "PipelineMediaProcessorProtocol",
    "SubprocessMediaCommandRunner",
    "UploadMediaDetails",
]
