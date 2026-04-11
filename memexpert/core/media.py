"""Media inspection, normalization, and preview helpers for the heavy content pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

import imagehash
from PIL import Image, ImageFilter, ImageSequence, ImageStat, UnidentifiedImageError

from memexpert.core.config import Settings, get_settings
from memexpert.core.media_blurhash import encode_blur_hash
from memexpert.core.storage import build_web_video_object_key
from memexpert.models.enums import ContentKind

if TYPE_CHECKING:
    import uuid

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
_VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/quicktime", "video/webm"})
_DEFAULT_IMAGE_DURATION_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class UploadMediaDetails:
    """Validated metadata derived from uploaded media bytes before ingest persistence."""

    media_type: ContentKind
    mime_type: str
    width: int
    height: int
    file_size_bytes: int
    perceptual_hash: str


@dataclass(frozen=True, slots=True)
class NormalizedMediaResult:
    """Normalized derivative output written during the transcode stage."""

    mime_type: str
    width: int
    height: int
    file_size_bytes: int
    quality_score: float
    blur_hash: str | None
    web_video_object_key: str
    web_video_bytes: bytes


class MediaProcessingError(RuntimeError):
    """Base error raised when media inspection or normalization cannot complete."""


class MediaTimeoutError(MediaProcessingError):
    """Raised when FFmpeg/FFprobe work exceeds the configured timeout."""


class MediaValidationError(MediaProcessingError):
    """Raised when uploaded media bytes or metadata are malformed."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured subprocess output from FFmpeg/FFprobe helpers."""

    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class MediaCommandRunner(Protocol):
    """Async subprocess boundary used to keep FFmpeg/FFprobe calls testable."""

    async def run(self, args: tuple[str, ...], *, timeout_seconds: float) -> CommandResult: ...


class PipelineMediaProcessorProtocol(Protocol):
    """Typed media boundary used by the upload service and worker runtime."""

    async def inspect_upload(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> UploadMediaDetails: ...

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult: ...

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes: ...


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
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
            raise MediaTimeoutError(f"Timed out after {timeout_seconds:.2f}s while running {' '.join(args)}.") from exc

        returncode = process.returncode if process.returncode is not None else -1
        return CommandResult(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


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
    ) -> NormalizedMediaResult:
        inspected_media = await self.inspect_upload(
            filename=filename,
            content_type=content_type,
            media_bytes=media_bytes,
        )
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

        with tempfile.TemporaryDirectory(prefix="memexpert-media-") as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / f"input{_suffix_for_filename(filename)}"
            output_path = temp_path / "web.mp4"
            input_path.write_bytes(media_bytes)

            ffmpeg_args = self._ffmpeg_command(
                input_path=input_path,
                output_path=output_path,
                content_type=inspected_media.mime_type,
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

            probe = await self._probe_video_file(output_path, expected_mime_type="video/mp4")
            width = probe.width
            height = probe.height
            if width <= 0 or height <= 0:
                raise MediaValidationError("Normalized media dimensions are invalid.")

        return NormalizedMediaResult(
            mime_type="video/mp4",
            width=width,
            height=height,
            file_size_bytes=len(web_video_bytes),
            quality_score=quality_score,
            blur_hash=blur_hash,
            web_video_object_key=build_web_video_object_key(meme_file_id, settings=self._settings),
            web_video_bytes=web_video_bytes,
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
                frame_args = (
                    self._settings.pipeline_ffmpeg_binary,
                    "-y",
                    "-i",
                    str(input_path),
                    "-vf",
                    "select=eq(n\\,0)",
                    "-frames:v",
                    "1",
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

        return await asyncio.to_thread(self._extract_raster_preview_frame, media_bytes)

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
            probe = await self._probe_video_file(input_path, expected_mime_type=content_type)

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
            width=probe.width,
            height=probe.height,
            file_size_bytes=len(media_bytes),
            perceptual_hash=perceptual_hash,
        )

    async def _probe_video_file(self, path: Path, *, expected_mime_type: str) -> _VideoProbeResult:
        args = (
            self._settings.pipeline_ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height:format=format_name",
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
        streams = payload.get("streams")
        format_payload = payload.get("format")
        if not isinstance(streams, list) or not isinstance(format_payload, dict):
            raise MediaValidationError("FFprobe returned malformed media metadata.")
        video_stream = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if video_stream is None:
            raise MediaValidationError("Uploaded file does not contain a readable video stream.")
        width = _coerce_positive_int(video_stream.get("width"), field_name="video width")
        height = _coerce_positive_int(video_stream.get("height"), field_name="video height")
        format_name = format_payload.get("format_name")
        if not isinstance(format_name, str) or not format_name.strip():
            raise MediaValidationError("FFprobe did not report a recognizable video container.")
        normalized_format_name = format_name.lower()
        if expected_mime_type == "video/webm" and "webm" not in normalized_format_name:
            raise MediaValidationError("Uploaded media bytes do not match the declared video/webm content type.")
        if expected_mime_type in {"video/mp4", "video/quicktime"} and not any(
            token in normalized_format_name for token in ("mp4", "mov", "3gp", "3g2", "mj2")
        ):
            raise MediaValidationError(
                f"Uploaded media bytes do not match the declared {expected_mime_type!r} content type."
            )
        return _VideoProbeResult(width=width, height=height)

    def _ffmpeg_command(self, *, input_path: Path, output_path: Path, content_type: str) -> tuple[str, ...]:
        scale_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2:force_original_aspect_ratio=decrease"
        if content_type in _VIDEO_MIME_TYPES or content_type == "image/gif":
            return (
                self._settings.pipeline_ffmpeg_binary,
                "-y",
                "-i",
                str(input_path),
                "-vf",
                f"{scale_filter},fps=15,format=yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                str(output_path),
            )

        return (
            self._settings.pipeline_ffmpeg_binary,
            "-y",
            "-loop",
            "1",
            "-i",
            str(input_path),
            "-t",
            f"{_DEFAULT_IMAGE_DURATION_SECONDS:.2f}",
            "-vf",
            f"{scale_filter},format=yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
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
class _VideoProbeResult:
    width: int
    height: int


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
        raise MediaValidationError(
            f"Filename extension .{extension} does not match uploaded media type {mime_type!r}."
        )


def _suffix_for_filename(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix
    if suffix:
        return suffix.lower()
    return ".bin"


def _load_image_from_bytes(image_bytes: bytes) -> Image.Image:
    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.convert("RGB")


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
