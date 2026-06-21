"""Import-safe media contracts shared by API code and worker implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid

    from memexpert.models.enums import ContentKind


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
    """Optional playback derivative and display metadata from media preparation."""

    quality_score: float
    blur_hash: str | None
    web_video_object_key: str | None = None
    web_video_bytes: bytes | None = None


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


__all__ = [
    "CommandResult",
    "MediaCommandRunner",
    "MediaProcessingError",
    "MediaTimeoutError",
    "MediaValidationError",
    "NormalizedMediaResult",
    "PipelineMediaProcessorProtocol",
    "UploadMediaDetails",
]
