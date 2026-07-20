# ruff: noqa: TC003
"""Lazy S3-compatible storage helpers for the content pipeline runtime."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from memexpert.core.config import Settings, get_settings

DEFAULT_STORAGE_CONNECTION_TIMEOUT_SECONDS: Final = 5.0
SUPPORTED_S3_ENDPOINT_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_VALID_BUCKET_CHARS_RE: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_VALID_EXTENSION_RE: Final = re.compile(r"^[a-z0-9]{1,10}$")
_MISSING_OBJECT_ERROR_CODES: Final = frozenset({"404", "nosuchkey", "notfound"})


@dataclass(frozen=True, slots=True)
class PipelineStorageSettings:
    """Normalized S3-compatible storage settings used by the content pipeline runtime."""

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    original_prefix: str
    temp_original_prefix: str
    derivative_prefix: str
    connection_timeout: float


@dataclass(frozen=True, slots=True)
class MediaGenerationObjectKey:
    """Recognized immutable derivative object-key components."""

    meme_file_id: uuid.UUID
    generation_id: uuid.UUID
    artifact_name: str


class StorageConfigurationError(ValueError):
    """Raised when S3-compatible settings cannot produce a storage client contract."""


class StorageConnectionError(RuntimeError):
    """Raised when the S3-compatible runtime cannot establish a real connection."""


class StorageObjectMissingError(RuntimeError):
    """Raised when S3 definitively reports that a requested object does not exist."""


class StorageObjectPresence(StrEnum):
    """Tri-state result for a non-mutating object-presence probe."""

    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


_s3_client: Any | None = None


def normalize_s3_endpoint(s3_endpoint: str) -> str:
    """Normalize and validate S3-compatible endpoints before constructing a client."""

    normalized_input = s3_endpoint.strip().rstrip("/")
    if not normalized_input:
        raise StorageConfigurationError(
            "S3 endpoint is required before constructing the storage client.",
        )

    parsed_url = urlparse(normalized_input)
    if parsed_url.scheme not in SUPPORTED_S3_ENDPOINT_SCHEMES:
        raise StorageConfigurationError("S3 endpoint must use http:// or https://.")

    try:
        port = parsed_url.port
    except ValueError as exc:
        raise StorageConfigurationError("S3 endpoint contains an invalid port.") from exc

    if parsed_url.hostname is None:
        raise StorageConfigurationError("S3 endpoint must include a hostname.")
    if port is not None and port <= 0:
        raise StorageConfigurationError("S3 endpoint port must be greater than zero.")

    return normalized_input


def normalize_s3_bucket_name(bucket_name: str) -> str:
    """Normalize and validate bucket names used for original/derived object storage."""

    normalized_bucket_name = bucket_name.strip().lower()
    if not normalized_bucket_name:
        raise StorageConfigurationError("S3 bucket name must not be blank.")
    if len(normalized_bucket_name) < 3 or len(normalized_bucket_name) > 63:
        raise StorageConfigurationError("S3 bucket name must be between 3 and 63 characters long.")
    if _VALID_BUCKET_CHARS_RE.fullmatch(normalized_bucket_name) is None:
        raise StorageConfigurationError(
            "S3 bucket name may contain only lowercase letters, numbers, dots, and hyphens.",
        )
    if normalized_bucket_name.count(".") == 3 and all(part.isdigit() for part in normalized_bucket_name.split(".")):
        raise StorageConfigurationError("S3 bucket name must not be formatted like an IP address.")
    return normalized_bucket_name


def normalize_object_key_prefix(prefix: str, *, field_name: str) -> str:
    """Normalize and validate object-key prefixes used for durable storage paths."""

    normalized_prefix = prefix.strip().strip("/")
    if not normalized_prefix:
        raise StorageConfigurationError(f"{field_name} must not be blank.")

    segments = tuple(segment for segment in normalized_prefix.split("/") if segment)
    if not segments or any(segment in {".", ".."} for segment in segments):
        raise StorageConfigurationError(f"{field_name} must not contain empty, '.' , or '..' path segments.")

    return "/".join(segments)


def _normalize_required_text(value: str, *, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise StorageConfigurationError(f"{field_name} must not be blank.")
    return normalized_value


def _normalize_extension(filename: str) -> str:
    basename = PurePosixPath(filename).name
    _, raw_extension = os.path.splitext(basename)
    normalized_extension = raw_extension.lstrip(".").lower()
    if not normalized_extension:
        return "bin"
    if _VALID_EXTENSION_RE.fullmatch(normalized_extension) is None:
        return "bin"
    return normalized_extension


def get_pipeline_storage_settings(settings: Settings | None = None) -> PipelineStorageSettings:
    """Return the normalized S3-compatible storage settings for pipeline writes."""

    resolved_settings = settings or get_settings()
    return PipelineStorageSettings(
        endpoint_url=normalize_s3_endpoint(resolved_settings.s3_endpoint),
        access_key=_normalize_required_text(resolved_settings.s3_access_key, field_name="s3_access_key"),
        secret_key=_normalize_required_text(resolved_settings.s3_secret_key, field_name="s3_secret_key"),
        bucket=normalize_s3_bucket_name(resolved_settings.s3_bucket),
        region=_normalize_required_text(resolved_settings.s3_region, field_name="s3_region"),
        original_prefix=normalize_object_key_prefix(
            resolved_settings.pipeline_s3_original_prefix,
            field_name="pipeline_s3_original_prefix",
        ),
        temp_original_prefix=normalize_object_key_prefix(
            resolved_settings.pipeline_s3_temp_original_prefix,
            field_name="pipeline_s3_temp_original_prefix",
        ),
        derivative_prefix=normalize_object_key_prefix(
            resolved_settings.pipeline_s3_derivative_prefix,
            field_name="pipeline_s3_derivative_prefix",
        ),
        connection_timeout=resolved_settings.pipeline_storage_connection_timeout_seconds,
    )


def build_s3_client(settings: Settings | None = None) -> Any:
    """Build a lazy boto3 S3 client without performing network I/O."""

    storage_settings = get_pipeline_storage_settings(settings)
    client_config = Config(
        signature_version="s3v4",
        connect_timeout=storage_settings.connection_timeout,
        read_timeout=storage_settings.connection_timeout,
        s3={"addressing_style": "path"},
    )

    try:
        return boto3.client(
            "s3",
            endpoint_url=storage_settings.endpoint_url,
            aws_access_key_id=storage_settings.access_key,
            aws_secret_access_key=storage_settings.secret_key,
            region_name=storage_settings.region,
            config=client_config,
        )
    except (TypeError, ValueError) as exc:
        raise StorageConfigurationError(
            "Unable to construct the S3-compatible client from the configured storage settings.",
        ) from exc


def get_s3_client() -> Any:
    """Return the process-wide S3-compatible client, creating it lazily."""

    global _s3_client
    if _s3_client is None:
        _s3_client = build_s3_client()
    return _s3_client


def is_s3_client_initialized() -> bool:
    """Expose whether the process-wide S3-compatible client has been created yet."""

    return _s3_client is not None


def build_original_object_key(
    meme_file_id: uuid.UUID,
    original_filename: str,
    *,
    settings: Settings | None = None,
) -> str:
    """Build the stable key used to persist an uploaded original asset."""

    storage_settings = get_pipeline_storage_settings(settings)
    normalized_extension = _normalize_extension(original_filename)
    return f"{storage_settings.original_prefix}/{meme_file_id}/original.{normalized_extension}"


def build_temp_original_object_key(
    ingest_request_id: uuid.UUID,
    original_filename: str,
    *,
    settings: Settings | None = None,
) -> str:
    """Build the temporary key used for raw originals awaiting worker inspection."""

    storage_settings = get_pipeline_storage_settings(settings)
    normalized_extension = _normalize_extension(original_filename)
    return f"{storage_settings.temp_original_prefix}/{ingest_request_id}/original.{normalized_extension}"


def build_web_video_object_key(
    meme_file_id: uuid.UUID,
    *,
    extension: str = "mp4",
    settings: Settings | None = None,
) -> str:
    """Build the stable key used to persist a transcode-derived web video."""

    storage_settings = get_pipeline_storage_settings(settings)
    normalized_extension = _normalize_extension(f"artifact.{extension}")
    return f"{storage_settings.derivative_prefix}/{meme_file_id}/web.{normalized_extension}"


def build_preview_image_object_key(
    meme_file_id: uuid.UUID,
    *,
    extension: str = "png",
    settings: Settings | None = None,
) -> str:
    """Build the stable key used to persist a moving-media preview frame."""

    storage_settings = get_pipeline_storage_settings(settings)
    normalized_extension = _normalize_extension(f"artifact.{extension}")
    return f"{storage_settings.derivative_prefix}/{meme_file_id}/preview.{normalized_extension}"


def build_web_video_generation_object_key(
    meme_file_id: uuid.UUID,
    generation_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> str:
    """Build one immutable web-video generation key."""

    storage_settings = get_pipeline_storage_settings(settings)
    return (
        f"{storage_settings.derivative_prefix}/{meme_file_id}/generations/"
        f"{generation_id}/web.mp4"
    )


def build_preview_image_generation_object_key(
    meme_file_id: uuid.UUID,
    generation_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> str:
    """Build the immutable poster key paired with one web-video generation."""

    storage_settings = get_pipeline_storage_settings(settings)
    return (
        f"{storage_settings.derivative_prefix}/{meme_file_id}/generations/"
        f"{generation_id}/preview.png"
    )


def derive_preview_image_object_key(
    web_video_object_key: str,
    *,
    meme_file_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> str:
    """Derive a video's sibling poster, retaining legacy file-id compatibility."""

    normalized_key = web_video_object_key.strip().strip("/")
    if not normalized_key:
        raise StorageConfigurationError("web_video_object_key must not be blank.")
    video_path = PurePosixPath(normalized_key)
    if video_path.name == "web.mp4":
        return str(video_path.with_name("preview.png"))
    if meme_file_id is None:
        raise StorageConfigurationError(
            "A non-canonical web-video key requires meme_file_id for legacy poster fallback."
        )
    return build_preview_image_object_key(meme_file_id, settings=settings)


