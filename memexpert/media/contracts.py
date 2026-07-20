"""Import-safe media contracts shared by API code and worker implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from memexpert.models.enums import ContentKind


WEB_VIDEO_PROFILE_ID: Final = "web-h264-aac-1080p30-v2"
SUPPORTED_MOVING_MEDIA_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "image/gif",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    }
)


@dataclass(frozen=True, slots=True)
class WebVideoProfile:
    """Immutable browser-video encoding profile used by every generation."""

    id: str
    landscape_max_width: int
    landscape_max_height: int
    portrait_max_width: int
    portrait_max_height: int
    square_max_dimension: int
    maximum_frame_rate: int
    video_codec: str
    video_profile: str
    video_level: str
    pixel_format: str
    preset: str
    crf: int
    maximum_video_bit_rate: int
    video_buffer_size: int
    maximum_gop_seconds: float
    audio_codec: str
    audio_profile: str
    audio_bit_rate: int
    audio_sample_rate: int
    audio_channels: int


WEB_VIDEO_PROFILE: Final = WebVideoProfile(
    id=WEB_VIDEO_PROFILE_ID,
    landscape_max_width=1920,
    landscape_max_height=1080,
    portrait_max_width=1080,
    portrait_max_height=1920,
    square_max_dimension=1080,
    maximum_frame_rate=30,
    video_codec="h264",
    video_profile="High",
    video_level="4.1",
    pixel_format="yuv420p",
    preset="medium",
    crf=21,
    maximum_video_bit_rate=6_000_000,
    video_buffer_size=12_000_000,
    maximum_gop_seconds=2.0,
    audio_codec="aac",
    audio_profile="LC",
    audio_bit_rate=128_000,
    audio_sample_rate=48_000,
    audio_channels=2,
)


class WebVideoFrameRateMode(StrEnum):
    """How a source frame-rate observation affects the generated video."""

    PRESERVE = "preserve"
    CAP_30 = "cap_30"
    NORMALIZE_30 = "normalize_30"


@dataclass(frozen=True, slots=True)
class MediaFrameRate:
    """Exact positive rational frame rate reported by FFprobe."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("Media frame-rate components must be positive.")

    @property
    def frames_per_second(self) -> float:
        return self.numerator / self.denominator


@dataclass(frozen=True, slots=True)
class VideoStreamObservation:
    """Sanitized FFprobe observations for one video stream."""

    index: int
    codec_name: str | None
    profile: str | None
    level: int | None
    pixel_format: str | None
    width: int | None
    height: int | None
    average_frame_rate: MediaFrameRate | None
    real_frame_rate: MediaFrameRate | None
    start_time_seconds: float | None
    duration_seconds: float | None
    bit_rate: int | None
    frame_count: int | None
    rotation_degrees: int = 0

    @property
    def display_width(self) -> int | None:
        if self.width is None or self.height is None:
            return None
        if self.rotation_degrees % 180:
            return self.height
        return self.width

    @property
    def display_height(self) -> int | None:
        if self.width is None or self.height is None:
            return None
        if self.rotation_degrees % 180:
            return self.width
        return self.height


@dataclass(frozen=True, slots=True)
class AudioStreamObservation:
    """Sanitized FFprobe observations for one audio stream."""

    index: int
    codec_name: str | None
    profile: str | None
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    start_time_seconds: float | None
    duration_seconds: float | None
    bit_rate: int | None


@dataclass(frozen=True, slots=True)
class MediaProbeObservations:
    """Durable, sanitized container and stream observations from FFprobe."""

    format_names: tuple[str, ...]
    format_long_name: str | None
    start_time_seconds: float | None
    duration_seconds: float | None
    bit_rate: int | None
    byte_size: int
    video_streams: tuple[VideoStreamObservation, ...]
    audio_streams: tuple[AudioStreamObservation, ...]
    subtitle_stream_count: int
    data_stream_count: int
    attachment_stream_count: int
    unknown_stream_types: tuple[str, ...]
    chapter_count: int

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_streams)

    @property
    def primary_video(self) -> VideoStreamObservation | None:
        return self.video_streams[0] if self.video_streams else None


@dataclass(frozen=True, slots=True)
class UploadMediaDetails:
    """Validated metadata derived from uploaded media bytes before ingest persistence."""

    media_type: ContentKind
    mime_type: str
    width: int
    height: int
    file_size_bytes: int
    perceptual_hash: str
    source_has_audio: bool | None = None
    source_observations: MediaProbeObservations | None = None


@dataclass(frozen=True, slots=True)
class NormalizedMediaResult:
    """Optional playback derivative and display metadata from media preparation."""

    quality_score: float
    blur_hash: str | None
    preview_image_object_key: str | None = None
    preview_image_bytes: bytes | None = None
    web_video_object_key: str | None = None
    web_video_bytes: bytes | None = None
    generation_id: uuid.UUID | None = None
    web_video_profile: str | None = None
    frame_rate_mode: WebVideoFrameRateMode | None = None
    source_has_audio: bool | None = None
    web_video_has_audio: bool | None = None
    source_observations: MediaProbeObservations | None = None
    output_observations: MediaProbeObservations | None = None
    web_video_verified_at: datetime | None = None


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
        generation_id: uuid.UUID | None = None,
    ) -> NormalizedMediaResult: ...

    async def extract_preview_frame(
        self,
        *,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> bytes: ...


__all__ = [
    "AudioStreamObservation",
    "CommandResult",
    "MediaFrameRate",
    "MediaCommandRunner",
    "MediaProbeObservations",
    "MediaProcessingError",
    "MediaTimeoutError",
    "MediaValidationError",
    "NormalizedMediaResult",
    "PipelineMediaProcessorProtocol",
    "SUPPORTED_MOVING_MEDIA_MIME_TYPES",
    "UploadMediaDetails",
    "VideoStreamObservation",
    "WEB_VIDEO_PROFILE",
    "WEB_VIDEO_PROFILE_ID",
    "WebVideoFrameRateMode",
    "WebVideoProfile",
]
