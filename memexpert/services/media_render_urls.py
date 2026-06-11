"""Build safe public media render/download URL contracts for visible meme files."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

from memexpert.core.config import Settings, get_settings
from memexpert.schemas.meme import PublicMemeFileRenderRead

if TYPE_CHECKING:
    import uuid

    from pydantic import SecretStr

    from memexpert.models.content import MemeFile

_IMAGE_MIME_PREFIX = "image/"
_SAFE_FILENAME_RE = re.compile(r"[^a-z0-9._-]+")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True, slots=True)
class PublicMediaRenderContext:
    """Public meme metadata used for SEO-friendly filenames."""

    meme_id: uuid.UUID
    seo_slug: str | None = None
    caption: str | None = None


class MediaRenderUrlService:
    """Construct public render contracts without exposing object keys in DTO fields."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._imgproxy_base_url = self._settings.imgproxy_base_url.rstrip("/")
        self._imgproxy_key = _decode_secret_bytes(self._settings.imgproxy_key)
        self._imgproxy_salt = _decode_secret_bytes(self._settings.imgproxy_salt)
        self._public_media_base_url = (
            self._settings.media_public_base_url.rstrip("/") if self._settings.media_public_base_url else None
        )

    def build_render(self, file: MemeFile, *, context: PublicMediaRenderContext) -> PublicMemeFileRenderRead:
        """Build render URLs for one already-visible meme file."""

        render = PublicMemeFileRenderRead(width=file.width, height=file.height, blur_hash=file.blur_hash)
        if _is_image_mime(file.mime_type):
            extension = _extension_for_file(file, default="jpg")
            filename = _download_filename(file=file, context=context, extension=extension)
            render.thumbnail_url = self._imgproxy_url(
                file.s3_original_key,
                options=("rs:fill:360:360", "g:sm", "f:webp"),
                extension="webp",
            )
            render.preview_url = self._imgproxy_url(
                file.s3_original_key,
                options=("rs:fit:900:900", "f:webp"),
                extension="webp",
            )
            render.display_url = render.preview_url
            render.original_url = self._imgproxy_url(
                file.s3_original_key,
                options=("rs:fit:1600:1600",),
                extension=extension,
            )
            render.download_url = self._imgproxy_url(
                file.s3_original_key,
                options=("rs:fit:4096:4096", "att:1", f"fn:{_base64url(filename)}:1"),
                extension=extension,
            )

        if file.s3_web_video_key:
            render.web_video_url = self._public_file_url(file.id, variant="web-video.mp4")
            if render.download_url is None:
                render.download_url = render.web_video_url
        return render

    def _imgproxy_url(self, object_key: str, *, options: tuple[str, ...], extension: str) -> str:
        source_url = f"s3://{self._settings.s3_bucket}/{object_key}"
        encoded_source = _base64url(source_url)
        path = f"/{'/'.join(options)}/{encoded_source}.{extension}"
        signature = self._signature(path)
        return f"{self._imgproxy_base_url}/{signature}{path}"

    def _signature(self, path: str) -> str:
        if self._imgproxy_key is None or self._imgproxy_salt is None:
            return "unsafe"
        digest = hmac.new(self._imgproxy_key, self._imgproxy_salt + path.encode(), hashlib.sha256).digest()
        return _base64url_bytes(digest)

    def _public_file_url(self, file_id: uuid.UUID, *, variant: str) -> str | None:
        if self._public_media_base_url is None:
            return None
        return f"{self._public_media_base_url}/{file_id}/{quote(variant, safe='')}"


def _is_image_mime(mime_type: str | None) -> bool:
    return bool(mime_type and mime_type.lower().startswith(_IMAGE_MIME_PREFIX))


def _extension_for_file(file: MemeFile, *, default: str) -> str:
    if file.mime_type:
        mime_extension = file.mime_type.lower().split("/", maxsplit=1)[-1]
        if mime_extension == "jpeg":
            return "jpg"
        if re.fullmatch(r"[a-z0-9]{1,10}", mime_extension):
            return mime_extension
    suffix = PurePosixPath(file.s3_original_key).suffix.lstrip(".").lower()
    if re.fullmatch(r"[a-z0-9]{1,10}", suffix):
        return suffix
    return default


def _download_filename(*, file: MemeFile, context: PublicMediaRenderContext, extension: str) -> str:
    stem_source = context.seo_slug or f"meme-{context.meme_id}-file-{file.id}"
    stem = _safe_filename_stem(stem_source)
    return f"{stem}.{extension}"


def _safe_filename_stem(value: str) -> str:
    normalized = _SAFE_FILENAME_RE.sub("-", value.strip().lower()).strip(".-_")
    return normalized or "meme-file"


def _base64url(value: str) -> str:
    return _base64url_bytes(value.encode())


def _base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode_secret_bytes(secret: SecretStr | None) -> bytes | None:
    if secret is None:
        return None
    value = secret.get_secret_value().strip()
    if not value:
        return None
    if len(value) % 2 == 0 and _HEX_RE.fullmatch(value) is not None:
        return bytes.fromhex(value)
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


__all__ = ["MediaRenderUrlService", "PublicMediaRenderContext"]