def parse_media_generation_object_key(
    object_key: str,
    *,
    settings: Settings | None = None,
) -> MediaGenerationObjectKey | None:
    """Parse only recognized immutable generation keys; unknown objects stay opaque."""

    storage_settings = get_pipeline_storage_settings(settings)
    pattern = re.compile(
        rf"^{re.escape(storage_settings.derivative_prefix)}/"
        r"(?P<file_id>[0-9a-fA-F-]{36})/generations/"
        r"(?P<generation_id>[0-9a-fA-F-]{36})/"
        r"(?P<artifact_name>web\.mp4|preview\.png)$"
    )
    match = pattern.fullmatch(object_key.strip().strip("/"))
    if match is None:
        return None
    try:
        meme_file_id = uuid.UUID(match.group("file_id"))
        generation_id = uuid.UUID(match.group("generation_id"))
    except ValueError:
        return None
    return MediaGenerationObjectKey(
        meme_file_id=meme_file_id,
        generation_id=generation_id,
        artifact_name=match.group("artifact_name"),
    )


def media_object_version_token(object_key: str) -> str:
    """Return a non-reversible cache version derived from an active object key."""

    normalized_key = object_key.strip()
    if not normalized_key:
        raise StorageConfigurationError("object_key must not be blank.")
    return hashlib.sha256(normalized_key.encode()).hexdigest()[:16]


async def download_object_bytes(
    client: Any,
    *,
    bucket: str,
    key: str,
) -> bytes:
    """Read one object from S3-compatible storage into memory."""

    try:
        response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
    except Exception as exc:
        if is_missing_storage_object_error(exc):
            raise StorageObjectMissingError(
                "The requested storage object no longer exists."
            ) from exc
        raise
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise StorageConnectionError(f"S3 object {key} did not return a readable body.")

    try:
        object_bytes = await asyncio.to_thread(body.read)
    except Exception as exc:  # pragma: no cover - boto body implementations vary at runtime.
        raise StorageConnectionError(f"Failed to read S3 object {key}: {exc}") from exc
    finally:
        close_method = getattr(body, "close", None)
        if callable(close_method):
            close_method()

    if not isinstance(object_bytes, bytes):
        raise StorageConnectionError(f"S3 object {key} returned a non-bytes payload.")
    return object_bytes


def is_missing_storage_object_error(exc: BaseException) -> bool:
    """Return whether an S3-compatible error definitively means object absence.

    Access failures, timeouts, endpoint failures, and unknown provider errors
    deliberately return ``False`` so callers never convert an outage into a
    destructive "missing object" decision.
    """

    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    raw_code = error.get("Code") if isinstance(error, dict) else None
    if isinstance(raw_code, (str, int)) and str(raw_code).strip().lower() in _MISSING_OBJECT_ERROR_CODES:
        return True
    metadata = response.get("ResponseMetadata")
    raw_status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return raw_status == 404 or raw_status == "404"


async def check_object_presence(
    client: Any,
    *,
    bucket: str,
    key: str,
    timeout: float | None = None,
) -> StorageObjectPresence:
    """HEAD one object without conflating absence with storage unavailability."""

    try:
        if timeout is None:
            await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
        else:
            async with asyncio.timeout(timeout):
                await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
    except Exception as exc:
        if is_missing_storage_object_error(exc):
            return StorageObjectPresence.MISSING
        return StorageObjectPresence.UNAVAILABLE
    return StorageObjectPresence.PRESENT


async def check_pipeline_object_presence(
    object_key: str,
    *,
    client: Any | None = None,
    settings: Settings | None = None,
) -> StorageObjectPresence:
    """Lazily HEAD one object using the configured pipeline storage boundary."""

    storage_settings = get_pipeline_storage_settings(settings)
    resolved_client = client or get_s3_client()
    return await check_object_presence(
        resolved_client,
        bucket=storage_settings.bucket,
        key=object_key,
        timeout=storage_settings.connection_timeout,
    )


async def upload_object_bytes(
    client: Any,
    *,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
) -> None:
    """Write one object to S3-compatible storage from in-memory bytes."""

    try:
        await asyncio.to_thread(
            client.put_object,
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ContentLength=len(body),
        )
    except Exception as exc:  # pragma: no cover - boto exceptions vary by backend.
        raise StorageConnectionError(f"Failed to store S3 object {key}: {exc}") from exc


async def delete_object_if_present(
    client: Any,
    *,
    bucket: str,
    key: str,
) -> None:
    """Best-effort object deletion used to clean up failed derivative writes."""

    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
    except Exception:
        return


async def verify_s3_storage(
    client: Any | None = None,
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
) -> PipelineStorageSettings:
    """Verify storage connectivity and bucket access with a bounded timeout."""

    storage_settings = get_pipeline_storage_settings(settings)
    resolved_timeout = timeout or storage_settings.connection_timeout
    resolved_client = client or build_s3_client(settings)

    try:
        async with asyncio.timeout(resolved_timeout):
            await asyncio.to_thread(resolved_client.head_bucket, Bucket=storage_settings.bucket)
    except TimeoutError as exc:
        raise StorageConnectionError(
            f"Timed out after {resolved_timeout:.2f}s while connecting to S3 bucket {storage_settings.bucket}.",
        ) from exc
    except (ConnectTimeoutError, ReadTimeoutError) as exc:
        raise StorageConnectionError(
            f"Timed out after {resolved_timeout:.2f}s while connecting to S3 bucket {storage_settings.bucket}: {exc}",
        ) from exc
    except (EndpointConnectionError, ClientError, BotoCoreError) as exc:
        raise StorageConnectionError(
            f"Unable to verify S3 bucket {storage_settings.bucket} at {storage_settings.endpoint_url}: {exc}",
        ) from exc

    return storage_settings


def reset_s3_client_state() -> None:
    """Dispose the cached S3 client so test/runtime state cannot leak."""

    global _s3_client

    cached_client = _s3_client
    _s3_client = None

    if cached_client is None:
        return

    close_method = getattr(cached_client, "close", None)
    if callable(close_method):
        close_method()


__all__ = [
    "DEFAULT_STORAGE_CONNECTION_TIMEOUT_SECONDS",
    "PipelineStorageSettings",
    "MediaGenerationObjectKey",
    "StorageConfigurationError",
    "StorageConnectionError",
    "StorageObjectMissingError",
    "StorageObjectPresence",
    "build_original_object_key",
    "build_preview_image_generation_object_key",
    "build_preview_image_object_key",
    "build_s3_client",
    "build_temp_original_object_key",
    "build_web_video_generation_object_key",
    "build_web_video_object_key",
    "check_object_presence",
    "check_pipeline_object_presence",
    "delete_object_if_present",
    "download_object_bytes",
    "derive_preview_image_object_key",
    "get_pipeline_storage_settings",
    "get_s3_client",
    "is_s3_client_initialized",
    "is_missing_storage_object_error",
    "media_object_version_token",
    "normalize_object_key_prefix",
    "normalize_s3_bucket_name",
    "normalize_s3_endpoint",
    "parse_media_generation_object_key",
    "reset_s3_client_state",
    "upload_object_bytes",
    "verify_s3_storage",
]
